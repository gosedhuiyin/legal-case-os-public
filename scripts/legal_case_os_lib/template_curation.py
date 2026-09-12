from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from .core import (
    LegalCaseError,
    atomic_write_json,
    ensure_not_originals,
    exclusive_file_lock,
    load_json,
    now_iso,
    sha256_file,
)


SCHEMA_VERSION = "1.0.0"
ENGINE_VERSION = "1.2.0"
USAGE_MODES = ("reference", "fillable_clone", "hybrid")
FIELD_POLICIES = (
    "fixed_locked",
    "required_verified",
    "conditional_verified",
    "optional_verified",
    "derived_needs_confirmation",
    "lawyer_decision_required",
    "manual_blank",
    "repeatable",
)
EDITABLE_POLICIES = frozenset(FIELD_POLICIES) - {"fixed_locked"}
DIRECT_SOURCE_KINDS = frozenset({"original_material", "verified_authority"})
FORBIDDEN_TRANSFER_DEFAULT = (
    "client_facts",
    "names",
    "dates",
    "amounts",
    "claims",
    "evidence",
    "authorities",
    "conclusions",
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W_T = f"{{{W_NS}}}t"
W_P = f"{{{W_NS}}}p"
W_R = f"{{{W_NS}}}r"
W_RPR = f"{{{W_NS}}}rPr"
W_PPR = f"{{{W_NS}}}pPr"
W_TBL = f"{{{W_NS}}}tbl"
W_TR = f"{{{W_NS}}}tr"
W_TC = f"{{{W_NS}}}tc"
W_SECTPR = f"{{{W_NS}}}sectPr"

_DANGEROUS_CLONE_LOCALS = frozenset({
    "altChunk", "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd",
    "commentReference", "del", "drawing", "endnoteReference", "fldSimple", "footnoteReference",
    "hyperlink", "ins", "instrText", "moveFrom", "moveTo", "object", "pict", "sdt", "sectPr",
    "smartTag", "tbl", "vMerge",
})

_PREFIXES = {
    W_NS: "w",
    R_NS: "r",
    XML_NS: "xml",
}
_PLACEHOLDER_PATTERNS = (
    (re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}$"), "mustache"),
    (re.compile(r"^\[\[\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\]\]$"), "brackets"),
)
_RAW_WT_RE = re.compile(
    rb"<(?P<prefix>[A-Za-z_][\w.-]*):t(?P<attrs>\s[^<>]*?)?"
    rb"(?:(?P<self>/>)|>(?P<body>.*?)</(?P=prefix):t\s*>)",
    re.DOTALL,
)
_INVALID_XML_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _error(code: str, message: str, **details: Any) -> LegalCaseError:
    return LegalCaseError(code, message, details or None)


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _subtree_digest(element_or_elements: ET.Element | Iterable[ET.Element]) -> str:
    """Hash exact OOXML subtree semantics without depending on namespace prefixes."""

    elements = [element_or_elements] if isinstance(element_or_elements, ET.Element) else list(element_or_elements)
    digest = hashlib.sha256()

    def visit(element: ET.Element) -> None:
        digest.update(_qname(element.tag).encode("utf-8"))
        for key, value in sorted(element.attrib.items(), key=lambda item: _qname(item[0])):
            digest.update(b"\x00")
            digest.update(_qname(key).encode("utf-8"))
            digest.update(b"=")
            digest.update(value.encode("utf-8"))
        digest.update(b"{")
        digest.update((element.text or "").encode("utf-8"))
        digest.update(b"[")
        for child in list(element):
            visit(child)
        digest.update(b"]}")

    for element in elements:
        visit(element)
        digest.update(b"\xff")
    return digest.hexdigest()


def _safe_filename(value: str) -> str:
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value.strip())
    normalized = re.sub(r"\s+", "-", normalized).strip(".-")
    return normalized or "template"


