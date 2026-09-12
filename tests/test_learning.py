#!/usr/bin/env python3
"""TEST-ONLY: source-bound, task-local and portable learning behavior."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from legal_case_os_lib.core import LegalCaseError  # noqa: E402
from legal_case_os_lib.learning import _new_json, approve_learning, build_learning_candidate, load_learning  # noqa: E402
from legal_case_os_lib.materials import ingest_material  # noqa: E402


class LearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="TEST-ONLY-learning-")
        self.root = Path(self.temporary.name).resolve()
        self.original = self.root / "TEST-ONLY-source.txt"
        self.text = "TEST-ONLY 合成材料。\n先说明争点，再列双方证据，最后写明结论成立的条件。\n对方材料不能仅因与己方观点相反而忽略。\n"
        self.original.write_text(self.text, encoding="utf-8")
        self.material = ingest_material(self.original, self.root / "task", role="exemplar", origin="session_upload")
        # Use the actual extracted text because each adapter defines its own text normalization.
        self.extracted = Path(self.material["text_path"]).read_text(encoding="utf-8")
        self.quote = "先说明争点，再列双方证据，最后写明结论成立的条件。"
        self.start = self.extracted.index(self.quote)
        self.proposal = {
            "id": "LEARN-TEST-001", "title": "TEST-ONLY 合成论证方法",
            "reasoning_patterns": [{
                "id": "PATTERN-TEST-001", "text": "先界定争点，再比较证据，最后给出有条件结论。",
                "applies_when": ["争议同时存在双方可定位材料。"],
                "not_applicable_when": ["仅机械填写身份栏而不涉及争议判断。"],
                "source_bindings": [{"id": self.material["id"], "start": self.start,
                                     "end": self.start + len(self.quote), "quote": self.quote}],
            }],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, proposal=None, name="candidate.json"):
        return build_learning_candidate([self.material], proposal or self.proposal, self.root / "task" / name)

    def confirmation(self, result):
        return {"decision": "approved", "object_id": result["candidate"]["id"],
                "candidate_sha256": result["candidate_sha256"], "actor": "TEST-ONLY-lawyer",
                "confirmed_at": datetime.now(timezone.utc).isoformat(), "reuse_reviewed": True}

    def approve(self, result, confirmation=None):
        return approve_learning(Path(result["candidate_path"]), self.root / "TEST-ONLY-library",
                                confirmation or self.confirmation(result))

    def assert_error(self, code, operation):
        with self.assertRaises(LegalCaseError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)

    def test_candidate_is_immediately_usable_without_long_term_write(self):
        before = self.original.read_bytes()
        result = self.build()
        self.assertTrue(result["applicable_now"])
        self.assertFalse(result["persisted_to_library"])
        self.assertFalse((self.root / "TEST-ONLY-library").exists())
        self.assertEqual(before, self.original.read_bytes())
        self.assertFalse(result["candidate"]["validation"]["semantic_truth_verified_by_script"])
        self.assertFalse(result["candidate"]["validation"]["model_training_performed"])
        self.assertEqual(hashlib.sha256(Path(result["candidate_path"]).read_bytes()).hexdigest(), result["candidate_sha256"])

    def test_all_learning_writers_refuse_library_and_originals_before_creating_paths(self):
        result = self.build()
        for protected_name in ("library", "00-originals", "LIBRARY", "00-ORIGINALS"):
            protected = self.root / protected_name
            output = protected / "nested" / "candidate.json"
            self.assert_error("LEARNING_OUTPUT_PROTECTED", lambda: build_learning_candidate([self.material], self.proposal, output))
            self.assert_error("LEARNING_OUTPUT_PROTECTED", lambda: approve_learning(Path(result["candidate_path"]), protected / "assets", self.confirmation(result)))
            self.assert_error("LEARNING_OUTPUT_PROTECTED", lambda: _new_json(output, {"TEST-ONLY": True}))
            self.assertFalse(protected.exists())

    def test_unsupported_semantic_guess_and_missing_sources_are_rejected(self):
        empty = {"id": "LEARN-TEST-001", "title": "TEST-ONLY"}
        self.assert_error("LEARNING_EMPTY", lambda: self.build(empty))
        unbound = copy.deepcopy(self.proposal)
        unbound["reasoning_patterns"][0]["source_bindings"] = []
        self.assert_error("LEARNING_SOURCE_REQUIRED", lambda: self.build(unbound))

    def test_quote_must_equal_exact_source_characters(self):
        wrong = copy.deepcopy(self.proposal)
        wrong["reasoning_patterns"][0]["source_bindings"][0]["quote"] = "错" + self.quote[1:]
        self.assert_error("LEARNING_QUOTE_MISMATCH", lambda: self.build(wrong))
        wrong["reasoning_patterns"][0]["source_bindings"][0]["start"] = True
        self.assert_error("LEARNING_INVALID_LOCATOR", lambda: self.build(wrong))

    def test_tampered_material_snapshot_cannot_create_candidate(self):
        Path(self.material["text_path"]).write_text("TEST-ONLY 变化后的正文", encoding="utf-8")
        self.assert_error("LEARNING_SOURCE_INVALID", lambda: self.build())

    def test_unrelated_unavailable_source_does_not_block_selected_learning(self):
        unrelated = {"id": "TEST-ONLY-unused-source", "snapshot_path": str(self.root / "missing.txt")}
        result = build_learning_candidate([self.material, unrelated], self.proposal, self.root / "task" / "selected.json")
        self.assertEqual([self.material["id"]], [ref["id"] for ref in result["candidate"]["material_refs"]])

    def test_viewpoint_requires_speaker_conditions_and_explicit_status(self):
        proposal = {"id": "LEARN-TEST-VIEW", "title": "TEST-ONLY 条件性观点", "viewpoints": [copy.deepcopy(self.proposal["reasoning_patterns"][0])]}
        view = proposal["viewpoints"][0]
        self.assert_error("LEARNING_REQUIRED_FIELD", lambda: self.build(proposal))
        view.update({"speaker": "TEST-ONLY 合成作者", "speaker_role": "代理意见作者",
                     "original_view": "应同时分析双方证据。", "rule_premises": ["这是论证方法，非已核验法条。"],
                     "fact_premises": ["双方材料均可定位。"], "counterarguments_or_limits": ["不能推断对方材料当然可信。"]})
        self.assert_error("LEARNING_VIEWPOINT_STATUS", lambda: self.build(proposal))
        view["verification_status"] = "source_position_only"
        result = self.build(proposal)
        self.assertEqual("source_position_only", result["candidate"]["learning"]["viewpoints"][0]["verification_status"])

    def test_approval_requires_current_exact_human_confirmation(self):
        result = self.build()
        self.assert_error("LEARNING_CONFIRMATION_REQUIRED", lambda: approve_learning(Path(result["candidate_path"]), self.root / "TEST-ONLY-library", {}))
        for field, value in (("candidate_sha256", "0" * 64), ("object_id", "OTHER-TEST")):
            receipt = self.confirmation(result)
            receipt[field] = value
            self.assert_error("LEARNING_CONFIRMATION_MISMATCH", lambda: self.approve(result, receipt))
        receipt = self.confirmation(result)
        receipt["actor"] = "assistant"
        self.assert_error("LEARNING_HUMAN_CONFIRMATION_REQUIRED", lambda: self.approve(result, receipt))
        receipt = self.confirmation(result)
        receipt["reuse_reviewed"] = False
        self.assert_error("LEARNING_REUSE_REVIEW_REQUIRED", lambda: self.approve(result, receipt))
        receipt = self.confirmation(result)
        receipt["confirmed_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assert_error("LEARNING_CONFIRMATION_STALE", lambda: self.approve(result, receipt))
        self.assertFalse((self.root / "TEST-ONLY-library").exists())

    def test_approval_refuses_changed_source_after_candidate(self):
        result = self.build()
        Path(self.material["snapshot_path"]).write_text("TEST-ONLY modified original snapshot", encoding="utf-8")
        self.assert_error("LEARNING_SOURCE_STALE", lambda: self.approve(result))

    def test_live_local_original_change_is_not_hidden_by_immutable_snapshot(self):
        result = self.build()
        asset = self.approve(result)
        self.original.write_text("TEST-ONLY 原文件的新版本", encoding="utf-8")
        self.assert_error("LEARNING_SOURCE_STALE", lambda: self.approve(result))
        self.assert_error("LEARNING_SOURCE_STALE", lambda: load_learning(Path(asset["asset_path"])))

    def test_source_record_changes_are_not_hidden_by_unchanged_text(self):
        result = self.build()
        asset = self.approve(result)
        record_path = Path(self.material["record_path"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["role"] = "TEST-ONLY-changed-role"
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.assert_error("LEARNING_SOURCE_STALE", lambda: self.approve(result))
        self.assert_error("LEARNING_SOURCE_STALE", lambda: load_learning(Path(asset["asset_path"])))

    def test_versions_preserve_old_asset_and_reject_old_confirmation(self):
        first = self.build()
        one = self.approve(first)
        old = Path(one["asset_path"]).read_bytes()
        newer = copy.deepcopy(self.proposal)
        newer["reasoning_patterns"][0]["text"] = "先明确结论需要的前提，再组织双方证据和回应。"
        second = self.build(newer, "candidate-v2.json")
        self.assert_error("LEARNING_CONFIRMATION_MISMATCH", lambda: self.approve(second, self.confirmation(first)))
        two = self.approve(second)
        self.assertEqual((1, 2), (one["version"], two["version"]))
        self.assertEqual(old, Path(one["asset_path"]).read_bytes())
        self.assertNotEqual(one["asset_path"], two["asset_path"])
        self.assert_error("LEARNING_PATH_EXISTS", lambda: self.build())

    def test_portable_load_with_original_sources_and_database_absent(self):
        result = self.build()
        asset = self.approve(result)
        version_dir = Path(asset["asset_path"]).parent
        portable = self.root / "TEST-ONLY-portable"
        shutil.copytree(version_dir, portable)
        # All these exact files are inside the test TemporaryDirectory.
        for field in ("snapshot_path", "text_path", "record_path"):
            path = Path(self.material[field]).resolve()
            self.assertTrue(path.is_relative_to(self.root))
            path.unlink()
        self.original.unlink()
        loaded = load_learning(portable / "learning.json")
        self.assertEqual("offline_snapshot_only", loaded["source_freshness"][0]["status"])
        self.assertFalse(loaded["source_freshness"][0]["live_database_checked"])
        self.assertEqual(self.quote, loaded["source_excerpts"][0]["text"])
        self.assertTrue(loaded["warnings"])
        source_copies = list((portable / "source-excerpts").glob("*"))
        self.assertEqual(1, len(source_copies))
        self.assertNotIn("TEST-ONLY 合成材料。", source_copies[0].read_text(encoding="utf-8"))
        self.assertFalse(any(path.name.startswith("source.") for path in portable.rglob("*")))

    def test_load_refuses_stale_source_and_tampered_portable_excerpt(self):
        result = self.build()
        asset = self.approve(result)
        path = Path(asset["asset_path"])
        loaded = load_learning(path)
        self.assertEqual("snapshot_matches", loaded["source_freshness"][0]["status"])
        snapshot = Path(self.material["snapshot_path"])
        original_bytes = snapshot.read_bytes()
        snapshot.write_bytes(b"TEST-ONLY changed")
        self.assert_error("LEARNING_SOURCE_STALE", lambda: load_learning(path))
        snapshot.write_bytes(original_bytes)
        excerpt = path.parent / asset["asset"]["source_excerpts"][0]["path"]
        excerpt.write_text("TEST-ONLY changed excerpt", encoding="utf-8")
        self.assert_error("LEARNING_EXCERPT_CHANGED", lambda: load_learning(path))

    def test_candidate_and_tampered_approval_are_not_active_learning(self):
        result = self.build()
        self.assert_error("LEARNING_NOT_APPROVED", lambda: load_learning(Path(result["candidate_path"])))
        asset = self.approve(result)
        path = Path(asset["asset_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["confirmation"]["actor"] = "TEST-ONLY forged"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assert_error("LEARNING_ASSET_CHANGED", lambda: load_learning(path))

    def test_portable_asset_cannot_read_an_excerpt_outside_its_version(self):
        result = self.build()
        asset = self.approve(result)
        path = Path(asset["asset_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_excerpts"][0]["path"] = "../outside.txt"
        body = {key: value for key, value in data.items() if key != "content_sha256"}
        canonical = (json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        data["content_sha256"] = hashlib.sha256(canonical).hexdigest()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assert_error("LEARNING_EXCERPT_PATH", lambda: load_learning(path))

    def test_missing_portable_excerpt_is_a_failure_even_when_original_available(self):
        result = self.build()
        asset = self.approve(result)
        path = Path(asset["asset_path"])
        (path.parent / asset["asset"]["source_excerpts"][0]["path"]).unlink()
        self.assert_error("LEARNING_EXCERPT_MISSING", lambda: load_learning(path))

    def test_long_raw_excerpt_requires_smaller_source_selection(self):
        proposal = copy.deepcopy(self.proposal)
        binding = proposal["reasoning_patterns"][0]["source_bindings"][0]
        binding.update({"start": 0, "end": 301, "quote": "甲" * 301})
        self.assert_error("LEARNING_EXCERPT_TOO_LONG", lambda: self.build(proposal))


if __name__ == "__main__":
    unittest.main()
