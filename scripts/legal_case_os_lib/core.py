from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = PROJECT_ROOT / "shared" / "schemas" / "case-state.schema.json"
WORKSPACE_TEMPLATE = PROJECT_ROOT / "matter-workspace-template"


class LegalCaseError(RuntimeError):
    """A deterministic, user-correctable workflow error."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, **self.details}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_content_hash(state: dict[str, Any]) -> str:
    """Hash current legal state without the audit cursor to avoid a circular hash."""
    payload = copy.deepcopy(state)
    payload.pop("audit", None)
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def load_json(path: Path) -> Any:
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


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _full_state_hash(state: dict[str, Any] | None) -> str | None:
    if state is None:
        return None
    return sha256_bytes(canonical_json(state).encode("utf-8"))


def _projection_version_for_error(state: dict[str, Any] | None) -> int | None:
    if state is None:
        return None
    focus_version = state.get("focus", {}).get("state_version")
    audit_sequence = int(state.get("audit", {}).get("last_sequence", 0))
    if isinstance(focus_version, int) and focus_version >= 1:
        return max(focus_version, audit_sequence, 1)
    return max(audit_sequence, 1)


@contextmanager
def exclusive_file_lock(
    lock_path: Path,
    timeout_seconds: float = 10.0,
    *,
    timeout_code: str = "FILE_LOCK_TIMEOUT",
    timeout_message: str = "文件正由另一进程修改；等待锁超时，未写入任何内容。",
    timeout_details: dict[str, Any] | None = None,
) -> Iterable[None]:
    """Acquire a crash-released, cross-process lock for one local resource."""
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise LegalCaseError("INVALID_LOCK_TIMEOUT", "文件锁等待时间必须是有限的非负数。")
    lock_path = lock_path.resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        last_error: OSError | None = None
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                last_error = exc
                contention = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EBUSY, errno.EDEADLK}
                contention = contention or getattr(exc, "winerror", None) in {33, 36}
                if not contention:
                    raise
                if time.monotonic() >= deadline:
                    details = {"lock": str(lock_path), "timeout_seconds": timeout_seconds}
                    details.update(timeout_details or {})
                    raise LegalCaseError(
                        timeout_code,
                        timeout_message,
                        details,
                    ) from last_error
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()


@contextmanager
def _state_file_lock(state_path: Path, timeout_seconds: float = 10.0) -> Iterable[None]:
    """Acquire the shared crash-released lock used by every state writer."""
    with exclusive_file_lock(
        state_path.parent / ".case-state.lock",
        timeout_seconds,
        timeout_code="STATE_LOCK_TIMEOUT",
        timeout_message="案件状态正由另一对话或运行提交；等待锁超时，未写入任何内容。",
        timeout_details={"state": str(state_path)},
    ):
        yield


def _pending_transaction_path(state_path: Path) -> Path:
    return state_path.parent / ".pending-state-transaction.json"


def _validated_audit_events(raw: bytes, *, path: Path) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LegalCaseError("AUDIT_LOG_INVALID", "审计日志不是合法 UTF-8，已停止状态写入。", {"path": str(path)}) from exc
    events: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for index, line in enumerate((line for line in text.splitlines() if line.strip()), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LegalCaseError(
                "AUDIT_LOG_INVALID", "审计日志包含不完整或非法事件，已停止状态写入。",
                {"path": str(path), "line": index},
            ) from exc
        if not isinstance(event, dict):
            raise LegalCaseError("AUDIT_LOG_INVALID", "审计事件必须是 JSON 对象。", {"path": str(path), "line": index})
        unsigned = copy.deepcopy(event)
        supplied_hash = unsigned.pop("event_hash", None)
        expected_hash = sha256_bytes(canonical_json(unsigned).encode("utf-8"))
        if event.get("sequence") != index or supplied_hash != expected_hash:
            raise LegalCaseError(
                "AUDIT_LOG_INVALID", "审计日志序号或事件哈希不一致，已停止状态写入。",
                {"path": str(path), "line": index},
            )
        if previous is not None and (
            event.get("previous_event_hash") != previous.get("event_hash")
            or event.get("before_state_hash") != previous.get("after_state_hash")
        ):
            raise LegalCaseError(
                "AUDIT_LOG_INVALID", "审计日志哈希链不连续，已停止状态写入。",
                {"path": str(path), "line": index},
            )
        events.append(event)
        previous = event
    return events


def _assert_audit_matches_state(state_path: Path, state: dict[str, Any] | None) -> tuple[bytes, list[dict[str, Any]]]:
    audit_path = state_path.parent / "audit-log.jsonl"
    raw = audit_path.read_bytes() if audit_path.exists() else b""
    if raw and not raw.endswith(b"\n"):
        raise LegalCaseError(
            "AUDIT_LOG_INVALID",
            "审计日志末尾缺少换行，已停止状态写入。",
            {"path": str(audit_path)},
        )
    events = _validated_audit_events(raw, path=audit_path)
    cursor = (state or {}).get("audit", {})
    sequence = int(cursor.get("last_sequence", 0))
    if sequence == 0:
        if events:
            raise LegalCaseError("AUDIT_STATE_MISMATCH", "状态审计游标为零，但审计日志并非空。")
        return raw, events
    if not events:
        raise LegalCaseError("AUDIT_STATE_MISMATCH", "状态指向审计事件，但审计日志为空。")
    last = events[-1]
    if (
        len(events) != sequence
        or last.get("event_hash") != cursor.get("last_event_hash")
        or state is None
        or last.get("after_state_hash") != state_content_hash(state)
    ):
        raise LegalCaseError(
            "AUDIT_STATE_MISMATCH",
            "案件状态与审计日志尾部不一致；为避免覆盖，已停止写入。",
            {"state": str(state_path), "audit": str(audit_path)},
        )
    return raw, events


def _remove_pending_transaction(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _recover_pending_transaction(state_path: Path) -> None:
    """Finish or abort a journaled state/audit commit using exact byte hashes."""
    pending_path = _pending_transaction_path(state_path)
    if not pending_path.exists():
        return
    try:
        journal = load_json(pending_path)
    except LegalCaseError as exc:
        raise LegalCaseError(
            "TRANSACTION_RECOVERY_REQUIRED", "待恢复事务日志无法读取；已停止写入。",
            {"path": str(pending_path)},
        ) from exc
    if not isinstance(journal, dict):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "待恢复事务日志格式无效。", {"path": str(pending_path)})
    unsigned = copy.deepcopy(journal)
    supplied_journal_hash = unsigned.pop("journal_hash", None)
    if supplied_journal_hash != sha256_bytes(canonical_json(unsigned).encode("utf-8")):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "待恢复事务日志哈希不匹配。", {"path": str(pending_path)})
    event = journal.get("event")
    if not isinstance(event, dict):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "待恢复事务缺少审计事件。", {"path": str(pending_path)})
    unsigned_event = copy.deepcopy(event)
    supplied_event_hash = unsigned_event.pop("event_hash", None)
    if supplied_event_hash != sha256_bytes(canonical_json(unsigned_event).encode("utf-8")):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "待恢复审计事件哈希不匹配。")
    event_line = (canonical_json(event) + "\n").encode("utf-8")
    if journal.get("event_line_sha256") != sha256_bytes(event_line):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "待恢复审计事件字节摘要不匹配。")

    audit_path = state_path.parent / "audit-log.jsonl"
    audit_raw = audit_path.read_bytes() if audit_path.exists() else b""
    prefix_size = int(journal.get("audit_size_before", -1))
    if prefix_size < 0 or len(audit_raw) < prefix_size:
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "审计日志短于事务记录的提交前长度。")
    audit_prefix = audit_raw[:prefix_size]
    if sha256_bytes(audit_prefix) != journal.get("audit_prefix_sha256"):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "审计日志提交前前缀已变化；拒绝猜测恢复。")
    base_events = _validated_audit_events(audit_prefix, path=audit_path)
    base_sequence = int(journal.get("base_audit_sequence", 0))
    base_event_hash = journal.get("base_audit_event_hash")
    if len(base_events) != base_sequence or (
        base_sequence and base_events[-1].get("event_hash") != base_event_hash
    ) or (not base_sequence and base_event_hash is not None):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "事务记录的基础审计游标不匹配。")
    suffix = audit_raw[prefix_size:]
    if suffix == b"":
        audit_position = "base"
    elif suffix == event_line:
        audit_position = "event"
    elif event_line.startswith(suffix):
        audit_position = "partial_event"
    else:
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "审计日志尾部不是当前事务的可验证前缀。")

    current_state = load_json(state_path) if state_path.exists() else None
    current_full_hash = _full_state_hash(current_state)
    if current_full_hash == journal.get("base_state_full_hash") and audit_position == "base":
        _remove_pending_transaction(pending_path)
        return
    if current_full_hash != journal.get("next_state_full_hash"):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "当前状态既不是事务前状态也不是事务后状态。")
    if current_state is None or state_content_hash(current_state) != event.get("after_state_hash"):
        raise LegalCaseError("TRANSACTION_RECOVERY_REQUIRED", "事务后状态内容哈希与审计事件不一致。")
    if audit_position in {"base", "partial_event"}:
        if audit_position == "partial_event":
            with audit_path.open("r+b") as handle:
                handle.truncate(prefix_size)
                handle.flush()
                os.fsync(handle.fileno())
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("ab") as handle:
            handle.write(event_line)
            handle.flush()
            os.fsync(handle.fileno())
    _remove_pending_transaction(pending_path)


def require_canonical_state_path(path: Path) -> None:
    if path.name != "case-state.json":
        raise LegalCaseError(
            "NOT_CANONICAL_STATE",
            "只允许把名为 case-state.json 的文件作为案件当前状态源。",
            {"path": str(path)},
        )


def ensure_not_originals(path: Path) -> None:
    if any(part.casefold() == "00-originals" for part in path.resolve().parts):
        raise LegalCaseError(
            "ORIGINALS_READ_ONLY",
            "禁止在 00-originals 中创建、覆盖、清洁、合并或重命名文件。",
            {"path": str(path)},
        )


def make_empty_state(matter_id: str, title: str, environment: str = "production") -> dict[str, Any]:
    if not re.fullmatch(r"M-[A-Za-z0-9._-]+", matter_id):
        raise LegalCaseError("INVALID_MATTER_ID", "matter-id 必须以 M- 开头且只含字母、数字、点、下划线或连字符。")
    if environment not in {"test", "production"}:
        raise LegalCaseError("INVALID_ENVIRONMENT", "environment 只能是 test 或 production。")
    created = now_iso()
    return {
        "schema_version": "1.2.0",
        "matter": {
            "id": matter_id,
            "title": title,
            "stage": "intake",
            "environment": environment,
            "party_role": None,
            "court": None,
            "cause_of_action": None,
            "objective": None,
            "created_at": created,
        },
        "intent": None,
        "focus": {
            "active_turn_id": None,
            "current_matter_id": matter_id,
            "current_source_id": None,
            "current_issue_id": None,
            "current_artifact_id": None,
            "current_template_id": None,
            "current_composition_spec_id": None,
            "current_batch_id": None,
            "current_event_id": None,
            "current_workflow_instance_id": None,
            "current_task_id": None,
            "recent_candidates": [],
            "pending_approval_ids": [],
            "output_mode": "discussion",
            "range_lock": [],
            "current_memory_mode": "relevant",
            "read_memory_scope": [],
            "state_version": 1,
            "snapshot": None,
        },
        "network_policy": {
            "mode": "deny",
            "proprietary_services_allowed": False,
            "api_credentials_allowed": False,
            "last_degradation": None,
        },
        "sources": [],
        "facts": [],
        "issues": [],
        "theories": [],
        "evidence": [],
        "authorities": [],
        "decisions": [],
        "approvals": [],
        "artifacts": [],
        "composition_specs": [],
        "templates": [],
        "processing_coverages": [],
        "sanitization_reports": [],
        "package_manifests": [],
        "external_actions": [],
        "tasks": [],
        "material_batches": [],
        "case_events": [],
        "triage_cards": [],
        "deadline_records": [],
        "impact_assessments": [],
        "prospective_matter_seeds": [],
        "workflow_instances": [],
        "run_attempts": [],
        "memory_state": {
            "storage_scope": "matter_workspace",
            "default_read_mode": "relevant",
            "default_write_mode": "candidate",
            "last_committed_event_seq": 0,
            "last_batch_id": None,
            "current_view_artifact_id": None,
            "current_view_version": 0,
            "current_view_as_of_event_seq": 0,
            "current_view_input_hash": None,
            "current_view_updated_at": None,
            "last_reflection_event_seq": 0,
            "pending_candidate_ids": [],
            "last_commit_actor": None,
            "last_commit_at": None,
            "last_commit_approval_id": None,
        },
        "status": {"workflow": "new", "current_gate": None, "blockers": [], "degradations": []},
        "audit": {"path": "_case-state/audit-log.jsonl", "last_sequence": 0, "last_event_hash": None},
        "last_updated": created,
    }


def mutate_state(
    state_path: Path,
    new_state: dict[str, Any],
    *,
    actor: str,
    command: str,
    event_type: str,
    object_ids: Iterable[str] = (),
    details: dict[str, Any] | None = None,
    old_state: dict[str, Any] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Commit canonical state with a process lock, CAS, and recoverable audit WAL."""
    require_canonical_state_path(state_path)
    with _state_file_lock(state_path, lock_timeout_seconds):
        _recover_pending_transaction(state_path)
        current_state = load_json(state_path) if state_path.exists() else None
        audit_raw, _events = _assert_audit_matches_state(state_path, current_state)

        expected_full_hash = _full_state_hash(old_state)
        actual_full_hash = _full_state_hash(current_state)
        if expected_full_hash != actual_full_hash:
            raise LegalCaseError(
                "STALE_STATE_VERSION",
                "案件状态已被另一对话或运行更新，本轮候选不能覆盖；请重新构建上下文后再提交。",
                {
                    "expected_state_version": _projection_version_for_error(old_state),
                    "actual_state_version": _projection_version_for_error(current_state),
                    "expected_state_full_hash": expected_full_hash,
                    "actual_state_full_hash": actual_full_hash,
                },
            )

        before_hash = state_content_hash(current_state) if current_state is not None else None
        old_audit = (current_state or {}).get("audit", {})
        sequence = int(old_audit.get("last_sequence", 0)) + 1
        previous_event_hash = old_audit.get("last_event_hash")
        committed_state = copy.deepcopy(new_state)
        # `state_version` is the canonical projection revision. The caller's
        # earlier version check is advisory; this lock-local CAS is authoritative.
        prior_projection_version = (current_state or {}).get("focus", {}).get("state_version")
        if not isinstance(prior_projection_version, int) or prior_projection_version < 1:
            prior_projection_version = max(int(old_audit.get("last_sequence", 0)), 1)
        else:
            prior_projection_version = max(
                prior_projection_version, int(old_audit.get("last_sequence", 0)), 1
            )
        committed_state.setdefault("focus", {})["state_version"] = prior_projection_version + 1
        committed_state["last_updated"] = now_iso()
        after_hash = state_content_hash(committed_state)
        event = {
            "sequence": sequence,
            "event_id": f"EV-{uuid.uuid4().hex}",
            "matter_id": committed_state["matter"]["id"],
            "timestamp": now_iso(),
            "actor": actor or "local-user",
            "command": command,
            "event_type": event_type,
            "object_ids": list(object_ids),
            "before_state_hash": before_hash,
            "after_state_hash": after_hash,
            "previous_event_hash": previous_event_hash,
            "details": details or {},
        }
        event["event_hash"] = sha256_bytes(canonical_json(event).encode("utf-8"))
        committed_state["audit"] = {
            "path": "_case-state/audit-log.jsonl",
            "last_sequence": sequence,
            "last_event_hash": event["event_hash"],
        }
        event_line = (canonical_json(event) + "\n").encode("utf-8")
        journal = {
            "schema_version": "1.0.0",
            "transaction_id": f"TX-{uuid.uuid4().hex}",
            "state_file": state_path.name,
            "base_state_full_hash": actual_full_hash,
            "next_state_full_hash": _full_state_hash(committed_state),
            "base_audit_sequence": int(old_audit.get("last_sequence", 0)),
            "base_audit_event_hash": previous_event_hash,
            "audit_size_before": len(audit_raw),
            "audit_prefix_sha256": sha256_bytes(audit_raw),
            "event_line_sha256": sha256_bytes(event_line),
            "event": event,
        }
        journal["journal_hash"] = sha256_bytes(canonical_json(journal).encode("utf-8"))
        pending_path = _pending_transaction_path(state_path)
        atomic_write_json(pending_path, journal)
        atomic_write_json(state_path, committed_state)
        audit_path = state_path.parent / "audit-log.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("ab") as handle:
            handle.write(event_line)
            handle.flush()
            os.fsync(handle.fileno())
        _remove_pending_transaction(pending_path)
        return event


