"""Synthetic source links: no real library scans, network or database."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib.core import LegalCaseError
from legal_case_os_lib import source_links


class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sha = "a" * 64
        self.profile_path = self.root / "profiles/p.json"
        self.catalog_path = self.root / "registry/catalog.json"
        self.map_path = self.root / "map.json"
        self.profile = {"template_id": "T1", "profile_id": "P1", "source": {"sha256": self.sha},
                        "status": "active", "usage_mode": "reference", "approval": {"target_version": "1"}}
        digest = self.write(self.profile_path, self.profile)
        self.entry = {"id": "T1", "version": "1", "status": "active", "approved_final": True,
                      "authorization": {"status": "verified"}, "usage_mode": "reference", "sha256": self.sha,
                      "profile_path": "../profiles/p.json", "profile_sha256": digest}
        self.catalog = {"templates": [self.entry]}
        catalog_sha = self.write(self.catalog_path, self.catalog)
        self.binding = {"active_template_id": "T1", "template_version": "1", "usage_mode": "reference",
                        "active_source_sha256": self.sha, "profile_sop_relative_path": "profiles/p.json",
                        "catalog_profile_sha256": digest, "actual_profile_sha256": digest}
        self.mapping = {"scope": {"catalog_sop_relative_path": "registry/catalog.json", "catalog_sha256": catalog_sha},
                        "matches": [{"sha256": self.sha, "knowledge_relative_path": "library/test.docx",
                                     "active_template_bindings": [self.binding]}]}
        self.write(self.map_path, self.mapping)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value).encode()
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def resolve(self):
        return source_links.resolve_source_links(self.sha, self.map_path, self.root)

    def assert_rejected(self, code):
        value = self.resolve()
        self.assertEqual(value["verified_links"], [])
        self.assertEqual(value["rejected_links"][0]["code"], code)

    def test_same_bytes_current_profile_without_source_original(self):
        result = self.resolve()
        self.assertEqual(len(result["verified_links"]), 1)
        self.assertTrue(result["catalog_matches_map_snapshot"])
        self.assertFalse(result["production_approval_checked"])
        self.assertEqual(result["verified_links"][0]["profile_path"], str(self.profile_path.resolve()))

    def test_profile_bytes_changed(self):
        self.profile_path.write_text("{}")
        self.assert_rejected("SOURCE_LINK_PROFILE_CHANGED")

    def test_current_catalog_inactive_or_new_version(self):
        for update, code in [({"status": "withdrawn"}, "SOURCE_LINK_INACTIVE"),
                             ({"status": "active", "version": "2"}, "SOURCE_LINK_CATALOG_CHANGED")]:
            self.entry.update(update)
            self.write(self.catalog_path, self.catalog)
            with self.subTest(update=update):
                self.assert_rejected(code)

    def test_profile_identity_checked_even_when_all_hashes_updated(self):
        self.profile["source"]["sha256"] = "b" * 64
        digest = self.write(self.profile_path, self.profile)
        self.entry["profile_sha256"] = self.binding["catalog_profile_sha256"] = self.binding["actual_profile_sha256"] = digest
        self.write(self.catalog_path, self.catalog)
        self.write(self.map_path, self.mapping)
        self.assert_rejected("SOURCE_LINK_PROFILE_IDENTITY")

    def test_path_escape_rejected_before_profile_read(self):
        self.entry["profile_path"] = "../../outside.json"
        self.write(self.catalog_path, self.catalog)
        self.assert_rejected("SOURCE_LINK_PATH_INVALID")

    def test_unrelated_catalog_change_does_not_break_current_entry(self):
        self.catalog["notice"] = "TEST-ONLY metadata revision"
        self.write(self.catalog_path, self.catalog)
        result = self.resolve()
        self.assertEqual(len(result["verified_links"]), 1)
        self.assertFalse(result["catalog_matches_map_snapshot"])

    def test_malformed_authorization_denies_link_without_crashing(self):
        self.entry["authorization"] = None
        self.write(self.catalog_path, self.catalog)
        self.assert_rejected("SOURCE_LINK_INACTIVE")

    def test_unmatched_hash_is_not_full_library_absence(self):
        result = source_links.resolve_source_links("b" * 64, self.map_path, self.root)
        self.assertEqual(result["matched_map_rows"], 0)
        self.assertEqual(result["verified_links"], [])
        for value in (None, True, "", "a" * 63, "x" * 64):
            with self.subTest(value=value), self.assertRaises(LegalCaseError) as error:
                source_links.resolve_source_links(value, self.map_path, self.root)
            self.assertEqual(error.exception.code, "SOURCE_LINK_HASH_INVALID")

    def test_concurrent_catalog_change_invalidates_returned_links(self):
        original = source_links._read
        def read(path):
            result = original(path)
            if path == self.profile_path:
                self.catalog["notice"] = "changed while reading"
                self.write(self.catalog_path, self.catalog)
            return result
        with mock.patch.object(source_links, "_read", side_effect=read), self.assertRaises(LegalCaseError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "SOURCE_LINK_METADATA_CHANGED")


if __name__ == "__main__":
    unittest.main()
