from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .core import LegalCaseError, PROJECT_ROOT, ensure_not_originals, load_json, now_iso, sha256_file


DEFAULT_REGISTRY = PROJECT_ROOT / "shared" / "templates" / "template-registry.json"
DEFAULT_PERSONAL_TEMPLATE_CATALOG = PROJECT_ROOT / "library" / "模板库" / "_registry" / "template-catalog.json"
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
PERSONAL_TEMPLATE_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}
PERSONAL_TEMPLATE_USAGE_MODES = {"reference", "fillable_clone", "hybrid"}


def _find_soffice() -> Path | None:
    """Find a local, non-network LibreOffice converter without installing anything."""
    for command in ("soffice.com", "soffice.exe", "soffice", "libreoffice"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice" / "program" / "soffice.com",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice" / "program" / "soffice.exe",
        Path("/usr/bin/libreoffice"),
        Path("/usr/bin/soffice"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _convert_markdown_to_pdf(markdown: str, output: Path, *, title: str) -> dict[str, Any]:
    """Create a real PDF through a local LibreOffice backend in an ASCII staging path.

    The requested output is written only after conversion, PDF magic and a
    positive page count all pass.  This prevents a failed conversion from
    leaving a text file with a .pdf suffix or a partial court candidate.
    """
    soffice = _find_soffice()
    if soffice is None:
        raise LegalCaseError(
            "PDF_RENDERER_UNAVAILABLE",
            "未找到本地 LibreOffice PDF 渲染后端；未生成伪 PDF。可先生成 DOCX，待受控渲染环境可用后再转换并逐页检查。",
            {"degraded": True, "visual_qa_required": True},
        )
    try:
        with tempfile.TemporaryDirectory(prefix="lcos-pdf-") as temporary:
            staging = Path(temporary)
            source_docx = staging / "input.docx"
            create_docx_from_markdown(markdown, source_docx, title=title)
            profile = staging / "profile"
            profile.mkdir()
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("PYTHON")
            }
            completed = subprocess.run(
                [
                    str(soffice),
                    f"-env:UserInstallation={profile.resolve().as_uri()}",
                    "--headless",
                    "--nologo",
                    "--norestore",
                    "--nofirststartwizard",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(staging),
                    str(source_docx),
                ],
                cwd=staging,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=45,
            )
            rendered_pdf = staging / "input.pdf"
            if completed.returncode != 0 or not rendered_pdf.is_file():
                raise LegalCaseError(
                    "PDF_CONVERSION_FAILED",
                    "本地 LibreOffice 未能生成 PDF；没有写入目标文件。",
                    {
                        "degraded": True,
                        "renderer": str(soffice),
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout[-2000:],
                        "stderr": completed.stderr[-2000:],
                        "visual_qa_required": True,
                    },
                )
            pdf_bytes = rendered_pdf.read_bytes()
            if not pdf_bytes.startswith(b"%PDF-"):
                raise LegalCaseError(
                    "INVALID_PDF_OUTPUT",
                    "渲染后端返回的文件没有 PDF 签名；没有写入目标文件。",
                    {"degraded": True, "renderer": str(soffice), "visual_qa_required": True},
                )
            # This count is deliberately conservative and dependency-free. A
            # positive count proves the converted file contains page objects;
            # final filing still requires page-by-page visual QA.
            page_count = len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))
            if page_count < 1:
                raise LegalCaseError(
                    "PDF_PAGE_COUNT_INVALID",
                    "生成的 PDF 未检测到有效页面；没有写入目标文件。",
                    {"degraded": True, "renderer": str(soffice), "visual_qa_required": True},
                )
            output.write_bytes(pdf_bytes)
            return {"renderer": str(soffice), "page_count": page_count}
    except subprocess.TimeoutExpired as exc:
        raise LegalCaseError(
            "PDF_CONVERSION_TIMEOUT",
            "本地 LibreOffice 转换在45秒内未完成；没有写入目标文件。",
            {"degraded": True, "renderer": str(soffice), "visual_qa_required": True},
        ) from exc


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    registry = load_json(path)
    if registry.get("registry_version") != "1.0.0" or not isinstance(registry.get("templates"), list):
        raise LegalCaseError("INVALID_TEMPLATE_REGISTRY", "模板注册表缺少 registry_version=1.0.0 或 templates 数组。")
    return registry


