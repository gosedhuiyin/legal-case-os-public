#!/usr/bin/env python3
"""TEST-ONLY regressions: stale evidence history and competing memory publishers."""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import legal_case_os as cli  # noqa: E402
from legal_case_os_lib.core import (  # noqa: E402
    LegalCaseError, load_json, sha256_file, state_content_hash,
    validate_semantics, validate_state_file,
)


class StateReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="TEST-ONLY-state-reliability-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def clone(self, fixture):
        workspace = self.root / fixture
        shutil.copytree(ROOT / "tests" / "fixtures" / fixture, workspace)
        return workspace, workspace / "_case-state" / "case-state.json"

    def memory_args(self, state_path, output, actor, version=1):
        return SimpleNamespace(state=str(state_path), output=str(output), actor=actor, expected_state_version=version)

    def assert_valid(self, state_path):
        result = validate_state_file(state_path)
        self.assertTrue(result["ok"], result["errors"])

    def test_source_invalidation_preserves_decisions_but_clears_entire_current_set(self):
        _workspace, state_path = self.clone("simple-case")
        self.assert_valid(state_path)
        before = load_json(state_path)
        previous_current = {item["id"] for item in before["evidence"] if item["current_submission"]}
        self.assertGreater(len(previous_current), 1)
        previous_decisions = {item["id"]: (item["lawyer_decision"], item["decision_reason"]) for item in before["evidence"]}
        g2_before = next(item for item in before["approvals"] if item["gate"] == "G2_evidence" and item["status"] == "active")
        result = cli.command_invalidate(SimpleNamespace(
            state=str(state_path), source_id="S-TEST-SIMPLE-002", new_hash="a" * 64,
            object_id=None, reason="TEST-ONLY 来源新版本", actor="TEST-ONLY-lawyer"))
        self.assertTrue(result["ok"])
        saved = load_json(state_path)
        self.assertFalse(any(item["current_submission"] for item in saved["evidence"]))
        for evidence in saved["evidence"]:
            self.assertEqual(previous_decisions[evidence["id"]], (evidence["lawyer_decision"], evidence["decision_reason"]))
            if evidence["id"] in previous_current:
                self.assertEqual("stale", evidence["status"])
                self.assertFalse(evidence["reserve"])
                self.assertFalse(evidence["internal_reference"])
        old_g2 = next(item for item in saved["approvals"] if item["id"] == g2_before["id"])
        self.assertEqual("stale", old_g2["status"])
        self.assertEqual("approved", old_g2["decision"])
        self.assertEqual(g2_before["scope_snapshot"], old_g2["scope_snapshot"])
        self.assertEqual(g2_before["scope_hash"], old_g2["scope_hash"])
        self.assert_valid(state_path)

    def test_stale_evidence_cannot_keep_active_flags(self):
        _workspace, state_path = self.clone("simple-case")
        state = load_json(state_path)
        evidence = next(item for item in state["evidence"] if item["current_submission"])
        evidence["status"] = "stale"
        errors = validate_semantics(state)
        self.assertTrue(any("失效证据" in error for error in errors))

    def test_stale_composition_keeps_approval_history_without_requiring_live_readiness(self):
        _workspace, state_path = self.clone("simple-case")
        state = load_json(state_path)
        bindings = [{"approval_id": item["id"], "gate": item["gate"], "object_id": item["object_id"],
                     "object_hash": item["object_hash"], "scope_hash": item["scope_hash"]}
                    for item in state["approvals"] if item["gate"] in {"G1_strategy", "G2_evidence"} and item["status"] == "active"]
        spec = {"id": "CMP-TEST-STALE-HISTORY", "matter_id": state["matter"]["id"], "status": "ready",
                "stale": False, "blockers": [], "template_roles": {},
                "trusted_binding": {"mode": "catalog_case_state", "matter_id": state["matter"]["id"],
                                    "approval_bindings": bindings, "template_bindings": []},
                "dependencies": [{"object_id": "S-TEST-SIMPLE-002", "object_type": "source"}],
                "authority_ids": [], "claim_bindings": []}
        state["composition_specs"] = [spec]
        artifact = copy.deepcopy(state["artifacts"][0])
        artifact.update({"id": "R-TEST-STALE-COMPOSED", "audience": "court_candidate", "status": "reviewed",
                         "review_status": "passed", "stale": False, "input_snapshot": [],
                         "composition_spec_id": spec["id"]})
        state["artifacts"].append(artifact)
        self.assertEqual([], validate_semantics(state))
        cli._mark_stale(state, {"S-TEST-SIMPLE-002"}, "TEST-ONLY source changed")
        self.assertEqual("stale", spec["status"])
        self.assertTrue(artifact["stale"])
        self.assertEqual([], validate_semantics(state))
        # Marking the historical artifact live again must still fail readiness.
        artifact["stale"] = False
        artifact["status"] = "reviewed"
        self.assertTrue(any("合成清单未就绪或已失效" in error for error in validate_semantics(state)))

    def test_cas_loser_cannot_overwrite_winner_view_or_alias(self):
        workspace, state_path = self.clone("incremental-case")
        output = workspace / "_case-state" / "memory" / "TEST-ONLY-current.md"
        loser_read = threading.Event()
        winner_done = threading.Event()
        loser_result = {}
        original_render = cli.render_current_case_view

        def render(state):
            if threading.current_thread().name == "TEST-ONLY-loser":
                loser_read.set()
                if not winner_done.wait(10):
                    raise RuntimeError("TEST-ONLY winner timeout")
                return original_render(state) + "\nTEST-ONLY loser text\n"
            return original_render(state) + "\nTEST-ONLY winner text\n"

        def run_loser():
            try:
                loser_result["value"] = cli.command_memory_view(self.memory_args(state_path, output, "TEST-ONLY-loser"))
            except Exception as exc:
                loser_result["error"] = exc

        with mock.patch.object(cli, "render_current_case_view", side_effect=render):
            thread = threading.Thread(target=run_loser, name="TEST-ONLY-loser")
            thread.start()
            self.assertTrue(loser_read.wait(10))
            try:
                winner = cli.command_memory_view(self.memory_args(state_path, output, "TEST-ONLY-winner"))
                winner_state_hash = state_content_hash(load_json(state_path))
                winner_alias = output.read_bytes()
            finally:
                winner_done.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(loser_result.get("error"), LegalCaseError)
        self.assertEqual("STALE_STATE_VERSION", loser_result["error"].code)
        self.assertEqual(winner_alias, output.read_bytes())
        self.assertIn(b"winner text", winner_alias)
        self.assertEqual(winner["sha256"], sha256_file(Path(winner["path"])))
        self.assertEqual(winner["sha256"], sha256_file(output))
        self.assertEqual(winner_state_hash, state_content_hash(load_json(state_path)))
        current = next(item for item in load_json(state_path)["artifacts"] if item["id"] == winner["artifact_id"])
        self.assertEqual(winner["sha256"], sha256_file(workspace / current["path"]))
        self.assert_valid(state_path)

    def test_late_successful_publisher_cannot_replace_newer_alias(self):
        workspace, state_path = self.clone("incremental-case")
        output = workspace / "_case-state" / "memory" / "TEST-ONLY-current.md"
        first_committed = threading.Event()
        newer_published = threading.Event()
        first_result = {}
        original_mutate = cli.mutate_state
        original_render = cli.render_current_case_view

        def mutate(*args, **kwargs):
            result = original_mutate(*args, **kwargs)
            if kwargs.get("actor") == "TEST-ONLY-first":
                first_committed.set()
                if not newer_published.wait(10):
                    raise RuntimeError("TEST-ONLY newer publisher timeout")
            return result

        def render(state):
            return original_render(state) + f"\nTEST-ONLY version-{state['focus']['state_version']}\n"

        def run_first():
            try:
                first_result["value"] = cli.command_memory_view(self.memory_args(state_path, output, "TEST-ONLY-first"))
            except Exception as exc:
                first_result["error"] = exc

        with mock.patch.object(cli, "mutate_state", side_effect=mutate), mock.patch.object(cli, "render_current_case_view", side_effect=render):
            thread = threading.Thread(target=run_first, name="TEST-ONLY-first")
            thread.start()
            self.assertTrue(first_committed.wait(10))
            try:
                current_version = load_json(state_path)["focus"]["state_version"]
                newer = cli.command_memory_view(self.memory_args(state_path, output, "TEST-ONLY-newer", current_version))
                newer_alias = output.read_bytes()
            finally:
                newer_published.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", first_result)
        first = first_result["value"]
        self.assertFalse(first["alias_updated"])
        self.assertTrue(newer["alias_updated"])
        self.assertEqual(newer_alias, output.read_bytes())
        self.assertEqual(newer["sha256"], sha256_file(output))
        self.assertEqual(first["sha256"], sha256_file(Path(first["path"])))
        self.assertNotEqual(first["path"], newer["path"])
        self.assert_valid(state_path)

    def test_version_file_cannot_be_reused_as_mutable_output_alias(self):
        workspace, state_path = self.clone("incremental-case")
        output = workspace / "_case-state" / "memory" / "TEST-ONLY-current.md"
        first = cli.command_memory_view(self.memory_args(state_path, output, "TEST-ONLY-lawyer"))
        state = load_json(state_path)
        with self.assertRaises(LegalCaseError) as caught:
            cli.command_memory_view(self.memory_args(state_path, Path(first["path"]), "TEST-ONLY-lawyer", state["focus"]["state_version"]))
        self.assertEqual("MEMORY_OUTPUT_IS_VERSION", caught.exception.code)
        self.assertEqual(first["sha256"], sha256_file(Path(first["path"])))
        self.assertEqual(state_content_hash(state), state_content_hash(load_json(state_path)))


if __name__ == "__main__":
    unittest.main()
