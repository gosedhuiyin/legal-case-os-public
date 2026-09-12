from __future__ import annotations

import copy
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .core import LegalCaseError, canonical_json, now_iso, sha256_bytes, sha256_file
from .documents import detect_kind, page_count


IGNORED_INTAKE_NAMES = {".gitkeep", "ORIGINALS-ARE-READ-ONLY.md"}
MEMORY_COLLECTIONS = (
    "sources",
    "facts",
    "issues",
    "theories",
    "evidence",
    "decisions",
    "artifacts",
    "material_batches",
    "case_events",
    "triage_cards",
    "deadline_records",
    "impact_assessments",
    "prospective_matter_seeds",
    "workflow_instances",
    "tasks",
    "run_attempts",
)


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\u241f".join(str(part) for part in parts)
    return f"{prefix}-{sha256_bytes(payload.encode('utf-8'))[:16]}"


def next_case_event_sequence(state: dict[str, Any]) -> int:
    return max((int(item.get("sequence", 0)) for item in state.get("case_events", [])), default=0) + 1


def projection_state_version(state: dict[str, Any]) -> int:
    """Return the canonical projection version, not the audit event sequence."""
    focus_version = state.get("focus", {}).get("state_version")
    audit_sequence = int(state.get("audit", {}).get("last_sequence", 0))
    if isinstance(focus_version, int) and focus_version >= 1:
        return max(focus_version, audit_sequence, 1)
    return max(audit_sequence, 1)


