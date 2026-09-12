"""Behavioral routing checks for file-only drafting and explicit filing scope."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import legal_case_os


class MaterialRoutingTests(unittest.TestCase):
    def setUp(self):
        self.state = json.loads((ROOT / "tests/fixtures/simple-case/_case-state/case-state.json").read_text(encoding="utf-8"))
        self.records = [{"id": "MAT-T", "role": "template", "origin": "session_upload", "source_sha256": "a" * 64},
                        {"id": "MAT-C", "role": "current_case", "origin": "session_upload", "source_sha256": "b" * 64}]

    def test_uploaded_template_draft_needs_no_case_state_registration_or_filing_approval(self):
        result = legal_case_os.route_text("按上传模板生成申请书", input_materials=self.records)
        self.assertEqual(result["task_frame"]["delivery_audience"], "internal_review")
        self.assertIn(result["clarification"]["decision"], {"execute", "preview"})
        self.assertNotIn("G1_strategy", result["run_spec"]["validators"])
        self.assertNotIn("G2_evidence", result["run_spec"]["validators"])
        self.assertNotIn("registered_template_profiles", result["run_spec"]["required_inputs"])
        self.assertIn("local_derived_write", result["run_spec"]["allowed_tools"])
        self.assertEqual(result["intent"]["output_formats"], ["docx"])

    def test_filing_request_keeps_gates_and_no_external_action(self):
        result = legal_case_os.route_text("生成给法院的起诉状提交稿", self.state)
        self.assertEqual(result["task_frame"]["delivery_audience"], "court_candidate")
        self.assertTrue({"G1_strategy", "G2_evidence"} <= set(result["run_spec"]["validators"]))
        self.assertFalse(result["run_spec"]["external_actions_allowed"])
        self.assertNotIn("G4_final", result["run_spec"]["human_review_points"])
        from legal_case_os_lib.language import validate_language_control_semantics
        result["run_spec"]["validators"].remove("G1_strategy")
        self.assertTrue(validate_language_control_semantics(result))

    def test_file_only_work_does_not_dispatch_library(self):
        result = legal_case_os.route_text("只用上传材料，参考模板生成起诉状，不调用MCP", input_materials=self.records)
        self.assertEqual(result["task_frame"]["knowledge_policy"], "off")
        self.assertNotIn("configured_library_read", result["run_spec"]["allowed_tools"])

    def test_blank_framework_does_not_require_invented_case_facts(self):
        result = legal_case_os.route_text("生成起诉状，只出空白框架")
        self.assertEqual(result["route"]["action"], "generate")
        self.assertIn(result["clarification"]["decision"], {"execute", "preview"})

    def test_embedded_tail_instruction_requires_boundary_resolution(self):
        result = legal_case_os.route_text("材料如下：甲欠乙十元。请据此起草起诉状。")
        self.assertEqual(result["clarification"]["decision"], "ask")
        self.assertIn("material_command_boundary", result["task_frame"]["missing_required"])

    def test_complex_uploaded_sources_keep_analysis_without_permanent_template_gate(self):
        result = legal_case_os.route_text("参考上传模板，深入分析案情和反方路径，再生成起诉状", input_materials=self.records)
        self.assertEqual(result["task_frame"]["delivery_audience"], "internal_review")
        self.assertIn("strongest_adverse_path", result["run_spec"]["required_artifacts"])
        self.assertNotIn("composition_spec", result["run_spec"]["required_artifacts"])


if __name__ == "__main__":
    unittest.main()
