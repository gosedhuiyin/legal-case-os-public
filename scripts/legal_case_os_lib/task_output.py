"""Render source-linked task drafts without requiring a database or matter state.

The current agent supplies the writing and analysis. This module verifies source
identity and quoted bindings and creates real files; it does not claim to judge
legal correctness, infer missing facts, approve a filing, or learn model weights.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .core import LegalCaseError, canonical_json, ensure_not_originals, now_iso, sha256_bytes, sha256_file
from .materials import read_material, validate_material
from .templates import create_docx_from_markdown


CURRENT_FACT_ROLES = {"current_case", "evidence", "user_statement"}
SECTION_KINDS = {"heading", "fact", "argument", "analogy", "authority", "administrative"}


def _fail(code: str, message: str) -> None:
    raise LegalCaseError(code, message)


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
        (staging / "draft.md").write_text(markdown, encoding="utf-8")
        create_docx_from_markdown(markdown, staging / "draft.docx", title=checked["title"])
        review_lines = ["# 内部审阅记录", "", "文书为待审初稿，尚未完成实质审阅和逐页版式复核。", ""]
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
        (staging / "review.md").write_text("\n".join(review_lines), encoding="utf-8")
        manifest = {
            "schema_version": "standalone-task-draft-v1", "created_at": now_iso(),
            "mode": checked["mode"], "title": checked["title"], "audience": "internal_review",
            "template_material_id": checked.get("template_material_id"),
            "template_use": checked.get("template_use", {}), "checks": checked["checks"],
            "proposal_sha256": sha256_bytes(canonical_json(proposal).encode("utf-8")),
            "sources": checked["source_records"], "sections": checked["sections"],
            "analysis": checked.get("analysis"), "unresolved_items": checked.get("unresolved_items", []),
            "files": {name: sha256_file(staging / name) for name in ("draft.docx", "draft.md", "review.md")},
            "database_required": False, "model_calls_executed": 0, "external_actions_executed": 0,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
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
