"""Deterministic contracts for multi-template legal-document composition.

The functions in this module are deliberately side-effect free.  They select
template roles, validate a ``CompositionSpec`` and its ``ClaimBinding`` rows,
audit citations, detect exemplar leakage from hash-only fingerprints, and
compare versioned dependencies.  They do not draft legal prose, fetch legal
authorities, or mutate ``case-state.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .core import (
    LegalCaseError,
    canonical_json,
    find_object,
    load_json,
    object_version_hash,
    sha256_file,
    validate_against_schema,
)


COMPOSITION_SCHEMA_VERSION = "1.0.0"
MAX_AUXILIARY_TEMPLATES = 3
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_CLAIM_MAP_SCHEMA_PATH = PROJECT_ROOT / "shared" / "schemas" / "artifact-claim-map.schema.json"
NON_SUBSTANTIVE_REASONS = {
    "heading",
    "caption",
    "signature_block",
    "court_identifier",
    "party_identity_block",
    "procedural_metadata",
    "page_header_footer",
    "table_label",
    "separator",
    "decorative_visual",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_pymupdf() -> Any:
    """Load PyMuPDF under its current name, with an old-install fallback."""

    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:
        import fitz as pymupdf  # type: ignore

        return pymupdf


def _authority_id(authority: Mapping[str, Any]) -> str:
    return str(authority.get("id") or authority.get("authority_id") or "")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256_text(canonical_json(value))


def _error(code: str, message: str, path: str = "$") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _template_score(template: Mapping[str, Any], role: str) -> tuple[float, float, str]:
    profile = template.get("profile") if isinstance(template.get("profile"), Mapping) else {}
    score = template.get(f"{role}_score", profile.get(f"{role}_score", 0))
    priority = template.get("priority", profile.get("priority", 0))
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        numeric_score = 0.0
    try:
        numeric_priority = float(priority)
    except (TypeError, ValueError):
        numeric_priority = 0.0
    # max() uses this tuple.  Reverse the identifier to neither hide nor add a
    # random tie-breaker: sorting below first makes equal inputs reproducible.
    return numeric_score, numeric_priority, str(template.get("id") or "")


def _section_targets(template: Mapping[str, Any]) -> list[str]:
    profile = template.get("profile") if isinstance(template.get("profile"), Mapping) else {}
    values = template.get("section_targets", profile.get("section_targets", []))
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _role_suitable(template: Mapping[str, Any], role: str) -> bool:
    profile = template.get("profile") if isinstance(template.get("profile"), Mapping) else {}
    values = template.get("role_suitability", profile.get("role_suitability", []))
    # Legacy/test candidates without this field remain deterministic; strict
    # catalog candidates always carry the approved profile value.
    return not values or role in values


def _selection_reason(template: Mapping[str, Any], role: str, mode: str) -> dict[str, Any]:
    score_name = f"{role}_score" if role in {"layout", "structure"} else "auxiliary_score"
    profile = template.get("profile") if isinstance(template.get("profile"), Mapping) else {}
    return {
        "role": f"{role}_primary" if role in {"layout", "structure"} else "auxiliary",
        "template_id": str(template.get("id") or ""),
        "mode": mode,
        "score": template.get(score_name, profile.get(score_name, 0)),
        "priority": template.get("priority", profile.get("priority", 0)),
        "section_targets": _section_targets(template),
        "reason": str(template.get("selection_notes") or profile.get("selection_notes") or mode),
    }


def select_template_roles(
    template_candidates: Sequence[Mapping[str, Any]],
    requested_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Resolve one layout primary, one structure primary, and at most 3 aids.

    A single explicitly requested template fills both primary roles.  With
    multiple (or no) requested templates, declared scores select the two
    primary roles and only templates with explicit section targets may become
    auxiliaries.  An unresolved explicit reference is returned to the caller
    and never silently replaced by an approximate match.
    """

    active = [
        dict(item) for item in template_candidates
        if item.get("status") == "active" and item.get("id")
    ]
    active.sort(key=lambda item: str(item["id"]))
    alias_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in active:
        terms = [item["id"], item.get("name", ""), *item.get("aliases", [])]
        for term in terms:
            normalized = str(term).strip().casefold()
            if normalized:
                alias_index[normalized].append(item)

    explicit = list(dict.fromkeys(str(ref).strip() for ref in (requested_refs or []) if str(ref).strip()))
    unresolved: list[str] = []
    ambiguous: list[str] = []
    resolved: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ref in explicit:
        matches = alias_index.get(ref.casefold(), [])
        if not matches:
            unresolved.append(ref)
            continue
        unique_matches = {str(item["id"]): item for item in matches}
        if len(unique_matches) != 1:
            ambiguous.append(ref)
            continue
        selected = next(iter(unique_matches.values()))
        if selected["id"] not in seen_ids:
            resolved.append(selected)
            seen_ids.add(str(selected["id"]))

    pool = resolved if explicit else active
    if unresolved or ambiguous or not pool:
        return {
            "layout_template_id": None,
            "structure_template_id": None,
            "auxiliary_templates": [],
            "resolved_template_ids": [str(item["id"]) for item in resolved],
            "unresolved_refs": unresolved,
            "ambiguous_refs": ambiguous,
            "selection_reasons": [],
        }

    if len(pool) == 1:
        template_id = str(pool[0]["id"])
        return {
            "layout_template_id": template_id,
            "structure_template_id": template_id,
            "auxiliary_templates": [],
            "resolved_template_ids": [template_id],
            "unresolved_refs": [],
            "ambiguous_refs": [],
            "selection_reasons": [
                _selection_reason(pool[0], "layout", "user_explicit_single"),
                _selection_reason(pool[0], "structure", "user_explicit_single"),
            ],
        }
    layout_pool = [item for item in pool if _role_suitable(item, "layout")]
    structure_pool = [item for item in pool if _role_suitable(item, "structure")]
    if not layout_pool or not structure_pool:
        return {
            "layout_template_id": None,
            "structure_template_id": None,
            "auxiliary_templates": [],
            "resolved_template_ids": [str(item["id"]) for item in pool],
            "unresolved_refs": [],
            "ambiguous_refs": [],
            "selection_reasons": [],
        }
    layout = max(layout_pool, key=lambda item: _template_score(item, "layout"))
    structure = max(structure_pool, key=lambda item: _template_score(item, "structure"))
    primary_ids = {str(layout["id"]), str(structure["id"])}
    auxiliary_pool = [
        item for item in pool
        if str(item["id"]) not in primary_ids and _section_targets(item) and _role_suitable(item, "auxiliary")
    ]
    auxiliary_pool.sort(
        key=lambda item: (
            -float(item.get("auxiliary_score", 0) or 0),
            -float(item.get("priority", 0) or 0),
            str(item["id"]),
        )
    )
    auxiliaries = [
        {"template_id": str(item["id"]), "section_targets": _section_targets(item)}
        for item in auxiliary_pool[:MAX_AUXILIARY_TEMPLATES]
    ]
    return {
        "layout_template_id": str(layout["id"]),
        "structure_template_id": str(structure["id"]),
        "auxiliary_templates": auxiliaries,
        "resolved_template_ids": [str(item["id"]) for item in pool],
        "unresolved_refs": [],
        "ambiguous_refs": [],
        "selection_reasons": [
            _selection_reason(layout, "layout", "trusted_profile_score" if explicit else "trusted_profile_automatic"),
            _selection_reason(structure, "structure", "trusted_profile_score" if explicit else "trusted_profile_automatic"),
            *[
                _selection_reason(item, "auxiliary", "trusted_profile_section_scope")
                for item in auxiliary_pool[:MAX_AUXILIARY_TEMPLATES]
            ],
        ],
    }


def _qualification_snapshot(
    template: Mapping[str, Any],
    roles: Sequence[str],
) -> dict[str, Any]:
    authorization = template.get("authorization") if isinstance(template.get("authorization"), Mapping) else {}
    profile = template.get("profile") if isinstance(template.get("profile"), Mapping) else {}
    profile_approval = profile.get("approval") if isinstance(profile.get("approval"), Mapping) else {}
    baseline = template.get("visual_baseline") if isinstance(template.get("visual_baseline"), Mapping) else {}
    return {
        "template_id": str(template.get("id") or ""),
        "roles": list(dict.fromkeys(str(role) for role in roles if str(role))),
        "approved_final": template.get("approved_final") is True,
        "authorization": {
            "status": authorization.get("status"),
            "basis": authorization.get("basis"),
        },
        "template_version": template.get("version"),
        "template_hash": template.get("sha256"),
        "profile": {
            "id": profile.get("id"),
            "version": profile.get("version"),
            "hash": profile.get("hash", profile.get("sha256")),
            "status": profile.get("status"),
            "fingerprint_manifest_hash": profile.get("fingerprint_manifest_hash"),
            "fingerprint_count": profile.get("fingerprint_count"),
            "approval": {
                "status": profile_approval.get("status"),
                "approval_id": profile_approval.get("approval_id"),
                "approved_at": profile_approval.get("approved_at"),
            },
        },
        "visual_baseline": {
            "id": baseline.get("id"),
            "hash": baseline.get("hash", baseline.get("sha256")),
            "status": baseline.get("status"),
        },
    }