def template_path(entry: dict[str, Any], registry_path: Path) -> Path:
    path = Path(entry["path"])
    return path if path.is_absolute() else (registry_path.parent / path).resolve()


def resolve_template(identifier: str, registry_path: Path = DEFAULT_REGISTRY) -> tuple[dict[str, Any], Path]:
    registry = load_registry(registry_path)
    query = identifier.casefold().strip()
    matches = []
    for entry in registry["templates"]:
        keys = [entry.get("id", ""), entry.get("name", ""), entry.get("document_type", ""), *entry.get("aliases", [])]
        if query in {str(key).casefold().strip() for key in keys}:
            matches.append(entry)
    if not matches:
        raise LegalCaseError("TEMPLATE_NOT_FOUND", f"未找到模板或别名：{identifier}")
    if len(matches) != 1:
        raise LegalCaseError("AMBIGUOUS_TEMPLATE", f"模板别名不唯一：{identifier}", {"template_ids": [item["id"] for item in matches]})
    entry = matches[0]
    if entry.get("status") != "active":
        raise LegalCaseError("TEMPLATE_NOT_ACTIVE", f"模板 {entry['id']} 状态为 {entry.get('status')}，禁止生成。")
    license_info = entry.get("license", {})
    if license_info.get("status") != "verified" or not license_info.get("id"):
        raise LegalCaseError("TEMPLATE_LICENSE_BLOCKED", f"模板 {entry['id']} 的许可未知或受限，禁止生成。")
    source_path = template_path(entry, registry_path)
    if not source_path.is_file():
        raise LegalCaseError("TEMPLATE_FILE_MISSING", f"模板登记文件不存在：{source_path}")
    actual_hash = sha256_file(source_path)
    if actual_hash != entry.get("sha256"):
        raise LegalCaseError(
            "TEMPLATE_HASH_MISMATCH",
            f"模板 {entry['id']} 内容哈希与注册表不一致，可能已变更或版本过期。",
            {"registered": entry.get("sha256"), "actual": actual_hash},
        )
    return entry, source_path


