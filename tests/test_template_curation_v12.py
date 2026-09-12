#!/usr/bin/env python3
"""TEST-ONLY tests for v1.2 template distillation and fidelity filling."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from legal_case_os_lib import template_curation as curation  # noqa: E402
from legal_case_os_lib import templates  # noqa: E402
from legal_case_os_lib.core import LegalCaseError, sha256_file, validate_against_schema  # noqa: E402
from legal_case_os_lib.template_curation import (  # noqa: E402
    FIELD_POLICIES,
    build_fill_plan,
    build_template_approval_receipt,
    compare_docx_packages,
    convert_legacy_doc,
    distill_template,
    fill_docx_from_plan,
    make_fill_binding,
    register_template,
    validate_fill_plan,
    validate_profile,
    validate_usage_mode,
)


FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "template-curation-v12"

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

DOCUMENT_RELS = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''

STYLES = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>'''

DOCUMENT_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>TEST-ONLY 委托代理手续</w:t></w:r></w:p>
    <w:p><w:r><w:t>TEST-ONLY 旧案敏感事实不得进入画像全文</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="8000" w:type="dxa"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="5000"/></w:tblGrid>
      <w:tr><w:tc><w:p><w:r><w:t>委托人</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>{{client_name}}</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>副本委托人</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>{{client_name}}</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>律师费</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>{{fee_amount}}</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>签字</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>{{signature}}</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>'''.encode("utf-8")


def make_test_docx(path: Path, *, version_marker: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document_xml = DOCUMENT_XML
    if version_marker:
        document_xml = document_xml.replace(
            b"TEST-ONLY \xe5\xa7\x94\xe6\x89\x98\xe4\xbb\xa3\xe7\x90\x86\xe6\x89\x8b\xe7\xbb\xad",
            f"TEST-ONLY \u59d4\u6258\u4ee3\u7406\u624b\u7eed {version_marker}".encode("utf-8"),
        )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", ROOT_RELS)
        package.writestr("word/document.xml", document_xml)
        package.writestr("word/styles.xml", STYLES)
        package.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)


def make_empty_cell_docx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    empty_document = DOCUMENT_XML.replace(b"<w:t>{{signature}}</w:t>", b"<w:t></w:t>")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", ROOT_RELS)
        package.writestr("word/document.xml", empty_document)
        package.writestr("word/styles.xml", STYLES)
        package.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)


def read_document_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as package:
        return package.read("word/document.xml").decode("utf-8")


def object_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_visual_baseline(root: Path, source: Path) -> dict:
    page = root / "render" / "page-1.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"\x89PNG\r\n\x1a\nTEST-ONLY-render-baseline")
    return {
        "status": "verified",
        "source_sha256": sha256_file(source),
        "renderer": "TEST-ONLY-renderer",
        "page_count": 1,
        "pages": [{"page_number": 1, "path": str(page), "sha256": sha256_file(page)}],
        "reviewed_by": "TEST-ONLY-reviewer",
        "reviewed_at": "2026-01-01T00:00:00Z"
    }


def bind_case_state_snapshot(root: Path, fixture: dict, profile: dict) -> tuple[dict, dict]:
    data = copy.deepcopy(fixture["data"])
    provenance = copy.deepcopy(fixture["provenance"])
    slots_by_field: dict[str, list[dict]] = {}
    for slot in profile.get("slots", []):
        if slot.get("field_key"):
            slots_by_field.setdefault(slot["field_key"], []).append(slot)
    fee_binding = make_fill_binding(
        profile, slots_by_field["fee_amount"][0], "replace", data["fee_amount"]
    )
    source_object = {
        "id": "TEST-SRC-001",
        "kind": "original_material",
        "verified": True,
        "sha256": "a" * 64
    }
    source_hash = object_digest(source_object)
    base_source_meta = provenance.pop("client_name")
    fact_objects: list[dict] = []
    fact_snapshot: list[dict] = []
    for index, slot in enumerate(slots_by_field["client_name"], start=1):
        client_binding = make_fill_binding(
            profile, slot, "replace", data["client_name"]
        )
        fact_object = {
            "id": f"TEST-FACT-CLIENT-{index:03d}",
            "statement": "TEST-ONLY：材料直接记载当事人名称。",
            "status": "confirmed",
            "field_key": "client_name",
            "source_locators": [{
                "source_id": "TEST-SRC-001",
                "locator": "第1页第1行",
                "record_kind": "direct_record",
                "quote": None,
                "verified": True,
            }],
            "normalized_value_sha256": client_binding["value_sha256"],
            "fill_binding": client_binding,
        }
        fact_hash = object_digest(fact_object)
        fact_objects.append(fact_object)
        fact_snapshot.append({**fact_object, "object_sha256": fact_hash})
        slot_meta = copy.deepcopy(base_source_meta)
        slot_meta["source_refs"][0].update({
            "sha256": source_object["sha256"],
            "object_sha256": source_hash,
            "fact_id": fact_object["id"],
            "fact_object_sha256": fact_hash,
            "locator": "第1页第1行",
            "extracted_value_sha256": client_binding["value_sha256"],
            "binding": client_binding,
        })
        provenance[slot["slot_id"]] = slot_meta
    decision_object = {
        "decision_id": "TEST-DECISION-FEE-001",
        "status": "approved",
        "actor_role": "lawyer",
        "fill_binding": fee_binding,
    }
    state = {
        "state_version": 1,
        "sources": [source_object],
        "facts": fact_objects,
        "decisions": [decision_object]
    }
    state_path = root / "_case-state" / "case-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    decision_hash = object_digest(decision_object)
    provenance["fee_amount"]["confirmation"]["object_sha256"] = decision_hash
    provenance["fee_amount"]["confirmation"].update({
        "approval_id": "TEST-APPROVAL-FEE-001",
        "fill_binding": fee_binding,
    })
    snapshot = {
        "case_state_path": str(state_path),
        "case_state_sha256": sha256_file(state_path),
        "state_version": 1,
        "sources": [{**source_object, "object_sha256": source_hash}],
        "facts": fact_snapshot,
        "decisions": [{**decision_object, "object_sha256": decision_hash}]
    }
    snapshot["snapshot_sha256"] = object_digest(snapshot)
    provenance["_registry_snapshot"] = snapshot
    return data, provenance


def activated_form_profile(root: Path) -> tuple[Path, dict]:
    source = root / "TEST-ONLY-form.docx"
    make_test_docx(source)
    result = distill_template(
        source,
        root / "profiles",
        "form",
        "fillable_clone",
        "TEST-FORM-001",
        "TEST-ONLY手续",
        "委托代理手续",
        overrides={
            "slot_policies": {
                "SLOT-0002": {
                    "policy": "manual_blank",
                    "field_key": "old_case_fact",
                    "block_original_text_in_output": True
                },
                "client_name": "required_verified",
                "fee_amount": "lawyer_decision_required",
                "signature": "manual_blank"
            },
            "approve_all_fixed_as_boilerplate": True,
            "visual_baseline": make_visual_baseline(root, source),
            "unresolved_items": []
        },
    )
    profile = result["profile"]
    profile["status"] = "active"
    profile["approval"] = {
        "confirmed": True,
        "approved_by": "TEST-ONLY-lawyer",
        "decision_id": "TEST-PROFILE-APPROVAL-001"
    }
    profile_path = Path(result["profile_path"])
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return source, profile


def ready_registration_draft(
    root: Path,
    source: Path,
    template_id: str,
    profile_folder: str,
    *,
    baseline_root: Path | None = None,
) -> dict:
    return distill_template(
        source,
        root / "library" / "\u6a21\u677f\u5e93" / "_profiles" / profile_folder,
        "form",
        "fillable_clone",
        template_id,
        "TEST-ONLY\u767b\u8bb0\u6a21\u677f",
        "\u6388\u6743\u59d4\u6258\u4e66",
        overrides={
            "slot_policies": {
                "SLOT-0002": {
                    "policy": "manual_blank",
                    "field_key": "old_case_fact",
                    "block_original_text_in_output": True,
                },
                "client_name": "required_verified",
                "fee_amount": "lawyer_decision_required",
                "signature": "manual_blank",
            },
            "approve_all_fixed_as_boilerplate": True,
            "visual_baseline": make_visual_baseline(
                baseline_root if baseline_root is not None else root / profile_folder,
                source,
            ),
            "unresolved_items": [],
        },
    )


def make_catalog(root: Path) -> Path:
    catalog = root / "library" / "\u6a21\u677f\u5e93" / "_registry" / "template-catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {"catalog_version": "1.0.0", "templates": [], "suites": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return catalog


APPROVAL = {
    "confirmed": True,
    "approved_by": "TEST-ONLY-lawyer",
    "decision_id": "TEST-APPROVAL-001",
}


def registration_receipt(
    catalog: Path,
    *,
    operation: str,
    template_id: str,
    target_version: str,
    decision_id: str,
    source: Path | None = None,
    profile: Path | None = None,
) -> dict:
    if operation == "retire":
        entry = next(
            item for item in json.loads(catalog.read_text(encoding="utf-8"))["templates"]
            if item["id"] == template_id
        )
        source_sha256 = entry["sha256"]
        profile_sha256 = entry["profile_sha256"]
    else:
        if source is None or profile is None:
            raise AssertionError("create/upgrade TEST-ONLY receipt requires source and profile")
        source_sha256 = sha256_file(source)
        profile_sha256 = sha256_file(profile)
    return build_template_approval_receipt(
        decision_id=decision_id,
        approved_by="TEST-ONLY-lawyer",
        operation=operation,
        template_id=template_id,
        target_version=target_version,
        source_sha256=source_sha256,
        profile_sha256=profile_sha256,
        catalog_before_sha256=sha256_file(catalog),
        decided_at="2026-08-25T00:00:00Z",
    )


class TemplateCurationV12Tests(unittest.TestCase):
    maxDiff = None

    def test_policy_enum_and_usage_mode_contract(self) -> None:
        self.assertEqual(len(FIELD_POLICIES), 8)
        self.assertTrue(validate_usage_mode("reference", profile_kind="writing")["ok"])
        self.assertTrue(validate_usage_mode("fillable_clone", profile_kind="form")["ok"])
        self.assertFalse(validate_usage_mode("fillable_clone", profile_kind="writing")["ok"])
        self.assertFalse(
            validate_usage_mode(
                "hybrid", profile_kind="writing", editable_slot_count=0, ready_for_use=True
            )["ok"]
        )

    def test_distillation_is_read_only_draft_and_does_not_copy_prose(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-curation-") as temporary:
            root = Path(temporary)
            source = root / "TEST-ONLY-reference.docx"
            make_test_docx(source)
            before = sha256_file(source)
            form_result = distill_template(
                source,
                root / "form-profile",
                "form",
                "fillable_clone",
                "TEST-FORM-DRAFT",
                "TEST-ONLY表单",
                "测试手续",
            )
            self.assertEqual(before, sha256_file(source))
            self.assertEqual(form_result["profile"]["status"], "draft")
            self.assertTrue(form_result["profile"]["unresolved_items"])
            serialized = Path(form_result["profile_path"]).read_text(encoding="utf-8")
            self.assertNotIn("旧案敏感事实", serialized)
            placeholders = [
                slot for slot in form_result["profile"]["slots"] if slot["placeholder_kind"]
            ]
            self.assertEqual({slot["field_key"] for slot in placeholders}, {"client_name", "fee_amount", "signature"})
            self.assertTrue(all(slot["policy"] is None for slot in placeholders))
            form_schema = json.loads(
                (PROJECT_ROOT / "shared" / "schemas" / "form-profile.schema.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validate_against_schema(form_result["profile"], form_schema), [])

            writing_result = distill_template(
                source,
                root / "writing-profile",
                "writing",
                "reference",
                "TEST-WRITING-DRAFT",
                "TEST-ONLY文书",
                "质证意见",
            )
            writing_json = Path(writing_result["profile_path"]).read_text(encoding="utf-8")
            self.assertNotIn("旧案敏感事实", writing_json)
            self.assertGreater(writing_result["profile"]["metrics"]["total_character_count"], 0)
            self.assertTrue(all("text" not in item for item in writing_result["profile"]["structure_outline"]))
            writing_schema = json.loads(
                (PROJECT_ROOT / "shared" / "schemas" / "writing-profile.schema.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validate_against_schema(writing_result["profile"], writing_schema), [])
            self.assertEqual(before, sha256_file(source))

    def test_build_validate_and_fill_preserves_package_structure(self) -> None:
        fixture = json.loads((FIXTURE_ROOT / "TEST-ONLY-field-input.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-fill-") as temporary:
            root = Path(temporary)
            source, profile = activated_form_profile(root)
            source_before = sha256_file(source)
            profile_path = root / "profiles" / "TEST-FORM-001.form-profile.json"
            data, provenance = bind_case_state_snapshot(root, fixture, profile)
            plan = build_fill_plan(profile_path, data, provenance)
            self.assertEqual(plan["status"], "ready")
            fill_schema = json.loads(
                (PROJECT_ROOT / "shared" / "schemas" / "fill-plan.schema.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validate_against_schema(plan, fill_schema), [])
            validation = validate_fill_plan(plan, profile)
            self.assertTrue(validation["ok"], validation)
            destination = root / "TEST-ONLY-filled.docx"
            result = fill_docx_from_plan(source, destination, profile, plan)
            self.assertTrue(result["ok"])
            self.assertEqual(result["changed_parts"], ["word/document.xml"])
            self.assertEqual(source_before, sha256_file(source))
            xml = read_document_xml(destination)
            self.assertIn("测试甲公司", xml)
            self.assertIn("10000元", xml)
            self.assertNotIn("{{client_name}}", xml)
            self.assertNotIn("{{fee_amount}}", xml)
            self.assertNotIn("{{signature}}", xml)
            self.assertNotIn("旧案敏感事实", xml)
            self.assertEqual(result["release_status"], "structurally_valid_pending_visual_qa")
            self.assertTrue(result["preflight"]["ok"])
            diff = compare_docx_packages(source, destination, allowed_changed_parts=["word/document.xml"])
            self.assertTrue(diff["ok"], diff)
            self.assertEqual(diff["structure_changed_parts"], [])

            wrong_locator = copy.deepcopy(plan)
            client_row = next(
                item for item in wrong_locator["slots"] if item["field_key"] == "client_name"
            )
            client_row["source_refs"][0]["locator"] = "第99页"
            wrong_locator_codes = {
                item["code"] for item in validate_fill_plan(wrong_locator, profile)["errors"]
            }
            self.assertIn("DIRECT_SOURCE_REQUIRED", wrong_locator_codes)

            replayed_decision = copy.deepcopy(plan)
            fee_row = next(
                item for item in replayed_decision["slots"] if item["field_key"] == "fee_amount"
            )
            fee_row["value"] = "20000元"
            replay_codes = {
                item["code"] for item in validate_fill_plan(replayed_decision, profile)["errors"]
            }
            self.assertIn("FILL_SLOT_BINDING_MISMATCH", replay_codes)
            self.assertIn("LAWYER_DECISION_REQUIRED", replay_codes)

            wrong_slot = copy.deepcopy(plan)
            fee_row = next(
                item for item in wrong_slot["slots"] if item["field_key"] == "fee_amount"
            )
            fee_row["confirmation"]["fill_binding"]["slot_id"] = "SLOT-OTHER"
            wrong_slot_codes = {
                item["code"] for item in validate_fill_plan(wrong_slot, profile)["errors"]
            }
            self.assertIn("LAWYER_DECISION_REQUIRED", wrong_slot_codes)

    def test_required_source_lawyer_decision_manual_blank_and_conflict_gates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-gates-") as temporary:
            root = Path(temporary)
            _source, profile = activated_form_profile(root)
            profile_path = root / "profiles" / "TEST-FORM-001.form-profile.json"
            plan = build_fill_plan(
                profile_path,
                {
                    "client_name": "只有模型猜测的名称",
                    "fee_amount": "8888元",
                    "signature": "AI代签"
                },
                {
                    "client_name": {
                        "source_refs": [{"id": "MODEL-1", "kind": "model_inference", "verified": False}]
                    }
                },
            )
            self.assertEqual(plan["status"], "blocked")
            validation = validate_fill_plan(plan, profile)
            codes = {item["code"] for item in validation["errors"]}
            self.assertIn("DIRECT_SOURCE_REQUIRED", codes)
            self.assertIn("LAWYER_DECISION_REQUIRED", codes)
            self.assertIn("FILL_PLAN_CONFLICTS_UNRESOLVED", codes)
            self.assertTrue(validation["conflict_batch"])
            self.assertEqual(
                len(validation["consolidated_review_batch"]),
                len(validation["conflict_batch"]) + len(validation["confirmation_batch"]),
            )
            self.assertEqual(
                validation["confirmation_batch"],
                [{"slot_id": next(item["slot_id"] for item in profile["slots"] if item.get("field_key") == "fee_amount"), "reason": "lawyer_decision"}],
            )

            # A non-placeholder dynamic node is never preserved merely because
            # no new value was supplied; doing so would leak a legacy amount or
            # client fact from the exemplar.
            legacy_slot = next(item for item in profile["slots"] if item.get("field_key") == "old_case_fact")
            legacy_slot["policy"] = "optional_verified"
            legacy_slot["content_role"] = "field_anchor"
            optional_profile_path = root / "profiles" / "TEST-FORM-OPTIONAL.form-profile.json"
            optional_profile_path.write_text(
                json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            blank_plan = build_fill_plan(optional_profile_path, {}, {})
            legacy_row = next(item for item in blank_plan["slots"] if item["slot_id"] == legacy_slot["slot_id"])
            self.assertEqual(legacy_row["action"], "clear_to_blank")
            tampered = copy.deepcopy(blank_plan)
            next(item for item in tampered["slots"] if item["slot_id"] == legacy_slot["slot_id"])["action"] = "preserve"
            codes = {item["code"] for item in validate_fill_plan(tampered, profile)["errors"]}
            self.assertIn("OPTIONAL_BLANK_NOT_CLEARED", codes)

            # Explicit behavioral coverage for the remaining field policies.
            # The profile is TEST-ONLY and never registered or activated in a
            # personal catalog.
            policy_profile = copy.deepcopy(profile)
            client_slots = [
                item for item in policy_profile["slots"] if item.get("field_key") == "client_name"
            ]
            client_slots[0].update({
                "policy": "conditional_verified",
                "field_key": "conditional_value",
                "content_role": "field_anchor",
            })
            client_slots[1].update({
                "policy": "derived_needs_confirmation",
                "field_key": "derived_value",
                "content_role": "field_anchor",
            })
            fee_slot = next(
                item for item in policy_profile["slots"] if item.get("field_key") == "fee_amount"
            )
            fee_slot.update({"policy": "optional_verified", "content_role": "field_anchor"})
            policy_path = root / "profiles" / "TEST-FORM-POLICIES.form-profile.json"
            policy_path.write_text(
                json.dumps(policy_profile, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            blank_policy_plan = build_fill_plan(
                policy_path,
                {},
                {
                    "conditional_value": {
                        "applies": False,
                        "blank_reason": "TEST-ONLY 条件不成立",
                    }
                },
            )
            by_policy = {item["policy"]: item for item in blank_policy_plan["slots"]}
            self.assertEqual(by_policy["conditional_verified"]["action"], "clear_to_blank")
            self.assertEqual(by_policy["derived_needs_confirmation"]["action"], "clear_to_blank")
            self.assertEqual(by_policy["optional_verified"]["action"], "clear_to_blank")
            blank_codes = {
                item["code"] for item in validate_fill_plan(blank_policy_plan, policy_profile)["errors"]
            }
            self.assertNotIn("CONDITION_DECISION_REQUIRED", blank_codes)
            self.assertNotIn("DERIVED_VALUE_CONFIRMATION_REQUIRED", blank_codes)

            unsafe_policy_plan = build_fill_plan(
                policy_path,
                {
                    "conditional_value": "TEST-ONLY条件值",
                    "derived_value": "TEST-ONLY推导值",
                    "fee_amount": "999999999999元",
                    "SLOT-0001": "不得覆盖固定标题",
                },
                {
                    "conditional_value": {"applies": True},
                    "derived_value": {"derivation_basis": "TEST-ONLY模型推导"},
                },
            )
            unsafe_validation = validate_fill_plan(unsafe_policy_plan, policy_profile)
            unsafe_codes = {item["code"] for item in unsafe_validation["errors"]}
            self.assertIn("CONDITIONAL_VALUE_UNVERIFIED", unsafe_codes)
            self.assertIn("DERIVED_VALUE_CONFIRMATION_REQUIRED", unsafe_codes)
            self.assertIn("OPTIONAL_VALUE_UNVERIFIED", unsafe_codes)
            self.assertIn("FILL_PLAN_CONFLICTS_UNRESOLVED", unsafe_codes)
            self.assertEqual(
                unsafe_validation["confirmation_batch"],
                [{"slot_id": client_slots[1]["slot_id"], "reason": "derived_value"}],
            )

            positive_data = {
                "conditional_value": "TEST-ONLY条件值",
                "derived_value": "TEST-ONLY经确认推导值",
                "fee_amount": "999999999999元",
            }
            seed_plan = build_fill_plan(
                policy_path,
                positive_data,
                {
                    "conditional_value": {"applies": True},
                    "derived_value": {"derivation_basis": "TEST-ONLY确定规则"},
                },
            )
            seed_rows = {item["field_key"]: item for item in seed_plan["slots"]}
            policy_source = {
                "id": "TEST-SRC-POLICY-001",
                "kind": "original_material",
                "verified": True,
                "sha256": "c" * 64,
            }
            policy_facts = []
            positive_provenance = {
                "conditional_value": {"applies": True, "source_refs": []},
                "derived_value": {
                    "derivation_basis": "TEST-ONLY确定规则",
                    "confirmation": {},
                },
                "fee_amount": {"source_refs": []},
            }
            for index, field_key in enumerate(("conditional_value", "fee_amount"), start=1):
                binding = seed_rows[field_key]["binding"]
                locator = f"第1页第{index}行"
                fact = {
                    "id": f"TEST-FACT-POLICY-{index:03d}",
                    "statement": f"TEST-ONLY：材料直接记载{field_key}。",
                    "status": "confirmed",
                    "field_key": field_key,
                    "source_locators": [{
                        "source_id": policy_source["id"],
                        "locator": locator,
                        "record_kind": "direct_record",
                        "quote": None,
                        "verified": True,
                    }],
                    "normalized_value_sha256": binding["value_sha256"],
                }
                fact_hash = object_digest(fact)
                policy_facts.append({**fact, "object_sha256": fact_hash})
                positive_provenance[field_key]["source_refs"].append({
                    "id": policy_source["id"],
                    "kind": "original_material",
                    "verified": True,
                    "sha256": policy_source["sha256"],
                    "object_sha256": object_digest(policy_source),
                    "fact_id": fact["id"],
                    "fact_object_sha256": fact_hash,
                    "locator": locator,
                    "extracted_value_sha256": binding["value_sha256"],
                    "binding": binding,
                })
            derived_binding = seed_rows["derived_value"]["binding"]
            derived_decision = {
                "decision_id": "TEST-DECISION-DERIVED-001",
                "status": "approved",
                "actor_role": "lawyer",
                "fill_binding": derived_binding,
            }
            decision_hash = object_digest(derived_decision)
            positive_provenance["derived_value"]["confirmation"] = {
                "status": "approved",
                "actor_role": "lawyer",
                "decision_id": derived_decision["decision_id"],
                "object_sha256": decision_hash,
                "approval_id": "TEST-APPROVAL-DERIVED-001",
                "fill_binding": derived_binding,
            }
            policy_state = {
                "state_version": 1,
                "sources": [policy_source],
                "facts": [
                    {key: value for key, value in item.items() if key != "object_sha256"}
                    for item in policy_facts
                ],
                "decisions": [derived_decision],
            }
            policy_state_path = root / "_case-state" / "policy-case-state.json"
            policy_state_path.parent.mkdir(parents=True, exist_ok=True)
            policy_state_path.write_text(
                json.dumps(policy_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            policy_snapshot = {
                "case_state_path": str(policy_state_path),
                "case_state_sha256": sha256_file(policy_state_path),
                "state_version": 1,
                "sources": [{**policy_source, "object_sha256": object_digest(policy_source)}],
                "facts": policy_facts,
                "decisions": [{**derived_decision, "object_sha256": decision_hash}],
            }
            policy_snapshot["snapshot_sha256"] = object_digest(policy_snapshot)
            positive_provenance["_registry_snapshot"] = policy_snapshot
            positive_plan = build_fill_plan(policy_path, positive_data, positive_provenance)
            positive_validation = validate_fill_plan(positive_plan, policy_profile)
            self.assertTrue(positive_validation["ok"], positive_validation)
            self.assertEqual(positive_plan["status"], "ready")

    def test_active_bypass_and_unclassified_empty_cells_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-active-gates-") as temporary:
            root = Path(temporary)
            source = root / "TEST-ONLY-empty-cell.docx"
            make_empty_cell_docx(source)
            result = distill_template(
                source,
                root / "profiles",
                "form",
                "fillable_clone",
                "TEST-EMPTY-CELL",
                "TEST-ONLY空白格",
                "手续",
                overrides={
                    "slot_policies": {
                        "SLOT-0002": {
                            "policy": "manual_blank",
                            "field_key": "old_case_fact",
                            "block_original_text_in_output": True
                        },
                        "client_name": "required_verified",
                        "fee_amount": "lawyer_decision_required"
                    },
                    "approve_all_fixed_as_boilerplate": True,
                    "unresolved_items": []
                },
            )
            bypassed = result["profile"]
            bypassed["status"] = "active"
            validation = validate_profile(bypassed, require_active=True)
            codes = {item["code"] for item in validation["errors"]}
            self.assertIn("PROFILE_APPROVAL_REQUIRED", codes)
            self.assertIn("VISUAL_BASELINE_REQUIRED", codes)
            self.assertIn("UNANCHORED_EMPTY_CELLS_UNRESOLVED", codes)

    def test_stale_hash_and_unexpected_package_change_fail_closed(self) -> None:
        fixture = json.loads((FIXTURE_ROOT / "TEST-ONLY-field-input.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-stale-") as temporary:
            root = Path(temporary)
            source, profile = activated_form_profile(root)
            profile_path = root / "profiles" / "TEST-FORM-001.form-profile.json"
            data, provenance = bind_case_state_snapshot(root, fixture, profile)
            plan = build_fill_plan(profile_path, data, provenance)
            stale = root / "TEST-ONLY-stale.docx"
            make_test_docx(stale)
            with zipfile.ZipFile(stale, "a") as package:
                package.writestr("TEST-ONLY-extra.txt", b"stale")
            with self.assertRaises(LegalCaseError) as raised:
                fill_docx_from_plan(stale, root / "must-not-exist.docx", profile, plan)
            self.assertEqual(raised.exception.code, "SOURCE_TEMPLATE_HASH_MISMATCH")
            self.assertFalse((root / "must-not-exist.docx").exists())

            candidate = root / "TEST-ONLY-structure-change.docx"
            make_test_docx(candidate)
            with zipfile.ZipFile(candidate, "r") as package:
                entries = [(info, package.read(info.filename)) for info in package.infolist()]
            replacement = root / "TEST-ONLY-repacked.docx"
            with zipfile.ZipFile(replacement, "w") as package:
                for info, data in entries:
                    if info.filename == "word/styles.xml":
                        data = data.replace(b"Normal", b"Changed")
                    package.writestr(info, data)
            diff = compare_docx_packages(candidate, replacement, allowed_changed_parts=["word/document.xml"])
            self.assertFalse(diff["ok"])
            self.assertEqual(diff["unexpected_changed_parts"], ["word/styles.xml"])

    def test_legacy_conversion_refuses_overwrite_and_missing_explicit_converter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-legacy-") as temporary:
            root = Path(temporary)
            source = root / "TEST-ONLY-old.doc"
            source.write_bytes(b"TEST-ONLY legacy placeholder")
            before = sha256_file(source)
            existing = root / "existing.docx"
            existing.write_bytes(b"do not overwrite")
            with self.assertRaises(LegalCaseError) as raised:
                convert_legacy_doc(source, existing, soffice=root / "missing-soffice.exe")
            self.assertEqual(raised.exception.code, "OUTPUT_EXISTS")
            self.assertEqual(existing.read_bytes(), b"do not overwrite")
            with self.assertRaises(LegalCaseError) as raised:
                convert_legacy_doc(source, root / "new.docx", soffice=root / "missing-soffice.exe")
            self.assertEqual(raised.exception.code, "LEGACY_DOC_CONVERTER_UNAVAILABLE")
            self.assertEqual(before, sha256_file(source))

    def test_legacy_conversion_optional_dual_render_report_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-legacy-render-") as temporary:
            root = Path(temporary)
            source = root / "TEST-ONLY-old.doc"
            source.write_bytes(b"TEST-ONLY legacy source")
            converter = root / "TEST-ONLY-soffice.exe"
            converter.write_bytes(b"TEST-ONLY fake executable")
            destination = root / "derived.docx"
            report_path = root / "legacy-render-report.json"

            def fake_run(args, **_kwargs):
                if "--version" in args:
                    return subprocess.CompletedProcess(args, 0, "LibreOffice TEST-ONLY\n", "")
                target = args[args.index("--convert-to") + 1]
                output_dir = Path(args[args.index("--outdir") + 1])
                input_path = Path(args[-1])
                output_dir.mkdir(parents=True, exist_ok=True)
                if target == "docx":
                    make_test_docx(output_dir / f"{input_path.stem}.docx", version_marker="converted")
                else:
                    (output_dir / f"{input_path.stem}.pdf").write_bytes(
                        b"%PDF-1.4\n1 0 obj<</Type /Page>>endobj\n%%EOF"
                    )
                return subprocess.CompletedProcess(args, 0, "TEST-ONLY converted\n", "")

            with mock.patch.object(curation.subprocess, "run", side_effect=fake_run):
                result = convert_legacy_doc(
                    source,
                    destination,
                    soffice=converter,
                    render_report=report_path,
                )
            self.assertTrue(result["page_count_equal"])
            self.assertTrue(result["registration_blocked_until_render_review"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["source_direct_pdf"]["page_count"], 1)
            self.assertEqual(report["derived_docx_pdf"]["page_count"], 1)
            self.assertEqual(report["comparison"]["visual_review_status"], "pending")
            self.assertFalse(report["registration_eligible"])
            self.assertEqual(report["source_doc"]["sha256"], sha256_file(source))
            self.assertEqual(report["derived_docx"]["sha256"], sha256_file(destination))

            mismatch_destination = root / "mismatch.docx"
            mismatch_report = root / "mismatch-report.json"

            def fake_mismatched_run(args, **_kwargs):
                if "--version" in args:
                    return subprocess.CompletedProcess(args, 0, "LibreOffice TEST-ONLY\n", "")
                target = args[args.index("--convert-to") + 1]
                output_dir = Path(args[args.index("--outdir") + 1])
                input_path = Path(args[-1])
                output_dir.mkdir(parents=True, exist_ok=True)
                if target == "docx":
                    make_test_docx(output_dir / f"{input_path.stem}.docx", version_marker="converted")
                else:
                    pages = 2 if input_path.suffix.casefold() == ".docx" else 1
                    payload = b"%PDF-1.4\n" + b"1 0 obj<</Type /Page>>endobj\n" * pages + b"%%EOF"
                    (output_dir / f"{input_path.stem}.pdf").write_bytes(payload)
                return subprocess.CompletedProcess(args, 0, "TEST-ONLY converted\n", "")

            with mock.patch.object(curation.subprocess, "run", side_effect=fake_mismatched_run):
                with self.assertRaises(LegalCaseError) as raised:
                    convert_legacy_doc(
                        source,
                        mismatch_destination,
                        soffice=converter,
                        render_report=mismatch_report,
                    )
            self.assertEqual(raised.exception.code, "LEGACY_RENDER_PAGE_COUNT_MISMATCH")
            self.assertFalse(mismatch_destination.exists())
            self.assertFalse(mismatch_report.exists())

    def test_unsupported_repeatable_and_writing_hybrid_fail_explicitly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-capability-") as temporary:
            root = Path(temporary)
            source, profile = activated_form_profile(root)
            editable = next(slot for slot in profile["slots"] if slot.get("field_key") == "client_name")
            editable["policy"] = "repeatable"
            result = validate_profile(profile, require_active=True)
            self.assertIn(
                "REPEATABLE_ROW_CLONING_UNSUPPORTED",
                {item["code"] for item in result["errors"]},
            )

            writing = distill_template(
                source,
                root / "writing-profile",
                "writing",
                "hybrid",
                "TEST-WRITING-HYBRID",
                "TEST-ONLY hybrid",
                "质证意见",
            )["profile"]
            writing["status"] = "active"
            writing["approval"] = APPROVAL
            writing["unresolved_items"] = []
            writing["visual_baseline"] = make_visual_baseline(root / "writing", source)
            result = validate_profile(writing, require_active=True)
            self.assertIn(
                "WRITING_HYBRID_COMPOSITION_UNSUPPORTED",
                {item["code"] for item in result["errors"]},
            )

    def test_registration_requires_explicit_approval_and_never_changes_original(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-register-") as temporary:
            root = Path(temporary)
            source = root / "incoming" / "TEST-ONLY-form.docx"
            make_test_docx(source)
            before = sha256_file(source)
            result = distill_template(
                source,
                root / "library" / "模板库" / "_profiles",
                "form",
                "fillable_clone",
                "TEST-REGISTER-001",
                "TEST-ONLY登记模板",
                "授权委托书",
                overrides={
                    "slot_policies": {
                        "SLOT-0002": {
                            "policy": "manual_blank",
                            "field_key": "old_case_fact",
                            "block_original_text_in_output": True
                        },
                        "client_name": "required_verified",
                        "fee_amount": "lawyer_decision_required",
                        "signature": "manual_blank"
                    },
                    "approve_all_fixed_as_boilerplate": True,
                    "visual_baseline": make_visual_baseline(root, source),
                    "unresolved_items": []
                },
            )
            catalog = root / "library" / "模板库" / "_registry" / "template-catalog.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                json.dumps({"catalog_version": "1.0.0", "templates": [], "suites": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(LegalCaseError) as raised:
                register_template(
                    source,
                    result["profile_path"],
                    catalog,
                    root / "library" / "模板库" / "10-手续套件",
                    operation="create",
                    approval={"confirmed": False},
                )
            self.assertEqual(raised.exception.code, "TEMPLATE_APPROVAL_REQUIRED")
            registered = register_template(
                source,
                result["profile_path"],
                catalog,
                root / "library" / "模板库" / "10-手续套件",
                operation="create",
                approval=registration_receipt(
                    catalog,
                    operation="create",
                    template_id="TEST-REGISTER-001",
                    target_version="1.0.0",
                    decision_id="TEST-APPROVAL-001",
                    source=source,
                    profile=Path(result["profile_path"]),
                ),
            )
            self.assertTrue(Path(registered["active_path"]).is_file())
            self.assertEqual(before, sha256_file(source))
            self.assertTrue(validate_profile(result["profile_path"], require_active=True)["ok"])
            self.assertTrue(templates.validate_personal_template_catalog(catalog)["ok"])

    def test_template_approval_receipt_is_exact_and_cannot_cross_operation_or_replay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-template-receipt-") as temporary:
            root = Path(temporary)
            source = root / "incoming" / "TEST-ONLY-form.docx"
            make_test_docx(source)
            distilled = distill_template(
                source,
                root / "library" / "模板库" / "_profiles",
                "form", "fillable_clone", "TEST-RECEIPT-001", "TEST-ONLY收据模板", "授权委托书",
                overrides={
                    "slot_policies": {
                        "SLOT-0002": {"policy": "manual_blank", "field_key": "old_case_fact", "block_original_text_in_output": True},
                        "client_name": "required_verified", "fee_amount": "lawyer_decision_required", "signature": "manual_blank",
                    },
                    "approve_all_fixed_as_boilerplate": True,
                    "visual_baseline": make_visual_baseline(root, source),
                    "unresolved_items": [],
                },
            )
            profile = Path(distilled["profile_path"])
            catalog = make_catalog(root)
            destination = root / "library" / "模板库" / "10-手续套件"
            common = {
                "decision_id": "APR-TEMPLATE-RECEIPT-001",
                "approved_by": "TEST-ONLY-lawyer",
                "template_id": "TEST-RECEIPT-001",
                "target_version": "1.0.0",
                "source_sha256": sha256_file(source),
                "profile_sha256": sha256_file(profile),
                "catalog_before_sha256": sha256_file(catalog),
                "decided_at": "2026-08-25T00:00:00Z",
            }
            wrong_operation = build_template_approval_receipt(operation="upgrade", **common)
            with self.assertRaises(LegalCaseError) as raised:
                register_template(
                    source, profile, catalog, destination,
                    operation="create", version="1.0.0", approval=wrong_operation,
                )
            self.assertEqual(raised.exception.code, "TEMPLATE_APPROVAL_RECEIPT_SCOPE_MISMATCH")

            correct = build_template_approval_receipt(operation="create", **common)
            created = register_template(
                source, profile, catalog, destination,
                operation="create", version="1.0.0", approval=correct,
            )
            self.assertTrue(created["ok"])
            with self.assertRaises(LegalCaseError) as replayed:
                register_template(
                    None, None, catalog, None,
                    operation="retire", template_id="TEST-RECEIPT-001", approval=correct,
                )
            self.assertEqual(replayed.exception.code, "TEMPLATE_APPROVAL_RECEIPT_REPLAYED")

    def test_registration_upgrade_and_retire_preserve_every_revision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-lifecycle-") as temporary:
            root = Path(temporary)
            template_id = "TEST-LIFECYCLE-001"
            source_v1 = root / "incoming" / "v1.docx"
            make_test_docx(source_v1, version_marker="v1")
            draft_v1 = ready_registration_draft(root, source_v1, template_id, "v1")
            catalog = make_catalog(root)
            destination_dir = root / "library" / "模板库" / "10-手续套件"
            created = register_template(
                source_v1,
                draft_v1["profile_path"],
                catalog,
                destination_dir,
                operation="create",
                version="1.0.0",
                approval=registration_receipt(
                    catalog, operation="create", template_id=template_id,
                    target_version="1.0.0", decision_id="TEST-APPROVAL-001",
                    source=source_v1, profile=Path(draft_v1["profile_path"]),
                ),
            )
            old_template = Path(created["active_path"])
            old_profile = Path(created["profile_path"])
            old_template_hash = sha256_file(old_template)

            unchanged_draft = ready_registration_draft(root, source_v1, template_id, "same-source")
            with self.assertRaises(LegalCaseError) as raised:
                register_template(
                    source_v1,
                    unchanged_draft["profile_path"],
                    catalog,
                    destination_dir,
                    operation="upgrade",
                    version="2.0.0",
                    approval=registration_receipt(
                        catalog, operation="upgrade", template_id=template_id,
                        target_version="2.0.0", decision_id="TEST-UPGRADE-UNCHANGED",
                        source=source_v1, profile=Path(unchanged_draft["profile_path"]),
                    ),
                )
            self.assertEqual(raised.exception.code, "TEMPLATE_UPGRADE_SOURCE_UNCHANGED")

            source_v2 = root / "incoming" / "v2.docx"
            make_test_docx(source_v2, version_marker="v2")
            draft_v2 = ready_registration_draft(root, source_v2, template_id, "v2")
            with self.assertRaises(LegalCaseError) as raised:
                register_template(
                    source_v2,
                    draft_v2["profile_path"],
                    catalog,
                    destination_dir,
                    operation="upgrade",
                    version="1.0.0",
                    approval=registration_receipt(
                        catalog, operation="upgrade", template_id=template_id,
                        target_version="1.0.0", decision_id="TEST-UPGRADE-OLD-VERSION",
                        source=source_v2, profile=Path(draft_v2["profile_path"]),
                    ),
                )
            self.assertEqual(raised.exception.code, "TEMPLATE_VERSION_NOT_NEWER")
            with self.assertRaises(LegalCaseError) as raised:
                register_template(
                    source_v2,
                    draft_v2["profile_path"],
                    catalog,
                    destination_dir,
                    operation="upgrade",
                    version="2.0.0",
                    approval={"confirmed": False},
                )
            self.assertEqual(raised.exception.code, "TEMPLATE_APPROVAL_REQUIRED")

            upgraded = register_template(
                source_v2,
                draft_v2["profile_path"],
                catalog,
                destination_dir,
                operation="upgrade",
                version="2.0.0",
                approval=registration_receipt(
                    catalog, operation="upgrade", template_id=template_id,
                    target_version="2.0.0", decision_id="TEST-UPGRADE-001",
                    source=source_v2, profile=Path(draft_v2["profile_path"]),
                ),
            )
            new_template = Path(upgraded["active_path"])
            new_profile = Path(upgraded["profile_path"])
            self.assertNotEqual(old_template, new_template)
            self.assertNotEqual(old_profile, new_profile)
            self.assertTrue(old_template.is_file())
            self.assertEqual(sha256_file(old_template), old_template_hash)
            self.assertEqual(json.loads(old_profile.read_text(encoding="utf-8"))["status"], "retired")
            catalog_value = json.loads(catalog.read_text(encoding="utf-8"))
            entry = catalog_value["templates"][0]
            self.assertEqual(entry["version"], "2.0.0")
            self.assertEqual(entry["status"], "active")
            self.assertEqual(entry["profile_sha256"], sha256_file(new_profile))
            self.assertEqual(entry["revision_history"][-1]["status"], "superseded")
            self.assertTrue(templates.validate_personal_template_catalog(catalog)["ok"])

            new_template_before = new_template.read_bytes()
            new_profile_before = new_profile.read_bytes()
            with self.assertRaises(LegalCaseError) as raised:
                register_template(
                    None,
                    None,
                    catalog,
                    None,
                    operation="retire",
                    template_id=template_id,
                    approval={"confirmed": False},
                )
            self.assertEqual(raised.exception.code, "TEMPLATE_APPROVAL_REQUIRED")
            retired = register_template(
                None,
                None,
                catalog,
                None,
                operation="retire",
                template_id=template_id,
                approval=registration_receipt(
                    catalog, operation="retire", template_id=template_id,
                    target_version="2.0.0", decision_id="TEST-RETIRE-001",
                ),
            )
            self.assertTrue(retired["files_preserved"])
            self.assertEqual(new_template.read_bytes(), new_template_before)
            self.assertEqual(new_profile.read_bytes(), new_profile_before)
            retired_entry = json.loads(catalog.read_text(encoding="utf-8"))["templates"][0]
            self.assertEqual(retired_entry["status"], "retired")
            self.assertTrue(templates.validate_personal_template_catalog(catalog)["ok"])

    def test_upgrade_and_retire_roll_back_on_catalog_commit_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-lifecycle-rollback-") as temporary:
            root = Path(temporary)
            template_id = "TEST-ROLLBACK-001"
            source_v1 = root / "incoming" / "v1.docx"
            make_test_docx(source_v1, version_marker="v1")
            draft_v1 = ready_registration_draft(root, source_v1, template_id, "v1")
            catalog = make_catalog(root)
            destination_dir = root / "library" / "模板库" / "10-手续套件"
            created = register_template(
                source_v1,
                draft_v1["profile_path"],
                catalog,
                destination_dir,
                operation="create",
                version="1.0.0",
                approval=registration_receipt(
                    catalog, operation="create", template_id=template_id,
                    target_version="1.0.0", decision_id="TEST-APPROVAL-ROLLBACK-CREATE",
                    source=source_v1, profile=Path(draft_v1["profile_path"]),
                ),
            )
            active_profile = Path(created["profile_path"])
            source_v2 = root / "incoming" / "v2.docx"
            make_test_docx(source_v2, version_marker="v2")
            draft_v2 = ready_registration_draft(root, source_v2, template_id, "v2")
            new_profile = Path(draft_v2["profile_path"])
            catalog_before = catalog.read_bytes()
            old_profile_before = active_profile.read_bytes()
            new_profile_before = new_profile.read_bytes()
            original_atomic_write = curation.atomic_write_json

            def fail_catalog_write(path, value):
                if Path(path).resolve() == catalog.resolve():
                    raise OSError("TEST-ONLY simulated catalog failure")
                return original_atomic_write(path, value)

            with mock.patch.object(curation, "atomic_write_json", side_effect=fail_catalog_write):
                with self.assertRaises(OSError):
                    register_template(
                        source_v2,
                        new_profile,
                        catalog,
                        destination_dir,
                        operation="upgrade",
                        version="2.0.0",
                        approval=registration_receipt(
                            catalog, operation="upgrade", template_id=template_id,
                            target_version="2.0.0", decision_id="TEST-UPGRADE-ROLLBACK",
                            source=source_v2, profile=new_profile,
                        ),
                    )
            self.assertEqual(catalog.read_bytes(), catalog_before)
            self.assertEqual(active_profile.read_bytes(), old_profile_before)
            self.assertEqual(new_profile.read_bytes(), new_profile_before)
            self.assertFalse((destination_dir / f"{template_id}-2.0.0.docx").exists())

            with mock.patch.object(curation, "atomic_write_json", side_effect=fail_catalog_write):
                with self.assertRaises(OSError):
                    register_template(
                        None,
                        None,
                        catalog,
                        None,
                        operation="retire",
                        template_id=template_id,
                        approval=registration_receipt(
                            catalog, operation="retire", template_id=template_id,
                            target_version="1.0.0", decision_id="TEST-RETIRE-ROLLBACK",
                        ),
                    )
            self.assertEqual(catalog.read_bytes(), catalog_before)
            self.assertEqual(active_profile.read_bytes(), old_profile_before)
            self.assertTrue(Path(created["active_path"]).is_file())

    def test_catalog_rollback_never_clobbers_a_later_external_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-rollback-cas-create-") as temporary:
            root = Path(temporary)
            template_id = "TEST-ROLLBACK-CAS-CREATE"
            source = root / "incoming" / "source.docx"
            make_test_docx(source)
            draft = ready_registration_draft(root, source, template_id, "create")
            profile = Path(draft["profile_path"])
            catalog = make_catalog(root)
            destination_dir = root / "library" / "模板库" / "10-手续套件"

            def external_create_write(path: Path, *, operation: str) -> None:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                value["external_marker"] = f"later-{operation}"
                curation.atomic_write_json(Path(path), value)
                raise RuntimeError("TEST-ONLY validation failed after an external write")

            with mock.patch.object(
                curation,
                "_validate_catalog_or_raise",
                side_effect=external_create_write,
            ):
                with self.assertRaises(LegalCaseError) as raised:
                    register_template(
                        source,
                        profile,
                        catalog,
                        destination_dir,
                        operation="create",
                        version="1.0.0",
                        approval=registration_receipt(
                            catalog,
                            operation="create",
                            template_id=template_id,
                            target_version="1.0.0",
                            decision_id="TEST-ROLLBACK-CAS-CREATE-APPROVAL",
                            source=source,
                            profile=profile,
                        ),
                    )
            self.assertEqual(raised.exception.code, "TEMPLATE_CATALOG_CHANGED_DURING_ROLLBACK")
            catalog_after = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertEqual(catalog_after["external_marker"], "later-create")
            self.assertEqual(catalog_after["templates"][0]["id"], template_id)
            self.assertTrue((destination_dir / f"{template_id}-1.0.0.docx").is_file())
            self.assertEqual(json.loads(profile.read_text(encoding="utf-8"))["status"], "active")

        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-rollback-cas-retire-") as temporary:
            root = Path(temporary)
            template_id = "TEST-ROLLBACK-CAS-RETIRE"
            source = root / "incoming" / "source.docx"
            make_test_docx(source)
            draft = ready_registration_draft(root, source, template_id, "retire")
            profile = Path(draft["profile_path"])
            catalog = make_catalog(root)
            destination_dir = root / "library" / "模板库" / "10-手续套件"
            register_template(
                source,
                profile,
                catalog,
                destination_dir,
                operation="create",
                version="1.0.0",
                approval=registration_receipt(
                    catalog,
                    operation="create",
                    template_id=template_id,
                    target_version="1.0.0",
                    decision_id="TEST-ROLLBACK-CAS-RETIRE-CREATE",
                    source=source,
                    profile=profile,
                ),
            )

            def external_retire_write(path: Path, *, operation: str) -> None:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                value["external_marker"] = f"later-{operation}"
                curation.atomic_write_json(Path(path), value)
                raise RuntimeError("TEST-ONLY validation failed after an external write")

            with mock.patch.object(
                curation,
                "_validate_catalog_or_raise",
                side_effect=external_retire_write,
            ):
                with self.assertRaises(LegalCaseError) as raised:
                    register_template(
                        None,
                        None,
                        catalog,
                        None,
                        operation="retire",
                        template_id=template_id,
                        approval=registration_receipt(
                            catalog,
                            operation="retire",
                            template_id=template_id,
                            target_version="1.0.0",
                            decision_id="TEST-ROLLBACK-CAS-RETIRE-APPROVAL",
                        ),
                    )
            self.assertEqual(raised.exception.code, "TEMPLATE_CATALOG_CHANGED_DURING_ROLLBACK")
            catalog_after = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertEqual(catalog_after["external_marker"], "later-retire")
            self.assertEqual(catalog_after["templates"][0]["status"], "retired")

    def test_relative_profile_paths_survive_registration_and_fill(self) -> None:
        fixture = json.loads((FIXTURE_ROOT / "TEST-ONLY-field-input.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-relative-profile-") as temporary:
            root = Path(temporary)
            source = root / "incoming" / "portable.docx"
            make_test_docx(source)
            library_root = root / "library" / "模板库"
            draft = ready_registration_draft(
                root,
                source,
                "TEST-PORTABLE-001",
                "portable",
                baseline_root=library_root / "_profiles" / "portable-baseline",
            )
            profile_path = Path(draft["profile_path"])
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertFalse(Path(profile["source"]["path"]).is_absolute())
            self.assertTrue(all(
                not Path(page["path"]).is_absolute() and "\\" not in page["path"]
                for page in profile["visual_baseline"]["pages"]
            ))

            catalog = make_catalog(root)
            destination_dir = library_root / "10-手续套件"
            registered = register_template(
                source,
                profile_path,
                catalog,
                destination_dir,
                operation="create",
                version="1.0.0",
                approval=registration_receipt(
                    catalog,
                    operation="create",
                    template_id="TEST-PORTABLE-001",
                    target_version="1.0.0",
                    decision_id="TEST-PORTABLE-APPROVAL",
                    source=source,
                    profile=profile_path,
                ),
            )
            active_profile_path = Path(registered["profile_path"])
            active_profile = json.loads(active_profile_path.read_text(encoding="utf-8"))
            self.assertFalse(Path(active_profile["source"]["path"]).is_absolute())
            catalog_entry = json.loads(catalog.read_text(encoding="utf-8"))["templates"][0]
            for field in ("path", "profile_path"):
                self.assertFalse(Path(catalog_entry[field]).is_absolute())
                self.assertNotIn("\\", catalog_entry[field])
            self.assertTrue(validate_profile(active_profile_path, require_active=True)["ok"])

            relocated_root = root / "relocated-template-library"
            shutil.copytree(library_root, relocated_root)
            shutil.rmtree(library_root)
            self.assertFalse(library_root.exists())
            relocated_catalog = relocated_root / "_registry" / "template-catalog.json"
            catalog_validation = templates.validate_personal_template_catalog(relocated_catalog)
            self.assertTrue(catalog_validation["ok"], catalog_validation)
            relocated_entry = json.loads(relocated_catalog.read_text(encoding="utf-8"))["templates"][0]
            relocated_profile_path = (relocated_catalog.parent / relocated_entry["profile_path"]).resolve()
            relocated_source_path = (relocated_catalog.parent / relocated_entry["path"]).resolve()
            relocated_profile = json.loads(relocated_profile_path.read_text(encoding="utf-8"))
            resolved_paths = [
                relocated_source_path,
                (relocated_profile_path.parent / relocated_profile["source"]["path"]).resolve(),
                *(
                    (relocated_profile_path.parent / page["path"]).resolve()
                    for page in relocated_profile["visual_baseline"]["pages"]
                ),
            ]
            self.assertTrue(all(path.is_relative_to(relocated_root.resolve()) for path in resolved_paths))
            self.assertTrue(validate_profile(relocated_profile_path, require_active=True)["ok"])

            data, provenance = bind_case_state_snapshot(root, fixture, relocated_profile)
            plan = build_fill_plan(relocated_profile_path, data, provenance)
            self.assertEqual(plan["status"], "ready")
            output = root / "portable-filled.docx"
            result = fill_docx_from_plan(
                relocated_source_path,
                output,
                relocated_profile_path,
                plan,
            )
            self.assertTrue(result["ok"])

    def test_template_writers_reject_originals_and_ai_approver(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-originals-guard-") as temporary:
            root = Path(temporary)
            source = root / "incoming" / "source.docx"
            make_test_docx(source)
            blocked_root = root / "matter" / "00-originals"
            with self.assertRaises(LegalCaseError) as blocked:
                distill_template(
                    source,
                    blocked_root / "profiles",
                    "form",
                    "fillable_clone",
                    "TEST-BLOCKED-001",
                    "TEST-ONLY阻断",
                    "授权委托书",
                )
            self.assertEqual(blocked.exception.code, "ORIGINALS_READ_ONLY")
            self.assertFalse(blocked_root.exists())

            catalog = make_catalog(root)
            for index, approver in enumerate(
                ("AI assistant", "GPT-5", "ChatGPT", "Claude", "DeepSeek", "Qwen", "千问", "豆包"),
                start=1,
            ):
                ai_receipt = build_template_approval_receipt(
                    decision_id=f"TEST-AI-APPROVAL-{index}",
                    approved_by=approver,
                    operation="retire",
                    template_id="TEST-NOT-PRESENT",
                    target_version="1.0.0",
                    source_sha256="a" * 64,
                    profile_sha256="b" * 64,
                    catalog_before_sha256=sha256_file(catalog),
                    decided_at="2026-08-25T00:00:00Z",
                )
                with self.subTest(approver=approver):
                    with self.assertRaises(LegalCaseError) as nonhuman:
                        register_template(
                            None,
                            None,
                            catalog,
                            None,
                            operation="retire",
                            template_id="TEST-NOT-PRESENT",
                            approval=ai_receipt,
                        )
                    self.assertEqual(nonhuman.exception.code, "TEMPLATE_APPROVAL_HUMAN_REQUIRED")

            library_root = root / "library" / "模板库"
            originals = library_root / "00-originals"
            originals.mkdir(parents=True)
            protected_profile = originals / "protected-profile.json"
            protected_profile.write_text("{}\n", encoding="utf-8")
            protected_before = protected_profile.read_bytes()
            protected_catalog = library_root / "_registry" / "template-catalog.json"
            protected_catalog.parent.mkdir(parents=True, exist_ok=True)
            protected_entry = {
                "id": "TEST-PROTECTED-RETIRE",
                "version": "1.0.0",
                "status": "active",
                "path": "../00-originals/protected.docx",
                "sha256": "c" * 64,
                "profile_path": "../00-originals/protected-profile.json",
                "profile_sha256": sha256_file(protected_profile),
            }
            protected_catalog.write_text(
                json.dumps(
                    {"catalog_version": "1.0.0", "templates": [protected_entry], "suites": []},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            protected_receipt = build_template_approval_receipt(
                decision_id="TEST-PROTECTED-RETIRE-APPROVAL",
                approved_by="TEST-ONLY-lawyer",
                operation="retire",
                template_id=protected_entry["id"],
                target_version=protected_entry["version"],
                source_sha256=protected_entry["sha256"],
                profile_sha256=protected_entry["profile_sha256"],
                catalog_before_sha256=sha256_file(protected_catalog),
                decided_at="2026-08-25T00:00:00Z",
            )
            with self.assertRaises(LegalCaseError) as protected:
                register_template(
                    None,
                    None,
                    protected_catalog,
                    None,
                    operation="retire",
                    template_id=protected_entry["id"],
                    approval=protected_receipt,
                )
            self.assertEqual(protected.exception.code, "ORIGINALS_READ_ONLY")
            self.assertEqual(protected_profile.read_bytes(), protected_before)

    def test_parallel_catalog_create_is_serialized_without_orphans(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-template-concurrency-") as temporary:
            root = Path(temporary)
            catalog = make_catalog(root)
            destination_dir = root / "library" / "模板库" / "10-手续套件"
            jobs = []
            for suffix in ("A", "B"):
                template_id = f"TEST-CONCURRENT-{suffix}"
                source = root / "incoming" / f"{suffix}.docx"
                make_test_docx(source, version_marker=suffix)
                draft = ready_registration_draft(root, source, template_id, f"concurrent-{suffix}")
                profile = Path(draft["profile_path"])
                jobs.append((
                    template_id,
                    source,
                    profile,
                    registration_receipt(
                        catalog,
                        operation="create",
                        template_id=template_id,
                        target_version="1.0.0",
                        decision_id=f"TEST-CONCURRENT-APPROVAL-{suffix}",
                        source=source,
                        profile=profile,
                    ),
                ))

            barrier = threading.Barrier(2)
            outcomes: list[tuple[str, str]] = []
            outcome_lock = threading.Lock()

            def worker(job) -> None:
                template_id, source, profile, receipt = job
                barrier.wait(timeout=5)
                try:
                    register_template(
                        source,
                        profile,
                        catalog,
                        destination_dir,
                        operation="create",
                        version="1.0.0",
                        approval=receipt,
                    )
                    outcome = (template_id, "ok")
                except LegalCaseError as exc:
                    outcome = (template_id, exc.code)
                with outcome_lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=worker, args=(job,), daemon=True) for job in jobs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertEqual(sum(status == "ok" for _identifier, status in outcomes), 1, outcomes)
            self.assertEqual(
                sum(status == "TEMPLATE_APPROVAL_RECEIPT_SCOPE_MISMATCH" for _identifier, status in outcomes),
                1,
                outcomes,
            )
            catalog_value = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertEqual(len(catalog_value["templates"]), 1)
            self.assertEqual(len(list(destination_dir.glob("*.docx"))), 1)

    def test_non_numeric_version_order_fails_closed(self) -> None:
        self.assertFalse(curation._version_is_newer("v2", "v1"))
        self.assertFalse(curation._version_is_newer("2.0.0-beta", "1.0.0-alpha"))
        self.assertTrue(curation._version_is_newer("1.0.0-beta", "1.0.0"))
        self.assertFalse(curation._version_is_newer("release-one", "release-two"))

    def test_schema_files_are_valid_json_and_test_only_fixture_is_marked(self) -> None:
        for name in ("form-profile.schema.json", "writing-profile.schema.json", "fill-plan.schema.json"):
            schema = json.loads((PROJECT_ROOT / "shared" / "schemas" / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertIn("$id", schema)
        fixture = json.loads((FIXTURE_ROOT / "TEST-ONLY-field-input.json").read_text(encoding="utf-8"))
        self.assertIn("TEST-ONLY", fixture["notice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
