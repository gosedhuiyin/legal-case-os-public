#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from legal_case_os_lib.core import (
    DEFAULT_SCHEMA,
    PROJECT_ROOT,
    WORKSPACE_TEMPLATE,
    LegalCaseError,
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    ensure_not_originals,
    find_object,
    load_json,
    make_empty_state,
    mutate_state,
    now_iso,
    object_version_hash,
    sha256_bytes,
    sha256_file,
    state_content_hash,
    validate_against_schema,
    validate_semantics,
    validate_state_file,
)
from legal_case_os_lib.documents import (
    build_material_index,
    bundle_pdfs,
    extract_docx_text,
    minimal_diff,
    preflight,
    sanitize_docx,
    write_print_sheet,
)
from legal_case_os_lib.templates import (
    DEFAULT_PERSONAL_TEMPLATE_CATALOG,
    DEFAULT_REGISTRY,
    fill_template,
    load_registry,
    resolve_personal_template_reference,
    resolve_template,
    safe_filename,
    validate_personal_template_catalog,
    validate_registry,
)
from legal_case_os_lib.template_curation import (
    apply_structural_docx_plan,
    build_fill_plan,
    build_hybrid_plan,
    build_repeatable_plan,
    distill_template,
    fill_docx_from_plan,
    make_fill_binding,
    register_template,
    validate_fill_plan,
)
from legal_case_os_lib.composition import (
    audit_citations,
    build_composition_spec,
    build_trusted_composition_spec,
    check_exemplar_leaks,
    synchronize_trusted_templates,
    validate_artifact_claim_map,
    validate_composition_spec,
    verify_composition_trust_binding,
)
from legal_case_os_lib.lifecycle import (
    build_context_capsule,
    load_json_object_or_list,
    make_case_event,
    reconcile_report,
    render_current_case_view,
    scan_material_batch,
    stable_id,
)
from legal_case_os_lib.language import (
    _resolve_scope_source,
    compile_language_control,
    prepare_language_input,
    validate_language_control_semantics,
)
from legal_case_os_lib.materials import ingest_material, import_original_parts, read_material
from legal_case_os_lib.learning import build_learning_candidate, approve_learning, load_learning
from legal_case_os_lib.task_output import render_task_draft, render_docx_patch
from legal_case_os_lib.docx_edit import inspect_docx_targets
from legal_case_os_lib.evidence_pages import build_evidence_pages
from legal_case_os_lib.delivery import publish_delivery, restore_delivery, get_current_delivery
from legal_case_os_lib.source_links import resolve_source_links


LANGUAGE_CONTRACT_SCHEMAS = {
    "task_frame": PROJECT_ROOT / "shared" / "schemas" / "task-frame.schema.json",
    "clarification": PROJECT_ROOT / "shared" / "schemas" / "clarification-decision.schema.json",
    "run_spec": PROJECT_ROOT / "shared" / "schemas" / "run-spec.schema.json",
    "preference_candidate": PROJECT_ROOT / "shared" / "schemas" / "preference-candidate.schema.json",
}

CURRENT_STATE_VERSION = "1.2.0"
READABLE_STATE_VERSIONS = {"1.0.0", "1.1.0", CURRENT_STATE_VERSION}


def emit(value: dict[str, Any], as_json: bool = False) -> None:
    # JSON is intentionally the stable interface even without --json.
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _staged_output_path(output: Path) -> Path:
    ensure_not_originals(output)
    if output.exists():
        raise LegalCaseError("OUTPUT_EXISTS", f"输出已存在；拒绝覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.with_name(f".{output.stem}.{uuid.uuid4().hex}.staging{output.suffix}")


def _publish_staged_output(staged: Path, output: Path) -> str:
    """Publish one verified staging file without overwriting another writer."""

    ensure_not_originals(output)
    expected_sha256 = sha256_file(staged)
    created_identity: tuple[int, int] | None = None
    try:
        with staged.open("rb") as incoming, output.open("xb") as outgoing:
            opened = os.fstat(outgoing.fileno())
            created_identity = (opened.st_dev, opened.st_ino)
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        written = output.stat()
        created_identity = (written.st_dev, written.st_ino)
        if sha256_file(output) != expected_sha256:
            raise LegalCaseError(
                "STAGED_OUTPUT_PUBLISH_MISMATCH",
                "暂存文件发布后哈希不一致；结果未被接受。",
                {"output": str(output)},
            )
    except FileExistsError as exc:
        raise LegalCaseError("OUTPUT_EXISTS", f"输出已由另一运行创建；拒绝覆盖：{output}") from exc
    except Exception:
        if created_identity is not None and output.is_file():
            try:
                current = output.stat()
                current_identity = (current.st_dev, current.st_ino)
                if current_identity == created_identity:
                    output.unlink()
            except OSError:
                pass
        raise
    return expected_sha256


def _load_json_argument(
    value: str | None,
    label: str,
    *,
    expected: type | tuple[type, ...] | None = None,
    default: Any = None,
) -> Any:
    """Load one CLI JSON argument from inline JSON or an explicit file path."""
    if value is None:
        return copy.deepcopy(default)
    raw = value.strip()
    try:
        parsed = json.loads(raw) if raw[:1] in {"{", "["} else load_json(Path(raw).resolve())
    except (json.JSONDecodeError, OSError) as exc:
        raise LegalCaseError(
            "INVALID_JSON_ARGUMENT",
            f"{label} 不是可解析的内联 JSON 或 JSON 文件。",
            {"value": value, "detail": str(exc)},
        ) from exc
    if expected is not None and not isinstance(parsed, expected):
        expected_name = (
            "/".join(item.__name__ for item in expected)
            if isinstance(expected, tuple)
            else expected.__name__
        )
        raise LegalCaseError(
            "JSON_ARGUMENT_TYPE_MISMATCH",
            f"{label} 顶层类型必须是 {expected_name}。",
        )
    return parsed


def _state_path(workspace: Path) -> Path:
    return workspace / "_case-state" / "case-state.json"


def _state_version(state: dict[str, Any]) -> int:
    # The projection revision is an optimistic-concurrency token, separate from
    # the audit cursor. Older v1 fixtures may have used the cursor itself; new
    # mutations always increment the stored token so the first write cannot
    # leave version 1 unchanged.
    focus_version = state.get("focus", {}).get("state_version")
    if isinstance(focus_version, int) and focus_version >= 1:
        return max(focus_version, int(state.get("audit", {}).get("last_sequence", 0)), 1)
    return max(int(state.get("audit", {}).get("last_sequence", 0)), 1)


def _require_v11(state: dict[str, Any]) -> None:
    if state.get("schema_version") not in {"1.1.0", CURRENT_STATE_VERSION}:
        raise LegalCaseError(
            "STATE_UPGRADE_REQUIRED",
            "该命令需要 1.1.0 以上案件状态；请先迁移旧工作区。",
            {"schema_version": state.get("schema_version")},
        )


def _require_v12(state: dict[str, Any]) -> None:
    if state.get("schema_version") != CURRENT_STATE_VERSION:
        raise LegalCaseError(
            "STATE_UPGRADE_REQUIRED",
            "模板合成状态写入需要 1.2.0；请先运行 upgrade-state。",
            {"schema_version": state.get("schema_version")},
        )


def _write_new_json(path: Path, value: Any) -> None:
    ensure_not_originals(path)
    if path.exists():
        raise LegalCaseError("OUTPUT_EXISTS", f"输出已经存在，拒绝覆盖：{path}")
    atomic_write_json(path, value)


def _require_expected_state_version(state: dict[str, Any], expected: int | None) -> None:
    actual = _state_version(state)
    if expected is None:
        raise LegalCaseError(
            "STATE_VERSION_REQUIRED",
            "该语义写入必须绑定读取时的案件版本；请提供 --expected-state-version。",
            {"actual_state_version": actual},
        )
    if expected != actual:
        raise LegalCaseError(
            "STALE_STATE_VERSION",
            "案件状态已被另一对话或运行更新，本轮候选不能直接覆盖；请重新构建上下文后再提交。",
            {"expected_state_version": expected, "actual_state_version": actual},
        )


def _workspace_from_state_path(state_path: Path) -> Path:
    resolved = state_path.resolve()
    if resolved.name != "case-state.json" or resolved.parent.name != "_case-state":
        raise LegalCaseError("NON_CANONICAL_STATE_PATH", "状态文件必须位于案件/_case-state/case-state.json。")
    return resolved.parents[1]


def _case_memory_root(state_path: Path) -> Path:
    return state_path.resolve().parent / "memory"


def _ensure_memory_output(output: Path, state_path: Path) -> Path:
    resolved = output.resolve()
    root = _case_memory_root(state_path).resolve()
    ensure_not_originals(resolved)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LegalCaseError(
            "MEMORY_OUTPUT_OUTSIDE_MATTER",
            "案件记忆和上下文派生文件必须写入本案 _case-state/memory 目录。",
            {"output": str(resolved), "memory_root": str(root)},
        ) from exc
    return resolved


def _validate_candidate_state(state: dict[str, Any]) -> None:
    errors = validate_against_schema(state, load_json(DEFAULT_SCHEMA)) + validate_semantics(state)
    if errors:
        raise LegalCaseError(
            "CANDIDATE_STATE_INVALID",
            "候选变更没有通过状态契约，未写入案件。",
            {"errors": errors},
        )


def _relative_or_absolute(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _template_snapshots() -> list[dict[str, Any]]:
    registry = load_registry(DEFAULT_REGISTRY)
    return copy.deepcopy(registry["templates"])


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise LegalCaseError("WORKSPACE_NOT_EMPTY", "目标工作区不是空目录；为避免覆盖，初始化已停止。")
    if not workspace.exists():
        workspace.mkdir(parents=True)
    shutil.copytree(WORKSPACE_TEMPLATE, workspace, dirs_exist_ok=True)
    for relative in (
        "_case-state/memory/indexes",
        "_case-state/memory/cards",
        "_case-state/memory/context-capsules",
        "_case-state/memory/workflows",
        "_case-state/memory/runs",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    state_path = _state_path(workspace)
    old_template_state = load_json(state_path)
    state = make_empty_state(args.matter_id, args.title or "", args.environment)
    state["templates"] = _template_snapshots()
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="init",
        event_type="workspace_initialized",
        object_ids=[args.matter_id],
        details={"environment": args.environment, "template_state_hash": state_content_hash(old_template_state)},
        old_state=old_template_state,
    )
    validation = validate_state_file(state_path)
    if not validation["ok"]:
        raise LegalCaseError("INITIALIZED_STATE_INVALID", "初始化后的状态未通过校验。", {"errors": validation["errors"]})
    return {
        "ok": True,
        "workspace": str(workspace),
        "state": str(state_path),
        "matter_id": args.matter_id,
        "template_count": len(state["templates"]),
        "audit_event_hash": event["event_hash"],
        "external_actions_executed": 0,
    }


def command_upgrade_state(args: argparse.Namespace) -> dict[str, Any]:
    """Upgrade a canonical readable workspace to the v1.2 write contract.

    The migration is additive: v1.0 gains the v1.1 lifecycle/memory fields,
    then v1.1/v1.0 gain the v1.2 composition fields.  Originals and existing
    case objects are never rewritten or removed.
    """
    state_path = Path(args.state).resolve()
    workspace = _workspace_from_state_path(state_path)
    state = load_json(state_path)
    _require_expected_state_version(state, args.expected_state_version)
    source_version = state.get("schema_version")
    if source_version == CURRENT_STATE_VERSION:
        return {
            "ok": True,
            "idempotent": True,
            "schema_version": CURRENT_STATE_VERSION,
            "state_version": _state_version(state),
            "external_actions_executed": 0,
        }
    if source_version not in {"1.0.0", "1.1.0"}:
        raise LegalCaseError(
            "UNSUPPORTED_STATE_VERSION",
            "只支持把 1.0.0 或 1.1.0 案件状态迁移到 1.2.0。",
            {"schema_version": source_version},
        )

    old_state = copy.deepcopy(state)
    defaults = make_empty_state(
        state["matter"]["id"],
        state["matter"].get("title", ""),
        state["matter"].get("environment", "production"),
    )
    # v1.1 lifecycle and matter-local memory additions.
    for collection in (
        "material_batches",
        "case_events",
        "triage_cards",
        "deadline_records",
        "impact_assessments",
        "prospective_matter_seeds",
        "workflow_instances",
        "run_attempts",
    ):
        state.setdefault(collection, [])
    state.setdefault("tasks", [])

    focus = state.setdefault("focus", {})
    for key in (
        "current_batch_id",
        "current_event_id",
        "current_workflow_instance_id",
        "current_task_id",
        "current_memory_mode",
        "read_memory_scope",
        "state_version",
        "snapshot",
    ):
        focus.setdefault(key, copy.deepcopy(defaults["focus"][key]))
    focus["state_version"] = _state_version(state)

    intent = state.get("intent")
    if isinstance(intent, dict):
        intent.setdefault("memory_mode", "relevant")
        intent.setdefault("read_memory_scope", [])
        intent.setdefault("write_memory_mode", "candidate")
        legacy_alias = intent.get("template_alias")
        intent.setdefault("template_refs", [legacy_alias] if legacy_alias else [])
    state["memory_state"] = {
        **copy.deepcopy(defaults["memory_state"]),
        **copy.deepcopy(state.get("memory_state", {})),
    }
    # v1.2 composition additions.  Profiles remain library-side artifacts;
    # only case-bound composition contracts belong in case-state.json.
    state.setdefault("composition_specs", [])
    focus.setdefault("current_composition_spec_id", None)
    state["schema_version"] = CURRENT_STATE_VERSION
    for relative in (
        "_case-state/memory/indexes",
        "_case-state/memory/cards",
        "_case-state/memory/context-capsules",
        "_case-state/memory/workflows",
        "_case-state/memory/runs",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)

    _validate_candidate_state(state)
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="upgrade-state",
        event_type="state_schema_upgraded",
        object_ids=[state["matter"]["id"]],
        details={"from": source_version, "to": CURRENT_STATE_VERSION, "originals_modified": False},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "idempotent": False,
        "schema_version": saved["schema_version"],
        "state_version": _state_version(saved),
        "audit_event_hash": event["event_hash"],
        "external_actions_executed": 0,
    }


def _mark_stale(state: dict[str, Any], seed_ids: set[str], reason: str) -> set[str]:
    affected = set(seed_ids)
    changed = True
    while changed:
        changed = False
        for fact in state.get("facts", []):
            if fact.get("id") in affected:
                continue
            if any(locator.get("source_id") in affected for locator in fact.get("source_locators", [])):
                fact["stale"] = True
                fact["stale_reason"] = reason
                affected.add(fact["id"])
                changed = True
        for evidence in state.get("evidence", []):
            if evidence.get("id") in affected:
                continue
            locators = [
                *evidence.get("source_refs", []),
                *evidence.get("source_locators", []),
            ]
            if any(locator.get("source_id") in affected for locator in locators):
                evidence["status"] = "stale"
                evidence["current_submission"] = False
                affected.add(evidence["id"])
                changed = True
        for authority in state.get("authorities", []):
            if authority.get("id") in affected:
                continue
            if authority.get("source_id") in affected:
                authority["stale"] = True
                authority["production_eligible"] = False
                authority["verification_status"] = "unverified"
                affected.add(authority["id"])
                changed = True
        for issue in state.get("issues", []):
            if issue.get("id") in affected:
                continue
            dependencies = (
                set(issue.get("source_ids", []))
                | set(issue.get("fact_ids", []))
                | set(issue.get("evidence_ids", []))
                | set(issue.get("authority_ids", []))
            )
            if dependencies & affected:
                issue["status"] = "stale"
                affected.add(issue["id"])
                changed = True
        for theory in state.get("theories", []):
            if theory.get("id") in affected:
                continue
            dependencies = (
                set(theory.get("source_ids", []))
                | set(theory.get("fact_ids", []))
                | set(theory.get("issue_ids", []))
                | set(theory.get("evidence_ids", []))
                | set(theory.get("authority_ids", []))
                | set(theory.get("input_ids", []))
            )
            if dependencies & affected:
                theory["status"] = "stale"
                affected.add(theory["id"])
                changed = True
        for decision in state.get("decisions", []):
            if decision.get("id") in affected:
                continue
            dependencies = set(decision.get("input_ids", [])) | {decision.get("subject_id")}
            if dependencies & affected:
                decision["status"] = "stale"
                affected.add(decision["id"])
                changed = True
        for spec in state.get("composition_specs", []):
            if spec.get("id") in affected:
                continue
            roles = spec.get("template_roles", {})
            template_ids = {
                roles.get("layout_template_id"),
                roles.get("structure_template_id"),
                *(item.get("template_id") for item in roles.get("auxiliary_templates", [])),
            }
            trust = spec.get("trusted_binding", {})
            approval_ids = {
                item.get("approval_id")
                for item in trust.get("approval_bindings", [])
                if isinstance(item, dict)
            }
            approval_ids.update(
                snapshot.get("approval_id")
                for snapshot in spec.get("gate_snapshots", {}).values()
                if isinstance(snapshot, dict)
            )
            dependencies = set(spec.get("authority_ids", [])) | {
                item.get("object_id") for item in spec.get("dependencies", [])
            } | template_ids | approval_ids
            if dependencies & affected:
                spec["stale"] = True
                spec["stale_reason"] = reason
                spec["status"] = "stale"
                blocker = f"上游输入已变化：{reason}"
                if blocker not in spec.setdefault("blockers", []):
                    spec["blockers"].append(blocker)
                affected.add(spec["id"])
                changed = True
        for artifact in state.get("artifacts", []):
            if artifact.get("id") in affected:
                continue
            if (
                artifact.get("composition_spec_id") in affected
                or any(snapshot.get("object_id") in affected for snapshot in artifact.get("input_snapshot", []))
            ):
                artifact["stale"] = True
                artifact["stale_reason"] = reason
                artifact["status"] = "stale"
                affected.add(artifact["id"])
                changed = True
        for package in state.get("package_manifests", []):
            if package.get("id") in affected:
                continue
            if any(item.get("object_id") in affected for item in package.get("items", [])):
                package["stale"] = True
                package["status"] = "stale"
                blocker = f"输入已失效：{reason}"
                if blocker not in package["blockers"]:
                    package["blockers"].append(blocker)
                affected.add(package["id"])
                changed = True
        # Approval invalidation is part of the same transitive closure.  In
        # particular, a changed source/evidence row can invalidate G2 only
        # after its own propagation pass; the newly stale approval must then
        # invalidate every CompositionSpec that froze that approval or gate
        # snapshot, followed by its artifacts and packages.
        evidence_changed = any(item.get("id") in affected for item in state.get("evidence", []))
        packages_changed = any(item.get("id") in affected for item in state.get("package_manifests", []))
        artifacts_changed = any(item.get("id") in affected for item in state.get("artifacts", []))
        for approval in state.get("approvals", []):
            if approval.get("id") in affected:
                continue
            should_stale = approval.get("object_id") in affected
            should_stale = should_stale or any(
                snapshot.get("object_id") in affected for snapshot in approval.get("scope_snapshot", [])
            )
            should_stale = should_stale or (evidence_changed and approval.get("gate") == "G2_evidence")
            should_stale = should_stale or (
                (artifacts_changed or packages_changed)
                and approval.get("gate") in {"G4_final", "G5_external_action"}
            )
            if should_stale:
                if approval.get("status") != "stale":
                    approval["status"] = "stale"
                    approval["invalidation_reason"] = reason
                if approval.get("id") not in affected:
                    affected.add(approval["id"])
                    changed = True
        # G2 approves one exact collection, not independent live flags on each
        # row. Once that collection is stale, no remaining member may continue
        # to look submission-ready. Keep the old lawyer decision and approval
        # snapshot as history while retiring all current members together.
        stale_g2 = any(
            item.get("gate") == "G2_evidence" and item.get("id") in affected
            and item.get("status") == "stale"
            for item in state.get("approvals", [])
        )
        if stale_g2:
            for evidence in state.get("evidence", []):
                if evidence.get("current_submission"):
                    evidence["status"] = "stale"
                    evidence["current_submission"] = False
                    if evidence["id"] not in affected:
                        affected.add(evidence["id"])
                        changed = True
        for evidence in state.get("evidence", []):
            if evidence.get("status") == "stale":
                evidence["current_submission"] = False
                evidence["reserve"] = False
                evidence["internal_reference"] = False
    for coverage in state.get("processing_coverages", []):
        if coverage.get("source_id") in affected:
            coverage["status"] = "partial"
            item = f"源版本已变化，需重新处理：{reason}"
            if item not in coverage["manual_review_items"]:
                coverage["manual_review_items"].append(item)
    for candidate in state.get("focus", {}).get("recent_candidates", []):
        if candidate.get("object_id") in affected or candidate.get("pending_approval_id") in affected:
            candidate["stale"] = True
            candidate["just_displayed"] = False
    state["focus"]["pending_approval_ids"] = [
        identifier for identifier in state["focus"].get("pending_approval_ids", []) if identifier not in affected
    ]
    if len(affected) > len(seed_ids):
        state["status"]["workflow"] = "waiting_approval"
        blocker = f"上游变化触发重新核验：{reason}"
        if blocker not in state["status"]["blockers"]:
            state["status"]["blockers"].append(blocker)
    return affected


def _mark_direct_impact_target_stale(collection: str | None, item: dict[str, Any] | None, reason: str) -> None:
    if item is None:
        return
    if collection == "facts":
        item["stale"] = True
        item["stale_reason"] = reason
    elif collection in {"issues", "theories", "evidence", "decisions", "artifacts", "composition_specs", "package_manifests"}:
        item["status"] = "stale"
        if collection == "evidence":
            item["current_submission"] = False
        if collection == "artifacts":
            item["stale"] = True
            item["stale_reason"] = reason
        if collection == "composition_specs":
            item["stale"] = True
            item["stale_reason"] = reason
            blocker = f"上游输入已变化：{reason}"
            if blocker not in item.setdefault("blockers", []):
                item["blockers"].append(blocker)
        if collection == "package_manifests":
            item["stale"] = True
            blocker = f"输入已失效：{reason}"
            if blocker not in item.get("blockers", []):
                item.setdefault("blockers", []).append(blocker)
    elif collection == "approvals":
        item["status"] = "stale"
        item["invalidation_reason"] = reason


def _is_stale_object(item: dict[str, Any] | None) -> bool:
    return bool(item) and (item.get("stale") is True or item.get("status") == "stale")


def command_index(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).resolve()
    state_path = _state_path(workspace)
    state = load_json(state_path)
    old_state = copy.deepcopy(state)
    source_dir = Path(args.source_dir).resolve() if args.source_dir else workspace / "00-originals"
    ocr_report = load_json(Path(args.ocr_report).resolve()) if args.ocr_report else None
    sources, derived = build_material_index(source_dir, state.get("sources", []), ocr_report)
    old_by_id = {item["id"]: item for item in state.get("sources", [])}
    changed_ids = {
        item["id"]
        for item in sources
        if item["id"] in old_by_id and item["sha256"] != old_by_id[item["id"]].get("sha256")
    }
    removed_ids = set(old_by_id) - {item["id"] for item in sources}
    state["sources"] = sources
    affected: set[str] = set()
    if changed_ids or removed_ids:
        affected = _mark_stale(state, changed_ids | removed_ids, "原始来源哈希变化或文件不再位于已索引范围")
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="index",
        event_type="materials_indexed",
        object_ids=[item["id"] for item in sources],
        details={"changed_source_ids": sorted(changed_ids), "removed_source_ids": sorted(removed_ids), "affected_ids": sorted(affected)},
        old_state=old_state,
    )
    saved_state = load_json(state_path)
    derived["derived_from_state_hash"] = state_content_hash(saved_state)
    derived["audit_event_hash"] = event["event_hash"]
    index_path = workspace / "10-index" / "material-index.json"
    atomic_write_json(index_path, derived)
    return {
        "ok": True,
        "file_count": len(sources),
        "duplicate_count": derived["duplicate_count"],
        "changed_source_ids": sorted(changed_ids),
        "removed_source_ids": sorted(removed_ids),
        "stale_ids": sorted(affected - changed_ids - removed_ids),
        "state_hash": state_content_hash(saved_state),
        "derived_index": str(index_path),
        "audit_event_hash": event["event_hash"],
    }