def _qname(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        namespace, local = tag[1:].split("}", 1)
        return f"{_PREFIXES.get(namespace, 'ns')}:{local}"
    return tag


def _require_docx(path: Path) -> None:
    if path.suffix.casefold() != ".docx":
        raise _error("DOCX_REQUIRED", f"模板蒸馏或保真填充只接受 DOCX：{path}")
    if not path.is_file():
        raise _error("TEMPLATE_FILE_MISSING", f"模板文件不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise _error("INVALID_DOCX_PACKAGE", f"文件不是有效的 DOCX/ZIP 包：{path}")


def _read_zip_entries(path: Path) -> list[tuple[zipfile.ZipInfo, bytes]]:
    _require_docx(path)
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(path, "r") as package:
            for info in package.infolist():
                if info.filename in seen:
                    raise _error(
                        "DOCX_DUPLICATE_PART",
                        f"DOCX 包含重复部件，禁止保真修改：{info.filename}",
                    )
                seen.add(info.filename)
                entries.append((info, package.read(info.filename)))
    except zipfile.BadZipFile as exc:
        raise _error("INVALID_DOCX_PACKAGE", f"DOCX 包无法解析：{path}") from exc
    return entries


def _xml_structure_hash(data: bytes) -> str | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    digest = hashlib.sha256()

    def visit(element: ET.Element) -> None:
        digest.update(_qname(element.tag).encode("utf-8"))
        for key, value in sorted(element.attrib.items(), key=lambda item: _qname(item[0])):
            digest.update(b"\x00")
            digest.update(_qname(key).encode("utf-8"))
            digest.update(b"=")
            digest.update(value.encode("utf-8"))
        digest.update(b"[")
        for child in list(element):
            visit(child)
        digest.update(b"]")

    visit(root)
    return digest.hexdigest()


def package_inventory(path: Path | str) -> dict[str, dict[str, Any]]:
    """Return content and structure hashes for every DOCX package part."""
    source = Path(path).resolve()
    inventory: dict[str, dict[str, Any]] = {}
    for info, data in _read_zip_entries(source):
        inventory[info.filename] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "structure_sha256": _xml_structure_hash(data),
            "compression": int(info.compress_type),
        }
    return inventory


def compare_docx_packages(
    reference: Path | str,
    candidate: Path | str,
    *,
    allowed_changed_parts: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare parts bytewise and XML structure independently.

    Text-only edits may change the content hash of an explicitly allowed XML
    part, but its structure hash must remain identical. No part may appear or
    disappear.
    """
    before = package_inventory(reference)
    after = package_inventory(candidate)
    allowed = set(allowed_changed_parts)
    before_names = set(before)
    after_names = set(after)
    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)
    changed = sorted(
        name for name in before_names & after_names if before[name]["sha256"] != after[name]["sha256"]
    )
    unexpected = sorted(set(changed) - allowed)
    structure_changed = sorted(
        name
        for name in set(changed) & allowed
        if before[name]["structure_sha256"] != after[name]["structure_sha256"]
    )
    return {
        "ok": not added and not removed and not unexpected and not structure_changed,
        "added_parts": added,
        "removed_parts": removed,
        "changed_parts": changed,
        "unexpected_changed_parts": unexpected,
        "structure_changed_parts": structure_changed,
        "unchanged_part_count": len(before_names & after_names) - len(changed),
    }


def _xml_parts(entries: list[tuple[zipfile.ZipInfo, bytes]]) -> dict[str, bytes]:
    selected: dict[str, bytes] = {}
    for info, data in entries:
        name = info.filename
        if name == "word/document.xml" or re.fullmatch(
            r"word/(?:header|footer)\d+\.xml|word/(?:footnotes|endnotes)\.xml", name
        ):
            selected[name] = data
    return selected


def _element_paths(root: ET.Element) -> dict[int, str]:
    paths: dict[int, str] = {id(root): f"/{_qname(root.tag)}[1]"}

    def walk(parent: ET.Element) -> None:
        counts: dict[str, int] = {}
        parent_path = paths[id(parent)]
        for child in list(parent):
            name = _qname(child.tag)
            counts[name] = counts.get(name, 0) + 1
            paths[id(child)] = f"{parent_path}/{name}[{counts[name]}]"
            walk(child)

    walk(root)
    return paths


def _placeholder_info(text: str) -> tuple[str | None, str | None]:
    stripped = text.strip()
    for pattern, kind in _PLACEHOLDER_PATTERNS:
        match = pattern.fullmatch(stripped)
        if match:
            return match.group(1), kind
    if stripped and re.fullmatch(r"[_＿]{3,}", stripped):
        return None, "underline"
    return None, None


def _text_nodes(part: str, data: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise _error("DOCX_XML_INVALID", f"DOCX XML 无法解析：{part}", detail=str(exc)) from exc
    paths = _element_paths(root)
    nodes: list[dict[str, Any]] = []
    for index, element in enumerate((item for item in root.iter() if item.tag == W_T), start=1):
        text = element.text or ""
        field_key, placeholder_kind = _placeholder_info(text)
        nodes.append(
            {
                "part": part,
                "path": paths[id(element)],
                "text_node_index": index,
                "expected_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "character_count": len(text),
                "field_key": field_key,
                "placeholder_kind": placeholder_kind,
            }
        )
    return nodes


def _paragraph_metrics(part: str, data: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    paths = _element_paths(root)
    result: list[dict[str, Any]] = []
    for index, paragraph in enumerate((item for item in root.iter() if item.tag == W_P), start=1):
        texts = [(node.text or "") for node in paragraph.iter() if node.tag == W_T]
        text = "".join(texts)
        style_node = paragraph.find(f"./{{{W_NS}}}pPr/{{{W_NS}}}pStyle")
        align_node = paragraph.find(f"./{{{W_NS}}}pPr/{{{W_NS}}}jc")
        style_id = style_node.get(f"{{{W_NS}}}val") if style_node is not None else None
        alignment = align_node.get(f"{{{W_NS}}}val") if align_node is not None else None
        run_count = sum(1 for node in paragraph if node.tag == f"{{{W_NS}}}r")
        bold_run_count = sum(
            1
            for run in paragraph.findall(f".//{{{W_NS}}}r")
            if run.find(f"./{{{W_NS}}}rPr/{{{W_NS}}}b") is not None
        )
        if style_id and re.search(r"(?:title|heading|标题)", style_id, re.IGNORECASE):
            role_hint = "heading"
        elif alignment == "center" and 0 < len(text) <= 50:
            role_hint = "centered_heading_candidate"
        elif not text:
            role_hint = "blank_spacing_or_container"
        else:
            role_hint = "body"
        result.append(
            {
                "paragraph_index": index,
                "part": part,
                "path": paths[id(paragraph)],
                "style_id": style_id,
                "alignment": alignment,
                "character_count": len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "run_count": run_count,
                "bold_run_count": bold_run_count,
                "sentence_mark_count": len(re.findall(r"[。！？!?；;]", text)),
                "citation_mark_count": len(re.findall(r"第.{0,16}条|〔?\d{4}〕?.{0,12}号", text)),
                "role_hint": role_hint,
            }
        )
    return result


def _layout_summary(xml_parts: dict[str, bytes]) -> dict[str, Any]:
    paragraph_count = 0
    table_count = 0
    section_count = 0
    text_node_count = 0
    empty_table_cells: list[dict[str, str]] = []
    for part, data in xml_parts.items():
        root = ET.fromstring(data)
        paths = _element_paths(root)
        paragraph_count += sum(1 for item in root.iter() if item.tag == W_P)
        table_count += sum(1 for item in root.iter() if item.tag == W_TBL)
        section_count += sum(1 for item in root.iter() if item.tag == W_SECTPR)
        text_node_count += sum(1 for item in root.iter() if item.tag == W_T)
        for cell in (item for item in root.iter() if item.tag == W_TC):
            if not any((node.text or "").strip() for node in cell.iter() if node.tag == W_T):
                empty_table_cells.append({"part": part, "path": paths[id(cell)]})
    return {
        "paragraph_count": paragraph_count,
        "table_count": table_count,
        "section_count": section_count,
        "text_node_count": text_node_count,
        "unanchored_empty_table_cells": empty_table_cells,
    }


def _attribute_map(element: ET.Element | None) -> dict[str, str]:
    if element is None:
        return {}
    return {_qname(key): value for key, value in sorted(element.attrib.items(), key=lambda item: _qname(item[0]))}


def _format_metrics(source: Path, xml_parts: dict[str, bytes]) -> dict[str, Any]:
    document_root = ET.fromstring(xml_parts["word/document.xml"])
    paths = _element_paths(document_root)
    sections: list[dict[str, Any]] = []
    for index, section in enumerate((item for item in document_root.iter() if item.tag == W_SECTPR), start=1):
        sections.append(
            {
                "section_index": index,
                "path": paths[id(section)],
                "page_size": _attribute_map(section.find(f"./{{{W_NS}}}pgSz")),
                "page_margins": _attribute_map(section.find(f"./{{{W_NS}}}pgMar")),
                "columns": _attribute_map(section.find(f"./{{{W_NS}}}cols")),
                "document_grid": _attribute_map(section.find(f"./{{{W_NS}}}docGrid")),
                "break_type": _attribute_map(section.find(f"./{{{W_NS}}}type")),
                "title_page": section.find(f"./{{{W_NS}}}titlePg") is not None,
            }
        )
    tables: list[dict[str, Any]] = []
    for index, table in enumerate((item for item in document_root.iter() if item.tag == W_TBL), start=1):
        direct_rows = table.findall(f"./{{{W_NS}}}tr")
        direct_cells = [cell for row in direct_rows for cell in row.findall(f"./{{{W_NS}}}tc")]
        grid = table.find(f"./{{{W_NS}}}tblGrid")
        tables.append(
            {
                "table_index": index,
                "path": paths[id(table)],
                "row_count": len(direct_rows),
                "cell_count": len(direct_cells),
                "grid_widths": [
                    item.get(f"{{{W_NS}}}w")
                    for item in (grid.findall(f"./{{{W_NS}}}gridCol") if grid is not None else [])
                ],
                "table_width": _attribute_map(table.find(f"./{{{W_NS}}}tblPr/{{{W_NS}}}tblW")),
                "grid_span_count": sum(1 for cell in direct_cells if cell.find(f"./{{{W_NS}}}tcPr/{{{W_NS}}}gridSpan") is not None),
                "vertical_merge_count": sum(1 for cell in direct_cells if cell.find(f"./{{{W_NS}}}tcPr/{{{W_NS}}}vMerge") is not None),
                "repeating_header_row_count": sum(
                    1 for row in direct_rows if row.find(f"./{{{W_NS}}}trPr/{{{W_NS}}}tblHeader") is not None
                ),
            }
        )
    package_bytes = {info.filename: data for info, data in _read_zip_entries(source)}
    style_catalog: list[dict[str, Any]] = []
    styles_data = package_bytes.get("word/styles.xml")
    if styles_data:
        styles_root = ET.fromstring(styles_data)
        for style in styles_root.findall(f"./{{{W_NS}}}style"):
            style_catalog.append(
                {
                    "style_id": style.get(f"{{{W_NS}}}styleId"),
                    "style_type": style.get(f"{{{W_NS}}}type"),
                    "name": _attribute_map(style.find(f"./{{{W_NS}}}name")).get("w:val"),
                    "based_on": _attribute_map(style.find(f"./{{{W_NS}}}basedOn")).get("w:val"),
                    "next_style": _attribute_map(style.find(f"./{{{W_NS}}}next")).get("w:val"),
                    "paragraph_properties_structure_sha256": _xml_structure_hash(
                        ET.tostring(style.find(f"./{{{W_NS}}}pPr"), encoding="utf-8")
                    )
                    if style.find(f"./{{{W_NS}}}pPr") is not None
                    else None,
                    "run_properties_structure_sha256": _xml_structure_hash(
                        ET.tostring(style.find(f"./{{{W_NS}}}rPr"), encoding="utf-8")
                    )
                    if style.find(f"./{{{W_NS}}}rPr") is not None
                    else None,
                }
            )
    all_roots = [ET.fromstring(data) for data in xml_parts.values()]

    def count(tag: str) -> int:
        qualified = f"{{{W_NS}}}{tag}"
        return sum(sum(1 for node in root.iter() if node.tag == qualified) for root in all_roots)

    numbering_data = package_bytes.get("word/numbering.xml")
    numbering = {"abstract_definition_count": 0, "instance_count": 0}
    if numbering_data:
        numbering_root = ET.fromstring(numbering_data)
        numbering = {
            "abstract_definition_count": len(numbering_root.findall(f"./{{{W_NS}}}abstractNum")),
            "instance_count": len(numbering_root.findall(f"./{{{W_NS}}}num")),
        }
    return {
        "sections": sections,
        "tables": tables,
        "style_catalog": style_catalog,
        "numbering": numbering,
        "annotations": {
            "drawing_count": count("drawing") + count("pict"),
            "hyperlink_count": count("hyperlink"),
            "bookmark_count": count("bookmarkStart"),
            "comment_reference_count": count("commentReference"),
            "footnote_reference_count": count("footnoteReference"),
            "endnote_reference_count": count("endnoteReference"),
            "field_instruction_count": count("instrText") + count("fldSimple"),
            "highlight_count": count("highlight"),
            "underline_count": count("u"),
        },
        "header_part_count": sum(1 for part in xml_parts if part.startswith("word/header")),
        "footer_part_count": sum(1 for part in xml_parts if part.startswith("word/footer")),
    }


def _argument_profile(xml_parts: dict[str, bytes]) -> dict[str, Any]:
    text = "".join(
        node.text or ""
        for data in xml_parts.values()
        for node in ET.fromstring(data).iter()
        if node.tag == W_T
    )
    groups = {
        "sequence": ("首先", "其次", "再次", "最后", "一是", "二是", "三是"),
        "contrast": ("但是", "然而", "相反", "反之", "即便", "纵使"),
        "conclusion": ("综上", "据此", "因此", "由此可见"),
        "counterargument": ("不能成立", "缺乏依据", "无法证明", "不予认可"),
        "evidence_link": ("证据", "证明", "记载", "显示", "印证"),
        "authority_link": ("规定", "第", "条", "司法解释", "指导案例"),
    }
    sentence_lengths = [len(item) for item in re.split(r"[。！？!?；;]", text) if item]
    return {
        "marker_counts": {
            group: sum(text.count(marker) for marker in markers) for group, markers in groups.items()
        },
        "sentence_count": len(sentence_lengths),
        "average_sentence_length": round(sum(sentence_lengths) / len(sentence_lengths), 2)
        if sentence_lengths
        else 0,
        "max_sentence_length": max(sentence_lengths, default=0),
    }


def validate_usage_mode(
    usage_mode: str,
    *,
    profile_kind: str | None = None,
    editable_slot_count: int | None = None,
    ready_for_use: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    if usage_mode not in USAGE_MODES:
        errors.append(f"usage_mode 必须为 {list(USAGE_MODES)} 之一")
    if profile_kind is not None and profile_kind not in {"form", "writing"}:
        errors.append("profile_kind 必须为 form 或 writing")
    if usage_mode == "fillable_clone" and profile_kind not in {None, "form"}:
        errors.append("fillable_clone 只能用于 FormProfile")
    if usage_mode == "reference" and editable_slot_count not in {None, 0}:
        errors.append("reference 模式不得登记可编辑槽位")
    if ready_for_use and usage_mode in {"fillable_clone", "hybrid"} and not editable_slot_count:
        errors.append(f"{usage_mode} 激活前至少需要一个已批准的可编辑槽位")
    return {"ok": not errors, "errors": errors}


def _apply_form_overrides(profile: dict[str, Any], overrides: dict[str, Any]) -> None:
    policies = overrides.get("slot_policies", {})
    by_id = {slot["slot_id"]: slot for slot in profile["slots"]}
    by_field: dict[str, list[dict[str, Any]]] = {}
    for candidate in profile["slots"]:
        if candidate.get("field_key"):
            by_field.setdefault(candidate["field_key"], []).append(candidate)
    for key, raw_rule in policies.items():
        rule = raw_rule if isinstance(raw_rule, dict) else {"policy": raw_rule}
        targets = [by_id[key]] if key in by_id else by_field.get(key, [])
        if not targets:
            raise _error("PROFILE_OVERRIDE_SLOT_NOT_FOUND", f"画像覆盖规则未找到槽位：{key}")
        policy = rule.get("policy")
        if policy not in FIELD_POLICIES:
            raise _error("INVALID_FIELD_POLICY", f"未知字段政策：{policy}", slot=key)
        for slot in targets:
            slot["policy"] = policy
            slot["policy_review_required"] = False
            if policy in EDITABLE_POLICIES:
                slot["content_role"] = "field_anchor"
            if rule.get("field_key"):
                slot["field_key"] = str(rule["field_key"])
            if "condition" in rule:
                slot["condition"] = rule["condition"]
            if rule.get("block_original_text_in_output") is True:
                slot["block_original_text_in_output"] = True
    fixed_roles = overrides.get("fixed_content_roles", {})
    for slot_id, role in fixed_roles.items():
        slot = by_id.get(slot_id)
        if slot is None:
            raise _error("PROFILE_OVERRIDE_SLOT_NOT_FOUND", f"固定内容复核未找到槽位：{slot_id}")
        if role not in {"template_boilerplate", "case_specific_blocked"}:
            raise _error("FIXED_CONTENT_ROLE_INVALID", f"固定内容角色无效：{role}", slot=slot_id)
        slot["content_role"] = role
    if overrides.get("approve_all_fixed_as_boilerplate") is True:
        for slot in profile["slots"]:
            if slot.get("policy") == "fixed_locked" and slot.get("content_role") == "unreviewed_fixed_content":
                slot["content_role"] = "template_boilerplate"
    if "empty_cell_decisions" in overrides:
        profile["empty_cell_decisions"] = copy.deepcopy(overrides["empty_cell_decisions"])
    if "unresolved_items" in overrides:
        profile["unresolved_items"] = list(overrides["unresolved_items"])


def _base_profile(
    source: Path,
    *,
    profile_kind: str,
    usage_mode: str,
    template_id: str,
    name: str,
    document_type: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    mode_validation = validate_usage_mode(usage_mode, profile_kind=profile_kind)
    if not mode_validation["ok"]:
        raise _error("INVALID_USAGE_MODE", "模板 usage_mode 无效。", errors=mode_validation["errors"])
    _require_docx(source)
    source_hash_before = sha256_file(source)
    entries = _read_zip_entries(source)
    xml_parts = _xml_parts(entries)
    if "word/document.xml" not in xml_parts:
        raise _error("DOCX_DOCUMENT_PART_MISSING", "DOCX 缺少 word/document.xml。")
    inventory = package_inventory(source)
    profile = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "profile_kind": profile_kind,
        "profile_id": f"PROFILE-{template_id}",
        "template_id": template_id,
        "name": name,
        "document_type": document_type,
        "usage_mode": usage_mode,
        "status": "draft",
        "distilled_at": now_iso(),
        "source": {
            "path": str(source),
            "sha256": source_hash_before,
            "suffix": source.suffix.casefold(),
            "read_only_verified": True,
            "package_inventory_sha256": _json_digest(inventory),
        },
        "layout": _layout_summary(xml_parts),
        "package_contract": {
            "parts": inventory,
            "editable_parts": [],
            "preserve_only_parts": sorted(inventory),
        },
        "visual_baseline": {
            "status": "pending",
            "source_sha256": source_hash_before,
            "renderer": None,
            "page_count": 0,
            "pages": [],
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "unresolved_items": [
            "field_policy_review",
            "editable_slot_review",
            "visual_baseline_review",
        ],
    }
    if sha256_file(source) != source_hash_before:
        raise _error("SOURCE_TEMPLATE_CHANGED_DURING_DISTILLATION", "蒸馏期间模板原件发生变化，结果已作废。")
    return profile, xml_parts


def _distill_form(
    source: Path,
    *,
    usage_mode: str,
    template_id: str,
    name: str,
    document_type: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    profile, xml_parts = _base_profile(
        source,
        profile_kind="form",
        usage_mode=usage_mode,
        template_id=template_id,
        name=name,
        document_type=document_type,
    )
    slots: list[dict[str, Any]] = []
    for part in sorted(xml_parts):
        for node in _text_nodes(part, xml_parts[part]):
            placeholder = node["placeholder_kind"]
            slot_number = len(slots) + 1
            slots.append(
                {
                    "slot_id": f"SLOT-{slot_number:04d}",
                    "field_key": node["field_key"],
                    "locator": {
                        "part": node["part"],
                        "path": node["path"],
                        "text_node_index": node["text_node_index"],
                        "expected_text_sha256": node["expected_text_sha256"],
                    },
                    "policy": None if placeholder else "fixed_locked",
                    "policy_suggestion": "required_verified" if placeholder else None,
                    "policy_review_required": bool(placeholder),
                    "placeholder_kind": placeholder,
                    "source_text_length": node["character_count"],
                    "condition": None,
                    "content_role": "field_anchor" if placeholder else "unreviewed_fixed_content",
                    "block_original_text_in_output": False,
                }
            )
    profile["slots"] = slots
    profile["repeatable_blocks"] = copy.deepcopy(overrides.get("repeatable_blocks", []))
    profile["empty_cell_decisions"] = []
    _apply_form_overrides(profile, overrides)
    profile["blocked_exemplar_text_sha256"] = sorted(
        {
            slot["locator"]["expected_text_sha256"]
            for slot in slots
            if slot.get("block_original_text_in_output") is True
        }
    )
    if "visual_baseline" in overrides:
        profile["visual_baseline"] = copy.deepcopy(overrides["visual_baseline"])
    editable_parts = sorted(
        {
            slot["locator"]["part"]
            for slot in slots
            if slot.get("policy") in EDITABLE_POLICIES and not slot.get("policy_review_required")
        } | {
            str(block.get("part"))
            for block in profile.get("repeatable_blocks", [])
            if block.get("part")
        }
    )
    profile["package_contract"]["editable_parts"] = editable_parts
    profile["package_contract"]["preserve_only_parts"] = sorted(
        set(profile["package_contract"]["parts"]) - set(editable_parts)
    )
    if profile["layout"]["unanchored_empty_table_cells"]:
        marker = "empty_table_cells_require_stable_text_anchors"
        if marker not in profile["unresolved_items"]:
            profile["unresolved_items"].append(marker)
    return profile


def _distill_writing(
    source: Path,
    *,
    usage_mode: str,
    template_id: str,
    name: str,
    document_type: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    profile, xml_parts = _base_profile(
        source,
        profile_kind="writing",
        usage_mode=usage_mode,
        template_id=template_id,
        name=name,
        document_type=document_type,
    )
    profile["unresolved_items"] = [
        "structure_role_review",
        "voice_argument_review",
        "annotation_and_length_review",
        "transfer_boundary_review",
        "visual_baseline_review",
    ]
    paragraphs = [
        metric
        for part in sorted(xml_parts)
        for metric in _paragraph_metrics(part, xml_parts[part])
    ]
    nonempty = [item for item in paragraphs if item["character_count"]]
    total_chars = sum(item["character_count"] for item in nonempty)
    style_counts: dict[str, int] = {}
    alignment_counts: dict[str, int] = {}
    for item in nonempty:
        style = item["style_id"] or "<direct-or-default>"
        alignment = item["alignment"] or "<inherited>"
        style_counts[style] = style_counts.get(style, 0) + 1
        alignment_counts[alignment] = alignment_counts.get(alignment, 0) + 1
    profile["metrics"] = {
        "paragraph_count": len(paragraphs),
        "nonempty_paragraph_count": len(nonempty),
        "total_character_count": total_chars,
        "average_nonempty_paragraph_length": round(total_chars / len(nonempty), 2) if nonempty else 0,
        "style_counts": style_counts,
        "alignment_counts": alignment_counts,
        "bold_run_ratio": round(
            sum(item["bold_run_count"] for item in nonempty)
            / max(1, sum(item["run_count"] for item in nonempty)),
            4,
        ),
        "citation_marker_count": sum(item["citation_mark_count"] for item in nonempty),
        "sentence_marker_count": sum(item["sentence_mark_count"] for item in nonempty),
    }
    # This outline deliberately contains hashes and measurements, never copied
    # paragraphs or case-specific prose.
    profile["structure_outline"] = paragraphs
    profile["format_metrics"] = _format_metrics(source, xml_parts)
    profile["argument_profile"] = _argument_profile(xml_parts)
    boundary_candidates: list[dict[str, Any]] = []
    for part in sorted(xml_parts):
        for node in _text_nodes(part, xml_parts[part]):
            boundary_candidates.append(
                {
                    "candidate_id": f"TEXT-{len(boundary_candidates) + 1:04d}",
                    "locator": {
                        "part": node["part"],
                        "path": node["path"],
                        "text_node_index": node["text_node_index"],
                        "expected_text_sha256": node["expected_text_sha256"],
                    },
                    "character_count": node["character_count"],
                }
            )
    profile["boundary_candidates"] = boundary_candidates
    curated_features: dict[str, list[str]] = {}
    for key in (
        "section_functions",
        "argument_pattern",
        "voice_rules",
        "annotation_rules",
        "length_rules",
    ):
        raw_items = overrides.get("curated_features", {}).get(key, [])
        if not isinstance(raw_items, list) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 240 or "\n" in item
            for item in raw_items
        ):
            raise _error(
                "WRITING_FEATURE_INVALID",
                f"{key} 必须是单行、每项不超过240字的可迁移规则数组；不得粘贴旧案正文。",
            )
        curated_features[key] = [item.strip() for item in raw_items]
    profile["curated_features"] = curated_features
    raw_preferences = overrides.get("composition_preferences", {})
    if not isinstance(raw_preferences, dict):
        raise _error("WRITING_COMPOSITION_PREFERENCES_INVALID", "composition_preferences 必须是对象。")
    profile["composition_preferences"] = {
        "layout_score": raw_preferences.get("layout_score", 1),
        "structure_score": raw_preferences.get("structure_score", 1),
        "auxiliary_score": raw_preferences.get("auxiliary_score", 0),
        "priority": raw_preferences.get("priority", 0),
        "role_suitability": list(raw_preferences.get("role_suitability", ["layout", "structure"])),
        "section_targets": list(raw_preferences.get("section_targets", [])),
        "selection_notes": str(raw_preferences.get("selection_notes", "")).strip(),
        "compatibility_tags": copy.deepcopy(raw_preferences.get("compatibility_tags", {
            "relief_or_position": [],
            "evidence_use": [],
            "authority": [],
            "external_risk": [],
        })),
    }
    profile["transfer_policy"] = {
        "reusable_aspects": list(
            overrides.get(
                "reusable_aspects",
                ["page_system", "typography", "section_order", "argument_pattern", "annotation_pattern"],
            )
        ),
        "forbidden_transfer": list(overrides.get("forbidden_transfer", FORBIDDEN_TRANSFER_DEFAULT)),
    }
    profile["body_boundaries"] = copy.deepcopy(overrides.get("body_boundaries", []))
    profile["mechanical_slots"] = copy.deepcopy(overrides.get("mechanical_slots", []))
    profile["body_regions"] = copy.deepcopy(overrides.get("body_regions", []))
    structural_parts = {
        str(item.get("locator", {}).get("part"))
        for item in profile["mechanical_slots"]
        if item.get("locator", {}).get("part")
    } | {
        str(item.get("part")) for item in profile["body_regions"] if item.get("part")
    }
    if structural_parts:
        profile["package_contract"]["editable_parts"] = sorted(structural_parts)
        profile["package_contract"]["preserve_only_parts"] = sorted(
            set(profile["package_contract"]["parts"]) - structural_parts
        )
    if "visual_baseline" in overrides:
        profile["visual_baseline"] = copy.deepcopy(overrides["visual_baseline"])
    if "unresolved_items" in overrides:
        profile["unresolved_items"] = list(overrides["unresolved_items"])
    return profile


def distill_template(
    source: Path | str,
    output_dir: Path | str,
    profile_kind: str,
    usage_mode: str,
    template_id: str,
    name: str,
    document_type: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a DOCX and write a draft sidecar profile without copying its prose.

    Distillation can never activate a profile. Even when explicit overrides
    resolve every review item, status remains ``draft`` until registration is
    performed with a separate lawyer approval record.
    """
    source_path = Path(source).resolve()
    destination_dir = Path(output_dir).resolve()
    rules = copy.deepcopy(overrides or {})
    if profile_kind == "form":
        profile = _distill_form(
            source_path,
            usage_mode=usage_mode,
            template_id=template_id,
            name=name,
            document_type=document_type,
            overrides=rules,
        )
    elif profile_kind == "writing":
        profile = _distill_writing(
            source_path,
            usage_mode=usage_mode,
            template_id=template_id,
            name=name,
            document_type=document_type,
            overrides=rules,
        )
    else:
        raise _error("INVALID_PROFILE_KIND", "profile_kind 必须为 form 或 writing。")
    if rules.get("test_only") is True:
        profile["test_only"] = True
        profile["notice"] = "TEST-ONLY synthetic template profile; not eligible for filing or production activation."
    profile["status"] = "draft"
    output = destination_dir / f"{_safe_filename(template_id)}.{profile_kind}-profile.json"
    ensure_not_originals(output)
    destination_dir.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise _error("PROFILE_OUTPUT_EXISTS", f"画像文件已经存在，拒绝覆盖：{output}")
    if sha256_file(source_path) != profile["source"]["sha256"]:
        raise _error("SOURCE_TEMPLATE_CHANGED_DURING_DISTILLATION", "蒸馏期间模板原件发生变化，结果已作废。")
    persisted_profile = copy.deepcopy(profile)
    persisted_profile["source"]["path"] = os.path.relpath(source_path, output.parent).replace("\\", "/")
    baseline = persisted_profile.get("visual_baseline")
    if isinstance(baseline, dict):
        for page in baseline.get("pages", []):
            if not isinstance(page, dict) or not page.get("path"):
                continue
            page_path = Path(str(page["path"]))
            if not page_path.is_absolute():
                page_path = (output.parent / page_path).resolve()
            page["path"] = os.path.relpath(page_path, output.parent).replace("\\", "/")
    atomic_write_json(output, persisted_profile)
    if sha256_file(source_path) != profile["source"]["sha256"]:
        output.unlink(missing_ok=True)
        raise _error("SOURCE_TEMPLATE_CHANGED_DURING_DISTILLATION", "写入画像时模板原件发生变化，画像已移除。")
    return {"ok": True, "profile_path": str(output), "profile": profile}


def _load_profile(profile_or_path: dict[str, Any] | Path | str) -> tuple[dict[str, Any], Path | None]:
    if isinstance(profile_or_path, dict):
        return copy.deepcopy(profile_or_path), None
    path = Path(profile_or_path).resolve()
    value = load_json(path)
    if not isinstance(value, dict):
        raise _error("INVALID_TEMPLATE_PROFILE", f"模板画像必须为 JSON 对象：{path}")
    return value, path


def validate_profile(
    profile_or_path: dict[str, Any] | Path | str,
    *,
    require_active: bool = False,
    profile_base: Path | str | None = None,
) -> dict[str, Any]:
    profile, path = _load_profile(profile_or_path)
    base = path.parent if path is not None else (
        Path(profile_base).resolve() if profile_base is not None else None
    )
    errors: list[dict[str, Any]] = []

    def fail(code: str, message: str, **details: Any) -> None:
        errors.append({"code": code, "message": message, **details})

    for field in ("profile_kind", "profile_id", "template_id", "usage_mode", "status", "source"):
        if not profile.get(field):
            fail("PROFILE_FIELD_MISSING", f"画像缺少 {field}", field=field)
    if profile.get("schema_version") != SCHEMA_VERSION:
        fail("PROFILE_SCHEMA_VERSION_INVALID", "画像 schema_version 不受支持。")
    if profile.get("profile_kind") == "writing":
        editable_count = len(profile.get("body_boundaries", [])) + len(profile.get("body_regions", [])) + len(profile.get("mechanical_slots", []))
    else:
        editable_count = sum(
            1 for slot in profile.get("slots", []) if slot.get("policy") in EDITABLE_POLICIES
        ) + len(profile.get("repeatable_blocks", []))
    mode = validate_usage_mode(
        str(profile.get("usage_mode", "")),
        profile_kind=profile.get("profile_kind"),
        editable_slot_count=editable_count,
        ready_for_use=require_active,
    )
    for message in mode["errors"]:
        fail("INVALID_USAGE_MODE", message)
    structural_hybrid_test = bool(
        profile.get("test_only") is True
        and profile.get("profile_kind") == "writing"
        and profile.get("usage_mode") == "hybrid"
        and profile.get("body_regions")
    )
    if require_active and profile.get("profile_kind") == "writing" and profile.get("usage_mode") == "hybrid" and not structural_hybrid_test:
        fail(
            "WRITING_HYBRID_COMPOSITION_UNSUPPORTED",
            "当前保真引擎尚不执行复杂文书正文重组；hybrid WritingProfile 只能保留为 draft，待独立合成器实现后再激活。",
        )
    if profile.get("status") not in {"draft", "active", "retired"}:
        fail("PROFILE_STATUS_INVALID", "画像 status 必须为 draft、active 或 retired。")
    if require_active and profile.get("status") != "active":
        fail("PROFILE_NOT_ACTIVE", "模板画像尚未由律师批准激活。")
    if require_active and profile.get("unresolved_items"):
        fail(
            "PROFILE_UNRESOLVED",
            "模板画像仍有未决项目，禁止填充。",
            unresolved_items=profile.get("unresolved_items"),
        )
    if require_active:
        approval = profile.get("approval", {})
        if not (
            isinstance(approval, dict)
            and approval.get("confirmed") is True
            and approval.get("approved_by")
            and approval.get("decision_id")
        ):
            fail("PROFILE_APPROVAL_REQUIRED", "active 模板画像必须绑定律师的显式批准记录。")
        baseline = profile.get("visual_baseline", {})
        pages = baseline.get("pages", []) if isinstance(baseline, dict) else []
        if not (
            isinstance(baseline, dict)
            and baseline.get("status") == "verified"
            and baseline.get("source_sha256") == profile.get("source", {}).get("sha256")
            and baseline.get("renderer")
            and isinstance(baseline.get("page_count"), int)
            and baseline.get("page_count", 0) > 0
            and baseline.get("page_count") == len(pages)
            and baseline.get("reviewed_by")
            and baseline.get("reviewed_at")
        ):
            fail("VISUAL_BASELINE_REQUIRED", "模板激活前必须完成逐页渲染并登记人工视觉复核。")
        else:
            page_numbers: set[int] = set()
            for page in pages:
                page_number = page.get("page_number")
                page_path = Path(str(page.get("path", ""))) if page.get("path") else None
                if page_path is not None and not page_path.is_absolute() and base is not None:
                    page_path = (base / page_path).resolve()
                if not isinstance(page_number, int) or page_number < 1 or page_number in page_numbers:
                    fail("VISUAL_BASELINE_PAGE_INVALID", "视觉基线页码缺失或重复。")
                    continue
                page_numbers.add(page_number)
                if page_path is None or not page_path.is_file():
                    fail("VISUAL_BASELINE_PAGE_MISSING", "视觉基线页面文件不存在。", page_number=page_number)
                    continue
                if page.get("sha256") != sha256_file(page_path):
                    fail("VISUAL_BASELINE_PAGE_HASH_MISMATCH", "视觉基线页面哈希不一致。", page_number=page_number)
                elif not page_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                    fail("VISUAL_BASELINE_PAGE_NOT_PNG", "视觉基线页面不是 PNG。", page_number=page_number)
            if page_numbers != set(range(1, baseline["page_count"] + 1)):
                fail("VISUAL_BASELINE_PAGE_SEQUENCE_INVALID", "视觉基线必须连续覆盖每一页。")
    source = profile.get("source", {})
    source_path = Path(str(source.get("path", ""))) if source.get("path") else None
    if source_path is not None and not source_path.is_absolute() and base is not None:
        source_path = (base / source_path).resolve()
    if source_path is None or not source_path.is_file():
        fail("PROFILE_SOURCE_MISSING", "画像登记的模板原件不存在。")
    elif source.get("sha256") != sha256_file(source_path):
        fail("PROFILE_SOURCE_HASH_MISMATCH", "模板原件哈希与画像不一致。")
    if profile.get("profile_kind") == "form":
        slots = profile.get("slots")
        if not isinstance(slots, list):
            fail("PROFILE_SLOTS_INVALID", "FormProfile 必须包含 slots 数组。")
        else:
            ids: set[str] = set()
            locators: set[str] = set()
            for slot in slots:
                slot_id = str(slot.get("slot_id", ""))
                if not slot_id or slot_id in ids:
                    fail("PROFILE_SLOT_ID_INVALID", "槽位 ID 缺失或重复。", slot_id=slot_id)
                ids.add(slot_id)
                locator = slot.get("locator") or {}
                locator_key = json.dumps(locator, ensure_ascii=False, sort_keys=True)
                if locator_key in locators:
                    fail("PROFILE_LOCATOR_DUPLICATE", "稳定 OOXML 定位重复。", slot_id=slot_id)
                locators.add(locator_key)
                if slot.get("policy") not in FIELD_POLICIES:
                    fail("PROFILE_POLICY_UNRESOLVED", "槽位政策未确定。", slot_id=slot_id)
                if require_active and slot.get("policy") == "repeatable":
                    block_ids = {str(item.get("block_id")) for item in profile.get("repeatable_blocks", [])}
                    if not (
                        profile.get("test_only") is True
                        and slot.get("repeatable_block_id") in block_ids
                    ):
                        fail(
                            "REPEATABLE_ROW_CLONING_UNSUPPORTED",
                            "repeatable 槽位只有在 TEST-ONLY 精确标准行合同下才可执行；真实模板仍须人工校准。",
                            slot_id=slot_id,
                        )
                if require_active and slot.get("policy_review_required"):
                    fail("PROFILE_POLICY_REVIEW_REQUIRED", "槽位政策尚未复核。", slot_id=slot_id)
                if require_active:
                    expected_role = "template_boilerplate" if slot.get("policy") == "fixed_locked" else "field_anchor"
                    if slot.get("content_role") != expected_role:
                        fail(
                            "FORM_CONTENT_TRANSFER_REVIEW_REQUIRED",
                            "固定文字或字段锚点尚未完成迁移边界复核。",
                            slot_id=slot_id,
                            expected_role=expected_role,
                        )
        if require_active:
            empty_cells = {
                (item.get("part"), item.get("path"))
                for item in profile.get("layout", {}).get("unanchored_empty_table_cells", [])
            }
            decisions = profile.get("empty_cell_decisions", [])
            decided: set[tuple[Any, Any]] = set()
            for decision in decisions:
                key = (decision.get("part"), decision.get("path"))
                if key in decided or key not in empty_cells:
                    fail("EMPTY_CELL_DECISION_INVALID", "空白表格单元格决定重复或不对应当前模板。", locator=key)
                    continue
                decided.add(key)
                if decision.get("action") not in {"not_fillable", "manual_blank"} or not decision.get("reason"):
                    fail("EMPTY_CELL_DECISION_INVALID", "空白单元格必须明确为无需填或人工留白，并记录理由。", locator=key)
            if decided != empty_cells:
                fail(
                    "UNANCHORED_EMPTY_CELLS_UNRESOLVED",
                    "仍有空白表格单元格未确认；需要填值的单元格必须先制作稳定文字锚点并重新蒸馏。",
                    missing=[{"part": part, "path": path} for part, path in sorted(empty_cells - decided)],
                )
        for digest in profile.get("blocked_exemplar_text_sha256", []):
            if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                fail("BLOCKED_EXEMPLAR_HASH_INVALID", "旧案特征文字阻断哈希格式无效。")
    elif profile.get("profile_kind") == "writing" and require_active:
        transfer = profile.get("transfer_policy", {})
        forbidden = set(transfer.get("forbidden_transfer", []))
        missing_forbidden = set(FORBIDDEN_TRANSFER_DEFAULT) - forbidden
        if missing_forbidden:
            fail(
                "WRITING_PROFILE_TRANSFER_BOUNDARY_INCOMPLETE",
                "复杂文书画像未完整阻断旧案事实迁移。",
                missing=sorted(missing_forbidden),
            )
        if not transfer.get("reusable_aspects"):
            fail("WRITING_PROFILE_REUSABLE_ASPECTS_MISSING", "复杂文书画像未登记可迁移的写作特征。")
        preferences = profile.get("composition_preferences", {})
        if not isinstance(preferences, dict):
            fail("WRITING_COMPOSITION_PREFERENCES_MISSING", "复杂文书画像未登记合成角色偏好。")
            preferences = {}
        for score_field in ("layout_score", "structure_score", "auxiliary_score", "priority"):
            score = preferences.get(score_field)
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 100:
                fail("WRITING_COMPOSITION_SCORE_INVALID", f"{score_field} 必须为0至100的数值。", field=score_field)
        suitability = preferences.get("role_suitability", [])
        if (
            not isinstance(suitability, list)
            or not suitability
            or len(suitability) != len(set(suitability))
            or not set(suitability) <= {"layout", "structure", "auxiliary"}
        ):
            fail("WRITING_ROLE_SUITABILITY_INVALID", "role_suitability 必须是不重复的 layout/structure/auxiliary 数组。")
        section_targets = preferences.get("section_targets", [])
        if not isinstance(section_targets, list) or any(not isinstance(item, str) or not item.strip() for item in section_targets):
            fail("WRITING_SECTION_TARGETS_INVALID", "section_targets 必须是非空字符串数组。")
        if "auxiliary" in suitability and not section_targets:
            fail("WRITING_AUXILIARY_SCOPE_MISSING", "可作为辅助范例的画像必须登记 section_targets。")
        if not str(preferences.get("selection_notes") or "").strip():
            fail("WRITING_SELECTION_NOTES_MISSING", "画像激活前必须记录角色选择理由。")
        compatibility = preferences.get("compatibility_tags", {})
        expected_dimensions = {"relief_or_position", "evidence_use", "authority", "external_risk"}
        if not isinstance(compatibility, dict) or set(compatibility) != expected_dimensions:
            fail("WRITING_COMPATIBILITY_TAGS_INVALID", "compatibility_tags 必须完整登记四个冲突维度。")
        else:
            for dimension, tags in compatibility.items():
                if not isinstance(tags, list) or any(not isinstance(item, str) or not item.strip() for item in tags):
                    fail("WRITING_COMPATIBILITY_TAGS_INVALID", f"{dimension} 标签必须是字符串数组。")
        curated = profile.get("curated_features", {})
        for feature in (
            "section_functions",
            "argument_pattern",
            "voice_rules",
            "annotation_rules",
            "length_rules",
        ):
            if not curated.get(feature):
                fail(
                    "WRITING_PROFILE_DEEP_REVIEW_MISSING",
                    f"复杂文书画像尚未完成人工深析：{feature}",
                    feature=feature,
                )
        candidates = {item.get("candidate_id"): item for item in profile.get("boundary_candidates", [])}
        boundaries = profile.get("body_boundaries", [])
        if profile.get("usage_mode") == "hybrid" and not boundaries and not profile.get("body_regions"):
            fail("WRITING_BODY_BOUNDARY_REQUIRED", "hybrid 文书模板必须登记正文边界。")
        boundary_ids: set[str] = set()
        for boundary in boundaries:
            boundary_id = str(boundary.get("boundary_id", ""))
            if not boundary_id or boundary_id in boundary_ids:
                fail("WRITING_BODY_BOUNDARY_INVALID", "正文边界 ID 缺失或重复。", boundary_id=boundary_id)
            boundary_ids.add(boundary_id)
            start = candidates.get(boundary.get("start_candidate_id"))
            end = candidates.get(boundary.get("end_candidate_id"))
            if start is None or end is None:
                fail("WRITING_BODY_BOUNDARY_LOCATOR_MISSING", "正文边界未对应蒸馏出的稳定文字定位。", boundary_id=boundary_id)
                continue
            if not boundary.get("purpose") or boundary.get("replacement_strategy") not in {
                "single_text_node",
                "external_docx_composer_required",
            }:
                fail("WRITING_BODY_BOUNDARY_INVALID", "正文边界缺少用途或合法替换策略。", boundary_id=boundary_id)
                continue
            start_locator = start["locator"]
            end_locator = end["locator"]
            if (
                start_locator.get("part") != "word/document.xml"
                or end_locator.get("part") != "word/document.xml"
                or int(start_locator.get("text_node_index", 0)) > int(end_locator.get("text_node_index", 0))
            ):
                fail(
                    "WRITING_BODY_BOUNDARY_ORDER_INVALID",
                    "正文边界必须位于主文档且起点不得晚于终点。",
                    boundary_id=boundary_id,
                )
            if (
                boundary.get("replacement_strategy") == "single_text_node"
                and boundary.get("start_candidate_id") != boundary.get("end_candidate_id")
            ):
                fail(
                    "WRITING_SINGLE_NODE_BOUNDARY_INVALID",
                    "single_text_node 策略的起止定位必须相同。",
                    boundary_id=boundary_id,
                )
    if source_path is not None and source_path.is_file():
        errors.extend(validate_structural_profile_contract(profile, source_path))
    return {
        "ok": not errors,
        "profile_path": str(path) if path else None,
        "template_id": profile.get("template_id"),
        "errors": errors,
    }


def _source_refs(meta: dict[str, Any]) -> list[dict[str, Any]]:
    raw = meta.get("source_refs", [])
    return copy.deepcopy(raw) if isinstance(raw, list) else []


def make_fill_binding(
    profile: dict[str, Any],
    slot: dict[str, Any],
    action: str,
    value: Any,
) -> dict[str, Any]:
    """Return the canonical, replay-resistant identity of one proposed fill.

    A source extraction or lawyer decision is useful only for the exact slot
    and exact value/action it approved.  Binding merely to a source file or a
    generic decision ID would let an unrelated approval be replayed for a fee,
    authority scope, court level, or another form field.
    """

    normalized_value = str(value) if action == "replace" and value is not None else None
    value_sha256 = _json_digest({"action": action, "value": normalized_value})
    binding = {
        "template_id": str(profile.get("template_id") or ""),
        "profile_id": str(profile.get("profile_id") or ""),
        "slot_id": str(slot.get("slot_id") or ""),
        "field_key": str(slot.get("field_key") or slot.get("slot_id") or ""),
        "action": action,
        "value_sha256": value_sha256,
    }
    binding["binding_sha256"] = _json_digest(binding)
    return binding


def _binding_matches(candidate: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict):
        return False
    fields = (
        "template_id",
        "profile_id",
        "slot_id",
        "field_key",
        "action",
        "value_sha256",
        "binding_sha256",
    )
    if any(candidate.get(field) != expected.get(field) for field in fields):
        return False
    unsigned = {field: candidate.get(field) for field in fields if field != "binding_sha256"}
    return candidate.get("binding_sha256") == _json_digest(unsigned)


def build_fill_plan(
    profile_path: Path | str,
    data: dict[str, Any],
    provenance: dict[str, Any],
    output: Path | str | None = None,
) -> dict[str, Any]:
    """Build one complete proposed-fill table from data plus provenance."""
    profile, resolved_profile_path = _load_profile(profile_path)
    if profile.get("profile_kind") != "form":
        raise _error("FORM_PROFILE_REQUIRED", "只有 FormProfile 可以生成 FillPlan。")
    slots: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = copy.deepcopy(provenance.get("_conflicts", []))
    for slot in profile.get("slots", []):
        slot_id = slot["slot_id"]
        key = slot.get("field_key") or slot_id
        policy = slot.get("policy")
        supplied = key in data
        value = data.get(key)
        slot_meta = provenance.get(slot_id)
        field_meta = provenance.get(key)
        if isinstance(slot_meta, dict) and isinstance(field_meta, dict) and slot_id != key and slot_meta != field_meta:
            conflicts.append(
                {
                    "id": f"PROVENANCE-{slot_id}",
                    "slot_ids": [slot_id],
                    "status": "unresolved",
                    "reason": "slot_and_field_provenance_conflict",
                }
            )
        meta = slot_meta if isinstance(slot_meta, dict) else (
            field_meta if isinstance(field_meta, dict) else {}
        )
        if policy == "fixed_locked":
            action = "preserve"
            if supplied:
                conflicts.append(
                    {
                        "id": f"LOCKED-{slot_id}",
                        "slot_ids": [slot_id],
                        "status": "unresolved",
                        "reason": "data_supplied_for_fixed_locked_slot",
                    }
                )
            value = None
        elif policy == "manual_blank":
            # A manual-only field may already contain an old signature, date,
            # stamp name, or sample value. It must be cleared even when the
            # source used ordinary text rather than a visible placeholder.
            action = "clear_to_blank" if slot.get("source_text_length", 0) > 0 else "preserve"
            if supplied and value not in (None, ""):
                conflicts.append(
                    {
                        "id": f"MANUAL-{slot_id}",
                        "slot_ids": [slot_id],
                        "status": "unresolved",
                        "reason": "manual_blank_must_not_receive_value",
                    }
                )
            value = None
        elif policy == "repeatable":
            action = "preserve"
            value = None
        elif supplied and value not in (None, ""):
            action = "replace"
            value = str(value)
        else:
            # Every non-locked dynamic slot is blank-by-construction when it
            # has no approved value.  Preserving ordinary legacy text here can
            # leak an old client name, amount, authority, or fact even when it
            # was not written as a visible placeholder.
            action = "clear_to_blank"
            value = None
        blank_reason = meta.get("blank_reason")
        if value is None and not blank_reason:
            blank_reason = {
                "manual_blank": "profile_requires_manual_signature_stamp_or_on_site_entry",
                "optional_verified": "no_verified_value_available",
                "derived_needs_confirmation": "no_confirmed_derived_value",
                "required_verified": "required_value_missing",
            }.get(str(policy))
        binding = make_fill_binding(profile, slot, action, value)
        slots.append(
            {
                "slot_id": slot_id,
                "field_key": key,
                "policy": policy,
                "locator": copy.deepcopy(slot.get("locator")),
                "action": action,
                "value": value,
                "applies": meta.get("applies"),
                "blank_reason": blank_reason,
                "source_refs": _source_refs(meta),
                "derivation_basis": meta.get("derivation_basis"),
                "confirmation": copy.deepcopy(meta.get("confirmation")),
                "binding": binding,
            }
        )
    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": f"FILL-{profile['template_id']}-{_json_digest(data)[:12]}",
        "profile_id": profile["profile_id"],
        "profile_path": str(resolved_profile_path) if resolved_profile_path else str(profile_path),
        "template_id": profile["template_id"],
        "template_sha256": profile["source"]["sha256"],
        "usage_mode": profile["usage_mode"],
        "created_at": now_iso(),
        "status": "draft",
        "slots": slots,
        "repeat_groups": [],
        "conflicts": conflicts,
        "provenance_snapshot": copy.deepcopy(provenance.get("_registry_snapshot")),
    }
    validation = validate_fill_plan(plan, resolved_profile_path or profile)
    plan["status"] = "ready" if validation["ok"] else "blocked"
    if output is not None:
        output_path = Path(output).resolve()
        ensure_not_originals(output_path)
        if output_path.exists():
            raise _error("FILL_PLAN_OUTPUT_EXISTS", f"FillPlan 已存在，拒绝覆盖：{output_path}")
        atomic_write_json(output_path, plan)
    return plan


def _valid_provenance_snapshot(snapshot: Any) -> bool:
    if not (
        isinstance(snapshot, dict)
        and snapshot.get("snapshot_sha256")
        and snapshot.get("case_state_path")
        and snapshot.get("case_state_sha256")
    ):
        return False
    payload = {key: copy.deepcopy(value) for key, value in snapshot.items() if key != "snapshot_sha256"}
    if snapshot.get("snapshot_sha256") != _json_digest(payload):
        return False
    template_catalog = snapshot.get("template_catalog")
    if template_catalog is not None:
        if not isinstance(template_catalog, dict):
            return False
        catalog_path = Path(str(template_catalog.get("catalog_path") or ""))
        unsigned_binding = {
            key: copy.deepcopy(value)
            for key, value in template_catalog.items()
            if key != "binding_sha256"
        }
        if (
            not catalog_path.is_file()
            or template_catalog.get("catalog_sha256") != sha256_file(catalog_path)
            or template_catalog.get("binding_sha256") != _json_digest(unsigned_binding)
        ):
            return False
    state_path = Path(str(snapshot["case_state_path"]))
    if not state_path.is_file() or sha256_file(state_path) != snapshot["case_state_sha256"]:
        return False
    try:
        state = load_json(state_path)
    except LegalCaseError:
        return False
    state_version = state.get("focus", {}).get("state_version", state.get("state_version"))
    if state_version != snapshot.get("state_version"):
        return False

    def indexed_objects() -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        stack: list[Any] = [state]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                identifier = current.get("id") or current.get("decision_id")
                if isinstance(identifier, str) and identifier:
                    found[identifier] = current
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return found

    objects = indexed_objects()
    for record in [
        *snapshot.get("sources", []),
        *snapshot.get("facts", []),
        *snapshot.get("decisions", []),
    ]:
        identifier = record.get("id") or record.get("decision_id")
        actual = objects.get(identifier)
        if actual is None or record.get("object_sha256") != _json_digest(actual):
            return False
    return True


def _has_direct_verified_source(
    source_refs: list[dict[str, Any]],
    snapshot: dict[str, Any],
    binding: dict[str, Any],
) -> bool:
    registry = {item.get("id"): item for item in snapshot.get("sources", []) if item.get("id")}
    facts = {item.get("id"): item for item in snapshot.get("facts", []) if item.get("id")}
    for item in source_refs:
        record = registry.get(item.get("id"))
        fact = facts.get(item.get("fact_id"))
        if not record or not fact:
            continue
        if not (
            item.get("verified") is True
            and record.get("verified") is True
            and item.get("kind") in DIRECT_SOURCE_KINDS
            and record.get("kind") == item.get("kind")
        ):
            continue
        if item.get("sha256") and record.get("sha256") != item.get("sha256"):
            continue
        if not item.get("object_sha256") or record.get("object_sha256") != item.get("object_sha256"):
            continue
        if not item.get("fact_object_sha256") or fact.get("object_sha256") != item.get("fact_object_sha256"):
            continue
        if fact.get("status") != "confirmed" or fact.get("stale") is True:
            continue
        if not _binding_matches(item.get("binding"), binding):
            continue
        if item.get("extracted_value_sha256") != binding.get("value_sha256"):
            continue
        if fact.get("normalized_value_sha256") != binding.get("value_sha256"):
            continue
        if fact.get("field_key") != binding.get("field_key"):
            continue
        locator = item.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            continue
        matching_locator = next(
            (
                candidate
                for candidate in fact.get("source_locators", [])
                if candidate.get("source_id") == item.get("id")
                and candidate.get("locator") == locator
                and candidate.get("record_kind") == "direct_record"
                and candidate.get("verified") is True
            ),
            None,
        )
        if matching_locator is None:
            continue
        return True
    return False


def _approved_lawyer_confirmation(
    value: Any,
    snapshot: dict[str, Any],
    binding: dict[str, Any],
) -> bool:
    if not (
        isinstance(value, dict)
        and value.get("status") == "approved"
        and value.get("actor_role") == "lawyer"
        and value.get("decision_id")
    ):
        return False
    registry = {
        item.get("decision_id"): item
        for item in snapshot.get("decisions", [])
        if item.get("decision_id")
    }
    record = registry.get(value["decision_id"])
    return bool(
        record
        and record.get("status") == "approved"
        and record.get("actor_role") == "lawyer"
        and value.get("object_sha256")
        and record.get("object_sha256") == value.get("object_sha256")
        and _binding_matches(value.get("fill_binding"), binding)
        and _binding_matches(record.get("fill_binding"), binding)
    )


def validate_fill_plan(
    plan_or_path: dict[str, Any] | Path | str,
    profile_or_path: dict[str, Any] | Path | str,
) -> dict[str, Any]:
    plan = copy.deepcopy(plan_or_path) if isinstance(plan_or_path, dict) else load_json(Path(plan_or_path))
    profile, resolved_profile_path = _load_profile(profile_or_path)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    confirmation_request: list[dict[str, Any]] = []

    def fail(code: str, message: str, slot_id: str | None = None, **details: Any) -> None:
        item = {"code": code, "message": message, **details}
        if slot_id:
            item["slot_id"] = slot_id
        errors.append(item)

    profile_check = validate_profile(
        profile,
        require_active=True,
        profile_base=resolved_profile_path.parent if resolved_profile_path is not None else None,
    )
    errors.extend(profile_check["errors"])
    snapshot = plan.get("provenance_snapshot")
    if not _valid_provenance_snapshot(snapshot):
        fail(
            "PROVENANCE_SNAPSHOT_INVALID",
            "FillPlan 必须绑定来源/律师决定注册表的完整状态快照哈希。",
        )
        snapshot = {"sources": [], "decisions": []}
    if plan.get("template_id") != profile.get("template_id"):
        fail("FILL_PLAN_TEMPLATE_MISMATCH", "FillPlan 与模板画像 ID 不一致。")
    if plan.get("template_sha256") != profile.get("source", {}).get("sha256"):
        fail("FILL_PLAN_HASH_MISMATCH", "FillPlan 的模板哈希已过期。")
    if plan.get("usage_mode") != profile.get("usage_mode"):
        fail("FILL_PLAN_USAGE_MODE_MISMATCH", "FillPlan 的 usage_mode 与画像不一致。")
    if profile.get("usage_mode") == "reference":
        fail("REFERENCE_TEMPLATE_NOT_FILLABLE", "reference 模式仅供学习，不得执行保真填充。")
    unresolved_conflicts = [item for item in plan.get("conflicts", []) if item.get("status") != "resolved"]
    if unresolved_conflicts:
        fail(
            "FILL_PLAN_CONFLICTS_UNRESOLVED",
            "FillPlan 存在未解决冲突，必须集中确认一次后再运行。",
            conflict_ids=[item.get("id") for item in unresolved_conflicts],
        )
    profile_slots = {item["slot_id"]: item for item in profile.get("slots", [])}
    plan_slots = plan.get("slots", [])
    if not isinstance(plan_slots, list):
        fail("FILL_PLAN_SLOTS_INVALID", "FillPlan slots 必须为数组。")
        plan_slots = []
    seen: set[str] = set()
    for item in plan_slots:
        slot_id = str(item.get("slot_id", ""))
        if not slot_id or slot_id in seen:
            fail("FILL_PLAN_SLOT_ID_INVALID", "FillPlan 槽位 ID 缺失或重复。", slot_id or None)
            continue
        seen.add(slot_id)
        slot = profile_slots.get(slot_id)
        if slot is None:
            fail("FILL_PLAN_SLOT_UNKNOWN", "FillPlan 包含画像中不存在的槽位。", slot_id)
            continue
        if item.get("policy") != slot.get("policy"):
            fail("FILL_PLAN_POLICY_MISMATCH", "FillPlan 不得覆盖画像中的字段政策。", slot_id)
        if item.get("locator") != slot.get("locator"):
            fail("FILL_PLAN_LOCATOR_MISMATCH", "FillPlan 的稳定 OOXML 定位与画像不一致。", slot_id)
        policy = slot.get("policy")
        action = item.get("action")
        value = item.get("value")
        binding = make_fill_binding(profile, slot, str(action or ""), value)
        source_refs = item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
        has_value = action == "replace" and value not in (None, "")
        if not _binding_matches(item.get("binding"), binding):
            fail(
                "FILL_SLOT_BINDING_MISMATCH",
                "FillPlan 的格子、动作或拟填值与来源/决定绑定不一致。",
                slot_id,
            )
        if action not in {"preserve", "replace", "clear_to_blank"}:
            fail("FILL_PLAN_ACTION_INVALID", "槽位动作必须为 preserve、replace 或 clear_to_blank。", slot_id)
        if policy != "fixed_locked" and not has_value and not item.get("blank_reason"):
            fail(
                "BLANK_REASON_REQUIRED",
                "所有动态格子的有意留空都必须记录空白原因。",
                slot_id,
            )
        if policy == "fixed_locked":
            if action != "preserve" or value not in (None, ""):
                fail("FIXED_LOCKED_EDIT_BLOCKED", "固定文字或版式槽位禁止修改。", slot_id)
        elif policy == "manual_blank":
            if action not in {"preserve", "clear_to_blank"} or value not in (None, ""):
                fail("MANUAL_BLANK_EDIT_BLOCKED", "签字、盖章或现场日期槽位必须留空。", slot_id)
        elif policy == "required_verified":
            if not has_value:
                fail("REQUIRED_VALUE_MISSING", "必填字段缺少拟填值。", slot_id)
            if not _has_direct_verified_source(source_refs, snapshot, binding):
                fail("DIRECT_SOURCE_REQUIRED", "必填字段必须绑定已核验原始材料或权威来源。", slot_id)
        elif policy == "conditional_verified":
            if item.get("applies") is True:
                if not has_value or not _has_direct_verified_source(source_refs, snapshot, binding):
                    fail("CONDITIONAL_VALUE_UNVERIFIED", "条件成立字段必须有值和直接来源。", slot_id)
            elif item.get("applies") is False:
                if has_value:
                    fail("CONDITIONAL_VALUE_MUST_BE_BLANK", "条件不成立字段必须留空。", slot_id)
                if action != "clear_to_blank":
                    fail("CONDITIONAL_BLANK_NOT_CLEARED", "条件不成立字段必须显式清空，禁止保留模板旧值。", slot_id)
                if not item.get("blank_reason"):
                    fail("CONDITIONAL_BLANK_REASON_REQUIRED", "条件不成立须记录留空理由。", slot_id)
            else:
                fail("CONDITION_DECISION_REQUIRED", "条件字段尚未判断是否适用。", slot_id)
        elif policy == "optional_verified":
            if has_value and not _has_direct_verified_source(source_refs, snapshot, binding):
                fail("OPTIONAL_VALUE_UNVERIFIED", "可选字段有值时仍必须绑定直接来源。", slot_id)
            if not has_value and action != "clear_to_blank":
                fail("OPTIONAL_BLANK_NOT_CLEARED", "可选字段无值时必须显式清空，禁止保留模板旧值。", slot_id)
        elif policy == "derived_needs_confirmation":
            if has_value:
                if not item.get("derivation_basis") or not _approved_lawyer_confirmation(
                    item.get("confirmation"), snapshot, binding
                ):
                    fail("DERIVED_VALUE_CONFIRMATION_REQUIRED", "推导字段必须记录推导依据并由律师确认。", slot_id)
                    confirmation_request.append({"slot_id": slot_id, "reason": "derived_value"})
            elif action != "clear_to_blank":
                fail("DERIVED_BLANK_NOT_CLEARED", "推导字段无获批值时必须显式清空旧值。", slot_id)
        elif policy == "lawyer_decision_required":
            if not _approved_lawyer_confirmation(item.get("confirmation"), snapshot, binding):
                fail("LAWYER_DECISION_REQUIRED", "收费、审级或代理权限字段必须由律师明确决定。", slot_id)
                confirmation_request.append({"slot_id": slot_id, "reason": "lawyer_decision"})
            if not has_value and action != "clear_to_blank":
                fail("LAWYER_DECISION_VALUE_OR_BLANK_REQUIRED", "律师必须明确决定填值或留空。", slot_id)
        elif policy == "repeatable":
            block_id = slot.get("repeatable_block_id")
            groups = {group.get("block_id") for group in plan.get("repeat_groups", [])}
            if action != "preserve" or value not in (None, ""):
                fail("REPEATABLE_MARKER_EDIT_BLOCKED", "repeatable 标记节点自身不得直接改写。", slot_id)
            if block_id not in groups:
                fail("REPEATABLE_GROUP_REQUIRED", "repeatable 标记必须对应一个完整的 repeat_group。", slot_id)
        else:
            fail("FIELD_POLICY_UNRESOLVED", "槽位字段政策未确定。", slot_id)
    missing_slots = sorted(set(profile_slots) - seen)
    if missing_slots:
        fail("FILL_PLAN_INCOMPLETE", "FillPlan 必须覆盖画像中的全部槽位。", missing_slots=missing_slots)
    # One de-duplicated batch is returned to the orchestrator; individual
    # high-risk fields are never asked in separate conversational loops.
    batch = sorted({(item["slot_id"], item["reason"]) for item in confirmation_request})
    conflict_batch = [
        {
            "conflict_id": item.get("id"),
            "slot_ids": sorted(str(slot_id) for slot_id in item.get("slot_ids", []) if slot_id),
            "reason": item.get("reason"),
        }
        for item in sorted(unresolved_conflicts, key=lambda value: str(value.get("id") or ""))
    ]
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "confirmation_batch": [
            {"slot_id": slot_id, "reason": reason} for slot_id, reason in batch
        ],
        "conflict_batch": conflict_batch,
        "consolidated_review_batch": [
            *[{"kind": "source_conflict", **item} for item in conflict_batch],
            *[
                {"kind": "field_confirmation", "slot_id": slot_id, "reason": reason}
                for slot_id, reason in batch
            ],
        ],
    }


