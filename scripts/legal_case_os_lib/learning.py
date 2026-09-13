"""Source-bound learning assets. Semantic proposals are supplied by the AI/user.

This module validates quotations and persistence boundaries; it does not infer a
writing style, endorse a legal proposition, train a model, or access the network.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import LegalCaseError, canonical_json
from .materials import read_material, validate_material


SCHEMA_VERSION = "1.0"
CATEGORIES = ("style_rules", "reasoning_patterns", "viewpoints")
MAX_QUOTE_CHARS = 300
VERIFICATION_STATES = {"unverified", "source_position_only", "verified_at_source_date", "disputed", "superseded"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _fail(code: str, message: str) -> None:
    raise LegalCaseError(code, message)


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegalCaseError("LEARNING_READ_FAILED", f"无法读取学习记录：{path}") from exc
    if not isinstance(value, dict):
        _fail("LEARNING_INVALID", "学习记录必须为 JSON 对象。")
    return value


def _writable_learning_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    if any(part.casefold() in {"library", "00-originals"} for part in resolved.parts):
        _fail("LEARNING_OUTPUT_PROTECTED", "学习候选与版本资产不得写入正式 library 或 00-originals；请选择独立的任务/学习输出目录。")
    return resolved


def _new_json(path: Path, value: dict[str, Any]) -> None:
    path = _writable_learning_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(_bytes(value))
    except FileExistsError as exc:
        raise LegalCaseError("LEARNING_PATH_EXISTS", "已有学习记录不得覆盖；请使用新候选路径或新版本。") from exc


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("LEARNING_REQUIRED_FIELD", f"{label} 必须是非空文字。")
    return value


def _strings(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        _fail("LEARNING_REQUIRED_FIELD", f"{label} 必须是非空文字数组。")
    for item in value:
        _text(item, label)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("LEARNING_INVALID_ID", f"{label} 必须是安全、明确的标识符。")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    _text(value, label)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegalCaseError("LEARNING_INVALID_TIME", f"{label} 不是有效 ISO 时间。") from exc
    if result.tzinfo is None:
        _fail("LEARNING_INVALID_TIME", f"{label} 必须包含时区。")
    return result


def _learning(proposal: dict[str, Any], *, max_quote_chars: int | None = MAX_QUOTE_CHARS) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(proposal, dict):
        _fail("LEARNING_INVALID", "学习提案必须是对象。")
    result: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for category in CATEGORIES:
        entries = proposal.get(category, [])
        if not isinstance(entries, list):
            _fail("LEARNING_INVALID", f"{category} 必须是数组。")
        result[category] = []
        for entry in entries:
            if not isinstance(entry, dict):
                _fail("LEARNING_INVALID", "学习条目必须是对象。")
            entry_id = _identifier(entry.get("id"), "条目 id")
            if entry_id in seen:
                _fail("LEARNING_DUPLICATE_ID", "不同学习条目不能使用相同 id。")
            seen.add(entry_id)
            normalized = {"id": entry_id, "text": _text(entry.get("text"), "学习内容")}
            for field in ("applies_when", "not_applicable_when"):
                _strings(entry.get(field), field)
                normalized[field] = copy.deepcopy(entry[field])
            if "application_guide" in entry:
                guide = entry["application_guide"]
                if not isinstance(guide, dict) or set(guide) != {"steps", "good_example", "bad_example", "difference"}:
                    _fail("LEARNING_GUIDE_INVALID", "application_guide 须包含 steps、good_example、bad_example、difference。")
                _strings(guide["steps"], "具体应用步骤")
                for field in ("good_example", "bad_example", "difference"):
                    _text(guide[field], field)
                if guide["good_example"].strip() == guide["bad_example"].strip():
                    _fail("LEARNING_GUIDE_INVALID", "正反示例不能相同；须写明具体差异。")
                normalized["application_guide"] = copy.deepcopy(guide)
            bindings = entry.get("source_bindings")
            if not isinstance(bindings, list) or not bindings:
                _fail("LEARNING_SOURCE_REQUIRED", "每条学习内容必须有具体来源绑定。")
            normalized["source_bindings"] = []
            for binding in bindings:
                if not isinstance(binding, dict):
                    _fail("LEARNING_INVALID_BINDING", "来源绑定必须是对象。")
                source_id = _text(binding.get("id"), "来源 id")
                start, end = binding.get("start"), binding.get("end")
                quote = _text(binding.get("quote"), "原文引文")
                if (type(start) is not int or type(end) is not int or start < 0 or end <= start
                        or end - start != len(quote)):
                    _fail("LEARNING_INVALID_LOCATOR", "来源字符位置必须为零基、左闭右开，且与原文长度一致。")
                if max_quote_chars is not None and len(quote) > max_quote_chars:
                    _fail("LEARNING_EXCERPT_TOO_LONG", "长期可复用学习只接受每条至多 300 字符的必要来源片段，不复制整段案件记录。")
                normalized["source_bindings"].append({"id": source_id, "start": start, "end": end, "quote": quote})
            if category == "viewpoints":
                for field in ("speaker", "speaker_role", "original_view"):
                    normalized[field] = _text(entry.get(field), field)
                for field in ("rule_premises", "fact_premises", "counterarguments_or_limits"):
                    _strings(entry.get(field), field)
                    normalized[field] = copy.deepcopy(entry[field])
                status = entry.get("verification_status")
                if status not in VERIFICATION_STATES:
                    _fail("LEARNING_VIEWPOINT_STATUS", "观点须明确核验状态，不能默认当作现行规则。")
                normalized["verification_status"] = status
                if status == "verified_at_source_date":
                    normalized["verified_at"] = _text(entry.get("verified_at"), "原核验日期")
                    normalized["verification_basis"] = _text(entry.get("verification_basis"), "原核验依据")
            result[category].append(normalized)
    if not seen:
        _fail("LEARNING_EMPTY", "至少提供一条已经完成语义提炼的风格、思维方法或条件性观点。")
    return result


def _bindings(learning: dict[str, Any]):
    for category in CATEGORIES:
        for entry in learning[category]:
            for binding in entry["source_bindings"]:
                yield binding


def _material_ref(record: dict[str, Any]) -> dict[str, Any]:
    fields = ("id", "origin", "role", "source_sha256", "text_sha256", "snapshot_path", "text_path", "record_path", "record_sha256", "quality")
    result = {field: copy.deepcopy(record[field]) for field in fields if field in record}
    provenance = record.get("provenance", {})
    # Do not copy arbitrary metadata or document bodies into long-term learning.
    if isinstance(provenance, dict):
        keys = ("local_source_path", "local_source_sha256", "provider", "document_id", "version_id", "source_sha256", "read_protocol")
        result["provenance"] = {key: copy.deepcopy(provenance[key]) for key in keys if key in provenance}
    return result


def _verify_with_materials(learning: dict[str, Any], materials: list[dict[str, Any]]) -> None:
    if not isinstance(materials, list):
        _fail("LEARNING_SOURCE_INVALID", "学习材料必须是记录数组。")
    by_id: dict[str, dict[str, Any]] = {}
    used_ids = {binding["id"] for binding in _bindings(learning)}
    for material in materials:
        if not isinstance(material, dict):
            _fail("LEARNING_SOURCE_INVALID", "每份学习材料必须是记录对象。")
        material_id = _text(material.get("id"), "材料 id")
        if material_id in by_id:
            _fail("LEARNING_DUPLICATE_SOURCE", "材料集合包含重复 id。")
        if material_id in used_ids:
            errors = validate_material(material)
            if errors:
                _fail("LEARNING_SOURCE_INVALID", "学习来源校验失败：" + "; ".join(str(item) for item in errors))
        by_id[material_id] = material
    for binding in _bindings(learning):
        if binding["id"] not in by_id:
            _fail("LEARNING_SOURCE_MISSING", f"学习来源不存在：{binding['id']}")
        part = read_material(by_id[binding["id"]], offset=binding["start"], limit=binding["end"] - binding["start"])
        if part.get("text") != binding["quote"]:
            _fail("LEARNING_QUOTE_MISMATCH", "来源引文与冻结材料的对应字符不一致。")


def build_learning_candidate(materials: list[dict[str, Any]], proposal: dict[str, Any], output: Path) -> dict[str, Any]:
    """Validate an AI-authored proposal and save a task-local, immediately usable candidate."""
    output = _writable_learning_path(output)
    learning = _learning(proposal)
    proposal_id = _identifier(proposal.get("id"), "提案 id")
    title = _text(proposal.get("title"), "提案标题")
    _verify_with_materials(learning, materials)
    used_ids = {binding["id"] for binding in _bindings(learning)}
    candidate = {
        "schema_version": SCHEMA_VERSION, "kind": "learning_candidate", "id": proposal_id,
        "title": title, "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "current_task", "status": "candidate", "learning": learning,
        "material_refs": [_material_ref(record) for record in materials if record["id"] in used_ids],
        "validation": {"source_bindings_verified": True, "semantic_truth_verified_by_script": False,
                       "model_training_performed": False, "case_facts_promoted": False,
                       "application_to_current_task_checked": False, "legal_currency_verified_by_script": False},
    }
    _new_json(output, candidate)
    return {"ok": True, "candidate_path": str(output), "candidate_sha256": _hash(output.read_bytes()),
            "candidate": candidate, "applicable_now": True, "persisted_to_library": False,
            "requires_task_applicability_check": True, "application_readiness": _application_readiness(learning)}


def _validate_candidate(candidate: dict[str, Any]) -> None:
    if candidate.get("schema_version") != SCHEMA_VERSION or candidate.get("kind") != "learning_candidate":
        _fail("LEARNING_INVALID", "不是支持的学习候选版本。")
    _identifier(candidate.get("id"), "候选 id")
    _text(candidate.get("title"), "候选标题")
    _timestamp(candidate.get("created_at"), "候选创建时间")
    if candidate.get("status") != "candidate" or candidate.get("scope") != "current_task":
        _fail("LEARNING_INVALID", "候选必须仅用于当前任务。")
    if _learning(candidate.get("learning")) != candidate.get("learning"):
        _fail("LEARNING_INVALID", "候选内容不是规范化学习记录。")
    refs = candidate.get("material_refs")
    if not isinstance(refs, list) or not refs:
        _fail("LEARNING_SOURCE_REQUIRED", "候选缺少冻结的来源引用。")
    source_ids = set()
    for ref in refs:
        if not isinstance(ref, dict):
            _fail("LEARNING_SOURCE_INVALID", "来源引用必须为对象。")
        source_id = _text(ref.get("id"), "来源 id")
        if source_id in source_ids:
            _fail("LEARNING_DUPLICATE_SOURCE", "来源 id 不唯一。")
        source_ids.add(source_id)
        for field in ("source_sha256", "text_sha256", "record_sha256"):
            if not isinstance(ref.get(field), str) or not _HASH.fullmatch(ref[field]):
                _fail("LEARNING_SOURCE_INVALID", "来源缺少有效的 SHA-256。")
    if {binding["id"] for binding in _bindings(candidate["learning"])} != source_ids:
        _fail("LEARNING_SOURCE_INVALID", "学习绑定和冻结来源集合不一致。")


def _verify_current_candidate_sources(candidate: dict[str, Any]) -> None:
    """Approval requires the exact task snapshots, not an unchecked cached quotation."""
    refs = {ref["id"]: ref for ref in candidate["material_refs"]}
    texts: dict[str, str] = {}
    for source_id, ref in refs.items():
        for path_field, hash_field in (("snapshot_path", "source_sha256"), ("text_path", "text_sha256")):
            raw = ref.get(path_field)
            if not raw or not Path(raw).is_file():
                _fail("LEARNING_SOURCE_UNAVAILABLE", "确认保存前必须能验证本次来源副本；缺失时不能把候选升为长期资产。")
            data = Path(raw).read_bytes()
            if _hash(data) != ref[hash_field]:
                _fail("LEARNING_SOURCE_STALE", "学习候选来源已改变，须重新生成并确认。")
            if path_field == "text_path":
                texts[source_id] = data.decode("utf-8")
        _check_local_original(ref)
        _check_material_record(ref)
    for binding in _bindings(candidate["learning"]):
        if texts[binding["id"]][binding["start"]:binding["end"]] != binding["quote"]:
            _fail("LEARNING_QUOTE_MISMATCH", "待保存学习引文和来源不一致。")


def _check_local_original(ref: dict[str, Any]) -> bool:
    provenance = ref.get("provenance", {})
    raw = provenance.get("local_source_path") if isinstance(provenance, dict) else None
    if not raw:
        return False
    source = Path(raw)
    if not source.is_file():
        return False
    expected = provenance.get("local_source_sha256")
    if expected != ref.get("source_sha256") or _hash(source.read_bytes()) != expected:
        _fail("LEARNING_SOURCE_STALE", "仍可访问的原文件已发生变化；须明确复核旧版学习适用性。")
    return True


def _check_material_record(ref: dict[str, Any]) -> bool:
    raw = ref.get("record_path")
    if not raw or not Path(raw).is_file():
        return False
    record = _read_json(Path(raw))
    digest = _hash(canonical_json({key: value for key, value in record.items() if key != "record_sha256"}).encode("utf-8"))
    if digest != ref.get("record_sha256") or record.get("record_sha256") != digest:
        _fail("LEARNING_SOURCE_STALE", "材料来源、定位或质量记录已发生变化；须重新核对学习候选。")
    return True


def _confirmation(confirmation: dict[str, Any], candidate: dict[str, Any], candidate_hash: str) -> dict[str, Any]:
    if not isinstance(confirmation, dict) or confirmation.get("decision") != "approved":
        _fail("LEARNING_CONFIRMATION_REQUIRED", "长期保存需要对这一份学习候选的明确批准。")
    if confirmation.get("object_id") != candidate["id"] or confirmation.get("candidate_sha256") != candidate_hash:
        _fail("LEARNING_CONFIRMATION_MISMATCH", "确认没有绑定当前候选对象和精确内容哈希。")
    actor = _text(confirmation.get("actor"), "确认人")
    if actor.strip().lower() in {"ai", "assistant", "model", "system", "codex", "agent"}:
        _fail("LEARNING_HUMAN_CONFIRMATION_REQUIRED", "AI 不得代替用户批准长期复用。")
    when = _timestamp(confirmation.get("confirmed_at"), "确认时间")
    if when < _timestamp(candidate["created_at"], "候选创建时间"):
        _fail("LEARNING_CONFIRMATION_STALE", "确认时间早于候选，不能复用旧确认。")
    if confirmation.get("reuse_reviewed") is not True:
        _fail("LEARNING_REUSE_REVIEW_REQUIRED", "保存前须明确确认学习内容及必要引文不携带不应长期保存的客户事实或身份信息。")
    return {key: confirmation[key] for key in ("decision", "object_id", "candidate_sha256", "actor", "confirmed_at", "reuse_reviewed")}


def approve_learning(candidate_path: Path, library_dir: Path, confirmation: dict[str, Any]) -> dict[str, Any]:
    """Save an explicitly approved, portable version without copying whole originals."""
    library_dir = _writable_learning_path(library_dir)
    candidate_path = Path(candidate_path).resolve()
    candidate = _read_json(candidate_path)
    _validate_candidate(candidate)
    candidate_hash = _hash(candidate_path.read_bytes())
    if candidate_hash != _hash(_bytes(candidate)):
        _fail("LEARNING_NONCANONICAL", "候选文件格式或内容已变更，请重建候选后确认。")
    receipt = _confirmation(confirmation, candidate, candidate_hash)
    _verify_current_candidate_sources(candidate)
    root = _writable_learning_path(library_dir / candidate["id"])
    root.mkdir(parents=True, exist_ok=True)
    # mkdir(exist_ok=False) reserves a version without overwriting concurrent work.
    version = 1
    while True:
        version_dir = root / f"v{version:04d}"
        try:
            version_dir.mkdir()
            break
        except FileExistsError:
            version += 1
    excerpt_dir = version_dir / "source-excerpts"
    excerpt_dir.mkdir()
    excerpts = []
    seen = set()
    for binding in _bindings(candidate["learning"]):
        key = (binding["id"], binding["start"], binding["end"])
        if key in seen:
            continue
        seen.add(key)
        data = binding["quote"].encode("utf-8")
        filename = f"excerpt-{len(excerpts) + 1:04d}.txt"
        (excerpt_dir / filename).write_bytes(data)
        excerpts.append({"id": binding["id"], "start": binding["start"], "end": binding["end"],
                         "path": f"source-excerpts/{filename}", "sha256": _hash(data)})
    asset = {
        "schema_version": SCHEMA_VERSION, "kind": "approved_learning", "id": candidate["id"],
        "version": version, "status": "approved", "confirmation": receipt,
        "approved_candidate": candidate, "source_excerpts": excerpts,
        "reuse_boundary": "Reusable patterns and conditional positions only; recheck applicability and legal currency in each matter.",
    }
    asset["content_sha256"] = _hash(_bytes(asset))
    asset_path = version_dir / "learning.json"
    _new_json(asset_path, asset)
    return {"ok": True, "asset_path": str(asset_path), "asset_sha256": _hash(asset_path.read_bytes()),
            "version": version, "asset": asset}


def load_learning(path: Path) -> dict[str, Any]:
    """Load a portable approved asset offline; do not silently accept changed sources."""
    path = Path(path).resolve()
    asset = _read_json(path)
    if asset.get("schema_version") != SCHEMA_VERSION or asset.get("kind") != "approved_learning" or asset.get("status") != "approved":
        _fail("LEARNING_NOT_APPROVED", "长期复用仅接受明确批准的版本；任务候选可用于本次任务。")
    body = {key: value for key, value in asset.items() if key != "content_sha256"}
    if asset.get("content_sha256") != _hash(_bytes(body)):
        _fail("LEARNING_ASSET_CHANGED", "学习资产的内容哈希不匹配。")
    candidate = asset.get("approved_candidate")
    if not isinstance(candidate, dict):
        _fail("LEARNING_INVALID", "学习资产缺少批准时的候选快照。")
    _validate_candidate(candidate)
    if asset.get("id") != candidate["id"] or type(asset.get("version")) is not int or asset["version"] < 1:
        _fail("LEARNING_INVALID", "学习资产的对象或版本不一致。")
    _confirmation(asset.get("confirmation"), candidate, _hash(_bytes(candidate)))
    expected = {(b["id"], b["start"], b["end"]): b["quote"] for b in _bindings(candidate["learning"])}
    actual = {}
    copied_excerpts = []
    for excerpt in asset.get("source_excerpts", []):
        relative = excerpt.get("path")
        if not isinstance(relative, str):
            _fail("LEARNING_EXCERPT_PATH", "学习资产缺少来源片段路径。")
        target = (path.parent / relative).resolve()
        if not target.is_relative_to(path.parent) or Path(relative).is_absolute():
            _fail("LEARNING_EXCERPT_PATH", "便携来源片段必须位于当前版本目录内。")
        try:
            data = target.read_bytes()
            quote = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise LegalCaseError("LEARNING_EXCERPT_MISSING", "便携来源片段缺失或无法读取。") from exc
        if _hash(data) != excerpt.get("sha256"):
            _fail("LEARNING_EXCERPT_CHANGED", "学习来源片段哈希不匹配。")
        key = (excerpt.get("id"), excerpt.get("start"), excerpt.get("end"))
        if key in actual:
            _fail("LEARNING_DUPLICATE_SOURCE", "学习资产包含重复的来源片段。")
        actual[key] = quote
        copied_excerpts.append({**excerpt, "text": quote})
    if actual != expected:
        _fail("LEARNING_EXCERPT_MISMATCH", "便携来源片段与批准候选的来源绑定不一致。")
    freshness = []
    for ref in candidate["material_refs"]:
        checked = []
        missing = []
        for path_field, hash_field in (("snapshot_path", "source_sha256"), ("text_path", "text_sha256")):
            raw = ref.get(path_field)
            original = Path(raw) if raw else None
            if original is not None and original.is_file():
                if _hash(original.read_bytes()) != ref[hash_field]:
                    _fail("LEARNING_SOURCE_STALE", "仍可访问的原来源副本已变化；不得静默复用旧学习。")
                checked.append(path_field)
            else:
                missing.append(path_field)
        local_original_checked = _check_local_original(ref)
        material_record_checked = _check_material_record(ref)
        freshness.append({"id": ref["id"], "status": "snapshot_matches" if not missing else "offline_snapshot_only",
                          "checked": checked, "unavailable": missing, "live_local_original_checked": local_original_checked,
                          "material_record_checked": material_record_checked,
                          "live_database_checked": False})
    return {"ok": True, "learning": candidate["learning"], "id": candidate["id"], "version": asset["version"],
            "source_excerpts": copied_excerpts, "source_freshness": freshness,
            "warnings": ["来源副本验证不等于当前数据库或现行法效力核验；每次使用须复核适用条件。",
                         "批准保存只允许复用该学习记录，不证明观点正确，也不代表已在当前输出中应用。"],
            "model_training_performed": False, "case_facts_promoted": False,
            "requires_task_applicability_check": True,
            "application_readiness": _application_readiness(candidate["learning"])}


def _application_readiness(learning: dict[str, Any]) -> list[dict[str, Any]]:
    purposes = {"style_rules": "presentation_only", "reasoning_patterns": "reasoning_method",
                "viewpoints": "conditional_position"}
    return [{"entry_id": entry["id"], "category": category, "purpose": purposes[category],
             "guide_status": "provided_not_semantically_verified" if "application_guide" in entry else "guide_missing",
             "current_application_checked": False}
            for category in CATEGORIES for entry in learning[category]]


def _application_quote(binding: Any, text: str) -> dict[str, Any]:
    if not isinstance(binding, dict):
        _fail("LEARNING_APPLICATION_QUOTE", "当前输出须有 start、end、quote 的精确引文。")
    start, end, quote = binding.get("start"), binding.get("end"), binding.get("quote")
    if (type(start) is not int or type(end) is not int or not isinstance(quote, str) or not quote.strip()
            or not 0 <= start < end <= len(text) or text[start:end] != quote):
        _fail("LEARNING_APPLICATION_QUOTE", "当前输出引文与零基、左闭右开的字符位置不一致。")
    return {"start": start, "end": end, "quote": quote}


def check_learning_application(learning_path: Path, application: dict[str, Any], output_text: str,
                               materials: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Check a task-local application trace, never semantic truth or model learning.

    Each selected entry needs an exact output quotation and a check for every
    recorded condition. Condition statuses are declarations, not script findings.
    Unknown/unmet conditions remain visible without blocking an unrelated draft.
    """
    learning_path = Path(learning_path).resolve()
    try:
        source_bytes = learning_path.read_bytes()
        source = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegalCaseError("LEARNING_READ_FAILED", "无法读取本次应用所用的学习记录。") from exc
    if not isinstance(source, dict):
        _fail("LEARNING_INVALID", "学习记录必须为 JSON 对象。")
    source_hash = _hash(source_bytes)
    if source.get("kind") == "learning_candidate":
        _validate_candidate(source)
        _verify_current_candidate_sources(source)
        learning = source["learning"]
    else:
        learning = load_learning(learning_path)["learning"]
    _text(output_text, "实际输出正文")
    entries = application.get("entries") if isinstance(application, dict) else None
    if not isinstance(entries, list) or not entries:
        _fail("LEARNING_APPLICATION_REQUIRED", "应用记录须包含非空 entries 数组。")
    materials = [] if materials is None else materials
    if (not isinstance(materials, list) or any(not isinstance(record, dict) or not isinstance(record.get("id"), str)
                                             for record in materials)):
        _fail("LEARNING_SOURCE_INVALID", "本次材料须为带 id 的材料记录数组。")
    records = {record["id"]: record for record in materials}
    if len(records) != len(materials):
        _fail("LEARNING_DUPLICATE_SOURCE", "本次材料集合包含重复 id。")
    available = {entry["id"]: (category, entry) for category in CATEGORIES for entry in learning[category]}
    readiness = {row["entry_id"]: row for row in _application_readiness(learning)}
    seen, results = set(), []
    for use in entries:
        if (not isinstance(use, dict) or not isinstance(use.get("entry_id"), str)
                or use["entry_id"] not in available):
            _fail("LEARNING_APPLICATION_ENTRY", "应用记录须选择当前候选/版本中存在的 entry_id。")
        entry_id = use["entry_id"]
        if entry_id in seen:
            _fail("LEARNING_DUPLICATE_ID", "同一学习条目只保留一份本次应用核对。")
        seen.add(entry_id)
        category, entry = available[entry_id]
        if use.get("category") != category:
            _fail("LEARNING_APPLICATION_CATEGORY", "风格、论证方法和条件性观点不能互相冒用。")
        index = use.get("source_binding_index")
        if type(index) is not int or not 1 <= index <= len(entry["source_bindings"]):
            _fail("LEARNING_APPLICATION_SOURCE", "source_binding_index 须为该条目实际来源绑定的一基序号。")
        output_binding = _application_quote(use.get("output_binding"), output_text)
        adaptation = _text(use.get("adaptation"), "本次如何调整并应用该条目")
        fields = ["applies_when", "not_applicable_when"]
        if category == "viewpoints":
            fields += ["rule_premises", "fact_premises", "counterarguments_or_limits"]
        required = {(field, number): condition for field in fields
                    for number, condition in enumerate(entry[field], 1)}
        checks = use.get("condition_checks")
        if not isinstance(checks, list):
            _fail("LEARNING_APPLICATION_CONDITIONS", "须逐项核对适用条件；不能用一句“已参考”代替。")
        checked, conditions, unmet = set(), [], []
        for condition in checks:
            if not isinstance(condition, dict):
                _fail("LEARNING_APPLICATION_CONDITIONS", "条件核对须为对象。")
            field, number = condition.get("field"), condition.get("index")
            if not isinstance(field, str) or type(number) is not int:
                _fail("LEARNING_APPLICATION_CONDITIONS", "条件须用 field 和一基 index 明确对应。")
            key = (field, number)
            if key not in required or key in checked:
                _fail("LEARNING_APPLICATION_CONDITIONS", "条件对应不存在或重复，不能遗漏或替换原条件。")
            checked.add(key)
            if field == "not_applicable_when":
                allowed, desired = {"present", "absent", "unknown"}, "absent"
            elif field == "counterarguments_or_limits":
                allowed, desired = {"addressed", "not_addressed", "unknown"}, "addressed"
            else:
                allowed, desired = {"met", "not_met", "unknown"}, "met"
            status = condition.get("status")
            if not isinstance(status, str) or status not in allowed:
                _fail("LEARNING_APPLICATION_CONDITIONS", f"{field} 的 status 只能为 {', '.join(sorted(allowed))}。")
            basis = _text(condition.get("basis"), "当前条件的具体依据或缺口")
            bindings = condition.get("source_bindings", [])
            if not isinstance(bindings, list):
                _fail("LEARNING_INVALID_BINDING", "本次条件的 source_bindings 须为数组。")
            if bindings:
                # Reuse exact source validation, but do not persist these current facts.
                selected = {"id": "APPLICATION-CHECK", "text": "Task-local condition evidence",
                            "applies_when": ["Current application"], "not_applicable_when": ["Another matter"],
                            "source_bindings": bindings}
                normalized = _learning({"reasoning_patterns": [selected]}, max_quote_chars=None)
                _verify_with_materials(normalized, materials)
                bindings = normalized["reasoning_patterns"][0]["source_bindings"]
            if field == "fact_premises" and status == "met":
                if not bindings:
                    _fail("LEARNING_CURRENT_FACT_SOURCE", "声称本案事实前提满足时，须绑定本次案件材料；缺材料请标 unknown。")
                if any(records[binding["id"]].get("role") not in {"current_case", "evidence", "user_statement"}
                       for binding in bindings):
                    _fail("LEARNING_CURRENT_FACT_SOURCE", "模板、旧案例和作者观点不能充当本案事实来源。")
            row = {"field": field, "index": number, "condition": required[key],
                   "declared_status": status, "basis": basis, "source_bindings": bindings}
            if field == "counterarguments_or_limits" and status == "addressed":
                row["output_binding"] = _application_quote(condition.get("output_binding"), output_text)
            conditions.append(row)
            if status != desired:
                unmet.append(copy.deepcopy(row))
        if checked != set(required):
            _fail("LEARNING_APPLICATION_CONDITIONS", "应用核对遗漏学习条目中的条件；未知项应逐项标 unknown。")
        limits = []
        if readiness[entry_id]["guide_status"] == "guide_missing":
            limits.append("guide_missing：旧条目未提供具体步骤和正反示例，不能称为应用指南已核对。")
        if category == "viewpoints":
            limits.append("观点的来源核验状态为 " + entry["verification_status"] + "；不证明当前法律效力或本案事实。")
        result = {"entry_id": entry_id, "category": category, "purpose": readiness[entry_id]["purpose"],
                  "guide_status": readiness[entry_id]["guide_status"],
                  "source_binding": copy.deepcopy(entry["source_bindings"][index - 1]),
                  "output_binding": output_binding, "adaptation": adaptation,
                  "condition_checks": conditions, "unmet_conditions": unmet, "limitations": limits,
                  "application_status": "needs_review" if unmet or limits else "declared_conditions_matched",
                  "semantic_review_required": True}
        if category == "viewpoints":
            result["source_position"] = {field: copy.deepcopy(entry[field]) for field in
                                         ("speaker", "speaker_role", "original_view", "verification_status",
                                          "verified_at", "verification_basis") if field in entry}
        results.append(result)
    if _hash(learning_path.read_bytes()) != source_hash:
        _fail("LEARNING_SOURCE_STALE", "核对期间学习文件发生变化，请对当前版本重新核对。")
    return {"ok": True, "kind": "learning_application_check", "scope": "current_task",
            "learning_path": str(learning_path), "learning_source_sha256": source_hash,
            "output_text_sha256": _hash(output_text.encode("utf-8")), "entries": results,
            "checks": {"application_structure": "passed", "source_and_output_quotes": "passed",
                       "semantic_truth_verified_by_script": False, "legal_currency_verified_by_script": False,
                       "case_facts_verified_by_script": False, "model_training_performed": False,
                       "semantic_review": "required", "filing_approved": False},
            "persisted_to_library": False,
            "limitations": ["本报告仅核对条目、来源引文、输出位置和条件记录；不证明已经学会或实际论证正确。",
                            "风格只控制表达，方法提供推理步骤，观点保留来源立场和适用前提；三者都不能直接成为本案事实。"]}
