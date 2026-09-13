"""Small, source-bound DOCX text patches; never reconstruct a user's document.

Coordinates are 1-based body paragraph / top-level table physical cell indices.
Only simple, uniformly formatted text is editable. Unsupported targets remain
inspectable; changing one does not require flattening the rest of the document.
"""
from __future__ import annotations

import copy
import io
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape

from .core import LegalCaseError, ensure_not_originals, now_iso, sha256_bytes, sha256_file
from .documents import W_NS

W = "{" + W_NS + "}"
PART = "word/document.xml"


def _error(code: str, message: str) -> None:
    raise LegalCaseError(code, message)


def _load(source: Path) -> tuple[bytes, bytes, ET.Element]:
    if source.suffix.lower() != ".docx":
        _error("DOCX_REQUIRED", "局部修改只接受 DOCX 文件。")
    try:
        data = source.read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = package.namelist()
            if len(names) != len(set(names)):
                _error("DOCX_AMBIGUOUS_PACKAGE", "DOCX 包含同名部件，无法唯一定位。")
            if any(name.startswith("_xmlsignatures/") for name in names):
                _error("DOCX_SIGNED_PACKAGE", "带数字签名的文档不能通过此入口修改。")
            xml = package.read(PART)
        # Byte-local patching deliberately supports ordinary UTF-8 OOXML only.
        xml.decode("utf-8-sig")
        encoding = re.match(rb"(?:\xef\xbb\xbf)?\s*<\?xml\b[^?]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]", xml, re.I)
        if encoding and encoding.group(1).lower().replace(b"-", b"") != b"utf8":
            _error("DOCX_UNSUPPORTED_XML", "局部字节修改仅支持 UTF-8 编码的 DOCX 正文。")
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            _error("DOCX_UNSUPPORTED_XML", "不支持包含 DTD 或实体声明的文档。")
        root = ET.fromstring(xml)
        if root.tag != W + "document" or len(root.findall(W + "body")) != 1:
            _error("DOCX_UNSUPPORTED_XML", "未找到唯一的标准 Word 正文。")
        return data, xml, root
    except FileNotFoundError as exc:
        raise LegalCaseError("FILE_NOT_FOUND", f"文件不存在：{source}") from exc
    except (zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeError, RuntimeError) as exc:
        if isinstance(exc, LegalCaseError):
            raise
        raise LegalCaseError("INVALID_DOCX", "无法读取标准 UTF-8 DOCX 正文。") from exc


def _text(element: ET.Element) -> str:
    pieces: list[str] = []
    for item in element.iter():
        if item.tag in {W + "t", W + "delText", W + "instrText"}:
            pieces.append(item.text or "")
        elif item.tag == W + "tab":
            pieces.append("\t")
        elif item.tag in {W + "br", W + "cr"}:
            pieces.append("\n")
    return "".join(pieces)


def _paragraph_reason(paragraph: ET.Element) -> str | None:
    complex_names = {"fldChar", "fldSimple", "instrText", "delText", "ins", "del", "moveFrom", "moveTo"}
    for node in paragraph.iter():
        name = node.tag.rsplit("}", 1)[-1]
        if name in complex_names or name.endswith("Change"):
            return "目标含字段或修订，不能安全替换为普通文字。"
    if any(node.tag not in {W + "pPr", W + "r"} for node in paragraph):
        return "目标含超链接、书签、内容控件或其他复杂段落内容。"
    runs = paragraph.findall(W + "r")
    styles = set()
    for run in runs:
        if any(node.tag not in {W + "rPr", W + "t"} for node in run):
            return "目标含换行、制表符、图形或其他非普通文字内容。"
        if len(run.findall(W + "rPr")) > 1:
            return "目标 run 属性不唯一。"
        properties = copy.deepcopy(run.find(W + "rPr"))
        if properties is not None:
            properties.tail = None
        styles.add(ET.tostring(properties, encoding="unicode") if properties is not None else "")
    if len(styles) > 1:
        return "目标包含不同格式的文字；整段替换会丢失富文本格式。"
    return None


