from __future__ import annotations

import copy
import difflib
import io
import json
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .core import (
    LegalCaseError,
    atomic_write_json,
    canonical_json,
    ensure_not_originals,
    load_json,
    now_iso,
    sha256_bytes,
    sha256_file,
    state_content_hash,
)


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

for prefix, namespace in {
    "w": W_NS,
    "r": R_NS,
    "cp": CP_NS,
    "dc": DC_NS,
    "dcterms": DCTERMS_NS,
    "ep": EP_NS,
    "mc": MC_NS,
}.items():
    ET.register_namespace(prefix, namespace)


def _serialize_ooxml(root: ET.Element, original: bytes) -> bytes:
    """Serialize XML while keeping namespaces named by mc:Ignorable valid.

    ElementTree drops unused xmlns declarations. OOXML consumers reject a part
    when mc:Ignorable still names a dropped prefix, so restore those exact
    original declarations after serialization.
    """
    serialized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    # Keep OPC manifests on their original default namespace. LibreOffice rejects
    # some otherwise valid packages when Types/Relationships are rewritten with
    # an ns0 prefix by ElementTree.
    original_default = re.search(rb"<[^!?][^>]*\sxmlns=[\"']([^\"']+)[\"']", original)
    if original_default:
        uri = original_default.group(1)
        generated = re.search(rb"xmlns:([A-Za-z_][\w.-]*)=[\"']" + re.escape(uri) + rb"[\"']", serialized)
        if generated:
            prefix = generated.group(1)
            serialized = re.sub(rb"\sxmlns:" + re.escape(prefix) + rb"=[\"']" + re.escape(uri) + rb"[\"']", b"", serialized, count=1)
            serialized = serialized.replace(b"<" + prefix + b":", b"<").replace(b"</" + prefix + b":", b"</")
            declaration_end = serialized.find(b"?>")
            root_start = serialized.find(b"<", declaration_end + 2)
            name_end = serialized.find(b" ", root_start)
            close_end = serialized.find(b">", root_start)
            if name_end == -1 or name_end > close_end:
                name_end = close_end
            serialized = serialized[:name_end] + b' xmlns="' + uri + b'"' + serialized[name_end:]
    original_namespaces = {
        match.group(1).decode("ascii"): match.group(2).decode("utf-8")
        for match in re.finditer(rb"xmlns:([A-Za-z_][\w.-]*)=[\"']([^\"']+)[\"']", original)
    }
    ignorable = set()
    for match in re.finditer(rb"(?:[A-Za-z_][\w.-]*:)?Ignorable=[\"']([^\"']*)[\"']", serialized):
        ignorable.update(match.group(1).decode("ascii", errors="ignore").split())
    missing = [
        prefix
        for prefix in sorted(ignorable)
        if prefix in original_namespaces and not re.search(rb"xmlns:" + re.escape(prefix.encode("ascii")) + rb"=", serialized)
    ]
    if not missing:
        return serialized
    declaration_end = serialized.find(b"?>")
    root_start = serialized.find(b"<", declaration_end + 2)
    name_end = serialized.find(b" ", root_start)
    close_end = serialized.find(b">", root_start)
    if name_end == -1 or name_end > close_end:
        name_end = close_end
    additions = b"".join(
        f' xmlns:{prefix}="{original_namespaces[prefix]}"'.encode("utf-8") for prefix in missing
    )
    return serialized[:name_end] + additions + serialized[name_end:]


def detect_kind(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".pdf": "pdf",
        ".docx": "docx",
        ".doc": "doc",
        ".txt": "text",
        ".md": "markdown",
        ".jpg": "image",
        ".jpeg": "image",
        ".png": "image",
        ".tif": "image",
        ".tiff": "image",
        ".bmp": "image",
        ".wav": "audio",
        ".mp3": "audio",
        ".m4a": "audio",
        ".mp4": "video",
    }.get(suffix, "binary")


def pdf_page_count(path: Path) -> int | None:
    data = path.read_bytes()
    if b"/Encrypt" in data:
        return None
    matches = re.findall(rb"/Type\s*/Page\b", data)
    return len(matches) or None


def docx_page_count(path: Path) -> int | None:
    try:
        with zipfile.ZipFile(path) as package:
            if "docProps/app.xml" not in package.namelist():
                return None
            root = ET.fromstring(package.read("docProps/app.xml"))
            pages = root.find(f"{{{EP_NS}}}Pages")
            if pages is not None and pages.text and pages.text.isdigit():
                value = int(pages.text)
                return value if value > 0 else None
    except (zipfile.BadZipFile, ET.ParseError):
        return None
    return None


def page_count(path: Path, kind: str | None = None) -> int | None:
    kind = kind or detect_kind(path)
    if kind == "pdf":
        return pdf_page_count(path)
    if kind == "docx":
        return docx_page_count(path)
    if kind == "image":
        return 1
    return None


def _source_id(relative_path: str) -> str:
    marker = sha256_bytes(relative_path.casefold().encode("utf-8"))[:16]
    return f"S-{marker}"