def _validate_replacement_text(value: str, locator: dict[str, Any]) -> None:
    if _INVALID_XML_TEXT_RE.search(value) or "\r" in value or "\n" in value or "\t" in value:
        raise _error(
            "OOXML_TEXT_VALUE_INVALID",
            "单一文字节点不接受控制字符、制表符或换行；应改用已登记的多段落槽位。",
            locator=locator,
        )
    if value != value.strip():
        raise _error(
            "OOXML_SPACE_PRESERVATION_REQUIRED",
            "拟填值含首尾空格，可能改变 xml:space；请先在模板中登记保留空格的稳定锚点。",
            locator=locator,
        )


def _patch_text_part(data: bytes, replacements: dict[int, tuple[dict[str, Any], str]]) -> bytes:
    nodes = _text_nodes("<in-memory>", data)
    raw_matches = list(_RAW_WT_RE.finditer(data))
    if len(nodes) != len(raw_matches):
        raise _error(
            "OOXML_TEXT_NODE_MAPPING_UNSTABLE",
            "XML 文字节点与原始字节定位数量不一致，禁止修改。",
            parsed=len(nodes),
            raw=len(raw_matches),
        )
    for index, (locator, value) in replacements.items():
        if index < 1 or index > len(nodes):
            raise _error("OOXML_LOCATOR_OUT_OF_RANGE", "稳定 OOXML 定位超出文字节点范围。", locator=locator)
        actual = nodes[index - 1]
        if actual["path"] != locator.get("path"):
            raise _error("OOXML_LOCATOR_PATH_MISMATCH", "OOXML 路径发生变化，禁止套用旧画像。", locator=locator)
        if actual["expected_text_sha256"] != locator.get("expected_text_sha256"):
            raise _error("OOXML_LOCATOR_TEXT_MISMATCH", "槽位原文字哈希发生变化，禁止静默替换。", locator=locator)
        _validate_replacement_text(value, locator)
    chunks: list[bytes] = []
    cursor = 0
    for index, match in enumerate(raw_matches, start=1):
        chunks.append(data[cursor : match.start()])
        replacement = replacements.get(index)
        if replacement is None:
            chunks.append(match.group(0))
        else:
            _locator, value = replacement
            prefix = match.group("prefix")
            attrs = match.group("attrs") or b""
            encoded = xml_escape(value).encode("utf-8")
            chunks.append(b"<" + prefix + b":t" + attrs + b">" + encoded + b"</" + prefix + b":t>")
        cursor = match.end()
    chunks.append(data[cursor:])
    patched = b"".join(chunks)
    try:
        ET.fromstring(patched)
    except ET.ParseError as exc:
        raise _error("OOXML_PATCH_INVALID", "文字替换后 XML 无法解析，未生成输出。", detail=str(exc)) from exc
    return patched


