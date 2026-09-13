"""Explicit, offline page mapping; original PDFs and catalog remain read only."""
from __future__ import annotations

import importlib
import io
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .core import LegalCaseError, atomic_write_json, canonical_json, now_iso, sha256_bytes, sha256_file
from .documents import _try_page_number_backend, _try_pdf_backend
from .docx_edit import inspect_docx_targets, patch_docx


IDENTITY = "legal-case-os/evidence-pages/v1"
FOOTER_HEIGHT = 28


def _fail(code: str, message: str) -> None:
    raise LegalCaseError(code, message)


def _keys(value: Any, required: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != required:
        _fail("INVALID_EVIDENCE_PLAN", f"{label} 必须明确且仅包含：{', '.join(sorted(required))}。")


def _source(spec: Any, suffix: str, label: str) -> tuple[Path, str, bytes]:
    _keys(spec, {"path", "sha256"}, label)
    if not isinstance(spec["path"], str) or not Path(spec["path"]).is_absolute():
        _fail("INVALID_EVIDENCE_SOURCE", f"{label} 必须提供明确的绝对路径。")
    path = Path(spec["path"]).resolve()
    digest = spec["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        _fail("INVALID_EVIDENCE_SOURCE", f"{label} 缺少有效的 SHA256。")
    if not path.is_file() or path.suffix.lower() != suffix:
        _fail("INVALID_EVIDENCE_SOURCE", f"{label} 必须是存在的 {suffix} 文件：{path}")
    data = path.read_bytes()
    if sha256_bytes(data) != digest.lower():
        _fail("EVIDENCE_SOURCE_CHANGED", f"{label} 已变化；请重新核对来源和页段后更新计划：{path}")
    return path, digest.lower(), data


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _recheck_sources(sources: dict[Path, str]) -> None:
    for path, digest in sources.items():
        if not path.is_file() or sha256_file(path) != digest:
            _fail("EVIDENCE_SOURCE_CHANGED", f"制作期间来源发生变化，未发布成品：{path}")


def _read_pdf(data: bytes, path: Path, PdfReader: Any) -> Any:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            _fail("ENCRYPTED_EVIDENCE_PDF", f"加密 PDF 暂不支持：{path}")
        if "/OCProperties" in reader.trailer["/Root"]:
            _fail("UNSUPPORTED_EVIDENCE_PAGE", "来源 PDF 含可选内容图层；当前页合并不能保留图层显隐设置，可能显示原先隐藏的内容，未生成派生件。")
        if reader.metadata and reader.metadata.get("/LegalCaseEvidencePages"):
            _fail("EVIDENCE_ALREADY_NUMBERED", f"该 PDF 是本工具的已编号派生件；请提供原始来源：{path}")
        for page in reader.pages:
            if page.get("/LegalCaseEvidencePage") or page.get("/LegalCaseSourceSHA256"):
                _fail("EVIDENCE_ALREADY_NUMBERED", f"该 PDF 含本工具的已编号页；请提供原始来源：{path}")
        return reader
    except LegalCaseError:
        raise
    except Exception as exc:
        raise LegalCaseError("INVALID_EVIDENCE_PDF", f"无法读取 PDF：{path}") from exc


def _geometry(page: Any) -> tuple[list[float], list[float], int, tuple[float, float]]:
    crop = [float(v) for v in page.cropbox]
    media = [float(v) for v in page.mediabox]
    raw_rotation = float(page.get("/Rotate", 0))
    rotation = int(raw_rotation) % 360 if math.isfinite(raw_rotation) else -1
    if (not all(math.isfinite(v) for v in crop + media) or not math.isfinite(raw_rotation) or
            crop[2] <= crop[0] or crop[3] <= crop[1] or raw_rotation != int(raw_rotation) or
            rotation not in {0, 90, 180, 270} or
            not (media[0] <= crop[0] < crop[2] <= media[2] and media[1] <= crop[1] < crop[3] <= media[3]) or
            float(page.get("/UserUnit", 1)) != 1):
        _fail("UNSUPPORTED_EVIDENCE_PAGE", "页框、旋转或 UserUnit 不受支持，未生成派生件。")
    # Keep the visible crop intact and add space at its displayed bottom.
    expanded = crop.copy()
    side, sign = {0: (1, -1), 90: (2, 1), 180: (3, 1), 270: (0, -1)}[rotation]
    expanded[side] += sign * FOOTER_HEIGHT
    center = [(crop[0] + crop[2]) / 2, (crop[1] + crop[3]) / 2]
    center[side % 2] = crop[side] + sign * FOOTER_HEIGHT / 2
    expanded_media = [min(media[0], expanded[0]), min(media[1], expanded[1]),
                      max(media[2], expanded[2]), max(media[3], expanded[3])]
    return expanded, expanded_media, rotation, tuple(center)


def _build_pdf(path: Path, entries: list[dict[str, Any]], PdfReader: Any, PdfWriter: Any,
               canvas: Any, generic: Any, mapping_hash: str) -> None:
    writer = PdfWriter()
    total = sum(len(item["pages"]) for item in entries)
    sequence = 0
    for item in entries:
        start = sequence
        for physical in item["pages"]:
            source_page = item["reader"].pages[physical - 1]
            crop, media, rotation, center = _geometry(source_page)
            page = writer.add_blank_page(width=media[2] - media[0], height=media[3] - media[1])
            page.mediabox = generic.RectangleObject(media)
            page.cropbox = generic.RectangleObject(crop)
            page[generic.NameObject("/Rotate")] = generic.NumberObject(rotation)
            # merge_page clips source content to its original cropbox. Merely
            # enlarging the original crop could reveal previously hidden text.
            page.merge_page(source_page, expand=False)
            overlay_data = io.BytesIO()
            overlay = canvas.Canvas(overlay_data, pagesize=(media[2] - media[0], media[3] - media[1]))
            overlay.translate(*center)
            overlay.rotate(rotation)
            overlay.setFont("Helvetica", 9)
            overlay.drawCentredString(0, -3, f"{sequence + 1} / {total}")
            overlay.save()
            footer = PdfReader(io.BytesIO(overlay_data.getvalue())).pages[0]
            footer.mediabox = generic.RectangleObject(media)
            footer.cropbox = generic.RectangleObject(media)
            page.merge_page(footer, expand=False)
            if "/Annots" in page and not page["/Annots"]:
                del page["/Annots"]
            page[generic.NameObject("/LegalCaseEvidencePage")] = generic.TextStringObject(IDENTITY)
            page[generic.NameObject("/LegalCaseSourceSHA256")] = generic.TextStringObject(item["source_sha256"])
            sequence += 1
        bookmark = f'{item["id"]} {item["name"]}'
        add_outline = getattr(writer, "add_outline_item", None) or getattr(writer, "addBookmark", None)
        if add_outline is None:
            _fail("PDF_BACKEND_UNAVAILABLE", "PDF 后端不支持书签；未生成半成品。")
        add_outline(bookmark, start)
    writer.add_metadata({"/LegalCaseEvidencePages": IDENTITY, "/LegalCasePageMapSHA256": mapping_hash})
    with path.open("xb") as handle:
        writer.write(handle)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_pdf(path: Path, entries: list[dict[str, Any]], PdfReader: Any, mapping_hash: str) -> None:
    reader = PdfReader(str(path))
    total = sum(len(item["pages"]) for item in entries)
    if len(reader.pages) != total or reader.metadata.get("/LegalCasePageMapSHA256") != mapping_hash:
        _fail("EVIDENCE_PDF_VERIFICATION_FAILED", "成品页数或页码映射标识不一致。")
    outlines = getattr(reader, "outline", None)
    if outlines is None:
        outlines = reader.outlines
    expected_bookmarks = [(f'{item["id"]} {item["name"]}', item["output_start"] - 1) for item in entries]
    actual_bookmarks = [(str(bookmark.title), reader.get_destination_page_number(bookmark)) for bookmark in outlines]
    if actual_bookmarks != expected_bookmarks:
        _fail("EVIDENCE_PDF_VERIFICATION_FAILED", "书签与页码映射不一致。")
    sequence = 0
    for item in entries:
        for physical in item["pages"]:
            page = reader.pages[sequence]
            crop, media, rotation, _ = _geometry(item["reader"].pages[physical - 1])
            if ([float(v) for v in page.cropbox] != crop or [float(v) for v in page.mediabox] != media or
                    int(page.get("/Rotate", 0)) != rotation or
                    page.get("/LegalCaseSourceSHA256") != item["source_sha256"] or
                    f"{sequence + 1} / {total}" not in (page.extract_text() or "")):
                _fail("EVIDENCE_PDF_VERIFICATION_FAILED", f"第 {sequence + 1} 页页码、来源或版面几何复核失败。")
            sequence += 1


def build_evidence_pages(plan: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Build PDF + locally patched DOCX from explicit 1-based physical ranges.

    plan = {catalog: {path, sha256}, items: [{id, name, source: {path, sha256},
      page_ranges: [[start, end], ...], catalog_cell: {table, row, column, expected_text}}]}
    Duplicate names are allowed: identity and cell coordinates are mandatory.
    No case state, original bundle, or review/filing approval is changed.
    """
    _keys(plan, {"catalog", "items"}, "页码计划")
    output_dir = Path(output_dir).absolute()
    if any(part.casefold() in {"library", "00-originals"} for part in output_dir.resolve().parts):
        _fail("ORIGINALS_READ_ONLY", "派生件不能写入 library 或 00-originals。")
    if os.path.lexists(output_dir):
        _fail("OUTPUT_EXISTS", "输出目录已存在；请选择新的任务目录，禁止覆盖。")
    catalog, catalog_hash, _ = _source(plan["catalog"], ".docx", "原证据清单")
    if not isinstance(plan["items"], list) or not plan["items"]:
        _fail("INVALID_EVIDENCE_PLAN", "必须明确列出至少一项证据及其页段和清单格子。")
    backend, backend_name = _try_pdf_backend()
    canvas, _ = _try_page_number_backend()
    if backend is None or canvas is None:
        _fail("PDF_BACKEND_UNAVAILABLE", "缺少 pypdf/reportlab PDF 处理依赖，未生成文件。")
    PdfReader, PdfWriter = backend
    generic = importlib.import_module(("pypdf" if "pypdf" in backend_name else "PyPDF2") + ".generic")
    inspected = inspect_docx_targets(catalog)
    cells = {(table["table"], row["row"], cell["target"]["column"]): cell
             for table in inspected["tables"] for row in table["rows"] for cell in row["cells"]}
    if inspected["source_sha256"] != catalog_hash:
        _fail("EVIDENCE_SOURCE_CHANGED", "清单已变化；请重新定位页码格子。")
    sources, readers, ids, locators, used_pages = {catalog: catalog_hash}, {}, set(), set(), set()
    entries, changes, page_map = [], [], []
    sequence = 1
    for item in plan["items"]:
        _keys(item, {"id", "name", "source", "page_ranges", "catalog_cell"}, "证据项")
        if any(not isinstance(item[k], str) or not item[k].strip() or "\n" in item[k] or "\r" in item[k]
               for k in ("id", "name")):
            _fail("INVALID_EVIDENCE_PLAN", "每项证据必须有非空、单行的明确 id 和名称。")
        if item["id"] in ids:
            _fail("DUPLICATE_EVIDENCE_ID", f'证据 id 重复：{item["id"]}')
        ids.add(item["id"])
        location = item["catalog_cell"]
        _keys(location, {"table", "row", "column", "expected_text"}, "清单页码格子")
        key = tuple(location[k] for k in ("table", "row", "column"))
        if not all(_positive_int(v) for v in key) or not isinstance(location["expected_text"], str):
            _fail("INVALID_CATALOG_MAPPING", "清单定位必须是从 1 开始的表、行、列及原格子文字。")
        if key in locators:
            _fail("DUPLICATE_CATALOG_MAPPING", "多项证据指向同一个清单格子；请明确拆分映射。")
        locators.add(key)
        target = cells.get(key)
        if not target or not target["editable"] or target["text"] != location["expected_text"]:
            _fail("INVALID_CATALOG_MAPPING", f"清单页码格子 {key} 不存在、不可局部编辑或原文字不符。")
        source, digest, data = _source(item["source"], ".pdf", f'证据 {item["id"]}')
        if source in sources and sources[source] != digest:
            _fail("EVIDENCE_SOURCE_CHANGED", f"同一来源出现不同版本：{source}")
        sources[source] = digest
        if digest not in readers:
            readers[digest] = _read_pdf(data, source, PdfReader)
        reader = readers[digest]
        ranges = item["page_ranges"]
        if not isinstance(ranges, list) or not ranges:
            _fail("INVALID_EVIDENCE_PAGE_RANGE", f'证据 {item["id"]} 未提供明确物理页段。')
        pages, geometries = [], []
        for span in ranges:
            if (not isinstance(span, list) or len(span) != 2 or not all(_positive_int(v) for v in span) or
                    span[0] > span[1] or span[1] > len(reader.pages)):
                _fail("INVALID_EVIDENCE_PAGE_RANGE", f'证据 {item["id"]} 的物理页段无效或超出 PDF 页数。')
            for physical in range(span[0], span[1] + 1):
                if (digest, physical) in used_pages:
                    _fail("DUPLICATE_EVIDENCE_PAGE", "同一版本的同一物理页被重复选中，未重复编号。")
                used_pages.add((digest, physical))
                page = reader.pages[physical - 1]
                if "/Group" in page:
                    _fail("UNSUPPORTED_EVIDENCE_PAGE", "选中页含页面透明组；当前页合并不能可靠保留其混合色空间和显示效果，未生成派生件。")
                if page.get("/Annots"):
                    _fail("UNSUPPORTED_EVIDENCE_PAGE", "选中页含批注或交互表单；当前工具不能可靠保留这些对象，请先核对来源。")
                expanded_crop, expanded_media, rotation, _ = _geometry(page)
                geometries.append({"physical_page": physical, "source_cropbox": [float(v) for v in page.cropbox],
                                   "source_mediabox": [float(v) for v in page.mediabox], "rotation": rotation,
                                   "output_cropbox": expanded_crop, "output_mediabox": expanded_media})
                pages.append(physical)
        start, end = sequence, sequence + len(pages) - 1
        page_text = str(start) if start == end else f"{start}-{end}"
        changes.append({"target": target["target"], "expected_text": location["expected_text"], "replacement_text": page_text})
        record = {"id": item["id"], "name": item["name"], "source_path": str(source), "source_sha256": digest,
                  "physical_pages": pages, "output_start": start, "output_end": end, "catalog_page_text": page_text,
                  "catalog_cell": location, "bookmark": f'{item["id"]} {item["name"]}', "page_geometry": geometries}
        page_map.append(record)
        entries.append({**record, "reader": reader, "pages": pages})
        sequence = end + 1
    mapping_hash = sha256_bytes(canonical_json(page_map).encode("utf-8"))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=str(output_dir.parent))).resolve()
    try:
        _build_pdf(staging / "evidence-numbered.pdf", entries, PdfReader, PdfWriter, canvas, generic, mapping_hash)
        _verify_pdf(staging / "evidence-numbered.pdf", entries, PdfReader, mapping_hash)
        patch_report = patch_docx(catalog, staging / "catalog-filled.docx", {"source_sha256": catalog_hash, "changes": changes})
        after = inspect_docx_targets(staging / "catalog-filled.docx")
        after_cells = {(t["table"], r["row"], c["target"]["column"]): c["text"]
                       for t in after["tables"] for r in t["rows"] for c in r["cells"]}
        for record in page_map:
            location = record["catalog_cell"]
            if after_cells.get(tuple(location[k] for k in ("table", "row", "column"))) != record["catalog_page_text"]:
                _fail("EVIDENCE_CATALOG_VERIFICATION_FAILED", "清单回填与页码映射不一致。")
        review = ("# 证据页码处理记录\n\n原 PDF 和原清单保持只读。本次仅选取计划中的物理页，"
                  "在派生 PDF 可见底部增加 28pt 页脚区，并回填指定清单格子。\n\n"
                  f"共 {len(entries)} 项、{sequence - 1} 页；页段、书签与清单页码共用 manifest.json 中的 page_map。\n\n"
                  f"原清单：{catalog}\n\n来源哈希、页段覆盖及精确格子定位详见 manifest.json。\n\n"
                  "尚需检查实际页面的页脚、裁切、旋转、清单换行和分页；还需核对证据取舍与证明内容。"
                  "这是独立内部派生件，不更新案件状态，不构成正式提交批准。\n")
        (staging / "review.md").write_text(review, encoding="utf-8")
        manifest = {"ok": True, "kind": "evidence_pages", "schema_version": "1.0.0", "created_at": now_iso(),
                    "output_directory": str(output_dir), "status": "internal_derived", "visual_qa_required": True,
                    "catalog_source": {"path": str(catalog), "sha256": catalog_hash}, "page_count": sequence - 1,
                    "page_map": page_map, "page_map_sha256": mapping_hash,
                    "source_hashes": {str(path): digest for path, digest in sources.items()},
                    "files": {name: sha256_file(staging / name) for name in ("evidence-numbered.pdf", "catalog-filled.docx", "review.md")},
                    "checks": {"source_hashes": "passed", "page_map": "passed", "visual_review": "required", "substantive_review": "required"},
                    "catalog_patch": patch_report,
                    "notice": "仅内部派生处理；不替代原组卷、G2/G4 或正式提交批准。"}
        # A nested patch record may contain the temporary location. Store only
        # relative artifact names while retaining its factual edit assertions.
        for key, value in list(patch_report.items()):
            if isinstance(value, str) and str(staging) in value:
                patch_report[key] = value.replace(str(staging), str(output_dir))
        atomic_write_json(staging / "manifest.json", manifest)
        _recheck_sources(sources)
        if os.path.lexists(output_dir):
            _fail("OUTPUT_EXISTS", "输出目录已由其他操作创建，未覆盖。")
        staging.rename(output_dir)
        return manifest
    finally:
        # Delete only this call's resolved, randomly named staging directory.
        if staging.exists() and staging.parent == output_dir.parent.resolve() and staging.name.startswith(f".{output_dir.name}.staging-"):
            shutil.rmtree(staging)