def build_material_index(
    source_dir: Path,
    existing_sources: list[dict[str, Any]],
    ocr_report: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not source_dir.is_dir():
        raise LegalCaseError("SOURCE_DIR_NOT_FOUND", f"原始材料目录不存在：{source_dir}")
    ocr_report = ocr_report or {}
    existing_by_path = {item.get("path"): item for item in existing_sources}
    ignored_names = {".gitkeep", "ORIGINALS-ARE-READ-ONLY.md"}
    files = sorted(
        (item for item in source_dir.rglob("*") if item.is_file() and item.name not in ignored_names),
        key=lambda item: str(item).casefold(),
    )
    now = now_iso()
    staged: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(source_dir.parent).as_posix()
        digest = sha256_file(path)
        kind = detect_kind(path)
        previous = existing_by_path.get(relative)
        version = int(previous.get("version", 1)) if previous else 1
        if previous and previous.get("sha256") != digest:
            version += 1
        ocr_value = ocr_report.get(relative, ocr_report.get(path.name))
        if isinstance(ocr_value, dict):
            ocr_status = ocr_value.get("status", "unknown")
        elif isinstance(ocr_value, str):
            ocr_status = ocr_value
        elif kind in {"text", "markdown", "docx"}:
            ocr_status = "not_applicable"
        elif kind in {"image", "pdf"}:
            ocr_status = "not_run"
        else:
            ocr_status = "unknown"
        if ocr_status not in {"not_applicable", "not_run", "partial", "complete", "failed", "unknown"}:
            ocr_status = "unknown"
        item = {
            "id": previous.get("id") if previous else _source_id(relative),
            "path": relative,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "version": version,
            "kind": kind,
            "original": True,
            "read_only": True,
            "page_count": page_count(path, kind),
            "ocr_status": ocr_status,
            "duplicate_of": hashes.get(digest),
            "confidentiality": previous.get("confidentiality", "ordinary") if previous else "ordinary",
            "ingested_at": previous.get("ingested_at", now) if previous else now,
        }
        hashes.setdefault(digest, item["id"])
        staged.append(item)
    derived = {
        "kind": "derived-material-index",
        "generated_at": now,
        "source_directory": str(source_dir),
        "file_count": len(staged),
        "duplicate_count": sum(1 for item in staged if item["duplicate_of"] is not None),
        "items": staged,
        "notice": "派生视图；case-state.json 才是当前状态唯一真值源。",
    }
    return staged, derived


def extract_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as package:
            texts: list[str] = []
            names = [
                name
                for name in package.namelist()
                if re.fullmatch(r"word/(document|header\d+|footer\d+|footnotes|endnotes)\.xml", name)
            ]
            for name in names:
                root = ET.fromstring(package.read(name))
                for paragraph in root.iter(f"{{{W_NS}}}p"):
                    paragraph_text = "".join(node.text or "" for node in paragraph.iter(f"{{{W_NS}}}t"))
                    if paragraph_text:
                        texts.append(paragraph_text)
            return "\n".join(texts)
    except zipfile.BadZipFile as exc:
        raise LegalCaseError("INVALID_DOCX", f"不是有效的 OOXML DOCX：{path}") from exc


_COURT_PROSE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ai_voice", re.compile(r"(?:AI|模型|大模型|智能助手)\s*(?:认为|建议|分析|提示)", re.I)),
    ("internal_note", re.compile(r"(?:^|[\r\n。；;])\s*(?:内部备注|审稿词|内部提示)\s*[:：]", re.I)),
    ("verification_marker", re.compile(r"(?:待核验|待确认|请律师确认|需律师确认|尚需核验)", re.I)),
    (
        "editorial_directive",
        re.compile(
            r"(?:^|[\r\n])\s*(?:建议|提醒|注意|备注|参考)(?:事项)?\s*[:：]",
            re.I,
        ),
    ),
    ("nonfiling_marker", re.compile(r"(?:仅供内部|不要提交(?:法院)?|不得提交(?:法院)?|内部审阅稿)", re.I)),
)


def court_prose_lint(text: str) -> dict[str, Any]:
    """Detect conversational/editorial traces that cannot enter a court copy.

    Findings retain only a marker category, position and hash/length of the
    matched phrase.  They intentionally do not echo possibly confidential
    prose into logs, and they are review blockers—not auto-deletion targets.
    """

    findings: list[dict[str, Any]] = []
    for marker, pattern in _COURT_PROSE_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            findings.append(
                {
                    "kind": "court_prose_marker",
                    "marker": marker,
                    "character_offset": match.start(),
                    "matched_text_sha256": sha256_bytes(value.encode("utf-8")),
                    "matched_length": len(value),
                    "severity": "blocker",
                }
            )
    return {"ok": not findings, "findings": findings}


