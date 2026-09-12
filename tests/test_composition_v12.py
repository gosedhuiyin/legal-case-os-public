#!/usr/bin/env python3
"""TEST-ONLY v1.2 multi-template composition and routing regressions."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import legal_case_os  # noqa: E402
from legal_case_os_lib.composition import (  # noqa: E402
    audit_citations,
    build_composition_spec,
    build_exemplar_fingerprints,
    build_trusted_composition_spec,
    check_exemplar_leaks,
    detect_composition_staleness,
    mark_composition_stale,
    select_template_roles,
    synchronize_trusted_templates,
    validate_composition_spec,
    validate_artifact_claim_map,
    verify_composition_trust_binding,
)
from legal_case_os_lib.core import (  # noqa: E402
    LegalCaseError,
    canonical_json,
    load_json,
    make_empty_state,
    object_version_hash,
    sha256_bytes,
    validate_against_schema,
)


class CompositionV12Tests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "composition-v12" / "TEST-ONLY-composition-data.json"
        )
        cls.state = load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "simple-case" / "_case-state" / "case-state.json"
        )

    def _payload(self) -> dict:
        context = copy.deepcopy(self.fixture["composition_context"])
        return {
            "id": "CMP-TEST-001",
            "matter_id": "M-TEST-SIMPLE-001",
            "document_type": "evidence_objection",
            "audience": "court_candidate",
            **context,
            "template_candidates": copy.deepcopy(self.fixture["templates"]),
            "template_refs": ["8073", "8075"],
            "claim_bindings": copy.deepcopy(self.fixture["claim_bindings"]),
            "authority_ids": ["AUTH-SUPPORT-001", "AUTH-LAW-001", "AUTH-ADVERSE-001"],
            "dependencies": copy.deepcopy(self.fixture["dependencies"]),
            "adverse_treatments": copy.deepcopy(self.fixture["adverse_treatments"]),
            "risk_conflicts": copy.deepcopy(self.fixture["risk_conflicts"]),
            "created_at": "2026-08-25T00:00:00Z",
        }

    def _trusted_state(self) -> dict:
        state = make_empty_state("M-TRUST-001", "TEST-ONLY可信绑定案", "test")
        g1_object = {"id": "DEC-TRUST-G1", "subject_id": state["matter"]["id"], "status": "approved", "reason": "TEST-ONLY G1", "version": 1}
        g2_object = {"id": "DEC-TRUST-G2", "subject_id": state["matter"]["id"], "status": "approved", "reason": "TEST-ONLY G2", "version": 1}
        state["decisions"] = [g1_object, g2_object]
        source = {
            "id": "SRC-TRUST-001", "kind": "document", "path": "TEST-ONLY/source.pdf",
            "sha256": "1" * 64, "version": 1, "source_level": "original_material",
            "ingestion_status": "indexed", "coverage_id": None, "test_only": True,
        }
        state["sources"] = [source]

        def approval(identifier: str, gate: str, target: dict, scope: list[dict]) -> dict:
            version, digest = object_version_hash(target)
            snapshots = []
            for item in scope:
                scope_version, scope_digest = object_version_hash(item)
                snapshots.append({"object_id": item["id"], "version": scope_version, "hash": scope_digest})
            return {
                "id": identifier, "gate": gate, "object_id": target["id"],
                "object_version": version, "object_hash": digest,
                "scope_snapshot": snapshots,
                "scope_hash": sha256_bytes(canonical_json(snapshots).encode("utf-8")),
                "stage": state["matter"]["stage"], "decision": "approved",
                "reason": "TEST-ONLY", "actor": "TEST-ONLY-lawyer",
                "decided_at": "2026-08-25T00:00:00Z", "status": "active",
                "invalidation_reason": None,
            }

        state["approvals"] = [
            approval("APR-TRUST-G1", "G1_strategy", g1_object, [g1_object]),
            approval("APR-TRUST-G2", "G2_evidence", g2_object, [source]),
        ]
        return state

    def _trusted_payload(self) -> dict:
        return {
            "id": "CMP-TRUST-001", "matter_id": "M-TRUST-001",
            "document_type": "civil_complaint", "audience": "internal_review",
            "template_refs": ["简模001"],
            "template_candidates": [{"id": "TPL-FORGED", "status": "active", "sha256": "f" * 64}],
            "gate_snapshots": {
                "G1_strategy": {"approval_id": "APR-TRUST-G1", "object_hash": "f" * 64},
                "G2_evidence": {"approval_id": "APR-TRUST-G2", "object_hash": "f" * 64},
            },
            "claim_bindings": [], "authority_ids": [], "dependencies": [],
            "dangerous_issues": [], "section_bindings": [],
            "adverse_treatments": [], "risk_conflicts": [],
            "created_at": "2026-08-25T00:00:00Z",
        }

    def test_composite_phrase_routes_to_generation_with_research_first(self) -> None:
        result = legal_case_os.route_text(
            "参考8073和8075的风格，结合网络案例生成质证意见",
            copy.deepcopy(self.state),
        )
        self.assertEqual(result["route"]["action"], "generate")
        self.assertEqual(result["task_frame"]["task_kind"], "generate")
        self.assertEqual(result["intent"]["template_refs"], ["8073", "8075"])
        self.assertIsNone(result["intent"]["template_alias"])
        self.assertTrue(result["task_frame"]["authority_research_precondition"])
        self.assertEqual(result["task_frame"]["network_policy"], "public_web_if_available")
        self.assertIn("verified_public_web_read", result["run_spec"]["allowed_tools"])
        phases = result["run_spec"]["workflow_phases"]
        self.assertLess(phases.index("research_public_authorities"), phases.index("draft_from_task_materials"))
        self.assertEqual(result["task_frame"]["delivery_audience"], "internal_review")
        self.assertIn("material_adverse_authority_coverage", result["run_spec"]["validators"])
        self.assertIn("citation_audit", result["run_spec"]["validators"])
        self.assertIn("exemplar_leak_check", result["run_spec"]["validators"])

    def test_legacy_single_template_alias_is_preserved_and_promoted(self) -> None:
        result = legal_case_os.route_text("按照简模001模板生成起诉状", copy.deepcopy(self.state))
        self.assertEqual(result["intent"]["template_alias"], "简模001")
        self.assertEqual(result["intent"]["template_refs"], ["简模001"])

    def test_template_roles_are_deterministic_and_auxiliaries_are_bounded(self) -> None:
        explicit = select_template_roles(self.fixture["templates"], ["8073", "8075"])
        self.assertEqual(explicit["layout_template_id"], "TPL-8073")
        self.assertEqual(explicit["structure_template_id"], "TPL-8075")
        self.assertEqual(explicit["auxiliary_templates"], [])

        automatic = select_template_roles(self.fixture["templates"])
        self.assertEqual(automatic["layout_template_id"], "TPL-8073")
        self.assertEqual(automatic["structure_template_id"], "TPL-8075")
        self.assertEqual(len(automatic["auxiliary_templates"]), 3)
        self.assertTrue(all(item["section_targets"] for item in automatic["auxiliary_templates"]))

        unresolved = select_template_roles(self.fixture["templates"], ["不存在的模板"])
        self.assertEqual(unresolved["unresolved_refs"], ["不存在的模板"])
        self.assertIsNone(unresolved["layout_template_id"])

        statusless = copy.deepcopy(self.fixture["templates"][0])
        statusless.pop("status")
        not_implicitly_active = select_template_roles([statusless], ["8073"])
        self.assertEqual(not_implicitly_active["unresolved_refs"], ["8073"])

    def test_valid_spec_passes_schema_and_semantic_gates(self) -> None:
        spec = build_composition_spec(self._payload())
        schema = load_json(PROJECT_ROOT / "shared" / "schemas" / "composition-spec.schema.json")
        self.assertEqual(validate_against_schema(spec, schema), [])
        result = validate_composition_spec({
            "spec": spec,
            "authorities": self.fixture["authorities"],
            "production": False,
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ready")

    def test_trusted_build_rebinds_catalog_and_current_gates_and_detects_drift(self) -> None:
        state = self._trusted_state()
        catalog = PROJECT_ROOT / "tests" / "fixtures" / "template-reference-cases" / "_registry" / "TEST-ONLY-personal-template-catalog.json"
        spec, snapshots = build_trusted_composition_spec(self._trusted_payload(), state, catalog)
        self.assertEqual(spec["template_roles"]["layout_template_id"], "TPL-P-S-001")
        self.assertNotIn("TPL-FORGED", json.dumps(spec, ensure_ascii=False))
        self.assertNotEqual(spec["gate_snapshots"]["G1_strategy"]["object_hash"], "f" * 64)
        self.assertEqual(spec["trusted_binding"]["mode"], "catalog_case_state")
        synchronize_trusted_templates(state, snapshots)
        self.assertTrue(verify_composition_trust_binding(spec, state, catalog)["ok"])
        state["approvals"][0]["status"] = "stale"
        drift = verify_composition_trust_binding(spec, state, catalog)
        self.assertFalse(drift["ok"])
        self.assertIn("trusted_gate_approval_changed", {item["code"] for item in drift["errors"]})

    def test_package_preflight_rechecks_live_template_profile_and_catalog_binding(self) -> None:
        fixture_root = PROJECT_ROOT / "tests" / "fixtures" / "template-reference-cases"
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-package-live-binding-") as temporary:
            root = Path(temporary)
            library_root = root / "template-reference-cases"
            shutil.copytree(fixture_root, library_root)
            catalog_path = library_root / "_registry" / "TEST-ONLY-personal-template-catalog.json"

            state = self._trusted_state()
            spec, snapshots = build_trusted_composition_spec(self._trusted_payload(), state, catalog_path)
            synchronize_trusted_templates(state, snapshots)
            spec.update({
                "status": "ready", "blockers": [], "stale": False,
                "stale_reason": None, "validated_at": "2026-08-25T00:01:00Z",
            })
            state["composition_specs"] = [spec]
            state["focus"]["current_composition_spec_id"] = spec["id"]

            workspace = root / "matter"
            state_path = workspace / "_case-state" / "case-state.json"
            artifact_path = workspace / "30-working" / "TEST-ONLY-ready.pdf"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(b"%PDF-1.4\n% TEST-ONLY package preflight fixture\n")
            artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            artifact = {
                "id": "ART-TRUST-PACKAGE-001", "kind": "TEST-ONLY-ready-pdf",
                "audience": "internal_review", "version": 1,
                "path": "30-working/TEST-ONLY-ready.pdf", "sha256": artifact_hash,
                "status": "reviewed", "review_status": "passed", "input_snapshot": [],
                "composition_spec_id": spec["id"], "stale": False,
                "stale_reason": None, "created_at": "2026-08-25T00:01:00Z",
            }
            state["artifacts"] = [artifact]
            package_items = [{
                "object_id": artifact["id"], "object_type": "artifact", "version": 1,
                "hash": artifact_hash, "path": artifact["path"], "order": 1,
                "bookmark": "TEST-ONLY",
            }]
            package = {
                "id": "PKG-TRUST-001", "version": 1,
                "manifest_hash": sha256_bytes(canonical_json(package_items).encode("utf-8")),
                "items": package_items, "status": "candidate", "stale": False,
                "blockers": [], "created_at": "2026-08-25T00:01:00Z",
            }
            state["package_manifests"] = [package]

            # Production package preflight requires the active G1/G2 approvals
            # to have append-only audit receipts, not just valid state objects.
            state["focus"]["state_version"] = 2
            first_after_hash = "e" * 64
            events = []
            for index, approval in enumerate(state["approvals"], start=1):
                event = {
                    "sequence": index,
                    "event_id": f"EV-TRUST-PACKAGE-{index:03d}",
                    "matter_id": state["matter"]["id"],
                    "timestamp": "2026-08-25T00:00:00Z",
                    "actor": "TEST-ONLY-lawyer",
                    "command": "approve",
                    "event_type": "approval_granted",
                    "object_ids": [approval["id"]],
                    "before_state_hash": None if index == 1 else first_after_hash,
                    "after_state_hash": first_after_hash if index == 1 else legal_case_os.state_content_hash(state),
                    "previous_event_hash": events[-1]["event_hash"] if events else None,
                    "details": {
                        "gate": approval["gate"], "hash": approval["object_hash"],
                        "scope_hash": approval["scope_hash"],
                    },
                }
                event["event_hash"] = sha256_bytes(canonical_json(event).encode("utf-8"))
                events.append(event)
            state["audit"] = {
                "path": "_case-state/audit-log.jsonl", "last_sequence": len(events),
                "last_event_hash": events[-1]["event_hash"],
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            (state_path.parent / "audit-log.jsonl").write_text(
                "".join(canonical_json(event) + "\n" for event in events), encoding="utf-8",
            )
            legal_case_os.atomic_write_json(state_path, state)

            self.assertEqual(legal_case_os.validate_semantics(state, state_path), [])
            self.assertEqual(
                legal_case_os._verify_package(state, state_path, package),  # noqa: SLF001 - production preflight boundary
                [(artifact_path, "TEST-ONLY")],
            )

            original_artifact_bytes = artifact_path.read_bytes()
            original_verify_package = legal_case_os._verify_package  # noqa: SLF001
            verify_calls = 0

            def verify_then_tamper(*verify_args, **verify_kwargs):
                nonlocal verify_calls
                verified = original_verify_package(*verify_args, **verify_kwargs)
                verify_calls += 1
                if verify_calls == 1:
                    artifact_path.write_bytes(b"%PDF-1.4\n% TEST-ONLY changed after first verification\n")
                return verified

            print_output = root / "TEST-ONLY-print-sheet.md"
            with mock.patch.object(
                legal_case_os,
                "_verify_package",
                side_effect=verify_then_tamper,
            ):
                with self.assertRaises(LegalCaseError) as changed_during_build:
                    legal_case_os.command_print_sheet(SimpleNamespace(
                        state=str(state_path),
                        package_id=package["id"],
                        output=str(print_output),
                    ))
            self.assertEqual(changed_during_build.exception.code, "PACKAGE_FILE_HASH_MISMATCH")
            self.assertFalse(print_output.exists())
            self.assertEqual(list(print_output.parent.glob(".*.staging.*")), [])
            artifact_path.write_bytes(original_artifact_bytes)

            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            profile_path = (catalog_path.parent / catalog["templates"][0]["profile_path"]).resolve()
            original_profile = profile_path.read_bytes()
            profile_path.write_bytes(original_profile + b"\n")
            with self.assertRaises(LegalCaseError) as profile_drift:
                legal_case_os._verify_package(state, state_path, package)  # noqa: SLF001
            self.assertEqual(profile_drift.exception.code, "PACKAGE_COMPOSITION_TRUST_CHANGED")

            profile_path.write_bytes(original_profile)
            self.assertEqual(len(legal_case_os._verify_package(state, state_path, package)), 1)  # noqa: SLF001
            catalog["templates"][0]["status"] = "expired"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(LegalCaseError) as retired_template:
                legal_case_os._verify_package(state, state_path, package)  # noqa: SLF001
            self.assertEqual(retired_template.exception.code, "PACKAGE_COMPOSITION_TRUST_CHANGED")

    def test_strict_catalog_single_multi_and_automatic_selection_use_approved_preferences(self) -> None:
        fixture_root = PROJECT_ROOT / "tests" / "fixtures" / "template-reference-cases"
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-selection-catalog-") as temporary:
            root = Path(temporary) / "template-reference-cases"
            shutil.copytree(fixture_root, root)
            catalog_path = root / "_registry" / "TEST-ONLY-personal-template-catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            base_entry = catalog["templates"][0]

            def add_template(template_id: str, alias: str, layout: int, structure: int, auxiliary: int, roles: list[str]) -> None:
                profile_path = root / "_profiles" / f"{template_id}.writing-profile.json"
                profile = json.loads((root / "_profiles" / "TPL-P-S-001.writing-profile.json").read_text(encoding="utf-8"))
                profile.update({"template_id": template_id, "profile_id": f"PROFILE-{template_id}", "name": alias})
                profile["approval"]["decision_id"] = f"APR-PROFILE-{template_id}"
                preferences = copy.deepcopy(profile["composition_preferences"])
                preferences.update({
                    "layout_score": layout, "structure_score": structure,
                    "auxiliary_score": auxiliary, "role_suitability": roles,
                    "section_targets": ["事实与理由"] if "auxiliary" in roles else [],
                    "selection_notes": f"TEST-ONLY {template_id} 已批准选择理由",
                })
                profile["composition_preferences"] = preferences
                profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                profile_hash = __import__("hashlib").sha256(profile_path.read_bytes()).hexdigest()
                manifest_path = root / "_profiles" / f"{template_id}.fingerprints.json"
                manifest = json.loads((root / "_profiles" / "TPL-P-S-001.fingerprints.json").read_text(encoding="utf-8"))
                manifest.update({"template_id": template_id, "profile_id": profile["profile_id"], "profile_sha256": profile_hash})
                for record in manifest["fingerprints"]:
                    record["exemplar_id"] = template_id
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                manifest_hash = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
                entry = copy.deepcopy(base_entry)
                entry.update({
                    "id": template_id, "name": alias, "aliases": [alias],
                    "profile_path": f"../_profiles/{profile_path.name}", "profile_sha256": profile_hash,
                    "fingerprint_manifest_path": f"../_profiles/{manifest_path.name}",
                    "fingerprint_manifest_sha256": manifest_hash,
                    "composition_preferences": preferences,
                })
                catalog["templates"].append(entry)

            # Base is the layout main; B is structure main; C is scoped auxiliary.
            catalog["templates"][0]["composition_preferences"].update({"layout_score": 99, "structure_score": 20})
            base_profile_path = root / "_profiles" / "TPL-P-S-001.writing-profile.json"
            base_profile = json.loads(base_profile_path.read_text(encoding="utf-8"))
            base_profile["composition_preferences"] = copy.deepcopy(catalog["templates"][0]["composition_preferences"])
            base_profile_path.write_text(json.dumps(base_profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            base_profile_hash = __import__("hashlib").sha256(base_profile_path.read_bytes()).hexdigest()
            base_manifest_path = root / "_profiles" / "TPL-P-S-001.fingerprints.json"
            base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
            base_manifest["profile_sha256"] = base_profile_hash
            base_manifest_path.write_text(json.dumps(base_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            catalog["templates"][0].update({
                "profile_sha256": base_profile_hash,
                "fingerprint_manifest_sha256": __import__("hashlib").sha256(base_manifest_path.read_bytes()).hexdigest(),
            })
            add_template("TPL-P-S-002", "结构范例002", 20, 100, 20, ["layout", "structure"])
            add_template("TPL-P-S-003", "分段范例003", 10, 10, 100, ["auxiliary"])
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            state = self._trusted_state()
            base_payload = self._trusted_payload()
            single_payload = copy.deepcopy(base_payload)
            single_payload["template_refs"] = ["简模001"]
            single, _ = build_trusted_composition_spec(single_payload, state, catalog_path)
            self.assertEqual(single["template_roles"]["layout_template_id"], "TPL-P-S-001")
            self.assertEqual(single["template_roles"]["structure_template_id"], "TPL-P-S-001")

            multi_payload = copy.deepcopy(base_payload)
            multi_payload["template_refs"] = ["简模001", "结构范例002", "分段范例003"]
            multi, _ = build_trusted_composition_spec(multi_payload, state, catalog_path)
            self.assertEqual(multi["template_roles"]["layout_template_id"], "TPL-P-S-001")
            self.assertEqual(multi["template_roles"]["structure_template_id"], "TPL-P-S-002")
            self.assertEqual(multi["template_roles"]["auxiliary_templates"][0]["template_id"], "TPL-P-S-003")
            self.assertEqual({item["mode"] for item in multi["template_selection_reasons"]}, {"trusted_profile_score", "trusted_profile_section_scope"})

            automatic_payload = copy.deepcopy(base_payload)
            automatic_payload["template_refs"] = []
            automatic, _ = build_trusted_composition_spec(automatic_payload, state, catalog_path)
            self.assertEqual(automatic["template_roles"], multi["template_roles"])

    def test_material_pairwise_conflicts_require_one_central_confirmation_batch(self) -> None:
        payload = self._payload()
        for index, template in enumerate(payload["template_candidates"][:2]):
            template["compatibility_tags"] = {
                "relief_or_position": [f"position-{index}"],
                "evidence_use": ["shared-evidence"],
                "authority": ["verified-only"],
                "external_risk": ["court-clean"],
            }
        spec = build_composition_spec(payload)
        self.assertEqual(spec["template_conflict_batch"]["status"], "pending")
        self.assertEqual(len(spec["template_conflict_batch"]["assessment_ids"]), 1)
        result = validate_composition_spec(spec, self.fixture["authorities"], production=False)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("material_template_conflict_unresolved", codes)
        self.assertIn("template_conflict_batch_unapproved", codes)

    def test_trusted_build_freezes_source_evidence_issue_analysis_and_gate_dependencies(self) -> None:
        state = self._trusted_state()
        state["facts"] = [{
            "id": "F-TRUST-001", "statement": "TEST-ONLY案件状态确认命题", "status": "confirmed",
            "version": 1, "stale": False,
            "source_locators": [{
                "source_id": "SRC-TRUST-001", "locator": "p1", "record_kind": "direct_record",
                "quote": "TEST-ONLY案件状态确认命题", "verified": True,
            }],
        }]
        state["evidence"] = [{
            "id": "EV-TRUST-001", "source_refs": [{
                "source_id": "SRC-TRUST-001", "locator": "p1", "record_kind": "direct_record",
            }],
            "version": 1, "status": "approved", "proposition": "TEST-ONLY案件状态确认命题",
            "lawyer_decision": "reserve", "current_submission": False,
        }]
        state["issues"] = [{"id": "I-TRUST-001", "version": 1, "title": "TEST-ONLY危险争点"}]
        state["artifacts"] = [{
            "id": "ART-TRUST-ANALYSIS", "version": 1, "sha256": "5" * 64,
        }]
        payload = self._trusted_payload()
        payload["claim_bindings"] = [{
            "id": "CLM-TRUST-001", "proposition": "CALLER-FORGED命题", "claim_type": "fact",
            "fact_ids": ["F-TRUST-001"], "evidence_ids": ["EV-TRUST-001"],
            "source_ids": ["SRC-FORGED"], "source_locators": [{
                "source_id": "SRC-FORGED", "locator": "p999", "record_kind": "model_inference", "verified": True,
                "record_sha256": "6" * 64,
            }],
            "authority_ids": [], "authority_locators": [], "court_candidate": False,
            "verification_status": "verified",
        }]
        payload["dangerous_issues"] = [{
            "issue_id": "I-TRUST-001", "title": "TEST-ONLY危险争点", "risk_level": "high",
            "analysis_artifact_id": "ART-TRUST-ANALYSIS", "analysis_version": 1,
            "analysis_hash": "5" * 64, "status": "completed", "adverse_path_covered": True,
        }]
        catalog = PROJECT_ROOT / "tests" / "fixtures" / "template-reference-cases" / "_registry" / "TEST-ONLY-personal-template-catalog.json"
        spec, _ = build_trusted_composition_spec(payload, state, catalog)
        dependency_keys = {(item["object_type"], item["object_id"]) for item in spec["dependencies"]}
        for required in {
            ("source", "SRC-TRUST-001"), ("fact", "F-TRUST-001"), ("evidence", "EV-TRUST-001"),
            ("issue", "I-TRUST-001"), ("artifact", "ART-TRUST-ANALYSIS"),
            ("approval", "APR-TRUST-G1"), ("approval", "APR-TRUST-G2"),
        }:
            self.assertIn(required, dependency_keys)
        self.assertEqual(spec["claim_bindings"][0]["proposition"], "TEST-ONLY案件状态确认命题")
        self.assertNotIn("SRC-FORGED", json.dumps(spec["claim_bindings"][0], ensure_ascii=False))
        spec["dependencies"] = [
            item for item in spec["dependencies"] if item["object_id"] != "EV-TRUST-001"
        ]
        result = validate_composition_spec(spec, [], production=False)
        self.assertIn("composition_dependency_missing", {item["code"] for item in result["errors"]})

    def test_production_claim_is_rebuilt_from_confirmed_fact_and_approved_evidence_and_detects_drift(self) -> None:
        state = self._trusted_state()
        state["facts"] = [{
            "id": "F-STATE-001", "statement": "案件状态中的唯一确认事实", "status": "confirmed",
            "version": 1, "stale": False,
            "source_locators": [{
                "source_id": "SRC-TRUST-001", "locator": "第1页第3行", "record_kind": "direct_record",
                "quote": "案件状态中的唯一确认事实", "verified": True,
            }],
        }]
        state["evidence"] = [{
            "id": "EV-STATE-001", "version": 1, "status": "approved",
            "proposition": "证明案件状态中的唯一确认事实", "lawyer_decision": "reserve",
            "current_submission": False,
            "source_refs": [{
                "source_id": "SRC-TRUST-001", "locator": "第1页第3行", "record_kind": "direct_record",
            }],
        }]
        payload = self._trusted_payload()
        payload["claim_bindings"] = [{
            "id": "CLM-STATE-001", "proposition": "调用方伪造命题", "claim_type": "fact",
            "fact_ids": ["F-STATE-001"], "evidence_ids": ["EV-STATE-001"],
            "source_ids": ["SRC-FORGED"],
            "source_locators": [{
                "source_id": "SRC-FORGED", "locator": "伪造位置", "record_kind": "model_inference",
                "verified": True, "record_sha256": "f" * 64,
            }],
            "authority_ids": [], "authority_locators": [], "court_candidate": False,
            "verification_status": "verified",
        }]
        catalog = PROJECT_ROOT / "tests" / "fixtures" / "template-reference-cases" / "_registry" / "TEST-ONLY-personal-template-catalog.json"
        spec, snapshots = build_trusted_composition_spec(payload, state, catalog)
        claim = spec["claim_bindings"][0]
        self.assertEqual(claim["proposition"], "案件状态中的唯一确认事实")
        self.assertEqual(claim["verification_status"], "verified")
        self.assertEqual(claim["source_ids"], ["SRC-TRUST-001"])
        self.assertEqual(claim["source_locators"][0]["fact_id"], "F-STATE-001")
        self.assertNotEqual(claim["source_locators"][0]["record_sha256"], "f" * 64)
        synchronize_trusted_templates(state, snapshots)
        self.assertTrue(verify_composition_trust_binding(spec, state, catalog)["ok"])

        state["facts"][0]["statement"] = "事后篡改的事实"
        drift = verify_composition_trust_binding(spec, state, catalog)
        self.assertFalse(drift["ok"])
        self.assertTrue({
            "trusted_claim_state_changed", "trusted_case_projection_changed",
        } & {item["code"] for item in drift["errors"]})

        state = self._trusted_state()
        state["facts"] = [{
            "id": "F-STATE-001", "statement": "确认事实", "status": "disputed", "version": 1,
            "source_locators": [{
                "source_id": "SRC-TRUST-001", "locator": "p1", "record_kind": "direct_record", "verified": True,
            }],
        }]
        state["evidence"] = [{
            "id": "EV-STATE-001", "version": 1, "status": "candidate", "proposition": "确认事实",
            "source_refs": [{"source_id": "SRC-TRUST-001", "locator": "p1", "record_kind": "direct_record"}],
        }]
        with self.assertRaises(LegalCaseError) as rejected:
            build_trusted_composition_spec(payload, state, catalog)
        self.assertEqual(rejected.exception.code, "TRUSTED_CLAIM_FACT_NOT_CONFIRMED")

    def test_state_authority_projection_blocks_external_same_id_replacement(self) -> None:
        state = self._trusted_state()
        authority = {
            "id": "AUTH-STATE-001", "version": 1, "title": "案件状态核验法源",
            "source_level": "L1_verified_authority", "verification_status": "verified",
            "production_eligible": True, "adverse": False, "environment": "production",
            "test_only": False, "full_text_available": True,
            "official_citation": "《虚构测试法》第一条",
            "passage_hashes": {"全文第1段": "a" * 64},
        }
        state["authorities"] = [authority]
        payload = self._trusted_payload()
        payload["authority_ids"] = [authority["id"]]
        payload["claim_bindings"] = [{
            "id": "CLM-AUTH-STATE-001", "proposition": "适用虚构测试法第一条", "claim_type": "law",
            "fact_ids": [], "evidence_ids": [], "source_ids": [], "source_locators": [],
            "authority_ids": [authority["id"]],
            "authority_locators": [{
                "authority_id": authority["id"], "locator": "全文第1段", "citation": "调用方伪造引文",
                "verified": False, "holding_sha256": "f" * 64,
            }],
            "court_candidate": False, "verification_status": "unverified",
        }]
        catalog = PROJECT_ROOT / "tests" / "fixtures" / "template-reference-cases" / "_registry" / "TEST-ONLY-personal-template-catalog.json"
        spec, snapshots = build_trusted_composition_spec(payload, state, catalog)
        synchronize_trusted_templates(state, snapshots)
        self.assertEqual(spec["claim_bindings"][0]["authority_locators"][0]["holding_sha256"], "a" * 64)
        self.assertEqual(spec["claim_bindings"][0]["authority_locators"][0]["verified"], True)
        self.assertTrue(verify_composition_trust_binding(spec, state, catalog)["ok"])
        valid = validate_composition_spec(spec, [copy.deepcopy(authority)], production=True)
        self.assertTrue(valid["ok"], valid)

        replacement = copy.deepcopy(authority)
        replacement["title"] = "同ID外部替换法源"
        replaced = validate_composition_spec(spec, [replacement], production=True)
        self.assertIn("external_authority_binding_mismatch", {item["code"] for item in replaced["errors"]})
        state["authorities"][0]["passage_hashes"]["全文第1段"] = "b" * 64
        drift = verify_composition_trust_binding(spec, state, catalog)
        self.assertFalse(drift["ok"])

    def test_invalid_authority_adverse_and_material_conflict_are_blocked(self) -> None:
        spec = build_composition_spec(self._payload())
        spec["authority_ids"].append("AUTH-LEAD-001")
        spec["adverse_treatments"] = []
        spec["risk_conflicts"][0]["status"] = "open"
        spec["risk_conflicts"][0]["resolution"] = None
        result = validate_composition_spec(spec, self.fixture["authorities"], production=True)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("authority_not_production_eligible", codes)
        self.assertIn("material_adverse_omitted", codes)
        self.assertIn("material_risk_conflict_unresolved", codes)

    def test_template_qualification_and_dependency_snapshot_are_mandatory(self) -> None:
        spec = build_composition_spec(self._payload())
        qualification = next(
            item for item in spec["template_qualifications"] if item["template_id"] == "TPL-8075"
        )
        qualification["approved_final"] = False
        qualification["authorization"]["status"] = "unknown"
        qualification["profile"]["approval"]["status"] = "pending"
        qualification["profile"]["fingerprint_count"] = 0
        qualification["visual_baseline"]["status"] = "pending"
        spec["dependencies"] = [
            item for item in spec["dependencies"] if item["object_id"] != "TPL-8075"
        ]
        result = validate_composition_spec(spec, self.fixture["authorities"], production=True)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("template_not_approved_final", codes)
        self.assertIn("template_authorization_unverified", codes)
        self.assertIn("template_profile_not_approved", codes)
        self.assertIn("profile_fingerprint_manifest_missing", codes)
        self.assertIn("visual_baseline_missing", codes)
        self.assertIn("role_template_dependency_missing", codes)

    def test_court_claim_requires_exact_locators_g2_and_verified_status(self) -> None:
        spec = build_composition_spec(self._payload())
        binding = spec["claim_bindings"][0]
        binding["verification_status"] = "partially_verified"
        binding["source_locators"][0]["verified"] = False
        binding["authority_locators"][0]["locator"] = ""
        spec["gate_snapshots"]["G2_evidence"]["approved_source_ids"] = []
        result = validate_composition_spec(spec, self.fixture["authorities"], production=True)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("court_claim_not_verified", codes)
        self.assertIn("source_locator_missing_or_unverified", codes)
        self.assertIn("authority_locator_missing_or_unverified", codes)
        self.assertIn("source_outside_g2_scope", codes)

    def test_court_contract_freezes_goal_gates_deep_analysis_and_sections(self) -> None:
        spec = build_composition_spec(self._payload())
        spec["document_goal"] = None
        spec["relief_or_position"] = None
        spec["procedural_requirements"] = []
        spec["gate_snapshots"]["G1_strategy"]["status"] = "stale"
        spec["dangerous_issues"][0]["status"] = "stale"
        spec["dangerous_issues"][0]["adverse_path_covered"] = False
        spec["section_bindings"] = []
        result = validate_composition_spec(spec, self.fixture["authorities"], production=True)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("document_goal_missing", codes)
        self.assertIn("relief_or_position_missing", codes)
        self.assertIn("procedural_requirements_missing", codes)
        self.assertIn("gate_snapshot_not_active", codes)
        self.assertIn("dangerous_issue_analysis_incomplete", codes)
        self.assertIn("section_bindings_missing", codes)

    def test_role_limit_and_claim_binding_gates(self) -> None:
        spec = build_composition_spec(self._payload())
        spec["template_roles"]["auxiliary_templates"] = [
            {"template_id": f"TPL-X-{index}", "section_targets": [f"章节{index}"]}
            for index in range(4)
        ]
        spec["claim_bindings"][0]["source_ids"] = []
        spec["claim_bindings"][0]["authority_ids"] = []
        result = validate_composition_spec(spec, self.fixture["authorities"], production=True)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("too_many_auxiliary_templates", codes)
        self.assertIn("fact_source_missing", codes)
        self.assertIn("legal_authority_missing", codes)

    def test_citation_catalog_reconciliation_and_production_gate(self) -> None:
        text = (
            "参照（2025）渝0105民初8073号及"
            "《中华人民共和国民法典》第五百零九条处理。"
        )
        passed = audit_citations(text, {
            "authorities": self.fixture["authorities"],
            "allowed_authority_ids": ["AUTH-SUPPORT-001", "AUTH-LAW-001"],
            "production": True,
            "test_mode": True,
        })
        self.assertTrue(passed["ok"], passed)
        self.assertEqual(set(passed["used_authority_ids"]), {"AUTH-SUPPORT-001", "AUTH-LAW-001"})

        abbreviation = audit_citations("依据民法典第509条处理。", {
            "authorities": self.fixture["authorities"],
            "allowed_authority_ids": ["AUTH-LAW-001"],
            "test_mode": True,
        })
        self.assertTrue(abbreviation["ok"], abbreviation)

        unknown = audit_citations("引用（2022）渝0105民初999号。", self.fixture["authorities"])
        self.assertFalse(unknown["ok"])
        self.assertIn("citation_not_registered", {item["code"] for item in unknown["findings"]})

        lead = audit_citations("引用（2023）渝0105民初1号。", self.fixture["authorities"])
        self.assertIn("citation_not_production_eligible", {item["code"] for item in lead["findings"]})

    def test_pdf_citation_and_exemplar_audits_use_text_layer_and_fail_closed_on_blank_page(self) -> None:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-pdf-audit-") as temporary:
            root = Path(temporary)
            text_pdf = root / "TEST-ONLY-text.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "TEST-ONLY 引用（2025）渝0105民初8073号 and case text", fontname="china-s")
            document.save(text_pdf)
            document.close()
            citation = audit_citations(text_pdf, {
                "authorities": self.fixture["authorities"],
                "allowed_authority_ids": ["AUTH-SUPPORT-001"],
                "production": False,
                "test_mode": True,
            })
            self.assertTrue(citation["ok"], citation)
            fingerprints = build_exemplar_fingerprints("TPL-OLD-PDF", {"characteristic_phrase": ["case text"]})
            leak = check_exemplar_leaks(text_pdf, fingerprints)
            self.assertFalse(leak["ok"])

            unknown_pdf = root / "TEST-ONLY-unknown.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "TEST-ONLY 引用（2022）渝0105民初999号 case number", fontname="china-s")
            document.save(unknown_pdf)
            document.close()
            unknown_citation = audit_citations(unknown_pdf, self.fixture["authorities"])
            self.assertIn("citation_not_registered", {item["code"] for item in unknown_citation["findings"]})
            read_result = check_exemplar_leaks(unknown_pdf, build_exemplar_fingerprints("TPL-PDF", {"characteristic_phrase": ["case number"]}))
            self.assertFalse(read_result["ok"])

            image_only = root / "TEST-ONLY-image-only.pdf"
            document = fitz.open()
            document.new_page()
            document.save(image_only)
            document.close()
            with self.assertRaises(LegalCaseError) as raised:
                audit_citations(image_only, self.fixture["authorities"])
            self.assertEqual(raised.exception.code, "PDF_OCR_REQUIRED")

    def test_artifact_claim_map_covers_every_output_segment_and_detects_document_drift(self) -> None:
        spec = build_composition_spec(self._payload())
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-claim-map-") as temporary:
            document = Path(temporary) / "TEST-ONLY-candidate.txt"
            document.write_text("TEST-ONLY质证意见\n被告应依约履行义务。\n", encoding="utf-8")
            digest = lambda value: __import__("hashlib").sha256(value.encode("utf-8")).hexdigest()
            claim_map = {
                "schema_version": "1.0.0", "artifact_id": "ART-CLAIM-MAP-001",
                "artifact_version": 1,
                "composition_spec_id": spec["id"],
                "document_sha256": __import__("hashlib").sha256(document.read_bytes()).hexdigest(),
                "artifact_sha256": __import__("hashlib").sha256(document.read_bytes()).hexdigest(),
                "visual_text_manifest": None,
                "segments": [
                    {
                        "locator": "segment:1", "text_sha256": digest("TEST-ONLY质证意见"),
                        "claim_ids": [], "substantive": False, "non_substantive_reason": "heading",
                    },
                    {
                        "locator": "segment:2", "text_sha256": digest("被告应依约履行义务。"),
                        "claim_ids": ["CLM-001"], "substantive": True, "non_substantive_reason": None,
                    },
                ],
            }
            self.assertTrue(validate_artifact_claim_map(document, claim_map, spec)["ok"])
            document.write_text(document.read_text(encoding="utf-8") + "新增未登记数字9999。\n", encoding="utf-8")
            drift = validate_artifact_claim_map(document, claim_map, spec)
            self.assertFalse(drift["ok"])
            self.assertIn("artifact_claim_map_document_drift", {item["code"] for item in drift["findings"]})
            self.assertIn("artifact_claim_map_coverage_incomplete", {item["code"] for item in drift["findings"]})

    def test_artifact_claim_map_schema_state_and_verified_claim_gates_fail_closed(self) -> None:
        malformed = {
            "schema_version": "1.0.0", "artifact_id": "ART-BAD-001",
            "composition_spec_id": "CMP-BAD-001", "document_sha256": "a" * 64,
            "segments": [{
                "locator": "segment:1", "text_sha256": "b" * 64, "claim_ids": [],
                "substantive": "yes", "non_substantive_reason": "任意理由",
            }],
        }
        schema_first = validate_artifact_claim_map(
            Path("TEST-ONLY-does-not-exist.pdf"), malformed,
            {"id": "CMP-BAD-001", "claim_bindings": [], "test_mode": True},
        )
        self.assertEqual(schema_first["findings"][0]["code"], "artifact_claim_map_schema_invalid")

        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-claim-map-state-") as temporary:
            document = Path(temporary) / "TEST-ONLY-candidate.txt"
            document.write_text("实体命题。\n", encoding="utf-8")
            document_hash = hashlib.sha256(document.read_bytes()).hexdigest()
            spec = {
                "id": "CMP-CLAIM-STATE-001", "test_mode": False,
                "trusted_binding": {"mode": "catalog_case_state"},
                "claim_bindings": [{
                    "id": "CLM-UNVERIFIED-001", "court_candidate": False,
                    "verification_status": "unverified",
                }],
            }
            claim_map = {
                "schema_version": "1.0.0", "artifact_id": "ART-CLAIM-STATE-001",
                "artifact_version": 2, "artifact_sha256": document_hash,
                "composition_spec_id": spec["id"], "document_sha256": document_hash,
                "visual_text_manifest": None,
                "segments": [{
                    "locator": "segment:1", "text_sha256": hashlib.sha256("实体命题。".encode()).hexdigest(),
                    "claim_ids": ["CLM-UNVERIFIED-001"], "substantive": True,
                    "non_substantive_reason": None,
                }],
            }
            state = {"artifacts": [{
                "id": "ART-CLAIM-STATE-001", "version": 1, "sha256": document_hash,
                "composition_spec_id": spec["id"], "audience": "court_candidate",
                "status": "approved", "stale": False,
            }]}
            result = validate_artifact_claim_map(document, claim_map, spec, state=state)
            codes = {item["code"] for item in result["findings"]}
            self.assertIn("artifact_claim_map_state_artifact_mismatch", codes)
            self.assertIn("artifact_substantive_claim_not_verified_court_candidate", codes)

    def test_visual_objects_require_page_ocr_manifest_and_exact_render_hash_for_pdf_and_docx(self) -> None:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        spec = {
            "id": "CMP-VISUAL-001", "test_mode": True,
            "trusted_binding": {"mode": "test_only_fixture"}, "claim_bindings": [],
        }

        def base_map(path: Path, text: str, artifact_id: str) -> dict:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return {
                "schema_version": "1.0.0", "artifact_id": artifact_id,
                "artifact_version": 1, "artifact_sha256": digest,
                "composition_spec_id": spec["id"], "document_sha256": digest,
                "visual_text_manifest": None,
                "segments": [{
                    "locator": "segment:1", "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "claim_ids": [], "substantive": False, "non_substantive_reason": "caption",
                }],
            }

        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-visual-map-") as temporary:
            root = Path(temporary)
            pdf = root / "TEST-ONLY-small-hidden-layer.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "X")
            page.draw_rect(fitz.Rect(60, 60, 180, 180), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
            document.save(pdf)
            document.close()
            pdf_map = base_map(pdf, "X", "ART-VISUAL-PDF-001")
            missing = validate_artifact_claim_map(pdf, pdf_map, spec)
            self.assertIn("artifact_visual_text_manifest_required", {item["code"] for item in missing["findings"]})
            with self.assertRaises(LegalCaseError) as citation_blocked:
                audit_citations(pdf, [])
            self.assertEqual(citation_blocked.exception.code, "VISUAL_TEXT_MANIFEST_REQUIRED")
            page_render_hash = missing["visual_inventory"]["pages"][0]["render_sha256"]
            visual_text = "第1页绘图对象经人工复核，不含实质案件文字。"
            pdf_map["visual_text_manifest"] = {
                "document_sha256": pdf_map["document_sha256"],
                "render_method": "pymupdf_144dpi_rgb", "page_count": 1,
                "pages": [{
                    "page_number": 1, "render_path": None, "render_sha256": page_render_hash,
                    "visual_text": visual_text,
                    "visual_text_sha256": hashlib.sha256(visual_text.encode()).hexdigest(),
                    "verification_status": "verified_visual_transcription", "claim_ids": [],
                }],
            }
            self.assertTrue(validate_artifact_claim_map(pdf, pdf_map, spec)["ok"])
            pdf_map["visual_text_manifest"]["pages"][0]["render_sha256"] = "0" * 64
            bad_render = validate_artifact_claim_map(pdf, pdf_map, spec)
            self.assertIn("artifact_visual_render_hash_mismatch", {item["code"] for item in bad_render["findings"]})

            docx = root / "TEST-ONLY-drawing.docx"
            with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>X</w:t><w:drawing><a:graphic/></w:drawing></w:r></w:p></w:body>'
                    '</w:document>'
                ))
            docx_map = base_map(docx, "X", "ART-VISUAL-DOCX-001")
            docx_missing = validate_artifact_claim_map(docx, docx_map, spec)
            self.assertIn("artifact_visual_text_manifest_required", {item["code"] for item in docx_missing["findings"]})
            render_page = root / "TEST-ONLY-page-1.png"
            render_page.write_bytes(b"\x89PNG\r\n\x1a\nTEST-ONLY-render")
            docx_visual_text = "第1页绘图经人工复核，不含实质案件文字。"
            docx_map["visual_text_manifest"] = {
                "document_sha256": docx_map["document_sha256"],
                "render_method": "external_verified_page_png", "page_count": 1,
                "pages": [{
                    "page_number": 1, "render_path": str(render_page),
                    "render_sha256": hashlib.sha256(render_page.read_bytes()).hexdigest(),
                    "visual_text": docx_visual_text,
                    "visual_text_sha256": hashlib.sha256(docx_visual_text.encode()).hexdigest(),
                    "verification_status": "verified_visual_transcription", "claim_ids": [],
                }],
            }
            self.assertTrue(validate_artifact_claim_map(docx, docx_map, spec)["ok"])

    def test_authority_holding_hash_drift_is_blocked(self) -> None:
        spec = build_composition_spec(self._payload())
        spec["claim_bindings"][0]["authority_locators"][0]["holding_sha256"] = "9" * 64
        result = validate_composition_spec(spec, self.fixture["authorities"], production=False)
        self.assertIn("authority_holding_hash_drift", {item["code"] for item in result["errors"]})

    def test_hash_only_exemplar_leak_gate(self) -> None:
        fingerprints = build_exemplar_fingerprints(
            "TPL-OLD-001",
            {
                "name": ["旧案甲公司"],
                "case_number": ["（2020）渝01民终123号"],
                "amount": ["987654.32元"],
                "characteristic_phrase": ["该抗辩实质上混淆了交付与验收"],
            },
        )
        self.assertNotIn("旧案甲公司", json.dumps(fingerprints, ensure_ascii=False))
        leaked = check_exemplar_leaks(
            "本案与旧案甲公司无关，但该名称不应进入新稿。",
            {"fingerprints": fingerprints},
        )
        self.assertFalse(leaked["ok"])
        self.assertIn("name", {item["kind"] for item in leaked["findings"]})
        clean = check_exemplar_leaks("本案主体和事实均来自当前材料。", fingerprints)
        self.assertTrue(clean["ok"], clean)
        empty = check_exemplar_leaks("任意候选稿", {"profile_id": "PRO-EMPTY", "fingerprints": []})
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["findings"][0]["code"], "fingerprint_manifest_empty")

    def test_invalidation_closure_covers_evidence_issue_and_g1_g2_gate_approvals(self) -> None:
        def make_state() -> dict:
            spec = build_composition_spec({
                "id": "CMP-CLOSURE-001",
                "matter_id": "M-CLOSURE-001",
                "document_type": "TEST-ONLY 质证意见",
                "audience": "internal_review",
                "test_mode": True,
                "template_candidates": [],
                "template_refs": [],
                "claim_bindings": [],
                "authority_ids": [],
                "dependencies": [
                    {"object_id": "SRC-CLOSURE-001", "object_type": "source", "version": 1, "hash": "1" * 64, "required": True},
                    {"object_id": "EV-CLOSURE-001", "object_type": "evidence", "version": 1, "hash": "2" * 64, "required": True},
                    {"object_id": "ISSUE-CLOSURE-001", "object_type": "issue", "version": 1, "hash": "3" * 64, "required": True},
                    {"object_id": "APR-CLOSURE-G1", "object_type": "approval", "version": 1, "hash": "4" * 64, "required": True},
                    {"object_id": "APR-CLOSURE-G2", "object_type": "approval", "version": 1, "hash": "5" * 64, "required": True},
                ],
                "adverse_treatments": [],
                "risk_conflicts": [],
                "created_at": "2026-08-25T00:00:00Z",
            })
            spec["gate_snapshots"] = {
                "G1_strategy": {"approval_id": "APR-CLOSURE-G1"},
                "G2_evidence": {"approval_id": "APR-CLOSURE-G2"},
            }
            spec["trusted_binding"] = {
                "mode": "test_only_fixture",
                "approval_bindings": [
                    {"gate": "G1_strategy", "approval_id": "APR-CLOSURE-G1"},
                    {"gate": "G2_evidence", "approval_id": "APR-CLOSURE-G2"},
                ],
            }
            return {
                "sources": [{"id": "SRC-CLOSURE-001"}],
                "facts": [],
                "evidence": [{
                    "id": "EV-CLOSURE-001", "status": "current", "current_submission": True,
                    "source_locators": [{"source_id": "SRC-CLOSURE-001", "locator": "p1"}],
                }],
                "authorities": [],
                "issues": [{
                    "id": "ISSUE-CLOSURE-001", "status": "active",
                    "source_ids": [], "fact_ids": [], "evidence_ids": ["EV-CLOSURE-001"], "authority_ids": [],
                }],
                "theories": [],
                "decisions": [],
                "approvals": [
                    {
                        "id": "APR-CLOSURE-G1", "gate": "G1_strategy", "status": "active",
                        "object_id": "DEC-CLOSURE-G1", "scope_snapshot": [{"object_id": "ISSUE-CLOSURE-001"}],
                    },
                    {
                        "id": "APR-CLOSURE-G2", "gate": "G2_evidence", "status": "active",
                        "object_id": "DEC-CLOSURE-G2", "scope_snapshot": [{"object_id": "EV-CLOSURE-001"}],
                    },
                ],
                "composition_specs": [spec],
                "artifacts": [{
                    "id": "ART-CLOSURE-001", "status": "draft", "stale": False,
                    "composition_spec_id": "CMP-CLOSURE-001", "input_snapshot": [],
                }],
                "package_manifests": [{
                    "id": "PKG-CLOSURE-001", "status": "candidate", "stale": False, "blockers": [],
                    "items": [{"object_id": "ART-CLOSURE-001"}],
                }],
                "processing_coverages": [],
                "focus": {"recent_candidates": [], "pending_approval_ids": []},
                "status": {"workflow": "drafting", "blockers": []},
            }

        for seed in (
            "SRC-CLOSURE-001",
            "EV-CLOSURE-001",
            "ISSUE-CLOSURE-001",
            "APR-CLOSURE-G1",
            "APR-CLOSURE-G2",
        ):
            with self.subTest(seed=seed):
                state = make_state()
                affected = legal_case_os._mark_stale(state, {seed}, "TEST-ONLY 上游变化")
                self.assertIn("CMP-CLOSURE-001", affected)
                self.assertIn("ART-CLOSURE-001", affected)
                self.assertIn("PKG-CLOSURE-001", affected)
                self.assertEqual(state["composition_specs"][0]["status"], "stale")
                if seed in {"SRC-CLOSURE-001", "EV-CLOSURE-001"}:
                    self.assertEqual(state["approvals"][1]["status"], "stale")
                if seed in {"SRC-CLOSURE-001", "EV-CLOSURE-001", "ISSUE-CLOSURE-001"}:
                    self.assertEqual(state["approvals"][0]["status"], "stale")

    def test_dependency_change_marks_spec_and_artifacts_for_invalidation(self) -> None:
        spec = build_composition_spec(self._payload())
        spec["dependent_artifact_ids"] = ["ART-DRAFT-001", "PKG-G4-001"]
        current = {
            "TPL-8073": {"id": "TPL-8073", "version": 2, "hash": "a" * 64},
            "AUTH-LAW-001": {"id": "AUTH-LAW-001", "version": 2, "hash": "b" * 64},
        }
        audit = detect_composition_staleness(spec, current)
        self.assertTrue(audit["stale"])
        self.assertEqual(audit["changes"][0]["reason"], "version_changed")
        stale = mark_composition_stale(spec, current)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["dependent_artifact_ids"], ["ART-DRAFT-001", "PKG-G4-001"])

    def test_cli_payload_errors_use_legal_case_error(self) -> None:
        with self.assertRaises(LegalCaseError) as caught:
            build_composition_spec({"document_type": "质证意见"})
        self.assertEqual(caught.exception.code, "INVALID_COMPOSITION_PAYLOAD")
        with self.assertRaises(LegalCaseError) as missing:
            audit_citations(Path("TEST-ONLY-does-not-exist.docx"), self.fixture["authorities"])
        self.assertEqual(missing.exception.code, "DOCUMENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
