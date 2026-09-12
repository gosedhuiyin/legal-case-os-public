#!/usr/bin/env python3
"""Offline acceptance runner for legal-case-os v1.2 synthetic fixtures.

All fixture content is TEST-ONLY.  The static suite uses only Python's standard
library.  Unless --static-only is supplied, the runner also exercises the root
CLI routing contract once scripts/legal_case_os.py is available.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
FIXTURES_ROOT = TESTS_ROOT / "fixtures"
CONTRACTS_ROOT = TESTS_ROOT / "contracts"


@dataclass
class Result:
    check_id: str
    status: str
    detail: str


class Acceptance:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def pass_(self, check_id: str, detail: str) -> None:
        self.results.append(Result(check_id, "PASS", detail))

    def fail(self, check_id: str, detail: str) -> None:
        self.results.append(Result(check_id, "FAIL", detail))

    def skip(self, check_id: str, detail: str) -> None:
        self.results.append(Result(check_id, "SKIP", detail))

    def check(self, check_id: str, fn: Callable[[], str]) -> None:
        try:
            self.pass_(check_id, fn())
        except Exception as exc:  # each check must report independently
            self.fail(check_id, f"{type(exc).__name__}: {exc}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    require(isinstance(value, dict), f"{path} 顶层必须是对象")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_object_hash(value: dict[str, Any]) -> str:
    payload = copy.deepcopy(value)
    payload.pop("content_hash", None)
    payload.pop("hash", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def by_id(items: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = item.get(key)
        require(isinstance(item_id, str) and item_id, f"缺少 {key}")
        require(item_id not in result, f"{key} 重复：{item_id}")
        result[item_id] = item
    return result


def check_json_and_test_markers() -> str:
    paths = sorted(FIXTURES_ROOT.rglob("*.json")) + sorted(CONTRACTS_ROOT.rglob("*.json"))
    require(paths, "未找到 JSON 夹具")
    for path in paths:
        data = load_json(path)
        marker = data.get("test_only") is True
        if path.name == "case-state.json":
            marker = marker or data.get("matter", {}).get("environment") == "test"
            marker = marker and "TEST-ONLY" in data.get("matter", {}).get("title", "")
        require(marker, f"缺少 TEST-ONLY 顶层标识：{path.relative_to(TESTS_ROOT)}")
    return f"{len(paths)} 个 JSON 文件均可解析且有 TEST-ONLY 标识"


def check_original_markers_and_no_personal_identifiers() -> str:
    originals = sorted(FIXTURES_ROOT.glob("*-case/00-originals/*"))
    require(len(originals) == 8, f"应有 8 份原始夹具，实际 {len(originals)}")
    risky_patterns = {
        "中国大陆身份证号": re.compile(r"(?<!\d)[1-9]\d{16}[0-9Xx](?!\d)"),
        "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        "电子邮箱": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    }
    for path in originals:
        text = path.read_text(encoding="utf-8")
        require("TEST-ONLY" in text[:120], f"原始夹具首段缺少 TEST-ONLY：{path.name}")
        require("虚构" in text or "测试" in text, f"未明确说明虚构：{path.name}")
        for label, pattern in risky_patterns.items():
            require(not pattern.search(text), f"{path.name} 疑似包含{label}")
    return "8 份原始材料均明确虚构，未检测到身份证号、手机号或邮箱"


def check_original_hashes() -> str:
    baseline = load_json(FIXTURES_ROOT / "baseline-hashes.json")
    files = baseline.get("files", {})
    require(len(files) == 8, "哈希基线必须覆盖 8 份原始材料")
    for rel, expected in files.items():
        path = FIXTURES_ROOT / rel
        require(path.is_file(), f"基线文件不存在：{rel}")
        actual = sha256(path)
        require(actual == expected, f"原始材料哈希变化：{rel}\nexpected={expected}\nactual={actual}")
    return "8 份原始材料 SHA-256 与只读基线一致"


def check_case_state_sources() -> str:
    checked = 0
    lifecycle_collections = {
        "material_batches", "case_events", "triage_cards", "deadline_records",
        "impact_assessments", "prospective_matter_seeds", "workflow_instances", "run_attempts",
    }
    focus_lifecycle_fields = {
        "current_batch_id", "current_event_id", "current_workflow_instance_id", "current_task_id",
        "current_memory_mode", "read_memory_scope", "state_version", "snapshot",
    }
    for case_name in ("simple-case", "complex-case"):
        state_path = FIXTURES_ROOT / case_name / "_case-state" / "case-state.json"
        require(state_path.is_file(), f"缺少状态夹具：{state_path.relative_to(TESTS_ROOT)}")
        state = load_json(state_path)
        require(state.get("matter", {}).get("environment") == "test", f"{case_name} 不是测试环境")
        require("TEST-ONLY" in state.get("matter", {}).get("title", ""), f"{case_name} 缺少测试标题")
        require(
            state.get("schema_version") in {"1.1.0", "1.2.0"},
            f"{case_name} 必须是可读的 1.1.0 兼容夹具或当前 1.2.0 测试状态",
        )
        require(
            lifecycle_collections <= set(state),
            f"{case_name} 缺少生命周期增量集合：{sorted(lifecycle_collections - set(state))}",
        )
        require(
            focus_lifecycle_fields <= set(state.get("focus", {})),
            f"{case_name} 缺少生命周期 FocusContext 字段",
        )
        if state.get("schema_version") == "1.2.0":
            require(isinstance(state.get("composition_specs"), list), f"{case_name} 缺少 v1.2 CompositionSpec 集合")
            require("current_composition_spec_id" in state.get("focus", {}), f"{case_name} 缺少 v1.2 合成焦点")
        require(state.get("memory_state", {}).get("storage_scope") == "matter_workspace", f"{case_name} 案件记忆不得存入全局")
        for source in state.get("sources", []):
            rel = source.get("path")
            expected_hash = source.get("sha256") or source.get("hash")
            require(rel and expected_hash, f"{case_name} 来源缺少 path/hash")
            source_path = FIXTURES_ROOT / case_name / rel
            require(source_path.is_file(), f"来源路径不存在：{source_path}")
            require(sha256(source_path) == expected_hash, f"状态哈希不匹配：{source_path.name}")
            checked += 1
    require(checked == 8, f"状态来源应覆盖 8 份原始材料，实际 {checked}")
    return "两个既有案件以 v1.1 兼容输入或 v1.2 当前状态读取，完整引用 8 份原始材料且哈希一致"


def check_simple_case_gate() -> str:
    state = load_json(FIXTURES_ROOT / "simple-case" / "_case-state" / "case-state.json")
    require(len(state.get("sources", [])) == 4, "简单案应有四类原始材料")
    evidence = state.get("evidence", [])
    require(len(evidence) == 4, "简单案应建立四项候选证据")
    assessment_fields = {
        "authenticity", "legality", "relevance", "probative_strength", "adverse_content",
        "opens_new_issue", "opponent_likely_use", "duplication", "timing",
        "consequence_if_not_submitted", "substitute", "applicable_stage",
        "procedural_authority_id", "risk_assessment_artifact_id",
    }
    for item in evidence:
        require(assessment_fields <= set(item), f"{item['id']} 缺少证据评估字段：{sorted(assessment_fields - set(item))}")
    approved = [item for item in evidence if item.get("lawyer_decision") == "submit_now"]
    require(0 < len(approved) < len(evidence), "测试必须体现“不是全部材料自动提交”")
    require(all(item.get("current_submission") is True for item in approved), "批准证据应标记当前提交")
    unapproved_current = [
        item for item in evidence
        if item.get("current_submission") is True
        and item.get("lawyer_decision") != "submit_now"
    ]
    require(not unapproved_current, "未批准证据不得标记为当前提交")
    packages = state.get("package_manifests", [])
    require(len(packages) == 1 and packages[0].get("status") == "candidate", "简单案应只有一个 G4 候选包")
    package_evidence = {
        item["object_id"] for item in packages[0].get("items", []) if item.get("object_type") == "evidence"
    }
    approved_ids = {item["id"] for item in approved}
    require(package_evidence == approved_ids, "G4 候选包证据必须精确等于当前律师批准集合")
    g2 = next(
        item for item in state.get("approvals", [])
        if item.get("gate") == "G2_evidence" and item.get("decision") == "approved" and item.get("status") == "active"
    )
    scope = {(item["object_id"], item["version"], item["hash"]) for item in g2.get("scope_snapshot", [])}
    sources = {item["id"]: item for item in state.get("sources", [])}
    for item in approved:
        require((item["id"], item["version"], canonical_object_hash(item)) in scope, f"G2 未精确绑定证据 {item['id']}")
        for locator in item.get("source_refs", []):
            source = sources[locator["source_id"]]
            require((source["id"], source["version"], source["sha256"]) in scope, f"G2 未精确绑定来源 {source['id']}")
    scoped_evidence_ids = {object_id for object_id, _, _ in scope if object_id.startswith("E-")}
    require(scoped_evidence_ids == approved_ids, "G2 证据快照必须与 current_submission 精确一致")
    for artifact in state.get("artifacts", []):
        artifact_path = FIXTURES_ROOT / "simple-case" / artifact["path"]
        require(artifact_path.is_file(), f"成果文件不存在：{artifact['path']}")
        require(sha256(artifact_path) == artifact["sha256"], f"成果哈希不匹配：{artifact['path']}")
    expected_manifest_hash = hashlib.sha256(
        json.dumps(packages[0]["items"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    require(expected_manifest_hash == packages[0]["manifest_hash"], "G4 清单哈希必须精确绑定有序项目快照")
    require(not state.get("external_actions"), "简单案夹具不得预置 G5 外部动作")
    return f"四项候选中仅 {len(approved)} 项进入精确 G4 候选包，成果哈希一致，G5 动作为零"


def check_complex_case_scope_and_adversity() -> str:
    state = load_json(FIXTURES_ROOT / "complex-case" / "_case-state" / "case-state.json")
    issues = by_id(state.get("issues", []), "id")
    issue = issues.get("I-LIMITATION-001")
    require(issue is not None, "缺少 I-LIMITATION-001")
    require(issue.get("analysis_mode") == "deep", "指定争点必须处于 deep")
    require(issue.get("scope_lock") is True, "指定争点必须锁定局部深析范围")
    other_deep = [item for item in issues.values() if item["id"] != "I-LIMITATION-001" and item.get("analysis_mode") == "deep"]
    require(not other_deep, "不应强迫其他争点进入深析")
    require(len(issue.get("conflicting_dates", [])) == 3, "必须记录三个冲突日期")
    require(issue.get("strongest_supportive_path"), "缺少有利路径")
    require(issue.get("strongest_adverse_path"), "缺少重大不利路径")
    communication = next((item for item in state.get("evidence", []) if item.get("id") == "E-TEST-COMM-001"), None)
    require(communication is not None and communication.get("adverse") is True, "双向通信必须保留不利属性")
    require("同时有利与不利" in communication.get("description", ""), "双向通信必须标记同时有利和不利")
    require(communication.get("adverse_content") and communication.get("opponent_likely_use"), "重大不利通信必须记录内容和对方利用路径")
    return "仅 I-LIMITATION-001 深析，三个日期及通信双向效果均保留"


def check_authorities() -> str:
    data = load_json(FIXTURES_ROOT / "authority-corpus" / "TEST-ONLY-authorities.json")
    authorities = data.get("authorities", [])
    require(len(authorities) == 3, "模拟法源库必须正好三条")
    positions = {item.get("position") for item in authorities}
    require("supportive" in positions and "adverse" in positions, "必须同时有支持和不利法源")
    adverse = [item for item in authorities if item.get("major_adverse")]
    require(len(adverse) == 1 and adverse[0]["authority_id"] == "A-TEST-ADVERSE-001", "重大不利法源配置错误")
    summary = next(item for item in authorities if item["authority_id"] == "A-TEST-SUMMARY-001")
    require(summary.get("verification_status") == "summary_only_unverified", "第三条必须仅摘要不可核验")
    require(summary.get("expected_use") == "research_lead_only_never_cite", "不可核验摘要只能作为线索")
    require(all(item.get("production_eligible") is False for item in authorities), "TEST-ONLY 法源不得进入生产引用")
    require(data.get("expected", {}).get("production_citation_ids") == [], "生产引用预期必须为空")
    computed_report_ids = sorted(
        item["authority_id"] for item in authorities
        if item.get("full_text_available") is True
        and item.get("verification_status") == "verified_against_local_test_record"
        and item.get("position") in {"supportive", "adverse"}
    )
    require(computed_report_ids == sorted(data["expected"]["research_report_ids"]), "离线研究策略生成的报告集合与预期不一致")
    require("A-TEST-ADVERSE-001" in computed_report_ids, "用户只找支持案例时，实际计算的研究报告仍必须保留重大不利结果")
    require("A-TEST-SUMMARY-001" not in computed_report_ids, "仅摘要不可核验线索不得冒充已核验研究结论")
    return "离线策略实际计算出支持+重大不利两项报告记录；仅摘要排除；生产可引用数量为零"


def check_template_scenarios() -> str:
    registry = load_json(FIXTURES_ROOT / "template-cases" / "TEST-ONLY-template-registry.json")
    scenarios = load_json(FIXTURES_ROOT / "template-cases" / "TEST-ONLY-template-scenarios.json")
    templates = by_id(registry.get("templates", []), "template_id")
    cases = by_id(scenarios.get("cases", []), "case_id")
    required = {
        "TPL-CASE-EXISTS": (True, None),
        "TPL-CASE-MISSING": (False, "template_not_found"),
        "TPL-CASE-LICENSE-UNKNOWN": (False, "license_unknown"),
        "TPL-CASE-REQUIRED-MISSING": (False, "required_fields_missing"),
        "TPL-CASE-STALE": (False, "template_stale"),
    }
    require(set(cases) == set(required), "模板情形必须严格覆盖五种状态")
    for case_id, (allowed, reason) in required.items():
        expected = cases[case_id].get("expected", {})
        require(expected.get("allowed") is allowed, f"{case_id} allowed 错误")
        require(expected.get("stop_reason") == reason, f"{case_id} 阻断原因错误")
    valid = templates["TPL-TEST-COMPLAINT-VALID"]
    require(valid.get("license") and valid.get("status") == "current", "有效模板必须有许可且为 current")
    require((FIXTURES_ROOT / "template-cases" / valid["path"]).is_file(), "有效模板文件不存在")
    return "存在、缺失、许可不明、字段缺失、版本过期五情形均有精确预期"


def check_routing_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "routing-cases.json")
    cases = by_id(data.get("cases", []), "case_id")
    require(len(cases) == 31, f"应有 31 条路由用例，实际 {len(cases)}")
    required_fragments = [
        "看一下这个材料", "大致内容", "不要那么复杂", "找一下支持的案例",
        "生成起诉状Word", "内部备注", "了解得怎么样", "很简单", "整理下文件夹",
        "只分析时效", "站在对方角度", "先不要提交", "简版提交稿", "压缩到3000字",
        "只修改第二项", "不要打印、不要提交", "材料更新了", "不要联网", "个人理财", "不存在的某某套件",
        "依照简模001号", "标准买卖合同起诉状模板", "调用下记忆模块", "不用关联记忆",
        "不要管其他记忆", "又来了一批材料", "不要写入记忆", "记成候选", "正式记入案件", "继续上次的工单",
    ]
    texts = "\n".join(item.get("text", "") for item in cases.values())
    for fragment in required_fragments:
        require(fragment in texts, f"缺少口语覆盖：{fragment}")
    concise = cases["ROUTE-003"]["expected"]
    require(concise.get("reasoning_depth") == "deep", "不要复杂不得降低内部深析")
    require(concise.get("presentation_length") == "brief", "不要复杂应缩短展示")
    package = cases["ROUTE-016"]["expected"]
    require(package.get("external_action") == "forbidden", "待打印包不得触发外部动作")
    require(cases["ROUTE-019"]["expected"].get("primary_skill") is None, "个人理财不得触发本项目")
    memory_modes = {cases[f"ROUTE-{number:03d}"]["expected"].get("memory_read_mode") for number in range(23, 31)}
    require(memory_modes == {"off", "file_scoped", "relevant", "reflect"}, "新增口语必须覆盖四种记忆读取模式")
    memory_write_modes = {cases[f"ROUTE-{number:03d}"]["expected"].get("memory_write_mode") for number in range(23, 31)}
    require(memory_write_modes == {"none", "candidate", "commit"}, "新增口语必须覆盖三种记忆写入模式")
    require(all(cases[f"ROUTE-{number:03d}"]["expected"].get("external_action") == "forbidden" for number in range(23, 31)), "记忆/生命周期口语不得授权外部动作")
    return "31 条口语覆盖完整，含四种记忆读取、三种写入、生命周期恢复及既有安全边界"


def dotted(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise AssertionError(f"缺少字段路径：{path}")
        current = current[part]
    return current


def check_language_control_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "language-control-cases.json")
    cases = by_id(data.get("cases", []), "case_id")
    require(len(cases) == 60, f"自然语言控制合同应有 60 条，实际 {len(cases)}")
    texts = "\n".join(item.get("text", "") for item in cases.values())
    for fragment in (
        "不难，生成下", "研究下", "探讨下", "先别生成", "只看送达问题",
        "不要看其他材料", "删除文件、提交法院", "把邮件发送给客户",
        "起草一封给客户的邮件", "再深一点", "以后都简短一点", "这次简短一点",
        "不要查案例", "不要撤诉", "不要保存文件", "仅依据当前消息",
        "邮件内容为：请把材料提交法院", "上传到法院系统", "替我立案",
        "以后都不要简短", "冷眼过筛", "只分析包装破损问题",
    ):
        require(fragment in texts, f"自然语言控制合同缺少：{fragment}")
    for schema_name in (
        "task-frame.schema.json", "clarification-decision.schema.json",
        "run-spec.schema.json", "preference-candidate.schema.json",
    ):
        schema = load_json(PROJECT_ROOT / "shared" / "schemas" / schema_name)
        require(schema.get("$schema", "").endswith("2020-12/schema"), f"{schema_name} 未声明 Draft 2020-12")
        require(schema.get("additionalProperties") is False, f"{schema_name} 顶层必须封闭字段")
    run_spec_schema = load_json(PROJECT_ROOT / "shared" / "schemas" / "run-spec.schema.json")
    require(
        set(run_spec_schema["properties"]["allowed_tools"]["items"].get("enum", []))
        == {"local_read", "case_state_read", "verified_public_web_read", "local_derived_write", "configured_library_read"},
        "RunSpec allowed_tools 必须使用封闭白名单",
    )
    clarification_schema = load_json(PROJECT_ROOT / "shared" / "schemas" / "clarification-decision.schema.json")
    preference_schema = load_json(PROJECT_ROOT / "shared" / "schemas" / "preference-candidate.schema.json")
    require(clarification_schema.get("allOf"), "澄清 Schema 必须约束 ask/question 和 R3/block 的组合")
    preference_properties = preference_schema.get("properties", {})
    require(
        preference_properties.get("status", {}).get("const") == "none"
        and preference_properties.get("promotion_eligible", {}).get("const") is False
        and preference_properties.get("requires_user_approval", {}).get("const") is False,
        "语言模块的偏好兼容接口必须固定关闭学习、晋升和确认路径",
    )
    require(
        {"independent_draft_review", "source_and_assumption_labels"} <= set(cases["LANG-001"]["contains"]["run_spec.validators"]),
        "Quick 内部起草合同必须保留来源及审阅检查",
    )
    require(
        "independent_review" in cases["LANG-003"]["contains"]["run_spec.required_artifacts"],
        "Deep 合同必须包含独立复核",
    )
    require(
        cases["LANG-011"]["equals"].get("run_spec.external_actions_allowed") is False,
        "外部动作合同必须保持 G5 禁止",
    )
    for case in cases.values():
        require("equals" in case, f"{case['case_id']} 缺少精确预期")
        serialized = json.dumps(case.get("equals", {}), ensure_ascii=False)
        require("preference_update" not in serialized, f"{case['case_id']} 不得进入偏好学习任务")
    return "60 条语言控制合同覆盖否定、范围、单问门控、执行配方、命令/数据隔离、外部动作与关闭的学习接口"


def check_incremental_fixture_storage() -> str:
    root = FIXTURES_ROOT / "incremental-case"
    materials = sorted((root / "materials").glob("*.md"))
    require(len(materials) == 8, f"增量案件应有 8 份虚构材料，实际 {len(materials)}")
    for path in materials:
        text = path.read_text(encoding="utf-8")
        require("TEST-ONLY" in text[:120], f"增量材料缺少 TEST-ONLY：{path.name}")
        require("虚构" in text or "测试" in text, f"增量材料未说明虚构：{path.name}")
    current_view = root / "_case-state" / "memory" / "TEST-ONLY-current-case-view.md"
    require(current_view.is_file(), "案件文件夹内缺少当前案件视图")
    view_text = current_view.read_text(encoding="utf-8")
    require("可重建" in view_text and "不是真实案件事实来源" in view_text, "派生视图必须声明可重建且不是事实来源")

    contract = load_json(CONTRACTS_ROOT / "memory-mode-cases.json")
    storage = contract.get("canonical_storage", {})
    workspace = Path(storage.get("matter_workspace", ""))
    require(not workspace.is_absolute(), "TEST-ONLY 案件记忆路径必须随项目相对定位")
    require(storage.get("canonical_state") == "_case-state/case-state.json", "唯一状态必须在案件目录的 _case-state")
    require(storage.get("derived_memory_directory") == "_case-state/memory", "派生记忆必须位于案件目录的专用记忆目录内")
    require(storage.get("global_case_memory_allowed") is False, "不得把具体案件记忆写入系统级全局存储")
    require(storage.get("copy_original_content_into_memory") is False, "记忆不得复制原件全文")
    require(storage.get("chat_transcript_is_authority") is False, "聊天记录不得成为案件权威状态")
    require(storage.get("portable_with_matter_folder") is True, "案件记忆必须跟随案件文件夹移动")
    return "8 份增量材料与一页派生视图均为 TEST-ONLY；案件记忆限定在案件目录且不复制原件/聊天"


def check_incremental_case_state() -> str:
    root = FIXTURES_ROOT / "incremental-case"
    state = load_json(root / "_case-state" / "case-state.json")
    require(
        state.get("schema_version") in {"1.1.0", "1.2.0"},
        "增量案件必须是可读的 1.1.0 兼容夹具或当前 1.2.0 状态",
    )
    if state.get("schema_version") == "1.2.0":
        require(isinstance(state.get("composition_specs"), list), "v1.2 增量案件缺少 CompositionSpec 集合")
        require("current_composition_spec_id" in state.get("focus", {}), "v1.2 增量案件缺少合成焦点")
    require(state.get("matter", {}).get("environment") == "test" and "TEST-ONLY" in state.get("matter", {}).get("title", ""), "增量案件必须明确 TEST-ONLY")
    require(state.get("memory_state", {}).get("storage_scope") == "matter_workspace", "增量案件记忆必须存于案件工作区")
    audit_sequence = state.get("audit", {}).get("last_sequence", 0)
    focus_version = state.get("focus", {}).get("state_version")
    minimum_version = max(audit_sequence, 1)
    maximum_version = audit_sequence + 1 if audit_sequence >= 1 else 1
    require(minimum_version <= focus_version <= maximum_version, "Focus状态版本必须是有效、单调递增的投影版本")
    require(len(state.get("sources", [])) == 8, "增量状态必须登记八份测试来源")
    for source in state["sources"]:
        path = root / source["path"]
        require(path.is_file(), f"增量来源不存在：{source['path']}")
        require(sha256(path) == source["sha256"], f"增量来源哈希不一致：{source['id']}")
        require(source.get("original") is True and source.get("read_only") is True, f"增量来源必须只读：{source['id']}")

    batches = by_id(state.get("material_batches", []), "id")
    cards = by_id(state.get("triage_cards", []), "id")
    events = by_id(state.get("case_events", []), "id")
    require(len(batches) == len(cards) == len(events) == 8, "八份增量材料必须各有批次、事件和分诊卡")
    ordinary = cards["TC-TEST-001"]
    require(ordinary.get("relevance") == "none" and ordinary.get("action_route") == "no_action", "普通邮件必须归档且不触发分析")
    deadline = by_id(state.get("deadline_records", []), "id")["DL-TEST-001"]
    require(deadline.get("status") == "candidate" and deadline.get("verified_by") is None, "日期材料只能形成未核验DeadlineRecord")
    client_evidence = by_id(state.get("evidence", []), "id")["E-TEST-CLIENT-INSISTS-001"]
    require(client_evidence.get("client_instruction_event_ids") == ["CE-TEST-003"], "客户坚持提交必须绑定客户指令事件")
    require(client_evidence.get("ai_recommendation") == "do_not_submit" and client_evidence.get("lawyer_decision") == "undecided", "客户指令不得覆盖AI判断或律师门禁")
    require(client_evidence.get("current_submission") is False and not state.get("package_manifests"), "G2前证据与候选包提交数量必须为零")
    seed = by_id(state.get("prospective_matter_seeds", []), "id")["SEED-TEST-001"]
    require(seed.get("status") == "candidate" and seed.get("human_decision") == "pending", "第三批弱信号只能形成候选Seed")
    require(seed.get("trigger_event_ids") == ["CE-TEST-004", "CE-TEST-005", "CE-TEST-006"], "Seed必须精确绑定三批弱信号事件")
    require(seed.get("promoted_matter_id") is None, "律师批准前不得创建新Matter")
    impact = by_id(state.get("impact_assessments", []), "id")["IA-TEST-001"]
    require(set(impact.get("stale_object_ids", [])) == {"F-TEST-DELIVERY-001", "I-TEST-PERFORMANCE-001"}, "影响候选只能声明局部依赖")
    require("F-TEST-IDENTITY-001" in impact.get("counterevidence_object_ids", []), "局部影响必须保留无关/相反检查对象")
    workflow = by_id(state.get("workflow_instances", []), "id")["WF-TEST-WITHDRAWAL-001"]
    capsule_ids = [item for item in workflow.get("related_object_ids", []) if item == "R-TEST-PROCEDURAL-CAPSULE-001"]
    require(capsule_ids == ["R-TEST-PROCEDURAL-CAPSULE-001"], "程序工单必须只绑定程序胶囊产物")
    failed_run = by_id(state.get("run_attempts", []), "id")["RUN-TEST-001"]
    require(failed_run.get("status") == "failed" and workflow.get("status") == "in_progress", "RunAttempt失败不得完成Workflow")
    require(not state.get("external_actions"), "增量案件不得预置G5外部动作")
    for artifact in state.get("artifacts", []):
        path = root / artifact["path"]
        require(path.is_file() and sha256(path) == artifact["sha256"], f"增量派生产物哈希错误：{artifact['id']}")
    return "增量案件兼容状态、八批事件/分诊、期限、客户指令、局部影响、Seed、程序工单及失败RunAttempt均可复核"


def check_memory_mode_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "memory-mode-cases.json")
    read_cases = by_id(data.get("read_mode_cases", []), "case_id")
    require(set(read_cases) == {"MEM-READ-OFF", "MEM-READ-FILE", "MEM-READ-RELEVANT", "MEM-READ-REFLECT"}, "读取模式必须精确覆盖四种情形")
    read_modes = {item["expected"]["read_mode"] for item in read_cases.values()}
    require(read_modes == {"off", "file_scoped", "relevant", "reflect"}, "读取模式枚举不完整")
    off = read_cases["MEM-READ-OFF"]["expected"]
    require(off.get("read_scope") == [] and "matter_memory" in off.get("forbidden_inputs", []), "off 必须真正禁止案件记忆")
    scoped = read_cases["MEM-READ-FILE"]["expected"]
    require(scoped.get("maximum_relation_hops") == 1, "文件限定模式只能展开一层直接关系")
    require("unrelated_matter_memory" in scoped.get("must_exclude", []), "文件限定模式必须排除无关记忆")
    relevant = read_cases["MEM-READ-RELEVANT"]["expected"]
    require(relevant.get("maximum_loaded_cards", 0) > 0 and relevant.get("must_publish_context_manifest") is True, "相关模式必须有预算并公开上下文清单")
    reflect = read_cases["MEM-READ-REFLECT"]["expected"]
    require(reflect.get("maximum_candidate_insights") == 3 and reflect.get("candidate_only") is True, "反思模式最多提出三个候选且不得直接定论")
    require(reflect.get("must_include_sources_and_counterevidence") is True, "反思必须同时绑定来源与反证")

    write_cases = by_id(data.get("write_mode_cases", []), "case_id")
    require({item["expected"]["write_mode"] for item in write_cases.values()} == {"none", "candidate", "commit"}, "写入模式必须精确覆盖 none/candidate/commit")
    require(write_cases["MEM-WRITE-NONE"]["expected"].get("canonical_state_mutations") == 0, "none 不得修改规范状态")
    require(write_cases["MEM-WRITE-CANDIDATE"]["expected"].get("canonical_fact_promotions") == 0, "candidate 不得自动提升为正式事实")
    commit = write_cases["MEM-WRITE-COMMIT"]["expected"]
    require(commit.get("exact_source_required") is True and commit.get("next_state_version") == commit.get("expected_base_state_version") + 1, "commit 必须绑定精确来源并原子递增版本")

    conversations = by_id(data.get("conversation_cases", []), "case_id")
    shared = conversations["CHAT-SAME-STATE-NO-PRIVATE-MEMORY"]["expected"]
    require(shared.get("canonical_state_count") == 1 and shared.get("private_case_memory_per_chat") is False, "多个对话必须共享一个案件状态且不得各建私有记忆")
    stale = conversations["CHAT-STALE-COMMIT-CONFLICT"]["expected"]
    require(stale.get("stale_commit_allowed") is False and stale.get("error_code") == "STALE_STATE_VERSION", "过期对话提交必须冲突而非覆盖")
    return "四种读取、三种写入及共享状态版本冲突语义均已锁定"


def check_incremental_lifecycle_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "incremental-lifecycle-cases.json")
    batches = by_id(data.get("batch_cases", []), "case_id")
    idem = batches["BATCH-IDEMPOTENT"]["expected"]
    require(idem.get("repeat_new_batch_count") == 0 and idem.get("source_duplicates_created") == 0, "重复批次必须幂等且不得复制来源")
    require(idem.get("cursor_after_repeat") == idem.get("cursor_after_first"), "重复批次不得重复推进游标")
    failure = batches["BATCH-FAILURE-CURSOR"]
    require(failure["expected"].get("cursor_after_failure") == failure["input"].get("base_cursor"), "失败批次不得推进游标")
    require(failure["expected"].get("canonical_semantic_mutations") == 0, "失败批次不得留下半提交语义变更")

    triage = by_id(data.get("triage_cases", []), "case_id")
    ordinary = triage["TRIAGE-ORDINARY-NO-DEEP"]["expected"]
    require(ordinary.get("route") == "archive_only" and ordinary.get("deep_analysis_runs") == 0, "普通无意义材料不得触发深析")
    require(sum(ordinary.get(key, -1) for key in ("new_evidence_count", "new_deadline_count", "new_seed_count")) == 0, "普通材料不得制造证据、期限或新案")
    date = triage["TRIAGE-DATE-CANDIDATE"]["expected"]
    require(date.get("deadline_status") == "candidate" and "treat_as_verified_deadline" in date.get("must_not", []), "日期材料只能形成未核验期限候选")
    require({"original_text", "source_locator", "received_at", "timezone", "verification_status"} <= set(date.get("must_preserve", [])), "期限候选必须保留原文、定位、收件时间、时区和核验状态")
    insists = triage["TRIAGE-CLIENT-INSISTS"]["expected"]
    require(insists.get("ai_recommendation") == "do_not_submit" and insists.get("lawyer_decision") == "undecided", "客户坚持不得覆盖AI分析与律师未决状态")
    require(insists.get("current_submission_before_G2") is False and insists.get("package_evidence_count_before_G2") == 0, "G2前客户要求的材料不得进入当前提交或包")

    impact = by_id(data.get("impact_cases", []), "case_id")["IMPACT-DECLARED-DEPENDENCIES-ONLY"]
    require(impact["expected"].get("invalidate_exactly") == impact.get("declared_dependency_ids"), "只允许失效声明的依赖")
    require(set(impact["expected"].get("preserve_current", [])) == set(impact.get("unrelated_ids", [])), "无关对象必须保持当前状态")
    require(impact["expected"].get("whole_matter_rerun") is False, "局部影响不得全案重跑")

    emergence = by_id(data.get("emergence_cases", []), "case_id")["SEED-THREE-BATCH-THRESHOLD"]
    seed_counts = [item.get("expected_seed_count") for item in emergence.get("batches", [])]
    require(seed_counts == [0, 0, 1], "弱信号前两批必须休眠，第三批才生成一个Seed")
    require(emergence["expected"].get("seed_status") == "candidate" and emergence["expected"].get("new_matter_count_before_lawyer_approval") == 0, "Seed只能作为候选，律师批准前不得新建立案")

    workflows = by_id(data.get("workflow_cases", []), "case_id")
    capsule = workflows["WORKFLOW-PROCEDURAL-CAPSULE-ONLY"]["expected"]
    require(capsule.get("context_kind") == "ProceduralCapsule" and capsule.get("entity_evidence_loaded") == 0, "纯程序工单只能加载ProceduralCapsule，不得加载实体证据")
    require(capsule.get("deep_analysis_runs") == 0, "纯程序工单不得默认深析")
    failed_run = workflows["RUN-FAILURE-DOES-NOT-COMPLETE-WORKFLOW"]["expected"]
    require(failed_run.get("run_status") == "failed" and failed_run.get("workflow_status") != "completed", "RunAttempt失败不得完成Workflow")
    require(failed_run.get("completed_at") is None and failed_run.get("failed_output_is_filing_candidate") is False, "失败运行不得留下完成时间或法院候选")

    invariants = data.get("global_invariants", {})
    require(invariants.get("external_actions_created") == 0 and invariants.get("external_actions_executed") == 0, "增量/生命周期流程的外部动作必须为零")
    require(invariants.get("automatic_print_send_upload_submit") is False, "不得自动打印、发送、上传或提交")
    return "批次、分诊、期限、客户指令、局部失效、弱信号、程序工单、运行失败和G5零动作合同完整"


def check_approval_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "approval-ok-cases.json")
    cases = by_id(data.get("cases", []), "case_id")
    require(set(cases) == {"OK-NONE", "OK-UNIQUE-CURRENT", "OK-MULTIPLE", "OK-STALE"}, "ok 必须覆盖四种情形")
    approved = [item for item in cases.values() if item["expected"].get("approval_created")]
    require(len(approved) == 1 and approved[0]["case_id"] == "OK-UNIQUE-CURRENT", "只能有唯一当前对象获批")
    exact = approved[0]
    require(exact["expected"]["approved_object_hash"] == exact["focus"]["exact_pending_approval_object"]["object_hash"], "批准必须绑定展示哈希")
    stale = cases["OK-STALE"]
    require(stale["focus"]["exact_pending_approval_object"]["object_hash"] != stale["current_objects"][0]["object_hash"], "过期场景必须真正发生哈希变化")
    return "无候选、唯一候选、多候选、过期候选四种 ok 语义正确"


def check_evidence_gate_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "evidence-gate-cases.json")
    evidence = by_id(data.get("evidence", []), "id")
    schema = load_json(PROJECT_ROOT / "shared" / "schemas" / "case-state.schema.json")["$defs"]["Evidence"]
    allowed_fields = set(schema["properties"])
    required_fields = set(schema["required"])
    for item in evidence.values():
        require(not (set(item) - allowed_fields), f"Evidence 合同含非 schema 字段：{sorted(set(item) - allowed_fields)}")
        require(not (required_fields - set(item)), f"Evidence 合同缺必填字段：{sorted(required_fields - set(item))}")
        for field, definition in schema["properties"].items():
            if field in item and "enum" in definition:
                require(item[field] in definition["enum"], f"Evidence {item['id']} 的 {field} 使用非法枚举：{item[field]}")
    computed = sorted(
        item_id for item_id, item in evidence.items()
        if item.get("lawyer_decision") == "submit_now" and item.get("current_submission") is True
    )
    expected = data.get("expected", {})
    require(computed == sorted(expected.get("package_evidence_ids", [])), "提交包证据必须由律师决定集合派生")
    require(expected.get("unapproved_in_package_count") == 0, "未批准证据计数必须为零")
    require(not (set(expected.get("must_exclude_ids", [])) & set(computed)), "排除证据错误进入提交包")
    court_only = evidence["E-TEST-006"]
    require(court_only.get("service_obligation_status") == "requires_verification", "只给法院意图在程序核验前必须阻断")
    require(court_only.get("current_submission") is False, "只给法院意图不能自动进入提交包")
    ai_only = [item for item in evidence.values() if item.get("ai_recommendation") == "submit_now" and item.get("lawyer_decision") == "undecided"]
    require(ai_only and all(not item.get("current_submission") for item in ai_only), "AI 候选不得自动升级")
    return f"6 项合同均使用真实 Evidence schema 字段；律师批准证据 {len(computed)} 项；未批准进入包为零；披露意图边界有效"


def check_staleness_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "staleness-cases.json")
    cases = by_id(data.get("cases", []), "case_id")
    main = cases["STALE-SOURCE-HASH-001"]
    require(main["event"]["old_hash"] != main["event"]["new_hash"], "哈希必须实际变化")
    invalid = set(main["expected"].get("invalidate", []))
    required = {"P-TEST-G2-001", "R-TEST-DRAFT-001", "P-TEST-G4-001", "B-TEST-PACKAGE-001"}
    require(required <= invalid, f"失效传播缺项：{sorted(required - invalid)}")
    require(main["expected"].get("old_approval_reusable") is False, "旧批准不得复用")
    require(main["expected"].get("old_package_reusable") is False, "旧包不得复用")
    unrelated = cases["STALE-UNRELATED-SOURCE-001"]["expected"]
    require(required & set(unrelated.get("must_not_invalidate", [])), "无关变更必须有不误伤断言")
    return "同名文件哈希变化会传播至 G2、文书、G4和包；无关变更不误伤"


def check_degradation_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "degradation-cases.json")
    cases = by_id(data.get("cases", []), "case_id")
    require(set(cases) == {
        "DEGRADE-NETWORK-DENIED", "DEGRADE-PUBLIC-WEB-UNAVAILABLE",
        "DEGRADE-MEMBER-DATABASE-ABSENT", "DEGRADE-OCR-PARTIAL",
    }, "降级场景不完整")
    unavailable = cases["DEGRADE-PUBLIC-WEB-UNAVAILABLE"]["expected"]
    require("claim_database_searched" in unavailable.get("must_not", []), "不得伪称已检索数据库")
    require("invent_case" in unavailable.get("must_not", []), "不得虚构案例")
    denied = cases["DEGRADE-NETWORK-DENIED"]["expected"]
    require("call_public_web" in denied.get("must_not", []), "禁网场景不得调用公网")
    partial = cases["DEGRADE-OCR-PARTIAL"]["expected"]
    require("claim_full_coverage" in partial.get("must_not", []), "OCR 部分失败不得声称全覆盖")
    return "禁网、公网不可用、会员库缺失、OCR 部分失败均有明确降级边界"


def check_sanitization_contract() -> str:
    data = load_json(CONTRACTS_ROOT / "sanitization-cases.json")
    require(data.get("pipeline") == [
        "independent_review", "lawyer_approve_exact_items", "derive_copy",
        "write_sanitization_report", "independent_rereview",
    ], "清洁流水线顺序错误")
    items = by_id(data.get("items", []), "item_id")
    expected = data.get("expected", {})
    computed_removed = sorted(item_id for item_id, item in items.items() if item.get("expected_result") == "removed")
    computed_blocked = sorted(item_id for item_id, item in items.items() if item.get("expected_result") == "blocked")
    require(computed_removed == sorted(expected.get("removed_item_ids", [])), "可清除项目集合不一致")
    require(computed_blocked == sorted(expected.get("blocked_item_ids", [])), "阻断项目集合不一致")
    require(items["SAN-TEST-006"].get("kind") == "third_party_evidence_watermark", "必须覆盖第三方证据水印")
    require(items["SAN-TEST-007"].get("reason") == "route_to_evidence_gate_or_drafting", "实质不利内容必须返回专业门禁")
    require(data["source"]["sha256"] == expected.get("source_hash_after"), "清洁前后原件哈希必须一致")
    require(expected.get("derived_copy_required") is True, "必须生成派生副本")
    require(expected.get("rereview_status_after_tool") == "required", "工具清洁后仍须独立复审")
    require(expected.get("court_candidate_eligible_before_rereview") is False, "复审前不得升级法院候选")
    return "4 项获批自有残留可移除，4 项真实性/实体内容阻断，原件不变且必须复审"


def parse_json_output(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise AssertionError("CLI 没有 JSON 输出")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
        for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
            try:
                value = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if value is None:
            raise AssertionError(f"无法解析 CLI JSON：{text[-500:]}")
    require(isinstance(value, dict), "CLI JSON 顶层必须是对象")
    return value


def find_cli() -> Path | None:
    for name in ("legal_case_os.py", "legal_case_cli.py"):
        path = PROJECT_ROOT / "scripts" / name
        if path.is_file():
            return path
    return None


def python_executable() -> str:
    candidate = Path(sys.executable)
    if candidate.is_file():
        return str(candidate)
    discovered = shutil.which("python") or shutil.which("python3")
    require(discovered is not None, "找不到可执行 Python 解释器")
    return discovered


def run_cli(cli: Path, *args: str, allow_exit_2: bool = False) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [python_executable(), str(cli), *args],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    allowed = {0, 2} if allow_exit_2 else {0}
    require(completed.returncode in allowed, f"CLI exit={completed.returncode}: {completed.stderr or completed.stdout}")
    payload = parse_json_output(completed.stdout)
    return completed.returncode, payload, completed.stderr


def check_cli_help(cli: Path) -> str:
    completed = subprocess.run(
        [python_executable(), str(cli), "--help"], cwd=str(PROJECT_ROOT), text=True,
        encoding="utf-8", errors="replace", capture_output=True, timeout=30, check=False,
    )
    require(completed.returncode == 0, f"--help 失败：{completed.stderr}")
    help_text = completed.stdout + completed.stderr
    commands = [
        "init", "upgrade-state", "index", "ingest-batch", "context-build", "memory-view", "triage-commit",
        "deadline-verify", "impact-apply", "seed-promote", "workflow-start", "workflow-update",
        "run-start", "run-finish", "reconcile-matter", "validate", "route", "approve-ok", "invalidate", "preflight",
        "sanitize-docx", "min-diff", "template-fill", "template-ref-validate", "template-ref-resolve",
        "template-distill", "template-register", "template-fill-plan", "template-fill-docx",
        "template-repeat-plan", "template-hybrid-plan", "template-structural-apply",
        "composition-build", "composition-validate", "citation-audit", "exemplar-leak-check",
        "name", "bundle", "print-sheet",
    ]
    missing = [command for command in commands if command not in help_text]
    require(not missing, f"CLI 缺少命令：{missing}")
    return f"CLI 暴露 {len(commands)} 个计划命令"


def check_cli_init_and_index(cli: Path) -> str:
    baseline = load_json(FIXTURES_ROOT / "baseline-hashes.json")["files"]
    fixture_hashes_before = {rel: sha256(FIXTURES_ROOT / rel) for rel in baseline}
    with tempfile.TemporaryDirectory(prefix="legal-case-os-init-test-") as temp:
        workspace = Path(temp) / "TEST-ONLY-matter"
        _, initialized, _ = run_cli(
            cli, "init", "--workspace", str(workspace), "--matter-id", "M-TEST-INIT-001",
            "--title", "TEST-ONLY 初始化与索引案", "--environment", "test", "--actor", "synthetic_test",
            "--json",
        )
        require(initialized.get("ok") is True, "init 应成功")
        require(initialized.get("external_actions_executed") == 0, "init 不得执行外部动作")
        originals = workspace / "00-originals"
        for source in sorted((FIXTURES_ROOT / "simple-case" / "00-originals").iterdir()):
            shutil.copy2(source, originals / source.name)
        copied_before = {path.name: sha256(path) for path in originals.iterdir() if path.is_file()}
        _, indexed, _ = run_cli(
            cli, "index", "--workspace", str(workspace), "--actor", "synthetic_test", "--json",
        )
        require(indexed.get("ok") is True, "index 应成功")
        require(indexed.get("file_count", 0) >= 4, "index 至少应覆盖四份测试材料")
        copied_after = {path.name: sha256(path) for path in originals.iterdir() if path.is_file()}
        require(copied_after == copied_before, "index 不得修改、移动或删除 00-originals 内容")
        state = load_json(workspace / "_case-state" / "case-state.json")
        require(all(item.get("original") and item.get("read_only") for item in state.get("sources", [])), "索引后的原件必须全为只读来源")
        require((workspace / "10-index" / "material-index.json").is_file(), "缺少派生材料索引")
    fixture_hashes_after = {rel: sha256(FIXTURES_ROOT / rel) for rel in baseline}
    require(fixture_hashes_after == fixture_hashes_before, "init/index 测试不得修改固定原始夹具")
    return "临时工作区完成 init/index；原件文件集和 SHA-256 均保持不变，外部动作为零"


def check_cli_state_upgrade(cli: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="legal-case-os-upgrade-test-") as temp:
        workspace = Path(temp) / "TEST-ONLY-v1-matter"
        shutil.copytree(PROJECT_ROOT / "matter-workspace-template", workspace)
        state_path = workspace / "_case-state" / "case-state.json"
        state = load_json(state_path)
        state["schema_version"] = "1.0.0"
        state["matter"].update(
            {
                "id": "M-TEST-UPGRADE-001",
                "title": "TEST-ONLY v1.0迁移案",
                "environment": "test",
            }
        )
        state["focus"]["current_matter_id"] = "M-TEST-UPGRADE-001"
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
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        original = workspace / "00-originals" / "TEST-ONLY-upgrade-original.md"
        original.write_text("TEST-ONLY｜虚构迁移原件｜不得提交\n", encoding="utf-8")
        original_hash = sha256(original)

        _, upgraded, _ = run_cli(
            cli, "upgrade-state", "--state", str(state_path),
            "--expected-state-version", "1", "--actor", "synthetic_test_lawyer", "--json",
        )
        require(upgraded.get("ok") is True and upgraded.get("idempotent") is False, "v1.0应显式迁移")
        require(upgraded.get("schema_version") == "1.2.0", "迁移后必须写入v1.2")
        require(upgraded.get("external_actions_executed") == 0, "迁移不得执行外部动作")
        migrated = load_json(state_path)
        require(migrated.get("memory_state", {}).get("storage_scope") == "matter_workspace", "迁移必须建立案件内记忆")
        require(isinstance(migrated.get("composition_specs"), list), "迁移必须建立v1.2合成清单集合")
        require("current_composition_spec_id" in migrated.get("focus", {}), "迁移必须建立v1.2合成焦点")
        require(migrated.get("focus", {}).get("state_version") == upgraded.get("state_version"), "迁移后Focus版本必须同步")
        for relative in ("indexes", "cards", "context-capsules", "workflows", "runs"):
            require((workspace / "_case-state" / "memory" / relative).is_dir(), f"迁移缺少记忆目录：{relative}")
        require(sha256(original) == original_hash, "状态迁移不得修改原件")
        _, validated, _ = run_cli(cli, "validate", "--state", str(state_path), "--json")
        require(validated.get("ok") is True, "迁移后的案件状态必须通过完整校验")

        before_repeat = sha256(state_path)
        _, repeated, _ = run_cli(
            cli, "upgrade-state", "--state", str(state_path),
            "--expected-state-version", str(upgraded.get("state_version")), "--json",
        )
        require(repeated.get("idempotent") is True, "重复迁移v1.2应为幂等no-op")
        require(sha256(state_path) == before_repeat, "幂等迁移不得改写状态")
    return "v1.0案件显式迁移到v1.2，案件内记忆与合成集合齐全、重复迁移幂等且原件不变"


def check_cli_routes(cli: Path) -> str:
    data = load_json(CONTRACTS_ROOT / "routing-cases.json")
    checked = 0
    failures: list[str] = []
    simple_state = FIXTURES_ROOT / "simple-case" / "_case-state" / "case-state.json"
    complex_state = FIXTURES_ROOT / "complex-case" / "_case-state" / "case-state.json"
    incremental_state = FIXTURES_ROOT / "incremental-case" / "_case-state" / "case-state.json"
    matter_states = {
        "M-TEST-SIMPLE-001": simple_state,
        "M-TEST-COMPLEX-001": complex_state,
        "M-TEST-INCREMENTAL-001": incremental_state,
    }
    for case in data["cases"]:
        expected = case["expected"]
        if expected.get("action") == "out_of_scope":
            continue
        try:
            route_args = ["route", "--text", case["text"]]
            if case["case_id"] in {"ROUTE-003", "ROUTE-004", "ROUTE-010", "ROUTE-011"}:
                route_args.extend(["--state", str(complex_state)])
            elif case.get("context", {}).get("current_matter") in matter_states:
                # These contracts explicitly declare an active matter.  Pass its
                # canonical state instead of accidentally testing a no-context turn.
                route_args.extend(["--state", str(matter_states[case["context"]["current_matter"]])])
            route_args.append("--json")
            _, payload, _ = run_cli(cli, *route_args)
            route = payload.get("route", {})
            intent = payload.get("intent", {})
            if route.get("action") != expected.get("action"):
                raise AssertionError(f"action={route.get('action')} expected={expected.get('action')}")
            expected_skill = expected.get("primary_skill")
            if expected_skill and route.get("skill") != expected_skill:
                raise AssertionError(f"skill={route.get('skill')} expected={expected_skill}")
            if expected.get("reasoning_depth") and intent.get("reasoning_depth") != expected["reasoning_depth"]:
                raise AssertionError(f"reasoning_depth={intent.get('reasoning_depth')} expected={expected['reasoning_depth']}")
            expected_length = expected.get("presentation_length")
            if expected_length in {"brief", "normal", "detailed"} and intent.get("display_length") != expected_length:
                raise AssertionError(f"display_length={intent.get('display_length')} expected={expected_length}")
            if expected.get("audience") and intent.get("audience") != expected["audience"]:
                raise AssertionError(f"audience={intent.get('audience')} expected={expected['audience']}")
            if expected.get("scope_lock") is not None and route.get("scope_lock") != expected["scope_lock"]:
                raise AssertionError(f"scope_lock={route.get('scope_lock')} expected={expected['scope_lock']}")
            if expected.get("analysis_lens") and route.get("analysis_lens") != expected["analysis_lens"]:
                raise AssertionError(f"analysis_lens={route.get('analysis_lens')} expected={expected['analysis_lens']}")
            if expected.get("template_alias") and intent.get("template_alias") != expected["template_alias"]:
                raise AssertionError(f"template_alias={intent.get('template_alias')} expected={expected['template_alias']}")
            if case["case_id"] in {"ROUTE-021", "ROUTE-022"} and expected.get("next_skill") not in route.get("next_skills", []):
                raise AssertionError(f"next_skills={route.get('next_skills')} expected to include {expected['next_skill']}")
            if expected.get("output_format") and expected["output_format"] not in intent.get("output_formats", []):
                raise AssertionError(f"output_formats={intent.get('output_formats')} expected to include {expected['output_format']}")
            if expected.get("allowed_modify_scope") and intent.get("modify_scope") != expected["allowed_modify_scope"]:
                raise AssertionError(f"modify_scope={intent.get('modify_scope')} expected={expected['allowed_modify_scope']}")
            memory_policy = route.get("memory_policy", {})
            if expected.get("memory_read_mode") and memory_policy.get("read_mode") != expected["memory_read_mode"]:
                raise AssertionError(f"memory.read_mode={memory_policy.get('read_mode')} expected={expected['memory_read_mode']}")
            if expected.get("memory_read_scope") is not None and memory_policy.get("read_scope") != expected["memory_read_scope"]:
                raise AssertionError(f"memory.read_scope={memory_policy.get('read_scope')} expected={expected['memory_read_scope']}")
            if expected.get("memory_write_mode") and memory_policy.get("write_mode") != expected["memory_write_mode"]:
                raise AssertionError(f"memory.write_mode={memory_policy.get('write_mode')} expected={expected['memory_write_mode']}")
            if expected.get("external_action") == "forbidden" and payload.get("safety", {}).get("external_actions_allowed") is not False:
                raise AssertionError("memory/lifecycle route must keep external_actions_allowed=false")
            if expected.get("memory_read_mode"):
                if memory_policy.get("canonical_store") != "matter_workspace":
                    raise AssertionError("case memory must be stored in matter_workspace")
                if memory_policy.get("chat_transcript_is_authority") is not False:
                    raise AssertionError("chat transcript must not become canonical memory")
            network_expectation = expected.get("network_policy")
            network_map = {"deny": "deny", "public_web_allowed": "public_web_if_available"}
            if network_expectation in network_map and intent.get("network_mode") != network_map[network_expectation]:
                raise AssertionError(f"network_mode={intent.get('network_mode')} expected={network_map[network_expectation]}")
            if case["case_id"] == "ROUTE-003":
                require(payload.get("safety", {}).get("presentation_only_reduction") is True, "不要复杂必须仅缩短展示")
            if case["case_id"] == "ROUTE-004":
                policy = route.get("research_policy", {})
                require(policy.get("include_material_adverse") is True, "支持案例检索必须显式保留重大不利结果")
                require(policy.get("exclude_unverified_from_court_candidate") is True, "未核验法源必须排除出法院候选")
            if case["case_id"] == "ROUTE-011":
                require(route.get("required_output") == expected.get("must_include"), "反方视角必须显式要求不利事实、证据缺口和改变结论的事实")
            checked += 1
        except Exception as exc:
            failures.append(f"{case['case_id']}: {exc}")
    require(not failures, "；".join(failures))

    _, evidence_versions, _ = run_cli(
        cli, "route", "--text", "这个证据做简版和详细版", "--json",
    )
    require(evidence_versions.get("route", {}).get("skill") == "legal-evidence-gate", "证据简版/详细版不得误路由至文书起草")

    _, concise_without_state, _ = run_cli(
        cli, "route", "--text", "不要那么复杂，告诉我主要问题", "--json",
    )
    require(concise_without_state.get("intent", {}).get("reasoning_depth") == "standard", "否定短语中的“复杂”不得凭空触发深析")
    require(concise_without_state.get("intent", {}).get("display_length") == "brief", "不要复杂必须缩短展示")

    _, concise_with_risk, _ = run_cli(
        cli, "route", "--text", "不要那么复杂，告诉我主要问题",
        "--state", str(complex_state), "--json",
    )
    require(concise_with_risk.get("intent", {}).get("reasoning_depth") == "deep", "已有重大复杂争点时不得因简短展示降级内部深析")
    require(concise_with_risk.get("safety", {}).get("reasoning_depth_preserved") is True, "复杂案应明确标记内部深度已保留")

    _, short_research, _ = run_cli(cli, "route", "--text", "帮我找支持案例", "--json")
    require(short_research.get("route", {}).get("skill") == "legal-authority-research", "短句‘帮我找支持案例’必须进入法源研究")
    require(short_research.get("intent", {}).get("network_mode") == "public_web_if_available", "支持案例短句必须声明可用时公网检索")

    _, short_modify, _ = run_cli(cli, "route", "--text", "只改第二项诉请", "--json")
    require(short_modify.get("intent", {}).get("modify_scope") == ["claim_item_2"], "短句‘只改第二项诉请’必须锁定第二项诉请")
    require(short_modify.get("route", {}).get("required_output") == ["minimal_change_diff"], "局部修改必须要求最小修改差异")

    for finance_text in ("财务规划", "做工资存款规划", "投资建议", "帮我规划工资和存款投资"):
        _, finance_blocked, _ = run_cli(
            cli, "route", "--text", finance_text, "--json", allow_exit_2=True,
        )
        require(finance_blocked.get("code") == "OUT_OF_SCOPE", f"{finance_text} 必须由法律专用范围门禁拒绝")

    no_web_capabilities = json.dumps({
        "local_files": True, "public_web": False,
        "member_database": False, "third_party_api": False,
    }, ensure_ascii=False)
    _, no_web, _ = run_cli(
        cli, "route", "--text", "帮我找支持案例",
        "--capabilities", no_web_capabilities, "--json",
    )
    no_web_assessment = no_web.get("capability_assessment", {})
    require(no_web_assessment.get("status") == "degraded_or_stopped", "公网不可用时研究路由必须明确降级或停止")
    require(
        {"public_web_unavailable", "search_incomplete", "unverified_items_blocked"}
        <= set(no_web_assessment.get("must_report", [])),
        "公网不可用降级必须报告检索不完整并阻断未核验项",
    )
    require(no_web_assessment.get("external_services_called") == [], "能力评估不得伪装已经调用外部服务")

    public_only_capabilities = json.dumps({
        "local_files": True, "public_web": True,
        "member_database": False, "third_party_api": False,
    }, ensure_ascii=False)
    _, public_only, _ = run_cli(
        cli, "route", "--text", "检索案例",
        "--capabilities", public_only_capabilities, "--json",
    )
    public_assessment = public_only.get("capability_assessment", {})
    require(public_assessment.get("status") == "continue_with_public_sources", "只有公网可用时应限定为公共来源继续")
    require(
        {"member_database_not_connected", "third_party_api_not_connected", "source_scope"}
        <= set(public_assessment.get("must_report", [])),
        "会员库/API缺失必须报告未连接及来源范围",
    )

    partial_ocr = json.dumps({"local_files": True, "ocr": "partial", "audio_transcription": False}, ensure_ascii=False)
    _, partial_material, _ = run_cli(
        cli, "route", "--text", "看一下全部材料", "--capabilities", partial_ocr, "--json",
    )
    partial_assessment = partial_material.get("capability_assessment", {})
    require(partial_material.get("route", {}).get("skill") == "legal-material-intake", "全部材料短句必须进入材料工位")
    require(partial_assessment.get("status") == "partial_with_blockers", "OCR部分失败必须进入带阻断的部分覆盖状态")
    require(
        {"processed_range", "failed_pages", "manual_check_items"}
        <= set(partial_assessment.get("must_report", [])),
        "OCR部分失败必须报告覆盖范围、失败页和人工核对项",
    )
    return f"CLI 正确路由 {checked} 条合同口语及短检索、短修改、财务越界、三类能力降级等回归"


def check_cli_language_control(cli: Path) -> str:
    data = load_json(CONTRACTS_ROOT / "language-control-cases.json")
    failures: list[str] = []
    compiled: dict[str, dict[str, Any]] = {}
    checked = 0
    for case in data["cases"]:
        try:
            args = ["route", "--text", case["text"]]
            state_path: Path | None = None
            if case.get("state_fixture"):
                state_path = FIXTURES_ROOT / case["state_fixture"] / "_case-state" / "case-state.json"
                before_hash = sha256(state_path)
                args.extend(["--state", str(state_path)])
            args.append("--json")
            _, payload, _ = run_cli(cli, *args)
            if state_path is not None:
                require(sha256(state_path) == before_hash, f"{case['case_id']} route 不得修改案件状态")

            for key in ("task_frame", "clarification", "run_spec", "preference_candidate"):
                require(isinstance(payload.get(key), dict), f"缺少语言控制对象：{key}")
                require(payload[key].get("schema_version") == "1.0.0", f"{key} schema_version 错误")
            for path, expected in case.get("equals", {}).items():
                actual = dotted(payload, path)
                require(actual == expected, f"{path}={actual!r} expected={expected!r}")
            for path, expected_items in case.get("contains", {}).items():
                actual = dotted(payload, path)
                require(isinstance(actual, list), f"{path} 应为列表")
                require(set(expected_items) <= set(actual), f"{path}={actual!r} 未包含 {expected_items!r}")
            for path, excluded_items in case.get("excludes", {}).items():
                actual = dotted(payload, path)
                if isinstance(actual, list):
                    require(not (set(excluded_items) & set(actual)), f"{path} 不应包含 {excluded_items!r}")
                elif isinstance(actual, str):
                    require(not any(item in actual for item in excluded_items), f"{path} 不应包含 {excluded_items!r}")
                else:
                    raise AssertionError(f"{path} 无法执行排除检查")
            for path in case.get("non_null", []):
                require(dotted(payload, path) is not None, f"{path} 不得为空")
            expected_segment_kinds = set(case.get("segment_source_kinds", []))
            if expected_segment_kinds:
                actual_segment_kinds = {
                    item.get("source_kind") for item in payload["task_frame"].get("input_segments", [])
                }
                require(expected_segment_kinds <= actual_segment_kinds, f"输入分段缺少 {expected_segment_kinds}")

            clarification = payload["clarification"]
            require(clarification.get("max_questions") == 1, "每轮最多只能问一个澄清问题")
            if clarification.get("decision") == "ask":
                require(isinstance(clarification.get("question"), str) and clarification["question"], "ask 必须只有一个非空问题")
            require(payload["run_spec"].get("external_actions_allowed") is False, "所有配方都必须保持 G5=false")
            require(payload["run_spec"].get("case_memory_commit_allowed") is False, "路由编译不得提交案件记忆")
            preference = payload["preference_candidate"]
            require(preference.get("status") == "none", "语言模块不得生成偏好或别名学习候选")
            require(preference.get("promotion_eligible") is False, "语言模块不得晋升偏好或别名")
            require(preference.get("requires_user_approval") is False, "关闭的学习接口不得发起保存确认")
            require(
                all(preference.get(key) is None for key in ("kind", "scope", "key", "value", "source_span")),
                "关闭的学习接口不得携带候选记录",
            )
            require(preference.get("case_memory_write_requested") is False, "偏好不得进入案件记忆")
            compiled[case["case_id"]] = payload
            checked += 1
        except Exception as exc:
            failures.append(f"{case['case_id']}: {exc}")
    require(not failures, "；".join(failures))
    scripts_root = str(PROJECT_ROOT / "scripts")
    if scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)
    from legal_case_os_lib.language import validate_language_control_semantics

    def control_bundle(payload: dict[str, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(payload[key]) for key in (
            "task_frame", "clarification", "run_spec", "preference_candidate",
        )}

    missing_gate = control_bundle(compiled["LANG-001"])
    missing_gate["run_spec"]["validators"].remove("independent_draft_review")
    require(validate_language_control_semantics(missing_gate), "语义校验必须拒绝删除独立审阅的快速内部文书配方")

    blocked_with_tool = control_bundle(compiled["LANG-030"])
    blocked_with_tool["run_spec"]["allowed_tools"] = ["local_read"]
    require(validate_language_control_semantics(blocked_with_tool), "语义校验必须拒绝给 R3 阻断任务分配工具")

    unresolved_acl = control_bundle(compiled["LANG-009"])
    unresolved_acl["task_frame"]["read_acl"]["items"] = []
    require(validate_language_control_semantics(unresolved_acl), "语义校验必须拒绝空白名单直接执行")

    control_reads_case = control_bundle(compiled["LANG-015"])
    control_reads_case["task_frame"]["memory_context_policy"]["read_mode"] = "relevant"
    require(validate_language_control_semantics(control_reads_case), "语义校验必须拒绝纯语言控制读取案件记忆")

    learning_candidate = control_bundle(compiled["LANG-015"])
    learning_candidate["preference_candidate"]["status"] = "candidate"
    require(validate_language_control_semantics(learning_candidate), "语义校验必须拒绝语言模块生成学习候选")
    return f"CLI 通过 {checked} 条语言控制回归及 5 条跨对象篡改测试，route 全程只读且学习接口固定关闭"


def clone_incremental_workspace(root: Path, name: str) -> tuple[Path, Path]:
    workspace = root / name
    shutil.copytree(FIXTURES_ROOT / "incremental-case", workspace)
    return workspace, workspace / "_case-state" / "case-state.json"


def write_json_payload(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def check_cli_memory_context(cli: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="legal-case-os-memory-test-") as temp:
        root = Path(temp)
        workspace, state_path = clone_incremental_workspace(root, "TEST-ONLY-memory-matter")

        _, off, _ = run_cli(
            cli, "context-build", "--state", str(state_path), "--mode", "off", "--json",
        )
        off_capsule = off.get("capsule", {})
        require(off.get("ok") is True and off_capsule.get("mode") == "off", "off 模式应成功生成显式关闭清单")
        require(off_capsule.get("included") == [] and off_capsule.get("context_slice") == {}, "off 模式不得加载案件记忆对象")
        require(off_capsule.get("coverage") == "memory_intentionally_disabled", "off 模式必须明确记忆被有意关闭")
        require(
            off_capsule.get("state_version") == load_json(state_path).get("focus", {}).get("state_version"),
            "上下文胶囊必须报告规范投影版本，不得退回审计序号",
        )

        scoped_output = workspace / "_case-state" / "memory" / "TEST-ONLY-file-scope-capsule.json"
        _, scoped, _ = run_cli(
            cli, "context-build", "--state", str(state_path), "--mode", "file_scoped",
            "--scope", "materials/TEST-ONLY-02-court-notice.md", "--max-items", "20",
            "--output", str(scoped_output), "--json",
        )
        scoped_capsule = scoped.get("capsule", {})
        require(scoped.get("ok") is True and scoped_output.is_file(), "文件限定上下文应写入案件内记忆目录")
        require(scoped_capsule.get("mode") == "file_scoped" and scoped_capsule.get("requested_scope") == ["materials/TEST-ONLY-02-court-notice.md"], "文件限定范围不得扩大")
        included_source_ids = {
            item.get("object_id") for item in scoped_capsule.get("included", []) if item.get("collection") == "sources"
        }
        require(included_source_ids == {"S-TEST-INCREMENTAL-002"}, f"文件限定模式加载了无关来源：{sorted(included_source_ids)}")
        require("DL-TEST-001" in {item.get("object_id") for item in scoped_capsule.get("included", [])}, "文件限定模式应带入直接关联的期限候选")

        _, relevant, _ = run_cli(
            cli, "context-build", "--state", str(state_path), "--mode", "relevant",
            "--query", "撤诉程序工单", "--max-items", "6", "--json",
        )
        relevant_capsule = relevant.get("capsule", {})
        require(len(relevant_capsule.get("included", [])) <= 6, "relevant 模式必须遵守上下文预算")
        require(relevant_capsule.get("invariants", {}).get("chat_transcript_is_authority") is False, "聊天不得成为权威来源")
        require(relevant_capsule.get("invariants", {}).get("memory_read_authorizes_write") is False, "读取记忆不得授权写入")
        require("WF-TEST-WITHDRAWAL-001" in {item.get("object_id") for item in relevant_capsule.get("included", [])}, "相关模式应召回撤诉工单")

        _, reflect, _ = run_cli(
            cli, "context-build", "--state", str(state_path), "--mode", "reflect",
            "--query", "跨批次弱信号和潜在新案", "--max-items", "12", "--json",
        )
        reflected_ids = {item.get("object_id") for item in reflect.get("capsule", {}).get("included", [])}
        require("SEED-TEST-001" in reflected_ids, "reflect 模式应召回跨批次Seed候选")
        require(len(reflected_ids) <= 12, "reflect 模式也必须遵守上下文预算")

        outside = root / "TEST-ONLY-global-memory.json"
        _, rejected, _ = run_cli(
            cli, "context-build", "--state", str(state_path), "--mode", "relevant",
            "--output", str(outside), "--json", allow_exit_2=True,
        )
        require(rejected.get("code") == "MEMORY_OUTPUT_OUTSIDE_MATTER" and not outside.exists(), "案件记忆写到案件目录外必须被阻断")

        generated_view = workspace / "_case-state" / "memory" / "TEST-ONLY-generated-current-view.md"
        _, view, _ = run_cli(
            cli, "memory-view", "--state", str(state_path), "--output", str(generated_view),
            "--expected-state-version", "1", "--actor", "synthetic_test_lawyer", "--json",
        )
        require(view.get("ok") is True and generated_view.is_file(), "当前案件短视图应在案件内生成")
        require(view.get("external_actions_executed") == 0, "重建案件视图不得执行外部动作")
        generated_text = generated_view.read_text(encoding="utf-8")
        require("可重建的短索引" in generated_text and "不记录完整聊天" in generated_text, "短视图必须声明派生性质和聊天边界")
        saved = load_json(state_path)
        view_artifact = next(item for item in saved.get("artifacts", []) if item.get("id") == view.get("artifact_id"))
        require(view_artifact.get("path", "").startswith("_case-state/memory/"), "案件视图产物路径必须位于本案记忆目录")
        require(saved.get("memory_state", {}).get("storage_scope") == "matter_workspace", "生成后存储范围不得漂移到全局")
        require(not saved.get("external_actions"), "记忆调用不得建立外部动作")
    return "CLI四种读取模式、上下文预算、文件限定、案件内存储、短视图重建及外部路径阻断均通过"


def check_cli_incremental_transactions(cli: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="legal-case-os-delta-test-") as temp:
        root = Path(temp)
        workspace, state_path = clone_incremental_workspace(root, "TEST-ONLY-delta-matter")
        initial = load_json(state_path)
        initial_counts = {
            key: len(initial.get(key, []))
            for key in ("sources", "evidence", "deadline_records", "impact_assessments", "prospective_matter_seeds")
        }
        initial_issue_modes = {item["id"]: item.get("analysis_mode") for item in initial.get("issues", [])}

        incoming = workspace / "incoming" / "ordinary"
        incoming.mkdir(parents=True)
        shutil.copy2(
            FIXTURES_ROOT / "incremental-case" / "materials" / "TEST-ONLY-01-ordinary-email.md",
            incoming / "TEST-ONLY-new-ordinary-email.md",
        )
        _, first, _ = run_cli(
            cli, "ingest-batch", "--workspace", str(workspace), "--source-dir", str(incoming),
            "--batch-id", "MB-TEST-DYNAMIC-001", "--received-at", "2026-08-20T08:00:00Z",
            "--arrival-channel", "TEST-ONLY email", "--sender", "synthetic_client",
            "--actor", "synthetic_test_lawyer", "--json",
        )
        require(first.get("ok") is True and first.get("idempotent") is False, "首次增量批次应真实入库")
        require(first.get("external_actions_executed") == 0, "批次入库不得执行外部动作")
        after_first = load_json(state_path)
        first_cursor = after_first["memory_state"]["last_committed_event_seq"]
        first_version = first.get("state_version")
        source_id = first.get("source_ids", [None])[0]
        require(len(after_first["sources"]) == initial_counts["sources"] + 1, "首次批次应只新增一个来源")

        state_hash_before_repeat = sha256(state_path)
        _, repeat, _ = run_cli(
            cli, "ingest-batch", "--workspace", str(workspace), "--source-dir", str(incoming),
            "--batch-id", "MB-TEST-DYNAMIC-001", "--received-at", "2026-08-20T08:00:00Z",
            "--actor", "synthetic_test_lawyer", "--json",
        )
        after_repeat = load_json(state_path)
        require(repeat.get("ok") is True and repeat.get("idempotent") is True, "重复同一批次应成为幂等no-op")
        require(sha256(state_path) == state_hash_before_repeat, "幂等重复不得改写案件状态文件")
        require(after_repeat["memory_state"]["last_committed_event_seq"] == first_cursor, "幂等重复不得二次推进游标")
        require(len(after_repeat["sources"]) == initial_counts["sources"] + 1, "幂等重复不得复制来源")

        conflict_dir = workspace / "incoming" / "conflict"
        conflict_dir.mkdir(parents=True)
        shutil.copy2(
            FIXTURES_ROOT / "incremental-case" / "materials" / "TEST-ONLY-02-court-notice.md",
            conflict_dir / "TEST-ONLY-conflicting-payload.md",
        )
        state_hash_before_failure = sha256(state_path)
        cursor_before_failure = after_repeat["memory_state"]["last_committed_event_seq"]
        _, failed, _ = run_cli(
            cli, "ingest-batch", "--workspace", str(workspace), "--source-dir", str(conflict_dir),
            "--batch-id", "MB-TEST-DYNAMIC-001", "--actor", "synthetic_test_lawyer",
            "--json", allow_exit_2=True,
        )
        require(failed.get("code") == "BATCH_ID_CONFLICT", "同一batch-id绑定不同材料必须失败")
        after_failure = load_json(state_path)
        require(sha256(state_path) == state_hash_before_failure, "失败批次不得留下半提交状态")
        require(after_failure["memory_state"]["last_committed_event_seq"] == cursor_before_failure, "失败批次不得推进游标")

        triage_packet = write_json_payload(
            root / "TEST-ONLY-ordinary-triage.json",
            {
                "test_only": True,
                "triage_card": {
                    "id": "TC-TEST-DYNAMIC-001",
                    "batch_id": "MB-TEST-DYNAMIC-001",
                    "source_id": source_id,
                    "relevance": "none",
                    "action_route": "no_action",
                    "direct_link_ids": [],
                    "summary": "TEST-ONLY：普通收讫邮件，无当前动作",
                    "reason": "无新增事实、期限、证据或程序要求"
                }
            },
        )
        state_hash_before_candidate = sha256(state_path)
        cursor_before_candidate = load_json(state_path)["memory_state"]["last_committed_event_seq"]
        _, candidate_preview, _ = run_cli(
            cli, "triage-commit", "--state", str(state_path), "--card", str(triage_packet),
            "--write-mode", "candidate", "--expected-state-version", str(first_version),
            "--actor", "synthetic_test_agent", "--json",
        )
        require(candidate_preview.get("candidate_only") is True, "candidate模式必须明确只是候选delta")
        require(candidate_preview.get("canonical_state_mutations") == 0, "candidate模式不得提交规范状态")
        require(candidate_preview.get("state_version") == first_version, "candidate预览不得推进状态版本")
        require(sha256(state_path) == state_hash_before_candidate, "candidate预览不得改写case-state.json")
        require(load_json(state_path)["memory_state"]["last_committed_event_seq"] == cursor_before_candidate, "candidate预览不得推进语义游标")

        _, triaged, _ = run_cli(
            cli, "triage-commit", "--state", str(state_path), "--card", str(triage_packet),
            "--write-mode", "commit", "--expected-state-version", str(first_version),
            "--actor", "synthetic_test_lawyer", "--json",
        )
        require(triaged.get("ok") is True and triaged.get("write_mode") == "commit", "普通材料分诊应提交成功")
        after_triage = load_json(state_path)
        dynamic_batch = next(item for item in after_triage["material_batches"] if item["id"] == "MB-TEST-DYNAMIC-001")
        require(dynamic_batch.get("status") == "triaged", "全部来源有唯一已提交卡后批次应标为triaged")
        for key in ("evidence", "deadline_records", "impact_assessments", "prospective_matter_seeds"):
            require(len(after_triage.get(key, [])) == initial_counts[key], f"普通无意义材料不得新增 {key}")
        require({item["id"]: item.get("analysis_mode") for item in after_triage.get("issues", [])} == initial_issue_modes, "普通材料不得触发或扩大深析")

        stale_base_version = first_version
        state_hash_before_stale = sha256(state_path)
        _, stale, _ = run_cli(
            cli, "triage-commit", "--state", str(state_path), "--card", str(triage_packet),
            "--write-mode", "commit", "--expected-state-version", str(stale_base_version),
            "--actor", "synthetic_test_lawyer", "--json", allow_exit_2=True,
        )
        require(stale.get("code") == "STALE_STATE_VERSION", "第二对话基于过期state version提交必须冲突")
        require(sha256(state_path) == state_hash_before_stale, "过期对话不得覆盖较新的规范状态")
        require(not after_triage.get("external_actions"), "增量入库和分诊不得建立外部动作")
    return "CLI批次幂等、失败不推进游标、普通材料不深析/不造对象、共享版本冲突及G5零动作均通过"


def check_cli_lifecycle_runtime(cli: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="legal-case-os-lifecycle-test-") as temp:
        root = Path(temp)
        workspace, state_path = clone_incremental_workspace(root, "TEST-ONLY-lifecycle-matter")

        assessment = write_json_payload(root / "TEST-ONLY-impact-id.json", {"id": "IA-TEST-001"})
        _, applied, _ = run_cli(
            cli, "impact-apply", "--state", str(state_path), "--assessment", str(assessment),
            "--decision", "approved", "--expected-state-version", "1",
            "--actor", "synthetic_test_lawyer", "--json",
        )
        require(applied.get("ok") is True and applied.get("status") == "applied", "局部影响卡应由律师批准后应用")
        after_impact = load_json(state_path)
        facts = by_id(after_impact.get("facts", []), "id")
        issues = by_id(after_impact.get("issues", []), "id")
        require(facts["F-TEST-DELIVERY-001"].get("stale") is True, "声明的交付事实应失效")
        require(issues["I-TEST-PERFORMANCE-001"].get("status") == "stale", "声明的履行争点应失效")
        require(facts["F-TEST-IDENTITY-001"].get("status") == "confirmed", "未声明的主体事实不得误伤")
        require(issues["I-TEST-JURISDICTION-001"].get("status") == "analyzed", "未声明的管辖争点不得误伤")
        require(applied.get("external_actions_executed") == 0, "影响应用不得执行外部动作")

        first_commit_hash = sha256(state_path)
        _, stale_first_window, _ = run_cli(
            cli, "seed-promote", "--state", str(state_path), "--seed-id", "SEED-TEST-001",
            "--decision", "rejected", "--expected-state-version", "1",
            "--actor", "synthetic_stale_window", "--json", allow_exit_2=True,
        )
        require(applied.get("state_version") == 2, "初始版本1的首次语义写入必须推进到版本2")
        require(stale_first_window.get("code") == "STALE_STATE_VERSION", "首次写入后，仍持版本1的第二窗口必须被拒绝")
        require(sha256(state_path) == first_commit_hash, "初始并发冲突不得改写状态")

        impact_version = applied.get("state_version")
        _, started_workflow, _ = run_cli(
            cli, "workflow-start", "--state", str(state_path), "--workflow-id", "WF-TEST-PROCEDURAL-002",
            "--kind", "withdrawal", "--title", "TEST-ONLY 第二个虚构撤诉工单",
            "--input-event-id", "CE-TEST-008", "--related-object-id", "R-TEST-PROCEDURAL-CAPSULE-001",
            "--expected-state-version", str(impact_version), "--actor", "synthetic_test_lawyer", "--json",
        )
        require(started_workflow.get("ok") is True and started_workflow.get("status") == "in_progress", "程序工单应独立于聊天启动")
        after_workflow = load_json(state_path)
        workflow = by_id(after_workflow.get("workflow_instances", []), "id")["WF-TEST-PROCEDURAL-002"]
        require(workflow.get("related_object_ids") == ["R-TEST-PROCEDURAL-CAPSULE-001"], "纯程序工单只能绑定ProceduralCapsule")
        require(not any(str(item).startswith("E-") for item in workflow.get("related_object_ids", [])), "纯程序工单不得加载实体证据对象")

        capsule = by_id(after_workflow.get("artifacts", []), "id")["R-TEST-PROCEDURAL-CAPSULE-001"]
        snapshot = write_json_payload(
            root / "TEST-ONLY-run-input.json",
            {"input_snapshot": [{"object_id": capsule["id"], "version": capsule["version"], "hash": capsule["sha256"]}]},
        )
        workflow_version = started_workflow.get("state_version")
        _, run_started, _ = run_cli(
            cli, "run-start", "--state", str(state_path), "--workflow-id", "WF-TEST-PROCEDURAL-002",
            "--operation", "TEST-ONLY selective procedural capsule retry", "--input-snapshot", str(snapshot),
            "--run-id", "RUN-TEST-DYNAMIC-FAIL-001", "--expected-state-version", str(workflow_version),
            "--actor", "synthetic_test_agent", "--json",
        )
        require(run_started.get("ok") is True and run_started.get("status") == "running", "RunAttempt应作为独立可重试运行启动")
        run_version = run_started.get("state_version")
        _, run_failed, _ = run_cli(
            cli, "run-finish", "--state", str(state_path), "--run-id", "RUN-TEST-DYNAMIC-FAIL-001",
            "--status", "failed", "--reason", "TEST_ONLY_SIMULATED_RENDER_FAILURE",
            "--expected-state-version", str(run_version), "--actor", "synthetic_test_agent", "--json",
        )
        require(run_failed.get("ok") is True and run_failed.get("status") == "failed", "模拟运行应以failed终止")
        require(run_failed.get("workflow_completed") is False and run_failed.get("commit_status") == "discarded", "失败RunAttempt不得完成工单或提交输出")
        final_state = load_json(state_path)
        final_workflow = by_id(final_state.get("workflow_instances", []), "id")["WF-TEST-PROCEDURAL-002"]
        final_run = by_id(final_state.get("run_attempts", []), "id")["RUN-TEST-DYNAMIC-FAIL-001"]
        require(final_workflow.get("status") == "in_progress" and final_workflow.get("completed_at") is None, "运行失败后Workflow必须保持未完成")
        require(final_run.get("status") == "failed" and final_run.get("output_ids") == [], "失败运行不得产生法院候选输出")
        require(not final_state.get("external_actions"), "生命周期和运行管理不得建立G5外部动作")

        before_reconcile = sha256(state_path)
        _, reconciled, _ = run_cli(cli, "reconcile-matter", "--state", str(state_path), "--json")
        require(reconciled.get("automatic_external_actions") == 0, "恢复核对不得触发外部动作")
        require(
            reconciled.get("state_version") == final_state.get("focus", {}).get("state_version"),
            "恢复核对必须返回当前规范投影版本，不得把audit序号当作state_version",
        )
        require(sha256(state_path) == before_reconcile, "reconcile-matter 必须只读")
    return "CLI局部失效、ProceduralCapsule工单、失败RunAttempt不完成Workflow、只读恢复及G5零动作均通过"


def check_cli_validate_states(cli: Path) -> str:
    paths = [
        FIXTURES_ROOT / "simple-case" / "_case-state" / "case-state.json",
        FIXTURES_ROOT / "complex-case" / "_case-state" / "case-state.json",
        FIXTURES_ROOT / "incremental-case" / "_case-state" / "case-state.json",
    ]
    for path in paths:
        _, payload, _ = run_cli(cli, "validate", "--state", str(path), "--json")
        require(payload.get("ok") is True, f"状态校验失败：{path.name} {payload}")
    with tempfile.TemporaryDirectory(prefix="legal-case-os-semantics-test-") as temp:
        root = Path(temp)
        inconsistent = load_json(paths[0])
        evidence = next(item for item in inconsistent["evidence"] if item["id"] == "E-TEST-SIMPLE-003")
        evidence["current_submission"] = True
        inconsistent_path = write_temp_state(root, inconsistent)
        _, rejected_evidence, _ = run_cli(cli, "validate", "--state", str(inconsistent_path), "--json", allow_exit_2=True)
        require(rejected_evidence.get("ok") is False, "与律师决定冲突的证据视图必须被拒绝")
        require("与律师决定 reserve" in "\n".join(rejected_evidence.get("errors", [])), "证据阻断原因必须指出跨字段不一致")

        forged = load_json(paths[1])
        authority = next(item for item in forged["authorities"] if item["id"] == "A-TEST-SUPPORT-001")
        authority["verification_status"] = "verified"
        authority["production_eligible"] = True
        forged_dir = root / "forged-authority"
        forged_dir.mkdir()
        forged_path = write_temp_state(forged_dir, forged)
        _, rejected_authority, _ = run_cli(cli, "validate", "--state", str(forged_path), "--json", allow_exit_2=True)
        require(rejected_authority.get("ok") is False, "TEST-ONLY 法源即使伪装 verified 也不得 production_eligible")
        require("TEST-ONLY/非生产环境" in "\n".join(rejected_authority.get("errors", [])), "法源阻断原因必须明确测试/环境边界")
    return "简单案、复杂案和增量生命周期案有效；证据跨字段冲突及伪装verified的TEST-ONLY法源均被根CLI拒绝"


def write_temp_state(directory: Path, state: dict[str, Any]) -> Path:
    path = directory / "case-state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if state.get("matter", {}).get("id") == "M-TEST-SIMPLE-001" and state.get("audit", {}).get("last_sequence", 0) > 0:
        source_audit = FIXTURES_ROOT / "simple-case" / "_case-state" / "audit-log.jsonl"
        shutil.copy2(source_audit, directory / "audit-log.jsonl")
    return path


def check_cli_approval_ok(cli: Path) -> str:
    base = load_json(FIXTURES_ROOT / "complex-case" / "_case-state" / "case-state.json")
    outcomes: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="legal-case-os-ok-test-") as temp:
        root = Path(temp)

        none_state = copy.deepcopy(base)
        none_state["focus"]["recent_candidates"] = []
        none_state["focus"]["pending_approval_ids"] = []
        none_state["approvals"] = []
        none_dir = root / "none"
        none_dir.mkdir()
        none_path = write_temp_state(none_dir, none_state)
        before = sha256(none_path)
        _, outcomes["none"], _ = run_cli(cli, "approve-ok", "--state", str(none_path), "--json", allow_exit_2=True)
        require(outcomes["none"].get("approved") is False, "无候选时不得批准")
        require(sha256(none_path) == before, "无候选阻断不应修改状态")

        unique_dir = root / "unique"
        unique_dir.mkdir()
        unique_path = write_temp_state(unique_dir, copy.deepcopy(base))
        _, outcomes["unique"], _ = run_cli(
            cli, "approve-ok", "--state", str(unique_path), "--actor", "synthetic_test_lawyer",
            "--reason", "TEST-ONLY exact approval", "--json", allow_exit_2=True,
        )
        require(outcomes["unique"].get("ok") is True and outcomes["unique"].get("approved") is True, "唯一当前候选应获批准")
        require(outcomes["unique"].get("object_id") == "D-TEST-COMPLEX-G1-001", "批准对象错误")

        multiple_state = copy.deepcopy(base)
        multiple_state["decisions"].append({
            "id": "D-TEST-COMPLEX-G1-002", "subject_id": "I-QUALITY-001", "version": 1,
            "content_hash": "5555555555555555555555555555555555555555555555555555555555555555",
            "status": "candidate", "reason": "TEST-ONLY second pending decision",
        })
        multiple_state["approvals"].append({
            "id": "P-TEST-COMPLEX-G1-002", "gate": "G1_strategy", "object_id": "D-TEST-COMPLEX-G1-002",
            "object_version": 1, "object_hash": "5555555555555555555555555555555555555555555555555555555555555555",
            "stage": "analysis", "decision": "pending", "reason": None, "actor": None,
            "decided_at": None, "status": "pending", "invalidation_reason": None, "scope_snapshot": [],
        })
        multiple_state["focus"]["recent_candidates"].append({
            "object_id": "D-TEST-COMPLEX-G1-002", "object_type": "decision", "version": 1,
            "hash": "5555555555555555555555555555555555555555555555555555555555555555",
            "pending_approval_id": "P-TEST-COMPLEX-G1-002", "displayed_in_turn": "TEST-TURN-COMPLEX-001",
            "displayed_at": "2026-08-23T01:29:30Z", "just_displayed": True, "stale": False,
        })
        multiple_state["focus"]["pending_approval_ids"].append("P-TEST-COMPLEX-G1-002")
        multiple_dir = root / "multiple"
        multiple_dir.mkdir()
        multiple_path = write_temp_state(multiple_dir, multiple_state)
        before = sha256(multiple_path)
        _, outcomes["multiple"], _ = run_cli(cli, "approve-ok", "--state", str(multiple_path), "--json", allow_exit_2=True)
        require(outcomes["multiple"].get("approved") is False, "多个候选时不得批准")
        require(sha256(multiple_path) == before, "多个候选阻断不应修改状态")

        stale_state = copy.deepcopy(base)
        stale_state["decisions"][0]["reason"] += "（TEST-ONLY：对象正文已改变）"
        stale_dir = root / "stale"
        stale_dir.mkdir()
        stale_path = write_temp_state(stale_dir, stale_state)
        before = sha256(stale_path)
        _, outcomes["stale"], _ = run_cli(cli, "approve-ok", "--state", str(stale_path), "--json", allow_exit_2=True)
        require(outcomes["stale"].get("approved") is False, "对象哈希变化后不得批准旧候选")
        require(sha256(stale_path) == before, "过期候选阻断不应修改状态")

    return "根 CLI 通过 ok 的无候选、唯一、多个、过期四情形，阻断时状态不变"


def check_cli_template_gates(cli: Path) -> str:
    registry_path = PROJECT_ROOT / "shared" / "templates" / "template-registry.json"
    registry = load_json(registry_path)
    source_entry = next(item for item in registry["templates"] if item["id"] == "TPL-CIVIL-COMPLAINT")
    data = {
        "plaintiff": "TEST-ONLY 测试原告（虚构）",
        "defendant": "TEST-ONLY 测试被告（虚构）",
        "cause_of_action": "TEST-ONLY 虚构买卖合同争议",
        "claims": "TEST-ONLY 虚构请求",
        "facts_and_reasons": "TEST-ONLY 虚构事实与理由",
        "evidence_summary": "TEST-ONLY 仅列入模拟 G2 批准证据",
        "court_name": "TEST-ONLY 测试法院（虚构）",
        "signature": "TEST-ONLY 不签名",
        "filing_date": "2026-08-23"
    }
    blocked = 0

    reference_catalog = (
        FIXTURES_ROOT / "template-reference-cases" / "_registry" / "TEST-ONLY-personal-template-catalog.json"
    )
    _, reference_validation, _ = run_cli(
        cli, "template-ref-validate", "--catalog", str(reference_catalog), "--json",
    )
    require(
        reference_validation.get("ok") is True
        and reference_validation.get("template_count") == 1
        and reference_validation.get("suite_count") == 1,
        "TEST-ONLY 个人模板目录应通过一模板一套件校验",
    )
    _, reference, _ = run_cli(
        cli, "template-ref-resolve", "--catalog", str(reference_catalog),
        "--template-id", "简模001号", "--json",
    )
    require(
        reference.get("id") == "TPL-P-S-001"
        and reference.get("load_policy") == "exact_named_reference_only"
        and reference.get("external_actions_executed") == 0,
        "编号变体必须唯一解析个人模板且不执行外部动作",
    )
    _, suite_reference, _ = run_cli(
        cli, "template-ref-resolve", "--catalog", str(reference_catalog),
        "--template-id", "简模001套件", "--json",
    )
    require(
        suite_reference.get("reference_kind") == "suite"
        and {item.get("role") for item in suite_reference.get("components", [])}
        == {"civil_complaint", "power_of_attorney", "law_firm_letter", "service_information"},
        "个人套件必须精确展开已登记诉状和手续组件",
    )
    with tempfile.TemporaryDirectory(prefix="legal-case-os-template-test-") as temp:
        root = Path(temp)
        success_output = root / "valid.md"
        _, payload, _ = run_cli(
            cli, "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
            "--data", json.dumps(data, ensure_ascii=False), "--output", str(success_output), "--json",
        )
        require(payload.get("ok") is True and success_output.is_file(), "有效模板和完整字段应生成测试文稿")
        require("TEST-ONLY" in success_output.read_text(encoding="utf-8"), "测试输出必须保留 TEST-ONLY 数据")

        missing_output = root / "missing.md"
        _, payload, _ = run_cli(
            cli, "template-fill", "--template-id", "TPL-NOT-EXISTS", "--data", "{}",
            "--output", str(missing_output), "--json", allow_exit_2=True,
        )
        require(payload.get("ok") is False and not missing_output.exists(), "缺失模板必须阻断且不产生文件")
        blocked += 1

        incomplete_output = root / "incomplete.md"
        _, payload, _ = run_cli(
            cli, "template-fill", "--template-id", "TPL-CIVIL-COMPLAINT",
            "--data", json.dumps({"plaintiff": "TEST-ONLY"}, ensure_ascii=False),
            "--output", str(incomplete_output), "--json", allow_exit_2=True,
        )
        require(payload.get("ok") is False and not incomplete_output.exists(), "必填字段缺失必须阻断且不产生文件")
        blocked += 1

        for label, mutation in (
            ("license", {"license": {"id": "UNKNOWN", "status": "unknown", "notice": "TEST-ONLY unknown"}}),
            ("stale", {"status": "expired"}),
        ):
            entry = copy.deepcopy(source_entry)
            entry["path"] = str((registry_path.parent / source_entry["path"]).resolve())
            entry.update(mutation)
            test_registry = {"registry_version": "1.0.0", "notice": "TEST-ONLY", "templates": [entry]}
            custom_registry = root / f"{label}-registry.json"
            custom_registry.write_text(json.dumps(test_registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output = root / f"{label}.md"
            _, payload, _ = run_cli(
                cli, "template-fill", "--template-id", entry["id"],
                "--data", json.dumps(data, ensure_ascii=False), "--output", str(output),
                "--registry", str(custom_registry), "--json", allow_exit_2=True,
            )
            require(payload.get("ok") is False and not output.exists(), f"{label} 模板必须阻断且不产生文件")
            blocked += 1
    require(blocked == 4, "应阻断模板缺失、字段缺失、许可不明、过期四种情形")
    return "根 CLI 允许有效可填充模板和精确个人参照/套件；阻断缺失、字段不全、许可不明和过期模板"


def check_cli_preflight_and_sanitize(cli: Path) -> str:
    fixture_dir = FIXTURES_ROOT / "document-cleaning"
    dirty = fixture_dir / "TEST-ONLY-cleaning-source.docx"
    cleanable = fixture_dir / "TEST-ONLY-cleanable-source.docx"
    approval = fixture_dir / "TEST-ONLY-sanitization-approval.json"
    require(dirty.is_file() and cleanable.is_file() and approval.is_file(), "缺少 TEST-ONLY 清洁文书夹具")
    dirty_hash = sha256(dirty)
    cleanable_hash = sha256(cleanable)

    _, dirty_preflight, _ = run_cli(cli, "preflight", "--path", str(dirty), "--json", allow_exit_2=True)
    dirty_kinds = {item.get("kind") for item in dirty_preflight.get("findings", [])}
    require({"hidden_text", "unresolved_placeholder"} <= dirty_kinds, "脏稿必须识别隐藏文字和未决占位符")
    _, source_preflight, _ = run_cli(cli, "preflight", "--path", str(cleanable), "--json", allow_exit_2=True)
    source_kinds = {item.get("kind") for item in source_preflight.get("findings", [])}
    require({"comments", "tracked_changes", "internal_id", "internal_note", "watermark_candidate"} <= source_kinds, "可清洁稿预检缺少预期发现")

    with tempfile.TemporaryDirectory(prefix="legal-case-os-sanitize-test-") as temp:
        root = Path(temp)
        output = root / "TEST-ONLY-derived.docx"
        report_path = root / "TEST-ONLY-sanitization-report.json"
        _, result, _ = run_cli(
            cli, "sanitize-docx", "--source", str(cleanable), "--approved-items", str(approval),
            "--output", str(output), "--report", str(report_path), "--json",
        )
        require(result.get("ok") is True and output.is_file() and report_path.is_file(), "获批清洁应生成派生件和报告")
        require(result.get("original_hash_unchanged") is True and sha256(cleanable) == cleanable_hash, "清洁不得修改源 DOCX")
        require(result.get("rereview_status") == "required", "清洁工具结束后必须保持待复审")
        _, after, _ = run_cli(cli, "preflight", "--path", str(output), "--json", allow_exit_2=True)
        after_kinds = {item.get("kind") for item in after.get("findings", [])}
        forbidden = {"comments", "tracked_changes", "internal_id", "internal_note", "watermark_candidate"}
        require(not (after_kinds & forbidden), f"派生件仍有获批残留：{sorted(after_kinds & forbidden)}")
        # This sanitizer fixture intentionally keeps its visible TEST-ONLY
        # disclaimer ("不得提交") after cleaning.  The production preflight is
        # correct to flag that phrase; this check is about whether the approved
        # comments/revisions/internal markers were removed, not whether a
        # synthetic fixture can become a court candidate.
        remaining_blockers = [
            item for item in after.get("blocking_findings", [])
            if not (
                item.get("kind") == "court_prose_marker"
                and item.get("marker") == "nonfiling_marker"
                and output.name.startswith("TEST-ONLY-")
            )
        ]
        require(not remaining_blockers, "派生件预检仍有非测试声明类阻断发现")

        blocked_output = root / "TEST-ONLY-dirty-blocked.docx"
        _, blocked, _ = run_cli(
            cli, "sanitize-docx", "--source", str(dirty), "--approved-items", str(approval),
            "--output", str(blocked_output), "--json", allow_exit_2=True,
        )
        require(blocked.get("ok") is False and not blocked_output.exists(), "含隐藏文字的稿件必须阻断且不产生派生件")

        mixed_approval = load_json(approval)
        watermark_target = next(item for item in mixed_approval["approved_targets"] if item["kind"] == "draft_watermark")
        watermark_target["ownership"] = "third_party"
        watermark_target["authority_basis"] = "TEST-ONLY：故意伪造为第三方真实性标记"
        mixed_approval["approval_set_sha256"] = hashlib.sha256(
            json.dumps(mixed_approval["approved_targets"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        mixed_approval_path = root / "TEST-ONLY-mixed-ownership-approval.json"
        mixed_approval_path.write_text(json.dumps(mixed_approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mixed_output = root / "TEST-ONLY-mixed-ownership-blocked.docx"
        _, mixed_blocked, _ = run_cli(
            cli, "sanitize-docx", "--source", str(cleanable), "--approved-items", str(mixed_approval_path),
            "--output", str(mixed_output), "--json", allow_exit_2=True,
        )
        require(mixed_blocked.get("code") == "SANITIZATION_TARGET_NOT_AUTHORIZED" and not mixed_output.exists(), "逐项目标标为第三方时，顶层self_generated不得绕过权属门禁")

        # Stateful cleaning must enforce the same append-only approval receipt
        # boundary as validation and packaging.  An otherwise exact SANITIZE
        # scope with audit cursor 0 is deliberately forged here and must fail
        # before any derivative is written.
        stateful_approval = load_json(approval)
        stateful_approval["approval_artifact_id"] = "R-TEST-SANITIZE-APPROVAL-001"
        stateful_approval_path = root / "TEST-ONLY-stateful-approval.json"
        stateful_approval_path.write_text(
            json.dumps(stateful_approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        approval_digest = sha256(stateful_approval_path)
        review_digest = "a" * 64
        forged_state = load_json(FIXTURES_ROOT / "complex-case" / "_case-state" / "case-state.json")
        forged_state["artifacts"].extend([
            {
                "id": stateful_approval["source_artifact_id"], "kind": "synthetic_docx",
                "audience": "internal_review", "version": 1, "path": str(cleanable),
                "sha256": cleanable_hash, "status": "reviewed", "review_status": "passed",
                "input_snapshot": [], "stale": False, "stale_reason": None,
                "created_at": "2026-08-23T07:58:00Z",
            },
            {
                "id": stateful_approval["independent_review_id"], "kind": "independent_review",
                "audience": "internal_review", "version": 1, "path": "TEST-ONLY-review.md",
                "sha256": review_digest, "status": "reviewed", "review_status": "passed",
                "input_snapshot": [], "stale": False, "stale_reason": None,
                "created_at": "2026-08-23T07:59:00Z",
            },
            {
                "id": stateful_approval["approval_artifact_id"], "kind": "sanitization_approval",
                "audience": "internal_review", "version": 1, "path": str(stateful_approval_path),
                "sha256": approval_digest, "status": "approved", "review_status": "passed",
                "input_snapshot": [], "stale": False, "stale_reason": None,
                "created_at": "2026-08-23T08:00:00Z",
            },
        ])
        forged_scope = [
            {"object_id": stateful_approval["source_artifact_id"], "version": 1, "hash": cleanable_hash},
            {"object_id": stateful_approval["approval_artifact_id"], "version": 1, "hash": approval_digest},
            {"object_id": stateful_approval["independent_review_id"], "version": 1, "hash": review_digest},
        ]
        forged_state["approvals"].append({
            "id": stateful_approval["approval_id"], "gate": "SANITIZE",
            "object_id": stateful_approval["source_artifact_id"], "object_version": 1,
            "object_hash": cleanable_hash, "stage": forged_state["matter"]["stage"],
            "decision": "approved", "reason": "TEST-ONLY forged active approval without receipt",
            "actor": "TEST-ONLY synthetic lawyer", "decided_at": "2026-08-23T08:00:00Z",
            "status": "active", "invalidation_reason": None, "scope_snapshot": forged_scope,
            "scope_hash": hashlib.sha256(
                json.dumps(forged_scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        })
        forged_state_path = root / "_case-state" / "case-state.json"
        forged_state_path.parent.mkdir(parents=True)
        forged_state_path.write_text(
            json.dumps(forged_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        forged_output = root / "TEST-ONLY-forged-state-cleaned.docx"
        _, forged_blocked, _ = run_cli(
            cli, "sanitize-docx", "--state", str(forged_state_path),
            "--source", str(cleanable), "--approved-items", str(stateful_approval_path),
            "--output", str(forged_output), "--json", allow_exit_2=True,
        )
        require(
            forged_blocked.get("code") == "CASE_STATE_INVALID"
            and "approval_granted" in forged_blocked.get("message", "")
            and not forged_output.exists(),
            "有状态清洁必须核验追加式 approval_granted 收据并在写出派生件前阻断",
        )

    require(sha256(dirty) == dirty_hash and sha256(cleanable) == cleanable_hash, "清洁测试前后两个源夹具哈希必须不变")
    return "预检识别阻断项；逐项目标权属受控；无追加式批准收据的有状态清洁被阻断；获批TEST-ONLY自有稿生成派生件并待复审；原件不变"


def check_cli_minimal_diff(cli: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="legal-case-os-diff-test-") as temp:
        root = Path(temp)
        original = root / "TEST-ONLY-original.md"
        allowed = root / "TEST-ONLY-allowed.md"
        outside = root / "TEST-ONLY-outside.md"
        original.write_text("标题\n第一项请求\n第二项请求：旧内容\n事实与理由不变\n落款\n", encoding="utf-8")
        allowed.write_text("标题\n第一项请求\n第二项请求：获批新内容\n事实与理由不变\n落款\n", encoding="utf-8")
        outside.write_text("标题\n第一项请求\n第二项请求：获批新内容\n事实与理由被顺手重写\n落款\n", encoding="utf-8")
        _, good, _ = run_cli(
            cli, "min-diff", "--original", str(original), "--modified", str(allowed),
            "--allowed-line", "3:3", "--max-change-ratio", "0.5", "--json",
        )
        require(good.get("ok") is True and good.get("outside_allowed_lines") == [], "授权行内修改应通过")
        _, bad, _ = run_cli(
            cli, "min-diff", "--original", str(original), "--modified", str(outside),
            "--allowed-line", "3:3", "--max-change-ratio", "0.8", "--json", allow_exit_2=True,
        )
        require(bad.get("ok") is False and 4 in bad.get("outside_allowed_lines", []), "范围外第4行改动必须阻断")
    return "只改第3行通过；顺手改第4行被最小修改门禁阻断"


def build_pdf_package_state(pdf_path: Path) -> dict[str, Any]:
    state = load_json(FIXTURES_ROOT / "complex-case" / "_case-state" / "case-state.json")
    digest = sha256(pdf_path)
    state["focus"]["recent_candidates"] = []
    state["focus"]["pending_approval_ids"] = []
    state["focus"]["current_artifact_id"] = "R-TEST-PDF-001"
    state["approvals"] = []
    state["artifacts"] = [{
        "id": "R-TEST-PDF-001", "kind": "synthetic_rendered_pdf", "audience": "court_candidate",
        "version": 1, "path": str(pdf_path.resolve()), "sha256": digest, "status": "reviewed",
        "review_status": "passed", "input_snapshot": [], "stale": False, "stale_reason": None,
        "created_at": "2026-08-23T02:00:00Z",
    }]
    item = {
        "object_id": "R-TEST-PDF-001", "object_type": "artifact", "version": 1,
        "hash": digest, "path": str(pdf_path.resolve()), "order": 1, "bookmark": "TEST-ONLY 文书",
    }
    manifest_hash = hashlib.sha256(
        json.dumps([item], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    state["package_manifests"] = [{
        "id": "B-TEST-PDF-001", "version": 1, "manifest_hash": manifest_hash, "items": [item],
        "status": "candidate", "stale": False, "blockers": [], "created_at": "2026-08-23T02:01:00Z",
    }]
    state["status"] = {"workflow": "candidate_ready", "current_gate": "G4_final", "blockers": [], "degradations": []}
    state["last_updated"] = "2026-08-23T02:01:00Z"
    return state


def check_cli_bundle_and_print_sheet(cli: Path) -> str:
    pdf_path = TESTS_ROOT / "artifacts" / "render-cleaning-source" / "render.pdf"
    require(pdf_path.is_file(), "缺少 TEST-ONLY 渲染 PDF")
    pdf_hash = sha256(pdf_path)
    with tempfile.TemporaryDirectory(prefix="legal-case-os-package-test-") as temp:
        workspace = Path(temp) / "workspace"
        state_dir = workspace / "_case-state"
        state_dir.mkdir(parents=True)
        state_path = write_temp_state(state_dir, build_pdf_package_state(pdf_path))
        _, validation, _ = run_cli(cli, "validate", "--state", str(state_path), "--json")
        require(validation.get("ok") is True, "合成 PDF 候选包状态必须通过校验")

        # Converting a sanitized DOCX to PDF must not erase the mandatory
        # independent re-review.  Package verification follows input_snapshot
        # recursively and blocks while the underlying report is still required.
        lineage_state = build_pdf_package_state(pdf_path)
        lineage_state["artifacts"][0]["input_snapshot"] = [{
            "object_id": "R-TEST-SANITIZED-ANCESTOR",
            "version": 1,
            "hash": pdf_hash,
        }]
        lineage_state["artifacts"].append({
            "id": "R-TEST-SANITIZED-ANCESTOR", "kind": "sanitized_docx", "audience": "court_candidate",
            "version": 1, "path": str(pdf_path.resolve()), "sha256": pdf_hash, "status": "reviewed",
            "review_status": "passed", "input_snapshot": [], "stale": False, "stale_reason": None,
            "created_at": "2026-08-23T01:59:00Z",
        })
        lineage_state["sanitization_reports"] = [{
            "id": "SR-TEST-LINEAGE-001", "source_artifact_id": "R-TEST-SOURCE-ANCESTOR",
            "source_sha256": pdf_hash, "derived_artifact_id": "R-TEST-SANITIZED-ANCESTOR",
            "derived_sha256": pdf_hash, "approved_item_ids": [], "items": [], "blocked_items": [],
            "rereview_status": "required", "created_at": "2026-08-23T01:59:30Z",
        }]
        lineage_path = write_temp_state(state_dir, lineage_state)
        blocked_sheet = workspace / "60-filing" / "TEST-ONLY-lineage-blocked.md"
        _, lineage_blocked, _ = run_cli(
            cli, "print-sheet", "--state", str(lineage_path), "--package-id", "B-TEST-PDF-001",
            "--output", str(blocked_sheet), "--json", allow_exit_2=True,
        )
        require(
            lineage_blocked.get("code") == "SANITIZATION_REREVIEW_REQUIRED" and not blocked_sheet.exists(),
            "sanitize→PDF 的递归来源在复审required时必须阻断组卷/打印生产单",
        )

        # Restore the baseline state for the positive print-sheet/bundle path.
        state_path = write_temp_state(state_dir, build_pdf_package_state(pdf_path))

        sheet = workspace / "60-filing" / "TEST-ONLY-print-sheet.md"
        _, print_result, _ = run_cli(
            cli, "print-sheet", "--state", str(state_path), "--package-id", "B-TEST-PDF-001",
            "--output", str(sheet), "--json",
        )
        require(print_result.get("ok") is True and sheet.is_file(), "应生成候选打印生产单")
        require(print_result.get("external_actions_executed") == 0, "打印单命令不得实际打印")
        sheet_text = sheet.read_text(encoding="utf-8")
        require("候选" in sheet_text and "不会触发打印" in sheet_text and "不执行任何外部动作" in sheet_text, "打印单必须明确仅为候选且不触发外部动作")

        bundle_output = workspace / "60-filing" / "TEST-ONLY-bundle.pdf"
        _, bundle_result, _ = run_cli(
            cli, "bundle", "--state", str(state_path), "--package-id", "B-TEST-PDF-001",
            "--output", str(bundle_output), "--page-numbers", "--json", allow_exit_2=True,
        )
        if bundle_result.get("ok") is True:
            require(bundle_output.is_file(), "组卷成功时必须产生真实 PDF")
            require(bundle_output.read_bytes().startswith(b"%PDF-"), "组卷结果必须具有真实PDF签名")
            require(bundle_result.get("page_count", 0) >= 1, "组卷结果必须复核正页数")
            require(bundle_result.get("bookmarks_added") == 1, "组卷必须保留清单指定书签")
            require(bundle_result.get("page_numbers_added") is True and bundle_result.get("page_number_text_verified") is True, "--page-numbers必须叠加并文本复核每页页码")
            require(bundle_result.get("external_actions_executed") == 0, "组卷不得触发外部动作")
            _, bundle_preflight, _ = run_cli(
                cli, "preflight", "--path", str(bundle_output), "--json", allow_exit_2=True,
            )
            require(
                not bundle_preflight.get("blocking_findings"),
                f"页码叠加后的候选包不得残留空批注容器或其他阻断项：{bundle_preflight.get('blocking_findings')}",
            )
        else:
            require(bundle_result.get("degraded") is True, "后端不可用时必须明确 degraded")
            require(bundle_result.get("code") in {"PDF_BACKEND_UNAVAILABLE", "PAGE_NUMBER_BACKEND_UNAVAILABLE"}, "组卷降级代码不明确")
            require(not bundle_output.exists(), "组卷降级时不得生成伪 PDF")
    require(sha256(pdf_path) == pdf_hash, "组卷和打印单测试不得修改输入 PDF")
    return "sanitize→PDF递归来源在复审required时被阻断；打印生产单未实际打印；PDF组卷校验签名/页数/书签/逐页页码（缺后端则明确降级）；输入不变"


def check_cli_g2_exact_snapshot(cli: Path) -> str:
    base = load_json(FIXTURES_ROOT / "simple-case" / "_case-state" / "case-state.json")
    with tempfile.TemporaryDirectory(prefix="legal-case-os-g2-test-") as temp:
        workspace = Path(temp) / "workspace"
        state_dir = workspace / "_case-state"
        state_dir.mkdir(parents=True)

        valid_path = write_temp_state(state_dir, copy.deepcopy(base))
        _, valid, _ = run_cli(cli, "validate", "--state", str(valid_path), "--json")
        require(valid.get("ok") is True, "精确 G2 快照基线应有效")

        borrowed = copy.deepcopy(base)
        new_evidence = copy.deepcopy(next(item for item in borrowed["evidence"] if item["id"] == "E-TEST-SIMPLE-003"))
        new_evidence.update({
            "id": "E-TEST-SIMPLE-NEW-001", "lawyer_decision": "submit_now", "decision_reason": "TEST-ONLY：故意构造未被旧G2批准的新证据",
            "current_submission": True, "reserve": False, "status": "approved", "applicable_stage": "synthetic_first_instance_filing",
            "service_obligation_status": "verified_required",
        })
        borrowed["evidence"].append(new_evidence)
        borrowed_path = write_temp_state(state_dir, borrowed)
        before = sha256(borrowed_path)
        _, rejected, _ = run_cli(cli, "validate", "--state", str(borrowed_path), "--json", allow_exit_2=True)
        errors = "\n".join(rejected.get("errors", []))
        require(rejected.get("ok") is False, "新增证据借用旧 G2 必须失败")
        require("E-TEST-SIMPLE-NEW-001" in errors and ("精确绑定" in errors or "不一致" in errors), "阻断原因必须指出新证据未被精确绑定")
        require(sha256(borrowed_path) == before, "校验失败不得修改状态")

        changed = copy.deepcopy(base)
        changed_item = next(item for item in changed["evidence"] if item["id"] == "E-TEST-SIMPLE-001")
        changed_item["proposition"] = "TEST-ONLY：版本内容已变化但仍试图借用旧G2哈希"
        changed_path = write_temp_state(state_dir, changed)
        _, changed_rejected, _ = run_cli(cli, "validate", "--state", str(changed_path), "--json", allow_exit_2=True)
        changed_errors = "\n".join(changed_rejected.get("errors", []))
        require(changed_rejected.get("ok") is False and "E-TEST-SIMPLE-001" in changed_errors and "精确绑定" in changed_errors, "同ID内容变化也必须使旧G2失效")

        double_active = copy.deepcopy(base)
        duplicate_g2 = copy.deepcopy(next(item for item in double_active["approvals"] if item["gate"] == "G2_evidence"))
        duplicate_g2["id"] = "P-TEST-G2-DUPLICATE"
        double_active["approvals"].append(duplicate_g2)
        double_path = write_temp_state(state_dir, double_active)
        _, double_rejected, _ = run_cli(cli, "validate", "--state", str(double_path), "--json", allow_exit_2=True)
        double_errors = "\n".join(double_rejected.get("errors", []))
        require(double_rejected.get("ok") is False and "一个 active G2" in double_errors, "两个active G2不得通过拼接scope")

        changed_approval_object = copy.deepcopy(base)
        g2_decision = next(item for item in changed_approval_object["decisions"] if item["id"] == "D-TEST-G2-001")
        g2_decision["reason"] += "（TEST-ONLY：批准后正文变化）"
        changed_approval_path = write_temp_state(state_dir, changed_approval_object)
        _, approval_rejected, _ = run_cli(cli, "validate", "--state", str(changed_approval_path), "--json", allow_exit_2=True)
        approval_errors = "\n".join(approval_rejected.get("errors", []))
        require(approval_rejected.get("ok") is False and "P-TEST-G2-001" in approval_errors and "对象版本或哈希" in approval_errors, "active Approval对象正文变化必须失效")

        stitched = copy.deepcopy(base)
        stitched_evidence = next(item for item in stitched["evidence"] if item["id"] == "E-TEST-SIMPLE-003")
        stitched_evidence.update({
            "lawyer_decision": "submit_now", "decision_reason": "TEST-ONLY：故意拼入旧G2范围",
            "current_submission": True, "reserve": False, "internal_reference": False,
            "status": "approved", "service_obligation_status": "verified_required",
        })
        stitched_g2 = next(item for item in stitched["approvals"] if item["gate"] == "G2_evidence")
        stitched_g2["scope_snapshot"].extend([
            {"object_id": stitched_evidence["id"], "version": stitched_evidence["version"], "hash": canonical_object_hash(stitched_evidence)},
            {"object_id": "S-TEST-SIMPLE-003", "version": 1, "hash": next(item["sha256"] for item in stitched["sources"] if item["id"] == "S-TEST-SIMPLE-003")},
        ])
        stitched_path = write_temp_state(state_dir, stitched)
        _, stitched_rejected, _ = run_cli(cli, "validate", "--state", str(stitched_path), "--json", allow_exit_2=True)
        stitched_errors = "\n".join(stitched_rejected.get("errors", []))
        require(stitched_rejected.get("ok") is False and "scope_snapshot 与 scope_hash 不一致" in stitched_errors, "旧G2范围被拼接时必须由不可变范围哈希拒绝")

        disclosure = copy.deepcopy(base)
        disclosed_evidence = next(item for item in disclosure["evidence"] if item["id"] == "E-TEST-SIMPLE-001")
        disclosed_evidence.update({
            "disclosure_intent": "court_candidate",
            "intended_recipient": "仅法院；保证对方一定看不到",
            "service_obligation_status": "verified_not_required",
            "procedural_authority_id": None,
        })
        disclosure_g2 = next(item for item in disclosure["approvals"] if item["gate"] == "G2_evidence")
        disclosure_snapshot = next(item for item in disclosure_g2["scope_snapshot"] if item["object_id"] == disclosed_evidence["id"])
        disclosure_snapshot["hash"] = canonical_object_hash(disclosed_evidence)
        disclosure_g2["scope_hash"] = hashlib.sha256(
            json.dumps(disclosure_g2["scope_snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        disclosure_path = write_temp_state(state_dir, disclosure)
        _, disclosure_rejected, _ = run_cli(cli, "validate", "--state", str(disclosure_path), "--json", allow_exit_2=True)
        disclosure_errors = "\n".join(disclosure_rejected.get("errors", []))
        require(disclosure_rejected.get("ok") is False and "承诺对方不可见" in disclosure_errors and "缺少可复核 procedural_authority_id" in disclosure_errors, "披露意图不得写成对方不可见保证，且仅法院决定必须绑定程序法源")
    return "有效G2精确绑定证据与来源；新增/改版证据、双active G2、对象变化、旧范围拼接及披露保证均被阻断"


def check_cli_invalidation(cli: Path) -> str:
    source_state = FIXTURES_ROOT / "simple-case" / "_case-state" / "case-state.json"
    before_hashes = load_json(FIXTURES_ROOT / "baseline-hashes.json")["files"]
    with tempfile.TemporaryDirectory(prefix="legal-case-os-test-") as temp_dir:
        temp_state = Path(temp_dir) / "case-state.json"
        temp_state.write_bytes(source_state.read_bytes())
        shutil.copy2(source_state.parent / "audit-log.jsonl", Path(temp_dir) / "audit-log.jsonl")
        _, payload, _ = run_cli(
            cli, "invalidate", "--state", str(temp_state), "--source-id", "S-TEST-SIMPLE-002",
            "--new-hash", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "--reason", "TEST-ONLY source hash changed", "--json",
        )
        require(payload.get("ok") is True, f"失效命令失败：{payload}")
        rewritten = load_json(temp_state)
        stale_count = sum(
            1 for collection in ("facts", "evidence", "approvals", "artifacts", "package_manifests")
            for item in rewritten.get(collection, []) if item.get("status") in {"stale", "invalid", "invalidated"}
        )
        require(stale_count > 0 or payload.get("invalidated_count", 0) > 0, "哈希变化后没有依赖对象失效")
    for rel, expected in before_hashes.items():
        require(sha256(FIXTURES_ROOT / rel) == expected, f"CLI 失效测试修改了原件：{rel}")
    return "临时状态发生失效传播，8 份原件哈希保持不变"


def build_report(results: list[Result], mode: str) -> dict[str, Any]:
    counts = {status: sum(item.status == status for item in results) for status in ("PASS", "FAIL", "SKIP")}
    return {
        "test_only": True,
        "suite": "legal-case-os-v1.2-offline-synthetic",
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "ok": counts["FAIL"] == 0,
        "results": [asdict(item) for item in results],
        "limitations": [
            "全部案件和法源均为 TEST-ONLY 虚构数据，不能证明真实案件效果。",
            "二进制 DOCX/PDF 清洁和逐页渲染由根测试任务另行覆盖。",
            "未执行打印、发送、上传、送达或提交等 G5 外部动作。"
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 legal-case-os v1 离线 TEST-ONLY 验收")
    parser.add_argument("--static-only", action="store_true", help="只验证测试夹具，不调用根目录 CLI")
    parser.add_argument("--json-report", type=Path, help="将机器可读结果写入指定 JSON")
    args = parser.parse_args()

    suite = Acceptance()
    static_checks: list[tuple[str, Callable[[], str]]] = [
        ("STATIC-JSON-MARKERS", check_json_and_test_markers),
        ("STATIC-ORIGINAL-MARKERS", check_original_markers_and_no_personal_identifiers),
        ("STATIC-ORIGINAL-HASHES", check_original_hashes),
        ("STATIC-CASE-STATE-SOURCES", check_case_state_sources),
        ("STATIC-SIMPLE-EVIDENCE-GATE", check_simple_case_gate),
        ("STATIC-COMPLEX-SCOPE", check_complex_case_scope_and_adversity),
        ("STATIC-AUTHORITIES", check_authorities),
        ("STATIC-TEMPLATES", check_template_scenarios),
        ("STATIC-ROUTING", check_routing_contract),
        ("STATIC-LANGUAGE-CONTROL", check_language_control_contract),
        ("STATIC-INCREMENTAL-FIXTURE", check_incremental_fixture_storage),
        ("STATIC-INCREMENTAL-STATE", check_incremental_case_state),
        ("STATIC-MEMORY-MODES", check_memory_mode_contract),
        ("STATIC-INCREMENTAL-LIFECYCLE", check_incremental_lifecycle_contract),
        ("STATIC-APPROVAL-OK", check_approval_contract),
        ("STATIC-EVIDENCE-GATE", check_evidence_gate_contract),
        ("STATIC-STALENESS", check_staleness_contract),
        ("STATIC-DEGRADATION", check_degradation_contract),
        ("STATIC-SANITIZATION", check_sanitization_contract),
    ]
    for check_id, fn in static_checks:
        suite.check(check_id, fn)

    mode = "static-only" if args.static_only else "static-and-cli"
    if args.static_only:
        suite.skip("CLI-HELP", "--static-only")
        suite.skip("CLI-INIT-INDEX", "--static-only")
        suite.skip("CLI-STATE-UPGRADE", "--static-only")
        suite.skip("CLI-ROUTES", "--static-only")
        suite.skip("CLI-LANGUAGE-CONTROL", "--static-only")
        suite.skip("CLI-VALIDATE-STATES", "--static-only")
        suite.skip("CLI-APPROVAL-OK", "--static-only")
        suite.skip("CLI-TEMPLATE-GATES", "--static-only")
        suite.skip("CLI-PREFLIGHT-SANITIZE", "--static-only")
        suite.skip("CLI-MIN-DIFF", "--static-only")
        suite.skip("CLI-BUNDLE-PRINT-SHEET", "--static-only")
        suite.skip("CLI-G2-EXACT-SNAPSHOT", "--static-only")
        suite.skip("CLI-INVALIDATION", "--static-only")
        suite.skip("CLI-MEMORY-CONTEXT", "--static-only")
        suite.skip("CLI-INCREMENTAL-TRANSACTIONS", "--static-only")
        suite.skip("CLI-LIFECYCLE-RUNTIME", "--static-only")
    else:
        cli = find_cli()
        if cli is None:
            suite.fail("CLI-PRESENT", "未找到 scripts/legal_case_os.py 或 scripts/legal_case_cli.py")
        else:
            suite.pass_("CLI-PRESENT", str(cli.relative_to(PROJECT_ROOT)))
            suite.check("CLI-HELP", lambda: check_cli_help(cli))
            suite.check("CLI-INIT-INDEX", lambda: check_cli_init_and_index(cli))
            suite.check("CLI-STATE-UPGRADE", lambda: check_cli_state_upgrade(cli))
            suite.check("CLI-ROUTES", lambda: check_cli_routes(cli))
            suite.check("CLI-LANGUAGE-CONTROL", lambda: check_cli_language_control(cli))
            suite.check("CLI-VALIDATE-STATES", lambda: check_cli_validate_states(cli))
            suite.check("CLI-APPROVAL-OK", lambda: check_cli_approval_ok(cli))
            suite.check("CLI-TEMPLATE-GATES", lambda: check_cli_template_gates(cli))
            suite.check("CLI-PREFLIGHT-SANITIZE", lambda: check_cli_preflight_and_sanitize(cli))
            suite.check("CLI-MIN-DIFF", lambda: check_cli_minimal_diff(cli))
            suite.check("CLI-BUNDLE-PRINT-SHEET", lambda: check_cli_bundle_and_print_sheet(cli))
            suite.check("CLI-G2-EXACT-SNAPSHOT", lambda: check_cli_g2_exact_snapshot(cli))
            suite.check("CLI-INVALIDATION", lambda: check_cli_invalidation(cli))
            suite.check("CLI-MEMORY-CONTEXT", lambda: check_cli_memory_context(cli))
            suite.check("CLI-INCREMENTAL-TRANSACTIONS", lambda: check_cli_incremental_transactions(cli))
            suite.check("CLI-LIFECYCLE-RUNTIME", lambda: check_cli_lifecycle_runtime(cli))

    report = build_report(suite.results, mode)
    for result in suite.results:
        print(f"[{result.status}] {result.check_id}: {result.detail}")
    print(json.dumps({"summary": report["counts"], "ok": report["ok"]}, ensure_ascii=False))

    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