def make_case_event(
    state: dict[str, Any],
    *,
    event_type: str,
    title: str,
    source_ids: Iterable[str] = (),
    related_object_ids: Iterable[str] = (),
    occurred_at: str | None = None,
    received_at: str | None = None,
    authority: str | None = None,
    confidence: str = "direct_record",
    channel: str | None = None,
    sender: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    recorded = now_iso()
    sequence = next_case_event_sequence(state)
    identifier = stable_id("CE", state["matter"]["id"], str(sequence), event_type, recorded)
    event: dict[str, Any] = {
        "id": identifier,
        "version": 1,
        "sequence": sequence,
        "event_type": event_type,
        "title": title,
        "occurred_at": occurred_at,
        "received_at": received_at,
        "recorded_at": recorded,
        "source_ids": list(dict.fromkeys(source_ids)),
        "related_object_ids": list(dict.fromkeys(related_object_ids)),
        "status": status,
    }
    if authority is not None:
        event["authority"] = authority
    event["confidence"] = confidence
    if channel is not None:
        event["channel"] = channel
    if sender is not None:
        event["sender"] = sender
    return event


def _ensure_inside_workspace(path: Path, workspace: Path, code: str) -> Path:
    resolved = path.resolve()
    root = workspace.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LegalCaseError(code, "路径必须位于当前案件工作区内。", {"path": str(resolved), "workspace": str(root)}) from exc
    return resolved


def _ocr_status(path: Path, kind: str, relative: str, ocr_report: dict[str, Any]) -> str:
    value = ocr_report.get(relative, ocr_report.get(path.name))
    if isinstance(value, dict):
        result = value.get("status", "unknown")
    elif isinstance(value, str):
        result = value
    elif kind in {"text", "markdown", "docx"}:
        result = "not_applicable"
    elif kind in {"image", "pdf"}:
        result = "not_run"
    else:
        result = "unknown"
    allowed = {"not_applicable", "not_run", "partial", "complete", "failed", "unknown"}
    return result if result in allowed else "unknown"


def scan_material_batch(
    workspace: Path,
    source_dir: Path,
    existing_sources: list[dict[str, Any]],
    ocr_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan one explicitly supplied arrival directory and merge it without removals.

    Unlike the first-intake indexer, this function never treats sources outside the
    selected directory as deleted.  It returns a staged state; callers commit it
    only after all files have been hashed successfully.
    """
    workspace = workspace.resolve()
    source_dir = _ensure_inside_workspace(source_dir, workspace, "BATCH_OUTSIDE_WORKSPACE")
    if not source_dir.is_dir():
        raise LegalCaseError("SOURCE_DIR_NOT_FOUND", f"增量材料目录不存在：{source_dir}")
    if any(part.casefold() == "_case-state" for part in source_dir.parts):
        raise LegalCaseError("INVALID_BATCH_DIRECTORY", "不能把案件状态或记忆目录作为材料批次。")

    files = sorted(
        (item for item in source_dir.rglob("*") if item.is_file() and item.name not in IGNORED_INTAKE_NAMES),
        key=lambda item: str(item).casefold(),
    )
    if not files:
        raise LegalCaseError("EMPTY_BATCH", "指定目录没有可入库文件。")

    ocr_report = ocr_report or {}
    existing_by_path = {str(item.get("path")): copy.deepcopy(item) for item in existing_sources}
    all_hashes = {str(item.get("sha256")): str(item.get("id")) for item in existing_sources if item.get("sha256")}
    staged_by_path = dict(existing_by_path)
    batch_items: list[dict[str, Any]] = []
    now = now_iso()
    for path in files:
        relative = path.relative_to(workspace).as_posix()
        digest = sha256_file(path)
        kind = detect_kind(path)
        previous = existing_by_path.get(relative)
        if previous is None:
            change = "added"
            version = 1
            source_id = stable_id("S", relative.casefold())
        elif previous.get("sha256") != digest:
            change = "modified"
            version = int(previous.get("version", 1)) + 1
            source_id = previous["id"]
        else:
            change = "unchanged"
            version = int(previous.get("version", 1))
            source_id = previous["id"]
        duplicate_id = all_hashes.get(digest)
        if duplicate_id == source_id:
            duplicate_id = previous.get("duplicate_of") if previous else None
        source = {
            "id": source_id,
            "path": relative,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "version": version,
            "kind": kind,
            "original": True,
            "read_only": True,
            "page_count": page_count(path, kind),
            "ocr_status": _ocr_status(path, kind, relative, ocr_report),
            "duplicate_of": duplicate_id,
            "confidentiality": previous.get("confidentiality", "ordinary") if previous else "ordinary",
            "ingested_at": previous.get("ingested_at", now) if previous else now,
        }
        all_hashes.setdefault(digest, source_id)
        staged_by_path[relative] = source
        batch_items.append(
            {
                "source_id": source_id,
                "path": relative,
                "sha256": digest,
                "version": version,
                "change": change,
            }
        )

    ordered_sources = sorted(staged_by_path.values(), key=lambda item: str(item.get("path", "")).casefold())
    identity_items = [{key: item[key] for key in ("path", "sha256", "version")} for item in batch_items]
    arrival_key = sha256_bytes(canonical_json(identity_items).encode("utf-8"))
    return {
        "sources": ordered_sources,
        "items": batch_items,
        "source_ids": [item["source_id"] for item in batch_items],
        "arrival_key": arrival_key,
        "added_source_ids": [item["source_id"] for item in batch_items if item["change"] == "added"],
        "modified_source_ids": [item["source_id"] for item in batch_items if item["change"] == "modified"],
        "unchanged_source_ids": [item["source_id"] for item in batch_items if item["change"] == "unchanged"],
    }


def _normalize_scope(value: str) -> str:
    return value.replace("\\", "/").strip().casefold()


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _string_values(nested)


def _references_any(item: dict[str, Any], identifiers: set[str]) -> bool:
    return bool(identifiers & {value for value in _string_values(item) if value in identifiers})


_QUERY_GLUE_PHRASES = (
    "请基于本案现有材料", "基于本案现有材料", "请帮我", "帮我", "本案", "现有",
    "请", "核对", "复核", "检查", "审查", "查验", "看看", "看一下", "列出",
    "对应的", "对应", "相关的", "相关", "主要问题", "主要", "一下", "哪些", "什么",
)
_QUERY_GLUE_TERMS = {
    "这个", "那个", "进行", "关于", "以及", "还有", "是否", "怎么", "如何", "问题",
}


def _normalize_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _query_terms(query: str) -> list[str]:
    """Create bounded search terms without requiring a Chinese tokenizer."""
    normalized = _normalize_search_text(query)
    terms: list[str] = []

    def extend_bounded(candidates: Iterable[str], *, limit: int = 80) -> None:
        available = list(dict.fromkeys(
            term for term in candidates
            if term and term not in terms and term not in _QUERY_GLUE_TERMS
        ))
        slots = limit - len(terms)
        if slots <= 0 or not available:
            return
        if len(available) <= slots:
            terms.extend(available)
            return
        if slots == 1:
            terms.append(available[-1])
            return
        # Sample the whole query instead of consuming the budget from its
        # beginning. This keeps decisive terms near the end of long prompts.
        indexes = [round(index * (len(available) - 1) / (slots - 1)) for index in range(slots)]
        terms.extend(available[index] for index in indexes)

    # Preserve exact object IDs and ordinary ASCII tokens before removing
    # conversational glue from the Chinese text.
    extend_bounded(re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)+", normalized))
    extend_bounded(re.findall(r"(?<![a-z0-9._-])[a-z0-9]{2,}(?![a-z0-9._-])", normalized))

    lexical = normalized
    for phrase in _QUERY_GLUE_PHRASES:
        lexical = lexical.replace(phrase, " ")
    chunks = re.findall(r"[\u3400-\u9fff]+", lexical)
    short_chunks: list[str] = []
    edge_terms: list[str] = []
    ngrams_by_size: dict[int, list[str]] = {2: [], 3: [], 4: []}
    for chunk in chunks:
        if 2 <= len(chunk) <= 4 and chunk not in _QUERY_GLUE_TERMS:
            short_chunks.append(chunk)
        for size in range(2, min(4, len(chunk)) + 1):
            edge_terms.extend((chunk[:size], chunk[-size:]))
            for start in range(0, len(chunk) - size + 1):
                ngrams_by_size[size].append(chunk[start:start + size])

    # First preserve short chunks and both edges of every clause. Then fill the
    # remaining budget with evenly sampled 2-, 3-, and 4-character refinements.
    extend_bounded(short_chunks)
    extend_bounded(edge_terms)
    for size in (2, 3, 4):
        extend_bounded(ngrams_by_size[size])
    return terms


def _query_term_weight(term: str) -> int:
    if re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)+", term):
        return 24
    if re.fullmatch(r"[a-z0-9]{2,}", term):
        return 8
    return {2: 5, 3: 8, 4: 12}.get(len(term), 4)


def _semantic_anchor_matches(matched: list[str], *, short_chinese_query: bool) -> list[str]:
    lexical = [
        term for term in matched
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)+", term)
    ]
    long_terms = [term for term in lexical if len(term) >= 3]
    two_character_terms = {term for term in lexical if len(term) == 2}
    if long_terms or len(two_character_terms) >= 2 or (short_chinese_query and two_character_terms):
        return lexical
    return []


def _memory_records(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (collection, item)
        for collection in MEMORY_COLLECTIONS
        for item in state.get(collection, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def _explicit_relation_graph(
    records: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, set[str]]]:
    by_id = {str(item["id"]): (collection, item) for collection, item in records}
    known_ids = set(by_id)
    graph = {identifier: set() for identifier in known_ids}
    for _collection, item in records:
        item_id = str(item["id"])
        for value in _string_values(item):
            if value in known_ids and value != item_id:
                graph[item_id].add(value)
                graph[value].add(item_id)
    return by_id, graph


def _project_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[nested content omitted]"
    if isinstance(value, str):
        return value if len(value) <= 600 else value[:600] + "…"
    if isinstance(value, list):
        return [_project_value(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        return {key: _project_value(nested, depth=depth + 1) for key, nested in value.items()}
    return value


def build_context_capsule(
    state: dict[str, Any],
    *,
    mode: str,
    scopes: list[str] | None = None,
    query: str = "",
    max_items: int = 24,
) -> dict[str, Any]:
    if mode not in {"off", "file_scoped", "relevant", "reflect"}:
        raise LegalCaseError("INVALID_MEMORY_MODE", f"未知记忆读取模式：{mode}")
    if max_items < 1 or max_items > 100:
        raise LegalCaseError("INVALID_CONTEXT_LIMIT", "max-items 必须在 1 到 100 之间。")
    scopes = [item for item in (scopes or []) if item.strip()]
    if mode == "file_scoped" and not scopes:
        raise LegalCaseError("MEMORY_SCOPE_REQUIRED", "文件限定模式必须给出至少一个 --scope。")

    state_hash = sha256_bytes(canonical_json({key: value for key, value in state.items() if key != "audit"}).encode("utf-8"))
    as_of = max((int(item.get("sequence", 0)) for item in state.get("case_events", [])), default=0)
    result: dict[str, Any] = {
        "kind": "context-capsule-manifest",
        "matter_id": state.get("matter", {}).get("id"),
        "mode": mode,
        "query": query,
        "requested_scope": scopes,
        "state_hash": state_hash,
        "state_version": projection_state_version(state),
        "as_of_event_seq": as_of,
        "generated_at": now_iso(),
        "included": [],
        "context_slice": {},
        "omitted_counts": {},
        "coverage": "complete_for_selected_scope" if mode == "file_scoped" else "complete_for_indexed_candidates",
        "warnings": [],
        "invariants": {
            "chat_transcript_is_authority": False,
            "original_sources_replaced_by_summary": False,
            "memory_read_authorizes_write": False,
            "external_action_gates_preserved": True,
        },
    }
    if mode == "off":
        result["coverage"] = "memory_intentionally_disabled"
        return result

    if mode in {"relevant", "reflect"}:
        result["warnings"].append("仅覆盖结构化索引和显式一跳关系，不代表全案语义完整。")

    anchors: set[str] = set()
    normalized_scopes = {_normalize_scope(item) for item in scopes}
    if mode == "file_scoped":
        for source in state.get("sources", []):
            source_path = _normalize_scope(str(source.get("path", "")))
            source_id = str(source.get("id", ""))
            if source_id.casefold() in normalized_scopes or any(
                source_path == scope or source_path.endswith("/" + scope) or scope.endswith("/" + source_path)
                for scope in normalized_scopes
            ):
                anchors.add(source_id)
        if not anchors:
            result["coverage"] = "selection_failed"
            result["warnings"].append("指定范围未匹配到案件来源；未扩大到其他记忆。")
            return result

    terms = _query_terms(query)
    normalized_query = _normalize_search_text(query)
    semantic_query = normalized_query
    for phrase in _QUERY_GLUE_PHRASES:
        semantic_query = semantic_query.replace(phrase, " ")
    chinese_query_length = len("".join(re.findall(r"[\u3400-\u9fff]+", semantic_query)))
    short_chinese_query = 0 < chinese_query_length <= 2
    records = _memory_records(state)
    by_id, relation_graph = _explicit_relation_graph(records)
    haystacks = {
        str(item["id"]): _normalize_search_text(" ".join(_string_values(item)))
        for _collection, item in records
    }
    term_matches: dict[str, list[str]] = {}
    anchor_term_matches: dict[str, list[str]] = {}
    for item_id, haystack in haystacks.items():
        matched = [term for term in terms if term in haystack]
        if matched:
            term_matches[item_id] = matched
            anchor_match = _semantic_anchor_matches(matched, short_chinese_query=short_chinese_query)
            if anchor_match:
                anchor_term_matches[item_id] = anchor_match

    explicit_id_tokens = set(re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)+", normalized_query))
    explicit_query_ids = {
        identifier for identifier in by_id
        if _normalize_search_text(identifier) in explicit_id_tokens
    }
    query_anchor_ids = set(anchor_term_matches) | explicit_query_ids
    if mode in {"relevant", "reflect"} and not terms:
        focus = state.get("focus", {})
        query_anchor_ids.update(
            value for key, value in focus.items()
            if key.startswith("current_") and isinstance(value, str) and value in by_id
        )
    if mode in {"relevant", "reflect"}:
        anchors.update(query_anchor_ids)

    relation_ids = {
        related
        for anchor in anchors
        for related in relation_graph.get(anchor, set())
    }
    declared_counterevidence_ids: set[str] = set()
    if mode in {"relevant", "reflect"}:
        for root_id in anchors | relation_ids:
            record = by_id.get(root_id)
            if not record:
                continue
            declared_counterevidence_ids.update(
                value for value in record[1].get("counterevidence_object_ids", [])
                if isinstance(value, str) and value in by_id
            )

    adverse_requested = bool(re.search(r"(?:反证|不利|反方|反驳|抗辩|风险)", query))
    support_requested = bool(re.search(r"(?:支持|支撑|有利|证据|材料)", query))
    material_adverse_records: list[dict[str, Any]] = []
    for collection, item in records:
        if collection != "evidence" or item.get("adverse") is not True:
            continue
        if item.get("relevance") not in {"medium", "high"}:
            continue
        if item.get("probative_strength") not in {"medium", "high"}:
            continue
        material_adverse_records.append(item)
    strength_rank = {"high": 2, "medium": 1}
    material_adverse_records.sort(
        key=lambda item: (
            -strength_rank.get(str(item.get("relevance")), 0),
            -strength_rank.get(str(item.get("probative_strength")), 0),
            str(item.get("id", "")),
        )
    )
    all_material_adverse_ids = {str(item["id"]) for item in material_adverse_records[:3]}
    material_adverse_overflow_ids = [str(item["id"]) for item in material_adverse_records[3:]]
    substantive_anchor = any(
        by_id.get(identifier, (None, None))[0] in {"issues", "theories", "evidence", "decisions"}
        for identifier in query_anchor_ids
    )
    material_adverse_ids = (
        all_material_adverse_ids
        if (
            (mode == "relevant" and (adverse_requested or substantive_anchor))
            or (mode == "reflect" and adverse_requested)
        )
        else set()
    )
    if material_adverse_ids and material_adverse_overflow_ids:
        result["warnings"].append(
            "重大不利证据安全池按相关性与证明力最多保留3项；池外还有："
            + "、".join(material_adverse_overflow_ids)
        )

    candidates: list[tuple[int, int, str, dict[str, Any], list[str]]] = []
    collection_priority = {name: index for index, name in enumerate(MEMORY_COLLECTIONS)}
    reflection_cursor = int(state.get("memory_state", {}).get("last_reflection_event_seq", 0))
    for collection in MEMORY_COLLECTIONS:
        items = state.get(collection, [])
        for item in items:
            item_id = str(item.get("id", ""))
            reasons: list[str] = []
            score = 0
            if item_id in anchors:
                if mode == "file_scoped":
                    score += 100
                    reasons.append("exact_scope")
                elif item_id in explicit_query_ids:
                    score += 120
                    reasons.append("exact_object_id")
                else:
                    matched = anchor_term_matches.get(item_id, [])
                    score += 32 + min(sum(_query_term_weight(term) for term in matched), 40)
                    reasons.extend(f"query:{term}" for term in matched[:6])
                    if collection == "issues" and re.search(r"(?:争点|问题|时效|责任|管辖)", query):
                        score += 40
                        reasons.append("query_issue_anchor")
            elif item_id in relation_ids:
                score += 34 if mode in {"relevant", "reflect"} else 80
                reasons.append("explicit_one_hop_relation")
            if item_id in declared_counterevidence_ids:
                score += 55
                reasons.append("declared_counterevidence")
            if item_id in material_adverse_ids:
                score += 60 if adverse_requested else 26
                reasons.append("material_adverse_safety")
            if mode == "relevant":
                if collection == "deadline_records" and item.get("status") in {"candidate", "verified"}:
                    score += 10 if score else 20
                    reasons.append("open_deadline" if score > 20 else "open_deadline_safety")
                if score and collection == "workflow_instances" and item.get("status") not in {"completed", "cancelled", "failed"}:
                    score += 10
                    reasons.append("related_open_workflow")
                if score and collection == "issues" and item.get("status") in {"open", "stale"}:
                    score += 6
                    reasons.append("related_open_issue")
                if score and support_requested and collection == "evidence" and item.get("relevance") in {"medium", "high"}:
                    score += 8
                    reasons.append("supporting_evidence")
            elif mode == "reflect":
                if not terms and collection == "case_events" and int(item.get("sequence", 0)) > reflection_cursor:
                    score += 15
                    reasons.append("new_since_last_reflection")
                if (score or not terms) and collection in {"impact_assessments", "prospective_matter_seeds"} and item.get("status") in {"candidate", "dormant"}:
                    score += 15
                    reasons.append("unresolved_reflection_object")
                if score and item.get("status") in {"candidate", "dormant", "stale", "open", "verified", "waiting_external", "waiting_approval"}:
                    score += 10
                    reasons.append("unresolved_or_weak_signal")
                if score and (item.get("relevance") in {"medium", "high"} or item.get("impact_level") in {"high", "critical"}):
                    score += 10
                    reasons.append("material_signal")
            elif mode == "file_scoped" and score == 0:
                continue
            if score > 0:
                candidates.append((-score, collection_priority[collection], collection, item, list(dict.fromkeys(reasons))))

    candidates.sort(key=lambda row: (row[0], row[1], str(row[3].get("id", ""))))
    selected = candidates[:max_items]
    selected_by_collection: dict[str, list[dict[str, Any]]] = {}
    for neg_score, _priority, collection, item, reasons in selected:
        selected_by_collection.setdefault(collection, []).append(_project_value(item))
        result["included"].append(
            {
                "collection": collection,
                "object_id": item.get("id"),
                "score": -neg_score,
                "reasons": reasons,
            }
        )
    result["context_slice"] = selected_by_collection
    selected_ids = {str(item.get("object_id")) for item in result["included"]}
    missing_declared_counterevidence = sorted(declared_counterevidence_ids - selected_ids)
    if missing_declared_counterevidence:
        result["warnings"].append(
            "受上下文预算限制，声明反证未载入：" + "、".join(missing_declared_counterevidence)
        )
    missing_material_adverse = sorted(material_adverse_ids - selected_ids)
    if missing_material_adverse:
        result["warnings"].append(
            "受上下文预算限制，重大不利证据未载入：" + "、".join(missing_material_adverse)
        )
    total_by_collection = {collection: len(state.get(collection, [])) for collection in MEMORY_COLLECTIONS}
    selected_counts = {collection: len(items) for collection, items in selected_by_collection.items()}
    result["omitted_counts"] = {
        collection: total - selected_counts.get(collection, 0)
        for collection, total in total_by_collection.items()
        if total - selected_counts.get(collection, 0) > 0
    }
    has_query_match = bool(query_anchor_ids)
    if mode in {"relevant", "reflect"} and terms and not has_query_match:
        if selected:
            result["coverage"] = "safety_fallback_only"
            result["warnings"].append("查询未命中结构化索引；仅载入未决期限或重大不利材料等安全项。")
        else:
            result["coverage"] = "no_indexed_relation"
    if len(candidates) > len(selected):
        result["coverage"] = "bounded_selection"
        result["warnings"].append(f"相关候选共 {len(candidates)} 项，本轮按预算载入 {len(selected)} 项。")
    if not selected and mode in {"relevant", "reflect"}:
        result["coverage"] = "no_indexed_relation"
    return result


def render_current_case_view(state: dict[str, Any]) -> str:
    matter = state.get("matter", {})
    as_of = max((int(item.get("sequence", 0)) for item in state.get("case_events", [])), default=0)
    lines = [
        "# 当前案件视图",
        "",
        "> 这是可重建的短索引，不是事实原件，也不替代来源核验。",
        "",
        f"- 案件：{matter.get('title') or '未命名'}（{matter.get('id')}）",
        f"- 生产阶段：{matter.get('stage')}；当前门禁：{state.get('status', {}).get('current_gate') or '无'}",
        f"- 截至案件事件序号：{as_of}；生成时间：{now_iso()}",
        "",
    ]

    def section(title: str, items: list[str], empty: str = "暂无") -> None:
        lines.extend([f"## {title}", ""])
        lines.extend([f"- {item}" for item in items] if items else [f"- {empty}"])
        lines.append("")

    issues = [
        f"{item.get('id')}｜{item.get('title')}｜{item.get('importance')}｜{item.get('status')}"
        for item in state.get("issues", [])
        if item.get("status") in {"open", "analyzed", "stale"}
    ][:5]
    deadlines = [
        f"{item.get('id')}｜{item.get('title')}｜{item.get('status')}｜{item.get('due_at') or '日期待核验'}｜来源事件 {item.get('source_event_id')}"
        for item in state.get("deadline_records", [])
        if item.get("status") in {"candidate", "verified"}
    ][:8]
    workflows = [
        f"{item.get('id')}｜{item.get('title')}｜{item.get('status')}｜等待：{item.get('waiting_for') or '无'}"
        for item in state.get("workflow_instances", [])
        if item.get("status") not in {"completed", "cancelled", "failed"}
    ][:8]
    impacts = [
        f"{item.get('id')}｜{item.get('impact_level')}｜{item.get('status')}｜{item.get('summary')}"
        for item in state.get("impact_assessments", [])
        if item.get("status") in {"candidate", "applied"}
    ][-5:]
    seeds = [
        f"{item.get('id')}｜{item.get('title')}｜{item.get('status')}｜反证 {len(item.get('counterevidence_object_ids', []))} 项"
        for item in state.get("prospective_matter_seeds", [])
        if item.get("status") in {"dormant", "candidate", "approved"}
    ][:3]
    untriaged = [
        f"{item.get('id')}｜{len(item.get('source_ids', []))}份材料｜{item.get('received_at')}"
        for item in state.get("material_batches", [])
        if item.get("status") == "pending_triage"
    ][:8]
    section("当前主要争点", issues)
    section("期限与程序日期", deadlines)
    section("进行中的工单", workflows)
    section("待分诊材料批次", untriaged)
    section("最近影响", impacts)
    section("潜在新案线索", seeds)
    lines.extend(
        [
            "## 使用边界",
            "",
            "- 查明事实时回到原始材料和精确定位；本页不能作为证据来源。",
            "- 对话读取本页不代表可以写回；实质结论仍须经过候选、核验和相应律师门禁。",
            "- 本页不记录完整聊天、模型推理过程或原件全文。",
            "",
        ]
    )
    return "\n".join(lines)


def reconcile_report(state: dict[str, Any], state_path: Path) -> dict[str, Any]:
    workspace = state_path.resolve().parents[1]
    missing_sources: list[str] = []
    changed_sources: list[str] = []
    for source in state.get("sources", []):
        raw_path = Path(str(source.get("path", "")))
        path = raw_path if raw_path.is_absolute() else workspace / raw_path
        if not path.exists():
            missing_sources.append(str(source.get("id")))
        elif path.is_file() and sha256_file(path) != source.get("sha256"):
            changed_sources.append(str(source.get("id")))
    max_event = max((int(item.get("sequence", 0)) for item in state.get("case_events", [])), default=0)
    memory_state = state.get("memory_state", {})
    untriaged_batches = [item.get("id") for item in state.get("material_batches", []) if item.get("status") == "pending_triage"]
    deadline_candidates = [item.get("id") for item in state.get("deadline_records", []) if item.get("status") == "candidate"]
    interrupted_runs = [item.get("id") for item in state.get("run_attempts", []) if item.get("status") in {"pending", "running"}]
    open_workflows = [
        item.get("id")
        for item in state.get("workflow_instances", [])
        if item.get("status") not in {"completed", "cancelled", "failed"}
    ]
    cursor_ahead = int(memory_state.get("last_committed_event_seq", 0)) > max_event
    current_view_stale = int(memory_state.get("current_view_as_of_event_seq", 0)) < max_event
    blockers = []
    if missing_sources:
        blockers.append("source_missing")
    if changed_sources:
        blockers.append("source_hash_changed")
    if cursor_ahead:
        blockers.append("memory_cursor_ahead_of_event_ledger")
    return {
        "ok": not blockers,
        "kind": "matter-reconciliation-report",
        "matter_id": state.get("matter", {}).get("id"),
        "state_version": projection_state_version(state),
        "as_of_event_seq": max_event,
        "generated_at": now_iso(),
        "missing_source_ids": missing_sources,
        "changed_source_ids": changed_sources,
        "untriaged_batch_ids": untriaged_batches,
        "deadline_candidate_ids": deadline_candidates,
        "interrupted_run_ids": interrupted_runs,
        "open_workflow_ids": open_workflows,
        "current_view_stale": current_view_stale,
        "cursor_ahead": cursor_ahead,
        "blockers": blockers,
        "automatic_external_actions": 0,
        "notice": "报告只恢复和核对状态；不会自动续写半截法院稿，也不会打印、发送、上传、送达或提交。",
    }


def load_json_object_or_list(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise LegalCaseError("FILE_NOT_FOUND", f"文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LegalCaseError(
            "INVALID_JSON",
            f"JSON 无法解析：{path}",
            {"line": exc.lineno, "column": exc.colno, "detail": exc.msg},
        ) from exc
