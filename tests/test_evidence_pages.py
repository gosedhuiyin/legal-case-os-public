"""Synthetic offline regression tests; no case materials or external services."""
from __future__ import annotations

import copy
import importlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib.core import LegalCaseError, sha256_file
from legal_case_os_lib import evidence_pages
from legal_case_os_lib.docx_edit import inspect_docx_targets
from legal_case_os_lib.documents import _try_pdf_backend, _try_page_number_backend


class EvidencePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "task-output"
        backend, name = _try_pdf_backend()
        self.canvas, _ = _try_page_number_backend()
        if backend is None or self.canvas is None:
            self.skipTest("pypdf/reportlab unavailable")
        self.PdfReader, self.PdfWriter = backend
        self.generic = importlib.import_module(("pypdf" if "pypdf" in name else "PyPDF2") + ".generic")
        self.source = self.make_pdf("source.pdf", [(0, None), (90, [30, 40, 270, 350]), (180, [20, 30, 280, 370])])
        self.catalog = self.root / "catalog.docx"
        self.make_catalog(self.catalog)
        self.plan = {"catalog": self.spec(self.catalog), "items": [
            {"id": "E1", "name": "TEST-ONLY same name", "source": self.spec(self.source),
             "page_ranges": [[3, 3], [1, 1]], "catalog_cell": {"table": 1, "row": 1, "column": 3, "expected_text": "-"}},
            {"id": "E2", "name": "TEST-ONLY same name", "source": self.spec(self.source),
             "page_ranges": [[2, 2]], "catalog_cell": {"table": 1, "row": 2, "column": 3, "expected_text": "-"}},
        ]}
        self.original_bytes = {path: path.read_bytes() for path in (self.source, self.catalog)}

    @staticmethod
    def spec(path):
        return {"path": str(path), "sha256": sha256_file(path)}

    def make_pdf(self, filename, geometries):
        data = io.BytesIO()
        canvas = self.canvas.Canvas(data, pagesize=(300, 400))
        for i in range(len(geometries)):
            canvas.setFont("Helvetica", 12)
            canvas.drawString(50, 210, f"TEST-ONLY physical page {i + 1}")
            canvas.drawString(0, 0, "CROPPED CORNER")
            canvas.rect(40, 60, 210, 280)
            canvas.showPage()
        canvas.save()
        reader = self.PdfReader(io.BytesIO(data.getvalue()))
        writer = self.PdfWriter()
        for page, (rotation, crop) in zip(reader.pages, geometries):
            page[self.generic.NameObject("/Rotate")] = self.generic.NumberObject(rotation)
            if crop:
                page.cropbox = self.generic.RectangleObject(crop)
            writer.add_page(page)
        path = self.root / filename
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    @staticmethod
    def make_catalog(path):
        cell = lambda text: f'<w:tc><w:tcPr><w:tcW w:w="1800" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'
        rows = "".join("<w:tr>" + cell(f"E{i}") + cell("TEST-ONLY same name") + cell("-") + "</w:tr>" for i in (1, 2))
        document = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>TEST-ONLY retain this title and TBD</w:t></w:r></w:p>'
                    '<w:tbl><w:tblPr><w:tblW w:w="5400" w:type="dxa"/></w:tblPr>' + rows +
                    '</w:tbl><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            package.writestr("word/document.xml", document)
            package.writestr("word/styles.xml", '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
            package.writestr("word/header1.xml", '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>TEST-ONLY untouched header</w:t></w:r></w:p></w:hdr>')

    def assert_failure(self, code, plan=None):
        with self.assertRaises(LegalCaseError) as caught:
            evidence_pages.build_evidence_pages(plan or self.plan, self.out)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".task-output.staging-*")), [])

    def test_ranges_catalog_bookmarks_and_duplicate_names_use_explicit_ids(self):
        result = evidence_pages.build_evidence_pages(self.plan, self.out)
        self.assertEqual(result["page_count"], 3)
        self.assertEqual([r["physical_pages"] for r in result["page_map"]], [[3, 1], [2]])
        self.assertEqual([r["catalog_page_text"] for r in result["page_map"]], ["1-2", "3"])
        self.assertEqual(result["checks"]["visual_review"], "required")
        self.assertEqual(result["status"], "internal_derived")
        self.assertEqual(json.loads((self.out / "manifest.json").read_text(encoding="utf-8")), result)
        for name, digest in result["files"].items():
            self.assertEqual(sha256_file(self.out / name), digest)
        reader = self.PdfReader(str(self.out / "evidence-numbered.pdf"))
        self.assertEqual([str(o.title) for o in reader.outline], ["E1 TEST-ONLY same name", "E2 TEST-ONLY same name"])
        self.assertEqual([reader.get_destination_page_number(o) for o in reader.outline], [0, 2])
        for index, physical in enumerate((3, 1, 2)):
            text = reader.pages[index].extract_text()
            self.assertIn(f"physical page {physical}", text)
            self.assertIn(f"{index + 1} / 3", text)
        inspected = inspect_docx_targets(self.out / "catalog-filled.docx")
        self.assertEqual([r["cells"][2]["text"] for r in inspected["tables"][0]["rows"]], ["1-2", "3"])
        with zipfile.ZipFile(self.catalog) as before, zipfile.ZipFile(self.out / "catalog-filled.docx") as after:
            for name in before.namelist():
                if name != "word/document.xml":
                    self.assertEqual(before.read(name), after.read(name))
            self.assertIn(b"retain this title and TBD", after.read("word/document.xml"))
        for source, raw in self.original_bytes.items():
            self.assertEqual(source.read_bytes(), raw)

    def test_rotated_crops_extend_only_display_bottom_and_keep_clipping(self):
        source = self.make_pdf("four-rotations.pdf", [(v, [30, 40, 270, 350]) for v in (0, 90, 180, 270)])
        plan = copy.deepcopy(self.plan)
        plan["items"] = [plan["items"][0]]
        plan["items"][0].update(source=self.spec(source), page_ranges=[[1, 4]])
        evidence_pages.build_evidence_pages(plan, self.out)
        reader = self.PdfReader(str(self.out / "evidence-numbered.pdf"))
        for page, rotation, crop in zip(reader.pages, (0, 90, 180, 270),
                                       ([30, 12, 270, 350], [30, 40, 298, 350], [30, 40, 270, 378], [2, 40, 270, 350])):
            self.assertEqual(int(page.get("/Rotate", 0)), rotation)
            self.assertEqual(list(page.cropbox), crop)
            operations = page.get_contents().operations
            self.assertIn(b"W", [operator for _, operator in operations])
            self.assertTrue(any(operator == b"re" and [float(v) for v in args] == [30, 40, 240, 310]
                                for args, operator in operations))

    def test_changed_source_and_catalog_are_rejected(self):
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assert_failure("EVIDENCE_SOURCE_CHANGED")
        self.source.write_bytes(self.original_bytes[self.source])
        self.catalog.write_bytes(self.catalog.read_bytes() + b"changed")
        self.assert_failure("EVIDENCE_SOURCE_CHANGED")

    def test_bad_ranges_duplicate_pages_and_unknown_or_missing_mapping_fail(self):
        for ranges in ([], [[0, 1]], [[3, 2]], [[1, 4]], [[True, 1]], [[1, 1, 2]], "1-2"):
            plan = copy.deepcopy(self.plan)
            plan["items"][0]["page_ranges"] = ranges
            with self.subTest(ranges=ranges):
                self.assert_failure("INVALID_EVIDENCE_PAGE_RANGE", plan)
        plan = copy.deepcopy(self.plan)
        plan["items"][1]["page_ranges"] = [[1, 1]]
        self.assert_failure("DUPLICATE_EVIDENCE_PAGE", plan)
        plan = copy.deepcopy(self.plan)
        plan["items"][1]["id"] = "E1"
        self.assert_failure("DUPLICATE_EVIDENCE_ID", plan)
        plan = copy.deepcopy(self.plan)
        plan["items"][1]["catalog_cell"]["row"] = 1
        self.assert_failure("DUPLICATE_CATALOG_MAPPING", plan)
        for update in ({"row": 99}, {"expected_text": "wrong"}):
            plan = copy.deepcopy(self.plan)
            plan["items"][0]["catalog_cell"].update(update)
            self.assert_failure("INVALID_CATALOG_MAPPING", plan)
        plan = copy.deepcopy(self.plan)
        del plan["items"][0]["catalog_cell"]
        self.assert_failure("INVALID_EVIDENCE_PLAN", plan)
        plan = copy.deepcopy(self.plan)
        plan["guess_by_name"] = True
        self.assert_failure("INVALID_EVIDENCE_PLAN", plan)

    def test_existing_output_is_never_overwritten(self):
        self.out.mkdir()
        sentinel = self.out / "keep.txt"
        sentinel.write_text("untouched")
        with self.assertRaises(LegalCaseError) as caught:
            evidence_pages.build_evidence_pages(self.plan, self.out)
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(sentinel.read_text(), "untouched")

    def test_own_derived_pdf_is_rejected_even_after_metadata_removal(self):
        evidence_pages.build_evidence_pages(self.plan, self.out)
        derived = self.out / "evidence-numbered.pdf"
        plan = copy.deepcopy(self.plan)
        plan["items"] = [plan["items"][0]]
        plan["items"][0].update(source=self.spec(derived), page_ranges=[[1, 1]])
        self.out = self.root / "task-output-again"
        self.assert_failure("EVIDENCE_ALREADY_NUMBERED", plan)
        reader = self.PdfReader(str(derived))
        writer = self.PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        stripped = self.root / "renamed-no-metadata.pdf"
        with stripped.open("wb") as handle:
            writer.write(handle)
        plan["items"][0]["source"] = self.spec(stripped)
        self.assert_failure("EVIDENCE_ALREADY_NUMBERED", plan)

    def test_interruption_never_publishes_partial_directory(self):
        def interrupt(path, *args):
            path.write_bytes(b"partial")
            raise KeyboardInterrupt("TEST-ONLY interruption")
        with mock.patch.object(evidence_pages, "_build_pdf", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                evidence_pages.build_evidence_pages(self.plan, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".task-output.staging-*")), [])
        for source, raw in self.original_bytes.items():
            self.assertEqual(source.read_bytes(), raw)

    def test_source_change_during_build_prevents_publication(self):
        original = evidence_pages._verify_pdf
        def mutate_source(*args):
            original(*args)
            self.source.write_bytes(self.source.read_bytes() + b"changed during build")
        with mock.patch.object(evidence_pages, "_verify_pdf", side_effect=mutate_source):
            self.assert_failure("EVIDENCE_SOURCE_CHANGED")

    def test_page_annotations_fail_without_silent_flattening(self):
        reader = self.PdfReader(str(self.source))
        writer = self.PdfWriter()
        for page in reader.pages:
            page[self.generic.NameObject("/Annots")] = self.generic.ArrayObject([self.generic.DictionaryObject()])
            writer.add_page(page)
        path = self.root / "annotated.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        plan = copy.deepcopy(self.plan)
        plan["items"][0]["source"] = self.spec(path)
        self.assert_failure("UNSUPPORTED_EVIDENCE_PAGE", plan)

    def test_optional_content_layers_are_rejected_without_revealing_hidden_content(self):
        reader = self.PdfReader(str(self.source))
        writer = self.PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        g = self.generic
        layer = writer._add_object(g.DictionaryObject({
            g.NameObject("/Type"): g.NameObject("/OCG"),
            g.NameObject("/Name"): g.TextStringObject("TEST-ONLY hidden layer"),
        }))
        writer._root_object[g.NameObject("/OCProperties")] = g.DictionaryObject({
            g.NameObject("/OCGs"): g.ArrayObject([layer]),
            g.NameObject("/D"): g.DictionaryObject({
                g.NameObject("/OFF"): g.ArrayObject([layer]),
                g.NameObject("/BaseState"): g.NameObject("/ON"),
            }),
        })
        path = self.root / "hidden-layer.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        before = path.read_bytes()
        plan = copy.deepcopy(self.plan)
        plan["items"][0]["source"] = self.spec(path)
        with self.assertRaises(LegalCaseError) as caught:
            evidence_pages.build_evidence_pages(plan, self.out)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_EVIDENCE_PAGE")
        self.assertIn("图层", str(caught.exception))
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".task-output.staging-*")), [])
        self.assertEqual(path.read_bytes(), before)

    def test_page_transparency_group_is_rejected_without_changing_blending_space(self):
        reader = self.PdfReader(str(self.source))
        writer = self.PdfWriter()
        g = self.generic
        for page in reader.pages:
            page[g.NameObject("/Group")] = g.DictionaryObject({
                g.NameObject("/S"): g.NameObject("/Transparency"),
                g.NameObject("/CS"): g.NameObject("/DeviceGray"),
                g.NameObject("/I"): g.BooleanObject(True),
                g.NameObject("/K"): g.BooleanObject(True),
            })
            writer.add_page(page)
        path = self.root / "transparency-group.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        before = path.read_bytes()
        plan = copy.deepcopy(self.plan)
        plan["items"][0]["source"] = self.spec(path)
        with self.assertRaises(LegalCaseError) as caught:
            evidence_pages.build_evidence_pages(plan, self.out)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_EVIDENCE_PAGE")
        self.assertIn("透明组", str(caught.exception))
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".task-output.staging-*")), [])
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
