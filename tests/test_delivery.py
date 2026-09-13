"""Synthetic byte snapshots exercise version integrity and pointer transactions."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib import delivery
from legal_case_os_lib.core import LegalCaseError, sha256_file


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "deliveries"
        self.task = self.fixture("task-one", b"TEST-ONLY DOCX bytes\r\n\x00")

    def fixture(self, name, data):
        directory = self.root / name
        directory.mkdir()
        (directory / "draft.docx").write_bytes(data)
        (directory / "review.md").write_text("TEST-ONLY 未完成实质及版式复核。", encoding="utf-8")
        manifest = {"files": {name: sha256_file(directory / name) for name in ("draft.docx", "review.md")},
                    "checks": {"visual_review": "required", "substantive_review": "required", "filing_approved": False}}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return directory

    def publish(self, task=None, expected=None):
        return delivery.publish_delivery(task or self.task, self.store, expected_current=expected)

    def assert_error(self, code, call):
        with self.assertRaises(LegalCaseError) as caught:
            call()
        self.assertEqual(caught.exception.code, "DELIVERY_" + code)

    def change_manifest(self, change):
        path = self.task / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        change(manifest)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_registers_original_bytes_and_actual_review_limitations(self):
        result = self.publish()
        for name in ("draft.docx", "review.md", "manifest.json"):
            self.assertEqual((Path(result["version_dir"]) / name).read_bytes(), (self.task / name).read_bytes())
        self.assertEqual(result["checks"]["visual_review"], "required")
        self.assertFalse(result["filing_approved"])
        self.assertEqual(result["external_actions_executed"], 0)
        self.assertEqual(delivery.get_current_delivery(self.store)["revision"], result["revision"])

    def test_same_publication_is_idempotent(self):
        first = self.publish()
        second = self.publish(expected=first["revision"])
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(second["operation"], "unchanged")
        self.assertEqual(len(list((self.store / "versions").iterdir())), 1)

    def test_restore_old_bytes_without_overwriting_user_edited_task(self):
        first = self.publish()
        second_task = self.fixture("task-two", b"different TEST-ONLY version")
        second = self.publish(second_task, first["revision"])
        (self.task / "draft.docx").write_bytes(b"user edited source")
        restored = delivery.restore_delivery(self.store, first["version_id"], expected_current=second["revision"])
        self.assertEqual(Path(restored["files"]["draft.docx"]).read_bytes(), b"TEST-ONLY DOCX bytes\r\n\x00")
        self.assertEqual((self.task / "draft.docx").read_bytes(), b"user edited source")
        self.assertNotEqual(restored["revision"], first["revision"])
        self.assertTrue(Path(second["files"]["draft.docx"]).is_file())

    def test_stale_and_aba_tokens_cannot_override_current(self):
        first = self.publish()
        task_two = self.fixture("task-two", b"two")
        self.assert_error("STALE_CURRENT", lambda: self.publish(task_two))
        second = self.publish(task_two, first["revision"])
        restored = delivery.restore_delivery(self.store, first["version_id"], expected_current=second["revision"])
        self.assert_error("STALE_CURRENT", lambda: self.publish(task_two, first["revision"]))
        self.assertEqual(delivery.get_current_delivery(self.store)["revision"], restored["revision"])

    def test_tampered_source_cannot_create_current(self):
        (self.task / "draft.docx").write_bytes(b"changed after manifest")
        self.assert_error("HASH_MISMATCH", self.publish)
        self.assertFalse((self.store / "current.json").exists())
        self.assertEqual(list(self.store.glob(".delivery-*")), [])

    def test_missing_independent_review_is_rejected(self):
        self.change_manifest(lambda manifest: manifest["files"].pop("review.md"))
        self.assert_error("INVALID_MANIFEST", self.publish)

    def test_external_paths_and_case_insensitive_collisions_are_rejected(self):
        for name in ("../secret.md", "C:/secret.md", "folder/secret.md", "x:secret.md", ".env", "DRAFT.docx"):
            with self.subTest(name=name):
                self.change_manifest(lambda m: m["files"].update({name: "a" * 64}))
                self.assert_error("UNSAFE_FILE", self.publish)
                self.change_manifest(lambda m: m["files"].pop(name))

    def test_protected_output_and_overlapping_directories(self):
        for folder in ("library", "00-originals", "secrets"):
            self.assert_error("PROTECTED_PATH", lambda: delivery.publish_delivery(
                self.task, self.root / folder / "deliveries", expected_current=None))
        self.assert_error("OVERLAPPING_PATHS", lambda: delivery.publish_delivery(
            self.task, self.task / "delivery", expected_current=None))

    def test_change_during_copy_keeps_previous_current(self):
        first = self.publish()
        task_two = self.fixture("task-two", b"second version")
        original = delivery._copy_verified
        def racing_copy(source, target, expected_hash):
            original(source, target, expected_hash)
            if source.name == "review.md":
                (task_two / "draft.docx").write_bytes(b"changed during later copy")
        with mock.patch.object(delivery, "_copy_verified", side_effect=racing_copy):
            self.assert_error("SOURCE_CHANGED", lambda: self.publish(task_two, first["revision"]))
        self.assertEqual(delivery.get_current_delivery(self.store)["revision"], first["revision"])

    def test_pointer_failure_keeps_complete_old_version_and_retry_recovers(self):
        first = self.publish()
        task_two = self.fixture("task-two", b"second version")
        with mock.patch.object(delivery, "atomic_write_json", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.publish(task_two, first["revision"])
        self.assertEqual(delivery.get_current_delivery(self.store)["revision"], first["revision"])
        result = self.publish(task_two, first["revision"])
        self.assertEqual(Path(result["files"]["draft.docx"]).read_bytes(), b"second version")
        self.assertEqual(len(list((self.store / "versions").iterdir())), 2)

    def test_restore_checks_old_version_integrity(self):
        first = self.publish()
        second = self.publish(self.fixture("task-two", b"two"), first["revision"])
        Path(first["files"]["draft.docx"]).write_bytes(b"edited history")
        self.assert_error("HASH_MISMATCH", lambda: delivery.restore_delivery(
            self.store, first["version_id"], expected_current=second["revision"]))
        self.assertEqual(delivery.get_current_delivery(self.store)["revision"], second["revision"])

    def test_current_edited_by_user_is_never_silently_replaced(self):
        first = self.publish()
        Path(first["files"]["draft.docx"]).write_bytes(b"user edited current")
        self.assert_error("HASH_MISMATCH", lambda: self.publish(
            self.fixture("task-two", b"two"), first["revision"]))
        self.assertEqual(Path(first["files"]["draft.docx"]).read_bytes(), b"user edited current")

    def test_path_alias_is_rejected_when_platform_supports_symlinks(self):
        external = self.root / "external.docx"
        external.write_bytes(b"private")
        linked = self.task / "draft.docx"
        linked.unlink()
        try:
            linked.symlink_to(external)
        except OSError:
            self.skipTest("Host does not permit symlink creation")
        self.assert_error("LINK_PATH", self.publish)

    def test_hardlinked_lock_cannot_modify_another_file(self):
        self.store.mkdir()
        original = self.root / "original-empty.txt"
        original.write_bytes(b"")
        os.link(original, self.store / ".delivery.lock")
        self.assert_error("LINK_PATH", self.publish)
        self.assertEqual(original.read_bytes(), b"")

    def test_self_declared_approval_does_not_grant_filing_approval(self):
        self.change_manifest(lambda m: m["checks"].update({"filing_approved": True}))
        result = self.publish()
        self.assertTrue(result["checks"]["filing_approved"])
        self.assertFalse(result["filing_approved"])
        self.assertEqual(result["model_calls_executed"], 0)


if __name__ == "__main__":
    unittest.main()