def _table_reason(table: ET.Element) -> str | None:
    if any(node is not table for node in table.iter(W + "tbl")):
        return "表格含嵌套表，当前入口不支持其单元格坐标。"
    for node in table.iter():
        if node.tag in {W + "vMerge", W + "hMerge", W + "gridBefore", W + "gridAfter"}:
            return "表格含合并单元格或不规则网格，当前入口不支持其坐标。"
        if node.tag == W + "gridSpan" and node.get(W + "val") != "1":
            return "表格含合并单元格，当前入口不支持其坐标。"
        name = node.tag.rsplit("}", 1)[-1]
        if name in {"ins", "del", "moveFrom", "moveTo"} or name.endswith("Change"):
            return "表格含修订，当前入口不支持其坐标。"
    if any(node.tag not in {W + "tblPr", W + "tblGrid", W + "tr"} for node in table):
        return "表格含包装行或其他复杂内容。"
    if any(node.tag not in {W + "trPr", W + "tblPrEx", W + "tc"} for row in table.findall(W + "tr") for node in row):
        return "表格行含包装单元格或其他复杂内容。"
    return None


def _targets(root: ET.Element) -> tuple[dict[str, Any], dict[tuple, tuple[dict, ET.Element | None]]]:
    body = root.find(W + "body")
    assert body is not None
    inventory: dict[str, Any] = {"coordinate_system": "1-based; body paragraphs and top-level physical table cells", "paragraphs": [], "tables": []}
    lookup: dict[tuple, tuple[dict, ET.Element | None]] = {}
    for number, paragraph in enumerate(body.findall(W + "p"), 1):
        reason = _paragraph_reason(paragraph)
        record = {"target": {"kind": "paragraph", "paragraph": number}, "text": _text(paragraph), "editable": reason is None, "reason": reason}
        inventory["paragraphs"].append(record)
        lookup[("paragraph", number)] = record, paragraph
    for table_number, table in enumerate(body.findall(W + "tbl"), 1):
        table_reason = _table_reason(table)
        table_record = {"table": table_number, "rows": [], "editable": table_reason is None, "reason": table_reason}
        inventory["tables"].append(table_record)
        for row_number, row in enumerate(table.findall(W + "tr"), 1):
            row_record = {"row": row_number, "cells": []}
            table_record["rows"].append(row_record)
            for column, cell in enumerate(row.findall(W + "tc"), 1):
                paragraphs = cell.findall(W + "p")
                reason = table_reason
                if reason is None and (len(paragraphs) != 1 or any(node.tag not in {W + "tcPr", W + "p"} for node in cell)):
                    reason = "单元格含多个段落或复杂内容，当前仅支持一个普通段落。"
                if reason is None:
                    reason = _paragraph_reason(paragraphs[0])
                record = {"target": {"kind": "cell", "table": table_number, "row": row_number, "column": column}, "text": "\n".join(_text(p) for p in paragraphs), "editable": reason is None, "reason": reason}
                row_record["cells"].append(record)
                lookup[("cell", table_number, row_number, column)] = record, paragraphs[0] if len(paragraphs) == 1 else None
    return inventory, lookup


def inspect_docx_targets(source: Path | str) -> dict[str, Any]:
    source = Path(source).resolve()
    data, _xml, root = _load(source)
    inventory, _lookup = _targets(root)
    return {"source": str(source), "source_sha256": sha256_bytes(data), **inventory}


def _target_key(target: Any) -> tuple:
    if not isinstance(target, dict):
        _error("DOCX_TARGET_INVALID", "目标必须使用明确的段落或单元格坐标。")
    kind = target.get("kind")
    if not isinstance(kind, str):
        _error("DOCX_TARGET_INVALID", "目标 kind 必须是 paragraph 或 cell。")
    keys = {"paragraph": ("paragraph",), "cell": ("table", "row", "column")}.get(kind)
    if keys is None or set(target) != {"kind", *keys}:
        _error("DOCX_TARGET_INVALID", "目标坐标不完整或含歧义字段。")
    if any(type(target[key]) is not int or target[key] < 1 for key in keys):
        _error("DOCX_TARGET_INVALID", "目标坐标必须是从 1 开始的整数。")
    return (kind, *(target[key] for key in keys))


