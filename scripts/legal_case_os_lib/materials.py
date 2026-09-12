"""Immutable task-local material snapshots; no database, MCP, OCR or network required.

Offsets in extracted text are Python Unicode character offsets (end-exclusive).
PDF page locators are physical, one-based pages. DOCX locators are OOXML paths,
not invented rendered page numbers. A valid record can have explicitly incomplete
text coverage; snapshot integrity never implies legal or OCR verification.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from .core import LegalCaseError, canonical_json, ensure_not_originals, sha256_file
from .documents import _try_pdf_backend

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SUPPORTED = {".docx", ".md", ".markdown", ".txt", ".pdf"}
MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
}


def _fail(code: str, message: str) -> LegalCaseError:
    return LegalCaseError(code, message)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record_digest(record: dict[str, Any]) -> str:
    return _digest(canonical_json({k: v for k, v in record.items() if k != "record_sha256"}).encode("utf-8"))


def _output_root(output_dir: Path) -> Path:
    root = Path(output_dir).resolve()
    ensure_not_originals(root)
    if any(part.casefold() == "library" for part in root.parts):
        raise _fail("MATERIAL_OUTPUT_IN_LIBRARY", "材料快照不能写入正式 library；请选择本次任务的输出目录。")
    if root.exists() and not root.is_dir():
        raise _fail("MATERIAL_OUTPUT_NOT_DIRECTORY", "材料输出路径必须是目录。")
    return root


def _paragraph_text(node: ET.Element) -> str:
    pieces: list[str] = []
    def visit(item: ET.Element) -> None:
        if item is not node and item.tag == W + "p":
            return
        if item.tag == W + "t":
            pieces.append(item.text or "")
        elif item.tag == W + "tab":
            pieces.append("\t")
        elif item.tag in {W + "br", W + "cr"}:
            pieces.append("\n")
        else:
            for child in item:
                visit(child)
    visit(node)
    return "".join(pieces)


def _extract(data: bytes, suffix: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    text_parts: list[str] = []
    segments: list[dict[str, Any]] = []
    offset = 0
    gaps: list[dict[str, Any]] = []
    warnings: list[str] = []
    physical_pages: int | None = None
    pages_with_text: int | None = None
    backend = "stdlib"
    status = "text_extracted"

    def append(text: str, locator: dict[str, Any]) -> None:
        nonlocal offset
        if not text:
            return
        if text_parts:
            text_parts.append("\n")
            offset += 1
        start = offset
        text_parts.append(text)
        offset += len(text)
        segments.append({"char_start": start, "char_end": offset,
                         "text_sha256": _digest(text.encode("utf-8")),
                         "source_locator": locator})

    if suffix in {".txt", ".md", ".markdown"}:
        try:
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                raw = data.decode("utf-16")
                backend = "utf-16-bom"
            else:
                raw = data.decode("utf-8-sig")
                backend = "utf-8"
        except UnicodeDecodeError as exc:
            raise _fail("MATERIAL_TEXT_ENCODING_UNSUPPORTED", "文字文件须为 UTF-8 或带 BOM 的 UTF-16；未猜测编码或丢弃乱码。") from exc
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        # Keep the exact normalized text, including blank lines and final newline.
        cursor = 0
        for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
            end = cursor + len(line)
            segments.append({"char_start": cursor, "char_end": end,
                             "text_sha256": _digest(line.encode("utf-8")),
                             "source_locator": {"kind": "text_line", "line_start": line_number,
                                                "line_end": line_number, "physical_page": None}})
            cursor = end
        text_parts = [raw] if raw else []
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                names = package.namelist()
                if names.count("word/document.xml") != 1:
                    raise ValueError("missing or duplicate document.xml")
                part = package.getinfo("word/document.xml")
                if part.file_size > 64 * 1024 * 1024:
                    raise ValueError("document.xml exceeds extraction limit")
                xml = package.read(part)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise ValueError("DTD/entity declarations are not supported")
                document = ET.fromstring(xml)
                body = document.find(W + "body")
                if body is None:
                    raise ValueError("missing document body")
                count = 0
                def walk(node: ET.Element, path: str, context: dict[str, int]) -> None:
                    nonlocal count
                    if node.tag == W + "p":
                        count += 1
                        append(_paragraph_text(node), {"kind": "docx_paragraph", "part": "word/document.xml",
                               "xml_path": path, "paragraph": count, "physical_page": None, **context})
                    counters: dict[str, int] = {}
                    for child in node:
                        tag = child.tag.rsplit("}", 1)[-1]
                        counters[tag] = counters.get(tag, 0) + 1
                        next_context = dict(context)
                        if tag in {"tbl", "tr", "tc"}:
                            next_context[{"tbl": "table", "tr": "row", "tc": "cell"}[tag]] = counters[tag]
                        walk(child, f"{path}/{tag}[{counters[tag]}]", next_context)
                walk(body, "/document/body[1]", {})
                images = sum(1 for name in names if name.startswith("word/media/") and not name.endswith("/"))
                if images:
                    gaps.append({"kind": "embedded_images_not_ocr", "physical_page": None, "object_count": images})
                warnings.append("DOCX 提取正文和表格；物理页须经渲染确定，页眉页脚及图片内容未作为正文核验。")
        except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, RuntimeError) as exc:
            raise _fail("MATERIAL_DOCX_INVALID", "DOCX 包损坏或不受支持，未产生可用文字记录。") from exc
    elif suffix == ".pdf":
        if not data.lstrip().startswith(b"%PDF-"):
            raise _fail("MATERIAL_PDF_INVALID", "输入没有真实 PDF 文件签名。")
        pdf_backend, backend_name = _try_pdf_backend()
        if pdf_backend is None:
            backend = "unavailable"
            status = "extraction_unavailable"
            gaps.append({"kind": "pdf_text_backend_unavailable", "physical_page": None})
            warnings.append("未安装可用的 pypdf/PyPDF2；原件已保存，未伪造正文或页数。")
        else:
            backend = str(backend_name)
            try:
                reader = pdf_backend[0](io.BytesIO(data))
                if reader.is_encrypted and not reader.decrypt(""):
                    raise _fail("MATERIAL_PDF_ENCRYPTED", "PDF 需要密码，未伪造可读正文。")
                physical_pages = len(reader.pages)
                pages_with_text = 0
                for page_number, page in enumerate(reader.pages, 1):
                    value = page.extract_text() or ""
                    if value.strip():
                        pages_with_text += 1
                        append(value, {"kind": "pdf_page", "physical_page": page_number})
                    else:
                        gaps.append({"kind": "no_text_layer_needs_visual_or_ocr", "physical_page": page_number})
                warnings.append("PDF 仅提取文字层；无文字页可能是扫描页或空白页，未运行 OCR，图片及文字层准确性未人工核验。")
            except LegalCaseError:
                raise
            except Exception as exc:
                raise _fail("MATERIAL_PDF_INVALID", "PDF 解析失败，未产生可用文字记录。") from exc
    else:
        raise _fail("MATERIAL_TYPE_UNSUPPORTED", "本地材料支持 DOCX、Markdown、TXT、PDF；其他格式须先产生可核验派生件。")

    text = "".join(text_parts)
    if status == "text_extracted" and not text.strip():
        status = "no_text"
    coverage = {"status": status, "characters": len(text), "segments": len(segments),
                "physical_pages": physical_pages, "pages_with_text": pages_with_text,
                "ocr_performed": False, "ocr_gaps": gaps,
                "scope": "body_and_tables" if suffix == ".docx" else "text_layer" if suffix == ".pdf" else "whole_text_file"}
    quality = {"extraction_backend": backend, "text_available": bool(text.strip()),
               "source_integrity": "sha256_verified", "human_verified": False,
               "legal_authority_verified": False, "warnings": warnings,
               "needs_visual_or_ocr": bool(gaps), "format_fidelity_verified": False}
    return text, segments, coverage, quality


def _persist(data: bytes, suffix: str, output_dir: Path, role: str, origin: str,
             provenance: dict[str, Any] | None) -> dict[str, Any]:
    root = _output_root(output_dir)
    if not isinstance(role, str) or not role.strip() or not isinstance(origin, str) or not origin.strip():
        raise _fail("MATERIAL_IDENTITY_INVALID", "材料 role 和 origin 必须是非空字符串。")
    if provenance is not None and not isinstance(provenance, dict):
        raise _fail("MATERIAL_PROVENANCE_INVALID", "provenance 必须是对象。")
    context = {"origin": origin, "role": role, "provenance": copy.deepcopy(provenance or {}), "file_type": suffix.lstrip(".")}
    try:
        context_hash = _digest(canonical_json(context).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise _fail("MATERIAL_PROVENANCE_INVALID", "provenance 必须能以 JSON 完整保存。") from exc
    source_hash = _digest(data)
    text, segments, coverage, quality = _extract(data, suffix)
    text_data = text.encode("utf-8")
    extraction_hash = _digest(canonical_json({"text_sha256": _digest(text_data), "segments": segments,
                                             "coverage": coverage, "quality": quality}).encode("utf-8"))
    identity = f"{source_hash}-{context_hash[:16]}-{extraction_hash[:16]}"
    final = root / "materials" / identity
    record: dict[str, Any] = {"schema_version": "1.0.0", "id": f"MAT-{identity}", **context,
        "source_sha256": source_hash, "source_size": len(data), "text_sha256": _digest(text_data),
        "snapshot_path": str(final / ("source" + suffix)), "text_path": str(final / "text.txt"),
        "record_path": str(final / "record.json"), "segments": segments,
        "coverage": coverage, "quality": quality,
        "persistence_scope": "task_only", "template_activation": False}
    record["record_sha256"] = _record_digest(record)
    if final.exists():
        try:
            existing = json.loads((final / "record.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _fail("MATERIAL_OUTPUT_EXISTS_INVALID", "已有材料快照不完整，拒绝覆盖。") from exc
        errors = validate_material(existing)
        if errors or existing != record:
            raise LegalCaseError("MATERIAL_OUTPUT_EXISTS_INVALID", "已有材料快照不同或已损坏，拒绝覆盖。", {"errors": errors})
        return existing
    final.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".material-", dir=final.parent)).resolve()
    try:
        (stage / ("source" + suffix)).write_bytes(data)
        (stage / "text.txt").write_bytes(text_data)
        (stage / "record.json").write_text(canonical_json(record) + "\n", encoding="utf-8")
        if sha256_file(stage / ("source" + suffix)) != source_hash or sha256_file(stage / "text.txt") != record["text_sha256"]:
            raise _fail("MATERIAL_WRITE_HASH_MISMATCH", "快照写入校验失败。")
        if json.loads((stage / "record.json").read_text(encoding="utf-8")) != record:
            raise _fail("MATERIAL_WRITE_RECORD_MISMATCH", "材料记录写入校验失败。")
        try:
            os.rename(stage, final)
        except OSError:
            # A concurrent identical ingest can finish first; never overwrite it.
            if not final.is_dir():
                raise
            existing = json.loads((final / "record.json").read_text(encoding="utf-8"))
            if existing != record or validate_material(existing):
                raise _fail("MATERIAL_OUTPUT_EXISTS_INVALID", "并发生成的快照与本次内容不一致。")
        return record
    finally:
        if stage.exists() and stage.parent == final.parent.resolve() and stage.name.startswith(".material-"):
            shutil.rmtree(stage)


def ingest_material(path: Path, output_dir: Path, role: str, origin: str = "session_upload",
                    provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read one explicitly selected file and persist a task-only immutable snapshot."""
    source = Path(path).resolve()
    _output_root(output_dir)
    if not source.is_file():
        raise _fail("MATERIAL_SOURCE_NOT_FOUND", "指定材料文件不存在。")
    suffix = source.suffix.casefold()
    if suffix not in SUPPORTED:
        raise _fail("MATERIAL_TYPE_UNSUPPORTED", "本地材料支持 DOCX、Markdown、TXT、PDF。")
    before = sha256_file(source)
    data = source.read_bytes()
    if _digest(data) != before or sha256_file(source) != before:
        raise _fail("MATERIAL_SOURCE_CHANGED", "读取期间原件发生变化，请重新读取当前版本。")
    if provenance is not None and not isinstance(provenance, dict):
        raise _fail("MATERIAL_PROVENANCE_INVALID", "provenance 必须是对象。")
    local_provenance = copy.deepcopy(provenance or {})
    local_provenance["local_source_path"] = str(source)
    local_provenance["local_source_sha256"] = before
    return _persist(data, suffix, output_dir, role, origin, local_provenance)