def _docx_findings(path: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    capability_gaps: list[str] = []
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            if "word/comments.xml" in names:
                comment_count = len(re.findall(rb"<w:comment\b", package.read("word/comments.xml")))
                if comment_count:
                    findings.append({"kind": "comments", "location": "word/comments.xml", "count": comment_count, "severity": "blocker"})
            if "word/commentsExtended.xml" in names:
                extended_count = len(re.findall(rb"<(?:w15|w16cid):commentEx\b", package.read("word/commentsExtended.xml")))
                if extended_count:
                    findings.append({"kind": "extended_comments", "location": "word/commentsExtended.xml", "count": extended_count, "severity": "blocker"})
            for name in sorted(names):
                if not name.endswith(".xml"):
                    continue
                data = package.read(name)
                if name.startswith("word/"):
                    tracked = len(re.findall(rb"<w:(?:ins|del|moveFrom|moveTo|rPrChange|pPrChange|tblPrChange)\b", data))
                    if tracked:
                        findings.append({"kind": "tracked_changes", "location": name, "count": tracked, "severity": "blocker"})
                    hidden = len(re.findall(rb"<w:vanish\b", data))
                    if hidden:
                        findings.append({"kind": "hidden_text", "location": name, "count": hidden, "severity": "blocker"})
                decoded = data.decode("utf-8", errors="ignore")
                placeholders = re.findall(
                    r"\{\{[^{}]+\}\}|【(?:待|TODO)[^】]*】|\[\[(?:PENDING|TODO):[^\]]+\]\]|\bTODO\b|待核验",
                    decoded,
                    flags=re.I,
                )
                if placeholders:
                    findings.append({"kind": "unresolved_placeholder", "location": name, "count": len(placeholders), "severity": "blocker"})
                internal_ids = re.findall(r"(?:INTERNAL[-_:][A-Za-z0-9._-]+|INT-[A-Z0-9][A-Z0-9._-]*|内部(?:ID|编号)[:：]?\s*[A-Za-z0-9._-]+)", decoded, flags=re.I)
                if internal_ids:
                    findings.append({"kind": "internal_id", "location": name, "values": sorted(set(internal_ids)), "severity": "blocker"})
                internal_notes = re.findall(r"\[(?:内部备注|审稿词)[:：][^\]]+\]|\[\[INTERNAL:[^\]]+\]\]", decoded, flags=re.I)
                if internal_notes:
                    findings.append({
                        "kind": "internal_note",
                        "location": name,
                        "count": len(internal_notes),
                        "values": [
                            {"original_text_sha256": sha256_bytes(value.encode("utf-8")), "length": len(value)}
                            for value in internal_notes
                        ],
                        "severity": "blocker",
                    })
                if name.startswith("word/header"):
                    watermark_values = re.findall(r"(?:DRAFT|草稿|内部审阅|机密|CONFIDENTIAL)", decoded, flags=re.I)
                    if watermark_values:
                        findings.append({"kind": "watermark_candidate", "location": name, "values": sorted(set(watermark_values)), "severity": "requires_authorization"})
            sensitive_names = {"creator", "lastModifiedBy", "keywords", "subject", "description", "category", "created", "modified", "Company", "Manager", "HyperlinkBase"}
            for metadata_name in sorted(name for name in names if name.startswith("docProps/") and name.endswith(".xml")):
                metadata_fields: list[str] = []
                try:
                    metadata_root = ET.fromstring(package.read(metadata_name))
                except ET.ParseError:
                    continue
                if metadata_name == "docProps/custom.xml":
                    if any((node.text or "").strip() for node in metadata_root.iter()):
                        metadata_fields.append("custom-properties")
                    if metadata_fields:
                        findings.append({"kind": "metadata", "location": metadata_name, "values": metadata_fields, "severity": "review"})
                    continue
                for node in metadata_root.iter():
                    local_name = node.tag.rsplit("}", 1)[-1]
                    if local_name in sensitive_names and (node.text or "").strip():
                        metadata_fields.append(local_name)
                if metadata_fields:
                    findings.append({"kind": "metadata", "location": metadata_name, "values": metadata_fields, "severity": "review"})
    except zipfile.BadZipFile as exc:
        raise LegalCaseError("INVALID_DOCX", f"不是有效的 OOXML DOCX：{path}") from exc
    for item in court_prose_lint(extract_docx_text(path))["findings"]:
        findings.append({**item, "location": "visible_docx_text"})
    return {"findings": findings, "capability_gaps": capability_gaps}


def _pdf_findings(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = data.decode("latin-1", errors="ignore")
    findings: list[dict[str, Any]] = []
    capability_gaps = ["标准库无法可靠检测 PDF 中的隐藏文字、逐字视觉水印或所有增量修订。"]
    if b"/Encrypt" in data:
        findings.append({"kind": "encrypted_pdf", "location": "trailer", "severity": "blocker"})
    annots = len(re.findall(rb"/Annots\b|/Subtype\s*/(?:Text|Highlight|StrikeOut|Underline)\b", data))
    if annots:
        findings.append({"kind": "annotations", "location": "PDF objects", "count": annots, "severity": "blocker"})
    for key in ("Author", "Creator", "Producer", "Subject", "Keywords"):
        if re.search(rf"/{key}\s*\(", text):
            findings.append({"kind": "metadata", "location": f"/{key}", "severity": "review"})
    placeholders = re.findall(r"\{\{[^{}]+\}\}|TODO|待核验", text, flags=re.I)
    if placeholders:
        findings.append({"kind": "unresolved_placeholder", "location": "PDF stream/object", "count": len(placeholders), "severity": "blocker"})
    watermarks = re.findall(r"DRAFT|草稿|内部审阅|CONFIDENTIAL", text, flags=re.I)
    if watermarks:
        findings.append({"kind": "watermark_candidate", "location": "PDF stream/object", "values": sorted(set(watermarks)), "severity": "requires_authorization"})
    extracted_pages: list[str] | None = None
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
            reader = module.PdfReader(str(path))
            extracted_pages = [page.extract_text() or "" for page in reader.pages]
            break
        except Exception:
            continue
    if extracted_pages is None:
        capability_gaps.append("PDF 可见正文未能可靠抽取；法院候选须绑定已审 DOCX 或先完成 OCR/文本层核验。")
    else:
        visible_text = "\n".join(extracted_pages)
        for item in court_prose_lint(visible_text)["findings"]:
            findings.append({**item, "location": "visible_pdf_text"})
    return {"findings": findings, "capability_gaps": capability_gaps}


def preflight(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LegalCaseError("FILE_NOT_FOUND", f"待检查文件不存在：{path}")
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        result = _docx_findings(path)
        file_type = "docx"
    elif suffix == ".pdf":
        result = _pdf_findings(path)
        file_type = "pdf"
    else:
        raise LegalCaseError("UNSUPPORTED_PREFLIGHT_TYPE", "preflight 只支持真实 DOCX 或 PDF。")
    blockers = [item for item in result["findings"] if item.get("severity") == "blocker"]
    return {
        "ok": not blockers,
        "path": str(path),
        "file_type": file_type,
        "sha256": sha256_file(path),
        "page_count": page_count(path, file_type),
        "findings": result["findings"],
        "blocking_findings": blockers,
        "capability_gaps": result["capability_gaps"],
        "notice": "预检不等于独立法律审阅；水印候选必须核对文件权属和批准范围。",
    }


def _validate_sanitization_approval(
    source: Path,
    approval: dict[str, Any],
    before_preflight: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    if approval.get("pre_review_status") != "passed" or not approval.get("independent_review_id"):
        raise LegalCaseError(
            "PRE_REVIEW_NOT_PASSED",
            "清洁前必须有 independent_review_id 且 pre_review_status=passed。",
        )
    targets = approval.get("approved_targets")
    if not isinstance(targets, list) or not targets:
        raise LegalCaseError("SANITIZATION_TARGETS_REQUIRED", "批准文件必须逐项列出 approved_targets，类别白名单不能代替精确目标。")
    allowed_kinds = {"comments", "tracked_changes", "draft_watermark", "authorized_metadata", "internal_ids", "internal_notes"}
    approved_categories = set(approval.get("approved_items", []))
    target_categories = {target.get("kind") for target in targets}
    if approved_categories != target_categories or not approved_categories <= allowed_kinds:
        raise LegalCaseError(
            "SANITIZATION_CATEGORY_TARGET_MISMATCH",
            "approved_items 类别白名单必须与 approved_targets 中实际类别完全一致且均在白名单内。",
            {"approved_items": sorted(approved_categories), "target_kinds": sorted(str(item) for item in target_categories)},
        )
    item_ids = [target.get("item_id") for target in targets]
    if any(not item for item in item_ids) or len(set(item_ids)) != len(item_ids):
        raise LegalCaseError("SANITIZATION_ITEM_ID_INVALID", "每个清洁目标必须有唯一、非空 item_id。")
    for target in targets:
        if target.get("ownership") != "self_generated" or not str(target.get("authority_basis") or "").strip():
            raise LegalCaseError(
                "SANITIZATION_TARGET_NOT_AUTHORIZED",
                "每个清洁目标都必须逐项标明 ownership=self_generated 和非空 authority_basis；顶层权属不能替代逐项授权。",
                {"item_id": target.get("item_id"), "ownership": target.get("ownership")},
            )
    expected_set_hash = sha256_bytes(canonical_json(targets).encode("utf-8"))
    if approval.get("approval_set_sha256") != expected_set_hash:
        raise LegalCaseError(
            "SANITIZATION_APPROVAL_SET_HASH_MISMATCH",
            "approval_set_sha256 与逐项目标清单不一致。",
            {"expected": expected_set_hash, "provided": approval.get("approval_set_sha256")},
        )
    findings_by_kind_location = {
        (finding.get("kind"), finding.get("location")): finding for finding in before_preflight.get("findings", [])
    }
    kind_to_finding = {
        "comments": "comments",
        "tracked_changes": "tracked_changes",
        "draft_watermark": "watermark_candidate",
        "authorized_metadata": "metadata",
        "internal_ids": "internal_id",
        "internal_notes": "internal_note",
    }
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in approved_categories}
    with zipfile.ZipFile(source) as package:
        names = set(package.namelist())
        for target in targets:
            kind = target["kind"]
            location = target.get("location")
            if not location or location not in names:
                raise LegalCaseError("SANITIZATION_TARGET_LOCATION_INVALID", f"清洁目标部件不存在：{location}", {"item_id": target["item_id"]})
            actual_part_hash = sha256_bytes(package.read(location))
            if target.get("source_part_sha256") != actual_part_hash:
                raise LegalCaseError(
                    "SANITIZATION_TARGET_PART_CHANGED",
                    f"清洁目标部件哈希已变化：{location}",
                    {"item_id": target["item_id"], "actual": actual_part_hash},
                )
            finding = findings_by_kind_location.get((kind_to_finding[kind], location))
            if finding is None:
                raise LegalCaseError(
                    "SANITIZATION_TARGET_NOT_FOUND",
                    f"预检未在指定位置发现获批类别：{kind} @ {location}",
                    {"item_id": target["item_id"]},
                )
            decoded = package.read(location).decode("utf-8", errors="ignore")
            if kind in {"comments", "tracked_changes", "authorized_metadata"}:
                actual_count = int(finding.get("count", len(finding.get("values", []))))
                if not isinstance(target.get("expected_count"), int) or target["expected_count"] != actual_count or actual_count < 1:
                    raise LegalCaseError(
                        "SANITIZATION_TARGET_COUNT_MISMATCH",
                        f"批量清洁目标必须用 expected_count 精确绑定预检数量：{kind} @ {location}",
                        {"item_id": target["item_id"], "expected": target.get("expected_count"), "actual": actual_count},
                    )
            if kind == "draft_watermark":
                value = target.get("value")
                if not value or value not in decoded or value not in approval.get("authorized_watermarks", []):
                    raise LegalCaseError("WATERMARK_TARGET_NOT_EXACT", "水印目标必须逐字存在且列入 authorized_watermarks。", {"item_id": target["item_id"]})
            elif kind == "internal_ids":
                value = target.get("value")
                if not value or value not in decoded or value not in approval.get("approved_internal_ids", []):
                    raise LegalCaseError("INTERNAL_ID_TARGET_NOT_EXACT", "内部ID必须逐字存在且列入 approved_internal_ids。", {"item_id": target["item_id"]})
            elif kind == "internal_notes":
                note_hash = target.get("original_text_sha256")
                detected = {item.get("original_text_sha256") for item in finding.get("values", [])}
                if not note_hash or note_hash not in detected:
                    raise LegalCaseError("INTERNAL_NOTE_HASH_NOT_FOUND", "内部备注原文哈希与预检结果不一致。", {"item_id": target["item_id"]})
            grouped[kind].append(target)
    return grouped


def _remove_comment_relationships(name: str, root: ET.Element) -> None:
    if name.endswith(".rels"):
        for child in list(root):
            target = child.attrib.get("Target", "").casefold()
            rel_type = child.attrib.get("Type", "").casefold()
            if "comment" in target or "comment" in rel_type or target.endswith("people.xml"):
                root.remove(child)
    elif name == "[Content_Types].xml":
        for child in list(root):
            part = child.attrib.get("PartName", "").casefold()
            content_type = child.attrib.get("ContentType", "").casefold()
            if "comment" in part or "comment" in content_type or part.endswith("/people.xml"):
                root.remove(child)


def _remove_custom_metadata_relationships(name: str, root: ET.Element) -> None:
    if name.endswith(".rels"):
        for child in list(root):
            target = child.attrib.get("Target", "").casefold()
            rel_type = child.attrib.get("Type", "").casefold()
            if target.endswith("custom.xml") or "custom-properties" in rel_type:
                root.remove(child)
    elif name == "[Content_Types].xml":
        for child in list(root):
            part = child.attrib.get("PartName", "").casefold()
            content_type = child.attrib.get("ContentType", "").casefold()
            if part.endswith("/custom.xml") or "custom-properties" in content_type:
                root.remove(child)


def _flatten_tracked_changes(parent: ET.Element) -> None:
    deletion_tags = {
        f"{{{W_NS}}}del",
        f"{{{W_NS}}}moveFrom",
        f"{{{W_NS}}}rPrChange",
        f"{{{W_NS}}}pPrChange",
        f"{{{W_NS}}}tblPrChange",
        f"{{{W_NS}}}trPrChange",
        f"{{{W_NS}}}tcPrChange",
        f"{{{W_NS}}}sectPrChange",
    }
    insertion_tags = {f"{{{W_NS}}}ins", f"{{{W_NS}}}moveTo"}
    for child in list(parent):
        _flatten_tracked_changes(child)
        if child.tag in deletion_tags:
            parent.remove(child)
        elif child.tag in insertion_tags:
            index = list(parent).index(child)
            parent.remove(child)
            for grandchild in list(child):
                parent.insert(index, grandchild)
                index += 1


def _remove_comment_markers(root: ET.Element) -> None:
    tags = {
        f"{{{W_NS}}}commentRangeStart",
        f"{{{W_NS}}}commentRangeEnd",
        f"{{{W_NS}}}commentReference",
    }
    for parent in root.iter():
        for child in list(parent):
            if child.tag in tags:
                parent.remove(child)


def _remove_authorized_watermarks(root: ET.Element, authorized: list[str]) -> int:
    authorized_folded = [item.casefold() for item in authorized if item]
    if not authorized_folded:
        return 0
    removed = 0
    for parent in root.iter():
        for child in list(parent):
            serialized = ET.tostring(child, encoding="unicode").casefold()
            if any(term in serialized for term in authorized_folded) and (
                child.tag.endswith("}pict") or child.tag.endswith("}shape") or child.tag.endswith("}sdt")
            ):
                parent.remove(child)
                removed += 1
    return removed


def _remove_exact_internal_ids(root: ET.Element, values: list[str]) -> int:
    removed = 0
    for node in root.iter(f"{{{W_NS}}}t"):
        if not node.text:
            continue
        for value in values:
            count = node.text.count(value)
            if count:
                node.text = node.text.replace(value, "")
                removed += count
    return removed


def _remove_internal_notes(root: ET.Element, allowed_hashes: set[str]) -> list[dict[str, Any]]:
    pattern = re.compile(r"\[(?:内部备注|审稿词)[:：][^\]]+\]|\[\[INTERNAL:[^\]]+\]\]", flags=re.I)
    removed: list[dict[str, Any]] = []
    for node in root.iter(f"{{{W_NS}}}t"):
        if not node.text:
            continue
        values = pattern.findall(node.text)
        for value in values:
            digest = sha256_bytes(value.encode("utf-8"))
            if digest in allowed_hashes:
                removed.append({"text_sha256": digest, "length": len(value)})
                node.text = node.text.replace(value, "")
    return removed


def _clear_metadata(name: str, root: ET.Element) -> int:
    cleared = 0
    if name == "docProps/core.xml":
        date_tags = {f"{{{DCTERMS_NS}}}created", f"{{{DCTERMS_NS}}}modified"}
        for node in list(root):
            if node.tag in date_tags:
                root.remove(node)
                cleared += 1
        allowed = {
            f"{{{DC_NS}}}creator",
            f"{{{CP_NS}}}lastModifiedBy",
            f"{{{CP_NS}}}keywords",
            f"{{{DC_NS}}}subject",
            f"{{{DC_NS}}}description",
            f"{{{CP_NS}}}category",
        }
        for node in root.iter():
            if node.tag in allowed and node.text:
                node.text = ""
                cleared += 1
    elif name == "docProps/app.xml":
        for local_name in ("Company", "Manager", "HyperlinkBase"):
            node = root.find(f"{{{EP_NS}}}{local_name}")
            if node is not None and node.text:
                node.text = ""
                cleared += 1
    return cleared


def sanitize_docx(
    source: Path,
    output: Path,
    approval: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    if source.suffix.casefold() != ".docx" or output.suffix.casefold() != ".docx":
        raise LegalCaseError("DOCX_REQUIRED", "sanitize-docx 的输入和输出都必须是 .docx。")
    if not source.is_file():
        raise LegalCaseError("FILE_NOT_FOUND", f"源 DOCX 不存在：{source}")
    if output.exists() or report_path.exists():
        raise LegalCaseError("OUTPUT_EXISTS", "输出或清洁报告已存在；为保留版本链，脚本拒绝覆盖。")
    ensure_not_originals(output)
    ensure_not_originals(report_path)
    if approval.get("ownership") != "self_generated":
        raise LegalCaseError(
            "THIRD_PARTY_OR_UNCONFIRMED_OWNERSHIP",
            "仅允许清洁已确认自有生成稿；第三方证据及权属不明文件必须阻止。",
        )
    if not approval.get("approval_id") or not approval.get("approved_by") or not approval.get("approved_at"):
        raise LegalCaseError("INVALID_SANITIZATION_APPROVAL", "批准文件必须包含 approval_id、approved_by、approved_at。")
    before_hash = sha256_file(source)
    before_preflight = preflight(source)
    if any(item.get("kind") == "unresolved_placeholder" for item in before_preflight["findings"]):
        raise LegalCaseError("UNRESOLVED_PLACEHOLDER", "未决占位符属于实质待办，清洁命令必须先阻断，不能删除或绕过。")
    if any(item.get("kind") == "hidden_text" for item in before_preflight["findings"]):
        raise LegalCaseError("HIDDEN_TEXT_REQUIRES_REVIEW", "隐藏文字可能是实质内容，不在自动清洁白名单内。")
    target_groups = _validate_sanitization_approval(source, approval, before_preflight)
    approved_items = set(target_groups)

    removed_parts: set[str] = set()
    removed_note_records: list[dict[str, Any]] = []
    item_results: list[dict[str, Any]] = []
    source_bytes = source.read_bytes()
    try:
        source_zip = zipfile.ZipFile(io.BytesIO(source_bytes))
    except zipfile.BadZipFile as exc:
        raise LegalCaseError("INVALID_DOCX", "输入不是有效 OOXML DOCX。") from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with source_zip, zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
        for info in source_zip.infolist():
            name = info.filename
            data = source_zip.read(name)
            metadata_locations = {target["location"] for target in target_groups.get("authorized_metadata", [])}
            if name == "docProps/custom.xml" and name in metadata_locations:
                removed_parts.add(name)
                continue
            if not name.endswith(".xml") and not name.endswith(".rels") and name != "[Content_Types].xml":
                target_zip.writestr(info, data)
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                target_zip.writestr(info, data)
                continue
            changed = False
            before_xml = ET.tostring(root, encoding="utf-8")
            if target_groups.get("comments") and name.startswith("word/"):
                _remove_comment_markers(root)
                if name in {target["location"] for target in target_groups["comments"]}:
                    for child in list(root):
                        root.remove(child)
            if "docProps/custom.xml" in metadata_locations:
                _remove_custom_metadata_relationships(name, root)
            if name in {target["location"] for target in target_groups.get("tracked_changes", [])}:
                _flatten_tracked_changes(root)
            watermark_values = [
                target["value"] for target in target_groups.get("draft_watermark", []) if target["location"] == name
            ]
            if watermark_values:
                _remove_authorized_watermarks(root, watermark_values)
            internal_id_values = [
                target["value"] for target in target_groups.get("internal_ids", []) if target["location"] == name
            ]
            if internal_id_values:
                _remove_exact_internal_ids(root, internal_id_values)
            internal_note_hashes = {
                target["original_text_sha256"]
                for target in target_groups.get("internal_notes", [])
                if target["location"] == name
            }
            if internal_note_hashes:
                for record in _remove_internal_notes(root, internal_note_hashes):
                    removed_note_records.append({"location": name, **record})
            if name in metadata_locations:
                _clear_metadata(name, root)
            after_xml = _serialize_ooxml(root, data)
            changed = before_xml != ET.tostring(root, encoding="utf-8")
            target_zip.writestr(info, after_xml if changed else data)

    output.write_bytes(buffer.getvalue())
    if sha256_file(source) != before_hash:
        output.unlink(missing_ok=True)
        raise LegalCaseError("ORIGINAL_CHANGED", "清洁过程中检测到源文件哈希变化，已删除派生件。")
    after_preflight = preflight(output)
    finding_kind = {
        "comments": {"comments", "extended_comments"},
        "tracked_changes": {"tracked_changes"},
        "draft_watermark": {"watermark_candidate"},
        "authorized_metadata": {"metadata"},
        "internal_ids": {"internal_id"},
        "internal_notes": {"internal_note"},
    }
    with zipfile.ZipFile(output) as cleaned_package:
        for target in approval["approved_targets"]:
            location = target["location"]
            remaining = any(
                finding.get("kind") in finding_kind[target["kind"]] and finding.get("location") == location
                for finding in after_preflight["findings"]
            )
            if target["kind"] in {"internal_ids", "draft_watermark"} and location in cleaned_package.namelist():
                remaining = target.get("value", "") in cleaned_package.read(location).decode("utf-8", errors="ignore")
            if target["kind"] == "internal_notes" and location in cleaned_package.namelist():
                decoded = cleaned_package.read(location).decode("utf-8", errors="ignore")
                note_values = re.findall(r"\[(?:内部备注|审稿词)[:：][^\]]+\]|\[\[INTERNAL:[^\]]+\]\]", decoded, flags=re.I)
                remaining = target.get("original_text_sha256") in {
                    sha256_bytes(value.encode("utf-8")) for value in note_values
                }
            item_results.append({
                "item_id": target["item_id"],
                "kind": target["kind"],
                "location": location,
                "reason": "律师逐项批准的自有生成稿清洁",
                "authorization": approval["approval_id"],
                "ownership": target["ownership"],
                "authority_basis": target["authority_basis"],
                "result": "blocked" if remaining else "removed",
                "original_text_sha256": target.get("original_text_sha256"),
                "removed_count": 0 if remaining else int(target.get("expected_count", 1)),
                "source_part_sha256": target.get("source_part_sha256"),
                "value": target.get("value"),
            })
    blocked = [
        item.get("kind", "unknown")
        for item in after_preflight["findings"]
        if item.get("severity") in {"blocker", "requires_authorization"}
    ]
    source_artifact_id = approval.get("source_artifact_id", f"R-SOURCE-{before_hash[:12]}")
    derived_hash = sha256_file(output)
    derived_artifact_id = approval.get("derived_artifact_id", f"R-CLEAN-{derived_hash[:12]}")
    report = {
        "id": approval.get("report_id", f"SR-{uuid.uuid4().hex[:16]}"),
        "source_artifact_id": source_artifact_id,
        "source_sha256": before_hash,
        "derived_artifact_id": derived_artifact_id,
        "derived_sha256": derived_hash,
        "approved_item_ids": [target["item_id"] for target in approval["approved_targets"]],
        "items": item_results,
        "blocked_items": sorted(set(blocked)),
        "rereview_status": "required",
        "created_at": now_iso(),
        "source_path": str(source),
        "derived_path": str(output),
        "original_hash_unchanged": sha256_file(source) == before_hash,
        "removed_parts": sorted(removed_parts),
        "removed_internal_note_records": removed_note_records,
        "post_clean_preflight": after_preflight,
        "notice": "派生件必须再次独立审阅；本报告不构成法院提交批准。",
    }
    atomic_write_json(report_path, report)
    return report


def _read_comparable_lines(path: Path) -> list[str]:
    if path.suffix.casefold() == ".docx":
        text = extract_docx_text(path)
    elif path.suffix.casefold() in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8-sig")
    else:
        raise LegalCaseError("UNSUPPORTED_DIFF_TYPE", "min-diff 只支持 DOCX、TXT 或 Markdown。")
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def minimal_diff(
    original: Path,
    modified: Path,
    allowed_ranges: list[tuple[int, int]],
    max_change_ratio: float,
) -> dict[str, Any]:
    before = _read_comparable_lines(original)
    after = _read_comparable_lines(modified)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    changed_original_lines: set[int] = set()
    operations: list[dict[str, Any]] = []
    changed_units = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed_units += max(i2 - i1, j2 - j1)
        changed_original_lines.update(range(i1 + 1, i2 + 1))
        if tag == "insert":
            anchor = min(max(i1, 1), max(len(before), 1))
            changed_original_lines.add(anchor)
        operations.append({"kind": tag, "original_lines": [i1 + 1, i2], "modified_lines": [j1 + 1, j2]})
    denominator = max(len(before), len(after), 1)
    ratio = changed_units / denominator

    def allowed(line: int) -> bool:
        return any(start <= line <= end for start, end in allowed_ranges)

    outside = sorted(line for line in changed_original_lines if allowed_ranges and not allowed(line))
    blockers: list[str] = []
    if outside:
        blockers.append(f"发现授权范围外改动，原稿行号：{outside}")
    if ratio > max_change_ratio:
        blockers.append(f"改动比例 {ratio:.3f} 超过阈值 {max_change_ratio:.3f}")
    return {
        "ok": not blockers,
        "original": str(original),
        "modified": str(modified),
        "original_sha256": sha256_file(original),
        "modified_sha256": sha256_file(modified),
        "change_ratio": round(ratio, 6),
        "allowed_ranges": [f"{start}:{end}" for start, end in allowed_ranges],
        "operations": operations,
        "outside_allowed_lines": outside,
        "blockers": blockers,
    }


def _enable_bundled_pdf_packages() -> None:
    """Expose the Codex bundled pure/local PDF packages to the matching Python ABI."""
    candidates = [
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "Lib" / "site-packages",
    ]
    for candidate in candidates:
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


def _try_pdf_backend() -> tuple[Any | None, str | None]:
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore

        return (PdfReader, PdfWriter), "pypdf"
    except ImportError:
        _enable_bundled_pdf_packages()
        try:
            from pypdf import PdfReader, PdfWriter  # type: ignore

            return (PdfReader, PdfWriter), "pypdf-bundled"
        except ImportError:
            try:
                from PyPDF2 import PdfReader, PdfWriter  # type: ignore

                return (PdfReader, PdfWriter), "PyPDF2"
            except ImportError:
                return None, None


def _try_page_number_backend() -> tuple[Any | None, str | None]:
    try:
        from reportlab.pdfgen import canvas  # type: ignore

        return canvas, "reportlab"
    except ImportError:
        _enable_bundled_pdf_packages()
        try:
            from reportlab.pdfgen import canvas  # type: ignore

            return canvas, "reportlab-bundled"
        except ImportError:
            return None, None


def bundle_pdfs(inputs: list[tuple[Path, str | None]], output: Path, page_numbers: bool) -> dict[str, Any]:
    ensure_not_originals(output)
    if output.suffix.casefold() != ".pdf":
        raise LegalCaseError("PDF_OUTPUT_REQUIRED", "组卷输出必须是 .pdf。")
    if output.exists():
        raise LegalCaseError("OUTPUT_EXISTS", "组卷输出已存在；脚本拒绝覆盖。")
    for path, _bookmark in inputs:
        if path.suffix.casefold() != ".pdf" or not path.is_file():
            raise LegalCaseError("INVALID_BUNDLE_INPUT", f"组卷输入必须是存在的 PDF：{path}")
    backend, backend_name = _try_pdf_backend()
    if backend is None:
        return {
            "ok": False,
            "degraded": True,
            "code": "PDF_BACKEND_UNAVAILABLE",
            "message": "未安装 pypdf/PyPDF2，未生成伪 PDF；请先提供可用 PDF 后端再组卷。",
            "inputs": [str(path) for path, _ in inputs],
            "output": str(output),
        }
    page_canvas = None
    page_backend_name = None
    if page_numbers:
        page_canvas, page_backend_name = _try_page_number_backend()
    if page_numbers and page_canvas is None:
        return {
            "ok": False,
            "degraded": True,
            "code": "PAGE_NUMBER_BACKEND_UNAVAILABLE",
            "message": "当前离线环境没有可靠的 reportlab 页码叠加后端，未改写 PDF；可先不加页码组卷，或配置受控后端后重试。",
            "inputs": [str(path) for path, _ in inputs],
            "output": str(output),
        }
    PdfReader, PdfWriter = backend
    writer = PdfWriter()
    readers = [(PdfReader(str(path)), bookmark) for path, bookmark in inputs]
    total_pages = sum(len(reader.pages) for reader, _bookmark in readers)
    written_pages = 0
    bookmarks_added = 0
    for reader, bookmark in readers:
        start = written_pages
        for page in reader.pages:
            if page_numbers:
                width = float(page.mediabox.width)
                height = float(page.mediabox.height)
                overlay_buffer = io.BytesIO()
                overlay = page_canvas.Canvas(overlay_buffer, pagesize=(width, height), pageCompression=1)
                overlay.setFont("Helvetica", 9)
                overlay.drawCentredString(width / 2, 18, f"{written_pages + 1} / {total_pages}")
                overlay.save()
                overlay_buffer.seek(0)
                overlay_page = PdfReader(overlay_buffer).pages[0]
                page.merge_page(overlay_page, over=True, expand=False)
                # ReportLab may contribute an empty /Annots array while
                # creating the numbering overlay. It is not an annotation,
                # but strict filing preflight treats any /Annots container as
                # suspicious. Remove only an empty array; real annotations are
                # never removed here.
                annotations = page.get("/Annots")
                if annotations is not None and len(annotations) == 0:
                    del page["/Annots"]
            writer.add_page(page)
            written_pages += 1
        if bookmark:
            add_outline = getattr(writer, "add_outline_item", None) or getattr(writer, "addBookmark", None)
            if add_outline:
                add_outline(bookmark, start)
                bookmarks_added += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        writer.write(handle)
    if not temporary.read_bytes().startswith(b"%PDF-"):
        temporary.unlink(missing_ok=True)
        raise LegalCaseError("INVALID_PDF_OUTPUT", "组卷结果没有PDF签名；未写入目标文件。")
    verification = PdfReader(str(temporary))
    if len(verification.pages) != total_pages:
        temporary.unlink(missing_ok=True)
        raise LegalCaseError("PDF_PAGE_COUNT_MISMATCH", "组卷后页数与输入总页数不一致；未写入目标文件。")
    if page_numbers:
        missing_numbers = []
        for index, page in enumerate(verification.pages, start=1):
            expected = f"{index} / {total_pages}"
            try:
                extracted = page.extract_text() or ""
            except Exception:
                extracted = ""
            if expected not in extracted:
                missing_numbers.append(index)
        if missing_numbers:
            temporary.unlink(missing_ok=True)
            raise LegalCaseError(
                "PAGE_NUMBER_VERIFICATION_FAILED",
                "页码叠加后无法从指定页面复核页脚；未写入目标文件。",
                {"pages": missing_numbers},
            )
    temporary.replace(output)
    return {
        "ok": True,
        "degraded": False,
        "backend": backend_name,
        "output": str(output),
        "sha256": sha256_file(output),
        "page_count": total_pages,
        "bookmarks_added": bookmarks_added,
        "page_numbers_added": bool(page_numbers),
        "page_number_backend": page_backend_name,
        "page_number_text_verified": bool(page_numbers),
        "visual_qa_required": True,
    }


def write_print_sheet(
    output: Path,
    matter: dict[str, Any],
    package: dict[str, Any],
    state_hash: str,
) -> dict[str, Any]:
    ensure_not_originals(output)
    if output.exists():
        raise LegalCaseError("OUTPUT_EXISTS", "打印生产单已存在；脚本拒绝覆盖。")
    lines = [
        "# 打印生产单（候选）",
        "",
        f"- 案件：{matter.get('title') or matter.get('id')}",
        f"- 案件ID：{matter.get('id')}",
        f"- 包清单：{package.get('id')} v{package.get('version')}",
        f"- 清单哈希：`{package.get('manifest_hash')}`",
        f"- 状态哈希：`{state_hash}`",
        f"- 生成时间：{now_iso()}",
        "- 当前性质：待人工核对和批准；本单不会触发打印、发送、上传或提交。",
        "",
        "| 顺序 | 文件 | 对象ID | 版本 | 哈希 | 书签 | 份数/单双面/签章 |",
        "|---:|---|---|---:|---|---|---|",
    ]
    for item in sorted(package.get("items", []), key=lambda value: value.get("order", 0)):
        lines.append(
            f"| {item.get('order')} | {item.get('path')} | {item.get('object_id')} | {item.get('version')} | "
            f"`{item.get('hash')}` | {item.get('bookmark') or ''} | 待填 |"
        )
    lines.extend([
        "",
        "## 人工检查",
        "",
        "- [ ] G2证据集合与清单一致",
        "- [ ] 文书均为非失效且审阅通过的法院候选",
        "- [ ] 批注、修订、内部ID、占位符和未授权水印为零",
        "- [ ] 当事人、案号、法院、日期、金额、页码和份数核对",
        "- [ ] 需要签字、盖章、骑缝章或原件核验的位置已标注",
        "- [ ] 已取得与精确包哈希绑定的G4批准",
        "",
        "> 本文件只生成生产清单，不执行任何外部动作。",
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "output": str(output), "sha256": sha256_file(output), "item_count": len(package.get("items", []))}
