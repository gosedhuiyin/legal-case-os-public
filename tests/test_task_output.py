"""Real DOCX output and source-boundary tests using TEST-ONLY synthetic material."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib import task_output
from legal_case_os_lib.core import LegalCaseError, sha256_file
from legal_case_os_lib.documents import extract_docx_text
from legal_case_os_lib.materials import ingest_material, read_material


def fixture(root: Path):
    """Reusable render specimen; no real person, case, authority or legal claim."""
    source_dir = root / "synthetic-sources"
    source_dir.mkdir(parents=True)
    inputs = [
        ("template", "TEST-ONLY 申请书结构为申请事项、事实说明、结尾。旧案甲公司的金额为888888元。"),
        ("current_case", "TEST-ONLY 本次申请人为测试甲公司。待调取记录的期间为2026年1月1日至1月31日。记录标识为TEST-ORDER-001。"),
        ("case", "TEST-ONLY 比较材料将记录范围与具体争点对应；范围过宽可能需要进一步说明。此段是虚构分析示例，并非真实裁判。"),
    ]
    records = []
    for role, value in inputs:
        path = source_dir / (role + ".txt")
        path.write_text(value, encoding="utf-8")
        records.append(ingest_material(path, root / "material-snapshots", role))
    template, current, old_case = records
    current_text = read_material(current)["text"]
    quote = "待调取记录的期间为2026年1月1日至1月31日。记录标识为TEST-ORDER-001。"
    start = current_text.index(quote)
    binding = {"id": current["id"], "start": start, "end": start + len(quote), "quote": quote}
    proposal = {
        "title": "测试材料调取申请书", "audience": "internal_review", "mode": "simple",
        "template_material_id": template["id"],
        "template_use": {"applied_patterns": ["沿用申请事项、事实说明、结尾的组织顺序"]},
        "sections": [
            {"kind": "administrative", "text": "致测试受理单位"},
            {"kind": "heading", "text": "申请事项"},
            {"kind": "fact", "text": "本次拟调取TEST-ORDER-001对应的2026年1月1日至1月31日记录。", "source_bindings": [binding]},
            {"kind": "heading", "text": "事实说明"},
            {"kind": "argument", "text": "申请范围以明确的记录标识和期间为限，便于核对材料。", "source_bindings": [binding]},
            {"kind": "administrative", "text": "请对上述申请予以审核。"},
        ],
        "exemplar_exclusions": ["旧案甲公司", "888888"],
        "unresolved_items": ["本文件仅用于功能和版式测试，不对应真实案件。"],
    }
    return records, proposal


def complex_proposal(records, simple):
    proposal = copy.deepcopy(simple)
    proposal["mode"] = "complex"
    proposal["title"] = "测试材料调取申请分析稿"
    old_case = records[2]
    text = read_material(old_case)["text"]
    quote = "范围过宽可能需要进一步说明。"
    start = text.index(quote)
    binding = {"id": old_case["id"], "start": start, "end": start + len(quote), "quote": quote}
    proposal["sections"].insert(-1, {"kind": "analogy", "text": "所附比较材料提示，应说明记录范围与争点的联系。",
        "source_bindings": [binding], "verification_status": "test_only"})
    proposal["analysis"] = {"issues": ["记录范围是否足够明确"],
        "source_comparison": ["比较材料提供论证方式，本案期间和标识来自本次材料"],
        "strongest_adverse_path": ["缺少记录与争点之间的具体联系"],
        "application_conditions": ["先确认标识和期间确与待证明事项对应"],
        "conclusion_limits": ["虚构样例不能支持真实案件的法律结论"], "source_bindings": [binding]}
    return proposal


class TaskOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records, self.simple = fixture(self.root)

    def error(self, code, proposal=None, records=None):
        with self.assertRaises(LegalCaseError) as caught:
            task_output.validate_task_draft(self.records if records is None else records,
                                            self.simple if proposal is None else proposal)
        self.assertEqual(caught.exception.code, code)

    def test_simple_real_docx_and_honest_review_state(self):
        original = copy.deepcopy(self.simple)
        result = task_output.render_task_draft(self.records, self.simple, self.root / "simple-output")
        path = Path(result["docx"])
        self.assertTrue(zipfile.is_zipfile(path))
        with zipfile.ZipFile(path) as package:
            self.assertIsNone(package.testzip())
            self.assertIn("word/document.xml", package.namelist())
        text = extract_docx_text(path)
        self.assertIn("测试材料调取申请书", text)
        self.assertIn("TEST-ORDER-001", text)
        self.assertNotIn("888888", text)
        self.assertNotIn("MAT-", text)
        self.assertNotIn("本文件仅用于功能和版式测试", text)
        self.assertEqual(self.simple, original)
        self.assertEqual(result["checks"]["visual_review"], "required")
        self.assertEqual(result["checks"]["substantive_review"], "required")
        self.assertFalse(result["checks"]["filing_approved"])
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["draft.docx"], sha256_file(path))
        self.assertFalse(manifest["database_required"])
        self.assertEqual(manifest["model_calls_executed"], 0)

    def test_complex_real_docx_with_separate_bound_analysis(self):
        proposal = complex_proposal(self.records, self.simple)
        result = task_output.render_task_draft(self.records, proposal, self.root / "complex-output")
        self.assertTrue(zipfile.is_zipfile(result["docx"]))
        self.assertIn("比较材料提示", extract_docx_text(Path(result["docx"])))
        review = Path(result["review"]).read_text(encoding="utf-8")
        self.assertIn("最强反方路径", review)
        self.assertIn("虚构样例不能支持真实案件", review)
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["analysis"]["source_bindings"][0]["source_sha256"], self.records[2]["source_sha256"])

    def test_old_template_and_case_cannot_supply_current_fact(self):
        for record in (self.records[0], self.records[2]):
            proposal = copy.deepcopy(self.simple)
            text = read_material(record)["text"]
            proposal["sections"][2]["source_bindings"] = [{"id": record["id"], "start": 0, "end": len(text), "quote": text}]
            self.error("TASK_OLD_FACT_TRANSFER", proposal)

    def test_exact_quote_and_source_id_required(self):
        proposal = copy.deepcopy(self.simple)
        proposal["sections"][2]["source_bindings"][0]["quote"] += "伪造"
        self.error("TASK_QUOTE_MISMATCH", proposal)
        proposal = copy.deepcopy(self.simple)
        proposal["sections"][2]["source_bindings"][0]["id"] = "MAT-DOES-NOT-EXIST"
        self.error("TASK_SOURCE_NOT_FOUND", proposal)

    def test_unsourced_statement_requires_described_assumption(self):
        proposal = copy.deepcopy(self.simple)
        proposal["sections"][2]["source_bindings"] = []
        self.error("TASK_BASIS_REQUIRED", proposal)
        proposal["sections"][2]["assumption"] = True
        self.error("TASK_ASSUMPTION_INVALID", proposal)
        proposal["sections"][2]["assumption"] = "须补充能够核对记录期间的材料。"
        result = task_output.render_task_draft(self.records, proposal, self.root / "assumption-output")
        self.assertIn("须补充能够核对", Path(result["review"]).read_text(encoding="utf-8"))
        self.assertEqual(result["checks"]["substantive_review"], "required")

    def test_complex_requires_analysis_and_its_sources(self):
        proposal = copy.deepcopy(self.simple)
        proposal["mode"] = "complex"
        self.error("TASK_COMPLEX_ANALYSIS_REQUIRED", proposal)
        proposal = complex_proposal(self.records, self.simple)
        proposal["analysis"]["source_bindings"] = []
        self.error("TASK_ANALYSIS_SOURCES_REQUIRED", proposal)

    def test_excluded_old_information_is_blocked_in_body_and_title(self):
        proposal = copy.deepcopy(self.simple)
        proposal["sections"][-1]["text"] += "旧案甲公司"
        self.error("TASK_EXEMPLAR_LEAK", proposal)
        proposal = copy.deepcopy(self.simple)
        proposal["title"] = "旧案甲公司申请书"
        self.error("TASK_EXEMPLAR_LEAK", proposal)

    def test_output_must_not_overwrite_or_write_source_library(self):
        output = self.root / "already-exists"
        output.mkdir()
        sentinel = output / "draft.docx"
        sentinel.write_bytes(b"DO NOT CHANGE")
        with self.assertRaises(LegalCaseError) as caught:
            task_output.render_task_draft(self.records, self.simple, output)
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(sentinel.read_bytes(), b"DO NOT CHANGE")
        for directory in (self.root / "library" / "new", self.root / "00-originals" / "new"):
            with self.assertRaises(LegalCaseError):
                task_output.render_task_draft(self.records, self.simple, directory)
            self.assertFalse(directory.exists())

    def test_broken_source_snapshot_and_duplicate_ids_are_blocked(self):
        self.error("TASK_MATERIALS_INVALID", records=self.records + [self.records[0]])
        Path(self.records[1]["text_path"]).write_text("tampered", encoding="utf-8")
        self.error("TASK_SOURCE_INVALID")

    def test_bad_json_shapes_return_structured_errors(self):
        for field, value, code in (("title", {}, "TASK_TITLE_REQUIRED"), ("sections", ["text"], "TASK_SECTION_INVALID"),
                                   ("mode", [], "TASK_MODE_INVALID"),
                                   ("exemplar_exclusions", "old", "TASK_EXCLUSION_INVALID"),
                                   ("unresolved_items", "unknown", "TASK_UNRESOLVED_INVALID")):
            proposal = copy.deepcopy(self.simple)
            proposal[field] = value
            self.error(code, proposal)
        proposal = copy.deepcopy(self.simple)
        proposal["sections"][2]["source_bindings"] = ["bad"]
        self.error("TASK_BINDINGS_INVALID", proposal)
        proposal = complex_proposal(self.records, self.simple)
        proposal["sections"][-2]["verification_status"] = []
        self.error("TASK_AUTHORITY_STATUS_REQUIRED", proposal)
        self.error("TASK_MATERIALS_INVALID", records=[{}])

    def test_failed_docx_write_never_publishes_output(self):
        output = self.root / "failed-output"
        with mock.patch.object(task_output, "create_docx_from_markdown", side_effect=OSError("synthetic write error")):
            with self.assertRaises(OSError):
                task_output.render_task_draft(self.records, self.simple, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".task-draft-*")), [])

    def test_court_candidate_status_cannot_be_self_granted(self):
        proposal = copy.deepcopy(self.simple)
        proposal["audience"] = "court_candidate"
        self.error("TASK_INTERNAL_DRAFT_ONLY", proposal)

    def test_cli_task_render_creates_real_docx_and_returns_json(self):
        import legal_case_os
        proposal_path = self.root / "TEST-ONLY-proposal.json"
        proposal_path.write_text(json.dumps(self.simple, ensure_ascii=False), encoding="utf-8")
        args = ["task-render", "--proposal", str(proposal_path),
                "--output-dir", str(self.root / "cli-output"), "--json"]
        for record in self.records:
            args.extend(["--material-record", record["record_path"]])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exit_code = legal_case_os.main(args)
        result = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["ok"])
        self.assertTrue(zipfile.is_zipfile(result["docx"]))
        self.assertEqual(result["checks"]["visual_review"], "required")


if __name__ == "__main__":
    unittest.main()
