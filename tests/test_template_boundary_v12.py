from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "template-boundary-v12"
MANIFEST = FIXTURE / "manifest.json"
BUILDER = ROOT / "tools" / "build_template_boundary_suite.py"
EXPECTED_SCENARIOS = {
    "normal-values",
    "explicit-empty",
    "max-party-name",
    "max-court-name",
    "large-amount",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def document_xml(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as archive:
        return archive.read("word/document.xml").decode("utf-8")


def workspace_python() -> Path:
    bundled = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "python"
        / "python.exe"
    )
    return bundled if bundled.is_file() else Path(sys.executable)


class TemplateBoundaryV12Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load(MANIFEST)
        cls.records = {item["scenario_id"]: item for item in cls.manifest["scenarios"]}

    def test_suite_is_test_only_complete_and_source_is_immutable(self) -> None:
        manifest = self.manifest
        self.assertTrue(manifest["test_only"])
        self.assertEqual(manifest["suite_id"], "TEST-ONLY-template-boundary-v12")
        self.assertEqual(manifest["scenario_count"], 5)
        self.assertEqual(set(self.records), EXPECTED_SCENARIOS)
        source = FIXTURE / manifest["source"]["path"]
        baseline = load(FIXTURE / manifest["source"]["baseline_path"])
        self.assertEqual(digest(source), baseline["sha256"])
        self.assertEqual(digest(source), manifest["source"]["sha256"])
        self.assertTrue(manifest["source"]["immutable_after_first_build"])
        self.assertTrue(manifest["source"]["unchanged_during_run"])
        self.assertFalse(manifest["source"]["lawyer_approved_template"])

    def test_all_five_derived_docx_pdf_and_png_sets_are_hash_verified(self) -> None:
        self.assertTrue(self.manifest["all_machine_checks_passed"])
        for record in self.records.values():
            docx = FIXTURE / record["output_docx"]["path"]
            pdf = FIXTURE / record["render"]["pdf"]["path"]
            pages = record["render"]["pages"]
            self.assertTrue(docx.is_file())
            self.assertTrue(pdf.is_file())
            self.assertEqual(digest(docx), record["output_docx"]["sha256"])
            self.assertEqual(digest(pdf), record["render"]["pdf"]["sha256"])
            self.assertEqual(record["render"]["page_count"], len(pages))
            self.assertGreater(len(pages), 0)
            self.assertTrue(record["all_machine_checks_passed"])
            self.assertTrue(all(item["status"] == "pass" for item in record["structural_checks"].values()))
            self.assertTrue(all(item["status"] == "pass" for item in record["machine_checks"].values()))
            for page in pages:
                page_path = FIXTURE / page["path"]
                self.assertTrue(page_path.is_file())
                self.assertEqual(digest(page_path), page["sha256"])

    def test_explicit_empty_is_blank_and_no_placeholder_survives(self) -> None:
        record = self.records["explicit-empty"]
        self.assertEqual(record["explicit_blank_fields"], ["optional_note"])
        self.assertEqual(record["input_values"]["optional_note"], "")
        xml = document_xml(FIXTURE / record["output_docx"]["path"])
        self.assertIn("可选备注", xml)
        self.assertNotIn("[[OPTIONAL_NOTE]]", xml)
        self.assertNotIn("仅用于TEST-ONLY离线模板测试", xml)
        for item in self.records.values():
            xml = document_xml(FIXTURE / item["output_docx"]["path"])
            self.assertNotIn("[[", xml)
            self.assertNotIn("]]", xml)

    def test_visual_review_is_ai_only_and_filing_gate_remains_closed(self) -> None:
        review = self.manifest["visual_review"]
        self.assertEqual(review["status"], "ai_visual_review_complete_pending_lawyer_approval")
        self.assertEqual(review["reviewer_type"], "ai")
        self.assertFalse(review["lawyer_approval"])
        self.assertFalse(review["lawyer_approval_substituted"])
        self.assertFalse(review["filing_use_allowed"])
        self.assertFalse(self.manifest["release_gate"]["lawyer_visual_approval_present"])
        self.assertFalse(self.manifest["release_gate"]["filing_use_allowed"])
        self.assertFalse(self.manifest["release_gate"]["template_activation_allowed"])
        self.assertEqual(self.manifest["release_gate"]["external_actions_executed"], 0)
        reviewed = {(item["scenario_id"], item["page_number"], item["sha256"]) for item in review["pages"]}
        expected = {
            (record["scenario_id"], page["page_number"], page["sha256"])
            for record in self.records.values()
            for page in record["render"]["pages"]
        }
        self.assertEqual(reviewed, expected)

    def test_builder_is_hash_idempotent_without_rerendering(self) -> None:
        tracked = sorted(path for path in FIXTURE.rglob("*") if path.is_file())
        before = {path.relative_to(FIXTURE).as_posix(): digest(path) for path in tracked}
        clean_env = os.environ.copy()
        # A host PYTHONPATH can force the bundled interpreter to resolve the
        # wrong package tree when it is launched from another Python process.
        for key in list(clean_env):
            if key.upper().startswith("PYTHON"):
                clean_env.pop(key, None)
        completed = subprocess.run(
            [str(workspace_python()), str(BUILDER), "--skip-render"],
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=f"{completed.stdout}\n{completed.stderr}")
        after_paths = sorted(path for path in FIXTURE.rglob("*") if path.is_file())
        after = {path.relative_to(FIXTURE).as_posix(): digest(path) for path in after_paths}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