def command_ingest_batch(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).resolve()
    state_path = _state_path(workspace)
    state = load_json(state_path)
    _require_v11(state)
    old_state = copy.deepcopy(state)
    source_dir = Path(args.source_dir).resolve()
    ocr_report = load_json(Path(args.ocr_report).resolve()) if args.ocr_report else None
    staged = scan_material_batch(workspace, source_dir, state.get("sources", []), ocr_report)

    explicit_batch = args.batch_id
    if explicit_batch:
        same_id = next((item for item in state.get("material_batches", []) if item.get("id") == explicit_batch), None)
        if same_id and same_id.get("arrival_key") != staged["arrival_key"]:
            raise LegalCaseError(
                "BATCH_ID_CONFLICT",
                "同一 batch-id 已绑定不同材料摘要，拒绝覆盖。",
                {"batch_id": explicit_batch},
            )
    existing = next(
        (item for item in state.get("material_batches", []) if item.get("arrival_key") == staged["arrival_key"]),
        None,
    )
    if existing:
        return {
            "ok": True,
            "idempotent": True,
            "batch_id": existing["id"],
            "arrival_key": staged["arrival_key"],
            "state_version": _state_version(state),
            "source_ids": existing.get("source_ids", []),
            "external_actions_executed": 0,
        }

    received_at = args.received_at or now_iso()
    batch_id = explicit_batch or stable_id("MB", state["matter"]["id"], staged["arrival_key"])
    semantic_event = make_case_event(
        state,
        event_type="material_received",
        title=f"收到增量材料批次 {batch_id}",
        source_ids=staged["source_ids"],
        related_object_ids=[batch_id],
        received_at=received_at,
        authority=args.authority,
        confidence="direct_record",
        channel=args.arrival_channel,
        sender=args.sender,
    )
    cursor_before = int(state.get("memory_state", {}).get("last_committed_event_seq", 0))
    batch = {
        "id": batch_id,
        "version": 1,
        "source_ids": staged["source_ids"],
        "arrival_key": staged["arrival_key"],
        "received_at": received_at,
        "status": "pending_triage",
        "cursor_before": cursor_before,
        "cursor_after": semantic_event["sequence"],
        "created_at": now_iso(),
    }
    if args.arrival_channel is not None:
        batch["arrival_channel"] = args.arrival_channel
    if args.sender is not None:
        batch["sender"] = args.sender
    state["sources"] = staged["sources"]
    state.setdefault("material_batches", []).append(batch)
    _advance_semantic_cursor(state, semantic_event)
    memory_state = state.setdefault("memory_state", {})
    memory_state["last_batch_id"] = batch_id
    affected: set[str] = set()
    if staged["modified_source_ids"]:
        affected = _mark_stale(state, set(staged["modified_source_ids"]), "增量批次中的原始来源哈希发生变化")
    _validate_candidate_state(state)
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="ingest-batch",
        event_type="material_batch_ingested",
        object_ids=[batch_id, semantic_event["id"], *staged["source_ids"]],
        details={
            "arrival_key": staged["arrival_key"],
            "added_source_ids": staged["added_source_ids"],
            "modified_source_ids": staged["modified_source_ids"],
            "unchanged_source_ids": staged["unchanged_source_ids"],
            "affected_ids": sorted(affected),
        },
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "idempotent": False,
        "batch_id": batch_id,
        "case_event_id": semantic_event["id"],
        "arrival_key": staged["arrival_key"],
        "source_ids": staged["source_ids"],
        "added_source_ids": staged["added_source_ids"],
        "modified_source_ids": staged["modified_source_ids"],
        "unchanged_source_ids": staged["unchanged_source_ids"],
        "stale_ids": sorted(affected - set(staged["modified_source_ids"])),
        "state_version": _state_version(saved),
        "cursor_after": semantic_event["sequence"],
        "audit_event_hash": event["event_hash"],
        "external_actions_executed": 0,
    }


def command_context_build(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    capsule = build_context_capsule(
        state,
        mode=args.mode,
        scopes=args.scope,
        query=args.query or "",
        max_items=args.max_items,
    )
    if args.output:
        output = _ensure_memory_output(Path(args.output), state_path)
        atomic_write_json(output, capsule)
        capsule["output"] = str(output)
    return {"ok": capsule.get("coverage") != "selection_failed", "capsule": capsule, "external_actions_executed": 0}


def command_memory_view(args: argparse.Namespace) -> dict[str, Any]:
    from legal_case_os_lib.core import _state_file_lock

    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    output = _ensure_memory_output(Path(args.output), state_path)
    if ".view-versions" in output.parts:
        raise LegalCaseError("MEMORY_OUTPUT_IS_VERSION", "不可把不可变视图版本当作可替换输出；请指定普通记忆视图路径。")
    view = render_current_case_view(state)
    # Each attempt owns a new file. A CAS loser must never touch the current
    # alias or bytes referenced by the winner's canonical artifact record.
    version_output = _ensure_memory_output(
        output.parent / ".view-versions" / f"{output.stem}-{uuid.uuid4().hex}{output.suffix}", state_path
    )
    atomic_write_text(version_output, view)
    digest = sha256_file(version_output)
    before_hash = state_content_hash(state)
    matter_id = state["matter"]["id"]
    artifact_id = stable_id("R", matter_id, "current-case-view")
    existing = next((item for item in state.get("artifacts", []) if item.get("id") == artifact_id), None)
    version = int(existing.get("version", 0)) + 1 if existing else 1
    matter_version, matter_hash = object_version_hash(state["matter"])
    artifact = {
        "id": artifact_id,
        "kind": "current_case_view",
        "audience": "internal_review",
        "version": version,
        "path": _relative_or_absolute(version_output, _workspace_from_state_path(state_path)),
        "sha256": digest,
        "status": "draft",
        "review_status": "not_reviewed",
        "input_snapshot": [{"object_id": matter_id, "version": matter_version, "hash": matter_hash}],
        "stale": False,
        "stale_reason": None,
        "created_at": now_iso(),
    }
    if existing:
        state["artifacts"] = [artifact if item.get("id") == artifact_id else item for item in state.get("artifacts", [])]
    else:
        state.setdefault("artifacts", []).append(artifact)
    as_of = max((int(item.get("sequence", 0)) for item in state.get("case_events", [])), default=0)
    memory_state = state.setdefault("memory_state", {})
    memory_state.update(
        {
            "current_view_artifact_id": artifact_id,
            "current_view_version": version,
            "current_view_as_of_event_seq": as_of,
            "current_view_input_hash": before_hash,
            "current_view_updated_at": now_iso(),
        }
    )
    _validate_candidate_state(state)
    try:
        event = mutate_state(
            state_path,
            state,
            actor=args.actor,
            command="memory-view",
            event_type="current_case_view_rebuilt",
            object_ids=[artifact_id],
            details={"as_of_event_seq": as_of, "input_state_hash": before_hash, "path": artifact["path"]},
            old_state=old_state,
        )
    except LegalCaseError as exc:
        if exc.code == "STALE_STATE_VERSION":
            version_output.unlink(missing_ok=True)
        raise
    alias_updated = False
    # The replaceable convenience alias is never the canonical hashed file.
    # A newer commit may win before publication; only the current version may
    # update the alias, under the same lock used by every canonical writer.
    with _state_file_lock(state_path):
        current = load_json(state_path)
        current_artifact = next((item for item in current.get("artifacts", []) if item.get("id") == artifact_id), None)
        if current_artifact and not _is_stale_object(current_artifact) and all(
            current_artifact.get(key) == artifact.get(key) for key in ("version", "path", "sha256")
        ):
            atomic_write_text(output, view)
            alias_updated = True
    saved = load_json(state_path)
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "version": version,
        "path": str(version_output),
        "requested_output": str(output),
        "alias_updated": alias_updated,
        "sha256": digest,
        "as_of_event_seq": as_of,
        "state_version": _state_version(saved),
        "audit_event_hash": event["event_hash"],
        "external_actions_executed": 0,
    }


