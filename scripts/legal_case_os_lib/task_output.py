"""Render source-linked task drafts without requiring a database or matter state.

The current agent supplies the writing and analysis. This module verifies source
identity and quoted bindings and creates real files; it does not claim to judge
legal correctness, infer missing facts, approve a filing, or learn model weights.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .core import LegalCaseError, canonical_json, ensure_not_originals, now_iso, sha256_bytes, sha256_file
from .materials import read_material, validate_material
from .templates import create_docx_from_markdown


CURRENT_FACT_ROLES = {"current_case", "evidence", "user_statement"}
SECTION_KINDS = {"heading", "fact", "argument", "analogy", "authority", "administrative"}


def _check_body_labels(text: str) -> None:
    # Production labels belong in review.md; legal uncertainty is not a label.
    if re.search(r"内部审阅稿|供\s*AI\s*审核|候选证据及类案参考|内部核验记录|待律师审核", text, re.I):
        _fail("TASK_INTERNAL_LABEL_IN_BODY", "正文含制作或审阅标签，请将对应说明放入独立审阅记录；未自动删除正文。")


def _fail(code: str, message: str) -> None:
    raise LegalCaseError(code, message)


def _validate_constraints(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - {"must_keep", "avoid", "terminology"}:
        _fail("TASK_CONSTRAINTS_INVALID", "写作要求仅支持must_keep、avoid和terminology。")
    for key in ("must_keep", "avoid"):
        items = value.get(key, [])
        if not isinstance(items, list) or any(not isinstance(v, str) or not v.strip() for v in items):
            _fail("TASK_CONSTRAINTS_INVALID", "must_keep/avoid须为非空文字组成的列表。")
    terms = value.get("terminology", {})
    if not isinstance(terms, dict) or any(not isinstance(k, str) or not k.strip() or
                                         not isinstance(v, str) or not v.strip() for k, v in terms.items()):
        _fail("TASK_CONSTRAINTS_INVALID", "terminology须为角色到指定称谓的文字映射。")
    return value


def _check_literal_constraints(text: str, constraints: dict) -> None:
    missing = [v for v in constraints.get("must_keep", []) if v not in text]
    forbidden = [v for v in constraints.get("avoid", []) if v in text]
    if missing or forbidden:
        _fail("TASK_CONSTRAINTS_UNSATISFIED", "文字要求未满足：" + json.dumps(
            {"未保留": missing, "仍出现": forbidden}, ensure_ascii=False))


def _material_text(record: dict[str, Any]) -> str:
    errors = validate_material(record)
    if errors:
        _fail("TASK_SOURCE_INVALID", "; ".join(errors))
    chunks: list[str] = []
    offset = 0
    while True:
        part = read_material(record, offset=offset, limit=12000)
        chunks.append(part["text"])
        next_offset = part.get("next_offset")
        if next_offset is None:
            return "".join(chunks)
        if not isinstance(next_offset, int) or next_offset <= offset:
            _fail("TASK_SOURCE_PAGINATION_INVALID", "材料分页未前进。")
        offset = next_offset


def _bindings(section: dict, records: dict, texts: dict) -> list[dict]:
    result: list[dict] = []
    source_bindings = section.get("source_bindings", [])
    if not isinstance(source_bindings, list) or any(not isinstance(item, dict) for item in source_bindings):
        _fail("TASK_BINDINGS_INVALID", "source_bindings须为来源绑定对象列表。")
    for item in source_bindings:
        identifier = item.get("id")
        if not isinstance(identifier, str) or identifier not in records:
            _fail("TASK_SOURCE_NOT_FOUND", f"不存在材料ID：{identifier}")
        start, end, quote = item.get("start"), item.get("end"), item.get("quote")
        if (type(start) is not int or type(end) is not int or not isinstance(quote, str)
                or not (0 <= start < end <= len(texts[identifier]))
                or texts[identifier][start:end] != quote):
            _fail("TASK_QUOTE_MISMATCH", f"引用定位与原文不符：{identifier}")
        result.append({**item, "source_sha256": records[identifier]["source_sha256"],
                       "text_sha256": records[identifier]["text_sha256"]})
    return result


def validate_task_draft(materials: list[dict], proposal: dict) -> dict:
    """Return the checked internal draft; never confer court eligibility."""
    if not isinstance(proposal, dict) or not isinstance(proposal.get("title"), str) or not proposal["title"].strip():
        _fail("TASK_TITLE_REQUIRED", "必须提供文书标题。")
    if proposal.get("audience", "internal_review") != "internal_review":
        _fail("TASK_INTERNAL_DRAFT_ONLY", "本入口只生成内部待审稿，法院候选和提交包须走对应审阅批准流程。")
    mode = proposal.get("mode", "simple")
    if not isinstance(mode, str) or mode not in {"simple", "complex"}:
        _fail("TASK_MODE_INVALID", "mode只能为simple或complex。")
    if (not isinstance(materials, list) or not materials
            or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in materials)):
        _fail("TASK_MATERIALS_INVALID", "材料须为非空且带有有效id的完整记录列表。")
    records = {r["id"]: r for r in materials}
    if len(records) != len(materials) or not records:
        _fail("TASK_MATERIALS_INVALID", "需要非空且ID不重复的明确材料。")
    texts = {identifier: _material_text(record) for identifier, record in records.items()}
    template_id = proposal.get("template_material_id")
    if template_id and (not isinstance(template_id, str) or template_id not in records or records[template_id]["role"] != "template"):
        _fail("TASK_TEMPLATE_INVALID", "指定的模板必须是本次材料中的template。")
    if template_id and not texts[template_id].strip():
        _fail("TASK_TEMPLATE_UNREADABLE", "模板没有可读正文，须先提供可读材料或经核对的提取结果。")
    template_use = proposal.get("template_use", {})
    if not isinstance(template_use, dict):
        _fail("TASK_TEMPLATE_INVALID", "template_use须为对象。")
    if template_use.get("mode", "reference") != "reference":
        _fail("TASK_TEMPLATE_MODE_UNSUPPORTED", "task-render只参考结构与表达后新建Word；保留原DOCX须使用已支持的原件填充或局部修改入口。")
    if template_id and (not isinstance(template_use, dict)
                        or not isinstance(template_use.get("applied_patterns"), list)
                        or not template_use["applied_patterns"]
                        or any(not isinstance(p, str) or not p.strip() for p in template_use["applied_patterns"])):
        _fail("TASK_TEMPLATE_USE_REQUIRED", "列出本次实际采用的模板结构或表达方式。")
    sections = proposal.get("sections")
    if not isinstance(sections, list) or not sections:
        _fail("TASK_SECTIONS_REQUIRED", "必须提供实际文书正文。")
    checked = copy.deepcopy(proposal)
    checked.update({"mode": mode, "audience": "internal_review"})
    for section in checked["sections"]:
        if not isinstance(section, dict):
            _fail("TASK_SECTION_INVALID", "正文条目须为带kind和text的对象。")
        kind, text = section.get("kind"), section.get("text")
        if not isinstance(kind, str) or kind not in SECTION_KINDS or not isinstance(text, str) or not text.strip():
            _fail("TASK_SECTION_INVALID", "正文条目须有有效kind和非空text。")
        if "assumption" in section and (not isinstance(section["assumption"], str) or not section["assumption"].strip()):
            _fail("TASK_ASSUMPTION_INVALID", "待核假设须写明具体内容，不能只标记true或留空。")
        bindings = _bindings(section, records, texts)
        if kind in {"fact", "argument", "analogy", "authority"} and not bindings and not section.get("assumption"):
            _fail("TASK_BASIS_REQUIRED", "事实与论证须有来源或明确的待核假设。")
        if kind == "fact" and bindings and any(records[b["id"]]["role"] not in CURRENT_FACT_ROLES for b in bindings):
            _fail("TASK_OLD_FACT_TRANSFER", "本案事实不能直接以旧模板、案例或作者观点为来源。")
        if kind in {"authority", "analogy"} and (not isinstance(section.get("verification_status"), str)
                or section["verification_status"] not in {"source_verified", "unverified", "test_only"}):
            _fail("TASK_AUTHORITY_STATUS_REQUIRED", "法源/类案必须保留本次核验状态。")
        section["source_bindings"] = bindings
    analysis = checked.get("analysis", {})
    if mode == "complex":
        if not isinstance(analysis, dict) or not all(analysis.get(k) for k in (
            "issues", "source_comparison", "strongest_adverse_path", "application_conditions", "conclusion_limits"
        )):
            _fail("TASK_COMPLEX_ANALYSIS_REQUIRED", "复杂任务须包含争点、来源比较、最强反方路径、适用条件和结论边界。")
        checked_analysis_bindings = _bindings(analysis, records, texts)
        if not checked_analysis_bindings:
            _fail("TASK_ANALYSIS_SOURCES_REQUIRED", "复杂分析必须绑定实际读取的材料。")
        analysis["source_bindings"] = checked_analysis_bindings
    prose = checked["title"] + "\n" + "\n".join(item["text"] for item in checked["sections"])
    _check_body_labels(prose)
    checked["constraints"] = _validate_constraints(checked.get("constraints", {}))
    _check_literal_constraints(prose, checked["constraints"])
    exclusions = proposal.get("exemplar_exclusions", [])
    if not isinstance(exclusions, list):
        _fail("TASK_EXCLUSION_INVALID", "旧案排除项须为文字列表。")
    for old_text in exclusions:
        if not isinstance(old_text, str) or not old_text:
            _fail("TASK_EXCLUSION_INVALID", "旧案排除项须为非空文字。")
        if old_text in prose:
            _fail("TASK_EXEMPLAR_LEAK", "正文仍含本次明确排除的旧案信息。")
    unresolved = checked.get("unresolved_items", [])
    if not isinstance(unresolved, list) or any(not isinstance(item, str) or not item.strip() for item in unresolved):
        _fail("TASK_UNRESOLVED_INVALID", "待核事项须为非空文字组成的列表。")
    # Revalidate after reading every material; source replacement during the
    # drafting check cannot be accepted as the registered snapshot.
    for record in materials:
        errors = validate_material(record)
        if errors:
            _fail("TASK_SOURCE_CHANGED", "; ".join(errors))
    checked["source_records"] = copy.deepcopy(materials)
    checked["checks"] = {
        "production_labels": "passed",
        "literal_constraints": "passed", "terminology_review": "required",
        "template_layout": "rebuilt_not_preserved" if template_id else "new_document",
        "source_hashes": "passed", "exact_quotes": "passed", "source_roles": "passed",
        "declared_exclusions": "passed", "substantive_review": "required", "visual_review": "required",
        "source_role_check_scope": "Only explicitly tagged fact sections; facts embedded in other prose require semantic review.",
        "permanent_template_registration_required": False, "filing_approved": False,
    }
    return checked


def render_task_draft(materials: list[dict], proposal: dict, output_dir: Path | str) -> dict:
    checked = validate_task_draft(materials, proposal)
    output_dir = Path(output_dir).resolve()
    ensure_not_originals(output_dir / "draft.docx")
    if any(part.casefold() in {"library", "00-originals"} for part in output_dir.parts):
        _fail("TASK_OUTPUT_PROTECTED", "产物目录不能位于正式原件库。")
    if output_dir.exists():
        _fail("OUTPUT_EXISTS", f"任务产物目录已存在，拒绝覆盖：{output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".task-draft-", dir=output_dir.parent)).resolve()
    try:
        lines = ["# " + checked["title"], ""]
        for item in checked["sections"]:
            lines.extend([("## " if item["kind"] == "heading" else "") + item["text"], ""])
        markdown = "\n".join(lines)
        applications = checked.get("learning_applications", [])
        if not isinstance(applications, list):
            _fail("TASK_LEARNING_APPLICATION_INVALID", "learning_applications须为学习来源与应用记录组成的数组。")
        frozen_learning = []
        for use in applications:
            if (not isinstance(use, dict) or set(use) != {"learning_path", "application"}
                    or not isinstance(use["learning_path"], str) or not Path(use["learning_path"]).is_absolute()):
                _fail("TASK_LEARNING_APPLICATION_INVALID", "每项须有learning_path绝对路径和application对象。")
            learning_path = Path(use["learning_path"]).resolve()
            frozen_learning.append((use, learning_path, sha256_file(learning_path)))
        (staging / "draft.md").write_text(markdown, encoding="utf-8")
        create_docx_from_markdown(markdown, staging / "draft.docx", title=checked["title"])
        application_reports = []
        if frozen_learning:
            from .learning import check_learning_application
            for use, learning_path, source_hash in frozen_learning:
                if sha256_file(learning_path) != source_hash:
                    _fail("TASK_LEARNING_SOURCE_CHANGED", "制作期间所用学习记录改变，未发布本稿。")
                # Check the learning record's sources after document generation,
                # including its original, snapshot and extracted-text hashes.
                application_reports.append(check_learning_application(
                    learning_path, use["application"], markdown, materials))
        checked["checks"]["learning_application"] = "structure_and_quotes_checked" if application_reports else "not_requested"
        review_lines = ["# 内部审阅记录", "", "文书为待审初稿，尚未完成实质审阅和逐页版式复核。", ""]
        if checked["constraints"]:
            review_lines.extend(["## 本次持续要求", "", json.dumps(checked["constraints"], ensure_ascii=False, indent=2),
                                 "", "文字存在与否已检查；观点、事实与称谓的实质对应仍需审阅。", ""])
        if checked.get("template_material_id"):
            review_lines.extend(["模板使用方式：参考结构与表达，Word版式重新生成。", ""])
        if checked["mode"] == "complex":
            for key, label in (
                ("issues", "争点"), ("source_comparison", "来源比较"),
                ("strongest_adverse_path", "最强反方路径"), ("application_conditions", "适用条件"),
                ("conclusion_limits", "结论边界"),
            ):
                value = checked["analysis"][key]
                if isinstance(value, list):
                    value = "\n".join(str(v) for v in value)
                review_lines.extend(["## " + label, "", str(value), ""])
        review_lines.extend(["## 待核事项与假设", ""])
        review_lines.extend("- " + str(value) for value in checked.get("unresolved_items", []))
        for item in checked["sections"]:
            if item.get("assumption"):
                review_lines.append("- " + str(item["assumption"]))
        if application_reports:
            review_lines.extend(["", "## 本次学习应用核对", "",
                                 "以下仅核对来源、条件记录和本稿位置；不证明观点正确或已经学会。", ""])
            for report in application_reports:
                for entry in report["entries"]:
                    review_lines.extend(["### " + entry["entry_id"], "",
                                         "应用状态：" + entry["application_status"],
                                         "本次调整：" + entry["adaptation"],
                                         "对应本稿：" + entry["output_binding"]["quote"], ""])
                    for condition in entry["unmet_conditions"]:
                        review_lines.append("- 待核条件：" + condition["condition"] + "；" + condition["basis"])
                    review_lines.extend("- " + limit for limit in entry["limitations"])
        (staging / "review.md").write_text("\n".join(review_lines), encoding="utf-8")
        manifest = {
            "schema_version": "standalone-task-draft-v1", "created_at": now_iso(),
            "mode": checked["mode"], "title": checked["title"], "audience": "internal_review",
            "template_material_id": checked.get("template_material_id"),
            "template_use": checked.get("template_use", {}), "checks": checked["checks"],
            "proposal_sha256": sha256_bytes(canonical_json(proposal).encode("utf-8")),
            "sources": checked["source_records"], "sections": checked["sections"],
            "analysis": checked.get("analysis"), "unresolved_items": checked.get("unresolved_items", []),
            "constraints": checked["constraints"],
            "learning_application_reports": application_reports,
            "files": {name: sha256_file(staging / name) for name in ("draft.docx", "draft.md", "review.md")},
            "database_required": False, "model_calls_executed": 0, "external_actions_executed": 0,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for report in application_reports:
            if sha256_file(Path(report["learning_path"])) != report["learning_source_sha256"]:
                _fail("TASK_LEARNING_SOURCE_CHANGED", "制作期间所用学习记录改变，未发布本稿。")
        for record in materials:
            errors = validate_material(record)
            if errors:
                _fail("TASK_SOURCE_CHANGED", "制作期间本次材料改变：" + "; ".join(errors))
        # Windows rename refuses an existing destination, including empty dirs.
        if output_dir.exists():
            _fail("OUTPUT_EXISTS", "发布前发现同名产物目录，拒绝覆盖。")
        os.rename(staging, output_dir)
        return {"ok": True, "output_dir": str(output_dir), "docx": str(output_dir / "draft.docx"),
                "review": str(output_dir / "review.md"), "manifest": str(output_dir / "manifest.json"),
                "checks": checked["checks"], "external_actions_executed": 0}
    finally:
        if staging.exists() and staging.parent == output_dir.parent and staging.name.startswith(".task-draft-"):
            shutil.rmtree(staging)


def render_docx_patch(source: Path | str, plan: dict, output_dir: Path | str) -> dict:
    """Wrap a bounded DOCX edit in the same task manifest used by task-render.

    Constraints travel with this derivative, not into global or matter memory.
    Exact-string checks do not confer semantic review or filing approval.
    """
    from .docx_edit import patch_docx

    if not isinstance(plan, dict):
        _fail("TASK_PATCH_PLAN_INVALID", "局部修改计划须为JSON对象。")
    source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
    ensure_not_originals(output_dir / "draft.docx")
    if any(part.casefold() == "library" for part in output_dir.parts):
        _fail("TASK_OUTPUT_PROTECTED", "任务产物不能写入正式原件库。")
    if output_dir.exists():
        _fail("OUTPUT_EXISTS", "任务产物目录已存在，拒绝覆盖。")
    effective = copy.deepcopy(plan)
    source_hash = sha256_file(source)
    parent_arg = effective.get("parent_manifest")
    if parent_arg is not None and not isinstance(parent_arg, str):
        _fail("TASK_PARENT_INVALID", "parent_manifest须为明确文件路径。")
    parent_path = Path(parent_arg).resolve() if parent_arg else source.parent / "manifest.json"
    inherited, parent_hash, parent = {}, None, None
    parent_review, parent_review_hash, parent_review_path = "", None, None
    if parent_path.exists() or parent_arg:
        try:
            parent_bytes = parent_path.read_bytes()
            parent = json.loads(parent_bytes.decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise LegalCaseError("TASK_PARENT_INVALID", "无法读取来源旁的任务记录，请定位正确记录后重试。") from exc
        if (not isinstance(parent, dict) or not isinstance(parent.get("files"), dict)
                or parent["files"].get(source.name) != source_hash
                or parent_path.parent != source.parent):
            _fail("TASK_PARENT_SOURCE_MISMATCH", "任务记录未绑定当前原稿版本，不能继承旧要求。")
        parent_hash = sha256_bytes(parent_bytes)
        inherited = copy.deepcopy(parent.get("constraints", {}))
        parent_review_path = parent_path.parent / "review.md"
        try:
            parent_review_bytes = parent_review_path.read_bytes()
            parent_review_hash = sha256_bytes(parent_review_bytes)
            if parent.get("files", {}).get("review.md") != parent_review_hash:
                _fail("TASK_PARENT_REVIEW_MISMATCH", "前版审阅记录已变化或未绑定，未继承未经核对的记录。")
            parent_review = parent_review_bytes.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise LegalCaseError("TASK_PARENT_REVIEW_INVALID", "前版审阅记录不可读，未发布修改稿。") from exc
    additions = effective.get("constraints", {})
    mode = effective.get("constraints_mode", "merge")
    if not isinstance(mode, str) or mode not in {"merge", "replace"}:
        _fail("TASK_CONSTRAINTS_INVALID", "constraints须为对象，constraints_mode仅支持merge/replace。")
    _validate_constraints(additions)
    _validate_constraints(inherited)
    reason = effective.get("constraints_change_reason")
    if mode == "replace" and (not isinstance(reason, str) or not reason.strip()):
        _fail("TASK_CONSTRAINT_CHANGE_REASON_REQUIRED", "替换已有写作要求须记录本次明确修改原因。")
    merged = {} if mode == "replace" else inherited
    for key, value in additions.items():
        if key in {"must_keep", "avoid"}:
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                _fail("TASK_CONSTRAINTS_INVALID", "must_keep/avoid须为非空文字组成的列表。")
            previous = merged.get(key, [])
            if not isinstance(previous, list) or any(not isinstance(v, str) for v in previous):
                _fail("TASK_CONSTRAINTS_INVALID", "来源任务中的写作要求无效。")
            merged[key] = list(dict.fromkeys(previous + value))
        elif key == "terminology":
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in value.items()):
                _fail("TASK_CONSTRAINTS_INVALID", "terminology须为角色到指定称谓的文字映射。")
            previous = merged.get(key, {})
            if not isinstance(previous, dict):
                _fail("TASK_CONSTRAINTS_INVALID", "来源任务中的称谓记录无效。")
            if any(k in previous and previous[k] != v for k, v in value.items()):
                _fail("TASK_CONSTRAINT_CONFLICT", "称谓与此前要求冲突；请明确替换要求及原因。")
            merged[key] = {**previous, **value}
        else:
            _fail("TASK_CONSTRAINTS_INVALID", f"不支持的写作要求字段：{key}")
    effective["constraints"] = merged
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".task-patch-", dir=output_dir.parent))
    try:
        change_record = patch_docx(source, staging / "draft.docx", effective)
        literal = change_record["constraint_checks"]
        if any(not item["present"] for item in literal["must_keep"]) or any(item["present"] for item in literal["avoid"]):
            _fail("TASK_CONSTRAINTS_UNSATISFIED", "修改后未满足持续有效的文字要求：" + json.dumps(literal, ensure_ascii=False))
        # Persist the final path, never the staging path removed by publication.
        change_record["output"] = change_record["docx"] = str(output_dir / "draft.docx")
        review = ["# 修改与审阅记录", "", "本次仅修改计划指定的位置；原稿保持不变。",
                  "字面要求检查不代替观点、事实与称谓的语义核对；尚需实质与页面审阅。", ""]
        if merged:
            review.extend(["## 持续有效的本次要求", json.dumps(merged, ensure_ascii=False, indent=2), ""])
        if parent_hash:
            review.extend([f"前版记录：{parent_path}", "前版未决事项和来源核验状态不因本次修改而自动解决。", ""])
            review.extend(["## 前版审阅记录（保留，不表示已解决）", "", parent_review, ""])
        (staging / "review.md").write_text("\n".join(review), encoding="utf-8")
        checks = {"source_hashes": "passed", "change_scope": "passed", "literal_constraints": "passed",
                  "terminology_review": "required",
                  "learning_application_review": "required" if parent and (parent.get("learning_application_reports") or
                      parent.get("prior_source_context", {}).get("learning_application_reports")) else "not_requested",
                  "substantive_review": "required", "visual_review": "required", "filing_approved": False}
        manifest = {"schema_version": "docx-local-patch-v1", "created_at": now_iso(),
                    "audience": "internal_review", "source": {"path": str(source), "sha256": source_hash},
                    "proposal_sha256": sha256_bytes(canonical_json(plan).encode("utf-8")),
                    "parent_manifest": str(parent_path) if parent_hash else None, "parent_manifest_sha256": parent_hash,
                    "parent_review_sha256": parent_review_hash, "parent_checks": parent.get("checks") if parent else None,
                    "prior_source_context": (parent.get("prior_source_context") or
                        {key: parent[key] for key in ("sources", "sections", "analysis", "learning_application_reports", "page_map", "page_map_sha256",
                                                      "source_hashes", "catalog_source") if key in parent}) if parent else {},
                    "constraints": merged, "constraints_mode": mode,
                    "constraints_change_reason": effective.get("constraints_change_reason"),
                    "changes": change_record, "checks": checks,
                    "unresolved_items": parent.get("unresolved_items", []) if parent else [],
                    "files": {name: sha256_file(staging / name) for name in ("draft.docx", "review.md")},
                    "model_calls_executed": 0, "external_actions_executed": 0}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if (sha256_file(source) != source_hash or (parent_hash and sha256_file(parent_path) != parent_hash)
                or (parent_review_hash and sha256_file(parent_review_path) != parent_review_hash)):
            _fail("TASK_SOURCE_CHANGED", "生成期间原稿或来源记录改变，未发布修改稿。")
        if output_dir.exists():
            _fail("OUTPUT_EXISTS", "发布前发现同名任务目录，拒绝覆盖。")
        os.rename(staging, output_dir)
        return {"ok": True, "docx": str(output_dir / "draft.docx"), "review": str(output_dir / "review.md"),
                "manifest": str(output_dir / "manifest.json"), "checks": checks, "constraints": merged,
                "external_actions_executed": 0}
    finally:
        if staging.exists() and staging.parent == output_dir.parent and staging.name.startswith(".task-patch-"):
            shutil.rmtree(staging)