def import_original_parts(parts: list[dict[str, Any]], output_dir: Path, role: str) -> dict[str, Any]:
    """Consume complete, ordered read_original MCP chunks; never trust supplied text."""
    _output_root(output_dir)
    if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) for p in parts):
        raise _fail("MATERIAL_PARTS_INVALID", "原件分块必须是非空对象列表。")
    first = parts[0]
    identity_fields = ("schema_version", "document_id", "version_id", "total_bytes", "source_sha256", "file_type", "mime_type")
    for key in ("document_id", "version_id", "source_sha256"):
        if not isinstance(first.get(key), str) or not first[key]:
            raise _fail("MATERIAL_PART_IDENTITY_INVALID", f"原件分块缺少 {key}。")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", first["source_sha256"]):
        raise _fail("MATERIAL_PART_HASH_INVALID", "原件 SHA256 格式无效。")
    total = first.get("total_bytes")
    if type(total) is not int or total < 0:
        raise _fail("MATERIAL_PART_SIZE_INVALID", "total_bytes 必须是非负整数。")
    position = 0
    chunks: list[bytes] = []
    for index, part in enumerate(parts):
        if any(part.get(key) != first.get(key) for key in identity_fields):
            raise _fail("MATERIAL_PART_VERSION_DRIFT", "原件分块的对象、版本、类型、长度或哈希发生漂移。")
        if type(part.get("offset")) is not int or part["offset"] != position:
            raise _fail("MATERIAL_PART_ORDER_INVALID", "原件分块缺失、重复或乱序；必须从 offset=0 连续读取。")
        if part.get("encoding") != "base64" or not isinstance(part.get("data_base64"), str):
            raise _fail("MATERIAL_PART_ENCODING_INVALID", "原件分块必须使用 base64 编码。")
        try:
            chunk = base64.b64decode(part["data_base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _fail("MATERIAL_PART_ENCODING_INVALID", "base64 分块损坏。") from exc
        if type(part.get("byte_length")) is not int or part["byte_length"] != len(chunk):
            raise _fail("MATERIAL_PART_SIZE_INVALID", "分块 byte_length 与真实字节数不一致。")
        if not isinstance(part.get("segment_sha256"), str) or _digest(chunk) != part["segment_sha256"].lower():
            raise _fail("MATERIAL_PART_HASH_MISMATCH", "原件分块 SHA256 不匹配。")
        position += len(chunk)
        if position > total:
            raise _fail("MATERIAL_PART_SIZE_INVALID", "分块超出声明的原件总长度。")
        terminal = index == len(parts) - 1
        if "next_offset" not in part or (terminal and (part["next_offset"] is not None or position != total)):
            raise _fail("MATERIAL_PART_INCOMPLETE", "最后分块必须覆盖原件结尾且 next_offset=null。")
        if not terminal and (not chunk or type(part["next_offset"]) is not int or part["next_offset"] != position):
            raise _fail("MATERIAL_PART_ORDER_INVALID", "中间分块必须非空且 next_offset 指向下一连续字节。")
        chunks.append(chunk)
    data = b"".join(chunks)
    if _digest(data) != first["source_sha256"].lower():
        raise _fail("MATERIAL_SOURCE_HASH_MISMATCH", "重建原件的完整 SHA256 不匹配。")
    suffix = "." + str(first.get("file_type") or "").lower().lstrip(".")
    if suffix not in SUPPORTED:
        suffix = MIMES.get(str(first.get("mime_type") or ""), "")
    if suffix not in SUPPORTED:
        raise _fail("MATERIAL_TYPE_UNSUPPORTED", "原件格式不能由当前离线后端提取。")
    provenance = {"provider": "law_library", "document_id": first["document_id"],
                  "version_id": first["version_id"], "source_sha256": first["source_sha256"].lower(),
                  "title": first.get("title"), "mime_type": first.get("mime_type"),
                  "read_protocol": "read_original", "verified_part_count": len(parts)}
    return _persist(data, suffix, output_dir, role, "law_library", provenance)


def validate_material(record: dict[str, Any]) -> list[str]:
    """Validate record metadata and available immutable bytes; no source service access."""
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["material_record_not_object"]
    required = ("id", "origin", "role", "source_sha256", "text_sha256", "snapshot_path", "text_path",
                "record_path", "record_sha256", "segments", "coverage", "quality", "provenance")
    if any(key not in record for key in required):
        return ["material_required_fields_missing"]
    try:
        if _record_digest(record) != record["record_sha256"]:
            errors.append("material_record_hash_mismatch")
        snapshot = Path(record["snapshot_path"])
        text_path = Path(record["text_path"])
        record_path = Path(record["record_path"])
        if not all(p.is_absolute() for p in (snapshot, text_path, record_path)):
            errors.append("material_paths_not_absolute")
        if not snapshot.is_file() or sha256_file(snapshot) != record["source_sha256"]:
            errors.append("material_source_hash_mismatch_or_missing")
        if not text_path.is_file() or sha256_file(text_path) != record["text_sha256"]:
            errors.append("material_text_hash_mismatch_or_missing")
        if not record_path.is_file() or json.loads(record_path.read_text(encoding="utf-8")) != record:
            errors.append("material_record_file_mismatch_or_missing")
        if not errors:
            text = text_path.read_bytes().decode("utf-8")
            if len(text) != record["coverage"].get("characters"):
                errors.append("material_character_count_mismatch")
            prior = 0
            for segment in record["segments"]:
                start, end = segment["char_start"], segment["char_end"]
                if type(start) is not int or type(end) is not int or not prior <= start < end <= len(text):
                    errors.append("material_segment_range_invalid")
                    break
                if _digest(text[start:end].encode("utf-8")) != segment["text_sha256"]:
                    errors.append("material_segment_hash_mismatch")
                    break
                prior = end
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        errors.append("material_record_invalid_or_unreadable")
    return errors


def read_material(record: dict[str, Any], offset: int = 0, limit: int = 12000) -> dict[str, Any]:
    """Read verified extracted text with the intersecting exact original locators."""
    errors = validate_material(record)
    if errors:
        raise LegalCaseError("MATERIAL_VALIDATION_FAILED", "材料快照校验失败。", {"errors": errors})
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 1_000_000:
        raise _fail("MATERIAL_READ_RANGE_INVALID", "offset 须为非负字符偏移，limit 须在 1..1000000。")
    text = Path(record["text_path"]).read_bytes().decode("utf-8")
    if _digest(text.encode("utf-8")) != record["text_sha256"]:
        raise _fail("MATERIAL_TEXT_CHANGED", "读取期间提取文字发生变化。")
    if offset > len(text):
        raise _fail("MATERIAL_READ_RANGE_INVALID", "offset 超出提取文字长度。")
    end = min(len(text), offset + limit)
    return {"id": record["id"], "text": text[offset:end], "offset": offset,
            "next_offset": end if end < len(text) else None, "total_characters": len(text),
            "source_sha256": record["source_sha256"], "text_sha256": record["text_sha256"],
            "segments": [copy.deepcopy(s) for s in record["segments"] if s["char_start"] < end and s["char_end"] > offset],
            "coverage": copy.deepcopy(record["coverage"]), "quality": copy.deepcopy(record["quality"])}
