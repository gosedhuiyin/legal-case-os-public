"""Read existing source-map metadata and validate current, local template links.

This does not search the knowledge library, read template originals, register an
asset, or prove production approval. Callers use the hash of verified original
bytes and must recheck profile bytes when using a returned path.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from .core import LegalCaseError


def _error(code: str, message: str) -> LegalCaseError:
    return LegalCaseError(code, message)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read(path: Path) -> tuple[dict[str, Any], str]:
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise _error("SOURCE_LINK_READ_FAILED", f"来源关联元信息不可读：{path}") from exc
    if not isinstance(value, dict):
        raise _error("SOURCE_LINK_INVALID", "来源关联元信息必须为对象。")
    return value, hashlib.sha256(data).hexdigest()


def _within(root: Path, base: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or PureWindowsPath(relative).drive or relative.startswith(("/", "\\")):
        raise _error("SOURCE_LINK_PATH_INVALID", "关联路径必须是套件内的相对路径。")
    target = (base / relative.replace("\\", "/")).resolve()
    if not target.is_relative_to(root):
        raise _error("SOURCE_LINK_PATH_INVALID", "关联路径超出指定套件目录。")
    return target


def resolve_source_links(source_sha256: str, source_map_path: Path, sop_root: Path) -> dict[str, Any]:
    """Resolve only existing map matches; return verified links and rejected reasons.

The mapping is an index, not authorization. A link needs the current active
catalog entry, matching template version, and exact current profile bytes.
No match means only that this saved map has no verified link for those bytes.
"""
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
        raise _error("SOURCE_LINK_HASH_INVALID", "需要已验证原件的完整 SHA256。")
    source_sha256 = source_sha256.lower()
    root = Path(sop_root).resolve()
    map_path = Path(source_map_path).resolve()
    mapping, map_sha = _read(map_path)
    if not isinstance(mapping.get("matches"), list) or not isinstance(mapping.get("scope"), dict):
        raise _error("SOURCE_LINK_INVALID", "来源映射缺少 matches 或 scope。")
    catalog_path = _within(root, root, mapping["scope"].get("catalog_sop_relative_path"))
    catalog, catalog_sha = _read(catalog_path)
    if not isinstance(catalog.get("templates"), list):
        raise _error("SOURCE_LINK_INVALID", "当前模板目录缺少 templates。")
    verified, rejected, seen = [], [], set()
    matches = [row for row in mapping["matches"] if isinstance(row, dict) and row.get("sha256") == source_sha256]
    for row in matches:
        bindings = row.get("active_template_bindings", [])
        if not isinstance(bindings, list):
            raise _error("SOURCE_LINK_INVALID", "来源映射的画像关联列表无效。")
        for binding in bindings:
            template_id = binding.get("active_template_id") if isinstance(binding, dict) else None
            try:
                if not template_id:
                    raise _error("SOURCE_LINK_INVALID", "来源映射缺少模板 ID。")
                entries = [entry for entry in catalog["templates"] if isinstance(entry, dict) and entry.get("id") == template_id]
                if len(entries) != 1:
                    raise _error("SOURCE_LINK_CATALOG_CHANGED", "当前目录中的模板不存在或 ID 不唯一。")
                entry = entries[0]
                if (entry.get("status") != "active" or entry.get("approved_final") is not True
                    or _object(entry.get("authorization")).get("status") != "verified"):
                    raise _error("SOURCE_LINK_INACTIVE", "模板当前不处于已授权启用状态。")
                if (entry.get("version") != binding.get("template_version")
                    or entry.get("usage_mode") != binding.get("usage_mode")
                    or entry.get("sha256") != source_sha256
                    or binding.get("active_source_sha256") != source_sha256):
                    raise _error("SOURCE_LINK_CATALOG_CHANGED", "目录版本、模式或原件哈希已变化。")
                profile_path = _within(root, catalog_path.parent, entry.get("profile_path"))
                mapped_profile = _within(root, root, binding.get("profile_sop_relative_path"))
                if profile_path != mapped_profile:
                    raise _error("SOURCE_LINK_CATALOG_CHANGED", "画像路径已变化。")
                profile, profile_sha = _read(profile_path)
                if any(value != profile_sha for value in (entry.get("profile_sha256"), binding.get("catalog_profile_sha256"), binding.get("actual_profile_sha256"))):
                    raise _error("SOURCE_LINK_PROFILE_CHANGED", "画像文件与目录或来源映射哈希不一致。")
                if (profile.get("template_id") != template_id or _object(profile.get("source")).get("sha256") != source_sha256
                    or profile.get("status") != "active" or profile.get("usage_mode") != entry["usage_mode"]
                    or _object(profile.get("approval")).get("target_version") != entry["version"]):
                    raise _error("SOURCE_LINK_PROFILE_IDENTITY", "画像内部来源、状态或批准目标版本不一致。")
                key = (template_id, entry["version"], profile_sha)
                if key in seen:
                    continue
                seen.add(key)
                verified.append({"template_id": template_id, "template_version": entry["version"],
                    "usage_mode": entry["usage_mode"], "source_sha256": source_sha256,
                    "knowledge_relative_path": row.get("knowledge_relative_path"),
                    "profile_path": str(profile_path), "profile_sha256": profile_sha,
                    "profile_id": profile.get("profile_id"), "profile_hash_recheck_required_on_use": True,
                    "link_status": "current_catalog_and_profile_hash_verified"})
            except LegalCaseError as exc:
                rejected.append({"template_id": template_id, "code": exc.code, "reason": exc.message})
    # Report a concurrent change instead of returning links checked against an
    # obsolete catalog or map. Profile paths still require hash checks on use.
    if _read(map_path)[1] != map_sha or _read(catalog_path)[1] != catalog_sha:
        raise _error("SOURCE_LINK_METADATA_CHANGED", "校验期间来源映射或模板目录发生变化，请重试。")
    return {"schema_version": "1.0.0", "source_sha256": source_sha256,
        "source_map_path": str(map_path), "source_map_sha256": map_sha,
        "catalog_path": str(catalog_path), "catalog_sha256": catalog_sha,
        "catalog_matches_map_snapshot": catalog_sha == mapping["scope"].get("catalog_sha256"),
        "matched_map_rows": len(matches), "verified_links": verified, "rejected_links": rejected,
        "scope": mapping.get("scope"), "production_approval_checked": False,
        "note": "仅在已保存映射范围内关联同字节来源；不代表已核验本案适用性、正式生产批准链或全库不存在其他版本。"}