def _build_template_qualifications(
    roles: Mapping[str, Any],
    template_candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    role_map: dict[str, list[str]] = defaultdict(list)
    if roles.get("layout_template_id"):
        role_map[str(roles["layout_template_id"])].append("layout_primary")
    if roles.get("structure_template_id"):
        role_map[str(roles["structure_template_id"])].append("structure_primary")
    for auxiliary in roles.get("auxiliary_templates", []):
        if isinstance(auxiliary, Mapping) and auxiliary.get("template_id"):
            role_map[str(auxiliary["template_id"])].append("auxiliary")
    by_id = {str(item.get("id")): item for item in template_candidates if item.get("id")}
    return [
        _qualification_snapshot(by_id.get(template_id, {"id": template_id}), role_map[template_id])
        for template_id in sorted(role_map)
    ]


def _initial_trust_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    supplied = payload.get("trusted_binding")
    if isinstance(supplied, Mapping):
        return copy.deepcopy(dict(supplied))
    test_mode = payload.get("test_mode") is True
    projection = {
        "template_candidates": payload.get("template_candidates", []),
        "gate_snapshots": payload.get("gate_snapshots", {}),
    }
    return {
        "mode": "test_only_fixture" if test_mode else "unbound",
        "catalog_path": None,
        "catalog_sha256": None,
        "matter_id": payload.get("matter_id"),
        "case_projection_sha256": _canonical_hash(projection) if test_mode else None,
        "template_bindings": [],
        "authority_bindings": [],
        "approval_bindings": [],
        "bound_at": str(payload.get("created_at") or _now_iso()),
    }


def _pairwise_conflict_contract(
    spec_id: str,
    roles: Mapping[str, Any],
    template_candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected_ids = list(dict.fromkeys(
        str(item) for item in (
            roles.get("layout_template_id"),
            roles.get("structure_template_id"),
            *(aux.get("template_id") for aux in roles.get("auxiliary_templates", []) if isinstance(aux, Mapping)),
        ) if item
    ))
    by_id = {str(item.get("id")): item for item in template_candidates if item.get("id")}
    dimension_to_affects = {
        "relief_or_position": "position",
        "evidence_use": "evidence_use",
        "authority": "authority",
        "external_risk": "external_risk",
    }
    assessments: list[dict[str, Any]] = []
    for left_index, left_id in enumerate(sorted(selected_ids)):
        for right_id in sorted(selected_ids)[left_index + 1:]:
            left = by_id.get(left_id, {})
            right = by_id.get(right_id, {})
            left_tags = left.get("compatibility_tags") if isinstance(left.get("compatibility_tags"), Mapping) else {}
            right_tags = right.get("compatibility_tags") if isinstance(right.get("compatibility_tags"), Mapping) else {}
            conflicting_dimensions: list[str] = []
            for dimension in dimension_to_affects:
                lhs = {str(item) for item in left_tags.get(dimension, []) if str(item)}
                rhs = {str(item) for item in right_tags.get(dimension, []) if str(item)}
                if lhs and rhs and lhs.isdisjoint(rhs):
                    conflicting_dimensions.append(dimension)
            severity = "material" if conflicting_dimensions else "style"
            affects = (
                sorted({dimension_to_affects[item] for item in conflicting_dimensions})
                if conflicting_dimensions else ["style_only"]
            )
            assessment_id = "TCA-" + _sha256_text(f"{spec_id}|{left_id}|{right_id}")[:16]
            assessments.append({
                "id": assessment_id,
                "template_ids": [left_id, right_id],
                "severity": severity,
                "affects": affects,
                "reason": (
                    "已批准画像的兼容标签在实质维度不相交，必须集中确认。"
                    if conflicting_dimensions else
                    "未发现诉请/立场/证据/法源/外部风险冲突；仅保留版式或文风差异。"
                ),
                "status": "pending" if severity == "material" else "resolved",
                "resolution": None if severity == "material" else "按主辅角色边界使用。",
            })
    material_basis = [
        {key: item[key] for key in ("id", "template_ids", "severity", "affects", "reason")}
        for item in assessments if item["severity"] == "material"
    ]
    assessment_hash = _canonical_hash(material_basis)
    batch = {
        "id": f"TCB-{spec_id}",
        "assessment_ids": [item["id"] for item in assessments if item["severity"] == "material"],
        "assessment_hash": assessment_hash,
        "status": "pending" if material_basis else "not_required",
        "approval_id": None,
        "resolution": None,
    }
    return assessments, batch


def _normalize_claim_binding(item: Mapping[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(dict(item))
    normalized["proposition_sha256"] = _sha256_text(str(item.get("proposition") or ""))
    normalized["fact_ids"] = list(dict.fromkeys(
        str(value) for value in item.get("fact_ids", []) if str(value)
    ))
    normalized["fact_bindings"] = [dict(value) for value in item.get("fact_bindings", [])]
    normalized["evidence_ids"] = list(dict.fromkeys(
        str(value) for value in item.get("evidence_ids", []) if str(value)
    ))
    normalized["evidence_bindings"] = [dict(value) for value in item.get("evidence_bindings", [])]
    normalized["proposition_basis_sha256"] = str(
        item.get("proposition_basis_sha256")
        or _canonical_hash({
            "claim_id": item.get("id"),
            "proposition_sha256": normalized["proposition_sha256"],
            "fact_bindings": normalized["fact_bindings"],
            "evidence_bindings": normalized["evidence_bindings"],
            "authority_locators": normalized.get("authority_locators", []),
        })
    )
    return normalized


def build_composition_spec(
    payload: Mapping[str, Any] | None = None,
    **legacy_kwargs: Any,
) -> dict[str, Any]:
    """Build an inert composition contract from a CLI-friendly JSON object.

    Keyword arguments remain accepted for Python callers created during the
    v1.2 migration, but a positional JSON-like mapping is the public contract.
    Invalid invocation shapes raise :class:`LegalCaseError`.
    """

    if payload is None:
        payload = legacy_kwargs
    elif legacy_kwargs:
        payload = {**dict(payload), **legacy_kwargs}
    if not isinstance(payload, Mapping):
        raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "合成参数必须是 JSON 对象。")
    spec_id = str(payload.get("id") or payload.get("spec_id") or "")
    document_type = str(payload.get("document_type") or "")
    template_candidates = payload.get("template_candidates", [])
    if not spec_id or not document_type:
        raise LegalCaseError(
            "INVALID_COMPOSITION_PAYLOAD",
            "合成参数必须包含 id/spec_id 与 document_type。",
            {"missing": [name for name, value in (("id", spec_id), ("document_type", document_type)) if not value]},
        )
    if not isinstance(template_candidates, list):
        raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "template_candidates 必须是数组。")
    requested_template_refs = payload.get("requested_template_refs", payload.get("template_refs", []))
    if not isinstance(requested_template_refs, list):
        raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "template_refs 必须是数组。")
    roles = select_template_roles(template_candidates, requested_template_refs)
    pairwise_assessments, conflict_batch = _pairwise_conflict_contract(spec_id, roles, template_candidates)
    blockers: list[str] = []
    if roles["unresolved_refs"]:
        blockers.append("unresolved_template_reference")
    if roles["ambiguous_refs"]:
        blockers.append("ambiguous_template_reference")
    if not roles["layout_template_id"] or not roles["structure_template_id"]:
        blockers.append("template_roles_incomplete")
    if conflict_batch["status"] == "pending":
        blockers.append("material_template_conflict_pending")
    return {
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "id": spec_id,
        "matter_id": payload.get("matter_id"),
        "document_type": document_type,
        "audience": str(payload.get("audience") or "court_candidate"),
        "test_mode": payload.get("test_mode") is True,
        "document_goal": payload.get("document_goal"),
        "relief_or_position": payload.get("relief_or_position"),
        "procedural_requirements": list(payload.get("procedural_requirements", [])),
        "requested_template_refs": list(requested_template_refs or []),
        "template_roles": {
            "layout_template_id": roles["layout_template_id"],
            "structure_template_id": roles["structure_template_id"],
            "auxiliary_templates": roles["auxiliary_templates"],
        },
        "template_selection_reasons": copy.deepcopy(roles.get("selection_reasons", [])),
        "pairwise_conflict_assessments": pairwise_assessments,
        "template_conflict_batch": conflict_batch,
        "template_qualifications": _build_template_qualifications(roles, template_candidates),
        "unresolved_template_refs": roles["unresolved_refs"],
        "ambiguous_template_refs": roles["ambiguous_refs"],
        "trusted_binding": _initial_trust_binding(payload),
        "gate_snapshots": {
            "G1_strategy": copy.deepcopy(
                payload.get("gate_snapshots", {}).get("G1_strategy")
                if isinstance(payload.get("gate_snapshots"), Mapping) else None
            ),
            "G2_evidence": copy.deepcopy(
                payload.get("gate_snapshots", {}).get("G2_evidence")
                if isinstance(payload.get("gate_snapshots"), Mapping) else None
            ),
        },
        "requires_deep_analysis": payload.get("requires_deep_analysis") is True,
        "dangerous_issues": [dict(item) for item in payload.get("dangerous_issues", [])],
        "section_bindings": [dict(item) for item in payload.get("section_bindings", [])],
        "claim_bindings": [
            _normalize_claim_binding(item)
            for item in payload.get("claim_bindings", [])
        ],
        "authority_ids": list(dict.fromkeys(str(item) for item in payload.get("authority_ids", []) if str(item))),
        "adverse_treatments": [dict(item) for item in payload.get("adverse_treatments", [])],
        "risk_conflicts": [dict(item) for item in payload.get("risk_conflicts", [])],
        "dependencies": [dict(item) for item in payload.get("dependencies", [])],
        "dependent_artifact_ids": list(dict.fromkeys(
            str(item) for item in payload.get("dependent_artifact_ids", []) if str(item)
        )),
        "status": "blocked" if blockers else "draft",
        "blockers": list(dict.fromkeys(blockers)),
        "stale": False,
        "stale_reason": None,
        "created_at": str(payload.get("created_at") or _now_iso()),
        "validated_at": None,
    }


def _trusted_catalog_candidate(entry: Mapping[str, Any], catalog_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a composition candidate exclusively from catalog-bound artifacts."""
    from .templates import personal_fingerprint_manifest_path, personal_profile_path

    profile_path = personal_profile_path(dict(entry), catalog_path)
    manifest_path = personal_fingerprint_manifest_path(dict(entry), catalog_path)
    profile = load_json(profile_path)
    manifest = load_json(manifest_path)
    if profile.get("profile_kind") != "writing":
        raise LegalCaseError(
            "COMPOSITION_WRITING_PROFILE_REQUIRED",
            f"模板 {entry.get('id')} 不是 WritingProfile，不得作为诉讼文书合成范例。",
        )
    baseline = profile.get("visual_baseline")
    if not isinstance(baseline, Mapping):
        raise LegalCaseError("VISUAL_BASELINE_MISSING", f"模板 {entry.get('id')} 缺少视觉基线。")
    approval = profile.get("approval") if isinstance(profile.get("approval"), Mapping) else {}
    preferences = entry.get("composition_preferences") if isinstance(entry.get("composition_preferences"), Mapping) else {}
    candidate = {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "aliases": list(entry.get("aliases", [])),
        "status": entry.get("status"),
        "test_only": profile.get("test_only") is True or manifest.get("test_only") is True,
        "approved_final": entry.get("approved_final") is True,
        "authorization": copy.deepcopy(entry.get("authorization", {})),
        "version": entry.get("version"),
        "sha256": entry.get("sha256"),
        "layout_score": preferences.get("layout_score", 1),
        "structure_score": preferences.get("structure_score", 1),
        "auxiliary_score": preferences.get("auxiliary_score", 0),
        "priority": preferences.get("priority", 0),
        "section_targets": list(preferences.get("section_targets", [])),
        "role_suitability": list(preferences.get("role_suitability", [])),
        "selection_notes": str(preferences.get("selection_notes") or ""),
        "compatibility_tags": copy.deepcopy(preferences.get("compatibility_tags", {})),
        "profile": {
            "id": profile.get("profile_id"),
            "version": profile.get("schema_version"),
            "hash": entry.get("profile_sha256"),
            "status": "approved" if profile.get("status") == "active" else profile.get("status"),
            "fingerprint_manifest_hash": entry.get("fingerprint_manifest_sha256"),
            "fingerprint_count": entry.get("fingerprint_count"),
            "approval": {
                "status": "approved" if approval.get("confirmed") is True else "pending",
                "approval_id": approval.get("decision_id"),
                "approved_at": profile.get("activated_at"),
            },
        },
        "visual_baseline": {
            "id": f"VIS-{entry.get('id')}",
            "hash": _canonical_hash(baseline),
            "status": "passed" if baseline.get("status") == "verified" else baseline.get("status"),
        },
    }
    binding = {
        "template_id": str(entry.get("id") or ""),
        "catalog_entry_sha256": _canonical_hash(entry),
        "template_sha256": str(entry.get("sha256") or ""),
        "profile_sha256": str(entry.get("profile_sha256") or ""),
        "visual_baseline_sha256": candidate["visual_baseline"]["hash"],
        "fingerprint_manifest_sha256": str(entry.get("fingerprint_manifest_sha256") or ""),
        "fingerprint_count": int(entry.get("fingerprint_count") or 0),
    }
    # The strict catalog validator already reconciles this metadata.  Recheck
    # here so a future relaxed catalog reader cannot silently bypass it.
    if manifest.get("profile_sha256") != binding["profile_sha256"] or manifest.get("source_sha256") != binding["template_sha256"]:
        raise LegalCaseError(
            "FINGERPRINT_BINDING_MISMATCH",
            f"模板 {entry.get('id')} 的串案指纹未绑定当前模板/画像版本。",
        )
    return candidate, binding


def _resolve_trusted_catalog_candidates(
    requested_refs: Sequence[str],
    catalog_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    from .templates import (
        load_personal_template_catalog,
        resolve_personal_template_reference,
        validate_personal_template_catalog,
    )

    validation = validate_personal_template_catalog(catalog_path)
    if not validation["ok"]:
        raise LegalCaseError(
            "INVALID_PERSONAL_TEMPLATE_CATALOG",
            "个人模板目录未通过严格校验，禁止建立正式合成清单。",
            {"errors": validation["errors"]},
        )
    catalog = load_personal_template_catalog(catalog_path)
    by_id = {str(entry.get("id")): entry for entry in catalog["templates"] if entry.get("id")}
    selected_ids: list[str] = []
    if requested_refs:
        for ref in requested_refs:
            resolved = resolve_personal_template_reference(str(ref), catalog_path)
            if resolved["kind"] == "template":
                selected_ids.append(str(resolved["entry"]["id"]))
                continue
            personal_components = [
                item for item in resolved["components"] if item.get("registry") == "personal"
            ]
            if not personal_components:
                raise LegalCaseError(
                    "COMPOSITION_SUITE_HAS_NO_PERSONAL_WRITING_TEMPLATE",
                    f"套件 {resolved['entry'].get('id')} 没有可用的个人写作模板。",
                )
            selected_ids.extend(str(item["template_id"]) for item in personal_components)
    else:
        selected_ids = [
            str(entry["id"]) for entry in catalog["templates"]
            if entry.get("status") == "active"
        ]
    selected_ids = list(dict.fromkeys(selected_ids))
    candidates: list[dict[str, Any]] = []
    bindings: dict[str, dict[str, Any]] = {}
    for template_id in selected_ids:
        entry = by_id.get(template_id)
        if entry is None:
            raise LegalCaseError("PERSONAL_TEMPLATE_NOT_FOUND", f"目录中不存在模板 {template_id}。")
        candidate, binding = _trusted_catalog_candidate(entry, catalog_path)
        candidates.append(candidate)
        bindings[template_id] = binding
    if not candidates:
        raise LegalCaseError("NO_ACTIVE_WRITING_TEMPLATE", "个人模板目录没有可用的 active WritingProfile。")
    return candidates, bindings


def _active_gate_approval(
    state: Mapping[str, Any],
    payload: Mapping[str, Any],
    gate_name: str,
) -> Mapping[str, Any]:
    gate_ids = payload.get("gate_approval_ids") if isinstance(payload.get("gate_approval_ids"), Mapping) else {}
    requested_id = gate_ids.get(gate_name)
    supplied_gates = payload.get("gate_snapshots") if isinstance(payload.get("gate_snapshots"), Mapping) else {}
    supplied = supplied_gates.get(gate_name) if isinstance(supplied_gates.get(gate_name), Mapping) else {}
    requested_id = requested_id or supplied.get("approval_id")
    candidates = [
        approval for approval in state.get("approvals", [])
        if approval.get("gate") == gate_name
        and approval.get("decision") == "approved"
        and approval.get("status") == "active"
    ]
    if requested_id:
        candidates = [item for item in candidates if item.get("id") == requested_id]
    if len(candidates) != 1:
        raise LegalCaseError(
            "TRUSTED_GATE_APPROVAL_NOT_UNIQUE",
            f"当前案件必须有且仅有一个匹配的 active {gate_name} 批准。",
            {"requested_approval_id": requested_id, "matches": [item.get("id") for item in candidates]},
        )
    return candidates[0]


def _gate_snapshot_from_state(state: Mapping[str, Any], approval: Mapping[str, Any]) -> dict[str, Any]:
    approved_source_ids: set[str] = set()
    for snapshot in approval.get("scope_snapshot", []):
        collection, current = find_object(dict(state), str(snapshot.get("object_id") or ""))
        if collection == "sources" and current is not None:
            approved_source_ids.add(str(current["id"]))
        if collection == "evidence" and current is not None:
            for locator in current.get("source_locators", []):
                if locator.get("source_id"):
                    approved_source_ids.add(str(locator["source_id"]))
    return {
        "approval_id": approval.get("id"),
        "object_id": approval.get("object_id"),
        "version": approval.get("object_version"),
        "object_hash": approval.get("object_hash"),
        "scope_hash": approval.get("scope_hash"),
        "decision": approval.get("decision"),
        "status": approval.get("status"),
        "approved_source_ids": sorted(approved_source_ids),
    }


def _source_record_from_fact(
    fact_id: str,
    fact_sha256: str,
    proposition_sha256: str,
    locator: Mapping[str, Any],
) -> dict[str, Any]:
    source_id = str(locator.get("source_id") or "")
    locator_value = str(locator.get("locator") or "").strip()
    record_kind = str(locator.get("record_kind") or "")
    quote_sha256 = _sha256_text(str(locator.get("quote") or ""))
    record_sha256 = _canonical_hash({
        "fact_id": fact_id,
        "fact_sha256": fact_sha256,
        "proposition_sha256": proposition_sha256,
        "source_id": source_id,
        "locator": locator_value,
        "record_kind": record_kind,
        "quote_sha256": quote_sha256,
    })
    return {
        "source_id": source_id,
        "locator": locator_value,
        "record_kind": record_kind,
        "verified": True,
        "record_sha256": record_sha256,
        "quote_sha256": quote_sha256,
        "fact_id": fact_id,
        "fact_sha256": fact_sha256,
    }


def _evidence_source_locators(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = evidence.get("source_refs")
    if not isinstance(values, list):
        values = evidence.get("source_locators")
    return [item for item in (values or []) if isinstance(item, Mapping)]


def _rebuild_trusted_claim_bindings(
    claims: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    *,
    audience: str,
) -> list[dict[str, Any]]:
    """Treat caller claim rows as ID/locator selectors, never verification facts."""
    rebuilt_claims: list[dict[str, Any]] = []
    for index, supplied in enumerate(claims):
        claim_id = str(supplied.get("id") or "")
        claim_type = str(supplied.get("claim_type") or "")
        if not claim_id or claim_type not in {"fact", "law", "mixed"}:
            raise LegalCaseError(
                "TRUSTED_CLAIM_SELECTOR_INVALID",
                f"第 {index + 1} 个ClaimBinding缺少有效ID或claim_type。",
            )
        court_candidate = audience == "court_candidate" or supplied.get("court_candidate") is True
        fact_ids = list(dict.fromkeys(str(item) for item in supplied.get("fact_ids", []) if str(item)))
        fact_bindings: list[dict[str, Any]] = []
        source_records: list[dict[str, Any]] = []
        proposition = str(supplied.get("proposition") or "")
        proposition_sha256 = _sha256_text(proposition)
        if claim_type in {"fact", "mixed"}:
            if len(fact_ids) != 1:
                raise LegalCaseError(
                    "TRUSTED_CLAIM_FACT_NOT_UNIQUE",
                    f"事实或混合命题 {claim_id} 必须精确选择一个已确认Fact；复合事实应拆分成多个ClaimBinding。",
                )
            collection, fact = find_object(dict(state), fact_ids[0])
            if collection != "facts" or fact is None:
                raise LegalCaseError("TRUSTED_CLAIM_FACT_NOT_FOUND", f"案件状态不存在Fact：{fact_ids[0]}")
            if fact.get("status") != "confirmed" or fact.get("stale") is True:
                raise LegalCaseError(
                    "TRUSTED_CLAIM_FACT_NOT_CONFIRMED",
                    f"Fact {fact_ids[0]} 未确认或已失效，不得重建正式命题。",
                )
            proposition = str(fact.get("statement") or "").strip()
            if not proposition:
                raise LegalCaseError("TRUSTED_CLAIM_FACT_EMPTY", f"Fact {fact_ids[0]} 没有statement。")
            proposition_sha256 = _sha256_text(proposition)
            fact_version, fact_sha256 = object_version_hash(fact)
            if not fact_sha256:
                raise LegalCaseError("TRUSTED_CLAIM_FACT_HASH_MISSING", f"Fact {fact_ids[0]} 无法冻结哈希。")
            raw_locators = fact.get("source_locators") if isinstance(fact.get("source_locators"), list) else []
            if not raw_locators:
                raise LegalCaseError("TRUSTED_CLAIM_FACT_LOCATOR_MISSING", f"Fact {fact_ids[0]} 没有来源定位。")
            for locator in raw_locators:
                if (
                    not isinstance(locator, Mapping)
                    or locator.get("record_kind") != "direct_record"
                    or locator.get("verified") is not True
                    or not str(locator.get("source_id") or "")
                    or not str(locator.get("locator") or "").strip()
                ):
                    raise LegalCaseError(
                        "TRUSTED_CLAIM_FACT_LOCATOR_UNVERIFIED",
                        f"Fact {fact_ids[0]} 含非直接记载或未核验定位。",
                    )
                source_collection, source = find_object(dict(state), str(locator["source_id"]))
                if source_collection != "sources" or source is None:
                    raise LegalCaseError(
                        "TRUSTED_CLAIM_SOURCE_NOT_FOUND",
                        f"Fact {fact_ids[0]} 的来源不存在：{locator.get('source_id')}",
                    )
                source_records.append(_source_record_from_fact(
                    fact_ids[0], str(fact_sha256), proposition_sha256, locator,
                ))
            fact_bindings.append({
                "fact_id": fact_ids[0],
                "version": fact_version,
                "fact_sha256": fact_sha256,
                "statement_sha256": proposition_sha256,
                "proposition_sha256": proposition_sha256,
                "source_record_sha256s": sorted(item["record_sha256"] for item in source_records),
            })

        evidence_ids = list(dict.fromkeys(str(item) for item in supplied.get("evidence_ids", []) if str(item)))
        evidence_bindings: list[dict[str, Any]] = []
        if claim_type in {"fact", "mixed"} and not evidence_ids:
            raise LegalCaseError(
                "TRUSTED_CLAIM_EVIDENCE_REQUIRED",
                f"事实或混合命题 {claim_id} 必须精确选择案件状态中的Evidence。",
            )
        record_by_locator = {
            (item["source_id"], item["locator"]): item["record_sha256"] for item in source_records
        }
        for evidence_id in evidence_ids:
            collection, evidence = find_object(dict(state), evidence_id)
            if collection != "evidence" or evidence is None:
                raise LegalCaseError("TRUSTED_CLAIM_EVIDENCE_NOT_FOUND", f"案件状态不存在Evidence：{evidence_id}")
            if evidence.get("status") != "approved":
                raise LegalCaseError(
                    "TRUSTED_CLAIM_EVIDENCE_NOT_APPROVED",
                    f"Evidence {evidence_id} 不是当前approved证据。",
                )
            if court_candidate and (
                evidence.get("lawyer_decision") != "submit_now"
                or evidence.get("current_submission") is not True
            ):
                raise LegalCaseError(
                    "TRUSTED_CLAIM_EVIDENCE_NOT_SUBMISSION_APPROVED",
                    f"Evidence {evidence_id} 未经律师决定进入当前提交。",
                )
            matching_records = sorted({
                record_by_locator[(str(locator.get("source_id")), str(locator.get("locator")))]
                for locator in _evidence_source_locators(evidence)
                if (str(locator.get("source_id")), str(locator.get("locator"))) in record_by_locator
            })
            if claim_type in {"fact", "mixed"} and not matching_records:
                raise LegalCaseError(
                    "TRUSTED_CLAIM_EVIDENCE_FACT_MISMATCH",
                    f"Evidence {evidence_id} 未精确指向Fact {fact_ids[0]} 的来源定位。",
                )
            evidence_version, evidence_sha256 = object_version_hash(evidence)
            if not evidence_sha256:
                raise LegalCaseError("TRUSTED_CLAIM_EVIDENCE_HASH_MISSING", f"Evidence {evidence_id} 无法冻结哈希。")
            evidence_bindings.append({
                "evidence_id": evidence_id,
                "version": evidence_version,
                "evidence_sha256": evidence_sha256,
                "proposition_sha256": _sha256_text(str(evidence.get("proposition") or "")),
                "source_record_sha256s": matching_records,
            })

        authority_ids = list(dict.fromkeys(str(item) for item in supplied.get("authority_ids", []) if str(item)))
        supplied_locators = {
            str(item.get("authority_id")): item
            for item in supplied.get("authority_locators", [])
            if isinstance(item, Mapping) and item.get("authority_id")
        }
        authority_locators: list[dict[str, Any]] = []
        for authority_id in authority_ids:
            collection, authority = find_object(dict(state), authority_id)
            if collection != "authorities" or authority is None:
                raise LegalCaseError("TRUSTED_CLAIM_AUTHORITY_NOT_FOUND", f"案件状态不存在Authority：{authority_id}")
            supplied_locator = supplied_locators.get(authority_id, {})
            locator_value = str(supplied_locator.get("locator") or "").strip()
            passage_hashes = authority.get("passage_hashes") if isinstance(authority.get("passage_hashes"), Mapping) else {}
            holding_sha256 = passage_hashes.get(locator_value)
            if not locator_value or not re.fullmatch(r"[a-f0-9]{64}", str(holding_sha256 or "")):
                raise LegalCaseError(
                    "TRUSTED_CLAIM_AUTHORITY_LOCATOR_UNVERIFIED",
                    f"Authority {authority_id} 的定位未绑定案件状态中的核验全文哈希。",
                )
            if not is_verified_production_authority(authority):
                raise LegalCaseError(
                    "TRUSTED_CLAIM_AUTHORITY_NOT_PRODUCTION_ELIGIBLE",
                    f"Authority {authority_id} 未满足正式法源门禁。",
                )
            locator_citations = authority.get("locator_citations") if isinstance(authority.get("locator_citations"), Mapping) else {}
            citation = str(
                locator_citations.get(locator_value)
                or authority.get("official_citation")
                or authority.get("citation")
                or ""
            ).strip()
            if not citation:
                raise LegalCaseError(
                    "TRUSTED_CLAIM_AUTHORITY_CITATION_MISSING",
                    f"Authority {authority_id} 未登记可核验引文。",
                )
            authority_locators.append({
                "authority_id": authority_id,
                "locator": locator_value,
                "citation": citation,
                "verified": True,
                "holding_sha256": holding_sha256,
            })
        if claim_type in {"law", "mixed"} and not authority_ids:
            raise LegalCaseError(
                "TRUSTED_CLAIM_AUTHORITY_REQUIRED",
                f"法律或混合命题 {claim_id} 必须选择案件状态中的已核验Authority。",
            )

        proposition_basis_sha256 = _canonical_hash({
            "claim_id": claim_id,
            "proposition_sha256": proposition_sha256,
            "fact_bindings": fact_bindings,
            "evidence_bindings": evidence_bindings,
            "authority_locators": authority_locators,
        })
        rebuilt_claims.append({
            "id": claim_id,
            "proposition": proposition,
            "proposition_sha256": proposition_sha256,
            "proposition_basis_sha256": proposition_basis_sha256,
            "claim_type": claim_type,
            "fact_ids": fact_ids,
            "fact_bindings": fact_bindings,
            "source_ids": list(dict.fromkeys(item["source_id"] for item in source_records)),
            "evidence_ids": evidence_ids,
            "evidence_bindings": evidence_bindings,
            "source_locators": source_records,
            "authority_ids": authority_ids,
            "authority_locators": authority_locators,
            "court_candidate": court_candidate,
            "verification_status": "verified",
        })
    return rebuilt_claims


def _case_trust_projection(
    state: Mapping[str, Any],
    approvals: Sequence[Mapping[str, Any]],
    template_bindings: Sequence[Mapping[str, Any]],
    authority_bindings: Sequence[Mapping[str, Any]],
    claim_bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    scope_objects: dict[str, dict[str, Any]] = {}
    for approval in approvals:
        for snapshot in approval.get("scope_snapshot", []):
            object_id = str(snapshot.get("object_id") or "")
            _collection, current = find_object(dict(state), object_id)
            if current is None:
                continue
            version, digest = object_version_hash(current)
            scope_objects[object_id] = {"object_id": object_id, "version": version, "hash": digest}
    return {
        "matter": {
            "id": state.get("matter", {}).get("id"),
            "stage": state.get("matter", {}).get("stage"),
            "environment": state.get("matter", {}).get("environment"),
        },
        "approvals": [copy.deepcopy(dict(item)) for item in approvals],
        "scope_objects": [scope_objects[key] for key in sorted(scope_objects)],
        "template_bindings": sorted(
            (copy.deepcopy(dict(item)) for item in template_bindings),
            key=lambda item: str(item.get("template_id")),
        ),
        "authority_bindings": sorted(
            (copy.deepcopy(dict(item)) for item in authority_bindings),
            key=lambda item: str(item.get("authority_id")),
        ),
        "claim_state_bindings": [
            {
                "claim_id": item.get("id"),
                "proposition_sha256": item.get("proposition_sha256"),
                "proposition_basis_sha256": item.get("proposition_basis_sha256"),
                "fact_bindings": copy.deepcopy(item.get("fact_bindings", [])),
                "evidence_bindings": copy.deepcopy(item.get("evidence_bindings", [])),
                "source_locators": copy.deepcopy(item.get("source_locators", [])),
                "authority_locators": copy.deepcopy(item.get("authority_locators", [])),
            }
            for item in claim_bindings
        ],
    }


def _authority_bindings_from_state(
    state: Mapping[str, Any],
    authority_ids: Sequence[str],
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for authority_id in sorted(set(str(item) for item in authority_ids if str(item))):
        collection, authority = find_object(dict(state), authority_id)
        if collection != "authorities" or authority is None:
            raise LegalCaseError("TRUSTED_AUTHORITY_NOT_FOUND", f"案件状态不存在Authority：{authority_id}")
        version, digest = object_version_hash(authority)
        if not digest:
            raise LegalCaseError("TRUSTED_AUTHORITY_HASH_MISSING", f"Authority {authority_id} 无法冻结哈希。")
        bindings.append({
            "authority_id": authority_id,
            "version": version,
            "authority_sha256": digest,
        })
    return bindings


def trusted_template_state_snapshot(
    entry: Mapping[str, Any],
    catalog_path: Path,
) -> dict[str, Any]:
    """Convert an approved personal catalog entry into a case-state snapshot."""
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "document_type": entry.get("document_type"),
        "path": str((catalog_path.parent / str(entry.get("path"))).resolve()),
        "source": str(entry.get("source") or "personal_authorized_template"),
        "license": {
            "id": "PERSONAL-AUTHORIZATION",
            "status": "verified",
            "notice": str(entry.get("authorization", {}).get("basis") or "verified personal authorization"),
        },
        "sha256": entry.get("sha256"),
        "version": str(entry.get("version")),
        "fixed_fields": [],
        "editable_fields": [],
        "required_fields": [],
        "aliases": list(entry.get("aliases", [])),
        "usage_mode": entry.get("usage_mode"),
        "profile_id": load_json((catalog_path.parent / str(entry.get("profile_path"))).resolve()).get("profile_id"),
        "profile_path": str((catalog_path.parent / str(entry.get("profile_path"))).resolve()),
        "profile_sha256": entry.get("profile_sha256"),
        "stale": False,
        "stale_reason": None,
        "status": "active",
    }


def build_trusted_composition_spec(
    payload: Mapping[str, Any],
    state: Mapping[str, Any],
    catalog_path: str | Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rebuild all trust-sensitive composition inputs from local sources."""
    from .templates import load_personal_template_catalog

    if payload.get("test_mode") is True:
        raise LegalCaseError("TEST_MODE_NOT_PRODUCTION_BINDABLE", "TEST-ONLY输入不得绑定为正式合成清单。")
    catalog_file = Path(catalog_path).resolve()
    refs = payload.get("requested_template_refs", payload.get("template_refs", []))
    if not isinstance(refs, list):
        raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "template_refs 必须是数组。")
    candidates, candidate_bindings = _resolve_trusted_catalog_candidates(refs, catalog_file)
    if payload.get("audience") == "court_candidate" and any(item.get("test_only") is True for item in candidates):
        raise LegalCaseError(
            "TEST_ONLY_TEMPLATE_NOT_PRODUCTION",
            "TEST-ONLY模板、画像或串案指纹不得绑定为法院候选合成输入。",
        )
    g1 = _active_gate_approval(state, payload, "G1_strategy")
    g2 = _active_gate_approval(state, payload, "G2_evidence")
    bound_payload = copy.deepcopy(dict(payload))
    bound_payload["test_mode"] = False
    bound_payload["matter_id"] = state.get("matter", {}).get("id")
    bound_payload["template_candidates"] = candidates
    # Caller-supplied gate contents are discarded; only their optional IDs are
    # treated as selectors for current case-state approvals.
    bound_payload["gate_snapshots"] = {
        "G1_strategy": _gate_snapshot_from_state(state, g1),
        "G2_evidence": _gate_snapshot_from_state(state, g2),
    }
    # Caller rows are selectors only.  The visible proposition, verification
    # flags, Fact/Evidence hashes and exact source records are rebuilt from the
    # current case state.
    bound_payload["claim_bindings"] = _rebuild_trusted_claim_bindings(
        [item for item in bound_payload.get("claim_bindings", []) if isinstance(item, Mapping)],
        state,
        audience=str(bound_payload.get("audience") or "court_candidate"),
    )
    authority_bindings = _authority_bindings_from_state(
        state,
        [str(item) for item in bound_payload.get("authority_ids", []) if str(item)],
    )
    bound_payload.pop("trusted_binding", None)
    spec = build_composition_spec(bound_payload)
    conflict_batch = spec.get("template_conflict_batch", {})
    if conflict_batch.get("status") == "pending":
        _collection, g1_target = find_object(dict(state), str(g1.get("object_id") or ""))
        if (
            g1_target is not None
            and g1_target.get("template_conflict_batch_sha256") == conflict_batch.get("assessment_hash")
            and str(g1_target.get("template_conflict_resolution") or "").strip()
        ):
            resolution = str(g1_target["template_conflict_resolution"]).strip()
            for assessment in spec.get("pairwise_conflict_assessments", []):
                if assessment.get("severity") == "material":
                    assessment["status"] = "resolved"
                    assessment["resolution"] = resolution
            conflict_batch["status"] = "approved"
            conflict_batch["approval_id"] = g1.get("id")
            conflict_batch["resolution"] = resolution
            spec["blockers"] = [
                item for item in spec.get("blockers", [])
                if item != "material_template_conflict_pending"
            ]
            if not spec["blockers"]:
                spec["status"] = "draft"
    selected_ids = {
        str(spec["template_roles"].get("layout_template_id") or ""),
        str(spec["template_roles"].get("structure_template_id") or ""),
        *(
            str(item.get("template_id") or "")
            for item in spec["template_roles"].get("auxiliary_templates", [])
        ),
    } - {""}
    selected_bindings = [candidate_bindings[item] for item in sorted(selected_ids)]
    auto_types = {"template", "source", "fact", "evidence", "authority", "issue", "artifact", "approval"}
    retained_dependencies = [
        copy.deepcopy(dict(item)) for item in payload.get("dependencies", [])
        if isinstance(item, Mapping) and item.get("object_type") not in auto_types
    ]
    retained_dependencies.extend({
        "object_id": template_id,
        "object_type": "template",
        "version": next(item for item in candidates if item["id"] == template_id)["version"],
        "hash": next(item for item in candidates if item["id"] == template_id)["sha256"],
        "required": True,
    } for template_id in sorted(selected_ids))
    collection_to_type = {
        "sources": "source",
        "facts": "fact",
        "evidence": "evidence",
        "authorities": "authority",
        "issues": "issue",
        "artifacts": "artifact",
        "approvals": "approval",
    }

    def append_state_dependency(object_id: str, expected_collection: str) -> None:
        collection, current = find_object(dict(state), object_id)
        if collection != expected_collection or current is None:
            raise LegalCaseError(
                "COMPOSITION_DEPENDENCY_NOT_FOUND",
                f"合成清单引用的 {expected_collection} 对象不存在：{object_id}",
            )
        version, digest = object_version_hash(current)
        if not digest:
            raise LegalCaseError("COMPOSITION_DEPENDENCY_HASH_MISSING", f"对象 {object_id} 无法冻结哈希。")
        retained_dependencies.append({
            "object_id": object_id,
            "object_type": collection_to_type[expected_collection],
            "version": version,
            "hash": digest,
            "required": True,
        })

    for claim in spec.get("claim_bindings", []):
        for source_id in claim.get("source_ids", []):
            append_state_dependency(str(source_id), "sources")
        for fact_id in claim.get("fact_ids", []):
            append_state_dependency(str(fact_id), "facts")
        for evidence_id in claim.get("evidence_ids", []):
            append_state_dependency(str(evidence_id), "evidence")
        for authority_id in claim.get("authority_ids", []):
            append_state_dependency(str(authority_id), "authorities")
    for authority_id in spec.get("authority_ids", []):
        append_state_dependency(str(authority_id), "authorities")
    for issue in spec.get("dangerous_issues", []):
        append_state_dependency(str(issue.get("issue_id")), "issues")
        append_state_dependency(str(issue.get("analysis_artifact_id")), "artifacts")
        _collection, analysis = find_object(dict(state), str(issue.get("analysis_artifact_id")))
        if analysis is not None and (
            str(issue.get("analysis_version")) != str(analysis.get("version", 1))
            or issue.get("analysis_hash") != analysis.get("sha256")
        ):
            raise LegalCaseError(
                "DANGEROUS_ISSUE_ANALYSIS_DRIFT",
                f"危险争点 {issue.get('issue_id')} 的深析成果版本/哈希不是当前Artifact。",
            )
    append_state_dependency(str(g1.get("id")), "approvals")
    append_state_dependency(str(g2.get("id")), "approvals")
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for dependency in retained_dependencies:
        deduplicated[(str(dependency.get("object_type")), str(dependency.get("object_id")))] = dependency
    retained_dependencies = [deduplicated[key] for key in sorted(deduplicated)]
    spec["dependencies"] = retained_dependencies
    approvals = [g1, g2]
    projection = _case_trust_projection(
        state, approvals, selected_bindings, authority_bindings, spec.get("claim_bindings", []),
    )
    spec["trusted_binding"] = {
        "mode": "catalog_case_state",
        "catalog_path": str(catalog_file),
        "catalog_sha256": sha256_file(catalog_file),
        "matter_id": state.get("matter", {}).get("id"),
        "case_projection_sha256": _canonical_hash(projection),
        "template_bindings": selected_bindings,
        "authority_bindings": authority_bindings,
        "approval_bindings": [
            {
                "gate": approval.get("gate"),
                "approval_id": approval.get("id"),
                "object_id": approval.get("object_id"),
                "object_hash": approval.get("object_hash"),
                "scope_hash": approval.get("scope_hash"),
            }
            for approval in approvals
        ],
        "bound_at": _now_iso(),
    }
    catalog = load_personal_template_catalog(catalog_file)
    by_id = {str(entry.get("id")): entry for entry in catalog["templates"]}
    snapshots = [trusted_template_state_snapshot(by_id[item], catalog_file) for item in sorted(selected_ids)]
    return spec, snapshots


def synchronize_trusted_templates(
    state: dict[str, Any],
    snapshots: Sequence[Mapping[str, Any]],
) -> None:
    """Insert exact template snapshots and reject any same-ID drift."""
    existing = {str(item.get("id")): item for item in state.get("templates", []) if item.get("id")}
    for snapshot_value in snapshots:
        snapshot = copy.deepcopy(dict(snapshot_value))
        prior = existing.get(str(snapshot.get("id")))
        if prior is None:
            state.setdefault("templates", []).append(snapshot)
            existing[str(snapshot.get("id"))] = snapshot
        elif canonical_json(prior) != canonical_json(snapshot):
            raise LegalCaseError(
                "CASE_TEMPLATE_SNAPSHOT_DRIFT",
                f"案件内模板快照 {snapshot.get('id')} 与当前严格目录不一致。",
            )


def verify_composition_trust_binding(
    spec: Mapping[str, Any],
    state: Mapping[str, Any],
    catalog_path: str | Path,
) -> dict[str, Any]:
    """Recompute catalog, profile, fingerprint and G1/G2 trust snapshots."""
    errors: list[dict[str, str]] = []
    binding = spec.get("trusted_binding") if isinstance(spec.get("trusted_binding"), Mapping) else {}
    if binding.get("mode") != "catalog_case_state" or spec.get("test_mode") is True:
        return {"ok": False, "errors": [_error("composition_trust_binding_missing", "正式合成清单未绑定严格个人模板目录与当前案件批准。", "$.trusted_binding")]}
    catalog_file = Path(catalog_path).resolve()
    if str(catalog_file) != binding.get("catalog_path"):
        errors.append(_error("trusted_catalog_path_changed", "校验目录与冻结目录不一致。", "$.trusted_binding.catalog_path"))
    if not catalog_file.is_file() or sha256_file(catalog_file) != binding.get("catalog_sha256"):
        errors.append(_error("trusted_catalog_changed", "个人模板目录已变化。", "$.trusted_binding.catalog_sha256"))
        return {"ok": False, "errors": errors}
    try:
        requested_ids = [str(item.get("template_id")) for item in binding.get("template_bindings", [])]
        candidates, actual_by_id = _resolve_trusted_catalog_candidates(requested_ids, catalog_file)
        actual_bindings = [actual_by_id[item] for item in sorted(requested_ids)]
        expected_bindings = sorted(
            (dict(item) for item in binding.get("template_bindings", [])),
            key=lambda item: str(item.get("template_id")),
        )
        if canonical_json(actual_bindings) != canonical_json(expected_bindings):
            errors.append(_error("trusted_template_artifact_changed", "模板、画像、视觉基线或串案指纹已变化。", "$.trusted_binding.template_bindings"))
        expected_authority_ids = sorted(str(item) for item in spec.get("authority_ids", []) if str(item))
        authority_bindings = _authority_bindings_from_state(state, expected_authority_ids)
        frozen_authority_bindings = sorted(
            (dict(item) for item in binding.get("authority_bindings", [])),
            key=lambda item: str(item.get("authority_id")),
        )
        if canonical_json(authority_bindings) != canonical_json(frozen_authority_bindings):
            errors.append(_error(
                "trusted_authority_changed",
                "案件状态中的法源对象或其核验全文已变化。",
                "$.trusted_binding.authority_bindings",
            ))
        rebuilt_claims = _rebuild_trusted_claim_bindings(
            [item for item in spec.get("claim_bindings", []) if isinstance(item, Mapping)],
            state,
            audience=str(spec.get("audience") or "court_candidate"),
        )
        if canonical_json(rebuilt_claims) != canonical_json(spec.get("claim_bindings", [])):
            errors.append(_error(
                "trusted_claim_state_changed",
                "ClaimBinding不再等于当前confirmed Fact/Evidence/Authority重建结果。",
                "$.claim_bindings",
            ))
        approvals_by_id = {str(item.get("id")): item for item in state.get("approvals", []) if item.get("id")}
        approvals: list[Mapping[str, Any]] = []
        for approval_binding in binding.get("approval_bindings", []):
            approval = approvals_by_id.get(str(approval_binding.get("approval_id")))
            if (
                approval is None
                or approval.get("gate") != approval_binding.get("gate")
                or approval.get("decision") != "approved"
                or approval.get("status") != "active"
                or approval.get("object_id") != approval_binding.get("object_id")
                or approval.get("object_hash") != approval_binding.get("object_hash")
                or approval.get("scope_hash") != approval_binding.get("scope_hash")
            ):
                errors.append(_error("trusted_gate_approval_changed", f"批准 {approval_binding.get('approval_id')} 已变化或失效。", "$.trusted_binding.approval_bindings"))
                continue
            approvals.append(approval)
        if {item.get("gate") for item in approvals} != {"G1_strategy", "G2_evidence"}:
            errors.append(_error("trusted_gate_set_incomplete", "当前 active G1/G2 批准集不完整。", "$.trusted_binding.approval_bindings"))
        else:
            current_gates = {str(item.get("gate")): _gate_snapshot_from_state(state, item) for item in approvals}
            if canonical_json(current_gates) != canonical_json(spec.get("gate_snapshots", {})):
                errors.append(_error("trusted_gate_snapshot_changed", "CompositionSpec中的G1/G2快照与当前批准不一致。", "$.gate_snapshots"))
            projection = _case_trust_projection(
                state, approvals, actual_bindings, authority_bindings, rebuilt_claims,
            )
            if _canonical_hash(projection) != binding.get("case_projection_sha256"):
                errors.append(_error("trusted_case_projection_changed", "案件批准或其范围对象已变化。", "$.trusted_binding.case_projection_sha256"))
        candidate_by_id = {str(item.get("id")): item for item in candidates}
        qualification_by_id = {str(item.get("template_id")): item for item in spec.get("template_qualifications", [])}
        roles = spec.get("template_roles", {})
        expected_qualifications = _build_template_qualifications(roles, candidates)
        if canonical_json(expected_qualifications) != canonical_json(list(qualification_by_id.values())):
            errors.append(_error("trusted_template_qualification_changed", "模板资格快照不是由当前目录重建。", "$.template_qualifications"))
        state_templates = {str(item.get("id")): item for item in state.get("templates", []) if item.get("id")}
        from .templates import load_personal_template_catalog
        catalog = load_personal_template_catalog(catalog_file)
        catalog_by_id = {str(item.get("id")): item for item in catalog["templates"]}
        for template_id in candidate_by_id:
            expected = trusted_template_state_snapshot(catalog_by_id[template_id], catalog_file)
            if canonical_json(state_templates.get(template_id)) != canonical_json(expected):
                errors.append(_error("case_template_snapshot_changed", f"案件内模板快照 {template_id} 与目录不一致。", "$.template_roles"))
    except (LegalCaseError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        errors.append(_error("trusted_binding_recheck_failed", str(exc), "$.trusted_binding"))
    return {"ok": not errors, "errors": errors}


def is_verified_production_authority(authority: Mapping[str, Any]) -> bool:
    """Return true only for a full, verified, non-test production authority."""

    verification = str(authority.get("verification_status") or "")
    source_level = str(authority.get("source_level") or "")
    return bool(
        authority.get("production_eligible") is True
        and authority.get("test_only") is not True
        and authority.get("environment", "production") == "production"
        and verification == "verified"
        and source_level in {"L1_verified_authority", "L2_original_or_official"}
        and authority.get("full_text_available", True) is not False
    )


def is_verified_test_authority(authority: Mapping[str, Any]) -> bool:
    """Accept only explicitly isolated, locally verifiable TEST-ONLY authority."""

    return bool(
        authority.get("test_only") is True
        and authority.get("environment") == "test"
        and authority.get("production_eligible") is False
        and authority.get("verification_status") in {"test_only_verified", "verified_against_local_test_record"}
        and str(authority.get("source_level") or "") in {
            "L1_verified_authority", "L2_original_or_official",
            "L1_verified_authoritative_source",
        }
        and authority.get("full_text_available", True) is not False
    )


def _authority_eligible(authority: Mapping[str, Any], test_mode: bool) -> bool:
    return is_verified_test_authority(authority) if test_mode else is_verified_production_authority(authority)


def validate_composition_spec(
    payload: Mapping[str, Any],
    authority_catalog: Sequence[Mapping[str, Any]] | None = None,
    *,
    production: bool | None = None,
) -> dict[str, Any]:
    """Validate roles, bindings and authorities from a JSON-like payload.

    The payload may be either the spec itself or ``{"spec": ..., "authorities":
    [...], "production": true}``, which maps directly to a CLI JSON body.
    """

    if not isinstance(payload, Mapping):
        raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "合成校验参数必须是 JSON 对象。")
    if "spec" in payload:
        spec = payload.get("spec")
        if not isinstance(spec, Mapping):
            raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "spec 必须是 JSON 对象。")
        if authority_catalog is None:
            authority_catalog = payload.get("authorities", [])
        if production is None:
            production = bool(payload.get("production", True))
    else:
        spec = payload
    if authority_catalog is None:
        authority_catalog = []
    if not isinstance(authority_catalog, Sequence) or isinstance(authority_catalog, (str, bytes)):
        raise LegalCaseError("INVALID_COMPOSITION_PAYLOAD", "authorities 必须是数组。")
    if production is None:
        production = True

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    test_mode = spec.get("test_mode") is True
    # TEST-ONLY fixtures exercise the same claim/source/authority gates while
    # remaining ineligible for production-ready status.
    strict_court_candidate = (production or test_mode) and spec.get("audience") == "court_candidate"
    trust_binding = spec.get("trusted_binding") if isinstance(spec.get("trusted_binding"), Mapping) else {}
    if production and test_mode:
        errors.append(_error(
            "test_only_composition_not_production",
            "TEST-ONLY合成清单不得通过生产/法院候选门禁。",
            "$.test_mode",
        ))
    if production and trust_binding.get("mode") != "catalog_case_state":
        errors.append(_error(
            "composition_trust_binding_missing",
            "正式合成清单必须绑定严格个人模板目录与当前案件G1/G2批准。",
            "$.trusted_binding",
        ))

    def valid_hash(value: Any) -> bool:
        return re.fullmatch(r"[a-fA-F0-9]{64}", str(value or "")) is not None

    roles = spec.get("template_roles") if isinstance(spec.get("template_roles"), Mapping) else {}
    layout_id = roles.get("layout_template_id")
    structure_id = roles.get("structure_template_id")
    auxiliaries = roles.get("auxiliary_templates", [])
    if not layout_id:
        errors.append(_error("layout_primary_missing", "必须指定唯一的版式主模板。", "$.template_roles.layout_template_id"))
    if not structure_id:
        errors.append(_error("structure_primary_missing", "必须指定唯一的结构/语气主范例。", "$.template_roles.structure_template_id"))
    if not isinstance(auxiliaries, list):
        errors.append(_error("auxiliary_templates_invalid", "分段辅助范例必须是数组。", "$.template_roles.auxiliary_templates"))
        auxiliaries = []
    if len(auxiliaries) > MAX_AUXILIARY_TEMPLATES:
        errors.append(_error("too_many_auxiliary_templates", "分段辅助范例最多三份。", "$.template_roles.auxiliary_templates"))
    auxiliary_ids: list[str] = []
    for index, auxiliary in enumerate(auxiliaries):
        if not isinstance(auxiliary, Mapping) or not auxiliary.get("template_id"):
            errors.append(_error("auxiliary_template_invalid", "辅助范例缺少 template_id。", f"$.template_roles.auxiliary_templates[{index}]"))
            continue
        template_id = str(auxiliary["template_id"])
        auxiliary_ids.append(template_id)
        if template_id in {layout_id, structure_id}:
            errors.append(_error("auxiliary_duplicates_primary", "辅助范例不得与主模板重复。", f"$.template_roles.auxiliary_templates[{index}]"))
        if not auxiliary.get("section_targets"):
            errors.append(_error("auxiliary_scope_missing", "辅助范例必须绑定明确章节。", f"$.template_roles.auxiliary_templates[{index}].section_targets"))
    if len(set(auxiliary_ids)) != len(auxiliary_ids):
        errors.append(_error("duplicate_auxiliary_template", "辅助范例不得重复。", "$.template_roles.auxiliary_templates"))
    if spec.get("unresolved_template_refs"):
        errors.append(_error("unresolved_template_reference", "存在未解析的明确模板引用，禁止近似替代。", "$.unresolved_template_refs"))
    if spec.get("ambiguous_template_refs"):
        errors.append(_error("ambiguous_template_reference", "模板别名匹配多个对象，必须消歧。", "$.ambiguous_template_refs"))

    selected_template_ids = {
        str(item) for item in (layout_id, structure_id) if item
    } | set(auxiliary_ids)
    reasons = spec.get("template_selection_reasons", [])
    reason_roles = {
        (str(item.get("template_id")), str(item.get("role")))
        for item in reasons if isinstance(item, Mapping)
    }
    expected_reason_roles: set[tuple[str, str]] = set()
    if layout_id:
        expected_reason_roles.add((str(layout_id), "layout_primary"))
    if structure_id:
        expected_reason_roles.add((str(structure_id), "structure_primary"))
    expected_reason_roles.update((item, "auxiliary") for item in auxiliary_ids)
    if reason_roles != expected_reason_roles or any(
        not str(item.get("reason") or "").strip() for item in reasons if isinstance(item, Mapping)
    ):
        errors.append(_error("template_selection_reason_incomplete", "主辅模板选择理由未完整冻结。", "$.template_selection_reasons"))

    expected_pairs = {
        tuple(sorted((left, right)))
        for index, left in enumerate(sorted(selected_template_ids))
        for right in sorted(selected_template_ids)[index + 1:]
    }
    assessments = spec.get("pairwise_conflict_assessments", [])
    actual_pairs: set[tuple[str, str]] = set()
    material_ids: set[str] = set()
    for index, assessment in enumerate(assessments if isinstance(assessments, list) else []):
        path = f"$.pairwise_conflict_assessments[{index}]"
        pair = assessment.get("template_ids", []) if isinstance(assessment, Mapping) else []
        if len(pair) != 2 or len(set(pair)) != 2:
            errors.append(_error("template_conflict_pair_invalid", "模板冲突评估必须绑定两个不同模板。", path))
            continue
        normalized_pair = tuple(sorted(str(item) for item in pair))
        actual_pairs.add(normalized_pair)
        if normalized_pair not in expected_pairs:
            errors.append(_error("template_conflict_pair_unselected", "冲突评估引用了未入选模板。", path))
        if not assessment.get("affects") or not str(assessment.get("reason") or "").strip():
            errors.append(_error("template_conflict_assessment_incomplete", "冲突评估缺少影响维度或理由。", path))
        if assessment.get("severity") == "material":
            material_ids.add(str(assessment.get("id") or ""))
            if assessment.get("status") != "resolved" or not str(assessment.get("resolution") or "").strip():
                errors.append(_error("material_template_conflict_unresolved", "实质模板冲突未经集中确认解决。", path))
    if actual_pairs != expected_pairs:
        errors.append(_error("template_conflict_assessment_coverage_incomplete", "入选模板未完成逐对冲突评估。", "$.pairwise_conflict_assessments"))
    conflict_batch = spec.get("template_conflict_batch") if isinstance(spec.get("template_conflict_batch"), Mapping) else {}
    if set(conflict_batch.get("assessment_ids", [])) != material_ids:
        errors.append(_error("template_conflict_batch_scope_mismatch", "集中确认批次未覆盖全部实质冲突。", "$.template_conflict_batch"))
    if material_ids and (
        conflict_batch.get("status") != "approved"
        or not conflict_batch.get("approval_id")
        or not str(conflict_batch.get("resolution") or "").strip()
    ):
        errors.append(_error("template_conflict_batch_unapproved", "所有实质冲突必须通过一个集中批次确认。", "$.template_conflict_batch"))
    qualifications = spec.get("template_qualifications", [])
    qualification_map: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    if isinstance(qualifications, list):
        for qualification in qualifications:
            if isinstance(qualification, Mapping) and qualification.get("template_id"):
                qualification_map[str(qualification["template_id"])].append(qualification)
    else:
        errors.append(_error("template_qualifications_invalid", "模板资格快照必须是数组。", "$.template_qualifications"))
    expected_roles: dict[str, set[str]] = defaultdict(set)
    if layout_id:
        expected_roles[str(layout_id)].add("layout_primary")
    if structure_id:
        expected_roles[str(structure_id)].add("structure_primary")
    for template_id in auxiliary_ids:
        expected_roles[template_id].add("auxiliary")
    for template_id in selected_template_ids:
        snapshots = qualification_map.get(template_id, [])
        if len(snapshots) != 1:
            errors.append(_error("template_qualification_missing", f"模板 {template_id} 必须有且仅有一份资格快照。", "$.template_qualifications"))
            continue
        qualification = snapshots[0]
        path = f"$.template_qualifications[{template_id}]"
        if set(qualification.get("roles", [])) != expected_roles[template_id]:
            errors.append(_error("template_role_snapshot_mismatch", f"模板 {template_id} 的角色快照不匹配。", f"{path}.roles"))
        if qualification.get("approved_final") is not True:
            errors.append(_error("template_not_approved_final", f"模板 {template_id} 不是已批准终稿。", f"{path}.approved_final"))
        authorization = qualification.get("authorization") if isinstance(qualification.get("authorization"), Mapping) else {}
        if authorization.get("status") != "verified" or not str(authorization.get("basis") or "").strip():
            errors.append(_error("template_authorization_unverified", f"模板 {template_id} 的授权未核验。", f"{path}.authorization"))
        if qualification.get("template_version") in {None, ""} or not valid_hash(qualification.get("template_hash")):
            errors.append(_error("template_version_or_hash_missing", f"模板 {template_id} 缺少版本或SHA-256。", path))
        profile = qualification.get("profile") if isinstance(qualification.get("profile"), Mapping) else {}
        approval = profile.get("approval") if isinstance(profile.get("approval"), Mapping) else {}
        if not profile.get("id") or profile.get("version") in {None, ""} or not valid_hash(profile.get("hash")):
            errors.append(_error("template_profile_snapshot_missing", f"模板 {template_id} 缺少画像ID、版本或SHA-256。", f"{path}.profile"))
        if profile.get("status") != "approved" or approval.get("status") != "approved" or not approval.get("approval_id") or not approval.get("approved_at"):
            errors.append(_error("template_profile_not_approved", f"模板 {template_id} 的画像尚未完成独立批准。", f"{path}.profile.approval"))
        if not valid_hash(profile.get("fingerprint_manifest_hash")) or not isinstance(profile.get("fingerprint_count"), int) or int(profile.get("fingerprint_count", 0)) < 1:
            errors.append(_error("profile_fingerprint_manifest_missing", f"模板 {template_id} 的画像未绑定非空串案指纹清单。", f"{path}.profile"))
        baseline = qualification.get("visual_baseline") if isinstance(qualification.get("visual_baseline"), Mapping) else {}
        if not baseline.get("id") or not valid_hash(baseline.get("hash")) or baseline.get("status") != "passed":
            errors.append(_error("visual_baseline_missing", f"模板 {template_id} 缺少已通过的视觉基线。", f"{path}.visual_baseline"))

    dependency_map = {
        (str(item.get("object_type") or ""), str(item.get("object_id") or "")): item
        for item in spec.get("dependencies", []) if isinstance(item, Mapping) and item.get("object_id")
    }
    for template_id in selected_template_ids:
        snapshots = qualification_map.get(template_id, [])
        if not snapshots:
            continue
        qualification = snapshots[0]
        dependency = dependency_map.get(("template", template_id))
        if not dependency:
            errors.append(_error("role_template_dependency_missing", f"角色模板 {template_id} 未冻结为依赖。", "$.dependencies"))
        elif (
            str(dependency.get("version")) != str(qualification.get("template_version"))
            or dependency.get("hash") != qualification.get("template_hash")
        ):
            errors.append(_error("role_template_dependency_mismatch", f"角色模板 {template_id} 的依赖版本/哈希与资格快照不一致。", "$.dependencies"))

    required_dependencies: set[tuple[str, str]] = set()
    for binding in spec.get("claim_bindings", []):
        if not isinstance(binding, Mapping):
            continue
        required_dependencies.update(("source", str(item)) for item in binding.get("source_ids", []) if str(item))
        required_dependencies.update(("evidence", str(item)) for item in binding.get("evidence_ids", []) if str(item))
        if production:
            required_dependencies.update(("fact", str(item)) for item in binding.get("fact_ids", []) if str(item))
            required_dependencies.update(("authority", str(item)) for item in binding.get("authority_ids", []) if str(item))
    if production:
        required_dependencies.update(("authority", str(item)) for item in spec.get("authority_ids", []) if str(item))
    for issue in spec.get("dangerous_issues", []):
        if not isinstance(issue, Mapping):
            continue
        if issue.get("issue_id"):
            required_dependencies.add(("issue", str(issue["issue_id"])))
        if issue.get("analysis_artifact_id"):
            required_dependencies.add(("artifact", str(issue["analysis_artifact_id"])))
    gates_for_dependencies = spec.get("gate_snapshots") if isinstance(spec.get("gate_snapshots"), Mapping) else {}
    for gate_name in ("G1_strategy", "G2_evidence"):
        gate = gates_for_dependencies.get(gate_name)
        if isinstance(gate, Mapping) and gate.get("approval_id"):
            required_dependencies.add(("approval", str(gate["approval_id"])))
    for object_type, object_id in sorted(required_dependencies):
        dependency = dependency_map.get((object_type, object_id))
        if dependency is None or dependency.get("required") is not True:
            errors.append(_error(
                "composition_dependency_missing",
                f"合成清单未冻结必要依赖 {object_type}:{object_id}。",
                "$.dependencies",
            ))

    if strict_court_candidate:
        for field, code, message in (
            ("document_goal", "document_goal_missing", "法院候选稿必须冻结文书目标。"),
            ("relief_or_position", "relief_or_position_missing", "法院候选稿必须冻结诉请或对外立场。"),
        ):
            if not str(spec.get(field) or "").strip():
                errors.append(_error(code, message, f"$.{field}"))
        if not spec.get("procedural_requirements"):
            errors.append(_error("procedural_requirements_missing", "法院候选稿必须冻结当前程序要求。", "$.procedural_requirements"))

        gates = spec.get("gate_snapshots") if isinstance(spec.get("gate_snapshots"), Mapping) else {}
        for gate_name in ("G1_strategy", "G2_evidence"):
            gate = gates.get(gate_name) if isinstance(gates.get(gate_name), Mapping) else {}
            path = f"$.gate_snapshots.{gate_name}"
            if not gate:
                errors.append(_error("gate_snapshot_missing", f"缺少 {gate_name} 批准快照。", path))
                continue
            if gate.get("decision") != "approved" or gate.get("status") != "active":
                errors.append(_error("gate_snapshot_not_active", f"{gate_name} 不是有效批准。", path))
            if not gate.get("approval_id") or not gate.get("object_id") or not isinstance(gate.get("version"), int) or int(gate.get("version", 0)) < 1:
                errors.append(_error("gate_snapshot_identity_missing", f"{gate_name} 缺少批准对象和版本。", path))
            if not valid_hash(gate.get("object_hash")) or not valid_hash(gate.get("scope_hash")):
                errors.append(_error("gate_snapshot_hash_missing", f"{gate_name} 缺少对象或范围SHA-256。", path))

        dangerous_issues = spec.get("dangerous_issues", [])
        if spec.get("requires_deep_analysis") is True and not dangerous_issues:
            errors.append(_error("dangerous_issue_analysis_missing", "标记为需深析时必须冻结危险争点及成果。", "$.dangerous_issues"))
        for index, issue in enumerate(dangerous_issues):
            path = f"$.dangerous_issues[{index}]"
            if not isinstance(issue, Mapping) or not issue.get("issue_id") or not issue.get("analysis_artifact_id"):
                errors.append(_error("dangerous_issue_snapshot_invalid", "危险争点缺少争点ID或深析成果ID。", path))
                continue
            if issue.get("status") != "completed" or issue.get("adverse_path_covered") is not True:
                errors.append(_error("dangerous_issue_analysis_incomplete", "危险争点必须完成深析并覆盖最强反方路径。", path))
            if issue.get("analysis_version") in {None, ""} or not valid_hash(issue.get("analysis_hash")):
                errors.append(_error("dangerous_issue_analysis_unfrozen", "危险争点的深析成果必须冻结版本和SHA-256。", path))

        if not spec.get("section_bindings"):
            errors.append(_error("section_bindings_missing", "法院候选稿必须登记章节、模板及命题边界。", "$.section_bindings"))

    authority_map = {
        _authority_id(item): item for item in (authority_catalog or []) if _authority_id(item)
    }
    spec_authority_ids = list(dict.fromkeys(str(item) for item in spec.get("authority_ids", []) if str(item)))
    if production:
        frozen_authorities = {
            str(item.get("authority_id")): item
            for item in trust_binding.get("authority_bindings", [])
            if isinstance(item, Mapping) and item.get("authority_id")
        }
        if set(frozen_authorities) != set(spec_authority_ids):
            errors.append(_error(
                "trusted_authority_binding_scope_mismatch",
                "正式合成的法源ID集合与案件状态法源绑定不一致。",
                "$.trusted_binding.authority_bindings",
            ))
        for authority_id in spec_authority_ids:
            authority = authority_map.get(authority_id)
            frozen = frozen_authorities.get(authority_id)
            if authority is None or frozen is None:
                continue
            version, digest = object_version_hash(dict(authority))
            if (
                str(version) != str(frozen.get("version"))
                or digest != frozen.get("authority_sha256")
            ):
                errors.append(_error(
                    "external_authority_binding_mismatch",
                    f"外部法源目录中的 {authority_id} 与案件状态冻结对象不是同一版本/哈希。",
                    "$.trusted_binding.authority_bindings",
                ))
    claim_ids: set[str] = set()
    court_claim_ids: set[str] = set()
    bound_source_ids: set[str] = set()
    for index, binding in enumerate(spec.get("claim_bindings", [])):
        if not isinstance(binding, Mapping):
            errors.append(_error("claim_binding_invalid", "命题绑定必须是对象。", f"$.claim_bindings[{index}]"))
            continue
        path = f"$.claim_bindings[{index}]"
        binding_id = str(binding.get("id") or "")
        if binding_id:
            claim_ids.add(binding_id)
        claim_type = binding.get("claim_type")
        court_candidate = binding.get("court_candidate") is True
        if court_candidate and binding_id:
            court_claim_ids.add(binding_id)
        if not binding.get("proposition"):
            errors.append(_error("claim_text_missing", "命题绑定缺少 proposition。", f"{path}.proposition"))
        if binding.get("proposition_sha256") != _sha256_text(str(binding.get("proposition") or "")):
            errors.append(_error("claim_proposition_hash_mismatch", "命题文本与冻结SHA-256不一致。", f"{path}.proposition_sha256"))
        fact_ids = [str(item) for item in binding.get("fact_ids", []) if str(item)]
        fact_bindings = [item for item in binding.get("fact_bindings", []) if isinstance(item, Mapping)]
        evidence_ids = [str(item) for item in binding.get("evidence_ids", []) if str(item)]
        evidence_bindings = [item for item in binding.get("evidence_bindings", []) if isinstance(item, Mapping)]
        if production:
            fact_binding_ids = [str(item.get("fact_id")) for item in fact_bindings if item.get("fact_id")]
            if fact_binding_ids != fact_ids or len(set(fact_binding_ids)) != len(fact_binding_ids):
                errors.append(_error(
                    "claim_fact_binding_scope_mismatch",
                    "Fact选择器与冻结FactBinding不一致。",
                    f"{path}.fact_bindings",
                ))
            evidence_binding_ids = [
                str(item.get("evidence_id")) for item in evidence_bindings if item.get("evidence_id")
            ]
            if evidence_binding_ids != evidence_ids or len(set(evidence_binding_ids)) != len(evidence_binding_ids):
                errors.append(_error(
                    "claim_evidence_binding_scope_mismatch",
                    "Evidence选择器与冻结EvidenceBinding不一致。",
                    f"{path}.evidence_bindings",
                ))
            if claim_type in {"fact", "mixed"} and len(fact_bindings) != 1:
                errors.append(_error(
                    "production_claim_fact_binding_required",
                    "正式事实/混合命题必须精确绑定一个confirmed Fact。",
                    f"{path}.fact_bindings",
                ))
            if claim_type in {"fact", "mixed"} and not evidence_bindings:
                errors.append(_error(
                    "production_claim_evidence_binding_required",
                    "正式事实/混合命题必须绑定案件状态中的approved Evidence。",
                    f"{path}.evidence_bindings",
                ))
            fact_map = {str(item.get("fact_id")): item for item in fact_bindings}
            record_hashes: set[str] = set()
            for locator in binding.get("source_locators", []):
                if not isinstance(locator, Mapping):
                    continue
                fact_binding = fact_map.get(str(locator.get("fact_id")))
                expected_record_hash = _canonical_hash({
                    "fact_id": locator.get("fact_id"),
                    "fact_sha256": locator.get("fact_sha256"),
                    "proposition_sha256": binding.get("proposition_sha256"),
                    "source_id": locator.get("source_id"),
                    "locator": locator.get("locator"),
                    "record_kind": locator.get("record_kind"),
                    "quote_sha256": locator.get("quote_sha256"),
                })
                if (
                    fact_binding is None
                    or locator.get("fact_sha256") != fact_binding.get("fact_sha256")
                    or fact_binding.get("proposition_sha256") != binding.get("proposition_sha256")
                    or fact_binding.get("statement_sha256") != binding.get("proposition_sha256")
                    or locator.get("record_sha256") != expected_record_hash
                ):
                    errors.append(_error(
                        "claim_fact_source_record_binding_mismatch",
                        "命题、Fact对象哈希与来源记录哈希未形成精确闭环。",
                        f"{path}.source_locators",
                    ))
                record_hashes.add(str(locator.get("record_sha256") or ""))
            for fact_binding in fact_bindings:
                if set(fact_binding.get("source_record_sha256s", [])) != record_hashes:
                    errors.append(_error(
                        "claim_fact_record_set_mismatch",
                        "FactBinding记录集与来源定位记录集不一致。",
                        f"{path}.fact_bindings",
                    ))
            for evidence_binding in evidence_bindings:
                if not set(evidence_binding.get("source_record_sha256s", [])) <= record_hashes:
                    errors.append(_error(
                        "claim_evidence_record_set_mismatch",
                        "EvidenceBinding引用了不属于该Fact命题的来源记录。",
                        f"{path}.evidence_bindings",
                    ))
            expected_basis = _canonical_hash({
                "claim_id": binding_id,
                "proposition_sha256": binding.get("proposition_sha256"),
                "fact_bindings": fact_bindings,
                "evidence_bindings": evidence_bindings,
                "authority_locators": binding.get("authority_locators", []),
            })
            if binding.get("proposition_basis_sha256") != expected_basis:
                errors.append(_error(
                    "claim_proposition_basis_hash_mismatch",
                    "命题与Fact/Evidence/Authority绑定投影哈希不一致。",
                    f"{path}.proposition_basis_sha256",
                ))
        if strict_court_candidate and court_candidate and binding.get("verification_status") != "verified":
            errors.append(_error("court_claim_not_verified", "进入法院候选的命题必须标记为 verified。", f"{path}.verification_status"))
        source_ids = [str(item) for item in binding.get("source_ids", []) if str(item)]
        bound_source_ids.update(source_ids)
        source_locators = binding.get("source_locators", [])
        if court_candidate and claim_type in {"fact", "mixed"} and not source_ids:
            errors.append(_error("fact_source_missing", "法院候选中的事实命题必须绑定材料来源。", f"{path}.source_ids"))
        if strict_court_candidate and court_candidate and claim_type in {"fact", "mixed"}:
            locator_map = {
                str(item.get("source_id")): item
                for item in source_locators if isinstance(item, Mapping) and item.get("source_id")
            }
            for source_id in source_ids:
                locator = locator_map.get(source_id)
                if (
                    not locator
                    or not str(locator.get("locator") or "").strip()
                    or locator.get("verified") is not True
                    or not re.fullmatch(r"[a-f0-9]{64}", str(locator.get("record_sha256") or ""))
                ):
                    errors.append(_error("source_locator_missing_or_unverified", f"事实来源 {source_id} 缺少已核验的精确定位。", f"{path}.source_locators"))
            if set(locator_map) - set(source_ids):
                errors.append(_error("source_locator_not_bound", "source_locators 含有未绑定到本命题的来源。", f"{path}.source_locators"))
        bound_authorities = [str(item) for item in binding.get("authority_ids", []) if str(item)]
        if court_candidate and claim_type in {"law", "mixed"} and not bound_authorities:
            errors.append(_error("legal_authority_missing", "法院候选中的法律命题必须绑定法源。", f"{path}.authority_ids"))
        if strict_court_candidate and court_candidate and claim_type in {"law", "mixed"}:
            authority_locators = binding.get("authority_locators", [])
            locator_map = {
                str(item.get("authority_id")): item
                for item in authority_locators if isinstance(item, Mapping) and item.get("authority_id")
            }
            for authority_id in bound_authorities:
                locator = locator_map.get(authority_id)
                if (
                    not locator
                    or not str(locator.get("locator") or "").strip()
                    or not str(locator.get("citation") or "").strip()
                    or locator.get("verified") is not True
                    or not re.fullmatch(r"[a-f0-9]{64}", str(locator.get("holding_sha256") or ""))
                ):
                    errors.append(_error("authority_locator_missing_or_unverified", f"法律来源 {authority_id} 缺少已核验的引文和精确定位。", f"{path}.authority_locators"))
            if set(locator_map) - set(bound_authorities):
                errors.append(_error("authority_locator_not_bound", "authority_locators 含有未绑定到本命题的法源。", f"{path}.authority_locators"))
        for authority_id in bound_authorities:
            if authority_id not in spec_authority_ids:
                errors.append(_error("authority_not_in_spec", f"命题引用的法源 {authority_id} 未进入合成清单。", f"{path}.authority_ids"))
                continue
            authority = authority_map.get(authority_id)
            if authority is None:
                errors.append(_error("authority_not_found", f"法源目录不存在 {authority_id}。", f"{path}.authority_ids"))
            elif strict_court_candidate and court_candidate and not _authority_eligible(authority, test_mode):
                errors.append(_error("authority_not_production_eligible", f"法源 {authority_id} 未满足正式稿核验门禁。", f"{path}.authority_ids"))
            elif strict_court_candidate and court_candidate:
                locator = next(
                    (
                        item for item in binding.get("authority_locators", [])
                        if isinstance(item, Mapping) and str(item.get("authority_id")) == authority_id
                    ),
                    {},
                )
                passage_hashes = authority.get("passage_hashes") if isinstance(authority.get("passage_hashes"), Mapping) else {}
                if passage_hashes.get(str(locator.get("locator") or "")) != locator.get("holding_sha256"):
                    errors.append(_error(
                        "authority_holding_hash_drift",
                        f"法源 {authority_id} 的裁判要旨/引文定位哈希与当前核验全文不一致。",
                        f"{path}.authority_locators",
                    ))

    if strict_court_candidate:
        gates = spec.get("gate_snapshots") if isinstance(spec.get("gate_snapshots"), Mapping) else {}
        g2 = gates.get("G2_evidence") if isinstance(gates.get("G2_evidence"), Mapping) else {}
        approved_source_ids = set(str(item) for item in g2.get("approved_source_ids", []) if str(item))
        for source_id in sorted(bound_source_ids - approved_source_ids):
            errors.append(_error("source_outside_g2_scope", f"来源 {source_id} 不在冻结的G2批准范围内。", "$.gate_snapshots.G2_evidence.approved_source_ids"))

        assigned_claim_ids: set[str] = set()
        for index, section in enumerate(spec.get("section_bindings", [])):
            path = f"$.section_bindings[{index}]"
            if not isinstance(section, Mapping) or not section.get("section_id") or not str(section.get("purpose") or "").strip():
                errors.append(_error("section_binding_invalid", "章节绑定缺少章节ID或诉讼功能。", path))
                continue
            if str(section.get("template_id") or "") not in selected_template_ids:
                errors.append(_error("section_template_not_selected", "章节引用了未入选的模板。", f"{path}.template_id"))
            section_claims = set(str(item) for item in section.get("claim_ids", []) if str(item))
            assigned_claim_ids.update(section_claims)
            if not section_claims or section_claims - claim_ids:
                errors.append(_error("section_claim_binding_invalid", "章节必须绑定一个或多个已登记命题。", f"{path}.claim_ids"))
            if set(str(item) for item in section.get("authority_ids", []) if str(item)) - set(spec_authority_ids):
                errors.append(_error("section_authority_not_in_spec", "章节引用了合成清单外的法源。", f"{path}.authority_ids"))
        if court_claim_ids - assigned_claim_ids:
            errors.append(_error("court_claim_not_assigned_to_section", "每个法院候选命题必须进入明确章节。", "$.section_bindings"))

    treatments = {
        str(item.get("authority_id")): item
        for item in spec.get("adverse_treatments", []) if isinstance(item, Mapping) and item.get("authority_id")
    }
    for authority_id in spec_authority_ids:
        authority = authority_map.get(authority_id)
        if authority is None:
            errors.append(_error("authority_not_found", f"法源目录不存在 {authority_id}。", "$.authority_ids"))
            continue
        if strict_court_candidate and not _authority_eligible(authority, test_mode):
            # Research leads may exist outside court-candidate ClaimBindings,
            # but cannot make a production composition ready.
            errors.append(_error("authority_not_production_eligible", f"法源 {authority_id} 未满足正式稿核验门禁。", "$.authority_ids"))
        material_adverse = authority.get("major_adverse") is True or (
            authority.get("adverse") is True and authority.get("material", True) is True
        )
        if material_adverse:
            treatment = treatments.get(authority_id)
            if not treatment:
                errors.append(_error("material_adverse_omitted", f"重大不利法源 {authority_id} 未登记处理。", "$.adverse_treatments"))
            elif treatment.get("status") != "resolved" or treatment.get("disposition") not in {
                "disclose", "distinguish", "accept"
            } or not str(treatment.get("analysis") or "").strip():
                errors.append(_error("material_adverse_unresolved", f"重大不利法源 {authority_id} 尚未完成披露、区分或接受分析。", "$.adverse_treatments"))

    for index, conflict in enumerate(spec.get("risk_conflicts", [])):
        if not isinstance(conflict, Mapping):
            errors.append(_error("risk_conflict_invalid", "风险冲突必须是对象。", f"$.risk_conflicts[{index}]"))
            continue
        if conflict.get("severity") == "material" and conflict.get("status") != "resolved":
            errors.append(_error("material_risk_conflict_unresolved", "会改变诉请、立场、证据、法源或对外风险的冲突必须先解决。", f"$.risk_conflicts[{index}]"))

    for index, dependency in enumerate(spec.get("dependencies", [])):
        if not isinstance(dependency, Mapping) or not dependency.get("object_id"):
            errors.append(_error("dependency_invalid", "依赖快照缺少 object_id。", f"$.dependencies[{index}]"))
            continue
        version = dependency.get("version")
        if version in {None, ""} or (isinstance(version, int) and version < 1):
            errors.append(_error("dependency_version_missing", "依赖快照必须记录非空版本。", f"$.dependencies[{index}].version"))
        if production and not re.fullmatch(r"[a-fA-F0-9]{64}", str(dependency.get("hash") or "")):
            errors.append(_error("dependency_hash_missing", "正式合成必须冻结每个依赖的 SHA-256。", f"$.dependencies[{index}].hash"))

    if spec.get("stale") is True or spec.get("status") == "stale":
        errors.append(_error("composition_stale", "合成清单已经失效，必须重新构建。", "$.stale"))
    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "status": "ready" if ok else "blocked",
    }