def _payload_list(payload: Any, key: str, singular_key: str | None = None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload if key == "triage_cards" else []
    elif isinstance(payload, dict):
        values = payload.get(key, [])
        if not values and singular_key and isinstance(payload.get(singular_key), dict):
            values = [payload[singular_key]]
        if key == "triage_cards" and not values and "batch_id" in payload and "source_id" in payload:
            values = [payload]
    else:
        raise LegalCaseError("INVALID_CANDIDATE_PACKET", "候选包必须是 JSON 对象或数组。")
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise LegalCaseError("INVALID_CANDIDATE_PACKET", f"{key} 必须是对象数组。")
    return copy.deepcopy(values)


def _replace_or_append_versioned(
    state: dict[str, Any],
    collection: str,
    item: dict[str, Any],
    *,
    allow_update: bool = False,
) -> tuple[dict[str, Any], bool]:
    identifier = item.get("id")
    if not identifier:
        raise LegalCaseError("OBJECT_ID_REQUIRED", f"{collection} 候选缺少 id。")
    values = state.setdefault(collection, [])
    existing_index = next((index for index, current in enumerate(values) if current.get("id") == identifier), None)
    if existing_index is None:
        values.append(item)
        return item, True
    existing = values[existing_index]
    if canonical_json(existing) == canonical_json(item):
        return existing, False
    if not allow_update:
        raise LegalCaseError("OBJECT_ID_CONFLICT", f"对象 {identifier} 已存在且内容不同，拒绝静默覆盖。")
    updated = copy.deepcopy(item)
    updated["version"] = int(existing.get("version", 1)) + 1
    values[existing_index] = updated
    return updated, True


def _advance_semantic_cursor(state: dict[str, Any], event: dict[str, Any]) -> None:
    state.setdefault("case_events", []).append(event)
    memory_state = state.setdefault("memory_state", {})
    memory_state["last_committed_event_seq"] = event["sequence"]
    # The short current-case view is a disposable projection. Any later event
    # invalidates it; the underlying event ledger remains authoritative.
    current_view_id = memory_state.get("current_view_artifact_id")
    if current_view_id and int(event.get("sequence", 0)) > int(memory_state.get("current_view_as_of_event_seq", 0)):
        current_view = next(
            (item for item in state.get("artifacts", []) if item.get("id") == current_view_id),
            None,
        )
        if current_view:
            current_view["stale"] = True
            current_view["stale_reason"] = f"案件事件已推进至 {event['sequence']}，需重建当前案件视图"
            current_view["status"] = "stale"


def command_triage_commit(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    payload = load_json_object_or_list(Path(args.card).resolve())
    cards = _payload_list(payload, "triage_cards", "triage_card")
    if not cards:
        raise LegalCaseError("TRIAGE_CARD_REQUIRED", "候选包中没有 triage_card。")
    batch_by_id = {item.get("id"): item for item in state.get("material_batches", [])}
    source_ids = {item.get("id") for item in state.get("sources", [])}
    touched_ids: list[str] = []
    now = now_iso()

    for card in cards:
        batch_id = card.get("batch_id")
        source_id = card.get("source_id")
        if batch_id not in batch_by_id:
            raise LegalCaseError("BATCH_NOT_FOUND", f"分诊卡引用不存在的批次：{batch_id}")
        if source_id not in source_ids or source_id not in batch_by_id[batch_id].get("source_ids", []):
            raise LegalCaseError("SOURCE_NOT_IN_BATCH", f"来源 {source_id} 不属于批次 {batch_id}。")
        card.setdefault("id", stable_id("TC", str(batch_id), str(source_id)))
        card.setdefault("version", 1)
        card.setdefault("relevance", "unknown")
        card.setdefault("action_route", "human_review")
        card.setdefault("direct_link_ids", [])
        card.setdefault("created_at", now)
        if args.write_mode == "commit":
            card["status"] = "committed"
            card["committed_by"] = args.actor
            card["committed_at"] = now
        else:
            card["status"] = "candidate"
            card.pop("committed_by", None)
            card.pop("committed_at", None)
        saved_card, changed = _replace_or_append_versioned(
            state,
            "triage_cards",
            card,
            allow_update=args.write_mode == "commit",
        )
        touched_ids.append(saved_card["id"])
        if args.write_mode == "candidate" and saved_card["id"] not in state["memory_state"]["pending_candidate_ids"]:
            state["memory_state"]["pending_candidate_ids"].append(saved_card["id"])
        if args.write_mode == "commit":
            state["memory_state"]["pending_candidate_ids"] = [
                item for item in state["memory_state"]["pending_candidate_ids"] if item != saved_card["id"]
            ]

    # Candidate event descriptions are converted into canonical CaseEvents here;
    # callers cannot inject their own sequence numbers or overwrite prior events.
    placeholder_event_ids: dict[str, str] = {}
    for candidate in _payload_list(payload, "event_candidates"):
        candidate_confidence = str(candidate.get("confidence") or "direct_record")
        event = make_case_event(
            state,
            event_type=str(candidate.get("event_type") or "other"),
            title=str(candidate.get("title") or "增量材料事件"),
            source_ids=candidate.get("source_ids", []),
            related_object_ids=candidate.get("related_object_ids", []),
            occurred_at=candidate.get("occurred_at"),
            received_at=candidate.get("received_at"),
            authority=candidate.get("authority"),
            confidence=candidate_confidence,
            channel=candidate.get("channel"),
            sender=candidate.get("sender"),
            status="candidate" if candidate_confidence in {"candidate", "model_inference"} else "active",
        )
        if candidate.get("id"):
            placeholder_event_ids[str(candidate["id"])] = event["id"]
        _advance_semantic_cursor(state, event)
        touched_ids.append(event["id"])
        if event.get("status") == "candidate" and event["id"] not in state["memory_state"]["pending_candidate_ids"]:
            state["memory_state"]["pending_candidate_ids"].append(event["id"])

    extra_specs = (
        ("deadline_records", "deadline_records"),
        ("impact_assessments", "impact_assessments"),
        ("prospective_matter_seeds", "prospective_matter_seeds"),
    )
    for payload_key, collection in extra_specs:
        for item in _payload_list(payload, payload_key):
            if item.get("source_event_id") in placeholder_event_ids:
                item["source_event_id"] = placeholder_event_ids[item["source_event_id"]]
            item.setdefault("version", 1)
            item.setdefault("created_at", now)
            if collection == "deadline_records":
                item.setdefault("status", "candidate")
                item.setdefault("due_at", None)
                item.setdefault("related_workflow_instance_id", None)
                item.setdefault("verified_by", None)
                item.setdefault("verified_at", None)
                item.setdefault("dismissal_reason", None)
                item.setdefault("completed_at", None)
            elif collection == "impact_assessments":
                item["status"] = "candidate"
                item.setdefault("lawyer_review_required", True)
                item["lawyer_decision"] = "pending"
                item["applied_at"] = None
                item.setdefault("stale_object_ids", [])
            else:
                item.setdefault("origin_matter_id", state["matter"]["id"])
                item.setdefault("status", "dormant")
                item["human_decision"] = "pending"
                item["decided_by"] = None
                item["decided_at"] = None
                item["promoted_matter_id"] = None
                item["promotion_event_id"] = None
                item.setdefault("updated_at", now)
            saved, _changed = _replace_or_append_versioned(state, collection, item)
            touched_ids.append(saved["id"])
            if saved["id"] not in state["memory_state"]["pending_candidate_ids"]:
                state["memory_state"]["pending_candidate_ids"].append(saved["id"])

    if args.write_mode == "candidate":
        # A candidate is an ephemeral, reviewable delta. It must not alter the
        # canonical projection, append audit history, or advance a real cursor.
        # We still validate the hypothetical post-state so broken references are
        # caught before the delta is shown to the lawyer.
        _validate_candidate_state(state)
        delta: dict[str, list[dict[str, Any]]] = {}
        for collection in (
            "triage_cards",
            "case_events",
            "deadline_records",
            "impact_assessments",
            "prospective_matter_seeds",
        ):
            old_by_id = {item.get("id"): item for item in old_state.get(collection, [])}
            proposed = [
                copy.deepcopy(item)
                for item in state.get(collection, [])
                if item.get("id") in touched_ids
                and canonical_json(item) != canonical_json(old_by_id.get(item.get("id"), {}))
            ]
            if proposed:
                delta[collection] = proposed
        return {
            "ok": True,
            "write_mode": "candidate",
            "candidate_only": True,
            "canonical_state_mutations": 0,
            "object_ids": touched_ids,
            "base_state_version": _state_version(old_state),
            "state_version": _state_version(old_state),
            "base_state_hash": state_content_hash(old_state),
            "proposed_event_cursor_after": state.get("memory_state", {}).get("last_committed_event_seq", 0),
            "candidate_delta": delta,
            "audit_event_hash": None,
            "external_actions_executed": 0,
        }

    for batch in state.get("material_batches", []):
        if batch.get("id") not in {card.get("batch_id") for card in cards}:
            continue
        decided_sources = {
            card.get("source_id")
            for card in state.get("triage_cards", [])
            if card.get("batch_id") == batch.get("id") and card.get("status") in {"committed", "rejected"}
        }
        if set(batch.get("source_ids", [])) <= decided_sources:
            batch["status"] = "triaged"
            batch["triaged_at"] = now
            batch["version"] = int(batch.get("version", 1)) + 1

    semantic_event = make_case_event(
        state,
        event_type="other",
        title="确认材料分诊",
        source_ids=[card.get("source_id") for card in cards],
        related_object_ids=touched_ids,
        authority="system",
        confidence="direct_record",
    )
    _advance_semantic_cursor(state, semantic_event)
    touched_ids.append(semantic_event["id"])
    if args.write_mode == "commit":
        state["memory_state"]["last_commit_actor"] = args.actor
        state["memory_state"]["last_commit_at"] = now
        state["memory_state"]["last_commit_approval_id"] = args.approval_id
    _validate_candidate_state(state)
    audit_event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="triage-commit",
        event_type="memory_commit_recorded",
        object_ids=touched_ids,
        details={"write_mode": args.write_mode, "base_state_version": args.expected_state_version},
        old_state=old_state,
    )
    saved_state = load_json(state_path)
    return {
        "ok": True,
        "write_mode": args.write_mode,
        "object_ids": touched_ids,
        "state_version": _state_version(saved_state),
        "as_of_event_seq": semantic_event["sequence"],
        "audit_event_hash": audit_event["event_hash"],
        "external_actions_executed": 0,
    }


def command_deadline_verify(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    deadline = next((item for item in state.get("deadline_records", []) if item.get("id") == args.deadline_id), None)
    if deadline is None:
        raise LegalCaseError("DEADLINE_NOT_FOUND", f"期限记录不存在：{args.deadline_id}")
    if deadline.get("status") not in {"candidate", "verified"}:
        raise LegalCaseError("DEADLINE_NOT_REVIEWABLE", "只有候选或已核验期限可以重新核验。")
    now = now_iso()
    deadline["version"] = int(deadline.get("version", 1)) + 1
    deadline["verified_by"] = args.actor
    deadline["verified_at"] = now
    if args.decision == "verified":
        due_at = args.due_at or deadline.get("due_at")
        if not due_at:
            raise LegalCaseError("DEADLINE_DUE_AT_REQUIRED", "核验为有效期限时必须提供 due-at。")
        deadline["due_at"] = due_at
        deadline["status"] = "verified"
        deadline["dismissal_reason"] = None
    else:
        if not args.reason:
            raise LegalCaseError("DISMISSAL_REASON_REQUIRED", "驳回期限候选必须说明理由。")
        deadline["status"] = "dismissed"
        deadline["dismissal_reason"] = args.reason
    if args.calculation_basis is not None:
        deadline["calculation_basis"] = args.calculation_basis
    if args.verification_note is not None:
        deadline["verification_note"] = args.verification_note
    semantic_event = make_case_event(
        state,
        event_type="deadline_change",
        title=f"期限记录{args.decision}：{deadline.get('title')}",
        related_object_ids=[deadline["id"]],
        authority="lawyer",
        confidence="lawyer_confirmed",
    )
    _advance_semantic_cursor(state, semantic_event)
    state["memory_state"]["pending_candidate_ids"] = [
        item for item in state["memory_state"]["pending_candidate_ids"] if item != deadline["id"]
    ]
    state["memory_state"].update(
        {"last_commit_actor": args.actor, "last_commit_at": now, "last_commit_approval_id": args.approval_id}
    )
    _validate_candidate_state(state)
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="deadline-verify",
        event_type="deadline_verified" if args.decision == "verified" else "deadline_dismissed",
        object_ids=[deadline["id"], semantic_event["id"]],
        details={"decision": args.decision, "due_at": deadline.get("due_at"), "reason": args.reason},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "deadline_id": deadline["id"],
        "status": deadline["status"],
        "state_version": _state_version(saved),
        "audit_event_hash": event["event_hash"],
        "external_actions_executed": 0,
    }


def command_impact_apply(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    assessment_path = Path(args.assessment)
    payload = (
        load_json_object_or_list(assessment_path.resolve())
        if assessment_path.exists()
        else {"id": args.assessment}
    )
    if isinstance(payload, dict) and isinstance(payload.get("impact_assessment"), dict):
        payload = payload["impact_assessment"]
    if not isinstance(payload, dict) or not payload.get("id"):
        raise LegalCaseError("IMPACT_ASSESSMENT_REQUIRED", "assessment 必须是含 id 的 JSON 对象。")
    existing = next((item for item in state.get("impact_assessments", []) if item.get("id") == payload["id"]), None)
    if existing and set(payload) <= {"id"}:
        assessment = copy.deepcopy(existing)
    else:
        assessment = copy.deepcopy(payload)
        assessment.setdefault("version", 1)
        assessment.setdefault("stale_object_ids", [])
        assessment.setdefault("counterevidence_object_ids", [])
        assessment.setdefault("lawyer_review_required", True)
        assessment.setdefault("lawyer_decision", "pending")
        assessment.setdefault("status", "candidate")
        assessment.setdefault("applied_at", None)
        assessment.setdefault("created_at", now_iso())

    known_ids = {state["matter"]["id"]}
    for collection in (
        "sources", "facts", "issues", "theories", "evidence", "authorities", "decisions", "approvals",
        "artifacts", "material_batches", "case_events", "triage_cards", "deadline_records", "workflow_instances", "tasks",
    ):
        known_ids.update(item.get("id") for item in state.get(collection, []) if item.get("id"))
    missing = [item for item in assessment.get("affected_object_ids", []) if item not in known_ids]
    if missing:
        raise LegalCaseError("IMPACT_TARGET_NOT_FOUND", "影响卡引用了不存在的对象。", {"missing_ids": missing})
    if args.decision == "not_required" and assessment.get("lawyer_review_required"):
        raise LegalCaseError("LAWYER_REVIEW_REQUIRED", "该影响卡要求律师审阅，不能标为 not_required。")

    now = now_iso()
    affected: set[str] = set()
    if args.decision == "rejected":
        assessment["status"] = "rejected"
        assessment["lawyer_decision"] = "rejected"
        assessment["applied_at"] = None
    else:
        assessment["lawyer_decision"] = args.decision
        impact_reason = f"已批准影响卡 {assessment['id']} 识别到新增材料影响"
        for object_id in assessment.get("affected_object_ids", []):
            collection, target = find_object(state, object_id)
            _mark_direct_impact_target_stale(collection, target, impact_reason)
        affected = _mark_stale(
            state,
            set(assessment.get("source_ids", [])) | set(assessment.get("affected_object_ids", [])),
            impact_reason,
        )
        current_view_id = state.get("memory_state", {}).get("current_view_artifact_id")
        if current_view_id:
            current_view = next((item for item in state.get("artifacts", []) if item.get("id") == current_view_id), None)
            if current_view and not current_view.get("stale"):
                current_view["stale"] = True
                current_view["stale_reason"] = f"影响卡 {assessment['id']} 已应用"
                current_view["status"] = "stale"
                affected.add(current_view_id)
        assessment["stale_object_ids"] = sorted(
            object_id for object_id in affected if _is_stale_object(find_object(state, object_id)[1])
        )
        assessment["status"] = "applied"
        assessment["applied_at"] = now
    stored, _changed = _replace_or_append_versioned(state, "impact_assessments", assessment, allow_update=True)
    semantic_event = make_case_event(
        state,
        event_type="fact_change" if args.decision != "rejected" else "strategy_change",
        title=f"影响卡{args.decision}：{stored.get('summary')}",
        source_ids=stored.get("source_ids", []),
        related_object_ids=[stored["id"], *stored.get("affected_object_ids", [])],
        authority="lawyer" if args.decision == "approved" else "system",
        confidence="lawyer_confirmed" if args.decision == "approved" else "rule_confirmed",
    )
    _advance_semantic_cursor(state, semantic_event)
    state["memory_state"]["pending_candidate_ids"] = [
        item for item in state["memory_state"]["pending_candidate_ids"] if item != stored["id"]
    ]
    state["memory_state"].update(
        {"last_commit_actor": args.actor, "last_commit_at": now, "last_commit_approval_id": args.approval_id}
    )
    _validate_candidate_state(state)
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="impact-apply",
        event_type="impact_applied" if args.decision != "rejected" else "impact_rejected",
        object_ids=[stored["id"], semantic_event["id"], *sorted(affected)],
        details={"decision": args.decision, "affected_ids": sorted(affected)},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "impact_assessment_id": stored["id"],
        "status": stored["status"],
        "stale_ids": stored.get("stale_object_ids", []),
        "state_version": _state_version(saved),
        "audit_event_hash": event["event_hash"],
        "external_actions_executed": 0,
    }


def command_seed_promote(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    seed = next((item for item in state.get("prospective_matter_seeds", []) if item.get("id") == args.seed_id), None)
    if seed is None:
        raise LegalCaseError("SEED_NOT_FOUND", f"潜在案件线索不存在：{args.seed_id}")
    if seed.get("status") in {"promoted", "rejected"}:
        raise LegalCaseError("SEED_ALREADY_DECIDED", "该潜在案件线索已经作出终局决定。")
    now = now_iso()
    if args.decision == "approved":
        if not args.new_matter_id:
            raise LegalCaseError("NEW_MATTER_ID_REQUIRED", "批准晋升时必须指定独立的 new-matter-id。")
        if args.new_matter_id == state["matter"]["id"]:
            raise LegalCaseError("NEW_MATTER_MUST_DIFFER", "新案件 ID 不能与来源案件相同。")
        event = make_case_event(
            state,
            event_type="new_matter_signal",
            title=f"潜在案件线索晋升：{seed.get('title')}",
            source_ids=seed.get("source_ids", []),
            related_object_ids=[seed["id"]],
            authority="lawyer",
            confidence="lawyer_confirmed",
        )
        seed["status"] = "promoted"
        seed["human_decision"] = "approved"
        seed["promoted_matter_id"] = args.new_matter_id
        seed["promotion_event_id"] = event["id"]
    else:
        event = make_case_event(
            state,
            event_type="new_matter_signal",
            title=f"潜在案件线索驳回：{seed.get('title')}",
            source_ids=seed.get("source_ids", []),
            related_object_ids=[seed["id"]],
            authority="lawyer",
            confidence="lawyer_confirmed",
        )
        seed["status"] = "rejected"
        seed["human_decision"] = "rejected"
        seed["promoted_matter_id"] = None
        seed["promotion_event_id"] = None
    seed["version"] = int(seed.get("version", 1)) + 1
    seed["decided_by"] = args.actor
    seed["decided_at"] = now
    seed["updated_at"] = now
    _advance_semantic_cursor(state, event)
    state["memory_state"]["pending_candidate_ids"] = [
        item for item in state["memory_state"]["pending_candidate_ids"] if item != seed["id"]
    ]
    state["memory_state"].update(
        {"last_commit_actor": args.actor, "last_commit_at": now, "last_commit_approval_id": args.approval_id}
    )
    _validate_candidate_state(state)
    audit_event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="seed-promote",
        event_type="prospective_matter_promoted" if args.decision == "approved" else "prospective_matter_rejected",
        object_ids=[seed["id"], event["id"]],
        details={"decision": args.decision, "new_matter_id": args.new_matter_id},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "seed_id": seed["id"],
        "status": seed["status"],
        "promoted_matter_id": seed.get("promoted_matter_id"),
        "new_workspace_created": False,
        "state_version": _state_version(saved),
        "audit_event_hash": audit_event["event_hash"],
        "external_actions_executed": 0,
    }


def command_workflow_start(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    now = now_iso()
    workflow_id = args.workflow_id or stable_id("WF", state["matter"]["id"], args.kind, args.title, now)
    if any(item.get("id") == workflow_id for item in state.get("workflow_instances", [])):
        raise LegalCaseError("WORKFLOW_ID_CONFLICT", f"工单已存在：{workflow_id}")
    existing_event_ids = {item.get("id") for item in state.get("case_events", [])}
    if args.input_event_id and args.input_event_id not in existing_event_ids:
        raise LegalCaseError("ORIGIN_EVENT_NOT_FOUND", f"来源事件不存在：{args.input_event_id}")
    event = make_case_event(
        state,
        event_type="workflow_change",
        title=f"启动工单：{args.title}",
        related_object_ids=[workflow_id, *(args.related_object_id or [])],
        authority="lawyer",
        confidence="lawyer_instruction",
    )
    origin_event_id = args.input_event_id or event["id"]
    workflow = {
        "id": workflow_id,
        "version": 1,
        "matter_id": state["matter"]["id"],
        "kind": args.kind,
        "title": args.title,
        "status": "in_progress",
        "origin_event_id": origin_event_id,
        "related_object_ids": list(dict.fromkeys(args.related_object_id or [])),
        "task_ids": [],
        "output_ids": [],
        "due_at": args.due_at,
        "waiting_for": [],
        "resume_condition": None,
        "approval_id": args.approval_id,
        "resolution_note": None,
        "created_at": now,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "completed_by": None,
    }
    state.setdefault("workflow_instances", []).append(workflow)
    _advance_semantic_cursor(state, event)
    _validate_candidate_state(state)
    audit_event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="workflow-start",
        event_type="workflow_started",
        object_ids=[workflow_id, event["id"]],
        details={"kind": args.kind, "due_at": args.due_at, "origin_event_id": origin_event_id},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "workflow_instance_id": workflow_id,
        "status": "in_progress",
        "state_version": _state_version(saved),
        "audit_event_hash": audit_event["event_hash"],
        "external_actions_executed": 0,
    }


def command_workflow_update(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    workflow = next((item for item in state.get("workflow_instances", []) if item.get("id") == args.workflow_id), None)
    if workflow is None:
        raise LegalCaseError("WORKFLOW_NOT_FOUND", f"工单不存在：{args.workflow_id}")
    if workflow.get("status") in {"completed", "cancelled", "failed"}:
        raise LegalCaseError("WORKFLOW_TERMINAL", "终态工单不能直接继续修改；如需新工作，请创建新工单。")
    now = now_iso()
    if args.action == "wait":
        if not args.waiting_for or not args.resume_condition:
            raise LegalCaseError("WAIT_DETAILS_REQUIRED", "等待状态必须同时说明 waiting-for 和 resume-condition。")
        workflow["status"] = "waiting_external"
        workflow["waiting_for"] = [args.waiting_for]
        workflow["resume_condition"] = args.resume_condition
    elif args.action == "resume":
        if workflow.get("status") not in {"waiting_external", "waiting_approval", "pending"}:
            raise LegalCaseError("WORKFLOW_NOT_WAITING", "只有等待或待办工单可以恢复。")
        workflow["status"] = "in_progress"
        workflow["waiting_for"] = []
        workflow["resume_condition"] = None
    elif args.action == "complete":
        proof_ids = list(dict.fromkeys((args.output_id or []) + ([args.completion_event_id] if args.completion_event_id else [])))
        if not proof_ids:
            raise LegalCaseError("COMPLETION_PROOF_REQUIRED", "完成工单必须绑定产物或完成事件；聊天结束不等于法律事项完成。")
        missing = [identifier for identifier in proof_ids if find_object(state, identifier)[1] is None]
        if missing:
            raise LegalCaseError("COMPLETION_PROOF_NOT_FOUND", "工单完成凭证不存在。", {"missing_ids": missing})
        tasks = {item.get("id"): item for item in state.get("tasks", [])}
        incomplete = [
            task_id for task_id in workflow.get("task_ids", [])
            if task_id not in tasks or tasks[task_id].get("status") not in {"done", "cancelled"}
        ]
        if incomplete:
            raise LegalCaseError("WORKFLOW_TASKS_INCOMPLETE", "工单仍有未完成子任务。", {"task_ids": incomplete})
        workflow["status"] = "completed"
        workflow["output_ids"] = list(dict.fromkeys(workflow.get("output_ids", []) + proof_ids))
        workflow["completed_at"] = now
        workflow["completed_by"] = args.actor
        workflow["resolution_note"] = args.reason or "已绑定内部完成产物或案件事件；不表示已执行外部提交动作。"
        workflow["waiting_for"] = []
        workflow["resume_condition"] = None
    else:
        if not args.reason:
            raise LegalCaseError("CANCELLATION_REASON_REQUIRED", "取消工单必须说明理由。")
        workflow["status"] = "cancelled"
        workflow["completed_at"] = now
        workflow["completed_by"] = args.actor
        workflow["resolution_note"] = args.reason
        workflow["waiting_for"] = []
        workflow["resume_condition"] = None
    workflow["version"] = int(workflow.get("version", 1)) + 1
    workflow["updated_at"] = now
    event = make_case_event(
        state,
        event_type="workflow_change",
        title=f"工单{args.action}：{workflow.get('title')}",
        related_object_ids=[workflow["id"], *workflow.get("output_ids", [])],
        authority="lawyer",
        confidence="lawyer_instruction",
    )
    _advance_semantic_cursor(state, event)
    _validate_candidate_state(state)
    audit_event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="workflow-update",
        event_type=f"workflow_{args.action}",
        object_ids=[workflow["id"], event["id"]],
        details={"action": args.action, "reason": args.reason, "external_action_completed": False},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "workflow_instance_id": workflow["id"],
        "status": workflow["status"],
        "state_version": _state_version(saved),
        "audit_event_hash": audit_event["event_hash"],
        "external_action_completed": False,
        "external_actions_executed": 0,
    }


def _load_input_snapshots(path: str) -> list[dict[str, Any]]:
    payload = load_json_object_or_list(Path(path).resolve())
    if isinstance(payload, dict):
        payload = payload.get("input_snapshot", payload.get("items"))
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise LegalCaseError("INVALID_INPUT_SNAPSHOT", "input-snapshot 必须是对象数组或含 input_snapshot 数组的对象。")
    return copy.deepcopy(payload)


def command_run_start(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    workflow = next((item for item in state.get("workflow_instances", []) if item.get("id") == args.workflow_id), None)
    if workflow is None:
        raise LegalCaseError("WORKFLOW_NOT_FOUND", f"工单不存在：{args.workflow_id}")
    if workflow.get("status") in {"completed", "cancelled", "failed"}:
        raise LegalCaseError("WORKFLOW_TERMINAL", "终态工单不能启动新的运行。")
    if args.task_id and args.task_id not in workflow.get("task_ids", []):
        raise LegalCaseError("TASK_NOT_IN_WORKFLOW", f"任务 {args.task_id} 不属于该工单。")
    if any(
        item.get("workflow_instance_id") == args.workflow_id
        and item.get("operation") == args.operation
        and item.get("status") in {"pending", "running"}
        for item in state.get("run_attempts", [])
    ):
        raise LegalCaseError("RUN_ALREADY_ACTIVE", "同一工单和操作已有活动运行。")
    snapshots = _load_input_snapshots(args.input_snapshot)
    for snapshot in snapshots:
        _collection, current = find_object(state, str(snapshot.get("object_id", "")))
        if current is None:
            raise LegalCaseError("SNAPSHOT_OBJECT_NOT_FOUND", f"输入快照对象不存在：{snapshot.get('object_id')}")
        version, digest = object_version_hash(current)
        if snapshot.get("version") != version or snapshot.get("hash") != digest:
            raise LegalCaseError("STALE_INPUT_SNAPSHOT", f"输入快照已经变化：{snapshot.get('object_id')}")
    now = now_iso()
    run_id = args.run_id or stable_id("RUN", args.workflow_id, args.operation, now)
    workspace = _workspace_from_state_path(state_path)
    if args.output_log:
        output_log_path = _ensure_memory_output(Path(args.output_log), state_path)
    else:
        output_log_path = _case_memory_root(state_path) / "runs" / f"{run_id}.jsonl"
    runs_root = (_case_memory_root(state_path) / "runs").resolve()
    try:
        output_log_path.resolve().relative_to(runs_root)
    except ValueError as exc:
        raise LegalCaseError("RUN_LOG_OUTSIDE_MATTER", "运行日志必须位于本案 _case-state/memory/runs。") from exc
    output_log = _relative_or_absolute(output_log_path, workspace)
    run = {
        "id": run_id,
        "version": 1,
        "workflow_instance_id": args.workflow_id,
        "task_id": args.task_id,
        "operation": args.operation,
        "status": "running",
        "actor": args.actor,
        "input_snapshot": snapshots,
        "output_ids": [],
        "output_log_path": output_log,
        "output_cursor": 0,
        "cursor_before": None,
        "cursor_after": None,
        "commit_status": "not_committed",
        "failure_reason": None,
        "created_at": now,
        "started_at": now,
        "finished_at": None,
        "state_version_before": _state_version(state),
        "state_version_after": None,
    }
    state.setdefault("run_attempts", []).append(run)
    _validate_candidate_state(state)
    audit_event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="run-start",
        event_type="run_started",
        object_ids=[run_id, args.workflow_id],
        details={"operation": args.operation, "input_count": len(snapshots)},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "run_id": run_id,
        "status": "running",
        "output_log_path": output_log,
        "state_version": _state_version(saved),
        "audit_event_hash": audit_event["event_hash"],
        "external_actions_executed": 0,
    }


def command_run_finish(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    _require_expected_state_version(state, args.expected_state_version)
    old_state = copy.deepcopy(state)
    run = next((item for item in state.get("run_attempts", []) if item.get("id") == args.run_id), None)
    if run is None:
        raise LegalCaseError("RUN_NOT_FOUND", f"运行不存在：{args.run_id}")
    if run.get("status") not in {"pending", "running"}:
        raise LegalCaseError("RUN_TERMINAL", "运行已经处于终态，不能重复完成。")
    status = "killed" if args.status == "aborted" else args.status
    output_ids = list(dict.fromkeys(args.output_id or []))
    if status in {"failed", "killed"}:
        commit_status = "discarded"
        if not args.reason:
            raise LegalCaseError("RUN_FAILURE_REASON_REQUIRED", "失败或中止运行必须记录原因。")
    else:
        commit_status = args.commit_status
        if commit_status == "committed":
            missing = [identifier for identifier in output_ids if find_object(state, identifier)[1] is None]
            if not output_ids or missing:
                raise LegalCaseError(
                    "COMMITTED_OUTPUT_NOT_FOUND",
                    "标记 committed 的运行必须绑定已经写入案件状态的输出对象。",
                    {"missing_ids": missing},
                )
    now = now_iso()
    run["version"] = int(run.get("version", 1)) + 1
    run["status"] = status
    run["output_ids"] = output_ids
    run["output_cursor"] = args.output_cursor
    run["cursor_after"] = args.cursor_after
    run["commit_status"] = commit_status
    run["failure_reason"] = args.reason
    run["finished_at"] = now
    run["state_version_after"] = _state_version(state) + 1 if status == "completed" and commit_status == "committed" else None
    _validate_candidate_state(state)
    audit_event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="run-finish",
        event_type=f"run_{status}",
        object_ids=[run["id"], run["workflow_instance_id"], *output_ids],
        details={"status": status, "commit_status": commit_status, "partial_output_internal_only": status != "completed"},
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "run_id": run["id"],
        "status": status,
        "commit_status": commit_status,
        "workflow_completed": False,
        "state_version": _state_version(saved),
        "audit_event_hash": audit_event["event_hash"],
        "external_actions_executed": 0,
    }


def command_reconcile_matter(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    _require_v11(state)
    validation = validate_state_file(state_path)
    report = reconcile_report(state, state_path)
    report["state_validation"] = validation
    report["ok"] = bool(report.get("ok")) and validation.get("ok") is True
    if args.output:
        output = _ensure_memory_output(Path(args.output), state_path)
        atomic_write_json(output, report)
        report["output"] = str(output)
    return report


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    result = validate_state_file(Path(args.state).resolve(), Path(args.schema).resolve() if args.schema else DEFAULT_SCHEMA)
    registry_result = validate_registry(Path(args.registry).resolve() if args.registry else DEFAULT_REGISTRY)
    personal_catalog_result = validate_personal_template_catalog(
        Path(args.personal_catalog).resolve() if args.personal_catalog else DEFAULT_PERSONAL_TEMPLATE_CATALOG
    )
    result["template_registry"] = registry_result
    result["personal_template_catalog"] = personal_catalog_result
    result["ok"] = result["ok"] and registry_result["ok"] and personal_catalog_result["ok"]
    return result


def _current_issue_is_deep(
    state: dict[str, Any] | None,
    scope_lock: list[str] | None = None,
    explicit_scope: bool = False,
) -> bool:
    if not state:
        return False
    focus = state.get("focus", {})
    target_ids = set(scope_lock or [])
    if not target_ids and not explicit_scope:
        target_ids.update(item for item in focus.get("range_lock", []) if isinstance(item, str))
        if isinstance(focus.get("current_issue_id"), str):
            target_ids.add(focus["current_issue_id"])
    if explicit_scope and not target_ids:
        return False
    return any(
        item.get("analysis_mode") == "deep"
        and item.get("importance") in {"high", "critical"}
        and item.get("status") not in {"approved", "stale"}
        and (not target_ids or item.get("id") in target_ids)
        for item in state.get("issues", [])
    )


def _extract_template_alias(text: str) -> str | None:
    prefix = r"(?:按照|按|依照|参照|参考)"
    continuation = r"(?=\s*(?:，|,)?\s*(?:给我|生成|写|制作|出|办理|$))"
    patterns = (
        rf"{prefix}\s*[‘’“”'\"]?([^，。；、：:]{{1,50}}?)[‘’“”'\"]?\s*模板{continuation}",
        rf"{prefix}\s*[‘’“”'\"]?([^，。；、：:]{{1,50}}?套件)[‘’“”'\"]?{continuation}",
        rf"{prefix}\s*[‘’“”'\"]?(TPL-[A-Za-z0-9_-]+)[‘’“”'\"]?{continuation}",
        rf"{prefix}\s*[‘’“”'\"]?([^，。；、：:]{{1,50}}?号)[‘’“”'\"]?{continuation}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip()
    return None


def _extract_modify_scope(text: str) -> list[str]:
    if not any(term in text for term in ("只修改", "仅修改", "局部修改", "只改", "仅改")):
        return []
    chinese_numbers = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    match = re.search(r"第\s*([一二三四五六七八九十]|\d+)\s*项(?:诉讼请求|诉请)", text)
    if match:
        token = match.group(1)
        number = int(token) if token.isdigit() else chinese_numbers[token]
        return [f"claim_item_{number}"]
    return [text]


def _resolve_issue_scope(
    text: str,
    state: dict[str, Any] | None,
    explicit_topic: bool = False,
) -> list[str]:
    """Resolve an analysis range without mutating FocusContext.

    Exact issue IDs/titles win. Common legal issue words may resolve only
    when they identify one state issue. Otherwise preserve the current
    explicit range lock; never invent a new issue ID.
    """
    if not state:
        return []
    explicit_scope = any(term in text for term in ("只分析", "仅分析", "只研究", "仅研究", "只看", "仅看"))
    issues = state.get("issues", [])
    matches: list[str] = []
    for issue in issues:
        issue_id = issue.get("id")
        title = str(issue.get("title") or "")
        if not issue_id:
            continue
        if issue_id in text or (title and title in text):
            matches.append(issue_id)
    if not matches:
        keywords = (
            "时效", "管辖", "质量", "包装", "破损", "货损", "运输", "交货", "瑕疵",
            "付款", "发票", "主体", "送达", "日期", "履行", "真实性", "披露",
        )
        for keyword in keywords:
            if keyword not in text:
                continue
            candidates = [
                issue.get("id") for issue in issues
                if issue.get("id") and keyword in str(issue.get("title") or "")
            ]
            if len(candidates) == 1:
                matches.extend(candidates)
    if matches:
        return list(dict.fromkeys(matches))
    if explicit_scope or explicit_topic:
        # A new explicit textual scope outranks the prior focus lock.  Keep it
        # unresolved in TaskFrame rather than silently reusing an unrelated ID.
        return []
    locked = state.get("focus", {}).get("range_lock", [])
    return [item for item in locked if isinstance(item, str)]


def _extract_memory_scope(text: str) -> list[str]:
    """Extract an explicitly named file/folder scope without broadening it.

    The result is intentionally textual.  Resolution against the matter workspace
    happens in the context builder, where path containment and source IDs can be
    checked deterministically.
    """
    scopes: list[str] = []
    for quoted in re.findall(r"[\"'“‘]([^\"'”’]+)[\"'”’]", text):
        candidate = quoted.strip()
        if re.search(r"(?:^[A-Za-z]:[\\/]|[/\\]|\.[A-Za-z0-9]{1,8}$)", candidate):
            scopes.append(candidate)
    path_match = re.search(
        r"([A-Za-z]:[\\/].+?)(?=(?:有关|相关|对应|然后|，|。|；|$))",
        text,
        flags=re.I,
    )
    if path_match:
        scopes.append(path_match.group(1).strip().strip("\"'“”‘’"))
    relation_match = re.search(r"只看(?:和|与)?(.+?)(?:有关|相关|对应)(?:的)?记忆", text)
    if relation_match:
        candidate = relation_match.group(1).strip().strip("\"'“”‘’")
        if candidate:
            scopes.append(candidate)
    bounded_match = re.search(
        r"(?:只|仅)(?:读|看|调|用|调用)(?:和|与)?(.+?)"
        r"(?:(?:有关|相关|对应)(?:的)?|这部分|的)记忆",
        text,
    )
    if bounded_match:
        candidate = bounded_match.group(1).strip().strip("\"'“”‘’")
        if candidate:
            scopes.append(candidate)
    direct_bounded_match = re.search(
        r"(?:只|仅)(?:读|看|调|用|调用)(?:和|与)?(.+?)记忆(?=分析|研究|讨论|生成|，|,|。|；|;|$)",
        text,
    )
    if direct_bounded_match:
        candidate = direct_bounded_match.group(1).strip().strip("\"'“”‘’")
        candidate = re.sub(r"(?:有关|相关|对应)(?:的)?$", "", candidate).strip().strip("\"'“”‘’")
        candidate = re.split(r"[，,]\s*(?:不要管|不看|别看)", candidate, maxsplit=1)[0].strip()
        if candidate and candidate not in {"案件", "本案", "全部", "所有", "完整"}:
            scopes.append(candidate)
    direct_limit_match = re.search(r"只看(.+?)(?:，|,)?(?:不要管|不看|别看)其他记忆", text)
    if direct_limit_match:
        candidate = direct_limit_match.group(1).strip().strip("\"'“”‘’")
        if candidate:
            scopes.append(candidate)
    return list(dict.fromkeys(scopes))


def _memory_controls(
    text: str,
    instruction_text: str | None = None,
) -> tuple[str, list[str], str]:
    """Resolve independent memory-read and memory-write controls.

    Reading context never implies permission to promote a model conclusion into
    canonical matter state.  Explicit user language has priority over defaults.
    """
    # Paths and named scopes may legitimately live in a quoted parameter, but
    # mode/write verbs may only come from instruction-plane text.  This keeps a
    # template named “正式记入案件” from turning a generation request into a
    # memory commit.
    control = instruction_text if instruction_text is not None else text
    memory_scope = _extract_memory_scope(text)
    def contains_unnegated_phrase(phrase: str) -> bool:
        for match in re.finditer(re.escape(phrase), control):
            prefix = control[max(0, match.start() - 8):match.start()]
            if re.search(r"(?:不要|别|不是|并非|不)(?:再)?\s*$", prefix):
                continue
            return True
        return False

    read_off = any(
        contains_unnegated_phrase(phrase)
        for phrase in (
            "不用关联记忆",
            "不要关联记忆",
            "不调用记忆",
            "不要调用记忆",
            "别调用记忆",
            "不用案件记忆",
            "不要用案件记忆",
            "不要读取记忆",
            "不读取记忆",
            "不要读记忆",
            "不读取案件记忆",
            "别看案件记忆",
            "不要看案件记忆",
            "仅依据当前消息",
            "只依据当前消息",
            "仅基于当前材料",
            "只基于当前材料",
            "仅以当前材料为准",
            "不要读取聊天记录",
            "关闭记忆",
            "只看当前消息",
            "只用当前附件",
            "仅凭这句话",
            "仅凭当前这句话",
            "仅凭这条消息",
            "就看我这条消息",
            "只看我这条消息",
            "从零分析",
            "独立分析当前材料",
        )
    ) or bool(re.search(
        r"(?:不要|别|不|无需|无须|不用|不必|不需要|禁止|不许|不得)(?:再)?(?:去)?"
        r"(?:读取|读|看|查看|翻|找|参考|结合|调用|调取|使用|利用|关联|带)"
        r"[^，。；]{0,10}(?:案件记忆|记忆|历史记录|之前聊天|之前内容|过往信息|过往记忆|"
        r"上下文|案件状态|既有记忆|历史上下文)"
        r"|(?:不要|别|不|无需|无须|不用|不必|不需要|禁止|不许|不得)(?:再)?"
        r"从(?:案件记忆|记忆|历史记录|上下文|案件状态)(?:里|中)?"
        r"[^，。；]{0,6}(?:找|翻|读取|读|查看|调取)?"
        r"|忽略(?:之前|以往|案件|历史)?(?:的)?(?:记忆|上下文|信息|记录)"
        r"|本轮禁用记忆|不带历史上下文",
        control,
    ))
    if read_off:
        memory_mode = "off"
    elif (
        any(phrase in control for phrase in ("不要管其他记忆", "只看和", "只看与", "文件限定"))
        or re.search(r"只看.+?(?:有关|相关|对应)(?:的)?记忆", control)
        or re.search(
            r"(?:只|仅)(?:读|看|调|用|调用)(?:和|与)?.+?"
            r"(?:(?:有关|相关|对应)(?:的)?|这部分|的)记忆",
            control,
        )
        or (
            bool(memory_scope)
            and re.search(r"(?:只|仅)(?:读|看|调|用|调用).+?记忆", control)
        )
    ) and "记忆" in control:
        memory_mode = "file_scoped"
    elif any(
        phrase in control
        for phrase in (
            "调用下记忆模块",
            "调用记忆模块",
            "看看有没关联或启发",
            "有没有关联或启发",
            "重新认识这个案子",
            "合起来重新看看",
            "长出新的案子",
        )
    ):
        memory_mode = "reflect"
    else:
        memory_mode = "relevant"

    if memory_mode == "off" or any(
        phrase in control
        for phrase in (
            "不要写入记忆", "本轮不写入", "不要记住", "只完成当前任务",
            "不保存到记忆", "别写进案件记忆", "不要更新记忆", "不沉淀到记忆", "不记入案件",
            "本轮禁止写记忆", "只读记忆不写回", "查记忆但别改记忆",
        )
    ) or re.search(
        r"(?:不要|别|不|先不|先别|暂不|无需|无须|不用|不必|不需要)(?:再)?"
        r"(?:把[^，。；]{0,12})?(?:正式|确认)?(?:存进|保存|写入|写进|更新|沉淀|记入|记成|"
        r"记下来|加入|写回)[^，。；]{0,10}(?:案件|候选)?(?:记忆|状态)?"
        r"|(?:不要|别|不|禁止|不许|不得)(?:"
        r"往案件里(?:写|写回|改)(?:记忆)?|(?:写|写回|改)(?:案件)?(?:记忆|状态))",
        control,
    ):
        write_mode = "none"
    elif any(
        phrase in control
        for phrase in ("正式记入案件", "写入案件状态", "正式更新记忆", "确认写入记忆")
    ):
        write_mode = "commit"
    else:
        # New semantic conclusions remain candidates unless the user makes an
        # exact, reviewable commit request. Mechanical audit events are separate.
        write_mode = "candidate"
    return memory_mode, memory_scope, write_mode


def _assess_route_capabilities(
    action: str,
    network_mode: str,
    capabilities: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Describe an explicit capability degradation without claiming a search ran."""
    if capabilities is None:
        return None
    reports: list[str] = []
    allowed_sources = ["user_provided", "local_verified"]
    forbidden_claims = ["invent_case", "promote_summary_to_verified"]
    status = "available_for_requested_local_step"
    search_complete: bool | None = None

    if action == "research":
        search_complete = False
        if network_mode == "deny":
            status = "completed_local_only"
            reports.extend(["network_not_used", "authority_questions_unresolved", "search_incomplete"])
            forbidden_claims.extend(["call_public_web", "claim_complete_authority_search"])
        elif capabilities.get("public_web") is not True:
            status = "degraded_or_stopped"
            reports.extend(["public_web_unavailable", "search_incomplete", "unverified_items_blocked"])
            forbidden_claims.extend(["claim_database_searched", "claim_complete_authority_search"])
        else:
            status = "continue_with_public_sources"
            allowed_sources.append("public_web_verified")
            reports.append("source_scope")
        if capabilities.get("member_database") is not True:
            reports.append("member_database_not_connected")
            forbidden_claims.append("claim_proprietary_coverage")
        if capabilities.get("third_party_api") is not True:
            reports.append("third_party_api_not_connected")
            forbidden_claims.append("claim_third_party_api_coverage")
    elif capabilities.get("ocr") == "partial":
        status = "partial_with_blockers"
        reports.extend(["processed_range", "failed_pages", "manual_check_items"])
        forbidden_claims.extend(["claim_full_coverage", "infer_unread_content"])
    elif network_mode == "deny":
        status = "completed_local_only"
        reports.append("network_not_used")

    return {
        "status": status,
        "degraded": status in {"degraded_or_stopped", "partial_with_blockers"},
        "capabilities_observed": copy.deepcopy(capabilities),
        "allowed_sources": list(dict.fromkeys(allowed_sources)),
        "source_scope": list(dict.fromkeys(allowed_sources)),
        "must_report": list(dict.fromkeys(reports)),
        "claims_forbidden": list(dict.fromkeys(forbidden_claims)),
        "search_complete": search_complete,
        "external_services_called": [],
    }


def route_text(
    text: str,
    state: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    input_materials: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_text = text.strip()
    if not source_text:
        raise LegalCaseError("EMPTY_INTENT", "路由文本不能为空。")
    language_draft = prepare_language_input(source_text)
    # A title does not have to contain a fixed word such as “合同” or “附件”.
    # When “只看 X” uniquely resolves to an existing matter source, promote it
    # to a hard read ACL before memory/network policy is compiled.  Ambiguous or
    # missing titles are left unresolved for the one-question gate below.
    if (
        state is not None
        and language_draft.get("read_scope_candidate")
        and not language_draft.get("source_only_read")
    ):
        _, resolved_scope_acl = _resolve_scope_source(language_draft.get("scope_target"), state)
        if resolved_scope_acl:
            language_draft["source_only_read"] = True
            language_draft["strict_read_acl"] = True
    # Existing deterministic routing consumes command-plane text only.  Quoted
    # evidence remains visible in TaskFrame.input_segments but cannot trigger an
    # action merely because it contains words such as “删除” or “提交”.
    control_text = language_draft["command_text"].strip()
    raw = language_draft["action_text"].strip()
    lowered = raw.casefold()

    finance_terms = (
        "理财", "炒股", "股票", "基金", "财富管理", "每月工资", "个人财产配置",
        "财务规划", "工资存款规划", "存款规划", "投资建议", "资产配置", "收入规划",
    )
    personal_money_compound = (
        (any(term in raw for term in ("工资", "收入", "存款")) and any(term in raw for term in ("投资", "理财", "资产配置")))
        or bool(re.search(r"(?:工资|收入|存款).{0,12}(?:规划|配置)", raw))
    )
    if any(term in raw for term in finance_terms) or personal_money_compound:
        return {
            "ok": False,
            "code": "OUT_OF_SCOPE",
            "message": "当前源码包仅处理法律办案，不包含收入、财富、投资或收费报价模块。",
            "route": {"action": "stop", "object": "out_of_scope", "skill": None, "stop_condition": "scope_boundary"},
        }

    exact_ok = bool(re.fullmatch(r"\s*(?:ok|okay|好的?|可以|就这个|同意)[。！!]?\s*", lowered, flags=re.I))
    presentation_only = any(term in raw for term in ("不要那么复杂", "不要复杂", "简单说", "很简单", "只说主要", "大致", "其他先不展开", "最能打我的点"))
    network_mode = "deny" if language_draft.get("no_network") else "local_only"
    if language_draft.get("current_input_only"):
        # “仅依据当前消息” is an input boundary, not merely a memory hint.  It
        # excludes both inherited matter context and public-web material.
        network_mode = "deny"
    if language_draft.get("source_only_read"):
        network_mode = "deny"
    action: str
    object_name: str
    skill: str
    zh_action: str
    zh_object: str
    next_routes: list[str] = []
    template_alias = _extract_template_alias(control_text)
    memory_mode, memory_scope, write_memory_mode = _memory_controls(control_text, raw)
    if language_draft.get("source_only_read") and "记忆" not in raw:
        memory_mode, memory_scope, write_memory_mode = "off", [], "none"
    if state is None and memory_mode == "relevant" and "记忆" not in control_text:
        explicit_memory_write = any(
            phrase in control_text
            for phrase in (
                "记成候选", "记入记忆候选", "准备往记忆里增加",
                "正式记入案件", "写入案件状态", "正式更新记忆", "确认写入记忆",
            )
        )
        memory_mode, memory_scope = "off", []
        if not explicit_memory_write:
            write_memory_mode = "none"
    memory_reflection = memory_mode == "reflect"
    lifecycle_terms = (
        "撤诉", "财产保全", "保全申请", "解除保全", "调取审计报告", "申请调取", "法院补材料",
        "法院要求补", "送达", "开庭日期", "继续上次的工单", "恢复工单", "等待法院", "等待客户",
    )
    delta_terms = (
        "又来了一批材料", "来了一批材料", "新增材料", "最近两批", "最近三批", "处理这批",
        "材料到达", "新邮件", "这封邮件有什么用", "会不会影响原来的判断", "是不是期限",
        "记一下这个日期", "这个日期记一下", "潜在新案", "产生新案",
    )

    action_hint = language_draft.get("action_hint")
    if exact_ok:
        action, object_name, skill, zh_action, zh_object = "approve", "matter", "legal-case-orchestrator", "批准", "案件"
    elif action_hint in {"constraint_only", "control_only"}:
        action, object_name, skill, zh_action, zh_object = "acknowledge", "action_constraint", None, "确认", "动作约束"
        memory_mode, memory_scope, write_memory_mode = "off", [], "none"
        network_mode = "deny"
    elif action_hint == "external_action":
        action, object_name, skill, zh_action, zh_object = "stop", "external_action", None, "阻断", "外部动作"
        memory_mode, memory_scope, write_memory_mode = "off", [], "none"
    elif action_hint == "authority_research":
        action, object_name, skill, zh_action, zh_object = "research", "issue", "legal-authority-research", "检索", "争点"
        if network_mode != "deny":
            network_mode = "public_web_if_available"
    elif action_hint in {"case_research", "followup_deepen"}:
        action, object_name, skill, zh_action, zh_object = "analyze", "issue", "legal-case-analysis", "分析", "争点"
    elif action_hint == "discuss":
        action, object_name, skill, zh_action, zh_object = "analyze", "issue", "legal-case-analysis", "分析", "争点"
    elif action_hint == "analyze":
        action, object_name, skill, zh_action, zh_object = "analyze", "issue", "legal-case-analysis", "分析", "争点"
    elif action_hint == "followup_reference":
        action, object_name, skill, zh_action, zh_object = "view", "matter", "legal-case-orchestrator", "查看", "案件"
    elif action_hint == "generate" and not any(term in control_text for term in (
        "手续", "整理文件夹", "待打印", "打印包", "提交包", "组卷",
    )):
        action, object_name, skill, zh_action, zh_object = "generate", "document", "legal-document-drafting", "生成", "文书"
    elif language_draft.get("external_reference"):
        action, object_name, skill, zh_action, zh_object = "view", "matter", "legal-case-orchestrator", "查看", "案件"
    elif memory_reflection:
        action, object_name, skill, zh_action, zh_object = "recall", "matter_memory", "legal-matter-delta-intake", "召回", "案件记忆"
    elif write_memory_mode == "commit" or any(term in raw for term in ("记成候选", "记入记忆候选", "准备往记忆里增加")):
        action, object_name, skill, zh_action, zh_object = "record", "matter_memory", "legal-matter-delta-intake", "记录", "案件记忆"
    elif any(term in raw for term in lifecycle_terms):
        is_resume = any(term in raw for term in ("继续上次", "恢复工单", "继续办理", "还没拿到"))
        action = "resume" if is_resume else "manage"
        zh_action = "恢复" if is_resume else "办理"
        object_name, skill, zh_object = "workflow", "legal-case-lifecycle-manager", "工单"
    elif any(term in raw for term in delta_terms):
        action, object_name, skill = "triage", "material_batch", "legal-matter-delta-intake"
        if any(term in raw for term in ("正式记入案件", "记一下", "是不是期限")):
            zh_action = "记录"
            zh_object = "期限" if any(term in raw for term in ("日期", "期限")) else "案件记忆"
        else:
            zh_action, zh_object = "查看", "材料批次"
    elif any(term in raw for term in ("去掉不能给法院看的", "去掉内部备注", "去掉审稿词", "去掉水印", "清洁", "清理批注", "接受修订")):
        action, object_name, skill, zh_action, zh_object = "sanitize", "document", "legal-document-review", "清洁", "文书"
    elif any(term in raw for term in ("支持案例", "支持的案例", "补充检索", "法源")):
        action, object_name, skill, zh_action, zh_object = "research", "issue", "legal-authority-research", "检索", "争点"
        if network_mode != "deny":
            network_mode = "public_web_if_available"
    elif any(term in raw for term in ("证据暂不提交", "证据不要提交", "证据先不要提交", "不提交这个证据", "这份证据先不要提交", "证据备用", "后续备用", "只作内部参考")):
        action, object_name, skill, zh_action, zh_object = "modify", "evidence", "legal-evidence-gate", "修改", "证据"
    elif "证据" in raw and any(term in raw for term in ("简版", "详细版", "简详", "两个版本", "两版")):
        action, object_name, skill, zh_action, zh_object = "modify", "evidence", "legal-evidence-gate", "修改", "证据"
    elif "页码" in raw and any(term in raw for term in ("回填", "填进", "填入", "补页码", "加页码")):
        action, object_name, skill, zh_action, zh_object = "modify", "document", "legal-document-drafting", "修改", "文书"
    elif any(term in raw for term in ("压缩到", "缩短到", "精简到", "压缩文书")):
        action, object_name, skill, zh_action, zh_object = "modify", "document", "legal-draft-compressor", "修改", "文书"
    elif any(term in raw for term in ("只修改", "仅修改", "局部修改", "改一下第", "只改", "仅改")):
        action, object_name, skill, zh_action, zh_object = "modify", "document", "legal-document-drafting", "修改", "文书"
    elif template_alias is not None and any(
        term in control_text for term in ("模板", "文书", "诉状", "起诉状", "答辩状", "上诉状", "代理意见", "Word", "DOCX", "PDF", "简版", "详细版")
    ):
        action, object_name, skill, zh_action, zh_object = "generate", "document", "legal-document-drafting", "生成", "文书"
        if any(term in raw for term in ("手续", "整理文件夹", "待打印", "提交包")):
            next_routes.append("legal-filing-packager")
    elif any(term in raw for term in ("手续", "整理文件夹", "待打印", "打印包", "提交包", "组卷", "整理下文件夹")):
        action, object_name, skill, zh_action, zh_object = "package", "filing_package", "legal-filing-packager", "组卷", "提交包"
    elif any(term in raw for term in ("大致内容", "概括", "总结", "了解得怎么样", "了解的怎么样", "这个事你了解")):
        if "了解" in raw:
            action, object_name, skill, zh_action, zh_object = "summarize", "matter", "legal-case-orchestrator", "总结", "案件"
        else:
            action, object_name, skill, zh_action, zh_object = "summarize", "material", "legal-material-intake", "总结", "材料"
    elif any(term in raw for term in ("看一下这个材料", "看一下全部材料", "看材料", "阅读材料", "只看我给的材料", "我给的材料", "全部材料", "材料更新", "重新运行", "这个材料")):
        action, object_name, skill, zh_action, zh_object = "view", "material", "legal-material-intake", "查看", "材料"
    else:
        action, object_name, skill, zh_action, zh_object = "view", "matter", "legal-case-orchestrator", "查看", "案件"

    explicit_issue_scope = language_draft.get("explicit_issue_topic") is True or any(
        term in control_text for term in ("只分析", "仅分析", "只研究", "仅研究", "只看", "仅看")
    )
    scope_lock = (
        _resolve_issue_scope(
            control_text,
            state,
            language_draft.get("explicit_issue_topic") is True,
        )
        if action in {"analyze", "research"} and not language_draft.get("current_input_only")
        else []
    )
    depth_text = raw
    for negated in ("不要那么复杂", "不要复杂", "不复杂", "很简单"):
        depth_text = depth_text.replace(negated, "")
    deep_terms = ("复杂", "深度", "最难", "矛盾", "站在对方角度", "对方会怎么", "时效", "管辖")
    reasoning_depth = "deep" if (
        action == "research"
        or action_hint in {"case_research", "followup_deepen"}
        or memory_reflection
        or _current_issue_is_deep(state, scope_lock, explicit_issue_scope)
        or any(term in depth_text for term in deep_terms)
    ) else "standard"
    display_length = "brief" if (
        presentation_only
        or action == "research"
        or (action == "view" and object_name == "material")
        or memory_reflection
        or any(term in raw for term in ("大致内容", "了解得怎么样", "了解的怎么样"))
    ) else "normal"
    if any(term in raw for term in ("详细", "细致", "全面")) and not presentation_only:
        display_length = "detailed"
    audience = "court_candidate" if any(term in control_text for term in ("给法院", "提交稿", "直接生成文书")) else "discussion"
    if action == "generate":
        audience = "court_candidate" if any(term in control_text for term in ("给法院", "提交稿", "法院候选")) else "internal_review"
    if action == "generate" and language_draft.get("client_email_request") is True:
        audience = "internal_review"
    if action in {"research", "package", "recall"}:
        audience = "internal_review"
    if any(term in control_text for term in ("内部审阅", "内部稿", "仅供内部", "探讨稿", "探讨完")):
        audience = "internal_review"
    if skill in {"legal-material-intake", "legal-matter-delta-intake", "legal-case-lifecycle-manager"}:
        network_mode = "deny"
    format_text = control_text.casefold()
    for scoped_value in memory_scope:
        format_text = format_text.replace(scoped_value.casefold(), "")
    formats = ["docx"] if action == "generate" and object_name != "client_email" else ["chat"]
    if "markdown" in format_text or re.search(r"(?:生成|输出|导出|格式).{0,8}\bmd\b", format_text):
        formats = ["markdown"]
    if "word" in format_text or "docx" in format_text:
        formats = ["docx"]
    if "pdf" in format_text:
        formats.append("pdf") if formats != ["chat"] else formats.__setitem__(0, "pdf")
    if language_draft.get("no_file_write") or any(term in control_text for term in ("只在聊天", "只贴文字", "不生成文件", "不写文件")):
        formats = ["chat"]
    modify_scope = _extract_modify_scope(control_text) if action == "modify" else []
    if memory_mode == "off":
        read_scope = ["current-request-only"]
    elif memory_mode == "file_scoped":
        read_scope = memory_scope.copy()
    elif memory_mode == "reflect":
        read_scope = ["matter-memory:reflection"]
    else:
        read_scope = ["matter-memory:relevant"]
    if network_mode == "public_web_if_available":
        read_scope.append("public-web")
    analysis_lens = "strongest_adverse_path" if any(
        term in raw for term in ("站在对方角度", "对方会怎么", "对方最强", "最能打我的点", "反方角度")
    ) else "neutral"
    research_policy = ({
        "include_material_adverse": True,
        "preserve_verification_status": True,
        "exclude_unverified_from_court_candidate": True,
    } if action == "research" else None)
    required_output = (["adverse_fact", "evidence_gap", "fact_that_changes_outcome"]
                       if analysis_lens == "strongest_adverse_path" else [])
    if action == "modify" and object_name == "document" and modify_scope:
        required_output.append("minimal_change_diff")
    intent = {
        "id": f"INT-{uuid.uuid4().hex[:16]}",
        "raw_text": source_text,
        "action": zh_action,
        "object": zh_object,
        "reasoning_depth": reasoning_depth,
        "display_length": display_length,
        "audience": audience,
        "output_formats": list(dict.fromkeys(formats)),
        "template_alias": template_alias,
        "read_scope": read_scope,
        "network_mode": network_mode,
        "modify_scope": modify_scope,
        "scope_lock": scope_lock,
        "analysis_lens": analysis_lens,
        "approval_response": "ok" if exact_ok else "none",
        "memory_mode": memory_mode,
        "read_memory_scope": memory_scope,
        "write_memory_mode": write_memory_mode,
        "created_at": now_iso(),
    }
    requires_approval = action in {"approve", "sanitize", "package"} or audience == "court_candidate" or object_name == "evidence"
    stop_condition = {
        "approve": "approval_validation",
        "research": "verified_answer_or_declared_degradation",
        "generate": "draft_created_or_gate_blocked",
        "sanitize": "derived_copy_and_report_or_authorization_block",
        "package": "candidate_package_or_gate_blocked",
        "stop": "external_action_blocked",
    }.get(action, "requested_view_completed_or_missing_input")
    result = {
        "ok": True,
        "intent": intent,
        "route": {
            "action": action,
            "object": object_name,
            "skill": skill,
            "next_skills": next_routes,
            "scope_lock": scope_lock,
            "analysis_lens": analysis_lens,
            "required_output": required_output,
            "research_policy": research_policy,
            "memory_policy": {
                "read_mode": memory_mode,
                "read_scope": memory_scope,
                "write_mode": write_memory_mode,
                "canonical_store": "matter_workspace",
                "chat_transcript_is_authority": False,
            },
            "stop_condition": stop_condition,
        },
        "safety": {
            "network_mode": network_mode,
            "requires_approval": requires_approval,
            "presentation_only_reduction": presentation_only,
            "reasoning_depth_preserved": presentation_only and reasoning_depth == "deep",
            "external_actions_allowed": False,
            "memory_read_does_not_authorize_write": True,
            "filing_and_external_action_gates_preserved_when_memory_off": True,
        },
    }
    result["input_materials"] = [
        {key: item[key] for key in ("id", "role", "origin", "source_sha256", "text_sha256", "record_path") if key in item}
        for item in (input_materials or [])
    ]
    capability_assessment = _assess_route_capabilities(action, network_mode, capabilities)
    if capability_assessment is not None:
        result["capability_assessment"] = capability_assessment
    language_control = compile_language_control(source_text, result, state, language_draft)
    for key, value in language_control.items():
        schema_path = LANGUAGE_CONTRACT_SCHEMAS[key]
        errors = validate_against_schema(value, load_json(schema_path))
        if errors:
            raise LegalCaseError(
                "LANGUAGE_CONTRACT_INVALID",
                f"自然语言控制对象 {key} 未通过内部契约校验。",
                {"errors": errors[:20]},
            )
        result[key] = value
    semantic_errors = validate_language_control_semantics(language_control)
    if semantic_errors:
        raise LegalCaseError(
            "LANGUAGE_CONTROL_SEMANTIC_INVALID",
            "自然语言控制对象之间存在不安全或矛盾的执行约束。",
            {"errors": semantic_errors[:20]},
        )
    page_fill = "页码" in raw and any(term in raw for term in ("回填", "填进", "填入", "补页码", "加页码"))
    local_edit = any(term in raw for term in ("只修改", "仅修改", "局部修改", "改一下第", "只改", "仅改"))
    if action == "modify" and object_name == "document" and (page_fill or local_edit):
        # A bounded tool hint only: the caller must inspect the real file and
        # resolve its exact coordinates; natural language does not execute edits.
        result["route"]["local_file_operation"] = {
            "inspect_command": "docx-inspect", "edit_command": "evidence-pages" if page_fill else "docx-patch",
            "requires_exact_source_hash": True, "requires_exact_targets": True,
            "preserve_outside_scope": True, "does_not_grant_filing_approval": True,
            "fallback_to_regeneration": False, "status": "suggested_not_executed",
        }
    return result


def command_route(args: argparse.Namespace) -> dict[str, Any]:
    state = load_json(Path(args.state).resolve()) if args.state else None
    capabilities = None
    if args.capabilities:
        raw_capabilities = args.capabilities.strip()
        if raw_capabilities.startswith("{"):
            try:
                capabilities = json.loads(raw_capabilities)
            except json.JSONDecodeError as exc:
                raise LegalCaseError(
                    "INVALID_CAPABILITIES_JSON",
                    "--capabilities 内联JSON无法解析。",
                    {"line": exc.lineno, "column": exc.colno, "detail": exc.msg},
                ) from exc
        else:
            capabilities = load_json(Path(args.capabilities).resolve())
        if not isinstance(capabilities, dict):
            raise LegalCaseError("CAPABILITIES_NOT_OBJECT", "--capabilities 必须是 JSON 对象或该对象的文件路径。")
    input_materials = [load_json(Path(path).resolve()) for path in getattr(args, "material_record", [])]
    return route_text(args.text, state, capabilities, input_materials)


def _task_records(args: argparse.Namespace) -> list[dict]:
    return [load_json(Path(path).resolve()) for path in getattr(args, "material_record", [])]


def command_source_links(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **resolve_source_links(args.sha256, Path(args.source_map), PROJECT_ROOT)}


def command_material_import(args: argparse.Namespace) -> dict[str, Any]:
    record = ingest_material(Path(args.file), Path(args.output_dir), args.role, origin=args.origin)
    return {"ok": True, "record": record, "record_path": record["record_path"]}


def command_material_import_original(args: argparse.Namespace) -> dict[str, Any]:
    parts = load_json(Path(args.parts).resolve())
    if not isinstance(parts, list):
        raise LegalCaseError("MATERIAL_PARTS_INVALID", "--parts 必须是完整原件分块的 JSON 数组文件。")
    record = import_original_parts(parts, Path(args.output_dir), args.role)
    return {"ok": True, "record": record, "record_path": record["record_path"]}


def command_material_read(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **read_material(load_json(Path(args.record).resolve()), args.offset, args.limit)}


def command_task_render(args: argparse.Namespace) -> dict[str, Any]:
    return render_task_draft(_task_records(args), load_json(Path(args.proposal).resolve()), Path(args.output_dir))


def command_docx_inspect(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **inspect_docx_targets(Path(args.file))}


def command_docx_patch(args: argparse.Namespace) -> dict[str, Any]:
    return render_docx_patch(Path(args.file), load_json(Path(args.plan).resolve()), Path(args.output_dir))


def command_evidence_pages(args: argparse.Namespace) -> dict[str, Any]:
    return build_evidence_pages(load_json(Path(args.plan).resolve()), Path(args.output_dir))


def command_delivery_current(args: argparse.Namespace) -> dict[str, Any]:
    return get_current_delivery(Path(args.store_dir))


def command_delivery_publish(args: argparse.Namespace) -> dict[str, Any]:
    expected = None if args.expected_current == "none" else args.expected_current
    return publish_delivery(Path(args.task_dir), Path(args.store_dir), expected_current=expected)


def command_delivery_restore(args: argparse.Namespace) -> dict[str, Any]:
    expected = None if args.expected_current == "none" else args.expected_current
    return restore_delivery(Path(args.store_dir), args.version_id, expected_current=expected)


def command_learning_build(args: argparse.Namespace) -> dict[str, Any]:
    return build_learning_candidate(_task_records(args), load_json(Path(args.proposal).resolve()), Path(args.output))


def command_learning_save(args: argparse.Namespace) -> dict[str, Any]:
    return approve_learning(Path(args.candidate), Path(args.library_dir), load_json(Path(args.confirmation).resolve()))


def command_learning_load(args: argparse.Namespace) -> dict[str, Any]:
    return load_learning(Path(args.path))


def command_learning_check(args: argparse.Namespace) -> dict[str, Any]:
    from legal_case_os_lib.learning import check_learning_application, _new_json
    draft_path = Path(args.draft_text).resolve()
    draft_hash = sha256_file(draft_path)
    draft_text = draft_path.read_text(encoding="utf-8-sig")
    report = check_learning_application(Path(args.learning), load_json(Path(args.application)),
                                       draft_text, _task_records(args))
    if sha256_file(draft_path) != draft_hash:
        raise LegalCaseError("LEARNING_OUTPUT_CHANGED", "核对期间本稿发生变化，未保存应用报告。")
    report.update(draft_text_path=str(draft_path), draft_text_sha256=draft_hash)
    output = Path(args.output).resolve()
    _new_json(output, report)
    return {**report, "report_path": str(output)}


def _hash_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.casefold() == right.casefold()


def command_approve_ok(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    old_state = copy.deepcopy(state)
    focus = state.get("focus", {})
    active_turn = focus.get("active_turn_id")
    candidates = [
        item
        for item in focus.get("recent_candidates", [])
        if item.get("just_displayed")
        and not item.get("stale")
        and active_turn is not None
        and item.get("displayed_in_turn") == active_turn
        and item.get("pending_approval_id") in focus.get("pending_approval_ids", [])
    ]
    if len(candidates) != 1:
        raise LegalCaseError(
            "APPROVAL_CONTEXT_NOT_UNIQUE",
            "ok 只能批准当前轮唯一、刚展示且未失效的精确对象。",
            {"approved": False, "candidate_count": len(candidates)},
        )
    candidate = candidates[0]
    approvals = [item for item in state.get("approvals", []) if item.get("id") == candidate["pending_approval_id"]]
    if len(approvals) != 1:
        raise LegalCaseError("PENDING_APPROVAL_NOT_FOUND", "候选没有唯一对应的待批准记录。", {"approved": False})
    approval = approvals[0]
    if approval.get("gate") == "G5_external_action":
        raise LegalCaseError("G5_EXPLICIT_AUTHORIZATION_REQUIRED", "外部动作必须明确写出动作和目标，不能只用 ok 授权。", {"approved": False})
    if approval.get("decision") != "pending" or approval.get("status") != "pending":
        raise LegalCaseError("APPROVAL_NOT_PENDING", "对应批准记录已不是 pending。", {"approved": False})
    if approval.get("gate") in {
        "G1_strategy", "G2_evidence", "G3_draft_plan", "G4_final", "SANITIZE", "MEMORY_COMMIT", "WORKFLOW_ACTION"
    } and not approval.get("scope_snapshot"):
        raise LegalCaseError(
            "APPROVAL_SCOPE_EMPTY",
            f"{approval.get('gate')} 在批准前必须精确绑定至少一个上游对象。",
            {"approved": False},
        )
    actual_scope_hash = sha256_bytes(canonical_json(approval.get("scope_snapshot", [])).encode("utf-8"))
    if approval.get("scope_hash") != actual_scope_hash:
        raise LegalCaseError(
            "APPROVAL_SCOPE_HASH_MISMATCH",
            "待批准记录的 scope_snapshot 与 scope_hash 不一致，必须重新建立并展示批准对象。",
            {"approved": False, "registered": approval.get("scope_hash"), "actual": actual_scope_hash},
        )
    if approval.get("stage") != state.get("matter", {}).get("stage"):
        raise LegalCaseError(
            "APPROVAL_STAGE_MISMATCH",
            "待批准记录不属于案件当前阶段，必须在当前阶段重新展示。",
            {"approved": False, "approval_stage": approval.get("stage"), "matter_stage": state.get("matter", {}).get("stage")},
        )
    _collection, current = find_object(state, candidate["object_id"])
    if current is None:
        raise LegalCaseError("APPROVAL_OBJECT_NOT_FOUND", "待批准对象已不存在。", {"approved": False})
    current_version, current_hash = object_version_hash(current)
    if (
        int(candidate["version"]) != int(approval["object_version"])
        or int(candidate["version"]) != current_version
        or not _hash_equal(candidate.get("hash"), approval.get("object_hash"))
        or not _hash_equal(candidate.get("hash"), current_hash)
    ):
        raise LegalCaseError(
            "APPROVAL_OBJECT_CHANGED",
            "候选、批准记录和当前对象的版本或哈希不一致，必须重新展示。",
            {
                "approved": False,
                "candidate_version": candidate.get("version"),
                "approval_version": approval.get("object_version"),
                "current_version": current_version,
                "candidate_hash": candidate.get("hash"),
                "approval_hash": approval.get("object_hash"),
                "current_hash": current_hash,
            },
        )
    for snapshot in approval.get("scope_snapshot", []):
        _scope_collection, scoped = find_object(state, snapshot.get("object_id", ""))
        if scoped is None:
            raise LegalCaseError(
                "APPROVAL_SCOPE_OBJECT_MISSING",
                f"批准范围对象不存在：{snapshot.get('object_id')}",
                {"approved": False},
            )
        scope_version, scope_hash = object_version_hash(scoped)
        if scope_version != snapshot.get("version") or not _hash_equal(scope_hash, snapshot.get("hash")):
            raise LegalCaseError(
                "APPROVAL_SCOPE_CHANGED",
                f"批准范围对象已变化：{snapshot.get('object_id')}，必须重新展示。",
                {"approved": False},
            )
    approval.update({
        "decision": "approved",
        "reason": args.reason or "用户在唯一、当前且未变化的候选上下文中确认 ok",
        "actor": args.actor,
        "decided_at": now_iso(),
        "status": "active",
        "invalidation_reason": None,
    })
    candidate["just_displayed"] = False
    focus["pending_approval_ids"] = [identifier for identifier in focus["pending_approval_ids"] if identifier != approval["id"]]
    if not focus["pending_approval_ids"]:
        state["status"]["workflow"] = "active"
        state["status"]["current_gate"] = None
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="approve-ok",
        event_type="approval_granted",
        object_ids=[approval["id"], approval["object_id"]],
        details={
            "gate": approval["gate"],
            "version": current_version,
            "hash": current_hash,
            "scope_hash": approval["scope_hash"],
        },
        old_state=old_state,
    )
    saved = load_json(state_path)
    return {
        "ok": True,
        "approved": True,
        "approval_id": approval["id"],
        "object_id": approval["object_id"],
        "gate": approval["gate"],
        "state_hash": state_content_hash(saved),
        "audit_event_hash": event["event_hash"],
    }


def command_invalidate(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    old_state = copy.deepcopy(state)
    if args.source_id:
        collection, source = find_object(state, args.source_id)
        if collection != "sources" or source is None:
            raise LegalCaseError("SOURCE_NOT_FOUND", f"未找到来源：{args.source_id}")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", args.new_hash or ""):
            raise LegalCaseError("INVALID_SHA256", "--new-hash 必须是64位十六进制SHA256。")
        if source["sha256"].casefold() == args.new_hash.casefold():
            raise LegalCaseError("SOURCE_HASH_UNCHANGED", "新哈希与当前哈希相同，无需失效传播。")
        source["sha256"] = args.new_hash.casefold()
        source["version"] = int(source.get("version", 1)) + 1
        seed = args.source_id
    else:
        seed = args.object_id
        collection, item = find_object(state, seed)
        if item is None:
            raise LegalCaseError("OBJECT_NOT_FOUND", f"未找到对象：{seed}")
        if collection == "artifacts":
            item.update({"stale": True, "stale_reason": args.reason, "status": "stale"})
        elif collection in {"evidence", "issues", "decisions", "package_manifests"}:
            item["status"] = "stale"
            if collection == "evidence":
                item["current_submission"] = False
            if collection == "package_manifests":
                item["stale"] = True
        elif collection == "approvals":
            item["status"] = "stale"
            item["invalidation_reason"] = args.reason
        elif collection == "templates":
            # A template version is immutable once it has been used.  Marking it
            # stale therefore retires that version instead of leaving an
            # apparently active template in the case snapshot.  The explicit
            # stale fields carry the propagation reason for CompositionSpec and
            # artifact invalidation while ``expired`` remains a schema-valid
            # template lifecycle status.
            item["status"] = "expired"
            item["stale"] = True
            item["stale_reason"] = args.reason
        else:
            item["stale"] = True
            item["stale_reason"] = args.reason
    affected = _mark_stale(state, {seed}, args.reason)
    event = mutate_state(
        state_path,
        state,
        actor=args.actor,
        command="invalidate",
        event_type="staleness_propagated",
        object_ids=sorted(affected),
        details={"seed_id": seed, "reason": args.reason, "new_hash": args.new_hash},
        old_state=old_state,
    )
    return {
        "ok": True,
        "seed_id": seed,
        "affected_ids": sorted(affected),
        "state_hash": state_content_hash(load_json(state_path)),
        "audit_event_hash": event["event_hash"],
    }


def command_preflight(args: argparse.Namespace) -> dict[str, Any]:
    return preflight(Path(args.path).resolve())


def _approved_sanitization_in_state(
    state: dict[str, Any],
    approval: dict[str, Any],
    source_hash: str,
    approval_file_hash: str,
) -> None:
    approval_id = approval.get("approval_id")
    records = [item for item in state.get("approvals", []) if item.get("id") == approval_id]
    if len(records) != 1:
        raise LegalCaseError("SANITIZATION_APPROVAL_NOT_IN_STATE", "--state 存在时，批准清单必须对应状态中的唯一批准记录。")
    record = records[0]
    if record.get("gate") != "SANITIZE" or record.get("decision") != "approved" or record.get("status") != "active":
        raise LegalCaseError("SANITIZATION_APPROVAL_INACTIVE", "状态中的清洁批准必须是有效 SANITIZE 批准。")
    if record.get("stage") != state.get("matter", {}).get("stage"):
        raise LegalCaseError("SANITIZATION_STAGE_MISMATCH", "SANITIZE批准与案件当前阶段不一致。")
    actual_scope_hash = sha256_bytes(canonical_json(record.get("scope_snapshot", [])).encode("utf-8"))
    if record.get("scope_hash") != actual_scope_hash:
        raise LegalCaseError("APPROVAL_SCOPE_HASH_MISMATCH", "SANITIZE批准范围哈希不一致，批准范围可能被改写。")
    if record.get("object_id") != approval.get("source_artifact_id"):
        raise LegalCaseError("SANITIZATION_SOURCE_OBJECT_MISMATCH", "SANITIZE批准对象不是批准文件指定的源文书。")
    if not _hash_equal(record.get("object_hash"), source_hash):
        raise LegalCaseError("SANITIZATION_SOURCE_CHANGED", "清洁批准绑定的源哈希与当前文件不一致。")
    approval_artifact_id = approval.get("approval_artifact_id")
    review_id = approval.get("independent_review_id")
    if not approval_artifact_id or not review_id:
        raise LegalCaseError("SANITIZATION_SCOPE_IDS_MISSING", "有状态清洁必须提供 approval_artifact_id 和 independent_review_id。")
    required_scope = {
        approval.get("source_artifact_id"): source_hash,
        approval_artifact_id: approval_file_hash,
    }
    review_collection, review = find_object(state, review_id)
    if review_collection != "artifacts" or review is None or review.get("review_status") != "passed" or review.get("status") not in {"reviewed", "approved"}:
        raise LegalCaseError("SANITIZATION_REVIEW_NOT_CURRENT", "独立审阅成果不存在、未通过或未处于已审阅状态。")
    required_scope[review_id] = review.get("sha256")
    approval_collection, approval_artifact = find_object(state, approval_artifact_id)
    if approval_collection != "artifacts" or approval_artifact is None or approval_artifact.get("sha256") != approval_file_hash:
        raise LegalCaseError("SANITIZATION_APPROVAL_ARTIFACT_CHANGED", "批准清单文件未登记为当前Artifact或哈希已变化。")
    scope_by_id = {item.get("object_id"): item for item in record.get("scope_snapshot", [])}
    for object_id, digest in required_scope.items():
        snapshot = scope_by_id.get(object_id)
        _collection, current = find_object(state, object_id)
        if snapshot is None or current is None:
            raise LegalCaseError("SANITIZATION_SCOPE_INCOMPLETE", f"SANITIZE scope_snapshot 未绑定：{object_id}")
        version, current_hash = object_version_hash(current)
        if snapshot.get("version") != version or not _hash_equal(snapshot.get("hash"), digest) or not _hash_equal(current_hash, digest):
            raise LegalCaseError("SANITIZATION_SCOPE_CHANGED", f"SANITIZE scope_snapshot 对象已变化：{object_id}")


def command_sanitize_docx(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    report_path = Path(args.report).resolve() if args.report else output.with_suffix(".sanitization-report.json")
    approval = load_json(Path(args.approved_items).resolve())
    approval_file_hash = sha256_file(Path(args.approved_items).resolve())
    state_path = Path(args.state).resolve() if args.state else None
    state = load_json(state_path) if state_path else None
    old_state = copy.deepcopy(state) if state else None
    source_hash = sha256_file(source)
    if state is not None:
        semantic_errors = validate_semantics(state, state_path)
        if semantic_errors:
            raise LegalCaseError(
                "CASE_STATE_INVALID",
                "有状态清洁前必须通过案件状态与追加式批准审计校验：" + "；".join(semantic_errors),
            )
        _approved_sanitization_in_state(state, approval, source_hash, approval_file_hash)
    else:
        # A state-less path exists only for explicitly synthetic regression
        # fixtures. Real/self-generated matter documents must pass through the
        # canonical case-state SANITIZE approval and its three-object scope.
        source_text = extract_docx_text(source)
        if approval.get("test_only") is not True or "TEST-ONLY" not in source_text:
            raise LegalCaseError(
                "SANITIZATION_STATE_REQUIRED",
                "非 TEST-ONLY 清洁必须提供 --state，并由有效 SANITIZE 批准精确绑定源文书、独立审阅成果和批准清单。",
            )
    report = sanitize_docx(source, output, approval, report_path)
    event_hash = None
    if state is not None and state_path is not None:
        source_artifact = next((item for item in state.get("artifacts", []) if item.get("id") == report["source_artifact_id"]), None)
        source_version = int(source_artifact.get("version", 1)) if source_artifact else 1
        audience = source_artifact.get("audience", "internal_review") if source_artifact else "internal_review"
        workspace = state_path.parent.parent
        state_report = {
            key: report[key]
            for key in (
                "id",
                "source_artifact_id",
                "source_sha256",
                "derived_artifact_id",
                "derived_sha256",
                "approved_item_ids",
                "items",
                "blocked_items",
                "rereview_status",
                "created_at",
            )
        }
        state["sanitization_reports"].append(state_report)
        state["artifacts"].append({
            "id": report["derived_artifact_id"],
            "kind": "sanitized_docx",
            "audience": audience,
            "version": 1,
            "path": _relative_or_absolute(output, workspace),
            "sha256": report["derived_sha256"],
            "status": "draft",
            "review_status": "needs_review",
            "input_snapshot": [{"object_id": report["source_artifact_id"], "version": source_version, "hash": report["source_sha256"]}],
            "stale": False,
            "stale_reason": None,
            "created_at": now_iso(),
        })
        event = mutate_state(
            state_path,
            state,
            actor=approval["approved_by"],
            command="sanitize-docx",
            event_type="sanitized_derivative_created",
            object_ids=[report["source_artifact_id"], report["derived_artifact_id"], report["id"]],
            details={"approval_id": approval["approval_id"], "blocked_items": report["blocked_items"]},
            old_state=old_state,
        )
        event_hash = event["event_hash"]
    return {
        "ok": True,
        "source": str(source),
        "output": str(output),
        "report": str(report_path),
        "source_sha256": report["source_sha256"],
        "derived_sha256": report["derived_sha256"],
        "original_hash_unchanged": report["original_hash_unchanged"],
        "blocked_items": report["blocked_items"],
        "rereview_status": report["rereview_status"],
        "audit_event_hash": event_hash,
    }


def _parse_ranges(values: list[str]) -> list[tuple[int, int]]:
    ranges = []
    for value in values:
        match = re.fullmatch(r"(\d+):(\d+)", value)
        if not match or int(match.group(1)) < 1 or int(match.group(2)) < int(match.group(1)):
            raise LegalCaseError("INVALID_LINE_RANGE", f"行范围必须为 START:END 且从1开始：{value}")
        ranges.append((int(match.group(1)), int(match.group(2))))
    return ranges


def command_min_diff(args: argparse.Namespace) -> dict[str, Any]:
    if not 0 <= args.max_change_ratio <= 1:
        raise LegalCaseError("INVALID_CHANGE_RATIO", "max-change-ratio 必须在0到1之间。")
    return minimal_diff(
        Path(args.original).resolve(),
        Path(args.modified).resolve(),
        _parse_ranges(args.allowed_line),
        args.max_change_ratio,
    )


def command_composition_build(args: argparse.Namespace) -> dict[str, Any]:
    payload = _load_json_argument(args.input, "--input", expected=dict)
    if args.test_mode:
        if args.state or args.catalog:
            raise LegalCaseError(
                "TEST_MODE_MUST_BE_ISOLATED",
                "--test-mode 合成不得绑定案件状态或生产个人模板目录。",
            )
        if payload.get("test_only") is not True:
            raise LegalCaseError(
                "TEST_ONLY_MARKER_REQUIRED",
                "--test-mode 输入必须显式标记 test_only=true。",
            )
        payload = copy.deepcopy(payload)
        payload["test_mode"] = True
        spec = build_composition_spec(payload)
        template_snapshots: list[dict[str, Any]] = []
        state = None
        state_path = None
    else:
        if not args.state:
            raise LegalCaseError(
                "COMPOSITION_STATE_REQUIRED",
                "正式 composition-build 必须提供 --state，以从当前案件重建G1/G2批准快照。",
            )
        state_path = Path(args.state).resolve()
        state = load_json(state_path)
        _require_v12(state)
        _require_expected_state_version(state, args.expected_state_version)
        _validate_candidate_state(state)
        supplied_matter = payload.get("matter_id")
        if supplied_matter is not None and supplied_matter != state["matter"]["id"]:
            raise LegalCaseError("COMPOSITION_MATTER_MISMATCH", "CompositionSpec 绑定了其他案件。")
        catalog_path = Path(args.catalog or DEFAULT_PERSONAL_TEMPLATE_CATALOG).resolve()
        spec, template_snapshots = build_trusted_composition_spec(payload, state, catalog_path)
    output_path = Path(args.output).resolve() if args.output else None
    if output_path is not None:
        _write_new_json(output_path, spec)
    event_hash = None
    if state is not None and state_path is not None:
        old_state = copy.deepcopy(state)
        if any(item.get("id") == spec.get("id") for item in state.get("composition_specs", [])):
            raise LegalCaseError("COMPOSITION_SPEC_EXISTS", f"案件中已存在合成清单：{spec.get('id')}")
        synchronize_trusted_templates(state, template_snapshots)
        state["composition_specs"].append(copy.deepcopy(spec))
        state["focus"]["current_composition_spec_id"] = spec["id"]
        _validate_candidate_state(state)
        event = mutate_state(
            state_path,
            state,
            actor=args.actor,
            command="composition-build",
            event_type="composition_spec_created",
            object_ids=[spec["id"]],
            details={"status": spec["status"], "output": str(output_path) if output_path else None},
            old_state=old_state,
        )
        event_hash = event["event_hash"]
    return {
        "ok": True,
        "spec": spec,
        "output": str(output_path) if output_path else None,
        "audit_event_hash": event_hash,
        "external_actions_executed": 0,
    }


def command_composition_validate(args: argparse.Namespace) -> dict[str, Any]:
    spec = _load_json_argument(args.spec, "--spec", expected=dict)
    authorities = _load_json_argument(args.authorities, "--authorities", expected=(dict, list), default=[])
    if isinstance(authorities, dict):
        authorities = authorities.get("authorities", authorities.get("items", []))
    if not isinstance(authorities, list):
        raise LegalCaseError("AUTHORITIES_NOT_ARRAY", "法源目录必须是数组或含 authorities 数组的对象。")
    state: dict[str, Any] | None = None
    state_path: Path | None = None
    trust_validation = {"ok": True, "errors": []}
    if args.test_mode:
        if args.state or args.catalog:
            raise LegalCaseError(
                "TEST_MODE_MUST_BE_ISOLATED",
                "--test-mode 校验不得绑定案件状态或生产个人模板目录。",
            )
        if spec.get("test_mode") is not True or spec.get("trusted_binding", {}).get("mode") != "test_only_fixture":
            raise LegalCaseError("TEST_ONLY_SPEC_REQUIRED", "--test-mode 只能校验隔离的 TEST-ONLY 合成清单。")
    else:
        if not args.state:
            raise LegalCaseError(
                "COMPOSITION_STATE_REQUIRED",
                "正式 composition-validate 必须提供 --state 与当前个人模板目录。",
            )
        state_path = Path(args.state).resolve()
        state = load_json(state_path)
        _require_v12(state)
        _require_expected_state_version(state, args.expected_state_version)
        _validate_candidate_state(state)
        catalog_path = Path(args.catalog or DEFAULT_PERSONAL_TEMPLATE_CATALOG).resolve()
        trust_validation = verify_composition_trust_binding(spec, state, catalog_path)
    validation = validate_composition_spec(spec, authorities, production=not args.test_mode)
    claim_map_validation: dict[str, Any] | None = None
    if not args.test_mode and spec.get("audience") == "court_candidate":
        if not args.document or not args.claim_map:
            validation["errors"].append({
                "code": "artifact_claim_map_required",
                "message": "法院候选文书必须提供 --document 与 --claim-map，完成逐段ClaimBinding覆盖。",
                "path": "$.artifact_claim_map",
            })
            validation["ok"] = False
            validation["status"] = "blocked"
        else:
            claim_map = _load_json_argument(args.claim_map, "--claim-map", expected=dict)
            claim_map_validation = validate_artifact_claim_map(
                Path(args.document).resolve(), claim_map, spec, state=state
            )
            if not claim_map_validation["ok"]:
                validation["errors"].extend({
                    "code": item.get("code", "artifact_claim_map_invalid"),
                    "message": "ArtifactClaimMap与最终文书或ClaimBinding不一致。",
                    "path": "$.artifact_claim_map",
                } for item in claim_map_validation["findings"])
                validation["ok"] = False
                validation["status"] = "blocked"
    if not trust_validation["ok"]:
        validation["errors"].extend(trust_validation["errors"])
        validation["ok"] = False
        validation["status"] = "blocked"
    validated_spec = copy.deepcopy(spec)
    validated_spec["status"] = (
        "validated" if validation["ok"] and args.test_mode
        else "ready" if validation["ok"]
        else "blocked"
    )
    validated_spec["validated_at"] = now_iso()
    validated_spec["blockers"] = list(dict.fromkeys(
        [] if validation["ok"] else [item.get("code", "validation_error") for item in validation["errors"]]
    ))
    output_path = Path(args.output).resolve() if args.output else None
    if output_path is not None:
        _write_new_json(output_path, validated_spec)
    event_hash = None
    if state is not None and state_path is not None:
        old_state = copy.deepcopy(state)
        matches = [item for item in state.get("composition_specs", []) if item.get("id") == validated_spec.get("id")]
        if len(matches) != 1:
            raise LegalCaseError("COMPOSITION_SPEC_NOT_UNIQUE", "案件状态中必须存在唯一同ID合成清单。")
        if canonical_json(matches[0]) != canonical_json(spec):
            raise LegalCaseError("COMPOSITION_SPEC_STATE_MISMATCH", "待校验清单与案件当前版本不一致。")
        index = state["composition_specs"].index(matches[0])
        state["composition_specs"][index] = validated_spec
        _validate_candidate_state(state)
        event = mutate_state(
            state_path,
            state,
            actor=args.actor,
            command="composition-validate",
            event_type="composition_spec_validated" if validation["ok"] else "composition_spec_blocked",
            object_ids=[validated_spec["id"]],
            details={"error_codes": validated_spec["blockers"], "production": not args.test_mode},
            old_state=old_state,
        )
        event_hash = event["event_hash"]
    return {
        "ok": validation["ok"],
        "validation": validation,
        "claim_map_validation": claim_map_validation,
        "spec": validated_spec,
        "output": str(output_path) if output_path else None,
        "audit_event_hash": event_hash,
        "external_actions_executed": 0,
    }


def command_citation_audit(args: argparse.Namespace) -> dict[str, Any]:
    authorities = _load_json_argument(args.authorities, "--authorities", expected=(dict, list))
    result = audit_citations(Path(args.document).resolve(), authorities)
    if bool(args.claim_map) != bool(args.composition_spec):
        raise LegalCaseError("CLAIM_MAP_ARGUMENTS_INCOMPLETE", "--claim-map 与 --composition-spec 必须同时提供。")
    if args.claim_map and args.composition_spec:
        claim_map = _load_json_argument(args.claim_map, "--claim-map", expected=dict)
        spec = _load_json_argument(args.composition_spec, "--composition-spec", expected=dict)
        claim_result = validate_artifact_claim_map(Path(args.document).resolve(), claim_map, spec)
        result["claim_map_validation"] = claim_result
        result["ok"] = result["ok"] and claim_result["ok"]
    result["external_actions_executed"] = 0
    return result


def command_exemplar_leak_check(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _load_json_argument(args.fingerprints, "--fingerprints", expected=(dict, list))
    result = check_exemplar_leaks(Path(args.document).resolve(), manifest)
    result["external_actions_executed"] = 0
    return result


def _bind_fill_provenance_to_state(
    provenance: dict[str, Any],
    state_path: Path,
    profile_path: Path,
    data: dict[str, Any],
    template_catalog_binding: dict[str, Any],
) -> dict[str, Any]:
    """Bind every proposed value to an exact fact or lawyer decision.

    Caller-supplied ``verified=true`` flags are never trusted.  The case state
    must contain either a confirmed direct-record Fact for the exact
    template/profile/slot/action/value binding, or an exact lawyer Decision
    with a current human Approval receipt.
    """
    _workspace_from_state_path(state_path)
    state = load_json(state_path)
    _require_v12(state)
    validation_errors = validate_against_schema(state, load_json(DEFAULT_SCHEMA)) + validate_semantics(state, state_path)
    if validation_errors:
        raise LegalCaseError(
            "FILL_PROVENANCE_STATE_INVALID",
            "逐格拟填来源绑定前，案件状态必须通过完整校验。",
            {"errors": validation_errors},
        )
    bound = copy.deepcopy(provenance)
    preliminary_plan = build_fill_plan(profile_path, data, bound)
    slot_records = preliminary_plan.get("slots", [])

    def bound_slot(key: str) -> dict[str, Any]:
        matches = [
            item for item in slot_records
            if item.get("slot_id") == key or item.get("field_key") == key
        ]
        unique = {str(item.get("slot_id")): item for item in matches}
        if len(unique) != 1:
            raise LegalCaseError(
                "FILL_PROVENANCE_SLOT_NOT_UNIQUE",
                f"逐格来源必须唯一对应一个已登记槽位：{key}",
                {"matched_slot_ids": sorted(unique)},
            )
        return next(iter(unique.values()))

    source_by_id = {item.get("id"): item for item in state.get("sources", [])}
    fact_by_id = {item.get("id"): item for item in state.get("facts", [])}
    decision_by_id = {
        str(item.get("id") or item.get("decision_id")): item
        for item in state.get("decisions", [])
        if item.get("id") or item.get("decision_id")
    }
    active_approvals = [
        item for item in state.get("approvals", [])
        if item.get("status") == "active" and item.get("decision") == "approved" and item.get("actor")
    ]
    used_sources: dict[str, dict[str, Any]] = {}
    used_facts: dict[str, dict[str, Any]] = {}
    used_decisions: dict[str, dict[str, Any]] = {}
    detected_conflicts: list[dict[str, Any]] = copy.deepcopy(
        bound.get("_conflicts", []) if isinstance(bound.get("_conflicts"), list) else []
    )
    for field_key, meta in list(bound.items()):
        if field_key.startswith("_") or not isinstance(meta, dict):
            continue
        plan_slot = bound_slot(field_key)
        binding = copy.deepcopy(plan_slot.get("binding"))
        if not isinstance(binding, dict) or binding.get("binding_sha256") is None:
            raise LegalCaseError("FILL_BINDING_MISSING", f"槽位缺少拟填绑定：{field_key}")
        refs = meta.get("source_refs", []) if isinstance(meta.get("source_refs"), list) else []
        observed_value_hashes: dict[str, list[str]] = {}
        for ref in refs:
            source_id = str(ref.get("id") or "")
            source = source_by_id.get(source_id)
            if source is None:
                raise LegalCaseError("FILL_SOURCE_NOT_IN_STATE", f"拟填字段引用了案件中不存在的来源：{source_id}")
            fact_id = str(ref.get("fact_id") or "")
            locator = str(ref.get("locator") or "").strip()
            fact = fact_by_id.get(fact_id)
            if fact is None or not locator:
                raise LegalCaseError(
                    "FILL_DIRECT_FACT_REQUIRED",
                    f"拟填字段必须引用案件中的精确事实记录和页码/行号：{field_key}",
                )
            exact_locator = next(
                (
                    item for item in fact.get("source_locators", [])
                    if item.get("source_id") == source_id
                    and item.get("locator") == locator
                    and item.get("record_kind") == "direct_record"
                    and item.get("verified") is True
                ),
                None,
            )
            if (
                fact.get("status") != "confirmed"
                or fact.get("stale") is True
                or exact_locator is None
                or fact.get("field_key") != binding.get("field_key")
            ):
                raise LegalCaseError(
                    "FILL_DIRECT_FACT_BINDING_MISMATCH",
                    f"材料事实没有精确绑定当前格子、动作和值：{field_key}",
                    {"fact_id": fact_id, "slot_id": plan_slot.get("slot_id")},
                )
            observed_hash = str(fact.get("normalized_value_sha256") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", observed_hash):
                raise LegalCaseError(
                    "FILL_DIRECT_FACT_VALUE_HASH_MISSING",
                    f"材料事实未登记提取值哈希：{fact_id}",
                )
            observed_value_hashes.setdefault(observed_hash.casefold(), []).append(fact_id)
            object_hash = sha256_bytes(canonical_json(source).encode("utf-8"))
            fact_hash = sha256_bytes(canonical_json(fact).encode("utf-8"))
            record = copy.deepcopy(source)
            record.update({
                "kind": "original_material" if source.get("original") and source.get("read_only") else "user_statement",
                "verified": bool(source.get("original") and source.get("read_only")),
                "object_sha256": object_hash,
            })
            used_sources[source_id] = record
            fact_record = copy.deepcopy(fact)
            fact_record["object_sha256"] = fact_hash
            used_facts[fact_id] = fact_record
            ref.update({
                "kind": record["kind"],
                "verified": record["verified"],
                "sha256": source.get("sha256"),
                "object_sha256": object_hash,
                "fact_id": fact_id,
                "fact_object_sha256": fact_hash,
                "locator": locator,
                "extracted_value_sha256": observed_hash.casefold(),
                "binding": copy.deepcopy(binding),
            })
        if refs and (
            len(observed_value_hashes) > 1
            or set(observed_value_hashes) != {str(binding.get("value_sha256") or "").casefold()}
        ):
            detected_conflicts.append({
                "id": f"SOURCE-CONFLICT-{plan_slot.get('slot_id')}",
                "slot_ids": [plan_slot.get("slot_id")],
                "status": "unresolved",
                "reason": "direct_sources_disagree_with_each_other_or_proposed_value",
                "candidate_value_hashes": [
                    {"value_sha256": digest, "fact_ids": sorted(fact_ids)}
                    for digest, fact_ids in sorted(observed_value_hashes.items())
                ],
            })
        confirmation = meta.get("confirmation")
        if isinstance(confirmation, dict) and confirmation.get("decision_id"):
            decision_id = str(confirmation["decision_id"])
            decision = decision_by_id.get(decision_id)
            if decision is None:
                raise LegalCaseError("FILL_DECISION_NOT_IN_STATE", f"拟填字段引用了案件中不存在的律师决定：{decision_id}")
            approval_matches = [
                item for item in active_approvals
                if item.get("object_id") == decision_id
                and item.get("gate") in {"G1_strategy", "G3_draft_plan"}
            ]
            exact_approval = approval_matches[0] if len(approval_matches) == 1 else None
            actor = str(exact_approval.get("actor") if exact_approval else "")
            is_human = bool(actor) and not re.search(r"(?i)(?:^|[-_])(ai|assistant|agent|model|system)(?:$|[-_])", actor)
            if decision.get("status") != "approved" or exact_approval is None or not is_human:
                raise LegalCaseError(
                    "FILL_LAWYER_DECISION_NOT_APPROVED",
                    f"高风险拟填决定没有绑定有效人工批准：{decision_id}",
                    {"matching_approval_count": len(approval_matches)},
                )
            if exact_approval.get("gate") not in {"G1_strategy", "G3_draft_plan"}:
                raise LegalCaseError(
                    "FILL_LAWYER_DECISION_GATE_INVALID",
                    f"高风险拟填决定必须由 G1 或 G3 精确批准：{decision_id}",
                )
            if canonical_json(decision.get("fill_binding")) != canonical_json(binding):
                raise LegalCaseError(
                    "FILL_LAWYER_DECISION_BINDING_MISMATCH",
                    f"律师决定没有精确绑定当前格子、动作和值：{field_key}",
                    {"decision_id": decision_id, "slot_id": plan_slot.get("slot_id")},
                )
            object_hash = sha256_bytes(canonical_json(decision).encode("utf-8"))
            record = copy.deepcopy(decision)
            record.update({
                "decision_id": decision_id,
                "status": "approved",
                "actor_role": "lawyer",
                "object_sha256": object_hash,
                "approval_id": exact_approval.get("id"),
                "fill_binding": copy.deepcopy(binding),
            })
            used_decisions[decision_id] = record
            confirmation.update({
                "status": "approved",
                "actor_role": "lawyer",
                "object_sha256": object_hash,
                "approval_id": exact_approval.get("id"),
                "fill_binding": copy.deepcopy(binding),
            })
    snapshot = {
        "case_state_path": str(state_path),
        "case_state_sha256": sha256_file(state_path),
        "state_version": _state_version(state),
        "template_catalog": copy.deepcopy(template_catalog_binding),
        "sources": list(used_sources.values()),
        "facts": list(used_facts.values()),
        "decisions": list(used_decisions.values()),
    }
    snapshot["snapshot_sha256"] = sha256_bytes(canonical_json(snapshot).encode("utf-8"))
    bound["_registry_snapshot"] = snapshot
    bound["_conflicts"] = list({
        str(item.get("id")): item for item in detected_conflicts if item.get("id")
    }.values())
    return bound


def _active_personal_fill_binding(
    catalog_path: Path,
    profile_path: Path,
    source_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve a form profile from the live active personal catalog.

    A self-declared ``status=active`` sidecar is not a trust root.  This check
    prevents pending drafts, fabricated profiles, and retired profiles from
    being used through direct ``--profile`` paths.
    """

    profile = load_json(profile_path)
    template_id = str(profile.get("template_id") or "")
    if not template_id:
        raise LegalCaseError("FILL_PROFILE_TEMPLATE_ID_MISSING", "FormProfile 缺少 template_id。")
    resolved = resolve_personal_template_reference(template_id, catalog_path)
    if resolved.get("kind") != "template":
        raise LegalCaseError("FILL_PERSONAL_TEMPLATE_REQUIRED", "逐格填充必须绑定个人模板目录中的单一 active 模板。")
    resolved_profile = Path(resolved["profile_path"]).resolve()
    resolved_source = Path(resolved["path"]).resolve()
    if resolved_profile != profile_path.resolve():
        raise LegalCaseError(
            "FILL_PROFILE_CATALOG_MISMATCH",
            "--profile 不是个人模板目录当前 active 条目登记的画像。",
        )
    if source_path is not None and resolved_source != source_path.resolve():
        raise LegalCaseError(
            "FILL_SOURCE_CATALOG_MISMATCH",
            "--source 不是个人模板目录当前 active 条目登记的模板副本。",
        )
    entry = resolved["entry"]
    if (
        entry.get("profile_sha256") != sha256_file(resolved_profile)
        or entry.get("sha256") != sha256_file(resolved_source)
        or profile.get("source", {}).get("sha256") != entry.get("sha256")
    ):
        raise LegalCaseError(
            "FILL_ACTIVE_TEMPLATE_HASH_MISMATCH",
            "个人模板目录、画像或active模板副本哈希不一致。",
        )
    binding = {
        "catalog_path": str(catalog_path.resolve()),
        "catalog_sha256": sha256_file(catalog_path),
        "template_id": template_id,
        "entry_sha256": sha256_bytes(canonical_json(entry).encode("utf-8")),
        "template_version": entry.get("version"),
        "source_path": str(resolved_source),
        "source_sha256": entry.get("sha256"),
        "profile_path": str(resolved_profile),
        "profile_sha256": entry.get("profile_sha256"),
    }
    binding["binding_sha256"] = sha256_bytes(canonical_json(binding).encode("utf-8"))
    return binding


def command_template_distill(args: argparse.Namespace) -> dict[str, Any]:
    overrides = _load_json_argument(args.overrides, "--overrides", expected=dict, default={})
    result = distill_template(
        Path(args.source).resolve(),
        Path(args.output_dir).resolve(),
        args.profile_kind,
        args.usage_mode,
        args.template_id,
        args.name,
        args.document_type,
        overrides=overrides,
    )
    result.update({
        "activation_performed": False,
        "next_gate": "lawyer_profile_approval",
        "external_actions_executed": 0,
    })
    return result


def command_template_register(args: argparse.Namespace) -> dict[str, Any]:
    approval = _load_json_argument(args.approval, "--approval", expected=dict)
    result = register_template(
        Path(args.source).resolve() if args.source else None,
        Path(args.profile).resolve() if args.profile else None,
        Path(args.catalog).resolve() if args.catalog else DEFAULT_PERSONAL_TEMPLATE_CATALOG,
        Path(args.destination_dir).resolve() if args.destination_dir else None,
        operation=args.operation,
        approval=approval,
        template_id=args.template_id,
        version=args.version,
        category=args.category,
        aliases=args.alias,
    )
    result["external_actions_executed"] = 0
    return result


def command_template_fill_plan(args: argparse.Namespace) -> dict[str, Any]:
    data = _load_json_argument(args.data, "--data", expected=dict)
    provenance = _load_json_argument(args.provenance, "--provenance", expected=dict)
    state_path = Path(args.state).resolve()
    _require_expected_state_version(load_json(state_path), args.expected_state_version)
    profile_path = Path(args.profile).resolve()
    catalog_path = Path(args.catalog).resolve() if args.catalog else DEFAULT_PERSONAL_TEMPLATE_CATALOG
    catalog_binding = _active_personal_fill_binding(catalog_path, profile_path)
    bound_provenance = _bind_fill_provenance_to_state(
        provenance,
        state_path,
        profile_path,
        data,
        catalog_binding,
    )
    plan = build_fill_plan(
        profile_path,
        data,
        bound_provenance,
        Path(args.output).resolve() if args.output else None,
    )
    validation = validate_fill_plan(plan, profile_path)
    return {
        "ok": validation["ok"],
        "plan": plan,
        "validation": validation,
        "output": str(Path(args.output).resolve()) if args.output else None,
        "confirmation_batch": validation.get("confirmation_batch", []),
        "conflict_batch": validation.get("conflict_batch", []),
        "consolidated_review_batch": validation.get("consolidated_review_batch", []),
        "external_actions_executed": 0,
    }


def command_template_fill_docx(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    _workspace_from_state_path(state_path)
    current_state_hash = sha256_file(state_path)
    plan = _load_json_argument(args.plan, "--plan", expected=dict)
    snapshot = plan.get("provenance_snapshot") or {}
    if snapshot.get("case_state_path") != str(state_path) or snapshot.get("case_state_sha256") != current_state_hash:
        raise LegalCaseError(
            "FILL_PLAN_STATE_STALE",
            "FillPlan 绑定的案件状态不是当前版本，必须重新生成拟填表。",
        )
    catalog_path = Path(args.catalog).resolve() if args.catalog else DEFAULT_PERSONAL_TEMPLATE_CATALOG
    current_catalog_binding = _active_personal_fill_binding(
        catalog_path,
        Path(args.profile).resolve(),
        Path(args.source).resolve(),
    )
    if canonical_json(snapshot.get("template_catalog")) != canonical_json(current_catalog_binding):
        raise LegalCaseError(
            "FILL_PLAN_TEMPLATE_CATALOG_STALE",
            "FillPlan 绑定的模板目录、版本、画像或源副本已变化，必须重新生成拟填表。",
        )
    result = fill_docx_from_plan(
        Path(args.source).resolve(),
        Path(args.output).resolve(),
        Path(args.profile).resolve(),
        plan,
    )
    result.update({
        "court_candidate": False,
        "filing_eligible": False,
        "next_gate": "page_by_page_visual_qa",
        "external_actions_executed": 0,
    })
    return result


def command_template_repeat_plan(args: argparse.Namespace) -> dict[str, Any]:
    if not args.test_mode:
        raise LegalCaseError(
            "REPEATABLE_TEST_MODE_REQUIRED",
            "标准行结构引擎当前仅开放 TEST-ONLY 合成夹具；真实模板须等待人工校准。",
        )
    groups = _load_json_argument(args.groups, "--groups", expected=list)
    plan = build_repeatable_plan(
        Path(args.profile).resolve(),
        groups,
        Path(args.output).resolve() if args.output else None,
    )
    return {
        "ok": True, "test_only": True, "plan": plan,
        "output": str(Path(args.output).resolve()) if args.output else None,
        "court_candidate": False, "filing_eligible": False, "external_actions_executed": 0,
    }


def command_template_hybrid_plan(args: argparse.Namespace) -> dict[str, Any]:
    if not args.test_mode:
        raise LegalCaseError(
            "HYBRID_TEST_MODE_REQUIRED",
            "正文结构引擎当前仅开放 TEST-ONLY 合成夹具；真实模板须等待人工校准。",
        )
    mechanical = _load_json_argument(args.mechanical, "--mechanical", expected=dict)
    bodies = _load_json_argument(args.bodies, "--bodies", expected=list)
    plan = build_hybrid_plan(
        Path(args.profile).resolve(),
        mechanical,
        bodies,
        Path(args.output).resolve() if args.output else None,
    )
    return {
        "ok": True, "test_only": True, "plan": plan,
        "output": str(Path(args.output).resolve()) if args.output else None,
        "court_candidate": False, "filing_eligible": False, "external_actions_executed": 0,
    }


def command_template_structural_apply(args: argparse.Namespace) -> dict[str, Any]:
    if not args.test_mode:
        raise LegalCaseError(
            "STRUCTURAL_TEST_MODE_REQUIRED",
            "结构变换当前仅开放 TEST-ONLY 合成夹具；不会处理真实模板。",
        )
    result = apply_structural_docx_plan(
        Path(args.source).resolve(), Path(args.output).resolve(),
        Path(args.profile).resolve(), _load_json_argument(args.plan, "--plan", expected=dict),
    )
    result["external_actions_executed"] = 0
    return result


def command_template_fill(args: argparse.Namespace) -> dict[str, Any]:
    registry_path = Path(args.registry).resolve() if args.registry else DEFAULT_REGISTRY
    raw_data = args.data.strip()
    if raw_data.startswith("{"):
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise LegalCaseError(
                "INVALID_TEMPLATE_DATA_JSON",
                "--data 内联JSON无法解析。",
                {"line": exc.lineno, "column": exc.colno, "detail": exc.msg},
            ) from exc
    else:
        data = load_json(Path(args.data).resolve())
    if not isinstance(data, dict):
        raise LegalCaseError("TEMPLATE_DATA_NOT_OBJECT", "模板数据必须是 JSON 对象。")
    data = copy.deepcopy(data)
    input_metadata = {
        key: data.pop(key)
        for key in ("test_only", "notice")
        if key in data
    }
    state_path: Path | None = None
    state: dict[str, Any] | None = None
    old_state: dict[str, Any] | None = None
    workspace: Path | None = None
    if args.state:
        state_path = Path(args.state).resolve()
        workspace = _workspace_from_state_path(state_path)
        state = load_json(state_path)
        _require_v12(state)
        _require_expected_state_version(state, getattr(args, "expected_state_version", None))
        _validate_candidate_state(state)
        old_state = copy.deepcopy(state)
    output = Path(args.output).resolve()
    result = fill_template(args.template_id, data, output, registry_path)
    output_stat = output.stat()
    output_identity = (
        output_stat.st_dev,
        output_stat.st_ino,
        output_stat.st_size,
        output_stat.st_mtime_ns,
    )
    result["input_metadata"] = input_metadata
    event_hash = None
    artifact_id: str | None = None
    try:
        if state is not None and state_path is not None and old_state is not None and workspace is not None:
            entry, _source = resolve_template(args.template_id, registry_path)
            if not any(item.get("id") == entry["id"] for item in state.get("templates", [])):
                state["templates"].append(copy.deepcopy(entry))
            artifact_id = f"R-{uuid.uuid4().hex[:16]}"
            state["artifacts"].append({
                "id": artifact_id,
                "kind": entry["document_type"],
                "audience": "internal_review",
                "version": 1,
                "path": _relative_or_absolute(output, workspace),
                "sha256": result["output_sha256"],
                "status": "draft",
                "review_status": "needs_review",
                "input_snapshot": [{"object_id": entry["id"], "version": 1, "hash": entry["sha256"]}],
                "stale": False,
                "stale_reason": None,
                "created_at": now_iso(),
            })
            state["focus"]["current_artifact_id"] = artifact_id
            state["focus"]["current_template_id"] = entry["id"]
            _validate_candidate_state(state)
            event = mutate_state(
                state_path,
                state,
                actor=args.actor,
                command="template-fill",
                event_type="template_artifact_created",
                object_ids=[entry["id"], artifact_id],
                details={"output": _relative_or_absolute(output, workspace), "output_sha256": result["output_sha256"]},
                old_state=old_state,
            )
            result["artifact_id"] = artifact_id
            event_hash = event["event_hash"]
    except Exception:
        if state_path is not None:
            # A lock-local CAS can still lose after the early version check.
            # Remove only this invocation's unchanged file and only when no
            # recoverable/committed state projection references its artifact.
            state_has_artifact = True
            try:
                latest_state = load_json(state_path)
                state_has_artifact = artifact_id is not None and any(
                    item.get("id") == artifact_id
                    for item in latest_state.get("artifacts", [])
                )
            except Exception:
                # State uncertainty is safer than deleting a possibly committed file.
                state_has_artifact = True
            if not state_has_artifact and output.is_file():
                try:
                    current_stat = output.stat()
                    current_identity = (
                        current_stat.st_dev,
                        current_stat.st_ino,
                        current_stat.st_size,
                        current_stat.st_mtime_ns,
                    )
                    if (
                        current_identity == output_identity
                        and sha256_file(output) == result["output_sha256"]
                    ):
                        output.unlink()
                except OSError:
                    pass
        raise
    result["audit_event_hash"] = event_hash
    return result


def command_template_ref_validate(args: argparse.Namespace) -> dict[str, Any]:
    catalog_path = Path(args.catalog).resolve() if args.catalog else DEFAULT_PERSONAL_TEMPLATE_CATALOG
    result = validate_personal_template_catalog(catalog_path)
    result["external_actions_executed"] = 0
    return result


def command_template_ref_resolve(args: argparse.Namespace) -> dict[str, Any]:
    catalog_path = Path(args.catalog).resolve() if args.catalog else DEFAULT_PERSONAL_TEMPLATE_CATALOG
    resolved = resolve_personal_template_reference(args.template_id, catalog_path)
    entry = resolved["entry"]
    result = {
        "ok": True,
        "reference_kind": resolved["kind"],
        "id": entry["id"],
        "name": entry["name"],
        "aliases": entry.get("aliases", []),
        "version": entry.get("version"),
        "path": str(resolved["path"]) if resolved["path"] else None,
        "profile_path": str(resolved.get("profile_path")) if resolved.get("profile_path") else None,
        "components": resolved["components"],
        "load_policy": "exact_named_reference_only",
        "next_skill": "legal-style-exemplar-retrieval",
        "forbidden_transfer": entry.get("forbidden_transfer", [
            "client_facts", "names", "dates", "amounts", "claims", "evidence", "authorities", "conclusions",
        ]),
        "external_actions_executed": 0,
    }
    if resolved["kind"] == "template":
        result.update({
            "sha256": entry["sha256"],
            "document_type": entry["document_type"],
            "usage_mode": entry.get("usage_mode"),
            "profile_sha256": entry.get("profile_sha256"),
            "reusable_aspects": entry["reusable_aspects"],
        })
    return result


def command_name(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "filename": safe_filename(args.matter_id, args.kind, args.title, args.version, args.ext),
        "notice": "仅生成名称，不移动或重命名任何现有文件。",
    }


def _select_package(state: dict[str, Any], package_id: str | None) -> dict[str, Any]:
    packages = state.get("package_manifests", [])
    if package_id:
        matches = [item for item in packages if item.get("id") == package_id]
    else:
        matches = [item for item in packages if item.get("status") == "candidate" and not item.get("stale")]
    if len(matches) != 1:
        raise LegalCaseError(
            "PACKAGE_NOT_UNIQUE",
            "必须指定唯一、未失效的候选包清单。",
            {"package_count": len(matches), "package_ids": [item.get("id") for item in matches]},
        )
    return matches[0]


def _verify_sanitization_lineage(state: dict[str, Any], artifact: dict[str, Any]) -> None:
    """Require passed re-review for every sanitized ancestor of an artifact.

    A PDF conversion must not erase the clean-copy gate.  We therefore follow
    Artifact.input_snapshot recursively instead of checking only the artifact
    directly listed in the package.
    """
    artifacts = {item.get("id"): item for item in state.get("artifacts", [])}
    reports_by_derived: dict[str, list[dict[str, Any]]] = {}
    for report in state.get("sanitization_reports", []):
        reports_by_derived.setdefault(report.get("derived_artifact_id"), []).append(report)
    visited: set[str] = set()
    stack = [artifact]
    while stack:
        current = stack.pop()
        identifier = current.get("id")
        if identifier in visited:
            continue
        visited.add(identifier)
        reports = reports_by_derived.get(identifier, [])
        if current.get("kind") == "sanitized_docx" or reports:
            if len(reports) != 1:
                raise LegalCaseError("SANITIZATION_REPORT_NOT_UNIQUE", f"清洁派生文书缺少唯一报告：{identifier}")
            report = reports[0]
            if (
                not _hash_equal(report.get("derived_sha256"), current.get("sha256"))
                or report.get("rereview_status") != "passed"
                or current.get("review_status") != "passed"
                or current.get("status") not in {"reviewed", "approved"}
                or current.get("stale")
            ):
                raise LegalCaseError(
                    "SANITIZATION_REREVIEW_REQUIRED",
                    f"清洁派生文书未完成再次独立审阅、已失效或报告哈希不匹配：{identifier}",
                )
        for snapshot in current.get("input_snapshot", []):
            ancestor_id = snapshot.get("object_id")
            ancestor = artifacts.get(ancestor_id)
            if ancestor is None:
                if ancestor_id in reports_by_derived:
                    raise LegalCaseError("SANITIZATION_ANCESTOR_MISSING", f"清洁来源Artifact不存在：{ancestor_id}")
                continue
            version, digest = object_version_hash(ancestor)
            if snapshot.get("version") != version or not _hash_equal(snapshot.get("hash"), digest):
                raise LegalCaseError("ARTIFACT_INPUT_SNAPSHOT_CHANGED", f"派生文书的输入快照已变化：{ancestor_id}")
            stack.append(ancestor)


def _verify_package(state: dict[str, Any], state_path: Path, package: dict[str, Any]) -> list[tuple[Path, str | None]]:
    semantic_errors = validate_semantics(state, state_path)
    if semantic_errors:
        raise LegalCaseError(
            "STATE_SEMANTICALLY_INVALID",
            "组卷前案件状态未通过语义门禁；不得依赖操作者漏跑 validate。",
            {"errors": semantic_errors},
        )
    if package.get("stale") or package.get("status") == "stale" or package.get("blockers"):
        raise LegalCaseError("PACKAGE_BLOCKED", "包清单已失效或含阻断项。", {"blockers": package.get("blockers", [])})
    expected_manifest_hash = sha256_bytes(canonical_json(package.get("items", [])).encode("utf-8"))
    if package.get("manifest_hash") != expected_manifest_hash:
        raise LegalCaseError(
            "PACKAGE_MANIFEST_HASH_MISMATCH",
            "包清单项目与 manifest_hash 不一致。",
            {"registered": package.get("manifest_hash"), "actual": expected_manifest_hash},
        )
    active_g2 = [
        item
        for item in state.get("approvals", [])
        if item.get("gate") == "G2_evidence" and item.get("decision") == "approved" and item.get("status") == "active"
    ]
    for approval in active_g2:
        actual_scope_hash = sha256_bytes(canonical_json(approval.get("scope_snapshot", [])).encode("utf-8"))
        if approval.get("scope_hash") != actual_scope_hash:
            raise LegalCaseError(
                "APPROVAL_SCOPE_HASH_MISMATCH",
                "有效G2的 scope_snapshot 与 scope_hash 不一致；禁止拼接旧批准范围。",
                {"approval_id": approval.get("id")},
            )
    package_has_evidence = any(item.get("object_type") == "evidence" for item in package.get("items", []))
    current_evidence_ids = {item.get("id") for item in state.get("evidence", []) if item.get("current_submission")}
    if len(active_g2) > 1 or ((package_has_evidence or current_evidence_ids) and len(active_g2) != 1):
        raise LegalCaseError(
            "G2_NOT_UNIQUE",
            "包含当前提交证据的组卷要求恰有一个有效G2；禁止缺失批准或拼接多次旧批准。",
            {"active_g2_count": len(active_g2), "approval_ids": [item.get("id") for item in active_g2]},
        )
    g2_scope = active_g2[0].get("scope_snapshot", []) if len(active_g2) == 1 else []
    g2_evidence_ids = {item.get("object_id") for item in g2_scope if str(item.get("object_id", "")).startswith("E-")}
    if len(active_g2) == 1 and g2_evidence_ids != current_evidence_ids:
        raise LegalCaseError(
            "G2_EVIDENCE_SET_MISMATCH",
            "唯一有效G2的证据集合与当前提交集合不一致。",
            {"g2_evidence_ids": sorted(g2_evidence_ids), "current_evidence_ids": sorted(current_evidence_ids)},
        )
    sources = {item.get("id"): item for item in state.get("sources", [])}
    composition_specs = {item.get("id"): item for item in state.get("composition_specs", [])}
    verified_composition_ids: set[str] = set()
    workspace = state_path.parent.parent
    inputs: list[tuple[Path, str | None]] = []
    for item in sorted(package.get("items", []), key=lambda value: value.get("order", 0)):
        collection, current = find_object(state, item["object_id"])
        if current is None:
            raise LegalCaseError("PACKAGE_OBJECT_MISSING", f"包项目对象不存在：{item['object_id']}")
        current_version, current_hash = object_version_hash(current)
        if current_version != item["version"]:
            raise LegalCaseError("PACKAGE_OBJECT_CHANGED", f"包项目对象已变化：{item['object_id']}")
        if item["object_type"] == "artifact":
            if not _hash_equal(current_hash, item["hash"]):
                raise LegalCaseError("PACKAGE_OBJECT_CHANGED", f"包项目文书哈希已变化：{item['object_id']}")
            if collection != "artifacts" or current.get("stale") or current.get("review_status") != "passed" or current.get("status") not in {"reviewed", "approved"}:
                raise LegalCaseError("PACKAGE_ARTIFACT_NOT_ELIGIBLE", f"文书未审阅通过、未批准或已失效：{item['object_id']}")
            composition_spec_id = current.get("composition_spec_id")
            if composition_spec_id and composition_spec_id not in verified_composition_ids:
                spec = composition_specs.get(composition_spec_id)
                if (
                    spec is None
                    or spec.get("status") != "ready"
                    or spec.get("stale")
                    or spec.get("blockers")
                ):
                    raise LegalCaseError(
                        "PACKAGE_COMPOSITION_SPEC_NOT_READY",
                        f"包内文书绑定的 CompositionSpec 未就绪或已失效：{composition_spec_id}",
                    )
                binding = spec.get("trusted_binding", {})
                catalog_path = binding.get("catalog_path") if isinstance(binding, dict) else None
                if not catalog_path:
                    raise LegalCaseError(
                        "PACKAGE_COMPOSITION_TRUST_MISSING",
                        f"包内文书绑定的 CompositionSpec 缺少个人模板 live binding：{composition_spec_id}",
                    )
                trust_validation = verify_composition_trust_binding(spec, state, Path(catalog_path).resolve())
                if not trust_validation["ok"]:
                    raise LegalCaseError(
                        "PACKAGE_COMPOSITION_TRUST_CHANGED",
                        f"包内文书绑定的模板、画像、串案指纹或批准已变化：{composition_spec_id}",
                        {"composition_spec_id": composition_spec_id, "errors": trust_validation["errors"]},
                    )
                verified_composition_ids.add(composition_spec_id)
            _verify_sanitization_lineage(state, current)
        elif item["object_type"] == "evidence":
            if collection != "evidence" or current.get("status") == "stale" or not current.get("current_submission") or current.get("lawyer_decision") != "submit_now":
                raise LegalCaseError("PACKAGE_EVIDENCE_NOT_APPROVED", f"证据没有有效G2当前提交决定：{item['object_id']}")
            evidence_version, evidence_hash = object_version_hash(current)
            exact_evidence = any(
                snapshot.get("object_id") == current.get("id")
                and snapshot.get("version") == evidence_version
                and _hash_equal(snapshot.get("hash"), evidence_hash)
                for snapshot in g2_scope
            )
            if not exact_evidence:
                raise LegalCaseError("PACKAGE_EVIDENCE_SNAPSHOT_MISMATCH", f"证据未被有效G2按当前版本和哈希绑定：{item['object_id']}")
            for locator in current.get("source_refs", []):
                source = sources.get(locator.get("source_id"))
                if source is None or not any(
                    snapshot.get("object_id") == source.get("id")
                    and snapshot.get("version") == source.get("version")
                    and _hash_equal(snapshot.get("hash"), source.get("sha256"))
                    for snapshot in g2_scope
                ):
                    raise LegalCaseError(
                        "PACKAGE_EVIDENCE_SOURCE_SNAPSHOT_MISMATCH",
                        f"证据来源未被同一有效G2按版本和哈希绑定：{locator.get('source_id')}",
                    )
        path = Path(item["path"])
        path = path if path.is_absolute() else workspace / path
        if not path.is_file() or sha256_file(path) != item["hash"]:
            raise LegalCaseError("PACKAGE_FILE_HASH_MISMATCH", f"包项目文件不存在或哈希变化：{path}")
        if path.suffix.casefold() != ".pdf":
            raise LegalCaseError("PACKAGE_ITEM_NOT_PDF", f"确定性PDF组卷只接受PDF输入：{path}")
        inputs.append((path, item.get("bookmark")))
    return inputs


def _reverify_package_before_publish(
    state_path: Path,
    *,
    state_file_sha256: str,
    package_id: str,
    manifest_hash: str,
) -> None:
    """Recheck state, catalog bindings, and every input after staging output."""

    if sha256_file(state_path) != state_file_sha256:
        raise LegalCaseError(
            "PACKAGE_STATE_CHANGED_DURING_BUILD",
            "生成暂存结果期间案件状态发生变化；未发布输出，请重新读取后再试。",
        )
    latest_state = load_json(state_path)
    latest_package = _select_package(latest_state, package_id)
    if latest_package.get("manifest_hash") != manifest_hash:
        raise LegalCaseError(
            "PACKAGE_MANIFEST_CHANGED_DURING_BUILD",
            "生成暂存结果期间包清单发生变化；未发布输出。",
        )
    _verify_package(latest_state, state_path, latest_package)


def command_bundle(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    state_file_sha256 = sha256_file(state_path)
    package = _select_package(state, args.package_id)
    inputs = _verify_package(state, state_path, package)
    output = Path(args.output).resolve()
    staged = _staged_output_path(output)
    try:
        result = bundle_pdfs(inputs, staged, args.page_numbers)
        result["output"] = str(output)
        if result.get("ok"):
            _reverify_package_before_publish(
                state_path,
                state_file_sha256=state_file_sha256,
                package_id=package["id"],
                manifest_hash=package["manifest_hash"],
            )
            result["sha256"] = _publish_staged_output(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    result["package_id"] = package["id"]
    result["manifest_hash"] = package["manifest_hash"]
    result["external_actions_executed"] = 0
    return result


def command_print_sheet(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    state_file_sha256 = sha256_file(state_path)
    package = _select_package(state, args.package_id)
    # Validate eligibility and file hashes without requiring a PDF backend.
    _verify_package(state, state_path, package)
    output = Path(args.output).resolve()
    staged = _staged_output_path(output)
    try:
        result = write_print_sheet(staged, state["matter"], package, state_content_hash(state))
        _reverify_package_before_publish(
            state_path,
            state_file_sha256=state_file_sha256,
            package_id=package["id"],
            manifest_hash=package["manifest_hash"],
        )
        result["sha256"] = _publish_staged_output(staged, output)
        result["output"] = str(output)
    finally:
        staged.unlink(missing_ok=True)
    result.update({"package_id": package["id"], "manifest_hash": package["manifest_hash"], "external_actions_executed": 0})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Legal Case OS v1.2 deterministic offline CLI")
    parser.add_argument("--version", action="version", version="legal-case-os 1.2.0")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("docx-inspect", help="inspect exact main-body paragraph and table-cell targets without editing")
    item.add_argument("--file", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_docx_inspect)

    item = sub.add_parser("docx-patch", help="apply a source-bound local edit and retain task constraints in a new derivative")
    item.add_argument("--file", required=True)
    item.add_argument("--plan", required=True)
    item.add_argument("--output-dir", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_docx_patch)

    item = sub.add_parser("evidence-pages", help="number explicit PDF page ranges and fill only their original catalog cells")
    item.add_argument("--plan", required=True)
    item.add_argument("--output-dir", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_evidence_pages)

    item = sub.add_parser("delivery-current", help="read the current immutable local deliverable version")
    item.add_argument("--store-dir", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_delivery_current)

    for name, handler in (("delivery-publish", command_delivery_publish), ("delivery-restore", command_delivery_restore)):
        item = sub.add_parser(name, help="register or restore exact local output bytes without granting formal approval")
        item.add_argument("--store-dir", required=True)
        item.add_argument("--task-dir" if name == "delivery-publish" else "--version-id", required=True)
        item.add_argument("--expected-current", required=True, help="current revision token from delivery-current; none for a new store")
        item.add_argument("--json", action="store_true")
        item.set_defaults(handler=handler)

    item = sub.add_parser("source-links", help="link an exact original hash to current approved profile versions")
    item.add_argument("--sha256", required=True)
    item.add_argument("--source-map", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_source_links)

    item = sub.add_parser("material-import", help="snapshot and read one explicitly supplied task file without a backend")
    item.add_argument("--file", required=True)
    item.add_argument("--output-dir", required=True)
    item.add_argument("--role", required=True)
    item.add_argument("--origin", default="session_upload")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_material_import)

    item = sub.add_parser("material-import-original", help="verify and import all MCP original chunks into task files")
    item.add_argument("--parts", required=True)
    item.add_argument("--output-dir", required=True)
    item.add_argument("--role", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_material_import_original)

    item = sub.add_parser("material-read", help="read source-linked text from a verified task record")
    item.add_argument("--record", required=True)
    item.add_argument("--offset", type=int, default=0)
    item.add_argument("--limit", type=int, default=12000)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_material_read)

    for name, handler in (("task-render", command_task_render), ("learning-build", command_learning_build)):
        item = sub.add_parser(name, help="use source-bound AI writing or learning proposals in the current task")
        item.add_argument("--material-record", action="append", required=True)
        item.add_argument("--proposal", required=True)
        item.add_argument("--output-dir" if name == "task-render" else "--output", required=True)
        item.add_argument("--json", action="store_true")
        item.set_defaults(handler=handler)

    item = sub.add_parser("learning-save", help="save the exact learning candidate after explicit user confirmation")
    item.add_argument("--candidate", required=True)
    item.add_argument("--library-dir", required=True)
    item.add_argument("--confirmation", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_learning_save)

    item = sub.add_parser("learning-load", help="load confirmed portable learning without a database")
    item.add_argument("--path", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_learning_load)

    item = sub.add_parser("learning-check", help="check a task-local learning application against its actual draft text")
    item.add_argument("--learning", required=True)
    item.add_argument("--application", required=True)
    item.add_argument("--draft-text", required=True)
    item.add_argument("--material-record", action="append", default=[])
    item.add_argument("--output", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_learning_check)

    item = sub.add_parser("init", help="initialize a non-overwriting matter workspace")
    item.add_argument("--workspace", required=True)
    item.add_argument("--matter-id", required=True)
    item.add_argument("--title", default="")
    item.add_argument("--environment", choices=["test", "production"], default="production")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_init)

    item = sub.add_parser("upgrade-state", help="migrate a canonical v1.0/v1.1 matter state to v1.2 without touching originals")
    item.add_argument("--state", required=True)
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_upgrade_state)

    item = sub.add_parser("index", help="hash, deduplicate and index original materials")
    item.add_argument("--workspace", required=True)
    item.add_argument("--source-dir")
    item.add_argument("--ocr-report")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_index)

    item = sub.add_parser("ingest-batch", help="durably ingest one incremental material batch without rescanning or removing other sources")
    item.add_argument("--workspace", required=True)
    item.add_argument("--source-dir", required=True)
    item.add_argument("--batch-id")
    item.add_argument("--received-at")
    item.add_argument("--arrival-channel")
    item.add_argument("--sender")
    item.add_argument("--authority", choices=["court", "client", "lawyer", "system", "third_party", "unknown"])
    item.add_argument("--ocr-report")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_ingest_batch)

    item = sub.add_parser("context-build", help="build a bounded matter-memory capsule using an explicit read mode")
    item.add_argument("--state", required=True)
    item.add_argument("--mode", choices=["off", "file_scoped", "relevant", "reflect"], required=True)
    item.add_argument("--scope", action="append", default=[])
    item.add_argument("--query", default="")
    item.add_argument("--max-items", type=int, default=24)
    item.add_argument("--output")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_context_build)

    item = sub.add_parser("memory-view", help="rebuild the short current-case view inside this matter workspace")
    item.add_argument("--state", required=True)
    item.add_argument("--output", required=True)
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_memory_view)

    item = sub.add_parser("triage-commit", help="preview a candidate delta or commit lawyer-confirmed triage without promoting evidence")
    item.add_argument("--state", required=True)
    item.add_argument("--card", required=True, help="JSON candidate packet")
    item.add_argument("--write-mode", choices=["candidate", "commit"], default="candidate")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--approval-id")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_triage_commit)

    item = sub.add_parser("deadline-verify", help="verify or dismiss a source-linked deadline candidate")
    item.add_argument("--state", required=True)
    item.add_argument("--deadline-id", required=True)
    item.add_argument("--decision", choices=["verified", "dismissed"], required=True)
    item.add_argument("--due-at")
    item.add_argument("--reason")
    item.add_argument("--calculation-basis")
    item.add_argument("--verification-note")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--approval-id")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_deadline_verify)

    item = sub.add_parser("impact-apply", help="apply or reject one bounded impact assessment and propagate declared staleness")
    item.add_argument("--state", required=True)
    item.add_argument("--assessment", required=True)
    item.add_argument("--decision", choices=["approved", "not_required", "rejected"], required=True)
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--approval-id")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_impact_apply)

    item = sub.add_parser("seed-promote", help="record a human decision on a potential new matter without creating its workspace")
    item.add_argument("--state", required=True)
    item.add_argument("--seed-id", required=True)
    item.add_argument("--decision", choices=["approved", "rejected"], required=True)
    item.add_argument("--new-matter-id")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--approval-id")
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_seed_promote)

    item = sub.add_parser("workflow-start", help="start a persistent legal work order independent of chat sessions")
    item.add_argument("--state", required=True)
    item.add_argument("--workflow-id")
    item.add_argument("--kind", choices=["withdrawal", "preservation", "audit_report_request", "supplemental_evidence", "hearing_preparation", "filing", "service", "other"], required=True)
    item.add_argument("--title", required=True)
    item.add_argument("--input-event-id")
    item.add_argument("--related-object-id", action="append", default=[])
    item.add_argument("--due-at")
    item.add_argument("--approval-id")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_workflow_start)

    item = sub.add_parser("workflow-update", help="wait, resume, complete, or cancel a persistent legal work order")
    item.add_argument("--state", required=True)
    item.add_argument("--workflow-id", required=True)
    item.add_argument("--action", choices=["wait", "resume", "complete", "cancel"], required=True)
    item.add_argument("--waiting-for")
    item.add_argument("--resume-condition")
    item.add_argument("--output-id", action="append", default=[])
    item.add_argument("--completion-event-id")
    item.add_argument("--reason")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_workflow_update)

    item = sub.add_parser("run-start", help="start one retryable AI or script run under a persistent workflow")
    item.add_argument("--state", required=True)
    item.add_argument("--workflow-id", required=True)
    item.add_argument("--task-id")
    item.add_argument("--operation", required=True)
    item.add_argument("--input-snapshot", required=True)
    item.add_argument("--run-id")
    item.add_argument("--output-log")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--actor", default="local-agent")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_run_start)

    item = sub.add_parser("run-finish", help="finish one run without implicitly completing its legal workflow")
    item.add_argument("--state", required=True)
    item.add_argument("--run-id", required=True)
    item.add_argument("--status", choices=["completed", "failed", "killed", "aborted"], required=True)
    item.add_argument("--commit-status", choices=["candidate", "committed", "discarded"], default="candidate")
    item.add_argument("--output-id", action="append", default=[])
    item.add_argument("--output-cursor", type=int, default=0)
    item.add_argument("--cursor-after", type=int)
    item.add_argument("--reason")
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--actor", default="local-agent")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_run_finish)

    item = sub.add_parser("reconcile-matter", help="read-only recovery audit for sources, cursors, deadlines, workflows, and interrupted runs")
    item.add_argument("--state", required=True)
    item.add_argument("--output")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_reconcile_matter)

    item = sub.add_parser("validate", help="validate canonical state, semantics, audit cursor and template registry")
    item.add_argument("--state", required=True)
    item.add_argument("--schema")
    item.add_argument("--registry")
    item.add_argument("--personal-catalog")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_validate)

    item = sub.add_parser("route", help="compile Chinese legal-work language into TaskFrame, clarification, and RunSpec")
    item.add_argument("--text", required=True)
    item.add_argument("--state")
    item.add_argument("--capabilities", help="inline JSON object or JSON file describing currently available local/web/database/API capabilities")
    item.add_argument("--material-record", action="append", default=[], help="explicit task input record; reads metadata only and does not load source text")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_route)

    item = sub.add_parser("approve-ok", help="approve only one exact current unchanged pending object")
    item.add_argument("--state", required=True)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--reason")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_approve_ok)

    item = sub.add_parser("invalidate", help="propagate explicit staleness without deleting prior artifacts")
    item.add_argument("--state", required=True)
    group = item.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-id")
    group.add_argument("--object-id")
    item.add_argument("--new-hash")
    item.add_argument("--reason", required=True)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_invalidate)

    item = sub.add_parser("preflight", help="inspect DOCX/PDF for internal and filing blockers")
    item.add_argument("--path", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_preflight)

    item = sub.add_parser("sanitize-docx", help="create an approved derivative DOCX and sanitization report")
    item.add_argument("--source", required=True)
    item.add_argument("--approved-items", required=True)
    item.add_argument("--output", required=True)
    item.add_argument("--report")
    item.add_argument("--state")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_sanitize_docx)

    item = sub.add_parser("min-diff", help="check change ratio and allowed original line ranges")
    item.add_argument("--original", required=True)
    item.add_argument("--modified", required=True)
    item.add_argument("--allowed-line", action="append", default=[])
    item.add_argument("--max-change-ratio", type=float, default=0.25)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_min_diff)

    item = sub.add_parser("template-distill", help="distill one immutable DOCX into a draft FormProfile or WritingProfile")
    item.add_argument("--source", required=True)
    item.add_argument("--output-dir", required=True)
    item.add_argument("--profile-kind", choices=["form", "writing"], required=True)
    item.add_argument("--usage-mode", choices=["fillable_clone", "reference", "hybrid"], required=True)
    item.add_argument("--template-id", required=True)
    item.add_argument("--name", required=True)
    item.add_argument("--document-type", required=True)
    item.add_argument("--overrides", help="inline JSON object or JSON file containing reviewed profile rules")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_distill)

    item = sub.add_parser("template-register", help="create, upgrade or retire one lawyer-approved personal template")
    item.add_argument("--operation", choices=["create", "upgrade", "retire"], required=True)
    item.add_argument("--source", help="required for create/upgrade; omitted for retire")
    item.add_argument("--profile", help="required draft profile for create/upgrade; omitted for retire")
    item.add_argument("--approval", required=True, help="inline JSON object or JSON approval file")
    item.add_argument("--destination-dir", help="required versioned-copy directory for create/upgrade")
    item.add_argument("--template-id", help="required for retire; optional profile cross-check otherwise")
    item.add_argument("--catalog")
    item.add_argument("--version", help="defaults to 1.0.0 for create; must increase for upgrade")
    item.add_argument("--category")
    item.add_argument("--alias", action="append")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_register)

    item = sub.add_parser("template-fill-plan", help="build one source-bound field proposal and consolidated confirmation batch")
    item.add_argument("--profile", required=True)
    item.add_argument("--catalog", help="active personal template catalog; defaults to the local personal catalog")
    item.add_argument("--data", required=True, help="inline JSON object or JSON file")
    item.add_argument("--provenance", required=True, help="inline JSON object or JSON file; canonical snapshot is rebuilt from --state")
    item.add_argument("--state", required=True)
    item.add_argument("--expected-state-version", type=int, required=True)
    item.add_argument("--output")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_fill_plan)

    item = sub.add_parser("template-fill-docx", help="clone a DOCX and change only approved stable text nodes")
    item.add_argument("--source", required=True)
    item.add_argument("--profile", required=True)
    item.add_argument("--catalog", help="active personal template catalog; defaults to the local personal catalog")
    item.add_argument("--plan", required=True)
    item.add_argument("--state", required=True)
    item.add_argument("--output", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_fill_docx)

    item = sub.add_parser("template-repeat-plan", help="build a TEST-ONLY exact repeatable-row plan")
    item.add_argument("--profile", required=True)
    item.add_argument("--groups", required=True, help="inline JSON array or JSON file")
    item.add_argument("--output")
    item.add_argument("--test-mode", action="store_true", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_repeat_plan)

    item = sub.add_parser("template-hybrid-plan", help="build a TEST-ONLY mechanical/body HybridPlan")
    item.add_argument("--profile", required=True)
    item.add_argument("--mechanical", required=True, help="inline JSON object or JSON file")
    item.add_argument("--bodies", required=True, help="inline JSON array or JSON file")
    item.add_argument("--output")
    item.add_argument("--test-mode", action="store_true", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_hybrid_plan)

    item = sub.add_parser("template-structural-apply", help="apply a TEST-ONLY repeatable or hybrid structural plan")
    item.add_argument("--source", required=True)
    item.add_argument("--profile", required=True)
    item.add_argument("--plan", required=True, help="inline JSON object or JSON file")
    item.add_argument("--output", required=True)
    item.add_argument("--test-mode", action="store_true", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_structural_apply)

    item = sub.add_parser("composition-build", help="freeze template roles, claims, authorities and versioned inputs in a CompositionSpec")
    item.add_argument("--input", required=True, help="inline JSON object or JSON file")
    item.add_argument("--output")
    item.add_argument("--state")
    item.add_argument("--catalog", help="strict personal template catalog; production defaults to the local personal catalog")
    item.add_argument("--test-mode", action="store_true", help="isolated TEST-ONLY fixture; cannot persist to case-state or become production-ready")
    item.add_argument("--expected-state-version", type=int)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_composition_build)

    item = sub.add_parser("composition-validate", help="validate a CompositionSpec against current verified authority")
    item.add_argument("--spec", required=True, help="inline JSON object or JSON file")
    item.add_argument("--authorities", required=True, help="inline JSON array/catalog or JSON file")
    item.add_argument("--test-mode", action="store_true", help="allow TEST-ONLY fixtures; never makes a production court candidate")
    item.add_argument("--output")
    item.add_argument("--document", help="final DOCX/PDF/text artifact; required for production court_candidate")
    item.add_argument("--claim-map", help="ArtifactClaimMap JSON; required with --document for production court_candidate")
    item.add_argument("--state")
    item.add_argument("--catalog", help="strict personal template catalog; production defaults to the local personal catalog")
    item.add_argument("--expected-state-version", type=int)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_composition_validate)

    item = sub.add_parser("citation-audit", help="block unregistered, unbound or unverified visible case/statute citations")
    item.add_argument("--document", required=True)
    item.add_argument("--authorities", required=True)
    item.add_argument("--composition-spec", help="optional CompositionSpec for output-side ClaimBinding coverage")
    item.add_argument("--claim-map", help="optional ArtifactClaimMap; must be supplied with --composition-spec")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_citation_audit)

    item = sub.add_parser("exemplar-leak-check", help="block old-case names, numbers, amounts or phrases using hash-only fingerprints")
    item.add_argument("--document", required=True)
    item.add_argument("--fingerprints", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_exemplar_leak_check)

    item = sub.add_parser("template-fill", help="verify and fill a registered template")
    item.add_argument("--template-id", required=True)
    item.add_argument("--data", required=True)
    item.add_argument("--output", required=True)
    item.add_argument("--registry")
    item.add_argument("--state")
    item.add_argument("--expected-state-version", type=int)
    item.add_argument("--actor", default="local-user")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_fill)

    item = sub.add_parser("template-ref-validate", help="validate the separate personal reference-template catalog")
    item.add_argument("--catalog")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_ref_validate)

    item = sub.add_parser("template-ref-resolve", help="resolve one exact personal template or suite by ID, name or alias")
    item.add_argument("--template-id", required=True)
    item.add_argument("--catalog")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_template_ref_resolve)

    item = sub.add_parser("name", help="generate a safe versioned filename without renaming files")
    item.add_argument("--matter-id", required=True)
    item.add_argument("--kind", required=True)
    item.add_argument("--title", required=True)
    item.add_argument("--version", type=int, required=True)
    item.add_argument("--ext", required=True)
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_name)

    item = sub.add_parser("bundle", help="assemble an eligible PDF package or report degradation")
    item.add_argument("--state", required=True)
    item.add_argument("--output", required=True)
    item.add_argument("--package-id")
    item.add_argument("--page-numbers", action="store_true")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_bundle)

    item = sub.add_parser("print-sheet", help="create a candidate print production sheet without printing")
    item.add_argument("--state", required=True)
    item.add_argument("--output", required=True)
    item.add_argument("--package-id")
    item.add_argument("--json", action="store_true")
    item.set_defaults(handler=command_print_sheet)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows isolated Python ignores PYTHONIOENCODING; JSON and help must still
    # preserve Chinese paths and text when captured by DSH or another client.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        emit(result, getattr(args, "json", False))
        return 0 if result.get("ok") else 2
    except LegalCaseError as exc:
        emit(exc.as_dict(), getattr(args, "json", False))
        return 2
    except KeyboardInterrupt:
        emit({"ok": False, "code": "INTERRUPTED", "message": "用户中断。"}, getattr(args, "json", False))
        return 130
    except Exception as exc:  # Never emit a false success on an unexpected failure.
        emit({"ok": False, "code": "UNEXPECTED_ERROR", "message": str(exc), "exception": type(exc).__name__}, getattr(args, "json", False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