def validate_registry(registry_path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    registry = load_registry(registry_path)
    errors: list[str] = []
    identifiers: set[str] = set()
    aliases: dict[str, str] = {}
    for entry in registry["templates"]:
        identifier = entry.get("id")
        if not identifier:
            errors.append("模板缺少 id")
            continue
        if identifier in identifiers:
            errors.append(f"模板ID重复：{identifier}")
        identifiers.add(identifier)
        for field in ("name", "document_type", "path", "source", "version", "sha256"):
            if not entry.get(field):
                errors.append(f"模板 {identifier} 缺少 {field}")
        if entry.get("license", {}).get("status") != "verified":
            errors.append(f"模板 {identifier} 许可未核验")
        try:
            source_path = template_path(entry, registry_path)
            if not source_path.is_file():
                errors.append(f"模板 {identifier} 文件不存在：{source_path}")
            elif sha256_file(source_path) != entry.get("sha256"):
                errors.append(f"模板 {identifier} 哈希不一致")
        except (KeyError, OSError) as exc:
            errors.append(f"模板 {identifier} 路径错误：{exc}")
        keys = [entry.get("name", ""), entry.get("document_type", ""), *entry.get("aliases", [])]
        for key in keys:
            normalized = str(key).casefold().strip()
            if not normalized:
                continue
            if normalized in aliases and aliases[normalized] != identifier:
                errors.append(f"模板别名冲突：{key} -> {aliases[normalized]} / {identifier}")
            aliases[normalized] = identifier
        fields = set(entry.get("editable_fields", [])) | set(entry.get("required_fields", []))
        try:
            placeholders = set(PLACEHOLDER_RE.findall(template_path(entry, registry_path).read_text(encoding="utf-8-sig")))
            undeclared = placeholders - fields
            if undeclared:
                errors.append(f"模板 {identifier} 含未登记占位字段：{sorted(undeclared)}")
            missing_in_template = set(entry.get("required_fields", [])) - placeholders
            if missing_in_template:
                errors.append(f"模板 {identifier} 的必填字段未出现在模板中：{sorted(missing_in_template)}")
        except OSError:
            pass
    return {"ok": not errors, "registry": str(registry_path), "template_count": len(registry["templates"]), "errors": errors}


def _personal_identifier_forms(value: object) -> set[str]:
    """Return user-facing equivalent forms without guessing across catalog entries."""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    normalized = normalized.strip(" \t\r\n'\"“”‘’《》<>，,。；;：:")
    if not normalized:
        return set()
    expanded = {normalized}
    pending = [normalized]
    while pending:
        current = pending.pop()
        for suffix in ("模板", "号"):
            if current.endswith(suffix) and len(current) > len(suffix):
                shortened = current[: -len(suffix)].rstrip()
                if shortened not in expanded:
                    expanded.add(shortened)
                    pending.append(shortened)
    compact = {re.sub(r"[\s\-_—–]+", "", item) for item in expanded}
    return {item for item in expanded | compact if item}


def load_personal_template_catalog(path: Path = DEFAULT_PERSONAL_TEMPLATE_CATALOG) -> dict[str, Any]:
    catalog = load_json(path)
    if catalog.get("catalog_version") != "1.0.0":
        raise LegalCaseError("INVALID_PERSONAL_TEMPLATE_CATALOG", "个人模板目录缺少 catalog_version=1.0.0。")
    if not isinstance(catalog.get("templates"), list) or not isinstance(catalog.get("suites"), list):
        raise LegalCaseError("INVALID_PERSONAL_TEMPLATE_CATALOG", "个人模板目录必须包含 templates 和 suites 数组。")
    return catalog


def personal_template_path(entry: dict[str, Any], catalog_path: Path) -> Path:
    raw = Path(entry["path"])
    resolved = raw.resolve() if raw.is_absolute() else (catalog_path.parent / raw).resolve()
    library_root = catalog_path.parent.parent.resolve()
    try:
        resolved.relative_to(library_root)
    except ValueError as exc:
        raise LegalCaseError(
            "PERSONAL_TEMPLATE_PATH_OUTSIDE_LIBRARY",
            f"个人模板路径越出模板库：{resolved}",
        ) from exc
    return resolved


def personal_profile_path(entry: dict[str, Any], catalog_path: Path) -> Path:
    raw = Path(entry["profile_path"])
    resolved = raw.resolve() if raw.is_absolute() else (catalog_path.parent / raw).resolve()
    library_root = catalog_path.parent.parent.resolve()
    try:
        resolved.relative_to(library_root)
    except ValueError as exc:
        raise LegalCaseError(
            "PERSONAL_PROFILE_PATH_OUTSIDE_LIBRARY",
            f"个人模板画像路径越出模板库：{resolved}",
        ) from exc
    return resolved


def personal_fingerprint_manifest_path(entry: dict[str, Any], catalog_path: Path) -> Path:
    """Resolve the hash-only exemplar manifest without leaving the template library."""
    raw = Path(entry["fingerprint_manifest_path"])
    resolved = raw.resolve() if raw.is_absolute() else (catalog_path.parent / raw).resolve()
    library_root = catalog_path.parent.parent.resolve()
    try:
        resolved.relative_to(library_root)
    except ValueError as exc:
        raise LegalCaseError(
            "PERSONAL_FINGERPRINT_PATH_OUTSIDE_LIBRARY",
            f"个人模板串案指纹路径越出模板库：{resolved}",
        ) from exc
    return resolved


def _validate_hash_only_manifest(
    entry: dict[str, Any],
    profile: dict[str, Any],
    catalog_path: Path,
) -> list[str]:
    """Validate a persisted manifest and reject plaintext-bearing records.

    Writing templates are not composition-eligible unless their catalog entry
    binds a non-empty, hash-only old-case leakage manifest.  The manifest is a
    sibling control artifact; it is never written into a generated document.
    """
    identifier = str(entry.get("id") or "<missing-id>")
    errors: list[str] = []
    for field in (
        "fingerprint_manifest_path",
        "fingerprint_manifest_sha256",
        "fingerprint_count",
    ):
        if entry.get(field) in {None, ""}:
            errors.append(f"个人模板 {identifier} 缺少 {field}")
    if errors:
        return errors
    try:
        manifest_path = personal_fingerprint_manifest_path(entry, catalog_path)
        if not manifest_path.is_file():
            return [f"个人模板 {identifier} 串案指纹清单不存在：{manifest_path}"]
        actual_hash = sha256_file(manifest_path)
        if actual_hash != entry.get("fingerprint_manifest_sha256"):
            errors.append(f"个人模板 {identifier} 串案指纹清单哈希不一致")
        manifest = load_json(manifest_path)
        if manifest.get("schema_version") != "1.0.0":
            errors.append(f"个人模板 {identifier} 串案指纹清单版本无效")
        if manifest.get("template_id") != identifier:
            errors.append(f"个人模板 {identifier} 串案指纹 template_id 不一致")
        if manifest.get("profile_id") != profile.get("profile_id"):
            errors.append(f"个人模板 {identifier} 串案指纹 profile_id 不一致")
        if manifest.get("profile_sha256") != entry.get("profile_sha256"):
            errors.append(f"个人模板 {identifier} 串案指纹未绑定当前画像哈希")
        if manifest.get("source_sha256") != entry.get("sha256"):
            errors.append(f"个人模板 {identifier} 串案指纹未绑定当前模板哈希")
        fingerprints = manifest.get("fingerprints")
        if not isinstance(fingerprints, list) or not fingerprints:
            errors.append(f"个人模板 {identifier} 串案指纹清单为空")
            fingerprints = []
        if entry.get("fingerprint_count") != len(fingerprints):
            errors.append(f"个人模板 {identifier} 串案指纹数量与目录不一致")
        allowed_record_keys = {"exemplar_id", "kind", "sha256", "normalized_length"}
        for index, record in enumerate(fingerprints):
            if not isinstance(record, dict) or set(record) != allowed_record_keys:
                errors.append(f"个人模板 {identifier} 第{index + 1}条指纹含明文或非法字段")
                continue
            if record.get("exemplar_id") != identifier:
                errors.append(f"个人模板 {identifier} 第{index + 1}条指纹 exemplar_id 不一致")
            if not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256") or "")):
                errors.append(f"个人模板 {identifier} 第{index + 1}条指纹SHA-256无效")
            if not isinstance(record.get("normalized_length"), int) or record.get("normalized_length", 0) < 2:
                errors.append(f"个人模板 {identifier} 第{index + 1}条指纹长度无效")
    except (KeyError, OSError, LegalCaseError, json.JSONDecodeError) as exc:
        errors.append(f"个人模板 {identifier} 串案指纹清单路径或内容错误：{exc}")
    return errors


def validate_personal_template_catalog(
    catalog_path: Path = DEFAULT_PERSONAL_TEMPLATE_CATALOG,
) -> dict[str, Any]:
    catalog = load_personal_template_catalog(catalog_path)
    errors: list[str] = []
    identifiers: set[str] = set()
    lookup: dict[str, str] = {}
    personal_ids: set[str] = set()

    def register_keys(kind: str, entry: dict[str, Any]) -> None:
        identifier = str(entry.get("id", "")).strip()
        if not identifier:
            errors.append(f"个人{kind}缺少 id")
            return
        if identifier in identifiers:
            errors.append(f"个人模板/套件 ID 重复：{identifier}")
        identifiers.add(identifier)
        for key in [identifier, entry.get("name", ""), *entry.get("aliases", [])]:
            for form in _personal_identifier_forms(key):
                previous = lookup.get(form)
                if previous and previous != identifier:
                    errors.append(f"个人模板别名冲突：{key} -> {previous} / {identifier}")
                lookup[form] = identifier

    for entry in catalog["templates"]:
        register_keys("模板", entry)
        identifier = str(entry.get("id", "")).strip()
        if identifier:
            personal_ids.add(identifier)
        for field in (
            "name", "document_type", "category", "path", "profile_path",
            "profile_sha256", "source", "version", "sha256", "usage_mode", "status",
        ):
            if not entry.get(field):
                errors.append(f"个人模板 {identifier or '<missing-id>'} 缺少 {field}")
        if entry.get("usage_mode") not in PERSONAL_TEMPLATE_USAGE_MODES:
            errors.append(
                f"个人模板 {identifier} usage_mode 必须为 reference、fillable_clone 或 hybrid"
            )
        if entry.get("status") not in {"active", "retired"}:
            errors.append(f"个人模板 {identifier} status 必须为 active 或 retired")
        if entry.get("approved_final") is not True:
            errors.append(f"个人模板 {identifier} 不是律师认可定稿")
        authorization = entry.get("authorization", {})
        if authorization.get("status") != "verified" or not authorization.get("basis"):
            errors.append(f"个人模板 {identifier} 授权状态未核验")
        if not entry.get("reusable_aspects"):
            errors.append(f"个人模板 {identifier} 未登记允许学习范围")
        forbidden = set(entry.get("forbidden_transfer", []))
        required_forbidden = {"client_facts", "names", "dates", "amounts", "claims", "evidence", "authorities", "conclusions"}
        if not required_forbidden <= forbidden:
            errors.append(f"个人模板 {identifier} 禁止迁移范围不完整：{sorted(required_forbidden - forbidden)}")
        try:
            source_path = personal_template_path(entry, catalog_path)
            if source_path.suffix.casefold() not in PERSONAL_TEMPLATE_EXTENSIONS:
                errors.append(f"个人模板 {identifier} 文件类型不支持：{source_path.suffix or '<none>'}")
            if entry.get("usage_mode") in {"fillable_clone", "hybrid"} and source_path.suffix.casefold() != ".docx":
                errors.append(f"个人模板 {identifier} 的 {entry.get('usage_mode')} 模式必须使用 DOCX")
            if not source_path.is_file():
                errors.append(f"个人模板 {identifier} 文件不存在：{source_path}")
            elif sha256_file(source_path) != entry.get("sha256"):
                errors.append(f"个人模板 {identifier} 哈希不一致")
            if entry.get("status") == "active" and "90-停用留档" in source_path.parts:
                errors.append(f"个人模板 {identifier} 位于停用留档目录却标记为 active")
            profile_path = personal_profile_path(entry, catalog_path)
            if not profile_path.is_file():
                errors.append(f"个人模板 {identifier} 画像不存在：{profile_path}")
            elif sha256_file(profile_path) != entry.get("profile_sha256"):
                errors.append(f"个人模板 {identifier} 画像哈希不一致")
            else:
                from .template_curation import validate_profile

                profile = load_json(profile_path)
                profile_validation = validate_profile(profile_path, require_active=entry.get("status") == "active")
                if not profile_validation["ok"]:
                    errors.append(f"个人模板 {identifier} 画像未通过激活门禁：{profile_validation['errors']}")
                if profile.get("template_id") != identifier:
                    errors.append(f"个人模板 {identifier} 与画像 template_id 不一致")
                if profile.get("usage_mode") != entry.get("usage_mode"):
                    errors.append(f"个人模板 {identifier} 与画像 usage_mode 不一致")
                if profile.get("source", {}).get("sha256") != entry.get("sha256"):
                    errors.append(f"个人模板 {identifier} 与画像源哈希不一致")
                if profile.get("profile_kind") == "writing":
                    if entry.get("composition_preferences") != profile.get("composition_preferences"):
                        errors.append(f"个人模板 {identifier} 目录选择偏好与已批准WritingProfile不一致")
                    errors.extend(_validate_hash_only_manifest(entry, profile, catalog_path))
        except (KeyError, OSError, LegalCaseError) as exc:
            errors.append(f"个人模板 {identifier} 路径错误：{exc}")

    builtin_ids: set[str] = set()
    try:
        builtin_ids = {str(item.get("id")) for item in load_registry(DEFAULT_REGISTRY)["templates"]}
    except (OSError, LegalCaseError) as exc:
        errors.append(f"无法读取内置模板注册表：{exc}")

    for suite in catalog["suites"]:
        register_keys("套件", suite)
        identifier = str(suite.get("id", "")).strip()
        for field in ("name", "status", "components"):
            if not suite.get(field):
                errors.append(f"个人套件 {identifier or '<missing-id>'} 缺少 {field}")
        if suite.get("status") not in {"active", "retired"}:
            errors.append(f"个人套件 {identifier} status 必须为 active 或 retired")
        components = suite.get("components", [])
        if not isinstance(components, list):
            errors.append(f"个人套件 {identifier} components 必须为数组")
            continue
        roles: set[str] = set()
        for component in components:
            role = str(component.get("role", "")).strip()
            template_id = str(component.get("template_id", "")).strip()
            source_registry = component.get("registry")
            if not role or not template_id or source_registry not in {"personal", "builtin"}:
                errors.append(f"个人套件 {identifier} 含无效组件：{component}")
                continue
            if role in roles:
                errors.append(f"个人套件 {identifier} 组件角色重复：{role}")
            roles.add(role)
            allowed_ids = personal_ids if source_registry == "personal" else builtin_ids
            if template_id not in allowed_ids:
                errors.append(f"个人套件 {identifier} 引用不存在的 {source_registry} 模板：{template_id}")

    return {
        "ok": not errors,
        "catalog": str(catalog_path),
        "template_count": len(catalog["templates"]),
        "suite_count": len(catalog["suites"]),
        "errors": errors,
    }


def resolve_personal_template_reference(
    identifier: str,
    catalog_path: Path = DEFAULT_PERSONAL_TEMPLATE_CATALOG,
) -> dict[str, Any]:
    validation = validate_personal_template_catalog(catalog_path)
    if not validation["ok"]:
        raise LegalCaseError(
            "INVALID_PERSONAL_TEMPLATE_CATALOG",
            "个人模板目录校验失败，禁止加载。",
            {"errors": validation["errors"]},
        )
    catalog = load_personal_template_catalog(catalog_path)
    query_forms = _personal_identifier_forms(identifier)
    matches: list[tuple[str, dict[str, Any]]] = []
    for kind, entries in (("template", catalog["templates"]), ("suite", catalog["suites"])):
        for entry in entries:
            keys = [entry.get("id", ""), entry.get("name", ""), *entry.get("aliases", [])]
            if query_forms & set().union(*(_personal_identifier_forms(key) for key in keys)):
                matches.append((kind, entry))
    if not matches:
        raise LegalCaseError("PERSONAL_TEMPLATE_NOT_FOUND", f"个人模板库未找到编号、名称或别名：{identifier}")
    if len(matches) != 1:
        raise LegalCaseError(
            "AMBIGUOUS_PERSONAL_TEMPLATE",
            f"个人模板编号或别名不唯一：{identifier}",
            {"matches": [{"kind": kind, "id": entry.get("id")} for kind, entry in matches]},
        )
    kind, entry = matches[0]
    if entry.get("status") != "active":
        raise LegalCaseError("PERSONAL_TEMPLATE_NOT_ACTIVE", f"个人模板/套件 {entry.get('id')} 已停用。")
    if kind == "template":
        source_path = personal_template_path(entry, catalog_path)
        profile_path = personal_profile_path(entry, catalog_path)
        return {
            "kind": kind,
            "entry": entry,
            "path": source_path,
            "profile_path": profile_path,
            "fingerprint_manifest_path": personal_fingerprint_manifest_path(entry, catalog_path)
            if entry.get("fingerprint_manifest_path") else None,
            "components": [],
        }

    personal_by_id = {item["id"]: item for item in catalog["templates"]}
    components: list[dict[str, Any]] = []
    for component in entry["components"]:
        if component["registry"] == "personal":
            template = personal_by_id[component["template_id"]]
            if template.get("status") != "active":
                raise LegalCaseError("PERSONAL_TEMPLATE_NOT_ACTIVE", f"套件组件 {template['id']} 已停用。")
            path = personal_template_path(template, catalog_path)
            profile_path = personal_profile_path(template, catalog_path)
        else:
            template, path = resolve_template(component["template_id"], DEFAULT_REGISTRY)
            profile_path = None
        components.append({
            "role": component["role"],
            "registry": component["registry"],
            "template_id": template["id"],
            "name": template["name"],
            "version": template["version"],
            "sha256": template["sha256"],
            "path": str(path),
            "profile_path": str(profile_path) if profile_path else None,
            "profile_sha256": template.get("profile_sha256"),
            "usage_mode": template.get("usage_mode"),
        })
    return {"kind": kind, "entry": entry, "path": None, "profile_path": None, "components": components}


def render_template_text(entry: dict[str, Any], source_path: Path, data: dict[str, Any]) -> str:
    missing = [field for field in entry.get("required_fields", []) if field not in data or data[field] in (None, "", [])]
    if missing:
        raise LegalCaseError("TEMPLATE_REQUIRED_FIELDS_MISSING", "模板必填字段缺失，禁止生成。", {"fields": missing})
    allowed = set(entry.get("editable_fields", [])) | set(entry.get("required_fields", []))
    extras = sorted(set(data) - allowed)
    if extras:
        raise LegalCaseError("TEMPLATE_UNKNOWN_FIELDS", "数据含模板未登记字段；为防止误填，已阻断。", {"fields": extras})
    text = source_path.read_text(encoding="utf-8-sig")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = data.get(key, "")
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    rendered = PLACEHOLDER_RE.sub(replace, text)
    unresolved = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if unresolved:
        raise LegalCaseError("UNRESOLVED_TEMPLATE_FIELDS", "模板仍有未决占位符，禁止生成。", {"fields": unresolved})
    return rendered


def _paragraph_xml(text: str, style: str | None = None, bold: bool = False, align: str | None = None) -> str:
    properties = []
    if style:
        properties.append(f'<w:pStyle w:val="{escape(style)}"/>')
    if align:
        properties.append(f'<w:jc w:val="{escape(align)}"/>')
    ppr = f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
    rpr = "<w:rPr><w:b/><w:bCs/></w:rPr>" if bold else ""
    if not text:
        # Markdown templates use blank lines for readability.  Rendering each
        # one as a full 1.5-line paragraph creates avoidable orphan pages in
        # short filing forms, so keep a visible but compact 6 pt spacer.
        return '<w:p><w:pPr><w:spacing w:line="120" w:lineRule="exact"/></w:pPr></w:p>'
    chunks = text.split("\t")
    runs = []
    for index, chunk in enumerate(chunks):
        if index:
            runs.append("<w:r><w:tab/></w:r>")
        runs.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(chunk)}</w:t></w:r>')
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _markdown_to_word_body(markdown: str) -> str:
    paragraphs: list[str] = []
    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.startswith("# "):
            paragraphs.append(_paragraph_xml(line[2:].strip(), style="Title", bold=True, align="center"))
        elif line.startswith("## "):
            paragraphs.append(_paragraph_xml(line[3:].strip(), style="Heading1", bold=True))
        elif line.startswith("### "):
            paragraphs.append(_paragraph_xml(line[4:].strip(), style="Heading2", bold=True))
        elif line.startswith("- "):
            paragraphs.append(_paragraph_xml("• " + line[2:].strip()))
        elif re.match(r"^\d+[.、]\s*", line):
            paragraphs.append(_paragraph_xml(line))
        elif line.startswith("|"):
            # Keep tables readable without claiming a full Markdown table engine.
            if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
                continue
            paragraphs.append(_paragraph_xml("\t".join(cell.strip() for cell in line.strip("|").split("|"))))
        else:
            paragraphs.append(_paragraph_xml(line))
    return "".join(paragraphs)


