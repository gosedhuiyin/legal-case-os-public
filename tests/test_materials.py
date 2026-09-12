"""Synthetic offline material tests; no customer files, database, MCP or network."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib.core import LegalCaseError
from legal_case_os_lib import materials


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parts(data, size=7):
    result = []
    for offset in range(0, len(data), size) if data else [0]:
        value = data[offset:offset + size]
        end = offset + len(value)
        result.append({"schema_version": "1.0.0", "document_id": "TEST-DOC", "version_id": "TEST-V1",
                       "title": "TEST-ONLY", "file_type": "txt", "mime_type": "text/plain",
                       "encoding": "base64", "data_base64": base64.b64encode(value).decode(),
                       "offset": offset, "byte_length": len(value), "total_bytes": len(data),
                       "next_offset": end if end < len(data) else None,
                       "source_sha256": digest(data), "segment_sha256": digest(value)})
    return result


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "task-output"

    def source(self, data, suffix=".txt"):
        path = self.root / ("TEST-ONLY-source" + suffix)
        path.write_bytes(data)
        return path

    def test_readonly_source_offline_snapshot_and_idempotence(self):
        raw = "TEST-ONLY 第一行\r\n\r\n第二行。\n".encode()
        source = self.source(raw, ".md")
        first = materials.ingest_material(source, self.output, "template")
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(first["provenance"]["local_source_path"], str(source.resolve()))
        self.assertEqual(first, materials.ingest_material(source, self.output, "template"))
        source.unlink()  # The local snapshot remains readable without its source service.
        self.assertEqual(materials.validate_material(first), [])
        page = materials.read_material(first, limit=5)
        self.assertEqual(page["next_offset"], 5)
        self.assertIn("第二行", materials.read_material(first)["text"])
        self.assertEqual(first["segments"][2]["source_locator"]["line_start"], 3)
        self.assertFalse(first["template_activation"])

    def test_docx_paragraph_and_table_locators(self):
        stream = io.BytesIO()
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:r><w:t>TEST-ONLY 段落</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格文字</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
        with zipfile.ZipFile(stream, "w") as package:
            package.writestr("word/document.xml", xml)
            package.writestr("word/media/image1.png", b"TEST-ONLY image placeholder")
        record = materials.ingest_material(self.source(stream.getvalue(), ".docx"), self.output, "template")
        self.assertEqual(materials.read_material(record)["text"], "TEST-ONLY 段落\n表格文字")
        table = record["segments"][1]["source_locator"]
        self.assertEqual((table["table"], table["row"], table["cell"]), (1, 1, 1))
        self.assertIn("/tbl[1]/tr[1]/tc[1]/p[1]", table["xml_path"])
        self.assertIsNone(table["physical_page"])
        self.assertTrue(record["quality"]["needs_visual_or_ocr"])

    def test_corrupted_snapshot_or_metadata_is_rejected(self):
        record = materials.ingest_material(self.source(b"TEST-ONLY evidence"), self.output, "case")
        changed = copy.deepcopy(record)
        changed["role"] = "authority"
        self.assertIn("material_record_hash_mismatch", materials.validate_material(changed))
        Path(record["snapshot_path"]).write_bytes(b"tampered")
        with self.assertRaises(LegalCaseError):
            materials.read_material(record)
        with self.assertRaises(LegalCaseError):
            materials.ingest_material(self.root / "TEST-ONLY-source.txt", self.output, "case")

    def test_protected_library_and_originals_outputs_rejected(self):
        source = self.source(b"TEST-ONLY")
        for output in (self.root / "library" / "new", self.root / "matter" / "00-originals" / "new"):
            with self.assertRaises(LegalCaseError):
                materials.ingest_material(source, output, "case")
            self.assertFalse(output.exists())
        # A sibling task subdirectory is allowed without moving the upload.
        self.assertEqual(materials.validate_material(materials.ingest_material(source, self.output, "case")), [])

    def test_write_failure_leaves_no_record_or_partial_material(self):
        source = self.source(b"TEST-ONLY source")
        real_write = Path.write_bytes
        def fail_text(path, value):
            if path.name == "text.txt":
                raise OSError("synthetic disk failure")
            return real_write(path, value)
        with mock.patch.object(Path, "write_bytes", fail_text):
            with self.assertRaises(OSError):
                materials.ingest_material(source, self.output, "case")
        self.assertEqual(list(self.output.rglob("record.json")), [])
        self.assertEqual(list(self.output.rglob("source.txt")), [])
        self.assertEqual(list(self.output.rglob(".material-*")), [])
        self.assertEqual(source.read_bytes(), b"TEST-ONLY source")

    def test_original_parts_reconstruction_and_offline_read(self):
        raw = "TEST-ONLY 从真实字节重建。".encode()
        record = materials.import_original_parts(parts(raw), self.output, "template")
        self.assertEqual(Path(record["snapshot_path"]).read_bytes(), raw)
        self.assertEqual(record["origin"], "law_library")
        self.assertEqual(record["provenance"]["version_id"], "TEST-V1")
        self.assertEqual(materials.read_material(record)["text"], raw.decode())

    def test_parts_missing_reordered_corrupt_and_version_drift_fail(self):
        good = parts(b"TEST-ONLY longer than two segments")
        bad_values = [good[1:], list(reversed(good)), good[:-1], good + [good[-1]]]
        changed = copy.deepcopy(good)
        changed[1]["version_id"] = "TEST-V2"
        bad_values.append(changed)
        changed = copy.deepcopy(good)
        changed[0]["data_base64"] = base64.b64encode(b"corrupt").decode()
        bad_values.append(changed)
        changed = copy.deepcopy(good)
        for part in changed:
            part["source_sha256"] = "0" * 64
        bad_values.append(changed)
        changed = copy.deepcopy(good)
        changed[-1]["next_offset"] = changed[-1]["total_bytes"]
        bad_values.append(changed)
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(LegalCaseError):
                    materials.import_original_parts(value, self.output, "case")
        self.assertFalse(self.output.exists())

    def test_empty_source_terminal_chunk_is_explicit_no_text(self):
        record = materials.import_original_parts(parts(b""), self.output, "case")
        self.assertEqual(materials.read_material(record)["text"], "")
        self.assertEqual(record["coverage"]["status"], "no_text")

    def test_text_corruption_and_out_of_range_read_fail(self):
        record = materials.ingest_material(self.source(b"TEST-ONLY current facts"), self.output, "current_case")
        with self.assertRaises(LegalCaseError):
            materials.read_material(record, offset=1000)
        Path(record["text_path"]).write_bytes(b"changed text")
        self.assertIn("material_text_hash_mismatch_or_missing", materials.validate_material(record))
        with self.assertRaises(LegalCaseError):
            materials.read_material(record)

    def test_pdf_backend_unavailable_is_explicit_and_snapshot_retained(self):
        with mock.patch.object(materials, "_try_pdf_backend", return_value=(None, None)):
            record = materials.ingest_material(self.source(b"%PDF-1.4\nTEST-ONLY", ".pdf"), self.output, "case")
        self.assertEqual(record["coverage"]["status"], "extraction_unavailable")
        self.assertIsNone(record["coverage"]["physical_pages"])
        self.assertFalse(record["quality"]["text_available"])
        self.assertEqual(materials.validate_material(record), [])

    def test_pdf_no_text_page_records_physical_gap(self):
        backend, _ = materials._try_pdf_backend()
        if backend is None:
            self.skipTest("optional PDF backend unavailable")
        writer = backend[1]()
        writer.add_blank_page(width=200, height=200)
        stream = io.BytesIO()
        writer.write(stream)
        record = materials.ingest_material(self.source(stream.getvalue(), ".pdf"), self.output, "case")
        self.assertEqual(record["coverage"]["physical_pages"], 1)
        self.assertEqual(record["coverage"]["pages_with_text"], 0)
        self.assertEqual(record["coverage"]["ocr_gaps"][0]["physical_page"], 1)
        self.assertEqual(record["coverage"]["status"], "no_text")

    def test_invalid_docx_does_not_publish_record(self):
        with self.assertRaises(LegalCaseError):
            materials.ingest_material(self.source(b"not a docx", ".docx"), self.output, "template")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
