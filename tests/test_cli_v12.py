#!/usr/bin/env python3
"""TEST-ONLY CLI contract regressions for Legal Case OS v1.2.

Every file created by this suite lives below a temporary directory.  The suite
does not install a plugin, call a network/API service, or execute a G5 action.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
CLI = SCRIPTS_ROOT / "legal_case_os.py"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import legal_case_os  # noqa: E402
from legal_case_os_lib.composition import build_composition_spec, build_exemplar_fingerprints  # noqa: E402
from legal_case_os_lib.core import make_empty_state, sha256_file  # noqa: E402


NEW_V12_COMMANDS = {
    "template-distill",
    "template-register",
    "template-fill-plan",
    "template-fill-docx",
    "composition-build",
    "composition-validate",
    "citation-audit",
    "exemplar-leak-check",
    "template-repeat-plan",
    "template-hybrid-plan",
    "template-structural-apply",
}

CONTENT_TYPES = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''

ROOT_RELS = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

STYLES = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>'''

DOCUMENT_XML = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>TEST-ONLY \xe8\x99\x9a\xe6\x9e\x84\xe6\xa8\xa1\xe6\x9d\xbf\xef\xbc\x8c\xe4\xb8\x8d\xe5\xbe\x97\xe6\x8f\x90\xe4\xba\xa4</w:t></w:r></w:p>
    <w:p><w:r><w:t>\xe4\xba\x89\xe8\xae\xae\xe7\x84\xa6\xe7\x82\xb9</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
  </w:body>
</w:document>'''


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_test_docx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", ROOT_RELS)
        package.writestr("word/document.xml", DOCUMENT_XML)
        package.writestr("word/styles.xml", STYLES)


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_cli(*args: str, expected_codes: tuple[int, ...] = (0,)) -> tuple[int, dict]:
    """Exercise the real argparse/main boundary without spawning another runtime.

    Some supported Windows runners host the tests in LibreOffice's bundled
    Python, whose ``sys.executable`` is not itself launchable.  Calling main is
    still the complete CLI contract (argument parsing, JSON emission and exit
    code), while remaining portable and deterministic.
    """

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        returncode = legal_case_os.main(list(args))
    if returncode not in expected_codes:
        raise AssertionError(
            f"CLI exit={returncode}; expected={expected_codes}\n"
            f"stdout={stdout.getvalue()}\nstderr={stderr.getvalue()}"
        )
    emitted = stdout.getvalue().strip()
    if not emitted:
        raise AssertionError(f"CLI did not emit JSON: {stderr.getvalue()}")
    return returncode, json.loads(emitted)


def assert_g5_zero(test: unittest.TestCase, result: dict, state_path: Path | None = None) -> None:
    test.assertEqual(result.get("external_actions_executed", 0), 0, result)
    if state_path is not None and state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        test.assertEqual(state.get("external_actions", []), [], "TEST-ONLY suite must never create G5 actions")


def write_legacy_state(workspace: Path, version: str) -> Path:
    """Create an empty, canonical TEST-ONLY v1.0/v1.1 state with no audit events."""

    state = make_empty_state(f"M-TEST-UPGRADE-{version.replace('.', '')}", f"TEST-ONLY {version} \u8fc1\u79fb\u6848", "test")
    state["schema_version"] = version
    state["audit"] = {"path": "_case-state/audit-log.jsonl", "last_sequence": 0, "last_event_hash": None}
    state["focus"]["state_version"] = 1
    state.pop("composition_specs", None)
    state["focus"].pop("current_composition_spec_id", None)
    if version == "1.0.0":
        for key in (
            "material_batches", "case_events", "triage_cards", "deadline_records",
            "impact_assessments", "prospective_matter_seeds", "workflow_instances",
            "run_attempts", "memory_state",
        ):
            state.pop(key, None)
        for key in (
            "current_batch_id", "current_event_id", "current_workflow_instance_id",
            "current_task_id", "current_memory_mode", "read_memory_scope",
            "state_version", "snapshot",
        ):
            state["focus"].pop(key, None)
    state_path = workspace / "_case-state" / "case-state.json"
    write_json(state_path, state)
    (state_path.parent / "audit-log.jsonl").write_text("", encoding="utf-8")
    (workspace / "00-originals").mkdir(parents=True, exist_ok=True)
    return state_path


class CliV12Tests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.composition_fixture = json.loads(
            (PROJECT_ROOT / "tests" / "fixtures" / "composition-v12" / "TEST-ONLY-composition-data.json")
            .read_text(encoding="utf-8")
        )

    def _composition_payload(self) -> dict:
        fixture = self.composition_fixture
        return {
            "test_only": True,
            "id": "CMP-TEST-CLI-001",
            "matter_id": "M-TEST-CLI-001",
            "document_type": "TEST-ONLY \u8d28\u8bc1\u610f\u89c1",
            "audience": "court_candidate",
            "template_candidates": copy.deepcopy(fixture["templates"]),
            "template_refs": ["8073", "8075"],
            "claim_bindings": copy.deepcopy(fixture["claim_bindings"]),
            "authority_ids": ["AUTH-SUPPORT-001", "AUTH-LAW-001", "AUTH-ADVERSE-001"],
            "dependencies": copy.deepcopy(fixture["dependencies"]),
            "adverse_treatments": copy.deepcopy(fixture["adverse_treatments"]),
            "risk_conflicts": copy.deepcopy(fixture["risk_conflicts"]),
            "created_at": "2026-08-25T00:00:00Z",
        }

    @staticmethod
    def _template_fill_data() -> dict:
        return {
            "plaintiff": "TEST-ONLY 测试原告（虚构）",
            "defendant": "TEST-ONLY 测试被告（虚构）",
            "cause_of_action": "TEST-ONLY 虚构买卖合同争议",
            "claims": "TEST-ONLY 虚构请求",
            "facts_and_reasons": "TEST-ONLY 虚构事实与理由",
            "evidence_summary": "TEST-ONLY 仅列入模拟证据",
            "court_name": "TEST-ONLY 测试法院（虚构）",
            "signature": "TEST-ONLY 不签名",
            "filing_date": "2026-08-25",
        }

    def test_command_inventory_includes_v12_and_standalone_commands(self) -> None:
        parser = legal_case_os.build_parser()
        subparsers = next(
            action for action in parser._actions  # noqa: SLF001 - argparse has no public command inventory API
            if isinstance(action, argparse._SubParsersAction)
        )
        commands = set(subparsers.choices)
        self.assertTrue({"material-import", "material-import-original", "material-read", "task-render", "learning-build", "learning-save", "learning-load"} <= commands)
        self.assertTrue(NEW_V12_COMMANDS <= commands)
        self.assertFalse({"print", "send", "upload", "serve", "file"} & commands)
        distill_parser = subparsers.choices["template-distill"]
        usage_action = next(action for action in distill_parser._actions if action.dest == "usage_mode")  # noqa: SLF001
        self.assertEqual(set(usage_action.choices or []), {"reference", "fillable_clone", "hybrid"})

    def test_init_writes_v12_and_never_creates_g5_action(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-v12-init-") as temporary:
            workspace = Path(temporary) / "TEST-ONLY-workspace"
            _, result = run_cli(
                "init", "--workspace", str(workspace), "--matter-id", "M-TEST-CLI-INIT-001",
                "--title", "TEST-ONLY CLI v1.2 \u521d\u59cb\u5316\u6848", "--environment", "test", "--json",
            )
            state_path = workspace / "_case-state" / "case-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], "1.2.0")
            self.assertEqual(state["composition_specs"], [])
            self.assertIsNone(state["focus"]["current_composition_spec_id"])
            assert_g5_zero(self, result, state_path)

    def test_v10_and_v11_upgrade_to_v12_and_repeat_is_idempotent(self) -> None:
        for legacy_version in ("1.0.0", "1.1.0"):
            with self.subTest(legacy_version=legacy_version), tempfile.TemporaryDirectory(
                prefix=f"TEST-ONLY-cli-upgrade-{legacy_version}-"
            ) as temporary:
                state_path = write_legacy_state(Path(temporary) / "TEST-ONLY-workspace", legacy_version)
                _, upgraded = run_cli(
                    "upgrade-state", "--state", str(state_path), "--expected-state-version", "1",
                    "--actor", "TEST-ONLY-lawyer", "--json",
                )
                self.assertFalse(upgraded["idempotent"])
                self.assertEqual(upgraded["schema_version"], "1.2.0")
                migrated = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(migrated["composition_specs"], [])
                self.assertIn("current_composition_spec_id", migrated["focus"])
                before_repeat = sha256_file(state_path)
                _, repeated = run_cli(
                    "upgrade-state", "--state", str(state_path),
                    "--expected-state-version", str(upgraded["state_version"]), "--json",
                )
                self.assertTrue(repeated["idempotent"])
                self.assertEqual(sha256_file(state_path), before_repeat)
                assert_g5_zero(self, upgraded, state_path)
                assert_g5_zero(self, repeated, state_path)

    def test_template_distill_only_writes_a_draft_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-distill-") as temporary:
            root = Path(temporary)
            source = root / "TEST-ONLY-reference.docx"
            make_test_docx(source)
            original_hash = sha256_file(source)
            output_dir = root / "TEST-ONLY-profiles"
            _, result = run_cli(
                "template-distill", "--source", str(source), "--output-dir", str(output_dir),
                "--profile-kind", "writing", "--usage-mode", "reference",
                "--template-id", "TPL-TEST-CLI-DRAFT", "--name", "TEST-ONLY \u53c2\u8003\u7a3f",
                "--document-type", "TEST-ONLY \u8d28\u8bc1\u610f\u89c1", "--json",
            )
            profile = result["profile"]
            self.assertEqual(profile["status"], "draft")
            self.assertFalse(result["activation_performed"])
            self.assertEqual(result["next_gate"], "lawyer_profile_approval")
            self.assertNotIn("approval", profile)
            self.assertNotIn("activated_at", profile)
            self.assertEqual(sha256_file(source), original_hash)
            self.assertEqual(list(output_dir.glob("*.json")), [Path(result["profile_path"])])
            assert_g5_zero(self, result)

    def test_legacy_template_fill_state_mode_requires_valid_v12_and_cas_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-template-fill-state-") as temporary:
            root = Path(temporary)
            data = json.dumps(self._template_fill_data(), ensure_ascii=False)

            invalid_workspace = root / "invalid-workspace"
            invalid_state_path = invalid_workspace / "_case-state" / "case-state.json"
            invalid_state = make_empty_state("M-TEST-FILL-INVALID-001", "TEST-ONLY 伪v1.2模板填充案", "test")
            invalid_state.pop("composition_specs")
            write_json(invalid_state_path, invalid_state)
            invalid_audit = invalid_state_path.parent / "audit-log.jsonl"
            invalid_audit.write_text("", encoding="utf-8")
            invalid_output = root / "invalid-output.md"
            _, rejected = run_cli(
                "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
                "--data", data, "--output", str(invalid_output),
                "--state", str(invalid_state_path), "--expected-state-version", "1",
                "--json", expected_codes=(2,),
            )
            self.assertEqual(rejected["code"], "CANDIDATE_STATE_INVALID")
            self.assertFalse(invalid_output.exists())
            self.assertEqual(invalid_audit.read_text(encoding="utf-8"), "")

            workspace = root / "valid-workspace"
            state_path = workspace / "_case-state" / "case-state.json"
            state = make_empty_state("M-TEST-FILL-VALID-001", "TEST-ONLY v1.2模板填充案", "test")
            write_json(state_path, state)
            audit_path = state_path.parent / "audit-log.jsonl"
            audit_path.write_text("", encoding="utf-8")

            missing_token_output = root / "missing-token.md"
            _, missing_token = run_cli(
                "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
                "--data", data, "--output", str(missing_token_output),
                "--state", str(state_path), "--json", expected_codes=(2,),
            )
            self.assertEqual(missing_token["code"], "STATE_VERSION_REQUIRED")
            self.assertFalse(missing_token_output.exists())
            self.assertEqual(audit_path.read_text(encoding="utf-8"), "")

            stale_output = root / "stale-token.md"
            _, stale = run_cli(
                "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
                "--data", data, "--output", str(stale_output),
                "--state", str(state_path), "--expected-state-version", "2",
                "--json", expected_codes=(2,),
            )
            self.assertEqual(stale["code"], "STALE_STATE_VERSION")
            self.assertFalse(stale_output.exists())
            self.assertEqual(audit_path.read_text(encoding="utf-8"), "")

            output = root / "valid-output.md"
            _, result = run_cli(
                "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
                "--data", data, "--output", str(output),
                "--state", str(state_path), "--expected-state-version", "1",
                "--json",
            )
            self.assertTrue(output.is_file())
            self.assertIsNotNone(result["audit_event_hash"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["focus"]["state_version"], 2)
            self.assertEqual(len(saved["composition_specs"]), 0)
            self.assertEqual(len(saved["artifacts"]), 1)
            self.assertEqual(len(audit_path.read_text(encoding="utf-8").splitlines()), 1)
            assert_g5_zero(self, result, state_path)

            race_output = root / "race-output.md"
            original_fill_template = legal_case_os.fill_template

            def fill_then_competing_commit(*fill_args, **fill_kwargs):
                fill_result = original_fill_template(*fill_args, **fill_kwargs)
                competing_old = legal_case_os.load_json(state_path)
                competing_new = copy.deepcopy(competing_old)
                competing_new["status"]["degradations"].append("TEST-ONLY-competing-write")
                legal_case_os.mutate_state(
                    state_path,
                    competing_new,
                    actor="TEST-ONLY-competing-writer",
                    command="competing-write",
                    event_type="competing_write",
                    old_state=competing_old,
                )
                return fill_result

            with mock.patch.object(
                legal_case_os,
                "fill_template",
                side_effect=fill_then_competing_commit,
            ):
                _, raced = run_cli(
                    "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
                    "--data", data, "--output", str(race_output),
                    "--state", str(state_path), "--expected-state-version", "2",
                    "--json", expected_codes=(2,),
                )
            self.assertEqual(raced["code"], "STALE_STATE_VERSION")
            self.assertFalse(race_output.exists())
            raced_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(raced_state["artifacts"]), 1)
            self.assertEqual(raced_state["focus"]["state_version"], 3)
            self.assertEqual(len(audit_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_personal_catalog_rejects_missing_profile_and_forged_active_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-catalog-") as temporary:
            library = Path(temporary) / "TEST-ONLY-library"
            registry = library / "_registry"
            template = library / "20-TEST-ONLY-complex" / "TEST-ONLY-template.docx"
            make_test_docx(template)
            base_entry = {
                "id": "TPL-TEST-PERSONAL-001",
                "name": "TEST-ONLY \u4e2a\u4eba\u6a21\u677f",
                "aliases": ["TEST-ONLY-001"],
                "document_type": "TEST-ONLY \u8d28\u8bc1\u610f\u89c1",
                "category": "TEST-ONLY",
                "path": "../20-TEST-ONLY-complex/TEST-ONLY-template.docx",
                "source": "TEST-ONLY synthetic",
                "version": "1.0.0",
                "sha256": sha256_file(template),
                "usage_mode": "reference",
                "status": "active",
                "approved_final": True,
                "authorization": {"status": "verified", "basis": "TEST-ONLY synthetic owner"},
                "reusable_aspects": ["structure"],
                "forbidden_transfer": [
                    "client_facts", "names", "dates", "amounts", "claims", "evidence",
                    "authorities", "conclusions",
                ],
            }
            catalog_path = registry / "template-catalog.json"
            write_json(catalog_path, {"catalog_version": "1.0.0", "templates": [base_entry], "suites": []})
            _, missing = run_cli(
                "template-ref-validate", "--catalog", str(catalog_path), "--json", expected_codes=(2,)
            )
            self.assertFalse(missing["ok"])
            self.assertTrue(any("profile" in error.casefold() or "\u753b\u50cf" in error for error in missing["errors"]))
            assert_g5_zero(self, missing)

            profile_dir = library / "_profiles"
            _, distilled = run_cli(
                "template-distill", "--source", str(template), "--output-dir", str(profile_dir),
                "--profile-kind", "writing", "--usage-mode", "reference",
                "--template-id", base_entry["id"], "--name", base_entry["name"],
                "--document-type", base_entry["document_type"], "--json",
            )
            profile_path = Path(distilled["profile_path"])
            forged = distilled["profile"]
            forged["status"] = "active"  # TEST-ONLY forgery: no approval or verified visual baseline.
            write_json(profile_path, forged)
            forged_entry = copy.deepcopy(base_entry)
            forged_entry.update({
                "profile_path": "../_profiles/" + profile_path.name,
                "profile_sha256": sha256_file(profile_path),
            })
            write_json(catalog_path, {"catalog_version": "1.0.0", "templates": [forged_entry], "suites": []})
            _, rejected = run_cli(
                "template-ref-validate", "--catalog", str(catalog_path), "--json", expected_codes=(2,)
            )
            self.assertFalse(rejected["ok"])
            self.assertTrue(any("\u6fc0\u6d3b" in error or "\u6279\u51c6" in error or "\u57fa\u7ebf" in error for error in rejected["errors"]))
            assert_g5_zero(self, distilled)
            assert_g5_zero(self, rejected)

    def test_composition_build_and_negative_validate_are_cli_visible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-composition-") as temporary:
            root = Path(temporary)
            payload_path = root / "TEST-ONLY-input.json"
            spec_path = root / "TEST-ONLY-spec.json"
            authorities_path = root / "TEST-ONLY-authorities.json"
            payload = self._composition_payload()
            write_json(payload_path, payload)
            write_json(authorities_path, {"authorities": self.composition_fixture["authorities"]})
            _, built = run_cli(
                "composition-build", "--input", str(payload_path), "--output", str(spec_path),
                "--test-mode", "--json"
            )
            self.assertEqual(built["spec"]["id"], payload["id"])
            self.assertTrue(spec_path.is_file())
            assert_g5_zero(self, built)

            invalid_spec = json.loads(spec_path.read_text(encoding="utf-8"))
            invalid_spec["authority_ids"].append("AUTH-LEAD-001")
            write_json(spec_path, invalid_spec)
            _, rejected = run_cli(
                "composition-validate", "--spec", str(spec_path), "--authorities", str(authorities_path),
                "--test-mode", "--json", expected_codes=(2,)
            )
            self.assertFalse(rejected["ok"])
            codes = {item["code"] for item in rejected["validation"]["errors"]}
            self.assertIn("authority_not_production_eligible", codes)
            self.assertEqual(rejected["spec"]["status"], "blocked")
            assert_g5_zero(self, rejected)

    def test_negative_citation_audit_blocks_unknown_case_number(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-citation-") as temporary:
            root = Path(temporary)
            document = root / "TEST-ONLY-court-candidate.txt"
            document.write_text("TEST-ONLY \u5f15\u7528\uff082022\uff09\u6e1d0105\u6c11\u521d999\u53f7\u3002\n", encoding="utf-8")
            authorities = root / "TEST-ONLY-authorities.json"
            write_json(authorities, {"authorities": self.composition_fixture["authorities"]})
            _, result = run_cli(
                "citation-audit", "--document", str(document), "--authorities", str(authorities),
                "--json", expected_codes=(2,)
            )
            self.assertFalse(result["ok"])
            self.assertIn("citation_not_registered", {item["code"] for item in result["findings"]})
            assert_g5_zero(self, result)

    def test_negative_exemplar_leak_check_blocks_old_case_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-exemplar-") as temporary:
            root = Path(temporary)
            document = root / "TEST-ONLY-new-draft.txt"
            document.write_text("TEST-ONLY \u65b0\u7a3f\u8bef\u5165\u65e7\u6848\u7532\u516c\u53f8\u3002\n", encoding="utf-8")
            fingerprints = build_exemplar_fingerprints(
                "TPL-TEST-OLD-001",
                {"name": ["\u65e7\u6848\u7532\u516c\u53f8"], "case_number": ["\uff082020\uff09\u6e1d01\u6c11\u7ec8123\u53f7"]},
            )
            fingerprint_path = root / "TEST-ONLY-fingerprints.json"
            write_json(fingerprint_path, {"fingerprints": fingerprints})
            _, result = run_cli(
                "exemplar-leak-check", "--document", str(document),
                "--fingerprints", str(fingerprint_path), "--json", expected_codes=(2,)
            )
            self.assertFalse(result["ok"])
            self.assertIn("name", {item["kind"] for item in result["findings"]})
            self.assertNotIn("\u65e7\u6848\u7532\u516c\u53f8", json.dumps(fingerprints, ensure_ascii=False))
            assert_g5_zero(self, result)

    def test_explicit_invalidate_propagates_to_spec_artifact_and_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-cli-invalidate-") as temporary:
            workspace = Path(temporary) / "TEST-ONLY-workspace"
            state_path = workspace / "_case-state" / "case-state.json"
            state = make_empty_state("M-TEST-INVALIDATE-001", "TEST-ONLY \u5931\u6548\u4f20\u64ad\u6848", "test")
            authority_id = "AUTH-TEST-UPSTREAM-001"
            spec_id = "CMP-TEST-INVALIDATE-001"
            artifact_id = "ART-TEST-INVALIDATE-001"
            package_id = "PKG-TEST-INVALIDATE-001"
            state["authorities"] = [{
                "id": authority_id,
                "title": "TEST-ONLY \u4e0a\u6e38\u6cd5\u6e90",
                "source_level": "L4_model_inference",
                "verification_status": "test_only",
                "production_eligible": False,
                "adverse": False,
                "environment": "test",
                "test_only": True,
                "stale": False,
            }]
            state["composition_specs"] = [build_composition_spec({
                "id": spec_id,
                "matter_id": state["matter"]["id"],
                "document_type": "TEST-ONLY \u8d28\u8bc1\u610f\u89c1",
                "audience": "internal_review",
                "test_mode": True,
                "template_candidates": [],
                "template_refs": [],
                "claim_bindings": [],
                "authority_ids": [authority_id],
                "adverse_treatments": [],
                "risk_conflicts": [],
                "dependencies": [{
                    "object_id": authority_id, "object_type": "authority", "version": 1,
                    "hash": "a" * 64, "required": True,
                }],
                "dependent_artifact_ids": [artifact_id],
                "created_at": "2026-08-25T00:00:00Z",
            })]
            artifact_file = workspace / "30-working" / "TEST-ONLY-draft.txt"
            artifact_file.parent.mkdir(parents=True, exist_ok=True)
            artifact_file.write_text("TEST-ONLY \u865a\u6784\u5185\u90e8\u7a3f\n", encoding="utf-8")
            state["artifacts"] = [{
                "id": artifact_id,
                "kind": "TEST-ONLY-draft",
                "audience": "internal_review",
                "version": 1,
                "path": "30-working/TEST-ONLY-draft.txt",
                "sha256": sha256_file(artifact_file),
                "status": "draft",
                "review_status": "not_reviewed",
                "input_snapshot": [{"object_id": spec_id, "version": 1, "hash": None}],
                "composition_spec_id": spec_id,
                "stale": False,
                "stale_reason": None,
                "created_at": "2026-08-25T00:00:00Z",
            }]
            package_items = [{
                "object_id": artifact_id, "object_type": "artifact", "version": 1,
                "hash": sha256_file(artifact_file), "path": "30-working/TEST-ONLY-draft.txt",
                "order": 1, "bookmark": "TEST-ONLY",
            }]
            state["package_manifests"] = [{
                "id": package_id,
                "version": 1,
                "manifest_hash": canonical_hash(package_items),
                "items": package_items,
                "status": "candidate",
                "stale": False,
                "blockers": [],
                "created_at": "2026-08-25T00:00:00Z",
            }]
            state["focus"]["current_composition_spec_id"] = spec_id
            write_json(state_path, state)
            (state_path.parent / "audit-log.jsonl").write_text("", encoding="utf-8")
            _, result = run_cli(
                "invalidate", "--state", str(state_path), "--object-id", authority_id,
                "--reason", "TEST-ONLY \u4e0a\u6e38\u6cd5\u6e90\u66f4\u65b0", "--json",
            )
            self.assertTrue({authority_id, spec_id, artifact_id, package_id} <= set(result["affected_ids"]))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["composition_specs"][0]["status"], "stale")
            self.assertTrue(saved["composition_specs"][0]["stale"])
            self.assertEqual(saved["artifacts"][0]["status"], "stale")
            self.assertTrue(saved["artifacts"][0]["stale"])
            self.assertEqual(saved["package_manifests"][0]["status"], "stale")
            self.assertTrue(saved["package_manifests"][0]["stale"])
            assert_g5_zero(self, result, state_path)

            # Regression: a Template used to fall through to generic
            # ``stale`` fields that the Template schema did not permit.  A
            # retired template version must remain a valid case-state object
            # and retain the exact invalidation reason.
            template_workspace = Path(temporary) / "TEST-ONLY-template-workspace"
            template_state_path = template_workspace / "_case-state" / "case-state.json"
            template_state = make_empty_state(
                "M-TEST-TEMPLATE-INVALIDATE-001",
                "TEST-ONLY 模板失效状态案",
                "test",
            )
            template_state["templates"] = [{
                "id": "TPL-TEST-INVALIDATE-001",
                "name": "TEST-ONLY 虚构模板",
                "document_type": "TEST-ONLY 文书",
                "path": "library/TEST-ONLY-template.docx",
                "source": "TEST-ONLY local fixture",
                "license": {
                    "id": "TEST-ONLY-personal",
                    "status": "verified",
                    "notice": "TEST-ONLY，不得提交",
                },
                "sha256": "b" * 64,
                "version": "1.0.0",
                "fixed_fields": [],
                "editable_fields": ["body"],
                "required_fields": [],
                "aliases": ["TEST-ONLY模板"],
                "usage_mode": "reference",
                "profile_id": None,
                "profile_path": None,
                "profile_sha256": None,
                "status": "active",
            }]
            write_json(template_state_path, template_state)
            (template_state_path.parent / "audit-log.jsonl").write_text("", encoding="utf-8")
            _, template_result = run_cli(
                "invalidate", "--state", str(template_state_path),
                "--object-id", "TPL-TEST-INVALIDATE-001",
                "--reason", "TEST-ONLY 模板版本已升级", "--json",
            )
            retired = json.loads(template_state_path.read_text(encoding="utf-8"))["templates"][0]
            self.assertEqual(retired["status"], "expired")
            self.assertTrue(retired["stale"])
            self.assertEqual(retired["stale_reason"], "TEST-ONLY 模板版本已升级")
            assert_g5_zero(self, template_result, template_state_path)


if __name__ == "__main__":
    unittest.main()