def create_docx_from_markdown(markdown: str, output: Path, *, title: str, creator: str = "Legal Case OS") -> None:
    body = _markdown_to_word_body(markdown)
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body>
</w:document>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="SimSun" w:eastAsia="宋体" w:hAnsi="SimSun"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/></w:rPr></w:style>
</w:styles>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    document_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    timestamp = now_iso()
    core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(title)}</dc:title><dc:creator>{escape(creator)}</dc:creator><cp:lastModifiedBy>{escape(creator)}</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>'''
    app_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Legal Case OS</Application><Pages>1</Pages><Company></Company></Properties>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", root_rels)
        package.writestr("word/document.xml", document_xml)
        package.writestr("word/styles.xml", styles_xml)
        package.writestr("word/_rels/document.xml.rels", document_rels)
        package.writestr("docProps/core.xml", core_xml)
        package.writestr("docProps/app.xml", app_xml)
    output.write_bytes(buffer.getvalue())


def fill_template(
    template_identifier: str,
    data: dict[str, Any],
    output: Path,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    ensure_not_originals(output)
    if output.exists():
        raise LegalCaseError("OUTPUT_EXISTS", "输出已存在；为保留版本链，模板填充拒绝覆盖。")
    entry, source_path = resolve_template(template_identifier, registry_path)
    rendered = render_template_text(entry, source_path, data)
    suffix = output.suffix.casefold()
    output.parent.mkdir(parents=True, exist_ok=True)
    if suffix in {".md", ".txt"}:
        output.write_text(rendered, encoding="utf-8")
        format_name = suffix[1:]
    elif suffix == ".docx":
        create_docx_from_markdown(rendered, output, title=entry["name"])
        format_name = "docx"
        renderer_details: dict[str, Any] = {}
    elif suffix == ".pdf":
        renderer_details = _convert_markdown_to_pdf(rendered, output, title=entry["name"])
        format_name = "pdf"
    else:
        raise LegalCaseError("UNSUPPORTED_OUTPUT_FORMAT", "模板填充只支持 .md、.txt、真实 .docx 和经本地后端验证的真实 .pdf。")
    if suffix in {".md", ".txt"}:
        renderer_details = {}
    result = {
        "ok": True,
        "template_id": entry["id"],
        "template_version": entry["version"],
        "template_sha256": entry["sha256"],
        "output": str(output),
        "output_format": format_name,
        "output_sha256": sha256_file(output),
        "required_fields": entry.get("required_fields", []),
        "license": entry["license"],
        "visual_qa_required": suffix in {".docx", ".pdf"},
    }
    result.update(renderer_details)
    return result


def safe_filename(matter_id: str, kind: str, title: str, version: int, extension: str) -> str:
    if version < 1:
        raise LegalCaseError("INVALID_VERSION", "版本号必须大于等于1。")
    extension = extension.lstrip(".").casefold()
    if not re.fullmatch(r"[a-z0-9]{1,10}", extension):
        raise LegalCaseError("INVALID_EXTENSION", "扩展名不合法。")
    parts = [matter_id, kind, title, f"v{version}"]
    cleaned = []
    for part in parts:
        value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(part)).strip(" .")
        value = re.sub(r"\s+", "_", value)
        cleaned.append(value or "untitled")
    filename = "__".join(cleaned) + f".{extension}"
    if len(filename) > 180:
        filename = filename[: 180 - len(extension) - 1].rstrip(" ._") + f".{extension}"
    return filename
