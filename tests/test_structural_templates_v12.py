#!/usr/bin/env python3
"""TEST-ONLY executable coverage for repeatable rows and hybrid body blocks."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from legal_case_os_lib import template_curation as curation  # noqa: E402
from legal_case_os_lib.core import LegalCaseError, sha256_file, validate_against_schema  # noqa: E402


CONTENT_TYPES = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
 <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''
ROOT_RELS = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
DOC_RELS = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''
STYLES = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>'''


def write_docx(path: Path, document: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", ROOT_RELS)
        package.writestr("word/document.xml", document.encode("utf-8"))
        package.writestr("word/styles.xml", STYLES)
        package.writestr("word/_rels/document.xml.rels", DOC_RELS)


def repeat_document(*, dangerous: bool = False) -> str:
    tc_pr = "<w:tcPr><w:vMerge w:val=\"restart\"/></w:tcPr>" if dangerous else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>TEST-ONLY 打印材料单</w:t></w:r></w:p>
 <w:tbl><w:tblGrid><w:gridCol w:w="800"/><w:gridCol w:w="4000"/><w:gridCol w:w="1200"/></w:tblGrid>
  <w:tr><w:tc><w:p><w:r><w:t>序号</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>材料</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>份数</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc>{tc_pr}<w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>TEST-ONLY OLD CLIENT</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>99</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
</w:body></w:document>'''


def hybrid_document(*, dangerous: bool = False) -> str:
    body = (
        '<w:p><w:hyperlink><w:r><w:t>TEST-ONLY OLD-CASE BODY</w:t></w:r></w:hyperlink></w:p>'
        if dangerous else
        '<w:p><w:pPr><w:spacing w:line="600"/></w:pPr><w:r><w:rPr><w:sz w:val="28"/></w:rPr><w:t>TEST-ONLY OLD-CASE BODY</w:t></w:r></w:p>'
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>TEST-ONLY 申请书</w:t></w:r></w:p>
 <w:p><w:r><w:t>TEST-ONLY 旧法院：</w:t></w:r></w:p>
 {body}
 <w:p><w:r><w:t>申请人：</w:t></w:r><w:r><w:t>TEST-ONLY OLD APPLICANT</w:t></w:r></w:p>
 <w:p><w:r><w:t>日期：</w:t></w:r><w:r><w:t>TEST-ONLY OLD DATE</w:t></w:r></w:p>
 <w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
</w:body></w:document>'''


def baseline(root: Path, source: Path) -> dict:
    page = root / "page-1.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"\x89PNG\r\n\x1a\nTEST-ONLY")
    return {
        "status": "verified", "source_sha256": sha256_file(source), "renderer": "TEST-ONLY-renderer",
        "page_count": 1, "pages": [{"page_number": 1, "path": str(page), "sha256": sha256_file(page)}],
        "reviewed_by": "TEST-ONLY-human", "reviewed_at": "2026-01-01T00:00:00Z",
    }


def source_tree(source: Path):
    entries = curation._read_zip_entries(source)
    root, _ = curation._document_root_from_entries(entries)
    return root, curation._element_paths(root)


def active_repeat_profile(root: Path, *, dangerous: bool = False) -> tuple[Path, Path, dict]:
    source = root / "TEST-ONLY-repeat.docx"
    write_docx(source, repeat_document(dangerous=dangerous))
    tree, paths = source_tree(source)
    table = next(tree.iter(curation.W_TBL))
    prototype = list(table.findall(f"./{curation.W_TR}"))[1]
    texts = list(prototype.iter(curation.W_T))
    block = {
        "block_id": "MATERIAL-ROWS", "part": "word/document.xml",
        "container_path": paths[id(table)], "prototype_row_path": paths[id(prototype)],
        "prototype_row_sha256": curation._subtree_digest(prototype),
        "prototype_text_node_count": len(texts), "fixed_text_nodes": [],
        "item_slots": [
            {"item_slot_id": "SEQ", "field_key": "sequence", "text_node_index": 1,
             "expected_text_sha256": hashlib.sha256((texts[0].text or "").encode()).hexdigest(),
             "policy": "required_verified", "max_chars": 3, "sequence": True},
            {"item_slot_id": "NAME", "field_key": "item_name", "text_node_index": 2,
             "expected_text_sha256": hashlib.sha256((texts[1].text or "").encode()).hexdigest(),
             "policy": "required_verified", "max_chars": 40},
            {"item_slot_id": "COPIES", "field_key": "copies", "text_node_index": 3,
             "expected_text_sha256": hashlib.sha256((texts[2].text or "").encode()).hexdigest(),
             "policy": "required_verified", "max_chars": 6},
        ],
        "min_items": 0, "max_items": 3,
        "blocked_text_sha256": [hashlib.sha256("TEST-ONLY OLD CLIENT".encode()).hexdigest(), hashlib.sha256("99".encode()).hexdigest()],
    }
    result = curation.distill_template(
        source, root / "profiles", "form", "fillable_clone", "TEST-REPEAT-001",
        "TEST-ONLY标准行", "打印材料单",
        overrides={"test_only": True, "repeatable_blocks": [block], "approve_all_fixed_as_boilerplate": True,
                   "visual_baseline": baseline(root / "baseline", source), "unresolved_items": []},
    )
    profile = result["profile"]
    profile.update({"status": "active", "approval": {"confirmed": True, "approved_by": "TEST-ONLY-human", "decision_id": "TEST-APPROVAL-REPEAT"}})
    profile_path = Path(result["profile_path"])
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return source, profile_path, profile


def active_hybrid_profile(root: Path, *, dangerous: bool = False) -> tuple[Path, Path, dict]:
    source = root / "TEST-ONLY-hybrid.docx"
    write_docx(source, hybrid_document(dangerous=dangerous))
    tree, paths = source_tree(source)
    body = next(item for item in tree.iter() if item.tag == f"{{{curation.W_NS}}}body")
    paragraphs = [item for item in list(body) if item.tag == curation.W_P]
    text_nodes = list(tree.iter(curation.W_T))
    def locator(node):
        return {"part": "word/document.xml", "path": paths[id(node)],
                "text_node_index": text_nodes.index(node) + 1,
                "expected_text_sha256": hashlib.sha256((node.text or "").encode()).hexdigest()}
    mechanics = [
        {"slot_id": "TITLE", "field_key": "title", "locator": locator(text_nodes[0]), "policy": "fixed_locked", "max_chars": 30, "block_original_text_in_output": False},
        {"slot_id": "COURT", "field_key": "court", "locator": locator(text_nodes[1]), "policy": "required_verified", "max_chars": 40, "block_original_text_in_output": True},
        {"slot_id": "APPLICANT", "field_key": "applicant", "locator": locator(text_nodes[-3]), "policy": "required_verified", "max_chars": 40, "block_original_text_in_output": True},
        {"slot_id": "DATE", "field_key": "date", "locator": locator(text_nodes[-1]), "policy": "manual_blank", "max_chars": 20, "block_original_text_in_output": True},
    ]
    region = {
        "region_id": "MAIN-BODY", "part": "word/document.xml", "container_path": paths[id(body)],
        "start_block_path": paths[id(paragraphs[2])], "end_block_path": paths[id(paragraphs[2])],
        "source_block_sha256": curation._subtree_digest([paragraphs[2]]),
        "preceding_anchor": {"path": paths[id(paragraphs[1])], "subtree_sha256": curation._subtree_digest(paragraphs[1])},
        "following_anchor": {"path": paths[id(paragraphs[3])], "subtree_sha256": curation._subtree_digest(paragraphs[3])},
        "min_paragraphs": 0, "max_paragraphs": 3,
        "paragraph_roles": [{"role": "body", "donor_paragraph_path": paths[id(paragraphs[2])],
                             "donor_paragraph_sha256": curation._subtree_digest(paragraphs[2]),
                             "donor_run_index": 1, "max_chars": 120}],
        "blocked_text_sha256": [hashlib.sha256("TEST-ONLY OLD-CASE BODY".encode()).hexdigest()],
    }
    result = curation.distill_template(
        source, root / "profiles", "writing", "hybrid", "TEST-HYBRID-001", "TEST-ONLY混合申请书", "申请书",
        overrides={
            "test_only": True, "mechanical_slots": mechanics, "body_regions": [region], "body_boundaries": [],
            "curated_features": {"section_functions": ["TEST-ONLY功能"], "argument_pattern": ["TEST-ONLY模式"],
                                 "voice_rules": ["TEST-ONLY语气"], "annotation_rules": ["TEST-ONLY标注"],
                                 "length_rules": ["TEST-ONLY篇幅"]},
            "composition_preferences": {"layout_score": 90, "structure_score": 90, "auxiliary_score": 0,
                "priority": 90, "role_suitability": ["layout", "structure"], "section_targets": [],
                "selection_notes": "TEST-ONLY hybrid donor",
                "compatibility_tags": {"relief_or_position": [], "evidence_use": [], "authority": [], "external_risk": []}},
            "reusable_aspects": ["page_system", "typography"],
            "visual_baseline": baseline(root / "baseline", source), "unresolved_items": [],
        },
    )
    profile = result["profile"]
    profile.update({"status": "active", "approval": {"confirmed": True, "approved_by": "TEST-ONLY-human", "decision_id": "TEST-APPROVAL-HYBRID"}})
    profile_path = Path(result["profile_path"])
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return source, profile_path, profile


def document_texts(path: Path) -> list[str]:
    root, _ = curation._document_root_from_entries(curation._read_zip_entries(path))
    return [item.text or "" for item in root.iter(curation.W_T)]


class StructuralTemplateV12Tests(unittest.TestCase):
    maxDiff = None

    def test_repeatable_zero_one_many_and_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-repeat-") as temporary:
            root = Path(temporary)
            source, profile_path, profile = active_repeat_profile(root)
            self.assertTrue(curation.validate_profile(profile, require_active=True)["ok"])
            schema = json.loads((PROJECT_ROOT / "shared/schemas/form-profile.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_against_schema(profile, schema), [])
            original_hash = sha256_file(source)
            for count in (0, 1, 3):
                items = [{"item_id": f"I-{i}", "values": {"item_name": f"TEST-ONLY材料{i}", "copies": f"{i}份"}} for i in range(1, count + 1)]
                plan = curation.build_repeatable_plan(profile_path, [{"block_id": "MATERIAL-ROWS", "items": items}])
                fill_schema = json.loads((PROJECT_ROOT / "shared/schemas/fill-plan.schema.json").read_text(encoding="utf-8"))
                self.assertEqual(validate_against_schema(plan, fill_schema), [])
                output = root / f"repeat-{count}.docx"
                result = curation.apply_structural_docx_plan(source, output, profile_path, plan)
                self.assertTrue(result["transform_aware_diff"]["ok"])
                self.assertEqual(sha256_file(source), original_hash)
                texts = document_texts(output)
                self.assertNotIn("TEST-ONLY OLD CLIENT", texts)
                self.assertEqual(sum(text.startswith("TEST-ONLY材料") for text in texts), count)

    def test_repeatable_bounds_dangerous_structure_leak_and_long_text_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-repeat-negative-") as temporary:
            root = Path(temporary)
            source, profile_path, _ = active_repeat_profile(root)
            with self.assertRaises(LegalCaseError) as raised:
                curation.build_repeatable_plan(profile_path, [{"block_id": "MATERIAL-ROWS", "items": [
                    {"item_id": f"I-{i}", "values": {"item_name": "TEST-ONLY X", "copies": "1"}} for i in range(4)
                ]}])
            self.assertEqual(raised.exception.code, "REPEATABLE_ITEM_COUNT_OUT_OF_RANGE")
            with self.assertRaises(LegalCaseError) as raised:
                curation.build_repeatable_plan(profile_path, [{"block_id": "MATERIAL-ROWS", "items": [
                    {"item_id": "LONG", "values": {"item_name": "X" * 41, "copies": "1"}}
                ]}])
            self.assertEqual(raised.exception.code, "STRUCTURAL_TEXT_TOO_LONG")
            leak_plan = curation.build_repeatable_plan(profile_path, [{"block_id": "MATERIAL-ROWS", "items": [
                {"item_id": "LEAK", "values": {"item_name": "TEST-ONLY OLD CLIENT", "copies": "1"}}
            ]}])
            with self.assertRaises(LegalCaseError) as raised:
                curation.apply_structural_docx_plan(source, root / "leak.docx", profile_path, leak_plan)
            self.assertEqual(raised.exception.code, "STRUCTURAL_EXEMPLAR_LEAK_BLOCKED")
            _danger_source, _danger_path, danger_profile = active_repeat_profile(root / "danger", dangerous=True)
            codes = {item["code"] for item in curation.validate_profile(danger_profile, require_active=True)["errors"]}
            self.assertIn("REPEATABLE_ROW_DANGEROUS_OOXML", codes)

    def test_hybrid_zero_one_many_and_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-hybrid-") as temporary:
            root = Path(temporary)
            source, profile_path, profile = active_hybrid_profile(root)
            self.assertTrue(curation.validate_profile(profile, require_active=True)["ok"])
            schema = json.loads((PROJECT_ROOT / "shared/schemas/writing-profile.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_against_schema(profile, schema), [])
            original_hash = sha256_file(source)
            for count in (0, 1, 3):
                bodies = [{"region_id": "MAIN-BODY", "paragraphs": [
                    {"paragraph_id": f"P-{i}", "role": "body", "text": f"TEST-ONLY新正文{i}。"} for i in range(1, count + 1)
                ]}]
                plan = curation.build_hybrid_plan(
                    profile_path, {"court": "TEST-ONLY新法院：", "applicant": "TEST-ONLY新申请人", "date": None}, bodies
                )
                plan_schema = json.loads((PROJECT_ROOT / "shared/schemas/hybrid-plan.schema.json").read_text(encoding="utf-8"))
                self.assertEqual(validate_against_schema(plan, plan_schema), [])
                output = root / f"hybrid-{count}.docx"
                result = curation.apply_structural_docx_plan(source, output, profile_path, plan)
                self.assertTrue(result["transform_aware_diff"]["ok"])
                self.assertEqual(sha256_file(source), original_hash)
                texts = document_texts(output)
                self.assertNotIn("TEST-ONLY OLD-CASE BODY", texts)
                self.assertEqual(sum(text.startswith("TEST-ONLY新正文") for text in texts), count)
                self.assertIn("TEST-ONLY新法院：", texts)
                self.assertIn("TEST-ONLY新申请人", texts)
                self.assertNotIn("TEST-ONLY OLD DATE", texts)

    def test_hybrid_bounds_dangerous_structure_leak_long_and_ooxml_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-hybrid-negative-") as temporary:
            root = Path(temporary)
            source, profile_path, _ = active_hybrid_profile(root)
            mechanical = {"court": "TEST-ONLY新法院：", "applicant": "TEST-ONLY新申请人", "date": None}
            with self.assertRaises(LegalCaseError) as raised:
                curation.build_hybrid_plan(profile_path, mechanical, [{"region_id": "MAIN-BODY", "paragraphs": [
                    {"paragraph_id": f"P-{i}", "role": "body", "text": "TEST-ONLY正文"} for i in range(4)
                ]}])
            self.assertEqual(raised.exception.code, "HYBRID_PARAGRAPH_COUNT_OUT_OF_RANGE")
            with self.assertRaises(LegalCaseError) as raised:
                curation.build_hybrid_plan(profile_path, mechanical, [{"region_id": "MAIN-BODY", "paragraphs": [
                    {"paragraph_id": "LONG", "role": "body", "text": "X" * 121}
                ]}])
            self.assertEqual(raised.exception.code, "STRUCTURAL_TEXT_TOO_LONG")
            with self.assertRaises(LegalCaseError) as raised:
                curation.build_hybrid_plan(profile_path, mechanical, [{"region_id": "MAIN-BODY", "paragraphs": [
                    {"paragraph_id": "XML", "role": "body", "text": "<w:instrText>evil</w:instrText>"}
                ]}])
            self.assertEqual(raised.exception.code, "DANGEROUS_OOXML_TEXT_BLOCKED")
            leak_plan = curation.build_hybrid_plan(profile_path, mechanical, [{"region_id": "MAIN-BODY", "paragraphs": [
                {"paragraph_id": "LEAK", "role": "body", "text": "TEST-ONLY OLD-CASE BODY"}
            ]}])
            with self.assertRaises(LegalCaseError) as raised:
                curation.apply_structural_docx_plan(source, root / "leak.docx", profile_path, leak_plan)
            self.assertEqual(raised.exception.code, "STRUCTURAL_EXEMPLAR_LEAK_BLOCKED")
            _danger_source, _danger_path, danger_profile = active_hybrid_profile(root / "danger", dangerous=True)
            codes = {item["code"] for item in curation.validate_profile(danger_profile, require_active=True)["errors"]}
            self.assertTrue({"HYBRID_DONOR_DANGEROUS_OOXML", "HYBRID_DONOR_RUN_INVALID"} & codes)


if __name__ == "__main__":
    unittest.main()