def _xml_spans(xml: bytes, root: ET.Element) -> dict[ET.Element, tuple[int, int, int, int]]:
    """Map elements to original byte spans, preserving every non-target byte."""
    parser = expat.ParserCreate(namespace_separator="}")
    elements = iter(root.iter())
    spans: dict[ET.Element, tuple[int, int, int, int]] = {}
    stack: list[tuple[ET.Element, int, int, bool]] = []

    def start(name: str, _attrs: dict) -> None:
        element = next(elements)
        if element.tag != ("{" + name if "}" in name else name):
            _error("DOCX_UNSUPPORTED_XML", "XML 元素无法唯一对齐。")
        begin = parser.CurrentByteIndex
        # Closing brackets inside quoted attributes are not element terminators.
        match = re.match(rb"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", xml[begin:])
        if match is None:
            _error("DOCX_UNSUPPORTED_XML", "无法定位 XML 起始标签。")
        open_end = begin + match.end()
        stack.append((element, begin, open_end, xml[begin:open_end].rstrip().endswith(b"/>")))

    def end(_name: str) -> None:
        element, begin, open_end, empty = stack.pop()
        close_begin = open_end if empty else parser.CurrentByteIndex
        finish = open_end if empty else xml.index(b">", close_begin) + 1
        spans[element] = begin, open_end, close_begin, finish

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(xml, True)
    return spans


def _content_patch(xml: bytes, span: tuple[int, int, int, int], content: bytes, *, preserve: bool = False) -> tuple[int, int, bytes]:
    begin, open_end, close_begin, finish = span
    opening = xml[begin:open_end]
    name = re.match(rb"<([^\s/>]+)", opening).group(1)
    if preserve and not re.search(rb"xml:space\s*=", opening):
        location = -2 if opening.endswith(b"/>") else -1
        opening = opening[:location] + b' xml:space="preserve"' + opening[location:]
    elif preserve:
        opening = re.sub(rb"xml:space\s*=\s*(['\"])[^'\"]*['\"]", b'xml:space="preserve"', opening)
    if opening.endswith(b"/>"):
        opening = opening[:-2] + b">"
    closing = b"</" + name + b">" if close_begin == finish else xml[close_begin:finish]
    return begin, finish, opening + content + closing


def _paragraph_patches(xml: bytes, paragraph: ET.Element, replacement: str, spans: dict) -> list[tuple[int, int, bytes]]:
    text_nodes = list(paragraph.iter(W + "t"))
    encoded = escape(replacement).encode("utf-8")
    if text_nodes:
        return [_content_patch(xml, spans[node], encoded if index == 0 else b"", preserve=index == 0) for index, node in enumerate(text_nodes)]
    runs = paragraph.findall(W + "r")
    container = runs[0] if runs else paragraph
    begin, open_end, close_begin, _finish = spans[container]
    name = re.match(rb"<([^\s/>]+)", xml[begin:open_end]).group(1)
    prefix = name.rsplit(b":", 1)[0] + b":" if b":" in name else b""
    new_text = b"<" + prefix + b't xml:space="preserve">' + encoded + b"</" + prefix + b"t>"
    if not runs:
        new_text = b"<" + prefix + b"r>" + new_text + b"</" + prefix + b"r>"
    return [_content_patch(xml, spans[container], xml[open_end:close_begin] + new_text)]


def _constraint_record(plan: dict, root: ET.Element) -> dict:
    constraints = plan.get("constraints", {})
    if not isinstance(constraints, dict):
        _error("DOCX_PLAN_INVALID", "constraints 必须是对象。")
    body = root.find(W + "body")
    assert body is not None
    full_text = "\n".join(_text(p) for p in body.iter(W + "p"))
    checks = {"kind": "literal_text_only", "semantic_review_required": True}
    for key in ("must_keep", "avoid"):
        values = constraints.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            _error("DOCX_PLAN_INVALID", f"constraints.{key} 必须是非空字符串列表。")
        checks[key] = [{"text": value, "present": value in full_text} for value in values]
    return {"constraints": copy.deepcopy(constraints), "constraint_checks": checks, "parent_manifest": copy.deepcopy(plan.get("parent_manifest"))}


