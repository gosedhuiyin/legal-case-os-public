"""Offline, byte-preserving local delivery versions; never a filing approval."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

from .core import (LegalCaseError, atomic_write_json, canonical_json,
                   exclusive_file_lock, sha256_bytes, sha256_file)

_PROTECTED = {"library", "00-originals", "secrets", ".ssh"}
_SUFFIXES = {".docx", ".pdf", ".md", ".txt", ".csv", ".xlsx"}
_VERSION = re.compile(r"v-[a-f0-9]{64}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_LIMITATIONS = ["仅登记本机成果版本；不代表正式定稿、法院提交或外部发送批准。",
                "文件哈希核验只确认字节一致；实质审阅、版式复核保留原任务记录。"]


def _fail(code: str, message: str) -> None:
    raise LegalCaseError("DELIVERY_" + code, message)


def _path(path: Path | str) -> Path:
    """Reject protected paths and link aliases, including Windows junctions."""
    lexical = Path(os.path.abspath(path))
    resolved = lexical.resolve()
    for candidate in (lexical, resolved):
        if any(part.casefold() in _PROTECTED for part in candidate.parts):
            _fail("PROTECTED_PATH", "成果版本目录及输入不能位于原件库或秘密目录。")
    for candidate in (lexical, *lexical.parents):
        if candidate.is_symlink() or (candidate.exists() and
                getattr(candidate.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            _fail("LINK_PATH", "成果版本不接受符号链接或目录联接。")
    return resolved


def _file(path: Path) -> Path:
    checked = _path(path)
    if not checked.is_file():
        _fail("FILE_MISSING", f"成果清单中的文件不存在：{path.name}")
    if checked.stat().st_nlink != 1:
        _fail("LINK_PATH", "成果版本不接受硬链接文件。")
    return checked


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LegalCaseError("DELIVERY_INVALID_MANIFEST", "成果记录必须是合法 JSON。") from exc
    if not isinstance(value, dict):
        _fail("INVALID_MANIFEST", "成果记录必须是 JSON 对象。")
    return value


def _manifest(raw: bytes) -> dict:
    manifest = _json(raw)
    files = manifest.get("files")
    if not isinstance(files, dict) or len(files) < 2 or "review.md" not in files:
        _fail("INVALID_MANIFEST", "成果清单须列出正文文件和独立 review.md 的 SHA256。")
    if not isinstance(manifest.get("checks"), dict) or not manifest["checks"]:
        _fail("INVALID_MANIFEST", "成果清单须保留实际核验记录 checks。")
    folded = set()
    for name, digest in files.items():
        if (not isinstance(name, str) or not name or len(name) > 180
                or name.startswith(".") or name[-1] in " ." or
                any(ord(char) < 32 for char in name) or
                any(char in name for char in '/\\:<>"|?*\r\n\t') or
                Path(name).suffix.lower() not in _SUFFIXES or name.casefold() in folded or
                name.casefold() in {"readme.md", "version.json", "manifest.json"}):
            _fail("UNSAFE_FILE", "成果清单只能引用任务目录内的普通文档文件名，不允许路径或保留名称。")
        folded.add(name.casefold())
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            _fail("INVALID_MANIFEST", "每份成果文件必须有完整的小写 SHA256。")
    return manifest


def _identity(raw: bytes, files: dict) -> str:
    return "v-" + sha256_bytes(canonical_json({
        "manifest_sha256": sha256_bytes(raw), "files": files,
    }).encode("utf-8"))


def _copy_verified(source: Path, target: Path, expected_hash: str) -> None:
    source = _file(source)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if sha256_file(target) != expected_hash or sha256_file(_file(source)) != expected_hash:
        _fail("HASH_MISMATCH", f"文件与清单不一致或复制时发生变化：{source.name}")


def _verified_version(store: Path, version_id: str) -> tuple[Path, dict]:
    if not isinstance(version_id, str) or not _VERSION.fullmatch(version_id):
        _fail("INVALID_VERSION", "版本号无效。")
    directory = _path(store / "versions" / version_id)
    raw = _file(directory / "manifest.json").read_bytes()
    manifest = _manifest(raw)
    if _identity(raw, manifest["files"]) != version_id:
        _fail("HASH_MISMATCH", "历史版本的清单已变化，拒绝恢复或覆盖。")
    for name, digest in manifest["files"].items():
        if sha256_file(_file(directory / name)) != digest:
            _fail("HASH_MISMATCH", f"历史版本文件已变化，拒绝恢复或覆盖：{name}")
    return directory, manifest


def _pointer(store: Path) -> dict | None:
    path = _path(store / "current.json")
    if not path.exists():
        return None
    current = _json(_file(path).read_bytes())
    if (not isinstance(current.get("revision"), str) or
            not re.fullmatch(r"[a-f0-9]{32}", current["revision"])):
        _fail("INVALID_CURRENT", "当前版本指针损坏，未修改任何版本。")
    _verified_version(store, current.get("version_id"))
    return current


def _lock_path(store: Path) -> Path:
    path = _path(store / ".delivery.lock")
    return _file(path) if path.exists() else path


def _cas(current: dict | None, expected_current: str | None) -> None:
    if (current or {}).get("revision") != expected_current:
        _fail("STALE_CURRENT", "当前成果已由其他任务改变；请读取当前版本后重新决定，未覆盖成果。")


def _result(store: Path, current: dict | None, operation: str) -> dict:
    result = {"ok": True, "operation": operation, "store_dir": str(store),
              "current": str(store / "current.json"), "revision": None,
              "version_id": None, "files": {}, "checks": {},
              "limitations": list(_LIMITATIONS), "filing_approved": False,
              "external_actions_executed": 0, "model_calls_executed": 0}
    if current:
        directory, manifest = _verified_version(store, current["version_id"])
        result.update(revision=current["revision"], version_id=current["version_id"],
                      version_dir=str(directory), manifest=str(directory / "manifest.json"),
                      files={name: str(directory / name) for name in manifest["files"]},
                      checks=manifest["checks"])
    return result


def _activate(store: Path, version_id: str, current: dict | None, operation: str) -> dict:
    if current and current["version_id"] == version_id:
        return _result(store, current, "unchanged")
    pointer = {"schema_version": "local-delivery-pointer-v1", "version_id": version_id,
               "revision": uuid.uuid4().hex, "label": "本机成果当前版本"}
    # Complete verification happens before the only mutable pointer is replaced.
    result = _result(store, pointer, operation)
    atomic_write_json(_path(store / "current.json"), pointer)
    return result


def get_current_delivery(store_dir: Path | str) -> dict:
    """Return the current revision token and verified, directly openable files."""
    store = _path(store_dir)
    if not store.exists():
        return _result(store, None, "current")
    with exclusive_file_lock(_lock_path(store)):
        return _result(store, _pointer(store), "current")


def publish_delivery(task_dir: Path | str, store_dir: Path | str, *,
                     expected_current: str | None) -> dict:
    """Register exact generated bytes. None means the store must have no current."""
    task, store = _path(task_dir), _path(store_dir)
    if task == store or task in store.parents or store in task.parents:
        _fail("OVERLAPPING_PATHS", "任务目录与版本目录必须彼此独立。")
    raw = _file(task / "manifest.json").read_bytes()
    manifest = _manifest(raw)
    version_id = _identity(raw, manifest["files"])
    store.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(_lock_path(store)):
        current = _pointer(store)
        _cas(current, expected_current)
        versions = _path(store / "versions")
        versions.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".delivery-", dir=store))
        try:
            for name, digest in manifest["files"].items():
                _copy_verified(task / name, staging / name, digest)
            (staging / "manifest.json").write_bytes(raw)
            # Recheck every input after all copies, including changes to earlier files.
            if _file(task / "manifest.json").read_bytes() != raw or any(
                    sha256_file(_file(task / name)) != digest
                    for name, digest in manifest["files"].items()):
                _fail("SOURCE_CHANGED", "任务文件在登记过程中变化，当前成果保持原版本。")
            destination = _path(versions / version_id)
            if destination.exists():
                _verified_version(store, version_id)
            else:
                # Never replace an existing version; current still points to its prior version.
                os.rename(staging, destination)
            return _activate(store, version_id, current, "published")
        finally:
            if staging.exists() and staging.resolve().parent == store and staging.name.startswith(".delivery-"):
                shutil.rmtree(staging)


def restore_delivery(store_dir: Path | str, version_id: str, *,
                     expected_current: str | None) -> dict:
    """Switch only the pointer to a verified existing version; never regenerate."""
    store = _path(store_dir)
    if not store.is_dir():
        _fail("STORE_MISSING", "成果版本目录不存在。")
    with exclusive_file_lock(_lock_path(store)):
        current = _pointer(store)
        _cas(current, expected_current)
        _verified_version(store, version_id)
        return _activate(store, version_id, current, "restored")