def _resolve_ref(schema_root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"Only local JSON Schema refs are supported: {ref}")
    current: Any = schema_root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[token]
    return current


def _is_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def validate_against_schema(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Validate the JSON-Schema subset used by this repository, dependency-free."""
    errors: list[str] = []

    def walk(value: Any, rule: dict[str, Any], location: str) -> None:
        if "$ref" in rule:
            walk(value, _resolve_ref(schema, rule["$ref"]), location)
            return
        if "allOf" in rule:
            for branch in rule["allOf"]:
                walk(value, branch, location)
        if "if" in rule:
            saved = len(errors)
            walk(value, rule["if"], location)
            condition_matches = len(errors) == saved
            del errors[saved:]
            selected = rule.get("then") if condition_matches else rule.get("else")
            if selected is not None:
                walk(value, selected, location)
        if "oneOf" in rule:
            branch_errors: list[list[str]] = []
            for branch in rule["oneOf"]:
                saved = len(errors)
                walk(value, branch, location)
                branch_errors.append(errors[saved:])
                del errors[saved:]
            passing = sum(1 for branch in branch_errors if not branch)
            if passing != 1:
                errors.append(f"{location}: oneOf 应且仅应匹配一个分支（实际 {passing}）")
            return
        if "const" in rule and value != rule["const"]:
            errors.append(f"{location}: 必须等于 {rule['const']!r}")
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{location}: {value!r} 不在允许值中")
        expected = rule.get("type")
        if expected is not None:
            expected_types = expected if isinstance(expected, list) else [expected]
            if not any(_is_type(value, item) for item in expected_types):
                errors.append(f"{location}: 类型应为 {expected_types}，实际为 {type(value).__name__}")
                return
        if isinstance(value, dict):
            required = rule.get("required", [])
            for key in required:
                if key not in value:
                    errors.append(f"{location}: 缺少必填字段 {key}")
            properties = rule.get("properties", {})
            if rule.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        errors.append(f"{location}.{key}: 不允许的字段")
            for key, child in properties.items():
                if key in value:
                    walk(value[key], child, f"{location}.{key}")
        elif isinstance(value, list):
            if len(value) < rule.get("minItems", 0):
                errors.append(f"{location}: 项目数量少于 {rule['minItems']}")
            if "maxItems" in rule and len(value) > rule["maxItems"]:
                errors.append(f"{location}: 项目数量多于 {rule['maxItems']}")
            if rule.get("uniqueItems"):
                seen: set[str] = set()
                for item in value:
                    marker = canonical_json(item)
                    if marker in seen:
                        errors.append(f"{location}: 含重复项目")
                        break
                    seen.add(marker)
            if "items" in rule:
                for index, item in enumerate(value):
                    walk(item, rule["items"], f"{location}[{index}]")
        elif isinstance(value, str):
            if len(value) < rule.get("minLength", 0):
                errors.append(f"{location}: 字符串过短")
            if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
                errors.append(f"{location}: 不符合格式 {rule['pattern']}")
            if rule.get("format") == "date-time":
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    errors.append(f"{location}: 不是 ISO 8601 时间")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                errors.append(f"{location}: 小于最小值 {rule['minimum']}")

    walk(instance, schema, "$")
    return errors


def _all_objects(state: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for collection in (
        "sources",
        "facts",
        "issues",
        "theories",
        "evidence",
        "authorities",
        "decisions",
        "approvals",
        "artifacts",
        "composition_specs",
        "templates",
        "processing_coverages",
        "sanitization_reports",
        "package_manifests",
        "external_actions",
        "tasks",
        "material_batches",
        "case_events",
        "triage_cards",
        "deadline_records",
        "impact_assessments",
        "prospective_matter_seeds",
        "workflow_instances",
        "run_attempts",
    ):
        for item in state.get(collection, []):
            yield collection, item


def _authority_is_test_only(authority: dict[str, Any], environment: str | None) -> bool:
    return (
        environment != "production"
        or authority.get("test_only") is True
        or authority.get("verification_status") == "test_only"
        or "TEST-ONLY" in str(authority.get("title") or "").upper()
    )


def validate_semantics(state: dict[str, Any], state_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    identifiers: dict[str, str] = {state.get("matter", {}).get("id", ""): "matter"}
    for collection, item in _all_objects(state):
        identifier = item.get("id")
        if not identifier:
            continue
        if identifier in identifiers:
            errors.append(f"对象 ID 重复：{identifier} 同时位于 {identifiers[identifier]} 和 {collection}")
        identifiers[identifier] = collection

    # v1.0 fixtures remain readable, while every newly-written v1.1+ projection
    # must carry the complete delta/lifecycle contract.
    if state.get("schema_version") in {"1.1.0", "1.2.0"}:
        required_v11_collections = (
            "material_batches",
            "case_events",
            "triage_cards",
            "deadline_records",
            "impact_assessments",
            "prospective_matter_seeds",
            "workflow_instances",
            "run_attempts",
        )
        for collection in required_v11_collections:
            if collection not in state:
                errors.append(f"v1.1 状态缺少顶层集合 {collection}")
        if "memory_state" not in state:
            errors.append("v1.1 状态缺少案件内 memory_state")
        intent = state.get("intent")
        if intent is not None:
            for field in ("memory_mode", "read_memory_scope", "write_memory_mode"):
                if field not in intent:
                    errors.append(f"v1.1 IntentEnvelope 缺少记忆控制字段 {field}")
        focus_v11 = state.get("focus", {})
        for field in (
            "current_batch_id",
            "current_event_id",
            "current_workflow_instance_id",
            "current_task_id",
            "current_memory_mode",
            "read_memory_scope",
            "state_version",
            "snapshot",
        ):
            if field not in focus_v11:
                errors.append(f"v1.1 FocusContext 缺少并发快照字段 {field}")
        if intent is not None:
            if intent.get("memory_mode") != focus_v11.get("current_memory_mode"):
                errors.append("IntentEnvelope.memory_mode 与 FocusContext.current_memory_mode 不一致")
            if intent.get("read_memory_scope", []) != focus_v11.get("read_memory_scope", []):
                errors.append("IntentEnvelope.read_memory_scope 与 FocusContext.read_memory_scope 不一致")
            if intent.get("memory_mode") == "file_scoped" and not intent.get("read_memory_scope"):
                errors.append("file_scoped 记忆模式必须明确 read_memory_scope")
            if intent.get("memory_mode") == "off" and intent.get("read_memory_scope"):
                errors.append("记忆读取已关闭时 read_memory_scope 必须为空")

    if state.get("schema_version") == "1.2.0":
        if "composition_specs" not in state:
            errors.append("v1.2 状态缺少顶层集合 composition_specs")
        intent_v12 = state.get("intent")
        if intent_v12 is not None and "template_refs" not in intent_v12:
            errors.append("v1.2 IntentEnvelope 缺少 template_refs")
        if "current_composition_spec_id" not in state.get("focus", {}):
            errors.append("v1.2 FocusContext 缺少 current_composition_spec_id")

    def require_reference(
        owner: str,
        field: str,
        object_id: Any,
        allowed: set[str] | None = None,
    ) -> bool:
        if object_id is None:
            return True
        collection = identifiers.get(str(object_id))
        if collection is None:
            errors.append(f"{owner} 的 {field} 引用了不存在的对象 {object_id}")
            return False
        if allowed is not None and collection not in allowed:
            errors.append(
                f"{owner} 的 {field} 应引用 {sorted(allowed)}，实际 {object_id} 位于 {collection}"
            )
            return False
        return True

    def require_references(
        owner: str,
        field: str,
        object_ids: Iterable[Any],
        allowed: set[str] | None = None,
    ) -> None:
        for object_id in object_ids:
            require_reference(owner, field, object_id, allowed)

    def is_human_actor(actor: Any) -> bool:
        normalized = str(actor or "").strip().casefold()
        return bool(normalized) and normalized not in {
            "ai", "model", "agent", "system", "automation", "assistant", "模型", "系统", "自动化"
        }

    source_ids = {item.get("id") for item in state.get("sources", [])}
    batch_by_id = {item.get("id"): item for item in state.get("material_batches", [])}
    event_by_id = {item.get("id"): item for item in state.get("case_events", [])}
    triage_by_source_batch: dict[tuple[str, str], list[dict[str, Any]]] = {}
    workflow_by_id = {item.get("id"): item for item in state.get("workflow_instances", [])}
    task_by_id = {item.get("id"): item for item in state.get("tasks", [])}

    arrival_keys: set[str] = set()
    for batch in state.get("material_batches", []):
        label = f"材料批次 {batch.get('id')}"
        arrival_key = str(batch.get("arrival_key") or "")
        if arrival_key in arrival_keys:
            errors.append(f"MaterialBatch arrival_key 重复：{arrival_key}")
        arrival_keys.add(arrival_key)
        require_references(label, "source_ids", batch.get("source_ids", []), {"sources"})
        before = batch.get("cursor_before")
        after = batch.get("cursor_after")
        if before is not None and after is not None and after < before:
            errors.append(f"{label} 的 cursor_after 不能小于 cursor_before")
        if batch.get("status") == "failed":
            if not batch.get("failure_reason"):
                errors.append(f"{label} 标为 failed 但缺少 failure_reason")
            if after is not None and after != before:
                errors.append(f"{label} 失败时不得推进 cursor_after")

    event_sequences: list[int] = []
    last_recorded_at: datetime | None = None
    active_memory_approvals = {
        item.get("object_id"): item
        for item in state.get("approvals", [])
        if item.get("gate") == "MEMORY_COMMIT"
        and item.get("decision") == "approved"
        and item.get("status") == "active"
    }
    for event in state.get("case_events", []):
        label = f"案件事件 {event.get('id')}"
        sequence = event.get("sequence")
        if isinstance(sequence, int):
            event_sequences.append(sequence)
        require_references(label, "source_ids", event.get("source_ids", []), {"sources"})
        require_references(label, "related_object_ids", event.get("related_object_ids", []))
        for locator in event.get("source_locators", []):
            require_reference(label, "source_locators.source_id", locator.get("source_id"), {"sources"})
        supersedes = event.get("supersedes_event_id")
        if supersedes is not None:
            if supersedes == event.get("id"):
                errors.append(f"{label} 不能取代自身")
            require_reference(label, "supersedes_event_id", supersedes, {"case_events"})
            prior = event_by_id.get(supersedes)
            if prior and isinstance(sequence, int) and prior.get("sequence", sequence) >= sequence:
                errors.append(f"{label} 只能取代更早的案件事件")
        if event.get("status") == "active" and event.get("confidence") in {"candidate", "model_inference"}:
            if event.get("id") not in active_memory_approvals:
                errors.append(f"{label} 属模型推断/候选，未经 active MEMORY_COMMIT 不得晋升为 active")
        try:
            recorded_at = datetime.fromisoformat(str(event.get("recorded_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            recorded_at = None
        if recorded_at is not None:
            if last_recorded_at is not None and recorded_at < last_recorded_at:
                errors.append("case_events 必须按 recorded_at 非递减排列")
            last_recorded_at = recorded_at
    if event_sequences:
        expected_sequences = list(range(1, len(event_sequences) + 1))
        if event_sequences != expected_sequences:
            errors.append(
                f"case_events.sequence 必须按追加顺序从1连续递增，实际为 {event_sequences}"
            )

    for card in state.get("triage_cards", []):
        label = f"材料分诊卡 {card.get('id')}"
        batch_id = card.get("batch_id")
        source_id = card.get("source_id")
        require_reference(label, "batch_id", batch_id, {"material_batches"})
        require_reference(label, "source_id", source_id, {"sources"})
        if batch_id in batch_by_id and source_id not in batch_by_id[batch_id].get("source_ids", []):
            errors.append(f"{label} 的 source_id 不属于其 MaterialBatch")
        triage_by_source_batch.setdefault((str(batch_id), str(source_id)), []).append(card)
        if card.get("status") == "committed":
            if not card.get("committed_at") or not card.get("committed_by"):
                errors.append(f"{label} 已提交但缺少 committed_by/committed_at")
            require_references(label, "direct_link_ids", card.get("direct_link_ids", []))
    for batch in state.get("material_batches", []):
        if batch.get("status") != "triaged":
            continue
        label = f"材料批次 {batch.get('id')}"
        if batch.get("cursor_after") is None or not batch.get("triaged_at"):
            errors.append(f"{label} 已分诊但缺少 cursor_after 或 triaged_at")
        for source_id in batch.get("source_ids", []):
            committed = [
                card for card in triage_by_source_batch.get((str(batch.get("id")), str(source_id)), [])
                if card.get("status") == "committed"
            ]
            if len(committed) != 1:
                errors.append(f"{label} 的来源 {source_id} 必须恰有一张 committed 分诊卡，实际 {len(committed)}")

    for deadline in state.get("deadline_records", []):
        label = f"期限记录 {deadline.get('id')}"
        source_event_id = deadline.get("source_event_id")
        locator = deadline.get("source_locator")
        if source_event_id is None and locator is None:
            errors.append(f"{label} 必须绑定案件事件或原始材料定位")
        require_reference(label, "source_event_id", source_event_id, {"case_events"})
        if locator is not None:
            require_reference(label, "source_locator.source_id", locator.get("source_id"), {"sources"})
        require_reference(
            label,
            "related_workflow_instance_id",
            deadline.get("related_workflow_instance_id"),
            {"workflow_instances"},
        )
        status = deadline.get("status")
        if status in {"verified", "completed"}:
            if not deadline.get("due_at") or not deadline.get("verified_at") or not is_human_actor(deadline.get("verified_by")):
                errors.append(f"{label} 只有经人工核验且有明确 due_at 才能标为 {status}")
        if status == "dismissed":
            if (
                not deadline.get("verified_at")
                or not is_human_actor(deadline.get("verified_by"))
                or not deadline.get("dismissal_reason")
            ):
                errors.append(f"{label} 驳回期限候选必须记录人工核验人、时间和理由")
        if status == "completed" and not deadline.get("completed_at"):
            errors.append(f"{label} 标为 completed 但缺少 completed_at")

    for impact in state.get("impact_assessments", []):
        label = f"影响评估 {impact.get('id')}"
        require_reference(label, "batch_id", impact.get("batch_id"), {"material_batches"})
        require_references(label, "source_ids", impact.get("source_ids", []), {"sources"})
        require_references(label, "affected_object_ids", impact.get("affected_object_ids", []))
        require_references(label, "stale_object_ids", impact.get("stale_object_ids", []))
        require_references(label, "counterevidence_object_ids", impact.get("counterevidence_object_ids", []))
        if impact.get("impact_level") in {"high", "critical"}:
            if not impact.get("requires_deep_analysis"):
                errors.append(f"{label} 属重大影响但未保留深析")
            if not impact.get("lawyer_review_required"):
                errors.append(f"{label} 属重大影响但未设置律师复核门禁")
        if impact.get("status") == "applied":
            if not impact.get("applied_at"):
                errors.append(f"{label} 已应用但缺少 applied_at")
            decision = impact.get("lawyer_decision")
            if impact.get("lawyer_review_required") and decision != "approved":
                errors.append(f"{label} 需要律师复核，未经 approved 不得应用")
            if not impact.get("lawyer_review_required") and decision not in {"not_required", "approved"}:
                errors.append(f"{label} 的 lawyer_decision 与 applied 状态不一致")
            for object_id in impact.get("stale_object_ids", []):
                target_collection, target = find_object(state, object_id)
                if target_collection in {
                    "facts", "issues", "theories", "evidence", "decisions",
                    "artifacts", "package_manifests", "approvals",
                } and target is not None and not (
                    target.get("stale") is True or target.get("status") == "stale"
                ):
                    errors.append(f"{label} 声明 {object_id} 失效，但当前对象未标为 stale")
        if impact.get("status") == "rejected" and impact.get("lawyer_decision") != "rejected":
            errors.append(f"{label} 标为 rejected 但缺少律师拒绝决定")

    matter_id = state.get("matter", {}).get("id")
    for seed in state.get("prospective_matter_seeds", []):
        label = f"潜在案件线索 {seed.get('id')}"
        if seed.get("origin_matter_id") != matter_id:
            errors.append(f"{label} 的 origin_matter_id 不等于当前案件")
        require_references(label, "trigger_event_ids", seed.get("trigger_event_ids", []), {"case_events"})
        require_references(label, "source_ids", seed.get("source_ids", []), {"sources"})
        require_references(label, "supporting_object_ids", seed.get("supporting_object_ids", []))
        require_references(label, "counterevidence_object_ids", seed.get("counterevidence_object_ids", []))
        status = seed.get("status")
        if status in {"approved", "promoted"}:
            if seed.get("human_decision") != "approved" or not is_human_actor(seed.get("decided_by")) or not seed.get("decided_at"):
                errors.append(f"{label} 未经明确人工批准不得标为 {status}")
        if status == "rejected":
            if seed.get("human_decision") != "rejected" or not is_human_actor(seed.get("decided_by")) or not seed.get("decided_at"):
                errors.append(f"{label} 的 rejected 状态缺少明确人工决定")
        if status == "promoted":
            promoted_matter_id = seed.get("promoted_matter_id")
            if not promoted_matter_id or promoted_matter_id == matter_id:
                errors.append(f"{label} 晋升时必须绑定不同于原案的新 matter_id")
            require_reference(label, "promotion_event_id", seed.get("promotion_event_id"), {"case_events"})

    for workflow in state.get("workflow_instances", []):
        label = f"业务工单 {workflow.get('id')}"
        if workflow.get("matter_id") != matter_id:
            errors.append(f"{label} 的 matter_id 不等于当前案件")
        require_reference(label, "origin_event_id", workflow.get("origin_event_id"), {"case_events"})
        require_references(label, "related_object_ids", workflow.get("related_object_ids", []))
        require_references(label, "task_ids", workflow.get("task_ids", []), {"tasks"})
        require_references(label, "output_ids", workflow.get("output_ids", []))
        require_reference(label, "approval_id", workflow.get("approval_id"), {"approvals"})
        for task_id in workflow.get("task_ids", []):
            task = task_by_id.get(task_id)
            if task and task.get("workflow_instance_id") != workflow.get("id"):
                errors.append(f"{label} 列出的任务 {task_id} 未反向绑定本工单")
        if workflow.get("status") in {"waiting_external", "waiting_approval"}:
            if not workflow.get("waiting_for") and not workflow.get("resume_condition"):
                errors.append(f"{label} 处于等待状态但缺少 waiting_for/resume_condition")
        if workflow.get("status") == "completed":
            if not workflow.get("completed_at") or not is_human_actor(workflow.get("completed_by")):
                errors.append(f"{label} 标为 completed 但缺少人工完成记录")
            unfinished = [
                task_id for task_id in workflow.get("task_ids", [])
                if task_by_id.get(task_id, {}).get("status") not in {"done", "cancelled"}
            ]
            if unfinished:
                errors.append(f"{label} 尚有未完成任务：{unfinished}")
        if workflow.get("status") in {"cancelled", "failed"} and not workflow.get("resolution_note"):
            errors.append(f"{label} 终止时必须把原因写入工单 resolution_note，不能只留在技术审计日志")

    for task in state.get("tasks", []):
        label = f"任务 {task.get('id')}"
        workflow_id = task.get("workflow_instance_id")
        require_reference(label, "workflow_instance_id", workflow_id, {"workflow_instances"})
        if workflow_id in workflow_by_id and task.get("id") not in workflow_by_id[workflow_id].get("task_ids", []):
            errors.append(f"{label} 绑定工单 {workflow_id}，但工单 task_ids 未包含该任务")
        require_references(label, "output_ids", task.get("output_ids", []))
        require_reference(label, "completed_by_event_id", task.get("completed_by_event_id"), {"case_events"})
        require_references(label, "depends_on_task_ids", task.get("depends_on_task_ids", []), {"tasks"})
        if task.get("id") in task.get("depends_on_task_ids", []):
            errors.append(f"{label} 不能依赖自身")
        if task.get("status") in {"waiting_external", "waiting_approval"}:
            if not task.get("waiting_for") and not task.get("resume_condition"):
                errors.append(f"{label} 处于等待状态但缺少 waiting_for/resume_condition")
        if workflow_id and task.get("status") == "done" and not task.get("completed_at"):
            errors.append(f"{label} 属于长期工单，完成时必须记录 completed_at")

    # Detect dependency cycles without changing the stored order.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_task(task_id: str) -> None:
        if task_id in visiting:
            errors.append(f"任务依赖形成循环：{task_id}")
            return
        if task_id in visited or task_id not in task_by_id:
            return
        visiting.add(task_id)
        for dependency_id in task_by_id[task_id].get("depends_on_task_ids", []):
            visit_task(str(dependency_id))
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in task_by_id:
        visit_task(str(task_id))

    for run in state.get("run_attempts", []):
        label = f"运行尝试 {run.get('id')}"
        workflow_id = run.get("workflow_instance_id")
        task_id = run.get("task_id")
        require_reference(label, "workflow_instance_id", workflow_id, {"workflow_instances"})
        require_reference(label, "task_id", task_id, {"tasks"})
        if workflow_id and task_id and task_id in task_by_id:
            if task_by_id[task_id].get("workflow_instance_id") != workflow_id:
                errors.append(f"{label} 的 task_id 与 workflow_instance_id 不属于同一工单")
        for snapshot in run.get("input_snapshot", []):
            require_reference(label, "input_snapshot.object_id", snapshot.get("object_id"))
        require_references(label, "output_ids", run.get("output_ids", []))
        before = run.get("cursor_before")
        after = run.get("cursor_after")
        if before is not None and after is not None and after < before:
            errors.append(f"{label} 的 cursor_after 不能小于 cursor_before")
        status = run.get("status")
        if status == "pending" and (run.get("started_at") or run.get("finished_at")):
            errors.append(f"{label} 尚未开始却已有开始或结束时间")
        if status == "running" and (not run.get("started_at") or run.get("finished_at")):
            errors.append(f"{label} 的 running 状态需要 started_at 且不能有 finished_at")
        if status in {"completed", "failed", "killed"}:
            if not run.get("started_at") or not run.get("finished_at"):
                errors.append(f"{label} 已终止但缺少 started_at/finished_at")
        if status in {"failed", "killed"}:
            if run.get("commit_status") == "committed":
                errors.append(f"{label} 失败或终止，不得把输出标为 committed")
            if not run.get("failure_reason"):
                errors.append(f"{label} 失败或终止但缺少 failure_reason")
        if status == "completed" and run.get("commit_status") == "committed":
            if run.get("state_version_after") is None:
                errors.append(f"{label} 声明已提交但缺少 state_version_after")
        version_before = run.get("state_version_before")
        version_after = run.get("state_version_after")
        if version_before is not None and version_after is not None and version_after < version_before:
            errors.append(f"{label} 的 state_version_after 不能小于 state_version_before")

    memory_state = state.get("memory_state")
    if isinstance(memory_state, dict):
        max_event_sequence = max(event_sequences, default=0)
        committed_sequence = int(memory_state.get("last_committed_event_seq", 0))
        reflection_sequence = int(memory_state.get("last_reflection_event_seq", 0))
        current_view_sequence = int(memory_state.get("current_view_as_of_event_seq", 0))
        if committed_sequence > max_event_sequence:
            errors.append("memory_state.last_committed_event_seq 超过案件事件时间线")
        if committed_sequence and committed_sequence not in set(event_sequences):
            errors.append("memory_state.last_committed_event_seq 未指向现存案件事件")
        if reflection_sequence > committed_sequence:
            errors.append("memory_state.last_reflection_event_seq 不能超过已提交事件游标")
        if current_view_sequence > committed_sequence:
            errors.append("当前案件视图不能读取尚未提交的事件")
        last_batch_id = memory_state.get("last_batch_id")
        require_reference("memory_state", "last_batch_id", last_batch_id, {"material_batches"})
        current_view_id = memory_state.get("current_view_artifact_id")
        if require_reference("memory_state", "current_view_artifact_id", current_view_id, {"artifacts"}) and current_view_id:
            artifact = next(
                (item for item in state.get("artifacts", []) if item.get("id") == current_view_id),
                None,
            )
            if artifact and artifact.get("version") != memory_state.get("current_view_version"):
                errors.append("memory_state.current_view_version 与当前视图产物版本不一致")
            if not memory_state.get("current_view_updated_at"):
                errors.append("memory_state 指向当前视图但缺少 current_view_updated_at")
        elif current_view_id is None and any(
            (
                memory_state.get("current_view_version", 0) != 0,
                memory_state.get("current_view_as_of_event_seq", 0) != 0,
                memory_state.get("current_view_input_hash") is not None,
                memory_state.get("current_view_updated_at") is not None,
            )
        ):
            errors.append("memory_state 未绑定当前视图产物，却保留了非空视图元数据")
        require_references("memory_state", "pending_candidate_ids", memory_state.get("pending_candidate_ids", []))
        approval_reference_ok = require_reference(
            "memory_state",
            "last_commit_approval_id",
            memory_state.get("last_commit_approval_id"),
            {"approvals"},
        )
        if approval_reference_ok and memory_state.get("last_commit_approval_id"):
            memory_approval = next(
                (
                    item for item in state.get("approvals", [])
                    if item.get("id") == memory_state.get("last_commit_approval_id")
                ),
                None,
            )
            committed_event = next(
                (
                    item for item in reversed(state.get("case_events", []))
                    if memory_approval.get("object_id") == item.get("id")
                    or memory_approval.get("object_id") in item.get("related_object_ids", [])
                ),
                None,
            )
            if not memory_approval or (
                memory_approval.get("gate") != "MEMORY_COMMIT"
                or memory_approval.get("decision") != "approved"
                or memory_approval.get("status") != "active"
            ):
                errors.append("memory_state.last_commit_approval_id 不是有效 MEMORY_COMMIT 批准")
            elif committed_event is None:
                errors.append("memory_state.last_commit_approval_id 的对象未出现在案件事件时间线")
            elif committed_event.get("sequence", committed_sequence + 1) > committed_sequence:
                errors.append("memory_state.last_commit_approval_id 指向尚未进入事件游标的案件事件")
            if not is_human_actor(memory_state.get("last_commit_actor")) or not memory_state.get("last_commit_at"):
                errors.append("记录 MEMORY_COMMIT 指针时必须保留人工 actor/time")

    focus_contract = state.get("focus", {})
    focus_reference_types = {
        "current_matter_id": {"matter"},
        "current_source_id": {"sources"},
        "current_issue_id": {"issues"},
        "current_artifact_id": {"artifacts"},
        "current_template_id": {"templates"},
        "current_composition_spec_id": {"composition_specs"},
        "current_batch_id": {"material_batches"},
        "current_event_id": {"case_events"},
        "current_workflow_instance_id": {"workflow_instances"},
        "current_task_id": {"tasks"},
    }
    for field, allowed in focus_reference_types.items():
        require_reference("FocusContext", field, focus_contract.get(field), allowed)
    focus_state_version = focus_contract.get("state_version")
    if focus_state_version is not None:
        audit_sequence = int(state.get("audit", {}).get("last_sequence", 0))
        minimum_state_version = max(audit_sequence, 1)
        maximum_state_version = audit_sequence + 1 if audit_sequence >= 1 else 1
        if not (minimum_state_version <= focus_state_version <= maximum_state_version):
            errors.append(
                f"FocusContext.state_version={focus_state_version} 与审计游标允许的投影版本范围 "
                f"{minimum_state_version}..{maximum_state_version} 不一致"
            )
    snapshot = focus_contract.get("snapshot")
    if isinstance(snapshot, dict):
        if snapshot.get("audit_sequence", 0) > state.get("audit", {}).get("last_sequence", 0):
            errors.append("FocusContext.snapshot 指向未来审计序号")
        require_references("FocusContext.snapshot", "object_ids", snapshot.get("object_ids", []))

    for source in state.get("sources", []):
        if source.get("original") and not source.get("read_only"):
            errors.append(f"原始材料 {source.get('id')} 必须标为 read_only=true")

    environment = state.get("matter", {}).get("environment")
    for authority in state.get("authorities", []):
        if environment == "production" and (
            authority.get("verification_status") != "verified" or not authority.get("production_eligible")
        ):
            # Research records may remain unverified; only flag if a filing artifact uses them below.
            pass

    active_g2_records = [
        approval
        for approval in state.get("approvals", [])
        if approval.get("gate") == "G2_evidence"
        and approval.get("decision") == "approved"
        and approval.get("status") == "active"
    ]
    if len(active_g2_records) > 1:
        errors.append(f"同一当前状态只能有一个 active G2，实际为 {len(active_g2_records)}")
    g2_scopes = active_g2_records[0].get("scope_snapshot", []) if len(active_g2_records) == 1 else []
    g2_evidence_ids = {snapshot.get("object_id") for snapshot in g2_scopes if str(snapshot.get("object_id", "")).startswith("E-")}
    current_evidence_ids = {item.get("id") for item in state.get("evidence", []) if item.get("current_submission")}
    if current_evidence_ids and len(active_g2_records) != 1:
        errors.append(f"存在 current_submission 时必须恰有一个 active G2，实际为 {len(active_g2_records)}")
    if len(active_g2_records) == 1 and g2_evidence_ids != current_evidence_ids:
        errors.append(
            "有效G2的证据快照集合与 current_submission 不一致："
            f"G2={sorted(g2_evidence_ids)}，current={sorted(current_evidence_ids)}"
        )
    source_by_id = {item.get("id"): item for item in state.get("sources", [])}
    required_assessment = (
        "authenticity",
        "legality",
        "relevance",
        "probative_strength",
        "adverse_content",
        "opens_new_issue",
        "opponent_likely_use",
        "duplication",
        "timing",
        "consequence_if_not_submitted",
        "substitute",
        "applicable_stage",
        "procedural_authority_id",
        "risk_assessment_artifact_id",
    )
    for evidence in state.get("evidence", []):
        decision = evidence.get("lawyer_decision")
        recipient_text = str(evidence.get("intended_recipient") or "")
        if re.search(r"保证.*对方.*看不到|对方.*一定.*看不到|仅法院.*(?:可见|披露)|绝不.*(?:送达|披露).*对方", recipient_text):
            errors.append(f"证据 {evidence.get('id')} 的披露表述承诺对方不可见；系统只能记录披露意图，不能作此保证")
        expected_views = {
            "submit_now": {"current_submission": True, "reserve": False, "internal_reference": False, "status": "approved"},
            "reserve": {"current_submission": False, "reserve": True, "internal_reference": False, "status": "approved"},
            "internal_only": {"current_submission": False, "reserve": False, "internal_reference": True, "status": "approved"},
            "do_not_submit": {"current_submission": False, "reserve": False, "internal_reference": False, "status": "rejected"},
        }
        if evidence.get("status") == "stale":
            # Preserve the historical lawyer decision without treating it as a
            # live submission/reserve instruction after its source or G2 lapsed.
            if any(evidence.get(field) for field in ("current_submission", "reserve", "internal_reference")):
                errors.append(f"失效证据 {evidence.get('id')} 不得保留当前提交、备用或内部参考活动标志")
            if decision in expected_views and not evidence.get("decision_reason"):
                errors.append(f"证据 {evidence.get('id')} 的历史律师决定缺少 decision_reason")
        elif decision in expected_views:
            for field, expected in expected_views[decision].items():
                if evidence.get(field) != expected:
                    errors.append(
                        f"证据 {evidence.get('id')} 的 {field}={evidence.get(field)!r} "
                        f"与律师决定 {decision} 应为 {expected!r} 不一致"
                    )
            if not evidence.get("decision_reason"):
                errors.append(f"证据 {evidence.get('id')} 已有律师决定但缺少 decision_reason")
        elif decision == "undecided":
            if evidence.get("current_submission") or evidence.get("reserve") or evidence.get("status") != "candidate":
                errors.append(f"证据 {evidence.get('id')} 尚未决定却被标为提交、备用或非candidate状态")
        if evidence.get("current_submission"):
            record_kinds = {locator.get("record_kind") for locator in evidence.get("source_refs", [])}
            if "model_inference" in record_kinds:
                errors.append(f"证据 {evidence.get('id')} 含 model_inference 来源，不能进入当前提交集")
            if "direct_record" not in record_kinds:
                errors.append(
                    f"证据 {evidence.get('id')} 没有 direct_record；user_statement 只能作为待核陈述，不能单独冒充原始材料"
                )
            if evidence.get("lawyer_decision") != "submit_now":
                errors.append(f"证据 {evidence.get('id')} 进入当前提交集但律师决定不是 submit_now")
            missing_assessment = [key for key in required_assessment if key not in evidence]
            if missing_assessment:
                errors.append(f"证据 {evidence.get('id')} 进入G2前缺少评估字段：{missing_assessment}")
            if evidence.get("service_obligation_status") == "requires_verification":
                errors.append(f"证据 {evidence.get('id')} 的送达或交换义务尚未核验")
            if evidence.get("disclosure_intent") == "court_candidate":
                if evidence.get("service_obligation_status") != "verified_not_required":
                    errors.append(f"证据 {evidence.get('id')} 请求仅作法院候选，但未核验为无需向其他程序参与人披露")
                authority_id = evidence.get("procedural_authority_id")
                authority = next((item for item in state.get("authorities", []) if item.get("id") == authority_id), None)
                if authority is None:
                    errors.append(f"证据 {evidence.get('id')} 的仅法院披露决定缺少可复核 procedural_authority_id")
                elif _authority_is_test_only(authority, environment) or not authority.get("production_eligible") or authority.get("verification_status") != "verified":
                    errors.append(f"证据 {evidence.get('id')} 的仅法院披露依据不是已核验、可生产使用的程序法源")
            if len(active_g2_records) != 1:
                errors.append(f"证据 {evidence.get('id')} 进入当前提交集但没有有效 G2 批准")
            else:
                version, digest = object_version_hash(evidence)
                exact_evidence = any(
                    snapshot.get("object_id") == evidence.get("id")
                    and snapshot.get("version") == version
                    and snapshot.get("hash") == digest
                    for snapshot in g2_scopes
                )
                if not exact_evidence:
                    errors.append(f"证据 {evidence.get('id')} 未被有效G2按当前版本和哈希精确绑定")
                for locator in evidence.get("source_refs", []):
                    source = source_by_id.get(locator.get("source_id"))
                    if source is None:
                        errors.append(f"证据 {evidence.get('id')} 引用了不存在的来源 {locator.get('source_id')}")
                        continue
                    exact_source = any(
                        snapshot.get("object_id") == source.get("id")
                        and snapshot.get("version") == source.get("version")
                        and snapshot.get("hash") == source.get("sha256")
                        for snapshot in g2_scopes
                    )
                    if not exact_source:
                        errors.append(f"证据 {evidence.get('id')} 的来源 {source.get('id')} 未被有效G2按版本和哈希精确绑定")

    authority_by_id = {item.get("id"): item for item in state.get("authorities", [])}
    for authority in state.get("authorities", []):
        if authority.get("production_eligible") and _authority_is_test_only(authority, environment):
            errors.append(
                f"法源 {authority.get('id')} 属于 TEST-ONLY/非生产环境，不能 production_eligible=true"
            )
        if authority.get("production_eligible") and (
            authority.get("source_level") != "L1_verified_authority"
            or authority.get("verification_status") != "verified"
        ):
            errors.append(
                f"法源 {authority.get('id')} 只有 L1_verified_authority 且 verified 才能 production_eligible=true"
            )
    composition_by_id = {item.get("id"): item for item in state.get("composition_specs", [])}
    approval_by_id = {item.get("id"): item for item in state.get("approvals", [])}
    template_by_id = {item.get("id"): item for item in state.get("templates", [])}
    for spec in state.get("composition_specs", []):
        label = f"合成清单 {spec.get('id')}"
        if spec.get("matter_id") not in {None, state.get("matter", {}).get("id")}:
            errors.append(f"{label} 绑定了其他案件 {spec.get('matter_id')}")
        roles = spec.get("template_roles", {})
        role_ids = [roles.get("layout_template_id"), roles.get("structure_template_id")]
        role_ids.extend(item.get("template_id") for item in roles.get("auxiliary_templates", []))
        for template_id in (item for item in role_ids if item):
            require_reference(label, "template_roles", template_id, {"templates"})
        trust = spec.get("trusted_binding", {})
        spec_is_stale = spec.get("stale") is True or spec.get("status") == "stale"
        if spec.get("test_mode") is True and spec.get("status") == "ready":
            errors.append(f"{label} 是TEST-ONLY清单却标为ready")
        if spec.get("status") == "ready" and trust.get("mode") != "catalog_case_state":
            errors.append(f"{label} 标为ready但未绑定个人模板目录与案件批准")
        if trust.get("mode") == "catalog_case_state":
            if trust.get("matter_id") != state.get("matter", {}).get("id"):
                errors.append(f"{label} 可信绑定指向其他案件")
            gate_bindings = trust.get("approval_bindings", [])
            if {item.get("gate") for item in gate_bindings} != {"G1_strategy", "G2_evidence"}:
                errors.append(f"{label} 可信绑定缺少完整G1/G2批准")
            for gate_binding in gate_bindings:
                approval = approval_by_id.get(gate_binding.get("approval_id"))
                if (
                    approval is None
                    or approval.get("gate") != gate_binding.get("gate")
                    or approval.get("decision") != "approved"
                    or (not spec_is_stale and approval.get("status") != "active")
                    or approval.get("object_id") != gate_binding.get("object_id")
                    or approval.get("object_hash") != gate_binding.get("object_hash")
                    or approval.get("scope_hash") != gate_binding.get("scope_hash")
                ):
                    errors.append(f"{label} 可信批准绑定已变化：{gate_binding.get('approval_id')}")
            qualification_by_id = {
                item.get("template_id"): item for item in spec.get("template_qualifications", [])
            }
            for template_binding in trust.get("template_bindings", []):
                template_id = template_binding.get("template_id")
                template = template_by_id.get(template_id)
                qualification = qualification_by_id.get(template_id, {})
                if template is None:
                    errors.append(f"{label} 缺少案件内模板快照：{template_id}")
                    continue
                if (
                    (not spec_is_stale and (
                        template.get("sha256") != template_binding.get("template_sha256")
                        or template.get("profile_sha256") != template_binding.get("profile_sha256")
                    ))
                    or qualification.get("template_hash") != template_binding.get("template_sha256")
                    or qualification.get("profile", {}).get("hash") != template_binding.get("profile_sha256")
                    or qualification.get("profile", {}).get("fingerprint_manifest_hash")
                    != template_binding.get("fingerprint_manifest_sha256")
                ):
                    errors.append(f"{label} 的模板/画像/指纹快照已变化：{template_id}")
        for dependency in spec.get("dependencies", []):
            object_id = dependency.get("object_id")
            # Profiles live in the immutable template library and are bound by
            # their own version/hash; all case objects must resolve here.
            if dependency.get("object_type") != "profile":
                require_reference(label, "dependencies", object_id)
        for authority_id in spec.get("authority_ids", []):
            require_reference(label, "authority_ids", authority_id, {"authorities"})
        for binding in spec.get("claim_bindings", []):
            for source_id in binding.get("source_ids", []):
                require_reference(f"{label}/{binding.get('id')}", "source_ids", source_id, {"sources"})
            for evidence_id in binding.get("evidence_ids", []):
                require_reference(f"{label}/{binding.get('id')}", "evidence_ids", evidence_id, {"evidence"})
            for authority_id in binding.get("authority_ids", []):
                require_reference(f"{label}/{binding.get('id')}", "authority_ids", authority_id, {"authorities"})
            if binding.get("court_candidate") and binding.get("verification_status") != "verified":
                errors.append(f"{label} 的法院命题 {binding.get('id')} 尚未 verified")
        if spec.get("status") == "ready":
            if spec.get("stale") or spec.get("blockers"):
                errors.append(f"{label} 标为 ready 但仍失效或含阻断项")
            if spec.get("unresolved_template_refs") or spec.get("ambiguous_template_refs"):
                errors.append(f"{label} 标为 ready 但仍有未解析或歧义模板")
            for authority_id in spec.get("authority_ids", []):
                authority = authority_by_id.get(authority_id)
                if spec.get("audience") == "court_candidate" and (
                    authority is None
                    or _authority_is_test_only(authority, environment)
                    or authority.get("verification_status") != "verified"
                    or not authority.get("production_eligible")
                ):
                    errors.append(f"{label} 的正式法源不满足生产门禁：{authority_id}")
        for artifact_id in spec.get("dependent_artifact_ids", []):
            require_reference(label, "dependent_artifact_ids", artifact_id, {"artifacts"})
    for artifact in state.get("artifacts", []):
        composition_spec_id = artifact.get("composition_spec_id")
        if composition_spec_id is not None:
            require_reference(f"文书 {artifact.get('id')}", "composition_spec_id", composition_spec_id, {"composition_specs"})
            linked_spec = composition_by_id.get(composition_spec_id)
            if artifact.get("audience") == "court_candidate" and not artifact.get("stale") and artifact.get("status") != "stale" and linked_spec and (
                linked_spec.get("status") != "ready" or linked_spec.get("stale") or linked_spec.get("blockers")
            ):
                errors.append(f"法院候选 {artifact.get('id')} 绑定的合成清单未就绪或已失效")
        if artifact.get("audience") == "court_candidate" and not artifact.get("stale"):
            if artifact.get("review_status") != "passed":
                errors.append(f"法院候选 {artifact.get('id')} 未通过独立审阅")
            for snapshot in artifact.get("input_snapshot", []):
                authority = authority_by_id.get(snapshot.get("object_id"))
                if authority and (
                    _authority_is_test_only(authority, environment)
                    or
                    authority.get("verification_status") != "verified"
                    or not authority.get("production_eligible")
                ):
                    errors.append(f"法院候选 {artifact.get('id')} 引用了未核验或 TEST-ONLY 法源 {authority.get('id')}")

    approvals = {item.get("id"): item for item in state.get("approvals", [])}
    for action in state.get("external_actions", []):
        approval = approvals.get(action.get("approval_id"))
        if not approval or approval.get("gate") != "G5_external_action" or approval.get("status") != "active":
            errors.append(f"外部动作 {action.get('id')} 没有绑定有效 G5 授权")

    for template in state.get("templates", []):
        if template.get("status") == "active" and template.get("license", {}).get("status") != "verified":
            errors.append(f"模板 {template.get('id')} 许可未核验却标为 active")
        if template.get("stale") is True and template.get("status") == "active":
            errors.append(f"模板 {template.get('id')} 已失效却仍标为 active")
        if template.get("stale") is True and not template.get("stale_reason"):
            errors.append(f"模板 {template.get('id')} 已失效但未记录原因")

    for approval in state.get("approvals", []):
        actual_scope_hash = sha256_bytes(canonical_json(approval.get("scope_snapshot", [])).encode("utf-8"))
        if approval.get("scope_hash") != actual_scope_hash:
            errors.append(f"批准 {approval.get('id')} 的 scope_snapshot 与 scope_hash 不一致，范围可能被拼接或改写")
        if approval.get("status") != "active" or approval.get("decision") != "approved":
            continue
        if approval.get("gate") in {
            "G1_strategy",
            "G2_evidence",
            "G3_draft_plan",
            "G4_final",
            "G5_external_action",
            "SANITIZE",
            "MEMORY_COMMIT",
            "WORKFLOW_ACTION",
        } and not approval.get("scope_snapshot"):
            errors.append(f"有效批准 {approval.get('id')} 的 {approval.get('gate')} scope_snapshot 不能为空")
        _collection, target = find_object(state, approval.get("object_id", ""))
        if target is None:
            errors.append(f"有效批准 {approval.get('id')} 绑定的对象不存在：{approval.get('object_id')}")
        else:
            target_version, target_hash = object_version_hash(target)
            if target_version != approval.get("object_version") or target_hash != approval.get("object_hash"):
                errors.append(f"有效批准 {approval.get('id')} 的对象版本或哈希与当前对象不一致")
            target_collection = identifiers.get(approval.get("object_id"))
            if approval.get("gate") == "MEMORY_COMMIT" and target_collection not in {
                "case_events",
                "triage_cards",
                "deadline_records",
                "impact_assessments",
                "prospective_matter_seeds",
            }:
                errors.append(
                    f"批准 {approval.get('id')} 的 MEMORY_COMMIT 只能绑定案件记忆候选对象，实际为 {target_collection}"
                )
            if approval.get("gate") == "WORKFLOW_ACTION" and target_collection not in {
                "workflow_instances",
                "tasks",
            }:
                errors.append(
                    f"批准 {approval.get('id')} 的 WORKFLOW_ACTION 只能绑定内部工单或任务，实际为 {target_collection}"
                )
        for snapshot in approval.get("scope_snapshot", []):
            _scope_collection, scoped = find_object(state, snapshot.get("object_id", ""))
            if scoped is None:
                errors.append(f"有效批准 {approval.get('id')} 的范围对象不存在：{snapshot.get('object_id')}")
                continue
            scope_version, scope_hash = object_version_hash(scoped)
            if scope_version != snapshot.get("version") or scope_hash != snapshot.get("hash"):
                errors.append(f"有效批准 {approval.get('id')} 的范围对象已变化：{snapshot.get('object_id')}")

    focus = state.get("focus", {})
    approval_ids = {item.get("id") for item in state.get("approvals", [])}
    for approval_id in focus.get("pending_approval_ids", []):
        if approval_id not in approval_ids:
            errors.append(f"FocusContext 引用了不存在的批准对象 {approval_id}")

    if state_path is not None:
        require_canonical_state_path(state_path)
        workspace_root = state_path.resolve().parents[1]
        run_root = (state_path.resolve().parent / "memory" / "runs").resolve()
        for run in state.get("run_attempts", []):
            output_log_path = run.get("output_log_path")
            if not output_log_path:
                continue
            candidate_path = Path(str(output_log_path))
            resolved_log = (
                candidate_path.resolve()
                if candidate_path.is_absolute()
                else (workspace_root / candidate_path).resolve()
            )
            try:
                resolved_log.relative_to(run_root)
            except ValueError:
                errors.append(
                    f"运行尝试 {run.get('id')} 的 output_log_path 必须位于本案 _case-state/memory/runs"
                )
        audit_path = state_path.parent / "audit-log.jsonl"
        cursor = state.get("audit", {})
        if cursor.get("last_sequence", 0) > 0:
            if not audit_path.exists():
                errors.append("状态指向审计事件，但 audit-log.jsonl 不存在")
            else:
                lines = [line for line in audit_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
                if not lines:
                    errors.append("状态指向审计事件，但 audit-log.jsonl 为空")
                else:
                    events: list[dict[str, Any]] = []
                    previous: dict[str, Any] | None = None
                    for index, line in enumerate(lines, start=1):
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            errors.append(f"audit-log.jsonl 第 {index} 行不是合法 JSON")
                            continue
                        events.append(event)
                        if event.get("sequence") != index:
                            errors.append(f"审计日志序号不连续：第 {index} 行记录为 {event.get('sequence')}")
                        supplied_hash = event.get("event_hash")
                        unsigned = copy.deepcopy(event)
                        unsigned.pop("event_hash", None)
                        expected_hash = sha256_bytes(canonical_json(unsigned).encode("utf-8"))
                        if supplied_hash != expected_hash:
                            errors.append(f"审计事件 {event.get('event_id')} 的 event_hash 不匹配")
                        if previous is not None:
                            if event.get("previous_event_hash") != previous.get("event_hash"):
                                errors.append(f"审计事件 {event.get('event_id')} 的 previous_event_hash 断链")
                            if event.get("before_state_hash") != previous.get("after_state_hash"):
                                errors.append(f"审计事件 {event.get('event_id')} 的状态哈希链不连续")
                        previous = event
                    if events:
                        last = events[-1]
                        if last.get("sequence") != cursor.get("last_sequence"):
                            errors.append("审计日志末序号与状态游标不一致")
                        if last.get("event_hash") != cursor.get("last_event_hash"):
                            errors.append("审计日志末哈希与状态游标不一致")
                        if last.get("after_state_hash") != state_content_hash(state):
                            errors.append("审计日志末 after_state_hash 与当前 case-state 内容不一致，状态可能绕过事务循环被改写")
                        for approval in state.get("approvals", []):
                            if approval.get("status") != "active" or approval.get("decision") != "approved":
                                continue
                            receipts = [
                                event for event in events
                                if event.get("event_type") == "approval_granted"
                                and approval.get("id") in event.get("object_ids", [])
                            ]
                            if len(receipts) != 1:
                                errors.append(f"有效批准 {approval.get('id')} 缺少唯一的追加式 approval_granted 审计收据")
                                continue
                            details = receipts[0].get("details", {})
                            if (
                                details.get("gate") != approval.get("gate")
                                or details.get("hash") != approval.get("object_hash")
                                or details.get("scope_hash") != approval.get("scope_hash")
                            ):
                                errors.append(f"有效批准 {approval.get('id')} 与追加式审计收据的对象/范围摘要不一致")
        elif any(
            approval.get("status") == "active" and approval.get("decision") == "approved"
            for approval in state.get("approvals", [])
        ):
            errors.append("存在有效批准但审计游标为0；active批准必须有追加式 approval_granted 审计收据")
    return errors


def validate_state_file(state_path: Path, schema_path: Path = DEFAULT_SCHEMA) -> dict[str, Any]:
    require_canonical_state_path(state_path)
    state = load_json(state_path)
    schema = load_json(schema_path)
    schema_errors = validate_against_schema(state, schema)
    semantic_errors = validate_semantics(state, state_path)
    errors = schema_errors + semantic_errors
    return {
        "ok": not errors,
        "state": str(state_path),
        "schema": str(schema_path),
        "schema_engine": "builtin-draft-2020-12-subset",
        "state_hash": state_content_hash(state),
        "errors": errors,
        "warnings": [
            "内置校验器只实现本项目使用的 JSON Schema 关键字；完整 Draft 2020-12 合规验证可在有依赖时另行执行。"
        ],
    }


def find_object(state: dict[str, Any], object_id: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    if state.get("matter", {}).get("id") == object_id:
        return "matter", state["matter"]
    for collection, item in _all_objects(state):
        if item.get("id") == object_id:
            return collection, item
    return None, None


def object_version_hash(item: dict[str, Any]) -> tuple[int, str | None]:
    version = int(item.get("version", 1))
    # File-backed objects bind their independently calculated file hash.
    if item.get("sha256"):
        return version, item["sha256"]
    # Package identity is the exact ordered item list, never a trusted cached field.
    if "manifest_hash" in item:
        return version, sha256_bytes(canonical_json(item.get("items", [])).encode("utf-8"))
    # Never trust a self-declared content_hash: recompute from substantive fields.
    payload = copy.deepcopy(item)
    payload.pop("content_hash", None)
    payload.pop("hash", None)
    return version, sha256_bytes(canonical_json(payload).encode("utf-8"))