_CASE_NUMBER_PATTERN = re.compile(r"[（(]\s*\d{4}\s*[）)][^\s，,。；;：:]{1,28}?\d+号")
_STATUTE_PATTERN = re.compile(
    r"《[^》\r\n]{2,80}》第[零〇一二三四五六七八九十百千万两\d]+条"
    r"(?:之[零〇一二三四五六七八九十百千万两\d]+)?"
)
_ABBREVIATED_STATUTE_PATTERN = re.compile(
    r"(?:民法典|民事诉讼法|民诉法|刑法|刑事诉讼法|刑诉法|著作权法|商标法|反不正当竞争法|公司法|合同法)"
    r"第[零〇一二三四五六七八九十百千万两\d]+条(?:之[零〇一二三四五六七八九十百千万两\d]+)?"
)


def normalize_citation(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value))
    value = re.sub(r"\s+", "", value)
    return value.replace("（", "(").replace("）", ")")


def _catalog_citations(authority: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("case_number", "official_citation", "citation"):
        if authority.get(key):
            raw = str(authority[key])
            case_matches = _CASE_NUMBER_PATTERN.findall(raw)
            statute_matches = _STATUTE_PATTERN.findall(raw)
            values.extend(case_matches or statute_matches or [raw])
    for key in ("citations", "citation_aliases", "statute_citations", "article_citations"):
        raw_values = authority.get(key, [])
        if isinstance(raw_values, list):
            values.extend(str(item) for item in raw_values if str(item).strip())
    return list(dict.fromkeys(normalize_citation(item) for item in values if normalize_citation(item)))


def citation_audit(
    text: str,
    authority_catalog: Sequence[Mapping[str, Any]],
    *,
    allowed_authority_ids: Sequence[str] | None = None,
    production: bool = True,
) -> dict[str, Any]:
    """Reconcile every visible case number/statute locator with the catalog."""

    citation_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for authority in authority_catalog:
        for citation in _catalog_citations(authority):
            citation_index[citation].append(authority)
    found = list(dict.fromkeys(
        normalize_citation(item)
        for item in [
            *_CASE_NUMBER_PATTERN.findall(text),
            *_STATUTE_PATTERN.findall(text),
            *_ABBREVIATED_STATUTE_PATTERN.findall(text),
        ]
    ))
    allowed = None if allowed_authority_ids is None else set(str(item) for item in allowed_authority_ids)
    findings: list[dict[str, Any]] = []
    used_ids: list[str] = []
    for citation in found:
        matches = citation_index.get(citation, [])
        kind = "case_number" if _CASE_NUMBER_PATTERN.fullmatch(citation) else "statute"
        if not matches:
            findings.append({"code": "citation_not_registered", "kind": kind, "citation": citation})
            continue
        ids = list(dict.fromkeys(_authority_id(item) for item in matches if _authority_id(item)))
        if len(ids) != 1:
            findings.append({"code": "citation_catalog_ambiguous", "kind": kind, "citation": citation, "authority_ids": ids})
            continue
        authority_id = ids[0]
        used_ids.append(authority_id)
        authority = matches[0]
        if allowed is not None and authority_id not in allowed:
            findings.append({"code": "citation_not_bound", "kind": kind, "citation": citation, "authority_id": authority_id})
        if production and not is_verified_production_authority(authority):
            findings.append({"code": "citation_not_production_eligible", "kind": kind, "citation": citation, "authority_id": authority_id})
    return {
        "ok": not findings,
        "citations": found,
        "used_authority_ids": list(dict.fromkeys(used_ids)),
        "findings": findings,
    }


def _document_visual_inventory(candidate: Path) -> dict[str, Any]:
    suffix = candidate.suffix.casefold()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(candidate) as archive:
                names = archive.namelist()
                has_media = any(name.startswith("word/media/") and not name.endswith("/") for name in names)
                xml_names = [name for name in names if name.startswith("word/") and name.endswith(".xml")]
                xml = b"\n".join(archive.read(name) for name in xml_names)
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            raise LegalCaseError("DOCX_READ_FAILED", f"无法读取 DOCX 视觉对象：{candidate}") from exc
        has_drawing = bool(re.search(
            br"<(?:w:drawing|w:pict|a:graphic|pic:pic|v:shape|wps:wsp)\b",
            xml,
        ))
        return {
            "format": "docx",
            "has_visual_objects": has_media or has_drawing,
            "page_count": None,
            "pages": [],
        }
    if suffix == ".pdf":
        try:
            fitz = _load_pymupdf()

            document = fitz.open(candidate)
            pages: list[dict[str, Any]] = []
            has_visual_objects = False
            for index in range(document.page_count):
                page = document.load_page(index)
                images = page.get_images(full=True)
                drawings = page.get_drawings()
                has_page_visual = bool(images or drawings)
                has_visual_objects = has_visual_objects or has_page_visual
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                render_sha256 = hashlib.sha256(pixmap.tobytes("png")).hexdigest()
                pages.append({
                    "page_number": index + 1,
                    "has_visual_objects": has_page_visual,
                    "render_sha256": render_sha256,
                })
            document.close()
        except Exception as exc:
            raise LegalCaseError(
                "PDF_VISUAL_INVENTORY_FAILED",
                f"无法检查PDF图片/绘图对象：{candidate}",
            ) from exc
        return {
            "format": "pdf",
            "has_visual_objects": has_visual_objects,
            "page_count": len(pages),
            "pages": pages,
        }
    return {"format": suffix.lstrip("."), "has_visual_objects": False, "page_count": None, "pages": []}


def _read_document_text(
    document_path_or_text: str | Path,
    *,
    reject_visual_objects: bool = False,
) -> str:
    """Read plain text/Markdown/DOCX, or treat a non-path string as text."""

    if isinstance(document_path_or_text, Path):
        candidate = document_path_or_text
        explicit_path = True
    elif not isinstance(document_path_or_text, str):
        raise LegalCaseError("INVALID_DOCUMENT_INPUT", "文书输入必须是路径或文本。")
    else:
        explicit_path = False
        candidate = Path(document_path_or_text)
        try:
            explicit_path = candidate.exists()
        except OSError:
            explicit_path = False
    if not explicit_path:
        return str(document_path_or_text)
    if not candidate.exists() or not candidate.is_file():
        raise LegalCaseError("DOCUMENT_NOT_FOUND", f"文书不存在：{candidate}")
    suffix = candidate.suffix.casefold()
    if reject_visual_objects and suffix in {".docx", ".pdf"}:
        visual_inventory = _document_visual_inventory(candidate)
        if visual_inventory["has_visual_objects"]:
            raise LegalCaseError(
                "VISUAL_TEXT_MANIFEST_REQUIRED",
                "文书含图片或绘图对象；必须先绑定逐页OCR/视觉文本与确定性渲染哈希，不能仅审计文本层。",
                {"document": str(candidate), "format": visual_inventory["format"]},
            )
    if suffix in {".txt", ".md", ".json", ".xml"}:
        try:
            return candidate.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise LegalCaseError("DOCUMENT_DECODE_FAILED", f"文书不是可解码的 UTF-8 文本：{candidate}") from exc
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(candidate) as archive:
                parts = [
                    name for name in archive.namelist()
                    if name == "word/document.xml"
                    or re.fullmatch(r"word/(?:header|footer|footnotes|endnotes)\d*\.xml", name)
                ]
                xml_text = "\n".join(archive.read(name).decode("utf-8") for name in parts)
        except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
            raise LegalCaseError("DOCX_READ_FAILED", f"无法读取 DOCX 正文：{candidate}") from exc
        xml_text = re.sub(r"</w:(?:p|tr|tc)>", "\n", xml_text)
        return re.sub(r"<[^>]+>", "", xml_text)
    if suffix == ".pdf":
        try:
            fitz = _load_pymupdf()

            document = fitz.open(candidate)
            if document.page_count < 1:
                raise LegalCaseError("PDF_PAGE_COUNT_INVALID", f"PDF没有页面：{candidate}")
            pages = [document.load_page(index).get_text("text") for index in range(document.page_count)]
            document.close()
        except LegalCaseError:
            raise
        except Exception as exc:
            raise LegalCaseError(
                "PDF_TEXT_EXTRACTOR_UNAVAILABLE",
                f"无法使用本地PDF文本引擎读取最终PDF：{candidate}",
            ) from exc
        empty_pages = [index + 1 for index, text in enumerate(pages) if not normalize_fingerprint_text(text)]
        if empty_pages:
            raise LegalCaseError(
                "PDF_OCR_REQUIRED",
                "PDF存在无可校验文本层的页面，必须OCR并逐页复核后才能执行引用/串案审计。",
                {"document": str(candidate), "page_numbers": empty_pages},
            )
        return "\n\f\n".join(pages)
    raise LegalCaseError(
        "UNSUPPORTED_DOCUMENT_FORMAT",
        f"引用与串案审计当前支持纯文本、Markdown、DOCX和含文本层PDF：{candidate.suffix or '(无扩展名)'}",
    )


def validate_artifact_claim_map(
    document_path: str | Path,
    claim_map: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify exact output-segment hashes and their ClaimBinding coverage.

    Every non-empty segment in a court-facing artifact must appear exactly
    once.  Substantive segments bind one or more frozen claims; headings,
    signatures and other non-substantive segments require an explicit reason.
    This detects output changes after mapping and prevents unaccounted factual
    or numeric paragraphs from bypassing claim/source/authority review.
    """
    findings: list[dict[str, Any]] = []
    # Schema is the first gate: malformed/extra/mistyped fields must not be
    # interpreted by permissive Python truthiness or partial semantic checks.
    schema = load_json(ARTIFACT_CLAIM_MAP_SCHEMA_PATH)
    schema_errors = validate_against_schema(dict(claim_map), schema)
    if schema_errors:
        return {
            "ok": False,
            "document_sha256": None,
            "segment_count": 0,
            "mapped_claim_ids": [],
            "findings": [{
                "code": "artifact_claim_map_schema_invalid",
                "errors": schema_errors,
            }],
        }
    path = Path(document_path).resolve()
    if not path.is_file():
        raise LegalCaseError("DOCUMENT_NOT_FOUND", f"文书不存在：{path}")
    document_sha256 = sha256_file(path)
    visual_inventory = _document_visual_inventory(path)
    text = _read_document_text(path)
    segments = [item.strip() for item in re.split(r"[\r\n\f]+", text) if normalize_fingerprint_text(item)]
    if claim_map.get("composition_spec_id") != spec.get("id"):
        findings.append({"code": "artifact_claim_map_spec_mismatch"})
    if claim_map.get("document_sha256") != document_sha256:
        findings.append({"code": "artifact_claim_map_document_drift"})
    if claim_map.get("artifact_sha256") != document_sha256:
        findings.append({"code": "artifact_claim_map_artifact_hash_mismatch"})
    production = (
        spec.get("test_mode") is not True
        and isinstance(spec.get("trusted_binding"), Mapping)
        and spec.get("trusted_binding", {}).get("mode") == "catalog_case_state"
    )
    if production and state is None:
        findings.append({"code": "artifact_claim_map_state_required"})
    if state is not None:
        artifacts = [
            item for item in state.get("artifacts", [])
            if item.get("id") == claim_map.get("artifact_id")
        ]
        if len(artifacts) != 1:
            findings.append({"code": "artifact_claim_map_state_artifact_not_unique"})
        else:
            artifact = artifacts[0]
            if (
                str(artifact.get("version")) != str(claim_map.get("artifact_version"))
                or artifact.get("sha256") != claim_map.get("artifact_sha256")
                or artifact.get("sha256") != document_sha256
                or artifact.get("composition_spec_id") != spec.get("id")
                or artifact.get("audience") != "court_candidate"
                or artifact.get("stale") is True
                or artifact.get("status") == "stale"
            ):
                findings.append({"code": "artifact_claim_map_state_artifact_mismatch"})
    claim_by_id = {
        str(item.get("id")): item for item in spec.get("claim_bindings", []) if item.get("id")
    }
    claim_ids = set(claim_by_id)
    court_claim_ids = {
        str(item.get("id")) for item in spec.get("claim_bindings", [])
        if item.get("id") and item.get("court_candidate") is True
    }
    entries = claim_map.get("segments")
    if not isinstance(entries, list):
        return {"ok": False, "findings": findings + [{"code": "artifact_claim_map_segments_invalid"}]}
    by_locator: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    used_claim_ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, Mapping):
            by_locator[str(entry.get("locator") or "")].append(entry)
    expected_locators = {f"segment:{index}" for index in range(1, len(segments) + 1)}
    if set(by_locator) != expected_locators or any(len(items) != 1 for items in by_locator.values()):
        findings.append({"code": "artifact_claim_map_coverage_incomplete"})
    for index, segment in enumerate(segments, start=1):
        locator = f"segment:{index}"
        matches = by_locator.get(locator, [])
        if len(matches) != 1:
            continue
        entry = matches[0]
        if entry.get("text_sha256") != _sha256_text(segment):
            findings.append({"code": "artifact_claim_segment_hash_mismatch", "locator": locator})
        bound_claims = {str(item) for item in entry.get("claim_ids", []) if str(item)}
        if bound_claims - claim_ids:
            findings.append({"code": "artifact_claim_id_unknown", "locator": locator})
        if entry.get("substantive") is True:
            if not bound_claims:
                findings.append({"code": "artifact_substantive_segment_unbound", "locator": locator})
            invalid_claims = sorted(
                claim_id for claim_id in bound_claims
                if claim_by_id.get(claim_id, {}).get("court_candidate") is not True
                or claim_by_id.get(claim_id, {}).get("verification_status") != "verified"
            )
            if invalid_claims:
                findings.append({
                    "code": "artifact_substantive_claim_not_verified_court_candidate",
                    "locator": locator,
                    "claim_ids": invalid_claims,
                })
            if entry.get("non_substantive_reason") is not None:
                findings.append({"code": "artifact_substantive_reason_must_be_null", "locator": locator})
            used_claim_ids.update(bound_claims)
        else:
            if bound_claims:
                findings.append({"code": "artifact_non_substantive_claim_ids_forbidden", "locator": locator})
            if entry.get("non_substantive_reason") not in NON_SUBSTANTIVE_REASONS:
                findings.append({"code": "artifact_non_substantive_reason_invalid", "locator": locator})

    visual_manifest = claim_map.get("visual_text_manifest")
    if visual_inventory["has_visual_objects"] and not isinstance(visual_manifest, Mapping):
        findings.append({"code": "artifact_visual_text_manifest_required"})
    if isinstance(visual_manifest, Mapping):
        if visual_manifest.get("document_sha256") != document_sha256:
            findings.append({"code": "artifact_visual_manifest_document_mismatch"})
        visual_pages = visual_manifest.get("pages", [])
        expected_page_numbers = list(range(1, int(visual_manifest.get("page_count") or 0) + 1))
        actual_page_numbers = [item.get("page_number") for item in visual_pages]
        if actual_page_numbers != expected_page_numbers or len(visual_pages) != visual_manifest.get("page_count"):
            findings.append({"code": "artifact_visual_manifest_page_coverage_incomplete"})
        if visual_inventory["format"] == "pdf":
            if visual_manifest.get("render_method") != "pymupdf_144dpi_rgb":
                findings.append({"code": "artifact_visual_manifest_render_method_mismatch"})
            if visual_manifest.get("page_count") != visual_inventory.get("page_count"):
                findings.append({"code": "artifact_visual_manifest_page_count_mismatch"})
            actual_render_hashes = {
                item["page_number"]: item["render_sha256"] for item in visual_inventory["pages"]
            }
        elif visual_inventory["format"] == "docx":
            if visual_manifest.get("render_method") != "external_verified_page_png":
                findings.append({"code": "artifact_visual_manifest_render_method_mismatch"})
            actual_render_hashes = {}
        else:
            actual_render_hashes = {}
        for page in visual_pages:
            page_number = page.get("page_number")
            if page.get("visual_text_sha256") != _sha256_text(str(page.get("visual_text") or "")):
                findings.append({"code": "artifact_visual_text_hash_mismatch", "page_number": page_number})
            if not normalize_fingerprint_text(str(page.get("visual_text") or "")):
                findings.append({"code": "artifact_visual_text_empty", "page_number": page_number})
            visual_claim_ids = {str(item) for item in page.get("claim_ids", []) if str(item)}
            invalid_visual_claims = sorted(
                claim_id for claim_id in visual_claim_ids
                if claim_by_id.get(claim_id, {}).get("court_candidate") is not True
                or claim_by_id.get(claim_id, {}).get("verification_status") != "verified"
            )
            if invalid_visual_claims:
                findings.append({
                    "code": "artifact_visual_claim_not_verified_court_candidate",
                    "page_number": page_number,
                    "claim_ids": invalid_visual_claims,
                })
            used_claim_ids.update(visual_claim_ids)
            if visual_inventory["format"] == "pdf":
                if page.get("render_sha256") != actual_render_hashes.get(page_number):
                    findings.append({"code": "artifact_visual_render_hash_mismatch", "page_number": page_number})
            elif visual_inventory["format"] == "docx":
                render_path_value = str(page.get("render_path") or "").strip()
                render_path = Path(render_path_value)
                if not render_path.is_absolute():
                    render_path = (path.parent / render_path).resolve()
                if not render_path.is_file() or sha256_file(render_path) != page.get("render_sha256"):
                    findings.append({"code": "artifact_visual_render_hash_mismatch", "page_number": page_number})
    if court_claim_ids - used_claim_ids:
        findings.append({"code": "artifact_court_claim_not_rendered", "claim_ids": sorted(court_claim_ids - used_claim_ids)})
    return {
        "ok": not findings,
        "document_sha256": document_sha256,
        "segment_count": len(segments),
        "mapped_claim_ids": sorted(used_claim_ids),
        "visual_inventory": visual_inventory,
        "findings": findings,
    }


def audit_citations(
    document_path_or_text: str | Path,
    authorities: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> dict[str, Any]:
    """CLI-friendly citation audit wrapper.

    ``authorities`` may be a catalog array or an object containing
    ``authorities``, ``allowed_authority_ids`` and ``production``.
    """

    if isinstance(authorities, Mapping):
        catalog = authorities.get("authorities", [])
        allowed = authorities.get("allowed_authority_ids")
        production = bool(authorities.get("production", True))
        test_mode = authorities.get("test_mode") is True
    else:
        catalog = authorities
        allowed = None
        production = True
        test_mode = False
    if not isinstance(catalog, Sequence) or isinstance(catalog, (str, bytes)):
        raise LegalCaseError("INVALID_AUTHORITY_CATALOG", "法源目录必须是数组。")
    result = citation_audit(
        _read_document_text(document_path_or_text, reject_visual_objects=True),
        catalog,
        allowed_authority_ids=allowed,
        production=False if test_mode else production,
    )
    if test_mode:
        # citation_audit's catalog reconciliation remains identical; replace
        # production eligibility findings with the explicit TEST-ONLY gate.
        result["findings"] = [
            item for item in result["findings"] if item.get("code") != "citation_not_production_eligible"
        ]
        used = set(result["used_authority_ids"])
        by_id = {_authority_id(item): item for item in catalog if _authority_id(item)}
        for authority_id in sorted(used):
            if not is_verified_test_authority(by_id.get(authority_id, {})):
                result["findings"].append({
                    "code": "citation_not_test_eligible", "authority_id": authority_id,
                })
        result["ok"] = not result["findings"]
    return result


def normalize_fingerprint_text(value: str) -> str:
    """Normalize without retaining sensitive exemplar text in the fingerprint."""

    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)


def build_exemplar_fingerprints(
    exemplar_id: str,
    values: Mapping[str, Iterable[str]],
) -> list[dict[str, Any]]:
    """Create hash-only fingerprints for old names, numbers, sums, and prose."""

    result: list[dict[str, Any]] = []
    for kind, entries in values.items():
        for entry in entries:
            normalized = normalize_fingerprint_text(str(entry))
            if not normalized:
                continue
            result.append({
                "exemplar_id": exemplar_id,
                "kind": str(kind),
                "sha256": _sha256_text(normalized),
                "normalized_length": len(normalized),
            })
    return result


def build_template_fingerprint_manifest(
    template_id: str,
    source_path: str | Path,
    *,
    profile_id: str,
    profile_sha256: str,
    source_sha256: str,
    test_only: bool = False,
) -> dict[str, Any]:
    """Derive a deterministic, hash-only leakage manifest for registration.

    The extractor deliberately stores no exemplar plaintext.  It fingerprints
    full non-trivial paragraphs plus recognizable case numbers, monetary
    amounts and organization-name candidates.  A lawyer can later replace or
    extend the hash-only manifest, but an empty manifest is never eligible for
    complex composition.
    """
    text = _read_document_text(Path(source_path))
    lines = [line.strip() for line in text.splitlines() if normalize_fingerprint_text(line)]
    values: dict[str, list[str]] = {
        "characteristic_phrase": [
            line for line in lines
            if len(normalize_fingerprint_text(line)) >= 6
        ],
        "case_number": re.findall(r"[（(]\d{4}[）)][^\s，。；;]{2,40}?号", text),
        "amount": re.findall(r"\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|元)", text),
        "name": re.findall(r"[一-鿿A-Za-z0-9]{2,36}(?:公司|法院|律师事务所|厂|中心)", text),
    }
    fingerprints = build_exemplar_fingerprints(template_id, values)
    # Remove duplicates deterministically without exposing source text.
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    for record in fingerprints:
        key = (str(record["kind"]), str(record["sha256"]), int(record["normalized_length"]))
        unique[key] = record
    records = [unique[key] for key in sorted(unique)]
    if not records:
        raise LegalCaseError(
            "FINGERPRINT_MANIFEST_EMPTY",
            "写作模板未能生成任何串案指纹，禁止登记为可合成模板。",
        )
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "template_id": template_id,
        "profile_id": profile_id,
        "profile_sha256": profile_sha256,
        "source_sha256": source_sha256,
        "fingerprints": records,
    }
    if test_only:
        manifest.update({
            "test_only": True,
            "notice": "TEST-ONLY：仅用于离线回归的哈希指纹清单。",
        })
    return manifest


def exemplar_leak_check(
    candidate_text: str,
    fingerprints: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Detect exact normalized leaks without storing old-case plaintext."""

    normalized = normalize_fingerprint_text(candidate_text)
    by_length: dict[int, set[str]] = defaultdict(set)
    record_index: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in fingerprints:
        try:
            length = int(record.get("normalized_length", 0))
        except (TypeError, ValueError):
            continue
        digest = str(record.get("sha256") or "").lower()
        if length < 2 or not re.fullmatch(r"[a-f0-9]{64}", digest):
            continue
        by_length[length].add(digest)
        record_index[(length, digest)].append(record)
    findings: list[dict[str, Any]] = []
    for length, expected_hashes in sorted(by_length.items()):
        if length > len(normalized):
            continue
        matched: set[str] = set()
        for start in range(0, len(normalized) - length + 1):
            digest = _sha256_text(normalized[start:start + length])
            if digest in expected_hashes:
                matched.add(digest)
        for digest in sorted(matched):
            for record in record_index[(length, digest)]:
                findings.append({
                    "code": "exemplar_fingerprint_leak",
                    "exemplar_id": record.get("exemplar_id"),
                    "kind": record.get("kind"),
                    "sha256": digest,
                    "normalized_length": length,
                })
    return {"ok": not findings, "findings": findings}


def check_exemplar_leaks(
    document_path_or_text: str | Path,
    fingerprint_manifest: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> dict[str, Any]:
    """CLI-friendly hash-only exemplar leakage gate."""

    if isinstance(fingerprint_manifest, Mapping):
        fingerprints = fingerprint_manifest.get("fingerprints", [])
        profile_id = fingerprint_manifest.get("profile_id")
        profile_hash = fingerprint_manifest.get("profile_hash")
    else:
        fingerprints = fingerprint_manifest
        profile_id = None
        profile_hash = None
    if not isinstance(fingerprints, Sequence) or isinstance(fingerprints, (str, bytes)):
        raise LegalCaseError("INVALID_FINGERPRINT_MANIFEST", "串案指纹清单必须是数组。")
    if not fingerprints:
        return {
            "ok": False,
            "profile_id": profile_id,
            "profile_hash": profile_hash,
            "findings": [{"code": "fingerprint_manifest_empty"}],
        }
    result = exemplar_leak_check(
        _read_document_text(document_path_or_text, reject_visual_objects=True),
        fingerprints,
    )
    result["profile_id"] = profile_id
    result["profile_hash"] = profile_hash
    return result


def detect_composition_staleness(
    spec: Mapping[str, Any],
    current_objects: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare the frozen dependency snapshots with current version/hash data."""

    if isinstance(current_objects, Mapping):
        current_map = {str(key): value for key, value in current_objects.items()}
    else:
        current_map = {
            str(item.get("id") or item.get("object_id")): item
            for item in current_objects if item.get("id") or item.get("object_id")
        }
    changes: list[dict[str, Any]] = []
    for dependency in spec.get("dependencies", []):
        object_id = str(dependency.get("object_id") or "")
        current = current_map.get(object_id)
        if current is None:
            if dependency.get("required", True) is True:
                changes.append({"object_id": object_id, "reason": "dependency_missing"})
            continue
        old_version = dependency.get("version")
        new_version = current.get("version")
        old_hash = dependency.get("hash")
        new_hash = current.get("hash", current.get("sha256"))
        if old_version != new_version:
            changes.append({
                "object_id": object_id,
                "reason": "version_changed",
                "expected_version": old_version,
                "current_version": new_version,
            })
        elif old_hash and old_hash != new_hash:
            changes.append({
                "object_id": object_id,
                "reason": "hash_changed",
                "expected_hash": old_hash,
                "current_hash": new_hash,
            })
    return {"stale": bool(changes), "changes": changes}


def mark_composition_stale(
    spec: Mapping[str, Any],
    current_objects: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a stale derived copy; the caller decides whether to persist it."""

    result = copy.deepcopy(dict(spec))
    audit = detect_composition_staleness(spec, current_objects)
    if audit["stale"]:
        result["stale"] = True
        result["status"] = "stale"
        result["stale_reason"] = ";".join(
            f"{item['object_id']}:{item['reason']}" for item in audit["changes"]
        )
        result["validated_at"] = None
    return result