def _write_docx_entries(entries: list[tuple[zipfile.ZipInfo, bytes]], output: Path) -> None:
    with zipfile.ZipFile(output, "w") as package:
        for info, data in entries:
            package.writestr(info, data)


def _exclusive_copy(source: Path, destination: Path) -> None:
    ensure_not_originals(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def fill_docx_from_plan(
    source: Path | str,
    destination: Path | str,
    profile: dict[str, Any] | Path | str,
    plan: dict[str, Any] | Path | str,
) -> dict[str, Any]:
    """Clone a DOCX package, replace only approved w:t nodes, and diff it."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    ensure_not_originals(destination_path)
    if source_path == destination_path:
        raise _error("SOURCE_OUTPUT_MUST_DIFFER", "输出路径不得与模板原件相同。")
    if destination_path.exists():
        raise _error("OUTPUT_EXISTS", f"输出已经存在，拒绝覆盖：{destination_path}")
    _require_docx(source_path)
    profile_value, resolved_profile_path = _load_profile(profile)
    plan_value = copy.deepcopy(plan) if isinstance(plan, dict) else load_json(Path(plan))
    validation = validate_fill_plan(plan_value, resolved_profile_path or profile_value)
    if not validation["ok"]:
        raise _error("FILL_PLAN_BLOCKED", "FillPlan 未通过门禁，未生成 DOCX。", validation=validation)
    source_hash_before = sha256_file(source_path)
    expected_hash = profile_value["source"]["sha256"]
    if source_hash_before != expected_hash or plan_value.get("template_sha256") != expected_hash:
        raise _error("SOURCE_TEMPLATE_HASH_MISMATCH", "模板原件与画像/FillPlan 哈希不一致。")
    replacements_by_part: dict[str, dict[int, tuple[dict[str, Any], str]]] = {}
    for item in plan_value["slots"]:
        if item["action"] == "replace":
            replacement = str(item["value"])
        elif item["action"] == "clear_to_blank":
            replacement = ""
        else:
            continue
        locator = item["locator"]
        part = locator["part"]
        index = int(locator["text_node_index"])
        if index in replacements_by_part.setdefault(part, {}):
            raise _error("OOXML_LOCATOR_DUPLICATE", "同一 OOXML 文字节点被重复计划修改。", locator=locator)
        replacements_by_part[part][index] = (locator, replacement)
    entries = _read_zip_entries(source_path)
    source_parts = {info.filename for info, _data in entries}
    missing_parts = sorted(set(replacements_by_part) - source_parts)
    if missing_parts:
        raise _error("OOXML_PART_MISSING", "画像引用的 DOCX 部件不存在。", parts=missing_parts)
    patched_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in entries:
        replacements = replacements_by_part.get(info.filename)
        patched_entries.append(
            (info, _patch_text_part(data, replacements)) if replacements else (info, data)
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=".lcos-fill-", suffix=".docx", dir=destination_path.parent, delete=False
    )
    temporary_path = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        _write_docx_entries(patched_entries, temporary_path)
        diff = compare_docx_packages(
            source_path,
            temporary_path,
            allowed_changed_parts=replacements_by_part,
        )
        if not diff["ok"]:
            raise _error("DOCX_FIDELITY_DIFF_FAILED", "DOCX 包结构或保留部件发生越权变化。", diff=diff)
        temporary_xml_parts = _xml_parts(_read_zip_entries(temporary_path))
        remaining_text_hashes = {
            node["expected_text_sha256"]
            for part, data in temporary_xml_parts.items()
            for node in _text_nodes(part, data)
            if node["character_count"] > 0
        }
        leaked_hashes = sorted(
            set(profile_value.get("blocked_exemplar_text_sha256", [])) & remaining_text_hashes
        )
        if leaked_hashes:
            raise _error(
                "EXEMPLAR_CONTENT_LEAK_BLOCKED",
                "输出仍含画像登记的旧案特征文字，未生成候选件。",
                leaked_hashes=leaked_hashes,
            )
        from .documents import preflight

        preflight_result = preflight(temporary_path)
        if not preflight_result["ok"]:
            raise _error(
                "DOCX_PREFLIGHT_BLOCKED",
                "填充结果仍含批注、修订、隐藏文字、内部标识或未决占位符。",
                preflight=preflight_result,
            )
        if sha256_file(source_path) != source_hash_before:
            raise _error("SOURCE_TEMPLATE_CHANGED_DURING_FILL", "填充期间模板原件发生变化，输出已作废。")
        _exclusive_copy(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "source": str(source_path),
        "source_sha256": source_hash_before,
        "destination": str(destination_path),
        "destination_sha256": sha256_file(destination_path),
        "changed_parts": diff["changed_parts"],
        "package_diff": diff,
        "preflight": preflight_result,
        "release_status": "structurally_valid_pending_visual_qa",
        "visual_qa_required": True,
    }


def _document_root_from_entries(entries: list[tuple[zipfile.ZipInfo, bytes]]) -> tuple[ET.Element, bytes]:
    for info, data in entries:
        if info.filename == "word/document.xml":
            try:
                return ET.fromstring(data), data
            except ET.ParseError as exc:
                raise _error("DOCX_XML_INVALID", "word/document.xml 无法解析。", detail=str(exc)) from exc
    raise _error("DOCX_DOCUMENT_PART_MISSING", "DOCX 缺少 word/document.xml。")


def _path_index(root: ET.Element) -> dict[str, ET.Element]:
    paths = _element_paths(root)
    return {path: element for element in root.iter() for path in [paths[id(element)]]}


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _dangerous_clone_content(element: ET.Element, *, allow_root_table: bool = False) -> list[str]:
    findings: set[str] = set()
    for candidate in element.iter():
        local = _local_name(candidate)
        if candidate is element and allow_root_table and local == "tbl":
            pass
        elif local in _DANGEROUS_CLONE_LOCALS:
            findings.add(local)
        for attribute in candidate.attrib:
            namespace = attribute[1:].split("}", 1)[0] if attribute.startswith("{") else ""
            if namespace == R_NS:
                findings.add("relationship_attribute")
    return sorted(findings)


def _plain_structural_text(value: Any, max_chars: int, *, context: str) -> str:
    if not isinstance(value, str):
        raise _error("STRUCTURAL_TEXT_REQUIRED", f"{context} 必须为纯文本字符串。")
    if len(value) > max_chars:
        raise _error(
            "STRUCTURAL_TEXT_TOO_LONG",
            f"{context} 超过已登记长度上限。",
            length=len(value),
            max_chars=max_chars,
        )
    if _INVALID_XML_TEXT_RE.search(value) or any(character in value for character in ("\r", "\n", "\t")):
        raise _error("STRUCTURAL_TEXT_CONTROL_CHARACTER", f"{context} 含控制字符、制表符或换行。")
    if re.search(r"(?i)<\s*/?\s*(?:w|r|v|a|wp|mc):|<\s*(?:script|xml|object|iframe)\b", value):
        raise _error("DANGEROUS_OOXML_TEXT_BLOCKED", f"{context} 疑似包含 OOXML/XML 指令。")
    if re.search(r"\{\{[^{}]+\}\}|\[\[[^\[\]]+\]\]|TODO|待核验|内部备注|审稿词|AI认为", value, re.I):
        raise _error("OUTWARD_TEXT_MARKER_BLOCKED", f"{context} 含占位符或内部审稿词。")
    return value


def validate_structural_profile_contract(
    profile: dict[str, Any], source: Path | str
) -> list[dict[str, Any]]:
    """Validate TEST-ONLY row/body transformation contracts against exact OOXML."""

    repeatable_blocks = profile.get("repeatable_blocks", [])
    body_regions = profile.get("body_regions", [])
    mechanical_slots = profile.get("mechanical_slots", [])
    if not repeatable_blocks and not body_regions and not mechanical_slots:
        return []
    errors: list[dict[str, Any]] = []

    def fail(code: str, message: str, **details: Any) -> None:
        errors.append({"code": code, "message": message, **details})

    if profile.get("test_only") is not True or "TEST-ONLY" not in str(profile.get("notice") or ""):
        fail(
            "STRUCTURAL_TEMPLATE_TEST_ONLY_REQUIRED",
            "结构增行/正文替换当前只允许明确标注 TEST-ONLY 的合成模板；真实模板继续阻断。",
        )
        return errors
    try:
        entries = _read_zip_entries(Path(source).resolve())
        root, _raw = _document_root_from_entries(entries)
    except LegalCaseError as exc:
        fail(exc.code, exc.message)
        return errors
    by_path = _path_index(root)
    block_ids: set[str] = set()
    occupied_rows: set[str] = set()
    for block in repeatable_blocks:
        block_id = str(block.get("block_id") or "")
        if not block_id or block_id in block_ids:
            fail("REPEATABLE_BLOCK_ID_INVALID", "repeatable block ID 缺失或重复。", block_id=block_id)
            continue
        block_ids.add(block_id)
        if block.get("part") != "word/document.xml":
            fail("REPEATABLE_PART_UNSUPPORTED", "标准行只允许位于 word/document.xml。", block_id=block_id)
            continue
        container = by_path.get(str(block.get("container_path") or ""))
        prototype = by_path.get(str(block.get("prototype_row_path") or ""))
        if container is None or prototype is None or container.tag != W_TBL or prototype.tag != W_TR:
            fail("REPEATABLE_ROW_LOCATOR_INVALID", "标准行必须精确定位到表格的直接 w:tr。", block_id=block_id)
            continue
        if prototype not in list(container):
            fail("REPEATABLE_ROW_NOT_DIRECT_CHILD", "标准行不是已登记表格的直接子行。", block_id=block_id)
        if str(block.get("prototype_row_path")) in occupied_rows:
            fail("REPEATABLE_ROW_OVERLAP", "同一原型行不得属于多个标准行合同。", block_id=block_id)
        occupied_rows.add(str(block.get("prototype_row_path")))
        if _subtree_digest(prototype) != block.get("prototype_row_sha256"):
            fail("REPEATABLE_ROW_HASH_MISMATCH", "标准行子树哈希已变化。", block_id=block_id)
        dangerous = _dangerous_clone_content(prototype)
        if dangerous:
            fail("REPEATABLE_ROW_DANGEROUS_OOXML", "标准行含不可安全克隆对象。", block_id=block_id, findings=dangerous)
        nodes = list(prototype.iter(W_T))
        if len(nodes) != block.get("prototype_text_node_count"):
            fail("REPEATABLE_TEXT_NODE_COUNT_MISMATCH", "标准行文字节点数量已变化。", block_id=block_id)
            continue
        classified: dict[int, str] = {}
        for fixed in block.get("fixed_text_nodes", []):
            index = fixed.get("text_node_index")
            if not isinstance(index, int) or not 1 <= index <= len(nodes) or index in classified:
                fail("REPEATABLE_TEXT_CLASSIFICATION_INVALID", "标准行固定节点索引无效或重复。", block_id=block_id)
                continue
            classified[index] = "fixed"
            actual = hashlib.sha256((nodes[index - 1].text or "").encode("utf-8")).hexdigest()
            if actual != fixed.get("expected_text_sha256"):
                fail("REPEATABLE_FIXED_TEXT_HASH_MISMATCH", "标准行固定文字哈希已变化。", block_id=block_id, text_node_index=index)
        field_keys: set[str] = set()
        for slot in block.get("item_slots", []):
            index = slot.get("text_node_index")
            field_key = str(slot.get("field_key") or "")
            if not isinstance(index, int) or not 1 <= index <= len(nodes) or index in classified:
                fail("REPEATABLE_TEXT_CLASSIFICATION_INVALID", "标准行字段节点索引无效或重复。", block_id=block_id)
                continue
            if not field_key or field_key in field_keys:
                fail("REPEATABLE_FIELD_KEY_INVALID", "标准行字段键缺失或重复。", block_id=block_id, field_key=field_key)
            field_keys.add(field_key)
            classified[index] = field_key
            actual = hashlib.sha256((nodes[index - 1].text or "").encode("utf-8")).hexdigest()
            if actual != slot.get("expected_text_sha256"):
                fail("REPEATABLE_ITEM_TEXT_HASH_MISMATCH", "标准行字段原文哈希已变化。", block_id=block_id, field_key=field_key)
        if set(classified) != set(range(1, len(nodes) + 1)):
            fail(
                "REPEATABLE_TEXT_NODES_UNCLASSIFIED",
                "标准行的每个文字节点都必须明确为固定或逐项字段。",
                block_id=block_id,
                missing=sorted(set(range(1, len(nodes) + 1)) - set(classified)),
            )
        if not isinstance(block.get("min_items"), int) or not isinstance(block.get("max_items"), int) or block["min_items"] > block["max_items"]:
            fail("REPEATABLE_CARDINALITY_INVALID", "标准行最小/最大数量无效。", block_id=block_id)

    region_ids: set[str] = set()
    occupied_blocks: set[str] = set()
    region_prefixes: list[str] = []
    for region in body_regions:
        region_id = str(region.get("region_id") or "")
        if not region_id or region_id in region_ids:
            fail("HYBRID_REGION_ID_INVALID", "正文区 ID 缺失或重复。", region_id=region_id)
            continue
        region_ids.add(region_id)
        if region.get("part") != "word/document.xml":
            fail("HYBRID_REGION_PART_UNSUPPORTED", "正文区只允许位于 word/document.xml。", region_id=region_id)
            continue
        container = by_path.get(str(region.get("container_path") or ""))
        start = by_path.get(str(region.get("start_block_path") or ""))
        end = by_path.get(str(region.get("end_block_path") or ""))
        if container is None or start is None or end is None or start.tag != W_P or end.tag != W_P:
            fail("HYBRID_REGION_LOCATOR_INVALID", "正文区必须由同一容器内完整 w:p 边界定义。", region_id=region_id)
            continue
        children = list(container)
        if start not in children or end not in children or children.index(start) > children.index(end):
            fail("HYBRID_REGION_ORDER_INVALID", "正文区起止段落不是同一容器内的有序直接子节点。", region_id=region_id)
            continue
        start_index, end_index = children.index(start), children.index(end)
        selected = children[start_index : end_index + 1]
        if any(item.tag != W_P for item in selected):
            fail("HYBRID_REGION_NON_PARAGRAPH_BLOCKED", "正文区不得跨表格、分节或其他块对象。", region_id=region_id)
        for path in {str(_element_paths(root)[id(item)]) for item in selected}:
            if path in occupied_blocks:
                fail("HYBRID_REGION_OVERLAP", "正文区不得重叠。", region_id=region_id)
            occupied_blocks.add(path)
        region_prefixes.extend(str(_element_paths(root)[id(item)]) for item in selected)
        if _subtree_digest(selected) != region.get("source_block_sha256"):
            fail("HYBRID_REGION_HASH_MISMATCH", "正文区源子树哈希已变化。", region_id=region_id)
        previous = children[start_index - 1] if start_index > 0 else None
        following = children[end_index + 1] if end_index + 1 < len(children) else None
        for label, actual_element in (("preceding_anchor", previous), ("following_anchor", following)):
            anchor = region.get(label)
            actual_path = _element_paths(root).get(id(actual_element)) if actual_element is not None else None
            if actual_element is None or not isinstance(anchor, dict) or anchor.get("path") != actual_path or anchor.get("subtree_sha256") != _subtree_digest(actual_element):
                fail("HYBRID_REGION_ANCHOR_MISMATCH", "正文区前后锚点必须紧邻且哈希精确匹配。", region_id=region_id, anchor=label)
        if not isinstance(region.get("min_paragraphs"), int) or not isinstance(region.get("max_paragraphs"), int) or region["min_paragraphs"] > region["max_paragraphs"]:
            fail("HYBRID_REGION_CARDINALITY_INVALID", "正文区最小/最大段落数无效。", region_id=region_id)
        roles: set[str] = set()
        for role in region.get("paragraph_roles", []):
            role_name = str(role.get("role") or "")
            donor = by_path.get(str(role.get("donor_paragraph_path") or ""))
            if not role_name or role_name in roles or donor is None or donor.tag != W_P:
                fail("HYBRID_DONOR_INVALID", "正文角色或 donor 段落无效。", region_id=region_id, role=role_name)
                continue
            roles.add(role_name)
            if _subtree_digest(donor) != role.get("donor_paragraph_sha256"):
                fail("HYBRID_DONOR_HASH_MISMATCH", "donor 段落哈希已变化。", region_id=region_id, role=role_name)
            runs = [item for item in list(donor) if item.tag == W_R]
            if not isinstance(role.get("donor_run_index"), int) or not 1 <= role["donor_run_index"] <= len(runs):
                fail("HYBRID_DONOR_RUN_INVALID", "donor 运行索引无效。", region_id=region_id, role=role_name)
            dangerous = _dangerous_clone_content(donor)
            if dangerous:
                fail("HYBRID_DONOR_DANGEROUS_OOXML", "donor 段落含不可安全克隆对象。", region_id=region_id, role=role_name, findings=dangerous)

    mechanical_ids: set[str] = set()
    for slot in mechanical_slots:
        slot_id = str(slot.get("slot_id") or "")
        locator = slot.get("locator") or {}
        node = by_path.get(str(locator.get("path") or ""))
        if not slot_id or slot_id in mechanical_ids or node is None or node.tag != W_T:
            fail("HYBRID_MECHANICAL_SLOT_INVALID", "机械字段 ID 或稳定文字定位无效。", slot_id=slot_id)
            continue
        mechanical_ids.add(slot_id)
        actual = hashlib.sha256((node.text or "").encode("utf-8")).hexdigest()
        if locator.get("part") != "word/document.xml" or actual != locator.get("expected_text_sha256"):
            fail("HYBRID_MECHANICAL_SLOT_HASH_MISMATCH", "机械字段原文字哈希已变化。", slot_id=slot_id)
        if any(str(locator.get("path")).startswith(prefix + "/") for prefix in region_prefixes):
            fail("HYBRID_MECHANICAL_BODY_OVERLAP", "机械字段不得位于将被整块替换的正文区内。", slot_id=slot_id)
    return errors


def _repeat_item_binding(profile: dict[str, Any], block_id: str, item: dict[str, Any]) -> str:
    return _json_digest({
        "template_id": profile.get("template_id"),
        "profile_id": profile.get("profile_id"),
        "block_id": block_id,
        "item_id": item.get("item_id"),
        "index": item.get("index"),
        "values": item.get("values"),
    })


def build_repeatable_plan(
    profile_path: Path | str,
    groups: list[dict[str, Any]],
    output: Path | str | None = None,
) -> dict[str, Any]:
    profile, resolved_profile_path = _load_profile(profile_path)
    profile_check = validate_profile(resolved_profile_path or profile, require_active=True)
    if not profile_check["ok"]:
        raise _error("REPEATABLE_PROFILE_BLOCKED", "repeatable FormProfile 未通过激活门禁。", validation=profile_check)
    if profile.get("profile_kind") != "form" or profile.get("test_only") is not True:
        raise _error("REPEATABLE_TEST_ONLY_REQUIRED", "标准行结构引擎当前只接受 active TEST-ONLY FormProfile。")
    blocks = {item["block_id"]: item for item in profile.get("repeatable_blocks", [])}
    supplied = {str(item.get("block_id") or ""): item for item in groups}
    if len(supplied) != len(groups) or set(supplied) != set(blocks):
        raise _error("REPEATABLE_GROUP_SET_MISMATCH", "计划必须且只能覆盖画像中的全部 repeatable blocks。")
    normalized_groups: list[dict[str, Any]] = []
    for block_id, block in blocks.items():
        raw_items = supplied[block_id].get("items", [])
        if not isinstance(raw_items, list) or not block["min_items"] <= len(raw_items) <= block["max_items"]:
            raise _error("REPEATABLE_ITEM_COUNT_OUT_OF_RANGE", "标准行数量超出已登记范围。", block_id=block_id)
        slots = {item["field_key"]: item for item in block["item_slots"]}
        normalized_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw_item in enumerate(raw_items, start=1):
            item_id = str(raw_item.get("item_id") or "")
            values = raw_item.get("values")
            if not item_id or item_id in seen_ids or not isinstance(values, dict):
                raise _error("REPEATABLE_ITEM_INVALID", "repeat item 必须有唯一 item_id 和 values 对象。", block_id=block_id)
            seen_ids.add(item_id)
            if set(values) - set(slots):
                raise _error("REPEATABLE_ITEM_FIELD_UNKNOWN", "repeat item 含画像未登记字段。", block_id=block_id)
            normalized_values: dict[str, str | None] = {}
            for field_key, slot in slots.items():
                raw_value = values.get(field_key)
                if slot.get("sequence") is True:
                    raw_value = str(index)
                if slot.get("policy") == "manual_blank":
                    if raw_value not in (None, ""):
                        raise _error("REPEATABLE_MANUAL_BLANK_BLOCKED", "标准行人工字段必须留空。", field_key=field_key)
                    normalized_values[field_key] = None
                    continue
                if raw_value in (None, "") and slot.get("policy") in {"required_verified", "lawyer_decision_required"}:
                    raise _error("REPEATABLE_REQUIRED_VALUE_MISSING", "标准行必填字段缺值。", field_key=field_key)
                normalized_values[field_key] = None if raw_value is None else _plain_structural_text(
                    str(raw_value), int(slot["max_chars"]), context=f"{block_id}.{item_id}.{field_key}"
                )
            item = {"item_id": item_id, "index": index, "values": normalized_values}
            item["binding_sha256"] = _repeat_item_binding(profile, block_id, item)
            normalized_items.append(item)
        group = {"block_id": block_id, "items": normalized_items}
        group["group_binding_sha256"] = _json_digest(group)
        normalized_groups.append(group)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "test_only": True,
        "notice": "TEST-ONLY repeatable-row plan; not eligible for filing or production use.",
        "plan_id": f"REPEAT-{profile['template_id']}-{_json_digest(normalized_groups)[:12]}",
        "profile_id": profile["profile_id"],
        "profile_path": str(resolved_profile_path or profile_path),
        "template_id": profile["template_id"],
        "template_sha256": profile["source"]["sha256"],
        "usage_mode": profile["usage_mode"],
        "created_at": now_iso(),
        "status": "ready",
        "slots": [],
        "repeat_groups": normalized_groups,
        "conflicts": [],
        "provenance_snapshot": None,
    }
    if output is not None:
        output_path = Path(output).resolve()
        ensure_not_originals(output_path)
        if output_path.exists():
            raise _error("REPEATABLE_PLAN_OUTPUT_EXISTS", f"计划已存在，拒绝覆盖：{output_path}")
        atomic_write_json(output_path, plan)
    return plan


def validate_repeatable_plan(plan: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    blocks = {item.get("block_id"): item for item in profile.get("repeatable_blocks", [])}
    groups = plan.get("repeat_groups")
    if plan.get("test_only") is not True or profile.get("test_only") is not True:
        errors.append({"code": "REPEATABLE_TEST_ONLY_REQUIRED", "message": "标准行结构执行只允许 TEST-ONLY。"})
    if plan.get("template_id") != profile.get("template_id") or plan.get("template_sha256") != profile.get("source", {}).get("sha256"):
        errors.append({"code": "REPEATABLE_PLAN_TEMPLATE_MISMATCH", "message": "标准行计划与画像/原件不一致。"})
    if not isinstance(groups, list) or {item.get("block_id") for item in groups} != set(blocks) or len(groups) != len(blocks):
        errors.append({"code": "REPEATABLE_GROUP_SET_MISMATCH", "message": "repeat_groups 集合不完整或重复。"})
        return {"ok": False, "errors": errors}
    for group in groups:
        block_id = group.get("block_id")
        block = blocks[block_id]
        items = group.get("items", [])
        if not block["min_items"] <= len(items) <= block["max_items"]:
            errors.append({"code": "REPEATABLE_ITEM_COUNT_OUT_OF_RANGE", "message": "标准行数量越界。", "block_id": block_id})
        if group.get("group_binding_sha256") != _json_digest({"block_id": block_id, "items": items}):
            errors.append({"code": "REPEATABLE_GROUP_BINDING_MISMATCH", "message": "标准行组绑定哈希不一致。", "block_id": block_id})
        for expected_index, item in enumerate(items, start=1):
            if item.get("index") != expected_index or item.get("binding_sha256") != _repeat_item_binding(profile, block_id, item):
                errors.append({"code": "REPEATABLE_ITEM_BINDING_MISMATCH", "message": "标准行项目顺序或绑定哈希不一致。", "block_id": block_id})
    return {"ok": not errors, "errors": errors}


def _hybrid_mechanical_binding(profile: dict[str, Any], slot: dict[str, Any], action: str, value: Any, approval: Any) -> str:
    return _json_digest({
        "template_id": profile.get("template_id"), "profile_id": profile.get("profile_id"),
        "slot_id": slot.get("slot_id"), "field_key": slot.get("field_key"),
        "action": action, "value": value, "approval": approval,
    })


def build_hybrid_plan(
    profile_path: Path | str,
    mechanical_data: dict[str, Any],
    bodies: list[dict[str, Any]],
    output: Path | str | None = None,
) -> dict[str, Any]:
    profile, resolved_profile_path = _load_profile(profile_path)
    profile_check = validate_profile(resolved_profile_path or profile, require_active=True)
    if not profile_check["ok"]:
        raise _error("HYBRID_PROFILE_BLOCKED", "hybrid WritingProfile 未通过激活门禁。", validation=profile_check)
    if profile.get("profile_kind") != "writing" or profile.get("usage_mode") != "hybrid" or profile.get("test_only") is not True:
        raise _error("HYBRID_TEST_ONLY_REQUIRED", "正文结构引擎当前只接受 active TEST-ONLY hybrid WritingProfile。")
    mechanical: list[dict[str, Any]] = []
    for slot in profile.get("mechanical_slots", []):
        key = slot["field_key"]
        supplied = mechanical_data.get(key)
        approval = supplied.get("approval") if isinstance(supplied, dict) else None
        raw_value = supplied.get("value") if isinstance(supplied, dict) else supplied
        policy = slot["policy"]
        if policy == "fixed_locked":
            if key in mechanical_data:
                raise _error("HYBRID_FIXED_EDIT_BLOCKED", "固定机械字段禁止传值。", field_key=key)
            action, value = "preserve", None
        elif policy == "manual_blank":
            if raw_value not in (None, ""):
                raise _error("HYBRID_MANUAL_BLANK_BLOCKED", "签章/现场日期必须留空。", field_key=key)
            action, value = "clear_to_blank", None
        elif raw_value in (None, ""):
            if policy in {"required_verified", "lawyer_decision_required"}:
                raise _error("HYBRID_REQUIRED_VALUE_MISSING", "机械字段缺少必填值。", field_key=key)
            action, value = "clear_to_blank", None
        else:
            value = _plain_structural_text(str(raw_value), int(slot["max_chars"]), context=f"mechanical.{key}")
            action = "replace"
        if policy in {"lawyer_decision_required", "derived_needs_confirmation"} and action == "replace":
            actor = str((approval or {}).get("approved_by") or "")
            if not (approval and (approval or {}).get("decision_id") and actor and not re.search(r"(?i)ai|assistant|agent|model|system", actor)):
                raise _error("HYBRID_LAWYER_APPROVAL_REQUIRED", "高风险机械字段必须绑定明确人工批准。", field_key=key)
        record = {"slot_id": slot["slot_id"], "action": action, "value": value, "approval": approval}
        record["binding_sha256"] = _hybrid_mechanical_binding(profile, slot, action, value, approval)
        mechanical.append(record)
    regions = {item["region_id"]: item for item in profile.get("body_regions", [])}
    supplied_bodies = {str(item.get("region_id") or ""): item for item in bodies}
    if len(supplied_bodies) != len(bodies) or set(supplied_bodies) != set(regions):
        raise _error("HYBRID_BODY_SET_MISMATCH", "计划必须且只能覆盖画像中的全部 body regions。")
    replacements: list[dict[str, Any]] = []
    for region_id, region in regions.items():
        raw_paragraphs = supplied_bodies[region_id].get("paragraphs", [])
        if not isinstance(raw_paragraphs, list) or not region["min_paragraphs"] <= len(raw_paragraphs) <= region["max_paragraphs"]:
            raise _error("HYBRID_PARAGRAPH_COUNT_OUT_OF_RANGE", "正文段落数量越界。", region_id=region_id)
        roles = {item["role"]: item for item in region["paragraph_roles"]}
        paragraphs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_paragraphs:
            paragraph_id = str(raw.get("paragraph_id") or "")
            role = str(raw.get("role") or "")
            if not paragraph_id or paragraph_id in seen or role not in roles:
                raise _error("HYBRID_PARAGRAPH_INVALID", "正文段落 ID 重复、缺失或角色未登记。", region_id=region_id)
            seen.add(paragraph_id)
            text = _plain_structural_text(raw.get("text"), int(roles[role]["max_chars"]), context=f"{region_id}.{paragraph_id}")
            if not text:
                raise _error("HYBRID_EMPTY_PARAGRAPH_BLOCKED", "空段应通过0段合同表达，不能伪装正文。", region_id=region_id)
            paragraphs.append({"paragraph_id": paragraph_id, "role": role, "text": text})
        replacement = {"region_id": region_id, "paragraphs": paragraphs}
        replacement["binding_sha256"] = _json_digest({
            "template_id": profile["template_id"], "profile_id": profile["profile_id"], **replacement
        })
        replacements.append(replacement)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "test_only": True,
        "notice": "TEST-ONLY hybrid plan; not eligible for filing or production use.",
        "plan_id": f"HYBRID-{profile['template_id']}-{_json_digest([mechanical, replacements])[:12]}",
        "profile_id": profile["profile_id"], "profile_path": str(resolved_profile_path or profile_path),
        "template_id": profile["template_id"], "template_sha256": profile["source"]["sha256"],
        "usage_mode": "hybrid", "created_at": now_iso(), "status": "ready",
        "mechanical_slots": mechanical, "body_replacements": replacements,
    }
    if output is not None:
        output_path = Path(output).resolve()
        ensure_not_originals(output_path)
        if output_path.exists():
            raise _error("HYBRID_PLAN_OUTPUT_EXISTS", f"计划已存在，拒绝覆盖：{output_path}")
        atomic_write_json(output_path, plan)
    return plan


def validate_hybrid_plan(plan: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if plan.get("test_only") is not True or profile.get("test_only") is not True:
        errors.append({"code": "HYBRID_TEST_ONLY_REQUIRED", "message": "hybrid 结构执行只允许 TEST-ONLY。"})
    if plan.get("template_id") != profile.get("template_id") or plan.get("template_sha256") != profile.get("source", {}).get("sha256"):
        errors.append({"code": "HYBRID_PLAN_TEMPLATE_MISMATCH", "message": "hybrid 计划与画像/原件不一致。"})
    slots = {item.get("slot_id"): item for item in profile.get("mechanical_slots", [])}
    records = plan.get("mechanical_slots", [])
    if len(records) != len(slots) or {item.get("slot_id") for item in records} != set(slots):
        errors.append({"code": "HYBRID_MECHANICAL_SET_MISMATCH", "message": "机械字段集合不完整或重复。"})
    else:
        for record in records:
            slot = slots[record["slot_id"]]
            if record.get("binding_sha256") != _hybrid_mechanical_binding(
                profile, slot, str(record.get("action")), record.get("value"), record.get("approval")
            ):
                errors.append({"code": "HYBRID_MECHANICAL_BINDING_MISMATCH", "message": "机械字段绑定哈希不一致。", "slot_id": record.get("slot_id")})
    regions = {item.get("region_id"): item for item in profile.get("body_regions", [])}
    replacements = plan.get("body_replacements", [])
    if len(replacements) != len(regions) or {item.get("region_id") for item in replacements} != set(regions):
        errors.append({"code": "HYBRID_BODY_SET_MISMATCH", "message": "正文区集合不完整或重复。"})
    else:
        for replacement in replacements:
            expected = _json_digest({
                "template_id": profile.get("template_id"), "profile_id": profile.get("profile_id"),
                "region_id": replacement.get("region_id"), "paragraphs": replacement.get("paragraphs"),
            })
            if replacement.get("binding_sha256") != expected:
                errors.append({"code": "HYBRID_BODY_BINDING_MISMATCH", "message": "正文区绑定哈希不一致。", "region_id": replacement.get("region_id")})
    return {"ok": not errors, "errors": errors}


def _serialize_document(root: ET.Element) -> bytes:
    ET.register_namespace("w", W_NS)
    ET.register_namespace("r", R_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _clone_body_paragraph(donor: ET.Element, donor_run_index: int, text: str) -> ET.Element:
    paragraph = ET.Element(W_P)
    paragraph_properties = donor.find(f"./{W_PPR}")
    if paragraph_properties is not None:
        paragraph.append(copy.deepcopy(paragraph_properties))
    runs = [item for item in list(donor) if item.tag == W_R]
    donor_run = runs[donor_run_index - 1]
    run = ET.Element(W_R)
    run_properties = donor_run.find(f"./{W_RPR}")
    if run_properties is not None:
        run.append(copy.deepcopy(run_properties))
    text_node = ET.SubElement(run, W_T)
    text_node.text = text
    paragraph.append(run)
    return paragraph


def _transform_aware_diff(
    source: Path, candidate: Path, *, changed_part: str, operation_manifest: list[dict[str, Any]]
) -> dict[str, Any]:
    before = package_inventory(source)
    after = package_inventory(candidate)
    names_equal = set(before) == set(after)
    changed = sorted(name for name in set(before) & set(after) if before[name]["sha256"] != after[name]["sha256"])
    unexpected = sorted(set(changed) - {changed_part})
    return {
        "ok": names_equal and not unexpected and changed_part in before and changed_part in after,
        "part_set_unchanged": names_equal,
        "changed_parts": changed,
        "unexpected_changed_parts": unexpected,
        "authorized_structure_change_part": changed_part,
        "operation_manifest": operation_manifest,
        "candidate_part_sha256": after.get(changed_part, {}).get("sha256"),
        "preserve_part_count": len(before) - (1 if changed_part in before else 0),
    }


def apply_structural_docx_plan(
    source: Path | str,
    destination: Path | str,
    profile_or_path: dict[str, Any] | Path | str,
    plan_or_path: dict[str, Any] | Path | str,
) -> dict[str, Any]:
    """Apply a TEST-ONLY repeatable-row or block-aligned hybrid transformation."""

    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    ensure_not_originals(destination_path)
    if source_path == destination_path or destination_path.exists():
        raise _error("STRUCTURAL_OUTPUT_INVALID", "结构输出必须是不存在且不同于原件的新路径。")
    profile, resolved_profile_path = _load_profile(profile_or_path)
    plan = copy.deepcopy(plan_or_path) if isinstance(plan_or_path, dict) else load_json(Path(plan_or_path))
    profile_check = validate_profile(resolved_profile_path or profile, require_active=True)
    if not profile_check["ok"]:
        raise _error("STRUCTURAL_PROFILE_BLOCKED", "结构画像未通过激活门禁。", validation=profile_check)
    source_hash_before = sha256_file(source_path)
    if source_hash_before != profile.get("source", {}).get("sha256") or plan.get("template_sha256") != source_hash_before:
        raise _error("STRUCTURAL_SOURCE_HASH_MISMATCH", "原件、画像和计划哈希不一致。")
    entries = _read_zip_entries(source_path)
    root, _raw = _document_root_from_entries(entries)
    by_path = _path_index(root)
    operation_manifest: list[dict[str, Any]] = []

    if profile.get("profile_kind") == "form":
        plan_check = validate_repeatable_plan(plan, profile)
        if not plan_check["ok"]:
            raise _error("REPEATABLE_PLAN_BLOCKED", "标准行计划校验失败。", validation=plan_check)
        groups = {item["block_id"]: item for item in plan["repeat_groups"]}
        operations: list[tuple[int, ET.Element, ET.Element, dict[str, Any], dict[str, Any]]] = []
        for block in profile.get("repeatable_blocks", []):
            container = by_path[block["container_path"]]
            prototype = by_path[block["prototype_row_path"]]
            operations.append((list(container).index(prototype), container, prototype, block, groups[block["block_id"]]))
        for row_index, container, prototype, block, group in sorted(operations, key=lambda item: item[0], reverse=True):
            new_rows: list[ET.Element] = []
            for item in group["items"]:
                row = copy.deepcopy(prototype)
                nodes = list(row.iter(W_T))
                for slot in block["item_slots"]:
                    value = item["values"].get(slot["field_key"])
                    nodes[int(slot["text_node_index"]) - 1].text = "" if value is None else str(value)
                dangerous = _dangerous_clone_content(row)
                if dangerous:
                    raise _error("REPEATABLE_CLONE_DANGEROUS_OOXML", "克隆结果出现危险OOXML。", findings=dangerous)
                new_rows.append(row)
            container.remove(prototype)
            for offset, row in enumerate(new_rows):
                container.insert(row_index + offset, row)
            operation_manifest.append({
                "kind": "repeatable_row_replace", "block_id": block["block_id"],
                "prototype_row_sha256": block["prototype_row_sha256"],
                "output_row_count": len(new_rows), "output_row_sha256": [_subtree_digest(row) for row in new_rows],
            })
    elif profile.get("profile_kind") == "writing":
        plan_check = validate_hybrid_plan(plan, profile)
        if not plan_check["ok"]:
            raise _error("HYBRID_PLAN_BLOCKED", "hybrid 计划校验失败。", validation=plan_check)
        mechanical_records = {item["slot_id"]: item for item in plan["mechanical_slots"]}
        for slot in profile.get("mechanical_slots", []):
            record = mechanical_records[slot["slot_id"]]
            node = by_path[slot["locator"]["path"]]
            if record["action"] == "replace":
                node.text = str(record["value"])
            elif record["action"] == "clear_to_blank":
                node.text = ""
        replacements = {item["region_id"]: item for item in plan["body_replacements"]}
        operations = []
        for region in profile.get("body_regions", []):
            container = by_path[region["container_path"]]
            children = list(container)
            start, end = by_path[region["start_block_path"]], by_path[region["end_block_path"]]
            start_index, end_index = children.index(start), children.index(end)
            donors = {
                role["role"]: (copy.deepcopy(by_path[role["donor_paragraph_path"]]), role)
                for role in region["paragraph_roles"]
            }
            operations.append((start_index, end_index, container, region, replacements[region["region_id"]], donors))
        for start_index, end_index, container, region, replacement, donors in sorted(operations, key=lambda item: item[0], reverse=True):
            current = list(container)
            for element in current[start_index : end_index + 1]:
                container.remove(element)
            new_paragraphs: list[ET.Element] = []
            for paragraph_record in replacement["paragraphs"]:
                donor, role = donors[paragraph_record["role"]]
                paragraph = _clone_body_paragraph(donor, int(role["donor_run_index"]), paragraph_record["text"])
                dangerous = _dangerous_clone_content(paragraph)
                if dangerous:
                    raise _error("HYBRID_OUTPUT_DANGEROUS_OOXML", "正文输出出现危险OOXML。", findings=dangerous)
                new_paragraphs.append(paragraph)
            for offset, paragraph in enumerate(new_paragraphs):
                container.insert(start_index + offset, paragraph)
            operation_manifest.append({
                "kind": "hybrid_body_replace", "region_id": region["region_id"],
                "source_block_sha256": region["source_block_sha256"],
                "output_paragraph_count": len(new_paragraphs),
                "output_paragraph_sha256": [_subtree_digest(item) for item in new_paragraphs],
            })
    else:
        raise _error("STRUCTURAL_PROFILE_KIND_UNSUPPORTED", "结构引擎只接受 form 或 writing 画像。")

    patched_document = _serialize_document(root)
    patched_entries = [
        (info, patched_document if info.filename == "word/document.xml" else data)
        for info, data in entries
    ]
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=".lcos-structural-", suffix=".docx", dir=destination_path.parent, delete=False)
    temporary_path = Path(handle.name)
    handle.close()
    try:
        _write_docx_entries(patched_entries, temporary_path)
        diff = _transform_aware_diff(
            source_path, temporary_path, changed_part="word/document.xml", operation_manifest=operation_manifest
        )
        if not diff["ok"]:
            raise _error("TRANSFORM_AWARE_DIFF_FAILED", "结构变换修改了未获准部件。", diff=diff)
        output_root, _ = _document_root_from_entries(_read_zip_entries(temporary_path))
        remaining_hashes = {
            hashlib.sha256((node.text or "").encode("utf-8")).hexdigest()
            for node in output_root.iter(W_T) if node.text
        }
        blocked = set(profile.get("blocked_exemplar_text_sha256", []))
        blocked.update(
            digest
            for block in profile.get("repeatable_blocks", [])
            for digest in block.get("blocked_text_sha256", [])
        )
        blocked.update(
            digest
            for region in profile.get("body_regions", [])
            for digest in region.get("blocked_text_sha256", [])
        )
        leaked = sorted(blocked & remaining_hashes)
        if leaked:
            raise _error("STRUCTURAL_EXEMPLAR_LEAK_BLOCKED", "结构输出仍含登记的旧案文字。", leaked_hashes=leaked)
        from .documents import preflight

        preflight_result = preflight(temporary_path)
        if not preflight_result["ok"]:
            raise _error("STRUCTURAL_DOCX_PREFLIGHT_BLOCKED", "结构输出未通过DOCX预检。", preflight=preflight_result)
        if sha256_file(source_path) != source_hash_before:
            raise _error("STRUCTURAL_SOURCE_CHANGED", "结构变换期间原件发生变化。")
        _exclusive_copy(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "ok": True, "test_only": True,
        "source": str(source_path), "source_sha256": source_hash_before,
        "destination": str(destination_path), "destination_sha256": sha256_file(destination_path),
        "transform_aware_diff": diff, "preflight": preflight_result,
        "release_status": "TEST-ONLY_structurally_valid_pending_visual_qa",
        "court_candidate": False, "filing_eligible": False, "visual_qa_required": True,
    }


def _find_soffice(explicit: Path | str | None = None) -> Path | None:
    if explicit is not None:
        candidate = Path(explicit).resolve()
        return candidate if candidate.is_file() else None
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


def _soffice_version(converter: Path) -> str:
    try:
        completed = subprocess.run(
            [str(converter), "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return (completed.stdout or completed.stderr).strip().splitlines()[0] if (completed.stdout or completed.stderr).strip() else "unavailable"


def _run_soffice_conversion(
    converter: Path,
    source: Path,
    output_dir: Path,
    profile_dir: Path,
    *,
    convert_to: str,
    timeout_seconds: int,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    # LibreOffice ships its own Python runtime on Windows.  Inheriting the
    # caller's PYTHONHOME/PYTHONPATH can make an otherwise valid conversion
    # fail or emit misleading runtime errors, so keep those variables available
    # to the invoking Python process but do not pass them into soffice.
    conversion_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
    }
    try:
        completed = subprocess.run(
            [
                str(converter),
                f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                "--headless",
                "--nologo",
                "--norestore",
                "--nofirststartwizard",
                "--convert-to",
                convert_to,
                "--outdir",
                str(output_dir),
                str(source),
            ],
            cwd=output_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=conversion_environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise _error(
            "LEGACY_DOC_CONVERSION_TIMEOUT",
            f"LibreOffice {convert_to} 转换在 {timeout_seconds} 秒内未完成；未发布任何派生件。",
        ) from exc
    suffix = ".docx" if convert_to == "docx" else ".pdf"
    converted = output_dir / f"{source.stem}{suffix}"
    valid = converted.is_file()
    if convert_to == "docx":
        valid = valid and zipfile.is_zipfile(converted)
    elif convert_to == "pdf":
        valid = valid and converted.read_bytes()[:5] == b"%PDF-"
    if completed.returncode != 0 or not valid:
        raise _error(
            "LEGACY_DOC_CONVERSION_FAILED",
            f"LibreOffice 未生成有效 {suffix}；未发布任何派生件。",
            convert_to=convert_to,
            exit_code=completed.returncode,
            stdout=completed.stdout[-2000:],
            stderr=completed.stderr[-2000:],
        )
    return converted


def _pdf_page_count(path: Path) -> tuple[int, str]:
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
            return len(module.PdfReader(str(path)).pages), module_name
        except Exception:
            continue
    # Fail-closed fallback for environments without a PDF library. It accepts
    # only conventional, non-object-stream page dictionaries and never guesses
    # a positive count when none can be found.
    count = len(re.findall(rb"/Type\s*/Page(?!s)\b", path.read_bytes()))
    if count < 1:
        raise _error(
            "LEGACY_RENDER_PAGE_COUNT_UNAVAILABLE",
            f"无法读取 PDF 页数，旧 .doc 转换结果禁止登记：{path}",
        )
    return count, "pdf-page-dictionary-fallback"


def convert_legacy_doc(
    source: Path | str,
    destination: Path | str,
    *,
    soffice: Path | str | None = None,
    timeout_seconds: int = 90,
    render_report: Path | str | None = None,
) -> dict[str, Any]:
    """Convert a legacy .doc without overwrite, optionally with dual PDF evidence.

    When ``render_report`` is supplied, the source ``.doc`` and derived
    ``.docx`` are independently rendered to PDF.  The DOCX and report are not
    published unless both PDFs have a readable, equal page count.  Visual
    comparison remains a separate human gate and is explicitly marked pending.
    """
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    report_path = Path(render_report).resolve() if render_report is not None else None
    ensure_not_originals(destination_path)
    if source_path.suffix.casefold() != ".doc" or not source_path.is_file():
        raise _error("LEGACY_DOC_REQUIRED", f"旧格式转换接口只接受存在的 .doc：{source_path}")
    if destination_path.suffix.casefold() != ".docx":
        raise _error("DOCX_OUTPUT_REQUIRED", f"旧格式转换输出必须为 .docx：{destination_path}")
    if destination_path.exists():
        raise _error("OUTPUT_EXISTS", f"转换输出已经存在，拒绝覆盖：{destination_path}")
    if report_path is not None:
        if report_path.suffix.casefold() != ".json":
            raise _error("LEGACY_RENDER_REPORT_JSON_REQUIRED", "render_report 必须为 .json 路径。")
        source_pdf_path = report_path.with_name(f"{report_path.stem}-source-doc.pdf")
        derived_pdf_path = report_path.with_name(f"{report_path.stem}-derived-docx.pdf")
        for output in (report_path, source_pdf_path, derived_pdf_path):
            ensure_not_originals(output)
            if output.exists():
                raise _error("OUTPUT_EXISTS", f"旧格式渲染比对输出已经存在，拒绝覆盖：{output}")
    else:
        source_pdf_path = None
        derived_pdf_path = None
    converter = _find_soffice(soffice)
    if converter is None:
        raise _error("LEGACY_DOC_CONVERTER_UNAVAILABLE", "未找到本地 LibreOffice，旧 .doc 保持原样且未生成派生件。")
    source_hash_before = sha256_file(source_path)
    converter_version = _soffice_version(converter)
    published: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="lcosdoc-") as temporary:
        staging = Path(temporary)
        short_source = staging / "input.doc"
        shutil.copyfile(source_path, short_source)
        converted = _run_soffice_conversion(
            converter,
            short_source,
            staging / "docx-output",
            staging / "lo-profile-docx",
            convert_to="docx",
            timeout_seconds=timeout_seconds,
        )
        report: dict[str, Any] | None = None
        direct_pdf: Path | None = None
        docx_pdf: Path | None = None
        if report_path is not None:
            direct_pdf = _run_soffice_conversion(
                converter,
                short_source,
                staging / "source-pdf-output",
                staging / "lo-profile-source-pdf",
                convert_to="pdf",
                timeout_seconds=timeout_seconds,
            )
            docx_pdf = _run_soffice_conversion(
                converter,
                converted,
                staging / "derived-pdf-output",
                staging / "lo-profile-derived-pdf",
                convert_to="pdf",
                timeout_seconds=timeout_seconds,
            )
            source_pages, source_counter = _pdf_page_count(direct_pdf)
            derived_pages, derived_counter = _pdf_page_count(docx_pdf)
            if source_pages != derived_pages:
                raise _error(
                    "LEGACY_RENDER_PAGE_COUNT_MISMATCH",
                    "旧 .doc 直转 PDF 与派生 DOCX 转 PDF 页数不同，禁止发布或登记。",
                    source_page_count=source_pages,
                    derived_page_count=derived_pages,
                )
        if sha256_file(source_path) != source_hash_before:
            raise _error("SOURCE_TEMPLATE_CHANGED_DURING_CONVERSION", "转换期间旧 .doc 原件发生变化，派生件已作废。")
        try:
            _exclusive_copy(converted, destination_path)
            published.append(destination_path)
            if report_path is not None and direct_pdf is not None and docx_pdf is not None:
                assert source_pdf_path is not None and derived_pdf_path is not None
                _exclusive_copy(direct_pdf, source_pdf_path)
                published.append(source_pdf_path)
                _exclusive_copy(docx_pdf, derived_pdf_path)
                published.append(derived_pdf_path)
                report = {
                    "schema_version": "1.0.0",
                    "kind": "legacy-doc-render-comparison",
                    "generated_at": now_iso(),
                    "path_base": "report_dir",
                    "converter": converter.name,
                    "converter_version": converter_version,
                    "source_doc": {
                        "path": os.path.relpath(source_path, report_path.parent).replace("\\", "/"),
                        "sha256": source_hash_before,
                    },
                    "derived_docx": {
                        "path": os.path.relpath(destination_path, report_path.parent).replace("\\", "/"),
                        "sha256": sha256_file(destination_path),
                    },
                    "source_direct_pdf": {
                        "path": os.path.relpath(source_pdf_path, report_path.parent).replace("\\", "/"),
                        "sha256": sha256_file(source_pdf_path),
                        "page_count": source_pages,
                        "page_counter": source_counter,
                    },
                    "derived_docx_pdf": {
                        "path": os.path.relpath(derived_pdf_path, report_path.parent).replace("\\", "/"),
                        "sha256": sha256_file(derived_pdf_path),
                        "page_count": derived_pages,
                        "page_counter": derived_counter,
                    },
                    "comparison": {
                        "page_count_equal": True,
                        "visual_review_status": "pending",
                        "reviewed_by": None,
                        "reviewed_at": None,
                    },
                    "registration_eligible": False,
                    "registration_block_reason": "human_visual_comparison_pending",
                }
                temporary_report = staging / "render-report.json"
                atomic_write_json(temporary_report, report)
                _exclusive_copy(temporary_report, report_path)
                published.append(report_path)
        except Exception:
            for output in reversed(published):
                output.unlink(missing_ok=True)
            raise
    result = {
        "ok": True,
        "source": str(source_path),
        "source_sha256": source_hash_before,
        "derived": str(destination_path),
        "derived_sha256": sha256_file(destination_path),
        "converter": str(converter),
        "converter_version": converter_version,
        "render_baseline_status": "pending",
        "registration_blocked_until_visual_baseline": True,
        "visual_qa_required": True,
    }
    if report_path is not None:
        result.update(
            {
                "render_report": str(report_path),
                "render_report_sha256": sha256_file(report_path),
                "source_direct_pdf": str(source_pdf_path),
                "derived_docx_pdf": str(derived_pdf_path),
                "page_count_equal": True,
                "registration_blocked_until_render_review": True,
            }
        )
    return result


def template_approval_receipt_sha256(receipt: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256", None)
    return hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_template_approval_receipt(
    *,
    decision_id: str,
    approved_by: str,
    operation: str,
    template_id: str,
    target_version: str,
    source_sha256: str,
    profile_sha256: str,
    catalog_before_sha256: str,
    decided_at: str,
) -> dict[str, Any]:
    """Build the exact receipt that the lawyer reviews and signs externally."""
    receipt = {
        "receipt_version": "1.0.0",
        "confirmed": True,
        "decision_id": decision_id,
        "approved_by": approved_by,
        "decided_at": decided_at,
        "operation": operation,
        "template_id": template_id,
        "target_version": target_version,
        "source_sha256": source_sha256,
        "profile_sha256": profile_sha256,
        "catalog_before_sha256": catalog_before_sha256,
    }
    receipt["receipt_sha256"] = template_approval_receipt_sha256(receipt)
    return receipt


def _require_registration_approval(
    approval: dict[str, Any],
    operation: str,
    *,
    expected: dict[str, str] | None = None,
) -> None:
    if not isinstance(approval, dict) or approval.get("confirmed") is not True:
        raise _error("TEMPLATE_APPROVAL_REQUIRED", f"模板 {operation} 必须有律师显式批准。")
    required = {
        "receipt_version", "confirmed", "decision_id", "approved_by", "decided_at",
        "operation", "template_id", "target_version", "source_sha256", "profile_sha256",
        "catalog_before_sha256", "receipt_sha256",
    }
    if not isinstance(approval, dict) or set(approval) != required:
        raise _error("TEMPLATE_APPROVAL_RECEIPT_REQUIRED", f"模板 {operation} 必须提供完整 TemplateApprovalReceipt。")
    if approval.get("receipt_version") != "1.0.0" or approval.get("confirmed") is not True:
        raise _error("TEMPLATE_APPROVAL_RECEIPT_INVALID", "模板批准收据版本或 confirmed 无效。")
    for field in ("decision_id", "approved_by", "decided_at"):
        if not str(approval.get(field) or "").strip():
            raise _error("TEMPLATE_APPROVAL_RECEIPT_INVALID", f"模板批准收据缺少 {field}。")
    approver = str(approval.get("approved_by") or "").strip()
    obvious_role = re.search(
        r"(?i)(?:^|[\s_:/-])(ai|assistant|agent|model|system)(?:$|[\s_:/-])|人工智能|模型助手|系统自动",
        approver,
    )
    obvious_product = re.fullmatch(
        r"(?i)(?:chatgpt|gpt|claude|deepseek|qwen|doubao|kimi|gemini|copilot|llama|mistral)"
        r"(?:[-_.:/ ]?(?:\d+[a-z]?|max|plus|turbo|mini|pro|opus|sonnet|haiku|coder|chat))*"
        r"|(?:千问|豆包)(?:[-_.:/ ]?(?:\d+[a-z]?|max|plus|pro))*",
        approver,
    )
    if obvious_role or obvious_product:
        raise _error(
            "TEMPLATE_APPROVAL_HUMAN_REQUIRED",
            "模板登记必须绑定真实人工审批身份；AI、模型或系统名称不能充当 approved_by。",
        )
    if approval.get("receipt_sha256") != template_approval_receipt_sha256(approval):
        raise _error("TEMPLATE_APPROVAL_RECEIPT_HASH_MISMATCH", "模板批准收据哈希不匹配，可能被改写。")
    if expected is not None:
        mismatches = {
            field: {"expected": value, "actual": approval.get(field)}
            for field, value in expected.items() if approval.get(field) != value
        }
        if mismatches:
            raise _error(
                "TEMPLATE_APPROVAL_RECEIPT_SCOPE_MISMATCH",
                "模板批准收据绑定的操作、对象、版本或哈希不是当前待登记对象。",
                mismatches=mismatches,
            )


def _load_registration_catalog(catalog_file: Path) -> dict[str, Any]:
    catalog = load_json(catalog_file)
    if (
        catalog.get("catalog_version") != "1.0.0"
        or not isinstance(catalog.get("templates"), list)
        or not isinstance(catalog.get("suites"), list)
    ):
        raise _error("INVALID_PERSONAL_TEMPLATE_CATALOG", "个人模板目录格式无效。")
    return catalog


def _validate_catalog_or_raise(catalog_file: Path, *, operation: str) -> None:
    from .templates import validate_personal_template_catalog

    validation = validate_personal_template_catalog(catalog_file)
    if not validation["ok"]:
        raise _error(
            "PERSONAL_TEMPLATE_CATALOG_INVALID_AFTER_REGISTRATION",
            f"模板 {operation} 后个人模板目录未通过完整校验，操作已回滚。",
            validation=validation,
        )


def _require_catalog_unchanged(catalog_file: Path, expected_sha256: str) -> None:
    if not catalog_file.is_file() or sha256_file(catalog_file) != expected_sha256:
        raise _error(
            "TEMPLATE_CATALOG_CHANGED_DURING_REGISTRATION",
            "个人模板目录在本次操作期间发生变化；为避免覆盖他人结果，当前操作已停止。",
            catalog=str(catalog_file),
        )


def _serialized_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _catalog_rollback_disposition(
    catalog_file: Path,
    *,
    before_sha256: str,
    written_sha256: str | None,
) -> tuple[str, str | None]:
    """Say whether rollback would restore our bytes or clobber another writer."""

    actual_sha256 = sha256_file(catalog_file) if catalog_file.is_file() else None
    if actual_sha256 == before_sha256:
        return "unchanged", actual_sha256
    if written_sha256 is not None and actual_sha256 == written_sha256:
        return "ours", actual_sha256
    return "diverged", actual_sha256


def _raise_catalog_rollback_conflict(
    catalog_file: Path,
    *,
    operation: str,
    before_sha256: str,
    written_sha256: str | None,
    actual_sha256: str | None,
    cause: Exception,
) -> None:
    raise _error(
        "TEMPLATE_CATALOG_CHANGED_DURING_ROLLBACK",
        "个人模板目录在失败回滚前又被外部修改；为避免抹掉他人结果，已停止自动回滚，请人工对账。",
        operation=operation,
        catalog=str(catalog_file),
        before_sha256=before_sha256,
        operation_write_sha256=written_sha256,
        actual_sha256=actual_sha256,
    ) from cause


def _parsed_version(value: str) -> tuple[list[int], list[int | str] | None] | None:
    match = re.fullmatch(
        r"v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?",
        value.strip(),
    )
    if match is None:
        return None
    release = [int(item) for item in match.group(1).split(".")]
    prerelease = match.group(2)
    if prerelease is None:
        return release, None
    identifiers: list[int | str] = []
    for item in re.split(r"[.-]", prerelease):
        identifiers.append(int(item) if item.isdigit() else item.casefold())
    return release, identifiers


def _version_is_newer(old: str, new: str) -> bool:
    old_value = _parsed_version(old)
    new_value = _parsed_version(new)
    if old_value is None or new_value is None:
        return False
    old_release, old_pre = old_value
    new_release, new_pre = new_value
    width = max(len(old_release), len(new_release))
    old_release_key = tuple(old_release + [0] * (width - len(old_release)))
    new_release_key = tuple(new_release + [0] * (width - len(new_release)))
    if old_release_key != new_release_key:
        return old_release_key < new_release_key
    if old_pre is None or new_pre is None:
        return old_pre is not None and new_pre is None
    for old_item, new_item in zip(old_pre, new_pre):
        if old_item == new_item:
            continue
        if isinstance(old_item, int) and isinstance(new_item, str):
            return True
        if isinstance(old_item, str) and isinstance(new_item, int):
            return False
        return old_item < new_item
    return len(old_pre) < len(new_pre)


def _catalog_approval_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"decision_id", "approval_decision_id"} and isinstance(item, str):
                found.add(item)
            found.update(_catalog_approval_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_catalog_approval_ids(item))
    return found


def _active_profile_and_entry(
    source_path: Path,
    profile_file: Path,
    active_path: Path,
    approval: dict[str, Any],
    *,
    version: str,
    category: str,
    aliases: list[str],
    catalog_file: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    profile, _ = _load_profile(profile_file)
    if profile.get("status") != "draft":
        raise _error("PROFILE_NOT_DRAFT", "只有经过复核的 draft 画像可以登记或升级。")
    if profile.get("unresolved_items"):
        raise _error("PROFILE_UNRESOLVED", "画像仍有未决项目，禁止登记。", items=profile["unresolved_items"])
    preliminary = validate_profile(profile_file, require_active=False)
    if not preliminary["ok"]:
        raise _error("PROFILE_INVALID", "模板画像校验失败，禁止登记。", validation=preliminary)
    expected_hash = profile.get("source", {}).get("sha256")
    if sha256_file(source_path) != expected_hash:
        raise _error("PROFILE_SOURCE_HASH_MISMATCH", "拟登记文件与画像源文件哈希不一致。")
    if sha256_file(active_path) != expected_hash or sha256_file(source_path) != expected_hash:
        raise _error(
            "SOURCE_TEMPLATE_CHANGED_DURING_REGISTRATION",
            "复制激活副本期间模板原件发生变化，登记已回滚。",
        )
    active_profile = copy.deepcopy(profile)
    active_profile["status"] = "active"
    active_profile["activated_at"] = now_iso()
    active_profile["approval"] = copy.deepcopy(approval)
    active_profile["source"]["path"] = os.path.relpath(active_path, profile_file.parent).replace("\\", "/")
    active_profile["source"]["sha256"] = sha256_file(active_path)
    active_check = validate_profile(
        active_profile,
        require_active=True,
        profile_base=profile_file.parent,
    )
    if not active_check["ok"]:
        raise _error("PROFILE_ACTIVATION_INVALID", "激活后的画像不满足使用门禁。", validation=active_check)
    transfer = active_profile.get("transfer_policy", {})
    reusable = transfer.get("reusable_aspects") or (
        ["layout", "field_rules"] if active_profile["profile_kind"] == "form" else ["structure", "style"]
    )
    forbidden = transfer.get("forbidden_transfer") or list(FORBIDDEN_TRANSFER_DEFAULT)
    entry = {
        "id": active_profile["template_id"],
        "name": active_profile["name"],
        "document_type": active_profile["document_type"],
        "category": category,
        "path": os.path.relpath(active_path, catalog_file.parent).replace("\\", "/"),
        "profile_path": os.path.relpath(profile_file, catalog_file.parent).replace("\\", "/"),
        "profile_sha256": None,
        "source": "personal_authorized_template",
        "version": version,
        "sha256": sha256_file(active_path),
        "usage_mode": active_profile["usage_mode"],
        "status": "active",
        "approved_final": True,
        "authorization": {"status": "verified", "basis": approval["decision_id"]},
        "registration_approval": copy.deepcopy(approval),
        "aliases": aliases,
        "reusable_aspects": list(reusable),
        "forbidden_transfer": list(forbidden),
    }
    if active_profile.get("profile_kind") == "writing":
        entry["composition_preferences"] = copy.deepcopy(
            active_profile.get("composition_preferences", {})
        )
    return active_profile, entry


def register_template(
    source: Path | str | None,
    profile_path: Path | str | None,
    catalog_path: Path | str,
    destination_dir: Path | str | None,
    *,
    operation: str,
    approval: dict[str, Any],
    template_id: str | None = None,
    version: str | None = None,
    category: str | None = None,
    aliases: list[str] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Serialize one catalog mutation, including all copies and rollback work."""
    catalog_file = Path(catalog_path).resolve()
    ensure_not_originals(catalog_file)
    lock_path = catalog_file.parent / f".{catalog_file.name}.lock"
    ensure_not_originals(lock_path)
    with exclusive_file_lock(
        lock_path,
        lock_timeout_seconds,
        timeout_code="TEMPLATE_CATALOG_LOCK_TIMEOUT",
        timeout_message="个人模板目录正由另一进程登记、升级或停用；等待锁超时，未写入任何内容。",
        timeout_details={"catalog": str(catalog_file)},
    ):
        return _register_template_locked(
            source,
            profile_path,
            catalog_file,
            destination_dir,
            operation=operation,
            approval=approval,
            template_id=template_id,
            version=version,
            category=category,
            aliases=aliases,
        )


def _register_template_locked(
    source: Path | str | None,
    profile_path: Path | str | None,
    catalog_path: Path | str,
    destination_dir: Path | str | None,
    *,
    operation: str,
    approval: dict[str, Any],
    template_id: str | None = None,
    version: str | None = None,
    category: str | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Create, upgrade, or retire one logical personal template.

    ``upgrade`` keeps the old template file and old profile, marks that profile
    retired, records a superseded revision, and publishes a distinct new copy.
    ``retire`` changes only catalog/profile status; no file is moved or deleted.
    Every operation requires an explicit lawyer approval and rolls back local
    writes if final catalog validation fails.
    """
    if operation not in {"create", "upgrade", "retire"}:
        raise _error(
            "TEMPLATE_REGISTER_OPERATION_INVALID",
            "operation 必须明确为 create、upgrade 或 retire。",
        )
    _require_registration_approval(approval, operation)
    catalog_file = Path(catalog_path).resolve()
    ensure_not_originals(catalog_file)
    catalog = _load_registration_catalog(catalog_file)
    catalog_before = catalog_file.read_bytes()
    catalog_before_sha256 = hashlib.sha256(catalog_before).hexdigest()
    if approval.get("decision_id") in _catalog_approval_ids(catalog):
        raise _error(
            "TEMPLATE_APPROVAL_RECEIPT_REPLAYED",
            f"批准收据 {approval.get('decision_id')} 已在个人模板目录中使用，禁止重放。",
        )

    if operation == "retire":
        if not template_id:
            raise _error("TEMPLATE_ID_REQUIRED", "retire 操作必须明确 template_id。")
        matches = [
            (index, entry)
            for index, entry in enumerate(catalog["templates"])
            if entry.get("id") == template_id
        ]
        if len(matches) != 1:
            raise _error(
                "PERSONAL_TEMPLATE_NOT_FOUND" if not matches else "AMBIGUOUS_PERSONAL_TEMPLATE",
                f"retire 未找到唯一模板：{template_id}",
            )
        index, existing = matches[0]
        if existing.get("status") != "active":
            raise _error("PERSONAL_TEMPLATE_NOT_ACTIVE", f"模板 {template_id} 已非 active，不能重复停用。")
        from .templates import personal_profile_path

        _require_registration_approval(
            approval,
            operation,
            expected={
                "operation": operation,
                "template_id": str(template_id),
                "target_version": str(existing.get("version")),
                "source_sha256": str(existing.get("sha256")),
                "profile_sha256": str(existing.get("profile_sha256")),
                "catalog_before_sha256": catalog_before_sha256,
            },
        )

        retired_profile_path = personal_profile_path(existing, catalog_file)
        ensure_not_originals(retired_profile_path)
        retired_profile_before = retired_profile_path.read_bytes()
        updated_catalog = copy.deepcopy(catalog)
        retired_at = now_iso()
        updated_entry = copy.deepcopy(existing)
        updated_entry["status"] = "retired"
        updated_entry["retired_at"] = retired_at
        updated_entry["retirement_approval"] = copy.deepcopy(approval)
        catalog_write_sha256: str | None = None
        try:
            updated_catalog["templates"][index] = updated_entry
            _require_catalog_unchanged(catalog_file, catalog_before_sha256)
            catalog_write_sha256 = _serialized_json_sha256(updated_catalog)
            atomic_write_json(catalog_file, updated_catalog)
            _validate_catalog_or_raise(catalog_file, operation=operation)
        except Exception as exc:
            rollback_state, actual_catalog_sha256 = _catalog_rollback_disposition(
                catalog_file,
                before_sha256=catalog_before_sha256,
                written_sha256=catalog_write_sha256,
            )
            if rollback_state == "diverged":
                _raise_catalog_rollback_conflict(
                    catalog_file,
                    operation=operation,
                    before_sha256=catalog_before_sha256,
                    written_sha256=catalog_write_sha256,
                    actual_sha256=actual_catalog_sha256,
                    cause=exc,
                )
            if rollback_state == "ours":
                catalog_file.write_bytes(catalog_before)
            raise
        if retired_profile_path.read_bytes() != retired_profile_before:
            catalog_file.write_bytes(catalog_before)
            retired_profile_path.write_bytes(retired_profile_before)
            raise _error(
                "RETIRED_PROFILE_CHANGED",
                "retire 只能改变目录状态；画像文件发生变化，操作已回滚。",
            )
        return {
            "ok": True,
            "operation": operation,
            "template_id": template_id,
            "status": "retired",
            "active_path": str((catalog_file.parent / existing["path"]).resolve()),
            "profile_path": str(retired_profile_path),
            "catalog_path": str(catalog_file),
            "files_preserved": True,
        }

    if source is None or profile_path is None or destination_dir is None:
        raise _error(
            "TEMPLATE_REGISTER_INPUT_MISSING",
            f"{operation} 必须提供 source、profile_path 和 destination_dir。",
        )
    source_path = Path(source).resolve()
    profile_file = Path(profile_path).resolve()
    active_dir = Path(destination_dir).resolve()
    ensure_not_originals(profile_file)
    ensure_not_originals(active_dir)
    profile, _ = _load_profile(profile_file)
    resolved_id = profile.get("template_id")
    if template_id is not None and template_id != resolved_id:
        raise _error("TEMPLATE_ID_PROFILE_MISMATCH", "显式 template_id 与画像不一致。")
    template_id = str(resolved_id or "")
    if not template_id:
        raise _error("TEMPLATE_ID_REQUIRED", "画像缺少 template_id。")
    existing_matches = [
        (index, entry)
        for index, entry in enumerate(catalog["templates"])
        if entry.get("id") == template_id
    ]
    if operation == "create" and existing_matches:
        raise _error("PERSONAL_TEMPLATE_ID_EXISTS", f"模板 ID 已登记：{template_id}")
    if operation == "upgrade" and len(existing_matches) != 1:
        raise _error(
            "PERSONAL_TEMPLATE_NOT_FOUND" if not existing_matches else "AMBIGUOUS_PERSONAL_TEMPLATE",
            f"upgrade 未找到唯一模板：{template_id}",
        )
    if operation == "upgrade" and existing_matches[0][1].get("status") != "active":
        raise _error("PERSONAL_TEMPLATE_NOT_ACTIVE", "只能升级当前 active 模板。")
    resolved_version = version or ("1.0.0" if operation == "create" else "")
    if operation == "upgrade":
        existing_entry = existing_matches[0][1]
        if sha256_file(source_path) == existing_entry.get("sha256"):
            raise _error(
                "TEMPLATE_UPGRADE_SOURCE_UNCHANGED",
                "升级必须提供内容哈希不同的新模板源文件；不能用旧副本伪装新版本。",
            )
        if sha256_file(profile_file) == existing_entry.get("profile_sha256"):
            raise _error(
                "TEMPLATE_UPGRADE_PROFILE_UNCHANGED",
                "升级必须提供新的 draft 画像及新画像哈希。",
            )
        old_version = str(existing_matches[0][1].get("version", ""))
        if not resolved_version or not _version_is_newer(old_version, resolved_version):
            raise _error(
                "TEMPLATE_VERSION_NOT_NEWER",
                f"升级版本必须高于当前版本：{old_version} -> {resolved_version or '<missing>'}",
            )
    _require_registration_approval(
        approval,
        operation,
        expected={
            "operation": operation,
            "template_id": template_id,
            "target_version": str(resolved_version),
            "source_sha256": sha256_file(source_path),
            "profile_sha256": sha256_file(profile_file),
            "catalog_before_sha256": catalog_before_sha256,
        },
    )
    library_root = catalog_file.parent.parent.resolve()
    try:
        active_dir.relative_to(library_root)
    except ValueError as exc:
        raise _error("PERSONAL_TEMPLATE_PATH_OUTSIDE_LIBRARY", f"激活目录越出模板库：{active_dir}") from exc
    active_path = active_dir / (
        f"{_safe_filename(template_id)}-{_safe_filename(resolved_version)}{source_path.suffix.casefold()}"
    )
    if active_path.exists():
        raise _error("OUTPUT_EXISTS", f"新版本副本已经存在，拒绝覆盖：{active_path}")
    profile_before = profile_file.read_bytes()
    prior_profile_path: Path | None = None
    prior_profile_before: bytes | None = None
    fingerprint_manifest_path: Path | None = None
    catalog_write_sha256: str | None = None
    active_dir.mkdir(parents=True, exist_ok=True)
    _exclusive_copy(source_path, active_path)
    try:
        effective_category = category or (
            existing_matches[0][1].get("category", "personal") if operation == "upgrade" else "personal"
        )
        effective_aliases = list(
            aliases
            if aliases is not None
            else (existing_matches[0][1].get("aliases", []) if operation == "upgrade" else [])
        )
        active_profile, new_entry = _active_profile_and_entry(
            source_path,
            profile_file,
            active_path,
            approval,
            version=resolved_version,
            category=str(effective_category),
            aliases=effective_aliases,
            catalog_file=catalog_file,
        )
        updated_catalog = copy.deepcopy(catalog)
        if operation == "upgrade":
            from .templates import personal_profile_path

            existing_index, existing = existing_matches[0]
            prior_profile_path = personal_profile_path(existing, catalog_file)
            ensure_not_originals(prior_profile_path)
            if prior_profile_path.resolve() == profile_file.resolve():
                raise _error(
                    "UPGRADE_PROFILE_MUST_BE_NEW",
                    "升级必须使用新的 draft 画像文件，不能覆盖当前 active 画像。",
                )
            prior_profile_before = prior_profile_path.read_bytes()
            prior_profile = load_json(prior_profile_path)
            superseded_at = now_iso()
            prior_profile["status"] = "retired"
            prior_profile["retired_at"] = superseded_at
            prior_profile["retirement_approval"] = copy.deepcopy(approval)
            atomic_write_json(prior_profile_path, prior_profile)
            history = list(existing.get("revision_history", []))
            history.append(
                {
                    "version": existing["version"],
                    "path": existing["path"],
                    "sha256": existing["sha256"],
                    "profile_path": existing["profile_path"],
                    "profile_sha256": sha256_file(prior_profile_path),
                    "status": "superseded",
                    "superseded_at": superseded_at,
                    "superseded_by_version": resolved_version,
                    "approval_decision_id": approval["decision_id"],
                }
            )
            new_entry["revision_history"] = history
            updated_catalog["templates"][existing_index] = new_entry
        else:
            updated_catalog["templates"].append(new_entry)
        atomic_write_json(profile_file, active_profile)
        new_profile_sha256 = sha256_file(profile_file)
        if operation == "upgrade" and new_profile_sha256 == existing_matches[0][1].get("profile_sha256"):
            raise _error(
                "TEMPLATE_UPGRADE_PROFILE_UNCHANGED",
                "升级激活后的画像哈希必须与旧画像不同。",
            )
        new_entry["profile_sha256"] = new_profile_sha256
        if active_profile.get("profile_kind") == "writing":
            from .composition import build_template_fingerprint_manifest

            fingerprint_manifest_path = profile_file.with_name(
                f"{profile_file.stem}.fingerprints.json"
            )
            ensure_not_originals(fingerprint_manifest_path)
            if fingerprint_manifest_path.exists():
                raise _error(
                    "FINGERPRINT_MANIFEST_EXISTS",
                    f"串案指纹清单已存在，拒绝覆盖：{fingerprint_manifest_path}",
                )
            manifest = build_template_fingerprint_manifest(
                template_id,
                active_path,
                profile_id=str(active_profile.get("profile_id") or ""),
                profile_sha256=new_profile_sha256,
                source_sha256=sha256_file(active_path),
                test_only=active_profile.get("test_only") is True,
            )
            atomic_write_json(fingerprint_manifest_path, manifest)
            new_entry.update({
                "fingerprint_manifest_path": os.path.relpath(
                    fingerprint_manifest_path, catalog_file.parent
                ).replace("\\", "/"),
                "fingerprint_manifest_sha256": sha256_file(fingerprint_manifest_path),
                "fingerprint_count": len(manifest["fingerprints"]),
            })
        if operation == "upgrade":
            updated_catalog["templates"][existing_matches[0][0]] = new_entry
        _require_catalog_unchanged(catalog_file, catalog_before_sha256)
        catalog_write_sha256 = _serialized_json_sha256(updated_catalog)
        atomic_write_json(catalog_file, updated_catalog)
        _validate_catalog_or_raise(catalog_file, operation=operation)
    except Exception as exc:
        rollback_state, actual_catalog_sha256 = _catalog_rollback_disposition(
            catalog_file,
            before_sha256=catalog_before_sha256,
            written_sha256=catalog_write_sha256,
        )
        if rollback_state == "diverged":
            _raise_catalog_rollback_conflict(
                catalog_file,
                operation=operation,
                before_sha256=catalog_before_sha256,
                written_sha256=catalog_write_sha256,
                actual_sha256=actual_catalog_sha256,
                cause=exc,
            )
        profile_file.write_bytes(profile_before)
        if prior_profile_path is not None and prior_profile_before is not None:
            prior_profile_path.write_bytes(prior_profile_before)
        if rollback_state == "ours":
            catalog_file.write_bytes(catalog_before)
        active_path.unlink(missing_ok=True)
        if fingerprint_manifest_path is not None:
            fingerprint_manifest_path.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "operation": operation,
        "template_id": template_id,
        "version": resolved_version,
        "status": "active",
        "active_path": str(active_path),
        "profile_path": str(profile_file),
        "catalog_path": str(catalog_file),
        "sha256": sha256_file(active_path),
        "profile_sha256": sha256_file(profile_file),
        "fingerprint_manifest_path": str(fingerprint_manifest_path)
        if fingerprint_manifest_path is not None else None,
        "fingerprint_manifest_sha256": sha256_file(fingerprint_manifest_path)
        if fingerprint_manifest_path is not None else None,
        "previous_version_preserved": operation == "upgrade",
    }
