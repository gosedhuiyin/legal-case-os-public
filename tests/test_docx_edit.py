"""Source-bound text patches using only synthetic OOXML fixtures."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib.core import LegalCaseError, sha256_file
from legal_case_os_lib.docx_edit import inspect_docx_targets, patch_docx


DOCUMENT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<w:body>
<w:p w:rsidR="00112233"><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>TEST-ONLY 标题</w:t></w:r></w:p>
<w:p><w:pPr><w:spacing w:after="200"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>保留称谓</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>与观点</w:t></w:r></w:p>
<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="8000" w:type="dxa"/></w:tblPr><w:tblGrid><w:gridCol w:w="6000"/><w:gridCol w:w="2000"/></w:tblGrid>
<w:tr><w:tc><w:tcPr><w:tcW w:w="6000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>证据一</w:t></w:r></w:p></w:tc><w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>待填</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>证据二</w:t></w:r></w:p></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl>
<w:sectPr><w:headerReference w:type="default" r:id="rId1"/><w:footerReference w:type="default" r:id="rId2"/><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/><w:pgMar w:top="1200" w:right="1300" w:bottom="1400" w:left="1500"/></w:sectPr>
</w:body></w:document>'''
OTHER_PARTS = {
    "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
    "word/styles.xml": b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:type="paragraph" w:styleId="Title"/></w:styles>',
    "word/header1.xml": b"TEST-ONLY original header bytes",
    "word/footer1.xml": b"TEST-ONLY original footer bytes",
    "word/_rels/document.xml.rels": b"TEST-ONLY original relationship bytes",
    "word/media/image1.png": b"TEST-ONLY original image bytes",
}


def fixture(path: Path, xml: str = DOCUMENT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.comment = b"TEST-ONLY package comment"
        package.writestr("word/document.xml", xml.encode("utf-8"))
        for name, data in OTHER_PARTS.items():
            package.writestr(name, data)
    return path


class DocxEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = fixture(self.root / "source.docx")
        self.output = self.root / "result.docx"

    def plan(self, target=None, expected="保留称谓与观点", replacement="保留称谓与核心观点"):
        return {"source_sha256": sha256_file(self.source), "changes": [{"target": target or {"kind": "paragraph", "paragraph": 2}, "expected_text": expected, "replacement_text": replacement}]}

    def assert_error(self, code, plan, output=None):
        source_digest = sha256_file(self.source)
        with self.assertRaises(LegalCaseError) as caught:
            patch_docx(self.source, output or self.output, plan)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(sha256_file(self.source), source_digest)
        self.assertFalse(self.output.exists())

    def test_inventory_has_unambiguous_body_and_cell_coordinates(self):
        result = inspect_docx_targets(self.source)
        self.assertEqual(result["source_sha256"], sha256_file(self.source))
        self.assertEqual(len(result["paragraphs"]), 2)
        self.assertEqual(result["paragraphs"][1]["text"], "保留称谓与观点")
        cell = result["tables"][0]["rows"][1]["cells"][1]
        self.assertEqual(cell["target"], {"kind": "cell", "table": 1, "row": 2, "column": 2})
        self.assertTrue(cell["editable"])
        self.assertEqual(cell["text"], "")

    def test_patch_preserves_every_outside_xml_byte_and_zip_part(self):
        source_digest = sha256_file(self.source)
        result = patch_docx(self.source, self.output, self.plan())
        self.assertEqual(sha256_file(self.source), source_digest)
        with zipfile.ZipFile(self.source) as old, zipfile.ZipFile(self.output) as new:
            self.assertEqual(old.namelist(), new.namelist())
            self.assertEqual(old.comment, new.comment)
            for name in OTHER_PARTS:
                self.assertEqual(old.read(name), new.read(name))
                self.assertEqual(old.getinfo(name).date_time, new.getinfo(name).date_time)
            expected = old.read("word/document.xml").replace('<w:t>保留称谓</w:t>'.encode(), '<w:t xml:space="preserve">保留称谓与核心观点</w:t>'.encode()).replace('<w:t>与观点</w:t>'.encode(), b'<w:t></w:t>')
            self.assertEqual(new.read("word/document.xml"), expected)
        self.assertEqual(result["output_sha256"], sha256_file(self.output))
        self.assertEqual(result["visual_review_status"], "not_run")
        self.assertTrue(result["constraint_checks"]["semantic_review_required"])

    def test_cell_long_text_preserves_grid_dimensions_and_other_cells(self):
        target = {"kind": "cell", "table": 1, "row": 1, "column": 2}
        text = " 测试长内容<&>" * 500
        patch_docx(self.source, self.output, self.plan(target, "待填", text))
        result = inspect_docx_targets(self.output)
        self.assertEqual(result["tables"][0]["rows"][0]["cells"][1]["text"], text)
        self.assertEqual(result["tables"][0]["rows"][0]["cells"][0]["text"], "证据一")
        with zipfile.ZipFile(self.output) as package:
            xml = package.read("word/document.xml")
            self.assertIn(b'<w:gridCol w:w="6000"/>', xml)
            self.assertIn(b'w:orient="landscape"', xml)

    def test_empty_cell_and_empty_text_node_support(self):
        target = {"kind": "cell", "table": 1, "row": 2, "column": 2}
        patch_docx(self.source, self.output, self.plan(target, "", "7—8"))
        self.assertEqual(inspect_docx_targets(self.output)["tables"][0]["rows"][1]["cells"][1]["text"], "7—8")
        self.output.unlink()
        fixture(self.source, DOCUMENT.replace("<w:p/>", '<w:p><w:r><w:rPr><w:i/></w:rPr><w:t /></w:r></w:p>'))
        patch_docx(self.source, self.output, self.plan(target, "", "9"))
        self.assertEqual(inspect_docx_targets(self.output)["tables"][0]["rows"][1]["cells"][1]["text"], "9")

    def test_empty_styled_run_support(self):
        fixture(self.source, DOCUMENT.replace("<w:p/>", '<w:p><w:r><w:rPr><w:i/></w:rPr></w:r></w:p>'))
        target = {"kind": "cell", "table": 1, "row": 2, "column": 2}
        patch_docx(self.source, self.output, self.plan(target, "", "9"))
        with zipfile.ZipFile(self.output) as package:
            self.assertIn(b'<w:rPr><w:i/></w:rPr><w:t xml:space="preserve">9</w:t>', package.read("word/document.xml"))

    def test_stale_source_and_mismatched_text_do_not_write(self):
        plan = self.plan()
        fixture(self.source, DOCUMENT.replace("TEST-ONLY 标题", "TEST-ONLY 新标题"))
        self.assert_error("DOCX_SOURCE_STALE", plan)
        self.assert_error("DOCX_TEXT_MISMATCH", self.plan(expected="不存在"))

    def test_duplicate_and_ambiguous_coordinates_rejected(self):
        plan = self.plan()
        plan["changes"].append(copy.deepcopy(plan["changes"][0]))
        self.assert_error("DOCX_TARGET_AMBIGUOUS", plan)
        plan = self.plan()
        plan["changes"][0]["target"]["text"] = "保留称谓与观点"
        self.assert_error("DOCX_TARGET_INVALID", plan)
        self.assert_error("DOCX_TARGET_INVALID", self.plan({"kind": "paragraph", "paragraph": True}))
        self.assert_error("DOCX_TARGET_INVALID", self.plan({"kind": [], "paragraph": 1}))

    def test_fields_and_tracked_changes_rejected_only_when_targeted(self):
        for markup in ('<w:fldSimple w:instr="PAGE"><w:r><w:t>1</w:t></w:r></w:fldSimple>', '<w:ins w:id="1"><w:r><w:t>修订</w:t></w:r></w:ins>', '<w:r><w:fldChar w:fldCharType="begin"/><w:instrText>PAGE</w:instrText></w:r>'):
            with self.subTest(markup=markup):
                fixture(self.source, DOCUMENT.replace('<w:r><w:t>TEST-ONLY 标题</w:t></w:r>', markup))
                inventory = inspect_docx_targets(self.source)
                self.assertFalse(inventory["paragraphs"][0]["editable"])
                self.assert_error("DOCX_TARGET_UNSUPPORTED", self.plan({"kind": "paragraph", "paragraph": 1}, inventory["paragraphs"][0]["text"], "new"))
                patch_docx(self.source, self.output, self.plan())
                self.output.unlink()

    def test_mixed_rich_text_rejected_with_reason(self):
        fixture(self.source, DOCUMENT.replace('<w:b/></w:rPr><w:t>与观点', '<w:i/></w:rPr><w:t>与观点'))
        record = inspect_docx_targets(self.source)["paragraphs"][1]
        self.assertFalse(record["editable"])
        self.assertIn("富文本", record["reason"])
        self.assert_error("DOCX_TARGET_UNSUPPORTED", self.plan())

    def test_merged_and_nested_tables_are_inspectable_but_not_patchable(self):
        for modification in (DOCUMENT.replace('<w:tcPr><w:tcW', '<w:tcPr><w:gridSpan w:val="2"/><w:tcW', 1), DOCUMENT.replace('<w:p/>', '<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl><w:p/>')):
            fixture(self.source, modification)
            self.assertFalse(inspect_docx_targets(self.source)["tables"][0]["rows"][0]["cells"][1]["editable"])
            self.assert_error("DOCX_TARGET_UNSUPPORTED", self.plan({"kind": "cell", "table": 1, "row": 1, "column": 2}, "待填", "1"))

    def test_multiple_cell_paragraphs_rejected(self):
        fixture(self.source, DOCUMENT.replace("<w:p/>", "<w:p/><w:p/>"))
        self.assert_error("DOCX_TARGET_UNSUPPORTED", self.plan({"kind": "cell", "table": 1, "row": 2, "column": 2}, "\n", "1"))

    def test_protected_destinations_and_source_cannot_be_written(self):
        self.assert_error("ORIGINALS_READ_ONLY", self.plan(), self.root / "00-originals" / "new.docx")
        self.assert_error("LIBRARY_READ_ONLY", self.plan(), self.root / "library" / "new.docx")
        self.assert_error("SOURCE_READ_ONLY", self.plan(), self.source)
        self.assertFalse((self.root / "00-originals").exists())
        self.assertFalse((self.root / "library").exists())

    def test_repeated_patch_does_not_replace_existing_output(self):
        patch_docx(self.source, self.output, self.plan())
        digest = sha256_file(self.output)
        with self.assertRaises(LegalCaseError) as caught:
            patch_docx(self.source, self.output, self.plan(replacement="different"))
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(sha256_file(self.output), digest)

    def test_publication_race_preserves_competing_output_and_cleans_temp(self):
        def race(_source, output):
            Path(output).write_bytes(b"TEST-ONLY concurrent artifact")
            raise FileExistsError()
        with mock.patch("legal_case_os_lib.docx_edit.os.link", side_effect=race):
            with self.assertRaises(LegalCaseError) as caught:
                patch_docx(self.source, self.output, self.plan())
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(self.output.read_bytes(), b"TEST-ONLY concurrent artifact")
        self.assertFalse(list(self.root.glob(".docx-patch-*")))

    def test_constraints_record_is_literal_and_preserves_preferences(self):
        plan = self.plan()
        plan["constraints"] = {"must_keep": ["核心观点", "尚不存在"], "avoid": ["内部审阅稿"], "terminology": {"被告": "我方"}}
        plan["parent_manifest"] = "previous/manifest.json"
        result = patch_docx(self.source, self.output, plan)
        self.assertEqual(result["constraints"], plan["constraints"])
        self.assertFalse(result["constraint_checks"]["must_keep"][1]["present"])
        self.assertTrue(result["constraint_checks"]["semantic_review_required"])
        self.assertEqual(result["parent_manifest"], "previous/manifest.json")

    def test_control_text_rejected_before_publication(self):
        for text in ("one\ntwo", "a\tb", "bad\x01", "bad\ud800"):
            self.assert_error("DOCX_TEXT_UNSUPPORTED", self.plan(replacement=text))

    def test_source_recheck_before_publication_stops_stale_result(self):
        plan = self.plan()
        def changed_hash(path):
            return "0" * 64 if Path(path) == self.source else sha256_file(path)
        with mock.patch("legal_case_os_lib.docx_edit.sha256_file", side_effect=changed_hash):
            self.assert_error("DOCX_SOURCE_STALE", plan)
        self.assertFalse(list(self.root.glob(".docx-patch-*")))

    def test_non_utf8_declared_encoding_rejected(self):
        fixture(self.source, DOCUMENT.replace('encoding="UTF-8"', 'encoding="ISO-8859-1"'))
        self.assert_error("DOCX_UNSUPPORTED_XML", self.plan())

    def test_existing_spaced_xml_space_attribute_is_not_duplicated(self):
        fixture(self.source, DOCUMENT.replace('<w:t>保留称谓', '<w:t xml:space = "default">保留称谓'))
        patch_docx(self.source, self.output, self.plan(replacement=" new words "))
        self.assertEqual(inspect_docx_targets(self.output)["paragraphs"][1]["text"], " new words ")

    def test_non_target_comment_and_ignorable_namespace_are_byte_preserved(self):
        fixture(self.source, DOCUMENT.replace('<w:body>', '<w:body><!-- TEST-ONLY retained comment -->').replace('xmlns:r=', 'xmlns:unused="urn:test-only" xmlns:r='))
        patch_docx(self.source, self.output, self.plan())
        with zipfile.ZipFile(self.output) as package:
            self.assertIn(b'xmlns:unused="urn:test-only"', package.read("word/document.xml"))
            self.assertIn(b'<!-- TEST-ONLY retained comment -->', package.read("word/document.xml"))


if __name__ == "__main__":
    unittest.main()