def patch_docx(source: Path | str, output: Path | str, plan: dict[str, Any]) -> dict[str, Any]:
    source = Path(source).resolve()
    output = Path(output)
    if output.is_symlink():
        _error("OUTPUT_EXISTS", "输出路径已存在，不会覆盖。")
    output = output.resolve()
    ensure_not_originals(output)
    if any(part.casefold() == "library" for part in output.parts):
        _error("LIBRARY_READ_ONLY", "禁止向 library 源资料目录写入修改稿。")
    if output == source:
        _error("SOURCE_READ_ONLY", "修改稿必须另存，不能覆盖原稿。")
    if output.suffix.lower() != ".docx":
        _error("DOCX_REQUIRED", "修改稿必须保存为 DOCX。")
    if output.exists():
        _error("OUTPUT_EXISTS", "输出文件已存在，不会覆盖或重复执行。")
    data, xml, root = _load(source)
    digest = sha256_bytes(data)
    if not isinstance(plan, dict) or plan.get("source_sha256") != digest:
        _error("DOCX_SOURCE_STALE", "原稿与修改计划的哈希不一致，请重新检查当前原稿。")
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        _error("DOCX_PLAN_INVALID", "修改计划必须包含明确的 changes 列表。")
    _inventory, lookup = _targets(root)
    selected: list[tuple[ET.Element, str]] = []
    records = []
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"target", "expected_text", "replacement_text"}:
            _error("DOCX_PLAN_INVALID", "每项修改须且仅含 target、expected_text、replacement_text。")
        key = _target_key(change["target"])
        if key in seen:
            _error("DOCX_TARGET_AMBIGUOUS", "同一目标出现多次，未执行任何修改。")
        seen.add(key)
        if key not in lookup:
            _error("DOCX_TARGET_NOT_FOUND", "未找到指定的段落或单元格坐标。")
        record, paragraph = lookup[key]
        if not record["editable"]:
            _error("DOCX_TARGET_UNSUPPORTED", record["reason"])
        if not isinstance(change["expected_text"], str) or change["expected_text"] != record["text"]:
            _error("DOCX_TEXT_MISMATCH", "指定目标的当前文字与预期文字不一致，未执行修改。")
        replacement = change["replacement_text"]
        if not isinstance(replacement, str) or re.search(r"[\x00-\x1f\ud800-\udfff\ufffe\uffff]", replacement):
            _error("DOCX_TEXT_UNSUPPORTED", "替换文字须为单段普通文字，不支持换行、制表符或非法 XML 字符。")
        assert paragraph is not None
        selected.append((paragraph, replacement))
        records.append(copy.deepcopy(change))
    spans = _xml_spans(xml, root)
    patches = [patch for paragraph, replacement in selected for patch in _paragraph_patches(xml, paragraph, replacement, spans)]
    updated_xml = xml
    for begin, finish, replacement in sorted(patches, reverse=True):
        updated_xml = updated_xml[:begin] + replacement + updated_xml[finish:]
    updated_root = ET.fromstring(updated_xml)
    updated_lookup = _targets(updated_root)[1]
    for record in records:
        if updated_lookup[_target_key(record["target"])][0]["text"] != record["replacement_text"]:
            _error("DOCX_PATCH_VERIFY_FAILED", "修改后的目标文字校验失败，未发布文件。")
    inherited = _constraint_record(plan, updated_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".docx-patch-", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(handle, "w") as result:
                result.comment = original.comment
                for info in original.infolist():
                    result.writestr(info, updated_xml if info.filename == PART else original.read(info.filename))
            handle.flush()
            os.fsync(handle.fileno())
        output_sha256 = sha256_file(temporary)
        if sha256_file(source) != digest:
            _error("DOCX_SOURCE_STALE", "发布前发现原稿发生变化，已停止修改。")
        # A hard-link publication is atomic and refuses a concurrently-created
        # destination. Temp and output share the same filesystem by construction.
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise LegalCaseError("OUTPUT_EXISTS", "输出文件已存在，未覆盖已有成果。") from exc
        except OSError as exc:
            raise LegalCaseError("DOCX_PUBLISH_FAILED", "无法安全地发布修改稿，未覆盖任何文件。") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"kind": "docx-local-patch", "created_at": now_iso(), "source": str(source), "source_sha256": digest, "output": str(output), "docx": str(output), "output_sha256": output_sha256, "changes": records, "changed_parts": [PART], "preservation": "All non-target document XML bytes and all other ZIP part bytes preserved.", "visual_review_status": "not_run", **inherited}
