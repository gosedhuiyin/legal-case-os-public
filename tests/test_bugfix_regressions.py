#!/usr/bin/env python3
"""Focused regressions for language, memory recall, and concurrent state commits."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import legal_case_os  # noqa: E402
from legal_case_os_lib import core  # noqa: E402
from legal_case_os_lib.documents import court_prose_lint, preflight  # noqa: E402
from legal_case_os_lib.language import prepare_language_input  # noqa: E402
from legal_case_os_lib.lifecycle import _query_terms, build_context_capsule  # noqa: E402


def _concurrent_commit_worker(
    state_path_text: str, marker: str, ready_text: str, start_text: str, result_text: str
) -> int:
    state_path = Path(state_path_text)
    old_state = core.load_json(state_path)
    candidate = copy.deepcopy(old_state)
    candidate["status"].setdefault("degradations", []).append(marker)
    Path(ready_text).write_text(marker, encoding="utf-8")
    deadline = time.monotonic() + 15
    while not Path(start_text).exists():
        if time.monotonic() >= deadline:
            Path(result_text).write_text(json.dumps([marker, "BARRIER_TIMEOUT", None]), encoding="utf-8")
            return 3
        time.sleep(0.02)
    try:
        event = core.mutate_state(
            state_path,
            candidate,
            actor=f"TEST-ONLY-{marker}",
            command="concurrent-regression",
            event_type="concurrent_regression",
            details={"marker": marker},
            old_state=old_state,
        )
        outcome = [marker, "ok", event["event_hash"]]
    except core.LegalCaseError as exc:
        outcome = [marker, exc.code, None]
    Path(result_text).write_text(json.dumps(outcome), encoding="utf-8")
    return 0


class BugfixRegressions(unittest.TestCase):
    maxDiff = None

    def test_multiple_quoted_template_refs_stay_parameters_and_are_clean(self) -> None:
        cases = (
            "按‘8073质证意见’和‘8075质证意见’的风格生成一份质证意见",
            "按“8073质证意见”和“8075质证意见”的风格生成一份质证意见",
            "按'8073质证意见'和'8075质证意见'的风格生成一份质证意见",
            '按"8073质证意见"和"8075质证意见"的风格生成一份质证意见',
            "按“‘8073质证意见’和‘8075质证意见’”的风格生成一份质证意见",
        )
        for text in cases:
            with self.subTest(text=text):
                prepared = prepare_language_input(text)
                self.assertEqual(
                    prepared["template_refs"],
                    ["8073质证意见", "8075质证意见"],
                )
                quoted_segments = [
                    segment for segment in prepared["segments"]
                    if any(mark in segment["text"] for mark in ("‘", "“", "'", '"'))
                ]
                self.assertTrue(quoted_segments)
                self.assertTrue(all(
                    segment["source_kind"] == "user_parameter"
                    for segment in quoted_segments
                ))
                self.assertIn("8073质证意见", prepared["command_text"])
                self.assertIn("8075质证意见", prepared["command_text"])
                routed = legal_case_os.route_text(text, None)
                self.assertEqual(
                    routed["task_frame"]["template_refs"],
                    ["8073质证意见", "8075质证意见"],
                )

        single = prepare_language_input("按‘8073质证意见’的风格生成一份质证意见")
        self.assertEqual(single["template_refs"], ["8073质证意见"])

        repeated_marker_cases = (
            "参考“8073质证意见”模板和“8075答辩状”模板生成文书",
            "参考「8073质证意见」和「8075答辩状」的模板生成文书",
            "参考《8073质证意见》和《8075答辩状》的模板生成文书",
        )
        for text in repeated_marker_cases:
            with self.subTest(repeated_marker=text):
                expected = ["8073质证意见", "8075答辩状"]
                self.assertEqual(prepare_language_input(text)["template_refs"], expected)
                self.assertEqual(
                    legal_case_os.route_text(text, None)["task_frame"]["template_refs"],
                    expected,
                )

        quoted_evidence = prepare_language_input(
            "材料中写：“按‘8073质证意见’和‘8075质证意见’的风格生成”。只分析该段"
        )
        self.assertEqual(quoted_evidence["template_refs"], [])
        self.assertTrue(any(
            segment["source_kind"] == "quoted_data"
            for segment in quoted_evidence["segments"]
        ))
        self.assertNotIn("8073质证意见", quoted_evidence["command_text"])
        self.assertNotIn("8075质证意见", quoted_evidence["command_text"])
        evidence_route = legal_case_os.route_text(
            "材料中写：“按‘8073质证意见’和‘8075质证意见’的风格生成”。只分析该段",
            None,
        )
        self.assertEqual(evidence_route["task_frame"]["template_refs"], [])
        self.assertEqual(evidence_route["task_frame"]["task_kind"], "analyze")

    def test_court_prose_lint_blocks_plain_ai_and_editorial_traces(self) -> None:
        lint = court_prose_lint(
            "AI认为：该证据可以采用。\n建议：提交前再核对。\n内部备注：仅供讨论。"
        )
        self.assertFalse(lint["ok"])
        self.assertEqual(
            {item["marker"] for item in lint["findings"]},
            {"ai_voice", "editorial_directive", "internal_note"},
        )
        self.assertTrue(all("matched_text" not in item for item in lint["findings"]))

        with tempfile.TemporaryDirectory(prefix="TEST-ONLY-prose-lint-") as temporary:
            path = Path(temporary) / "TEST-ONLY-ai-trace.docx"
            document_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>AI\xe8\xae\xa4\xe4\xb8\xba\xef\xbc\x9a\xe8\xaf\xa5\xe8\xaf\x81\xe6\x8d\xae\xe5\x8f\xaf\xe4\xbb\xa5\xe9\x87\x87\xe7\x94\xa8\xe3\x80\x82</w:t></w:r></w:p></w:body>
</w:document>'''
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
                package.writestr("word/document.xml", document_xml)
            result = preflight(path)
            self.assertFalse(result["ok"])
            self.assertIn(
                "court_prose_marker",
                {item["kind"] for item in result["blocking_findings"]},
            )

    def test_service_deadline_is_read_only_but_execution_stays_blocked(self) -> None:
        state = core.load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "incremental-case" / "_case-state" / "case-state.json"
        )
        result = legal_case_os.route_text("请基于本案现有材料核对送达期限并列出缺口", state)
        self.assertEqual(result["route"]["action"], "analyze")
        self.assertEqual(result["task_frame"]["task_kind"], "analyze")
        self.assertEqual(result["task_frame"]["mutation_scope"], "read_only")
        self.assertEqual(result["task_frame"]["read_acl"]["mode"], "default")
        self.assertEqual(result["task_frame"]["output_scope"]["items"], [])
        self.assertEqual(result["task_frame"]["memory_context_policy"]["read_mode"], "relevant")
        self.assertEqual(result["clarification"]["decision"], "execute")

        for text in (
            "核对完期限后，把材料送达给对方",
            "将已核对材料送达给对方",
            "请核对提交期限并提交材料",
            "请核对上传状态并上传材料",
            "请核对打印记录并打印文件",
            "请把分析报告提交法院",
            "请分析完后把材料提交法院",
            "提交到法院",
            "先分析，然后发送给客户",
            "请发送邮件",
            "发送这封邮件",
            "提交补充材料到法院",
            "上传材料到法院系统",
            "报送材料至法院",
            "送达材料给对方",
            "寄材料给客户",
            "打印两份起诉状",
            "打印这个文件",
            "删除这份文件",
            "删除该材料",
            "请把未来方案提交法院",
            "将未知来源材料提交法院",
            "将未核对材料提交法院",
            "请把尚未核对的材料提交法院",
            "不要提交法院，先发送给客户",
            "先别上传，改成提交到法院",
            "发这封邮件给客户",
            "请转发这封邮件给客户",
            "抄送这封邮件给客户",
            "交材料给法院",
            "送材料给对方",
            "传材料给客户",
            "投递材料给法院",
            "呈交材料给法院",
            "如何送达给对方？按此方法立即送达给对方",
            "告诉我怎么提交并提交法院",
        ):
            with self.subTest(external=text):
                blocked = legal_case_os.route_text(text, state)
                self.assertEqual(blocked["task_frame"]["task_kind"], "external_action")
                self.assertEqual(blocked["clarification"]["decision"], "block")

        for text in (
            "请核对提交到法院的材料清单",
            "请审查上传到法院系统的材料版本",
            "请列出需要提交给法院的材料",
            "请分析直接送达的效力",
            "请研究再次提交的效力",
            "请核对合并提交清单",
            "不要把材料提交法院，只分析现有材料",
            "是否应把材料提交到法院",
            "请勿提交法院",
            "暂缓提交法院",
            "不再提交法院",
            "尚未提交法院",
            "未提交法院的材料有哪些",
            "准备提交法院的材料有哪些",
            "拟提交法院的材料清单",
            "待提交法院的材料有哪些",
            "已提交法院的材料有哪些",
            "已经提交到法院了吗",
            "提交法院了吗",
            "送达给对方了吗",
            "材料提交法院没有",
            "你有没有把材料提交法院",
            "有没有送达对方",
            "能不能提交到法院",
            "该不该提交法院",
            "应否提交法院",
            "请评估立即送达的必要性",
            "请分析之后提交的材料是否有效",
            "把材料拟提交到法院",
            "把材料已提交到法院",
            "送达给对方的回证在哪里",
            "上传到法院系统的截图是哪一张",
            "提交法院的日期是什么",
            "送达对方的期限是哪天",
            "向法院提交材料的要求有哪些",
            "报送材料至法院的义务是什么",
            "提交到法院的回执在哪里",
            "发送给客户的结果如何",
            "送达对方的情况怎么样",
            "提交法院的依据是什么",
            "邮寄给客户的凭证有哪些",
            "是否已经送达对方",
            "抄送给客户的邮件是哪一封",
            "抄送给客户的是哪封邮件",
            "发给客户的是哪条消息",
            "提交法院的是哪份材料",
            "上传到法院系统的是哪个文件",
            "送达给对方的是哪批材料",
            "谁把材料提交法院了",
            "是谁把材料提交法院的",
            "什么时候提交法院的",
            "何时提交到法院",
            "为什么提交法院",
            "为何送达给对方",
            "怎么提交到法院",
            "如何送达给对方",
            "哪一方把材料提交法院了",
        ):
            with self.subTest(read_only=text):
                allowed = legal_case_os.route_text(text, state)
                self.assertNotEqual(allowed["task_frame"]["task_kind"], "external_action")
                self.assertNotEqual(allowed["clarification"]["decision"], "block")
                self.assertEqual(allowed["task_frame"]["mutation_scope"], "read_only")

        no_false_scope = legal_case_os.route_text("核对权限和期限", state)
        self.assertEqual(no_false_scope["task_frame"]["read_acl"]["mode"], "default")
        self.assertEqual(no_false_scope["task_frame"]["output_scope"]["items"], [])

        self.assertEqual(prepare_language_input("只限于合同第8条")["scope_target"], "合同第8条")

        evidence_reserve = legal_case_os.route_text("这份证据先不要提交，放到后续备用", state)
        self.assertEqual(evidence_reserve["route"]["action"], "modify")
        package_only = legal_case_os.route_text("整理成待打印包，但不要打印、不要提交", state)
        self.assertEqual(package_only["route"]["action"], "package")
        filing_question = legal_case_os.route_text("是否应把材料提交到法院", state)
        self.assertEqual(filing_question["task_frame"]["task_kind"], "freeform_readonly")

    def test_chinese_recall_keeps_issue_and_material_adverse_evidence(self) -> None:
        state = core.load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "complex-case" / "_case-state" / "case-state.json"
        )
        capsule = build_context_capsule(
            state,
            mode="relevant",
            query="请核对诉讼时效争点对应的支持材料和重大反证",
            max_items=6,
        )
        ids = {item["object_id"] for item in capsule["included"]}
        self.assertIn("I-LIMITATION-001", ids)
        self.assertNotIn("I-QUALITY-001", ids)
        self.assertIn("E-TEST-COMPLEX-002", ids)
        self.assertIn("E-TEST-COMM-001", ids)
        self.assertNotEqual(capsule["coverage"], "complete_for_selected_scope")

        by_id = build_context_capsule(state, mode="relevant", query="I-LIMITATION-001", max_items=8)
        by_id_ids = {item["object_id"] for item in by_id["included"]}
        self.assertIn("I-LIMITATION-001", by_id_ids)
        self.assertIn("D-TEST-COMPLEX-G1-001", by_id_ids)

        missing = build_context_capsule(
            state, mode="relevant", query="完全不存在的量子航运术语", max_items=6
        )
        self.assertEqual(missing["coverage"], "no_indexed_relation")
        self.assertEqual(missing["included"], [])

        prefix_collision = build_context_capsule(
            state, mode="relevant", query="S-TEST-COMPLEX-0010", max_items=6
        )
        prefix_ids = {item["object_id"] for item in prefix_collision["included"]}
        self.assertNotIn("S-TEST-COMPLEX-001", prefix_ids)

        bounded = build_context_capsule(state, mode="relevant", query="I-LIMITATION-001", max_items=1)
        self.assertEqual([item["object_id"] for item in bounded["included"]], ["I-LIMITATION-001"])
        self.assertEqual(bounded["coverage"], "bounded_selection")
        self.assertTrue(any("E-TEST-COMPLEX-002" in warning for warning in bounded["warnings"]))

        long_query = "".join(chr(0x4E00 + index) for index in range(110)) + "最后重点分析测试时效"
        self.assertIn("时效", _query_terms(long_query))
        long_capsule = build_context_capsule(state, mode="relevant", query=long_query, max_items=6)
        self.assertIn("I-LIMITATION-001", {item["object_id"] for item in long_capsule["included"]})

    def test_adverse_pool_prefers_stronger_evidence_and_reflect_can_recall_it(self) -> None:
        state = core.load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "complex-case" / "_case-state" / "case-state.json"
        )
        reflected = build_context_capsule(
            state, mode="reflect", query="有哪些重大反证和风险", max_items=6
        )
        reflected_ids = {item["object_id"] for item in reflected["included"]}
        self.assertIn("E-TEST-COMM-001", reflected_ids)
        self.assertIn("E-TEST-COMPLEX-002", reflected_ids)

        medium_template = copy.deepcopy(next(
            item for item in state["evidence"] if item["id"] == "E-TEST-COMPLEX-002"
        ))
        medium_items = []
        for index in range(3):
            item = copy.deepcopy(medium_template)
            item["id"] = f"E-TEST-ADVERSE-MEDIUM-{index}"
            item["relevance"] = "medium"
            item["probative_strength"] = "medium"
            medium_items.append(item)
        strongest = copy.deepcopy(medium_template)
        strongest["id"] = "E-TEST-ADVERSE-HIGH-HIGH"
        strongest["relevance"] = "high"
        strongest["probative_strength"] = "high"
        state["evidence"] = medium_items + state["evidence"] + [strongest]

        ranked = build_context_capsule(state, mode="relevant", query="列出重大反证和风险", max_items=10)
        ranked_ids = {item["object_id"] for item in ranked["included"]}
        self.assertIn("E-TEST-COMM-001", ranked_ids)
        self.assertIn("E-TEST-ADVERSE-HIGH-HIGH", ranked_ids)
        self.assertNotIn("E-TEST-ADVERSE-MEDIUM-0", ranked_ids)
        self.assertTrue(any("池外还有" in warning for warning in ranked["warnings"]))

    def test_file_scope_stays_within_explicit_one_hop_relations(self) -> None:
        state = core.load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "incremental-case" / "_case-state" / "case-state.json"
        )
        capsule = build_context_capsule(
            state,
            mode="file_scoped",
            query="完全无关的查询",
            scopes=["S-TEST-INCREMENTAL-007"],
            max_items=12,
        )
        ids = {item["object_id"] for item in capsule["included"]}
        self.assertIn("S-TEST-INCREMENTAL-007", ids)
        self.assertNotIn("F-TEST-IDENTITY-001", ids)
        self.assertEqual(capsule["coverage"], "complete_for_selected_scope")

    def test_reflection_preserves_declared_counterevidence(self) -> None:
        state = core.load_json(
            PROJECT_ROOT / "tests" / "fixtures" / "incremental-case" / "_case-state" / "case-state.json"
        )
        capsule = build_context_capsule(
            state, mode="reflect", query="北辰平台潜在新案线索", max_items=12
        )
        ids = {item["object_id"] for item in capsule["included"]}
        self.assertIn("SEED-TEST-001", ids)
        self.assertIn("F-TEST-IDENTITY-001", ids)

    def _initialize_temporary_state(self, root: Path) -> Path:
        state_path = root / "_case-state" / "case-state.json"
        state_path.parent.mkdir(parents=True)
        initial = core.make_empty_state("M-TEST-CONCURRENCY-001", "TEST-ONLY concurrency", "test")
        core.mutate_state(
            state_path,
            initial,
            actor="TEST-ONLY-setup",
            command="init-regression",
            event_type="workspace_initialized",
            old_state=None,
        )
        return state_path

    def test_two_processes_cannot_overwrite_the_same_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="legal-case-os-cas-") as temporary:
            state_path = self._initialize_temporary_state(Path(temporary))
            # LibreOffice's bundled Python reports its runtime directory rather
            # than python.exe in sys.executable; normalize it for Windows spawn.
            launcher = Path(sys.executable)
            if launcher.is_dir():
                launcher = launcher.parent / "python.exe"
            start_path = Path(temporary) / "start.signal"
            ready_paths = [Path(temporary) / f"ready-{index}" for index in range(2)]
            result_paths = [Path(temporary) / f"result-{index}.json" for index in range(2)]
            processes = [
                subprocess.Popen(
                    [
                        str(launcher), "-B", str(Path(__file__).resolve()), "--cas-worker",
                        str(state_path), marker, str(ready_paths[index]), str(start_path), str(result_paths[index]),
                    ],
                    cwd=str(PROJECT_ROOT),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index, marker in enumerate(("writer-a", "writer-b"))
            ]
            deadline = time.monotonic() + 15
            while not all(path.exists() for path in ready_paths):
                if time.monotonic() >= deadline:
                    self.fail("并发 worker 未在时限内到达同一旧版本屏障")
                time.sleep(0.02)
            start_path.write_text("go", encoding="utf-8")
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr or stdout)
            outcomes = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
            self.assertEqual(sum(status == "ok" for _marker, status, _hash in outcomes), 1)
            self.assertEqual(
                sum(status == "STALE_STATE_VERSION" for _marker, status, _hash in outcomes), 1
            )

            final_state = core.load_json(state_path)
            markers = set(final_state["status"]["degradations"]) & {"writer-a", "writer-b"}
            self.assertEqual(len(markers), 1)
            audit_lines = [
                line for line in (state_path.parent / "audit-log.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(audit_lines), 2)
            self.assertTrue(core.validate_state_file(state_path)["ok"])

    def test_pending_wal_recovers_missing_audit_append_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="legal-case-os-wal-") as temporary:
            state_path = self._initialize_temporary_state(Path(temporary))
            old_state = core.load_json(state_path)
            candidate = copy.deepcopy(old_state)
            candidate["status"]["degradations"].append("wal-state-written")

            original_atomic_write_json = core.atomic_write_json

            def fail_after_state_replace(path: Path, value: object) -> None:
                original_atomic_write_json(path, value)
                if path.resolve() == state_path.resolve():
                    raise RuntimeError("TEST-ONLY crash after state replace")

            core.atomic_write_json = fail_after_state_replace
            try:
                with self.assertRaisesRegex(RuntimeError, "TEST-ONLY crash"):
                    core.mutate_state(
                        state_path,
                        candidate,
                        actor="TEST-ONLY-wal",
                        command="wal-regression",
                        event_type="wal_regression",
                        old_state=old_state,
                    )
            finally:
                core.atomic_write_json = original_atomic_write_json

            self.assertTrue((state_path.parent / ".pending-state-transaction.json").exists())
            state_after_crash = core.load_json(state_path)
            second = copy.deepcopy(state_after_crash)
            second["status"]["degradations"].append("recovery-continued")
            core.mutate_state(
                state_path,
                second,
                actor="TEST-ONLY-recovery",
                command="recovery-regression",
                event_type="recovery_regression",
                old_state=state_after_crash,
            )

            self.assertFalse((state_path.parent / ".pending-state-transaction.json").exists())
            events = [
                json.loads(line)
                for line in (state_path.parent / "audit-log.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
            self.assertEqual(len({event["event_hash"] for event in events}), 3)
            self.assertTrue(core.validate_state_file(state_path)["ok"])

    def test_missing_audit_newline_fails_closed_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="legal-case-os-audit-newline-") as temporary:
            state_path = self._initialize_temporary_state(Path(temporary))
            audit_path = state_path.parent / "audit-log.jsonl"
            audit_path.write_bytes(audit_path.read_bytes().rstrip(b"\n"))
            state_before = state_path.read_bytes()
            audit_before = audit_path.read_bytes()
            old_state = core.load_json(state_path)
            candidate = copy.deepcopy(old_state)
            candidate["status"]["degradations"].append("must-not-commit")

            with self.assertRaises(core.LegalCaseError) as raised:
                core.mutate_state(
                    state_path,
                    candidate,
                    actor="TEST-ONLY-audit-newline",
                    command="audit-newline-regression",
                    event_type="audit_newline_regression",
                    old_state=old_state,
                )

            self.assertEqual(raised.exception.code, "AUDIT_LOG_INVALID")
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(audit_path.read_bytes(), audit_before)
            self.assertFalse((state_path.parent / ".pending-state-transaction.json").exists())

    def test_file_lock_rejects_nonfinite_or_negative_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="legal-case-os-lock-timeout-") as temporary:
            lock_path = Path(temporary) / "test.lock"
            for timeout in (float("nan"), float("inf"), float("-inf"), -0.01):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(core.LegalCaseError) as raised:
                        with core.exclusive_file_lock(lock_path, timeout):
                            self.fail("invalid timeout must fail before lock acquisition")
                    self.assertEqual(raised.exception.code, "INVALID_LOCK_TIMEOUT")


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--cas-worker":
        raise SystemExit(_concurrent_commit_worker(*sys.argv[2:]))
    unittest.main(verbosity=2)
