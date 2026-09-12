"""Natural-language control plane for Legal Case OS.

This module is intentionally deterministic and side-effect free.  It compiles a
user utterance into an auditable TaskFrame, a one-question clarification gate,
and an executable RunSpec.  It does not read or write canonical matter state.
"""

from __future__ import annotations

import re
import uuid
from typing import Any


LANGUAGE_SCHEMA_VERSION = "1.0.0"
PARSER_VERSION = "legal-language-control-1.3.0"


_READ_ONLY_HEAD = (
    r"(?:分析|评估|研究|讨论|探讨|审阅|审查|核对|复核|核验|检查|查验|校验|"
    r"梳理|盘点|列出|识别|提取|总结|概括|查看|查询|判断|说明|解释|计算)(?:一下|下)?"
)
_ACTION_SWITCH = (
    r"(?:(?:完(?:成)?|好)?后(?!续)|之后|然后|接着|随即|再(?!次|审)|并且|"
    r"(?<!合)并(?!列|行|非|未|不))"
)
_EXTERNAL_ACTION_VERB = (
    r"(?:发送(?:给|至)?|发送出去|转发|抄送(?:给)?|群发|发给|发至|发过去给|发出去|"
    r"寄(?:送)?(?:给|至)?|寄出去|邮寄(?:给|到)?|递交(?:给|到)?|提交(?:给|到)?|"
    r"呈交|投递|报送(?:给|至|到)?|上传(?:到)?|递给|传(?:给|到)?|交(?:给|到)?|"
    r"打印|印一份|删除|删掉|清空|覆盖|立案|撤诉|申请(?:财产)?保全|送达|发|交|送|传)"
)


_EVIDENCE_CUES = (
    "材料写", "材料中", "证据写", "证据中", "邮件写", "邮件中", "合同写", "合同中",
    "原文", "引文", "引用", "对方说", "对方称", "载明", "内容是", "内容为", "记录显示",
)
_PARAMETER_PREFIX = re.compile(
    r"(?:按照|依照|参照|参考|按|只看|仅看|只分析|仅分析|只研究|仅研究|修改|检索|搜索|我说)\s*$"
)
_PARAMETER_SUFFIX = re.compile(r"^\s*(?:模板|套件|文件|路径|目录|第\s*\d+\s*条|条款)")
_PATH_LIKE = re.compile(r"(?:^[A-Za-z]:[\\/]|[/\\]|\.[A-Za-z0-9]{1,8}$)")
_CHAINED_PARAMETER_PREFIX = re.compile(
    r"(?:按照|依照|参照|参考|按)\s*"
    r"(?:[‘“'\"][^，,。；;：:\r\n]{1,100}?[’”'\"]\s*(?:和|及|与|、|/|／|，|,)\s*)+$"
)
_CHAINED_TEMPLATE_SUFFIX = re.compile(
    r"^\s*(?:"
    r"(?:和|及|与|、|/|／|，|,)\s*[‘“'\"][^，,。；;：:\r\n]{1,100}?[’”'\"]\s*"
    r")*(?:的)?(?:模板|套件|范例|风格|写法|版式|结构)"
)


def _quote_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    for opener, closer in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")):
        pattern = re.escape(opener) + r"([^" + re.escape(closer) + r"]*)" + re.escape(closer)
        for match in re.finditer(pattern, text):
            ranges.append((match.start(), match.end(), match.group(1)))
    ranges.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str]] = []
    last_end = -1
    for item in ranges:
        if item[0] >= last_end:
            selected.append(item)
            last_end = item[1]
    return selected


def _quote_source_kind(text: str, start: int, end: int, inner: str) -> str:
    prefix = text[max(0, start - 28):start]
    suffix = text[end:min(len(text), end + 18)]
    if _PATH_LIKE.search(inner.strip()) or _PARAMETER_PREFIX.search(prefix) or _PARAMETER_SUFFIX.search(suffix):
        return "user_parameter"
    # In an explicit multi-template expression, each separately quoted item is
    # a parameter.  The first item is covered by _PARAMETER_PREFIX; recognize
    # later items only when both the preceding quote chain and the following
    # template/style noun are present.  This keeps unrelated quoted evidence
    # on the data plane.
    chained_prefix = text[max(0, start - 256):start]
    chained_suffix = text[end:min(len(text), end + 256)]
    if (
        _CHAINED_PARAMETER_PREFIX.search(chained_prefix)
        and _CHAINED_TEMPLATE_SUFFIX.search(chained_suffix)
    ):
        return "user_parameter"
    if any(cue in prefix for cue in _EVIDENCE_CUES):
        return "quoted_data"
    # Quotation marks are a data-plane boundary by default.  Parameters are
    # promoted only by an explicit grammatical cue above.
    return "quoted_data"


def segment_input(text: str) -> list[dict[str, Any]]:
    """Split current user input into command, parameter, and quoted-data spans."""
    segments: list[dict[str, Any]] = []
    cursor = 0
    index = 1
    for start, end, inner in _quote_ranges(text):
        if start > cursor:
            segments.append({
                "id": f"SEG-{index:03d}",
                "start": cursor,
                "end": start,
                "text": text[cursor:start],
                "source_kind": "user_instruction",
                "trust_level": "current_command",
                "command_eligible": True,
            })
            index += 1
        source_kind = _quote_source_kind(text, start, end, inner)
        segments.append({
            "id": f"SEG-{index:03d}",
            "start": start,
            "end": end,
            "text": text[start:end],
            "source_kind": source_kind,
            "trust_level": "current_parameter" if source_kind == "user_parameter" else "untrusted_data",
            "command_eligible": source_kind == "user_parameter",
        })
        index += 1
        cursor = end
    if cursor < len(text) or not segments:
        segments.append({
            "id": f"SEG-{index:03d}",
            "start": cursor,
            "end": len(text),
            "text": text[cursor:],
            "source_kind": "user_instruction",
            "trust_level": "current_command",
            "command_eligible": True,
        })
    # Bare parameters occupy the same data plane as quoted parameters.  This
    # covers common Chinese forms such as “按 X 模板” and “模板名是 X 模板”,
    # plus unquoted file paths in a “只看 X.pdf” scope.
    bare_parameter_ranges: list[tuple[int, int]] = []
    bare_parameter_patterns = (
        re.compile(r"(?:按照|依照|参照|参考|按)\s*([^，,。；;\r\n]{1,80}?)(?=模板(?:，|,|生成|起草|写|出|$))"),
        re.compile(r"(?:模板名|模板名称)(?:为|是|叫)\s*([^，,。；;\r\n]{1,80}?模板)(?=[，,。；;\r\n]|$)"),
        re.compile(
            r"(?:只看|仅看|只允许(?:看|读取)|只能(?:看|读取)|只许(?:看|读取)?|只准(?:看|读取)?)"
            r"\s*([^，,。；;\r\n]{1,120}?\.[A-Za-z0-9]{1,8})(?=[，,。；;\r\n]|$)"
        ),
    )
    instruction_ranges = [
        (int(segment["start"]), int(segment["end"]))
        for segment in segments if segment["source_kind"] == "user_instruction"
    ]
    for pattern in bare_parameter_patterns:
        for match in pattern.finditer(text):
            candidate = (match.start(1), match.end(1))
            if any(start <= candidate[0] and candidate[1] <= end for start, end in instruction_ranges):
                bare_parameter_ranges.append(candidate)
    if bare_parameter_ranges:
        bare_parameter_ranges = sorted(set(bare_parameter_ranges))
        parameterized: list[dict[str, Any]] = []
        for segment in segments:
            if segment["source_kind"] != "user_instruction":
                parameterized.append(segment)
                continue
            cursor = int(segment["start"])
            end = int(segment["end"])
            for range_start, range_end in bare_parameter_ranges:
                if range_start < cursor or range_end > end:
                    continue
                if range_start > cursor:
                    parameterized.append({**segment, "start": cursor, "end": range_start, "text": text[cursor:range_start]})
                parameterized.append({
                    **segment,
                    "start": range_start,
                    "end": range_end,
                    "text": text[range_start:range_end],
                    "source_kind": "user_parameter",
                    "trust_level": "current_parameter",
                    "command_eligible": True,
                })
                cursor = range_end
            if cursor < end:
                parameterized.append({**segment, "start": cursor, "end": end, "text": text[cursor:end]})
        segments = parameterized
    # A source introduction without quotation marks is still data.  Preserve
    # the surrounding instruction, but mask the introduced sentence from the
    # action classifier (for example: 邮件内容为：请提交。分析一下).
    refined: list[dict[str, Any]] = []
    # Two grammatical forms introduce unquoted data.  Document-content
    # introductions normally run to the end of the sentence, while reported
    # speech/requests stop at a comma too, so a following “分析一下” remains a
    # live instruction.  The introduced span (including the introductory
    # words) is data-plane only.
    data_intros: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(
                r"(?:邮件|材料|证据|合同|通知|聊天记录|原文|诉状|答辩状|文书|判决书|裁定书|"
                r"客户邮件)(?:中|的)?(?:(?:内容)?(?:为|是(?!否|哪|什么|谁|怎么|如何|何时|什么时候)|"
                r"写着|载明|如下|写道|中写|中写着)"
                r"\s*[：:]?|内容\s*[：:])"
            ),
            "document",
        ),
        (
            re.compile(
                r"(?:对方|客户|当事人|原告|被告|证人|客户来信|来信)"
                r"(?:说|称|表示|要求|主张|写道|写着|载明)\s*[：:]?"
            ),
            "reported",
        ),
        (
            re.compile(
                r"(?:(?:以下)?(?:案件)?事实|案情|问题背景)"
                r"(?:(?:为|是|如下)\s*[：:]?|\s*[：:])"
            ),
            "reported",
        ),
    )
    readonly_resume = re.compile(
        r"\s*(?:(?:请你|请|帮我|你来|现在请|接下来请|然后请|再请|然后|再)\s*)?"
        r"(?:分析|评估|讨论|探讨|解释|判断|总结|概括|看看|研究)(?:一下|下)?"
    )
    unsafe_resume = re.compile(
        r"(?:生成|起草|写一份|写一版|提交|发送|发给|上传|删除|打印|立案|撤诉|"
        r"正式记入|写入案件|调用记忆)"
    )

    def introduced_data_end(payload_start: int, span_end: int, mode: str) -> int:
        """Find an explicit, read-only switch back to the user's instruction."""
        tail = text[payload_start:span_end]
        boundaries = r"[，,。\r\n]" if mode == "reported" else r"[。\r\n]"
        for boundary in re.finditer(boundaries, tail):
            suffix = tail[boundary.end():]
            if readonly_resume.match(suffix) and not unsafe_resume.search(suffix):
                return payload_start + boundary.end()
        # Ambiguous unquoted material remains data through the end of the span.
        # This deliberately trades some convenience for command-injection safety.
        return span_end
    for segment in segments:
        if segment["source_kind"] != "user_instruction":
            refined.append(segment)
            continue
        start, end = segment["start"], segment["end"]
        cursor = start
        intro_matches: list[tuple[int, int, re.Match[str], str]] = []
        for intro_pattern, mode in data_intros:
            intro_matches.extend(
                (match.start(), match.end(), match, mode)
                for match in intro_pattern.finditer(text, start, end)
            )
        intro_matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        for _, _, match, mode in intro_matches:
            if match.start() < cursor:
                continue
            if match.start() > cursor:
                refined.append({**segment, "start": cursor, "end": match.start(), "text": text[cursor:match.start()]})
            data_end = introduced_data_end(match.end(), end, mode)
            refined.append({
                **segment,
                "start": match.start(),
                "end": data_end,
                "text": text[match.start():data_end],
                "source_kind": "unquoted_data",
                "trust_level": "untrusted_data",
                "command_eligible": False,
            })
            cursor = data_end
        if cursor < end:
            refined.append({**segment, "start": cursor, "end": end, "text": text[cursor:end]})
    for index, segment in enumerate(refined, start=1):
        segment["id"] = f"SEG-{index:03d}"
    return refined


def _command_text(text: str, segments: list[dict[str, Any]]) -> str:
    chars = list(text)
    for segment in segments:
        if segment["command_eligible"]:
            continue
        for index in range(segment["start"], segment["end"]):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return "".join(chars)


def _action_text(text: str, segments: list[dict[str, Any]]) -> str:
    """Return only direct instruction spans; parameters cannot become verbs."""
    chars = list(text)
    for segment in segments:
        if segment["source_kind"] == "user_instruction":
            continue
        for index in range(segment["start"], segment["end"]):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return "".join(chars)


def _first_span(text: str, patterns: tuple[str, ...]) -> dict[str, Any] | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return {
                "start": match.start(),
                "end": match.end(),
                "text": match.group(0),
                "source_kind": "user_instruction",
                "trust_level": "explicit_current_command",
            }
    return None


def _first_nonnegated_span(text: str, patterns: tuple[str, ...]) -> dict[str, Any] | None:
    """Return the first style span that is not governed by a nearby negator."""
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            prefix = text[max(0, match.start() - 14):match.start()]
            if re.search(
                r"(?:不要|别|不再|无需|无须|不必|不用|不需要|不是|并非)(?:再)?"
                r"(?:写得?|写|说得?|用|采用|保持|表达得?)?(?:太|那么)?\s*$",
                prefix,
            ):
                continue
            return {
                "start": match.start(),
                "end": match.end(),
                "text": match.group(0),
                "source_kind": "user_instruction",
                "trust_level": "explicit_current_command",
            }
    return None


def _all_nonnegated_spans(text: str, patterns: tuple[str, ...]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            prefix = text[max(0, match.start() - 14):match.start()]
            if re.search(
                r"(?:不要|别|不再|无需|无须|不必|不用|不需要|不是|并非)(?:再)?"
                r"(?:写得?|写|说得?|用|采用|保持|表达得?)?(?:太|那么)?\s*$",
                prefix,
            ):
                continue
            spans.append({
                "start": match.start(), "end": match.end(), "text": match.group(0),
                "source_kind": "user_instruction", "trust_level": "explicit_current_command",
            })
    spans.sort(key=lambda item: (int(item["start"]), int(item["end"])))
    return spans


def _correction_boundary(text: str) -> int | None:
    """Return the start of the final explicit self-correction clause."""
    boundaries: list[int] = []
    for marker in re.finditer(r"(?:改成|改为|改做|算了)", text):
        boundaries.append(marker.end())
    for marker in re.finditer(
        r"(?:不是|并非)[^，,。；;]{0,50}[，,]\s*(?:而?是)",
        text,
    ):
        boundaries.append(marker.end())
    return max(boundaries) if boundaries else None


def _explicit_issue_topic_span(text: str) -> dict[str, Any] | None:
    for match in re.finditer(r"(?:分析|研究|讨论|探讨|评估)([^，,。；;！？!?]*)", text):
        raw_tail = match.group(1)
        tail = raw_tail.strip()
        tail = re.sub(r"^(?:一下|下)", "", tail).strip()
        tail = re.sub(r"^(?:一个)?(?:新)?问题\s*[：:]?", "", tail).strip()
        if not tail or tail in {"这个", "本案", "现有材料", "当前材料", "全部材料", "所有材料"}:
            continue
        start = match.start(1) + (len(raw_tail) - len(raw_tail.lstrip()))
        return {
            "start": start, "end": match.end(), "text": tail,
            "source_kind": "user_instruction", "trust_level": "explicit_current_command",
        }
    return None


def _alias_definition_span(text: str) -> dict[str, Any] | None:
    """Locate an alias definition whose right-hand side is data, not an action."""
    match = re.search(
        r"(?:以后|今后).{0,12}(?:我说|说)\s*[“‘\"']?"
        r"([^“”‘’\"'，。]{1,20})[”’\"']?\s*(?:就是|表示|等于)\s*([^，。；]{1,30})",
        text,
    )
    if not match:
        return None
    return {
        "start": match.start(),
        "end": match.end(),
        "text": match.group(0),
        "source_kind": "user_instruction",
        "trust_level": "explicit_current_command",
    }


def _extract_template_refs(text: str) -> list[str]:
    """Extract explicit template/style references without treating them as verbs.

    The legacy router exposes only one ``template_alias``.  v1.2 keeps that
    field but also preserves ordered multi-template expressions such as
    “参考8073和8075的风格”.  Approximate matching is intentionally deferred to
    the registered template catalog.
    """

    refs: list[str] = []
    boundary_quotes = "‘’“”'\"「」『』《》〈〉"

    def add_raw(raw_value: str) -> None:
        raw_value = re.sub(r"^(?:这个|该|上述|前述)", "", raw_value.strip()).strip()
        for item in re.split(r"\s*(?:和|及|与|、|/|／|，|,)\s*", raw_value):
            part = item.strip().strip(boundary_quotes).strip()
            part = re.sub(
                r"(?:的)?(?:模板|套件|范例|风格|写法|版式|结构)$",
                "",
                part,
            ).strip().strip(boundary_quotes).strip()
            if part and part not in refs:
                refs.append(part)

    # Capture the whole reference clause first.  A marker may follow every
    # quoted item (A模板和B模板), not only the entire list (A和B的模板).
    # The older single-regex path below would otherwise stop at the first one.
    quoted_token_pattern = (
        r"(?:“[^”]+”|‘[^’]+’|\"[^\"]+\"|'[^']+'|"
        r"「[^」]+」|『[^』]+』|《[^》]+》|〈[^〉]+〉)"
    )
    quoted_item = re.compile(
        r"“([^”]+)”|‘([^’]+)’|\"([^\"]+)\"|'([^']+)'|"
        r"「([^」]+)」|『([^』]+)』|《([^》]+)》|〈([^〉]+)〉"
    )
    quoted_sequence = re.compile(
        rf"(?:按照|依照|参照|参考|按)\s*(?P<sequence>"
        rf"{quoted_token_pattern}(?:\s*(?:模板|套件|范例|风格|写法|版式|结构)?"
        rf"\s*(?:和|及|与|、|/|／|，|,)\s*{quoted_token_pattern})*"
        rf"\s*(?:的)?(?:模板|套件|范例|风格|写法|版式|结构))"
    )
    covered_reference_spans: list[tuple[int, int]] = []
    for sequence_match in quoted_sequence.finditer(text):
        covered_reference_spans.append(sequence_match.span())
        clause = sequence_match.group("sequence")
        quoted_values = [
            next(group for group in match.groups() if group is not None)
            for match in quoted_item.finditer(clause)
        ]
        for value in quoted_values:
            add_raw(value)

    patterns = (
        re.compile(
            r"(?:按照|依照|参照|参考|按)\s*[‘’“”'\"]?"
            r"([^，,。；;：:\r\n]{1,100}?)[‘’“”'\"]?"
            r"(?:的)?(?:模板|套件|范例|风格|写法|版式|结构)"
        ),
        re.compile(
            r"(?:模板|范例)(?:编号|名称|名)?(?:为|是|叫)\s*[‘’“”'\"]?"
            r"([^，,。；;：:\r\n]{1,100}?)[‘’“”'\"]?(?=[，,。；;]|$)"
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(
                match.start() < covered_end and covered_start < match.end()
                for covered_start, covered_end in covered_reference_spans
            ):
                continue
            raw = match.group(1).strip()
            add_raw(raw)
    return refs


def _mask_span(text: str, span: dict[str, Any] | None) -> str:
    if not span:
        return text
    chars = list(text)
    for index in range(int(span["start"]), int(span["end"])):
        if chars[index] not in "\r\n":
            chars[index] = " "
    return "".join(chars)


def _marker_describes_action_object(prefix: str, marker: re.Match[str]) -> bool:
    """Distinguish an object's state from the requested action's state."""
    last_disposal = max(prefix.rfind("把"), prefix.rfind("将"))
    if last_disposal > marker.start():
        return False
    tail = prefix[marker.end():].strip()
    return bool(tail and re.search(
        r"(?:的)?(?:材料|文件|文书|方案|报告|邮件|起诉状|答辩状|上诉状|申请书|"
        r"证据|清单|记录|附件)(?:立即|马上|直接)?$",
        tail,
    ))


def _verb_is_read_only_reference(text: str, verb_start: int) -> bool:
    """Return true when an action-shaped word is the object of read-only analysis.

    Chinese legal nouns often contain verbs (for example ``送达期限`` or
    ``提交记录``).  A preceding inspection predicate keeps such a word on the
    data plane unless an explicit sequence marker switches back to execution.
    """
    clause_start = max(
        text.rfind(mark, 0, verb_start) for mark in ("，", ",", "。", "；", ";", "!", "?", "！", "？")
    ) + 1
    clause_end_candidates = [
        position for mark in ("，", ",", "。", "；", ";", "!", "?", "！", "？")
        if (position := text.find(mark, verb_start + 1)) >= 0
    ]
    clause_end = min(clause_end_candidates, default=len(text))
    prefix = text[clause_start:verb_start]
    suffix = text[verb_start:clause_end]
    question_markers = list(re.finditer(
        r"(?:是否|可否|能否|要不要|应不应该|有没有|能不能|可不可以|该不该|应否|"
        r"是不是|需不需要|谁|何人|哪(?:一)?方|什么时候|何时|为什么|为何|怎么|如何)",
        prefix,
    ))
    if question_markers and re.search(_ACTION_SWITCH, prefix[question_markers[-1].end():]) is None:
        return True
    if re.search(r"(?:了)?吗\s*$|(?:有没有|没有|没)\s*$", suffix):
        return True
    if re.search(
        r"的(?:材料|文件|文书|清单|版本|记录|状态|效力|必要性|风险|后果|影响|流程)"
        r"(?:是否|有无|有哪些|是否有效|如何|怎么|是什么)?",
        suffix,
    ):
        return True
    if re.search(
        r"的[^，,。；;!?！？]{0,20}(?:在哪里|是哪(?:一?(?:个|份|张|天))?|是什么|有哪些|"
        r"多少|何时|哪天|如何|怎么样|怎么)\s*$",
        suffix,
    ):
        return True
    if re.search(
        r"(?:的)?是?哪(?:一)?(?:个|份|封|条|批|件|张)?[^，,。；;!?！？]{0,12}\s*$",
        suffix,
    ):
        return True
    state_markers = list(re.finditer(r"(?:已经|曾经|准备|计划|尚未|还没|没有|不再|拟|待|已|未)", prefix))
    for state_marker in reversed(state_markers):
        if _marker_describes_action_object(prefix, state_marker):
            continue
        if re.search(_ACTION_SWITCH, prefix[state_marker.end():]) is None:
            return True
    heads = list(re.finditer(_READ_ONLY_HEAD, prefix))
    if not heads:
        return False
    last_head = heads[-1]
    # “把分析报告提交” contains an analysis-shaped noun inside the disposal
    # object; it does not make the later submission read-only.  Conversely,
    # “分析把材料提交的风险” has 把 after the governing analysis predicate.
    last_disposal = max(prefix.rfind("把"), prefix.rfind("将"))
    if 0 <= last_disposal < last_head.start():
        return False
    return re.search(_ACTION_SWITCH, prefix[last_head.end():]) is None


def _verb_is_locally_negated(text: str, verb_start: int) -> bool:
    clause_start = max(
        text.rfind(mark, 0, verb_start) for mark in ("，", ",", "。", "；", ";", "!", "?", "！", "？")
    ) + 1
    prefix = text[clause_start:verb_start]
    negations = list(re.finditer(
        r"(?:请勿|不再|尚未|还没有|还没|暂缓|暂停|停止|不要|不得|禁止|无需|无须|"
        r"不用|不必|不需要|先不|先别|暂不|没有|别|勿|未)",
        prefix,
    ))
    for negation in reversed(negations):
        if _marker_describes_action_object(prefix, negation):
            continue
        if re.search(_ACTION_SWITCH, prefix[negation.end():]) is None:
            return True
    return False


def _has_later_unnegated_external_verb(text: str, verb_start: int) -> bool:
    clause_end_candidates = [
        position for mark in ("，", ",", "。", "；", ";", "!", "?", "！", "？")
        if (position := text.find(mark, verb_start + 1)) >= 0
    ]
    clause_end = min(clause_end_candidates, default=len(text))
    for candidate in re.finditer(_EXTERNAL_ACTION_VERB, text[verb_start + 1:clause_end]):
        candidate_start = verb_start + 1 + candidate.start()
        between = text[verb_start:candidate_start]
        if re.search(_ACTION_SWITCH, between) is None:
            continue
        if _verb_is_locally_negated(text, candidate_start):
            continue
        if _verb_is_read_only_reference(text, candidate_start):
            continue
        return True
    return False


def _nonexecuting_external_spans(
    text: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return read-only/state and prohibitive external-action mentions."""
    reference_span: dict[str, Any] | None = None
    constraint_span: dict[str, Any] | None = None
    for verb in re.finditer(_EXTERNAL_ACTION_VERB, text):
        span = {
            "start": verb.start(),
            "end": verb.end(),
            "text": verb.group(0),
            "source_kind": "user_instruction",
            "trust_level": "explicit_current_command",
        }
        if _verb_is_read_only_reference(text, verb.start()):
            reference_span = reference_span or span
        elif _verb_is_locally_negated(text, verb.start()):
            constraint_span = constraint_span or span
    return reference_span, constraint_span


def _has_unnegated_external_action(text: str) -> tuple[bool, dict[str, Any] | None]:
    patterns = (
        r"(?:请|帮我|替我|现在|立即|马上|直接)\s*(?:把|将)?[^，。；!?！？]{0,35}?(?:发送(?:给|至)|发给|发至|发过去给|抄送给|寄(?:送)?(?:给|至)?|寄出去|邮寄(?:给|到)?|递交(?:给|到)?|提交(?:给|到)?|报送(?:给|至|到)?|上传(?:到)?|交(?:给|到)?|递给|传(?:给|到)?|送达(?:给)?|打印(?:出来|一份)?|印一份|删除掉?|删掉|清空|覆盖|办理立案|完成送达|登录[^，。；!?！？]{0,12}提交)",
        r"(?:请|帮我|替我|现在|立即|马上|直接)\s*(?:立案|撤诉|完成送达|办理送达)",
        r"(?:请|帮我|替我|现在|立即|马上|直接)\s*(?:申请(?:财产)?保全|办理撤诉|解除保全|申请调取)",
        r"(?:给|向)(?:客户|当事人|对方)\s*(?:发|发送|寄|邮寄)(?:一封)?(?:邮件|消息|材料)",
        r"(?:发|发送|寄|邮寄)(?:给|至)(?:客户|当事人|对方)(?:一封|封)?(?:邮件|消息|材料)",
        r"(?:把|将)[^，。；!?！？]{1,40}?(?:发送(?:给|至)|发给|发至|发过去给|抄送给|寄(?:送)?(?:给|至)?|寄出去|邮寄(?:给|到)?|递交(?:给|到)?|"
        r"提交(?:给|到)?|报送(?:给|至|到)?|上传(?:到)?|交(?:给|到)?|递给|传(?:给|到)?|送达(?:给)?|打印出来|印一份|删除掉?|删掉|清空|覆盖)",
        r"向(?:法院|仲裁委|客户|对方)[^，。；!?！？]{0,16}(?:提交|发送|送达|递交|邮寄)",
        r"(?:上传到|递交到?|寄给|邮寄到?|登录法院系统[^，。；!?！？]{0,12}提交)[^，。；!?！？]{0,24}",
        r"(?<!提)(?:交到|传给|递(?:过去)?给)(?:法院|仲裁委|客户|当事人|对方)",
        r"(?:发|发送)(?:一封|封)?邮件给(?:客户|当事人|对方)",
        r"(?:群发|抄送)(?:一封|封)?邮件(?:给|至)(?:所有)?(?:客户|当事人|对方)",
        r"(?:给|向)(?:客户|当事人|对方)\s*送达[^，。；!?！？]{0,20}",
        r"(?:去|到)法院立案",
        r"(?:写|起草|生成|拟)[^，。；!?！？]{0,30}(?:邮件|消息)[^。；!?！？]{0,20}"
        r"(?:并|然后|之后|后|再|，|,)\s*(?:直接)?(?:发|发送)(?:出去)?(?:给)?(?:客户|当事人|对方)?",
        r"(?:写|起草|生成|拟)[^，。；!?！？]{0,30}(?:邮件|消息)[^，。；!?！？]{0,20}"
        r"(?:发出去|发送出去|发给(?:客户|当事人|对方)|发送给(?:客户|当事人|对方))",
        r"(?:拟好|写好|起草好|生成好)(?:之后|后|再)\s*(?:直接)?"
        r"(?:发|发送)(?:出去)?(?:给)?(?:客户|当事人|对方)",
        r"打印(?:出来|一份)[^，。；!?！？]{0,20}(?:起诉状|答辩状|申请书|材料|文书|文件)?",
        r"印一份[^，。；!?！？]{0,20}(?:起诉状|答辩状|申请书|材料|文书|文件)",
        r"删除(?:原|源)?(?:文件|材料|文书)",
        r"(?:删掉|清空|覆盖)(?:这个|该|原|源)?(?:文件|材料|文书)",
        r"(?:现在|立即|马上)打印(?:一份|出来)?",
        r"(?:提交|上传|报送|送达|发送|转发|抄送|寄送|邮寄|寄|递交|呈交|投递|发|交|送|传)"
        r"[^，。；!?！？]{0,16}(?:给|至|到)(?:法院|法院系统|仲裁委|仲裁系统|客户|当事人|对方)",
        r"(?:发送|转发|抄送)(?:这封|该封|这|该)?邮件",
        r"打印(?:这份|该份|这个|该|这|\d+\s*份|[一二三四五六七八九十两]+\s*份|一份)?"
        r"(?:起诉状|答辩状|上诉状|申请书|代理意见|材料|文书|文件)",
        r"删除(?:这份|该份|这个|该|这|原|源)?(?:文件|材料|文书)",
        r"(?:提交|递交|上传|报送)(?:给|到|至)?(?:法院|仲裁委|法院系统|仲裁系统)",
        r"(?:发送|发|寄|寄送|邮寄)(?:给|至)(?:客户|当事人|对方)",
        r"送达(?:给|至)?(?:客户|当事人|对方)",
        r"打印(?:一份)?(?:起诉状|答辩状|上诉状|申请书|代理意见|材料|文书|文件)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 14):match.start()]
            suffix = text[match.end():min(len(text), match.end() + 24)]
            value = match.group(0)
            left_context = text[max(0, match.start() - 24):match.start()]
            right_context = text[match.end():min(len(text), match.end() + 32)]
            context = (
                re.split(r"[，,。；;!?！？]", left_context)[-1]
                + value
                + re.split(r"[，,。；;!?！？]", right_context)[0]
            )
            external_verbs = list(re.finditer(_EXTERNAL_ACTION_VERB, value))
            later_execution_present = False
            if external_verbs:
                unnegated_external_verb = False
                for verb in external_verbs:
                    verb_prefix = value[max(0, verb.start() - 16):verb.start()]
                    if re.search(
                        r"(?:请勿|不再|尚未|还没有|还没|暂缓|暂停|停止|不要|别|勿|未|"
                        r"不得|禁止|无需|无须|不用|不必|不需要|先不|先别|暂不)"
                        r"(?:再|直接)?\s*$",
                        verb_prefix,
                    ):
                        continue
                    absolute_verb_start = match.start() + verb.start()
                    if _verb_is_locally_negated(text, absolute_verb_start):
                        continue
                    if _verb_is_read_only_reference(text, absolute_verb_start):
                        if _has_later_unnegated_external_verb(text, absolute_verb_start):
                            later_execution_present = True
                            unnegated_external_verb = True
                            break
                        continue
                    unnegated_external_verb = True
                    break
                if not unnegated_external_verb:
                    continue
            if later_execution_present:
                return True, {
                    "start": match.start(),
                    "end": match.end(),
                    "text": value,
                    "source_kind": "user_instruction",
                    "trust_level": "explicit_current_command",
                }
            clause_prefix = re.split(r"[，,。；;!?！？]", prefix)[-1]
            if re.search(
                r"(?:请勿|不再|尚未|还没有|还没|暂缓|暂停|停止|不要|别|勿|未|"
                r"不得|禁止|无需|无须|不用|不必|不需要|先不|暂不)"
                r"(?:再|现在|立即|马上|直接)?\s*$",
                clause_prefix,
            ) or re.match(
                r"\s*(?:请勿|不再|尚未|还没有|还没|暂缓|暂停|停止|不要|别|勿|未|"
                r"不得|禁止|无需|无须|不用|不必|不需要|先不|暂不)",
                value,
            ):
                continue
            if re.search(
                r"(?:已|已经|准备|拟|待|要)[^，。；!?！？]{0,8}(?:提交|发送|上传|打印)"
                r"[^，。；!?！？]{0,20}(?:看看|查看|总结|找出|概括|分析|审阅)",
                context,
            ):
                continue
            # Past events, recovery questions, process inspection, and risk
            # questions describe an external action; they do not authorize it.
            nearby = text[max(0, match.start() - 28):min(len(text), match.end() + 36)]
            if re.search(
                r"(?:已|已经|曾经)(?:寄|邮寄|递交|提交|上传|删除|删掉|清空|覆盖|立案|送达)"
                r".{0,24}(?:找出|查看|恢复|怎么办|如何处理)",
                nearby,
            ) or re.search(
                r"的(?:风险|后果|影响|流程|记录|状态)(?:是|有|如何|怎么|什么)?",
                suffix,
            ) or re.search(r"(?:是否|可否|能否|合不合适|是否合适|应不应该)", right_context):
                continue
            if re.search(
                r"(?:如果|假如|假设|关于|分析(?:一下)?|讨论(?:一下)?|研究(?:一下)?|"
                r"评估(?:一下)?|判断|是否(?:应|应该|要|需要)?|可否|能否|要不要)\s*$",
                prefix,
            ) or re.match(
                r"\s*(?:是否|可否|能否|会有什么风险|有什么风险|的风险|的方案|"
                r"合不合适|是否合适|应不应该)", suffix
            ):
                continue
            if re.search(r"(?:提交|打印|发送)(?:稿|包|清单|生产单)", value):
                continue
            return True, {
                "start": match.start(),
                "end": match.end(),
                "text": value,
                "source_kind": "user_instruction",
                "trust_level": "explicit_current_command",
            }
    return False, None


def _extract_only_scope(
    text: str,
    instruction_text: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    eligible = instruction_text if instruction_text is not None else text
    def locally_negated(position: int) -> bool:
        prefix = eligible[max(0, position - 8):position]
        return bool(re.search(r"(?:不要|别|不是|并非|不)(?:再)?\s*$", prefix))

    special_allow_patterns = (
        r"以\s*([^，,。；;！？!?]{1,40}?)\s*为唯一依据",
        r"(?:材料)?范围(?:只)?限于\s*([^，,。；;！？!?]{1,40})",
        r"(?:材料范围)?只限(?:于)?\s*([^，,。；;！？!?]{1,40})",
        r"(?:不准|不得|禁止)(?:看|读|读取)\s*([^，,。；;！？!?]{1,40}?)\s*(?:之|以)外(?:的)?(?:文件|材料)",
    )
    special_allow = next((
        match
        for pattern in special_allow_patterns
        for match in re.finditer(pattern, eligible)
        if not locally_negated(match.start())
    ), None)
    if special_allow:
        target = text[special_allow.start(1):special_allow.end(1)].strip().strip("\"'“”‘’")
        if target:
            return target, {
                "start": special_allow.start(1), "end": special_allow.end(1), "text": target,
                "source_kind": "user_instruction", "trust_level": "explicit_current_command",
            }

    inverse_patterns = (
        r"除(?:了)?\s*([^，,。；;！？!?]{1,40}?)\s*(?:(?:之|以)?外)?(?:的)?"
        r"(?:其他)?(?:材料|文件)?(?:都|一概)?(?:别看|不要看|不得看|不看|不读|不读取)",
        r"([^，,。；;！？!?]{1,40}?)\s*(?:之|以)外(?:的)?(?:其他)?(?:材料|文件)?(?:都)?"
        r"(?:别看|不要看|不得看|不看|不读|不读取)",
    )
    inverse = next((
        match
        for pattern in inverse_patterns
        for match in re.finditer(pattern, eligible)
        if not locally_negated(match.start())
    ), None)
    if inverse:
        target = text[inverse.start(1):inverse.end(1)].strip().strip("\"'“”‘’")
        if target:
            return target, {
                "start": inverse.start(1),
                "end": inverse.end(1),
                "text": target,
                "source_kind": "user_instruction",
                "trust_level": "explicit_current_command",
            }
    # Do not consume trailing whitespace here: quoted user parameters are
    # represented as same-length spaces in ``eligible``.  Stopping exactly
    # after the operator lets us recover the adjacent parameter from ``text``
    # without making control words *inside* that parameter executable.
    operator_pattern = re.compile(
        r"(?:只看|仅看|只分析|仅分析|只研究|仅研究|"
        r"只允许(?:看|读取)|只能(?:看|读取)|只许(?:看|读取)?|只准(?:看|读取)?|"
        r"只查阅|仅查阅|只参考|仅参考|(?:材料)?范围(?:只)?限于|(?:材料范围)?只限(?:于)?|"
        r"仅用|只用|仅基于|只基于|仅依据|只依据)(?:\s*一下)?"
    )
    operator = next(
        (match for match in operator_pattern.finditer(eligible) if not locally_negated(match.start())),
        None,
    )
    if not operator:
        return None, None
    target_start = operator.end()
    target_match = re.match(r"([^，,。；;！？!?]+)", text[target_start:])
    if not target_match:
        return None, None
    raw_target = target_match.group(1)
    leading = len(raw_target) - len(raw_target.lstrip())
    target_start += leading
    target = raw_target.strip().strip("\"'“”‘’")
    if raw_target.strip().startswith(("\"", "'", "“", "‘")):
        target_start += 1
    for stopper in (
        "不要上网", "不要联网", "不联网", "不要看其他材料", "不要管其他记忆",
        "其他先不展开", "其他不展开", "然后分析", "然后研究", "然后生成",
    ):
        if stopper in target:
            target = target.split(stopper, 1)[0].strip()
    if re.match(r"(?:仅用|只用|仅基于|只基于|仅依据|只依据)", operator.group(0)):
        target = re.split(r"(?:来)?(?:分析|研究|生成|起草)", target, maxsplit=1)[0].strip()
    if not target or "记忆" in target:
        return None, None
    # In the compact colloquial form “只看材料分析”, the final verb resumes
    # the instruction plane rather than becoming part of the material title.
    target = re.sub(r"(?<=(?:材料|文件|附件|合同|邮件))分析(?:一下|下)?$", "", target).strip()
    return target, {
        "start": target_start,
        "end": target_start + len(target),
        "text": target,
        "source_kind": "user_instruction",
        "trust_level": "explicit_current_command",
    }


def prepare_language_input(text: str) -> dict[str, Any]:
    """Parse only control-plane hints needed before legacy route resolution."""
    segments = segment_input(text)
    control = _command_text(text, segments)
    action_control = _action_text(text, segments)
    compact = control
    alias_definition = _alias_definition_span(compact)
    # The right-hand side of “以后我说 X 就是研究下” describes the alias.  It
    # must not launch research in the same turn.  A command after the defining
    # clause remains visible because only the matched clause is masked.
    action_compact = _mask_span(action_control, alias_definition)
    correction_boundary = _correction_boundary(action_compact)
    if correction_boundary is not None:
        action_compact = _mask_span(action_compact, {
            "start": 0, "end": correction_boundary, "text": text[:correction_boundary],
        })
    scope_target, scope_span = _extract_only_scope(compact, action_compact)
    # Scope values are parameters. Bare template names were already segmented
    # as user_parameter by segment_input(), so _action_text() has masked them.
    action_compact = _mask_span(action_compact, scope_span)

    negated_generate = _first_span(action_compact, (
        r"先(?:别|不要|不)\s*(?:直接)?生成",
        r"(?:别|不要|不再|暂不|无需|无须|不用|不必|不需要|(?:我)?没(?:有)?让你|"
        r"不是让你|并非(?:要|让你)|不是要)\s*(?:直接)?生成",
        r"先不出(?:文书|终稿|稿)", r"不出(?:文书|终稿)",
    ))
    negated_research = _first_span(action_compact, (
        r"(?:不要|别|先不|暂不|不再|不要再|无需|无须|不用|不必|不需要|"
        r"(?:我)?没(?:有)?让你|不是让你|并非(?:要|让你)|不是要)(?:再)?"
        r"(?:研究|查(?:找)?|检索|搜索|找(?:案例|判例|法条|法源|司法解释)?)",
    ))
    negated_lifecycle = _first_span(action_compact, (
        r"(?:不要|别|先不|暂不|暂缓|暂停|停止|不再|不要再|无需|无须|不用|不必|不需要|不|"
        r"(?:我)?没(?:有)?让你|不是让你|并非(?:要|让你)|不是要)(?:再)?"
        r"(?:撤诉|办理(?:手续)?|申请(?:财产)?保全|解除保全|送达|提交补充材料|"
        r"调取审计报告|申请调取)",
        r"(?:撤诉|财产保全|申请保全|解除保全|送达|调取审计报告|申请调取)"
        r"[^，,。；;！？!?]{0,8}(?:先不|先别|暂不|暂缓|暂停|先不用)",
    ))
    negated_local_mutation = _first_span(action_compact, (
        r"(?:不要|别|先不|先别|暂不|暂缓|暂停|不用|无需|无须|不必|不需要)"
        r"(?:再)?(?:正式|确认)?(?:压缩(?:文书)?|清理批注|接受修订|整理(?:提交包)?|组卷|"
        r"记成候选|记入记忆候选|写入案件状态|写入记忆|确认写入记忆|正式记入案件|"
        r"正式更新记忆|更新记忆)",
        r"(?:压缩文书|清理批注|接受修订|整理提交包|组卷|正式记入案件|确认写入记忆|"
        r"正式更新记忆)[^，,。；;！？!?]{0,8}(?:先不|先别|暂不|暂缓|暂停|先不用)",
    ))
    no_file_write = _first_span(action_compact, (
        r"不要动文件", r"别动文件", r"不要修改文件", r"只读(?:处理|看看)?", r"不写(?:入)?文件",
        r"不要保存(?:文件)?", r"不用保存文件", r"别保存(?:文件)?", r"不要写到文件", r"别改动任何文件",
        r"只在聊天里回复", r"只给文字不要文件", r"不落盘", r"别落盘", r"不要落盘", r"不要生成文件",
        r"别落到文件", r"不要输出文档", r"只在对话框展示", r"只回复消息", r"不创建文件",
        r"不要产生文档文件", r"不保存本地", r"别写盘", r"不导出文件", r"只展示草稿",
        r"不要写入任何文件夹",
    ))
    no_network = _first_nonnegated_span(action_compact, (
        r"不联网", r"别联网", r"不要联网", r"不要上网", r"禁止上网", r"不上网", r"离线",
        r"不使用网络", r"不要网络检索", r"仅本地", r"只用本地", r"只看材料", r"只用现有材料", r"仅用现有材料",
        r"(?:不要|别|不用|禁止|不许|不得|无需|不使用)(?:再)?"
        r"(?:去|通过|使用|访问|用)?(?:联网|上网|互联网|网络|公网|网上|外部数据库)"
        r"(?:查|搜|检索|查询|访问)?",
        r"(?:案例|判例|判决|法条|司法解释|最高院观点)(?:不要|别|不用|禁止|不许|不得)"
        r"(?:再)?(?:去|通过|使用|访问|用)?(?:联网|上网|互联网|网络|公网|网上|外部数据库)"
        r"(?:查|搜|检索|查询|访问)?",
    ))
    authority_object_present = bool(re.search(
            r"(?:案例|判例|判决|裁判文书|法条|法源|司法解释|最高院观点|法院观点|"
            r"裁判规则|同类案件|类似案件)",
            action_compact,
        ))
    authority_verb_span = _first_nonnegated_span(
        action_compact,
        (
            r"找", r"搜", r"检索", r"查", r"研究", r"看看", r"有没有",
            r"结合(?:网络|公网|网上)(?:检索到的?)?",
        ),
    )
    research_span = _first_nonnegated_span(action_compact, (
        r"深入研究(?:一下)?", r"仔细研究(?:一下)?", r"先研究(?:一下)?", r"再研究(?:一下)?", r"研究(?:一下|下|研究)?",
    ))
    discuss_span = _first_span(action_compact, (
        r"探讨(?:一下|下)?", r"讨论(?:一下|下)?", r"先聊聊", r"一起聊聊", r"先推演(?:一下)?",
    ))
    analyze_span = _first_nonnegated_span(action_compact, (
        r"只分析", r"仅分析", _READ_ONLY_HEAD,
        r"主要问题", r"问题在哪", r"材料分析", r"争点", r"矛盾",
        r"站在对方角度", r"对方会怎么",
    ))
    deepen_span = _first_span(action_compact, (r"再深一点", r"再深入一点", r"继续深挖", r"往深了看"))
    followup_span = _first_span(action_compact, (r"就按刚才那个", r"按刚才那个", r"继续刚才那个", r"还是刚才那个"))
    generate_patterns = (
        r"(?:只|仅)?(?:出|要|生成)(?:一份|一个|个)?(?:空白)?(?:起诉状|答辩状|申请书)?(?:空白框架|空白模板|空白稿)",
        r"(?:生成|写)(?:一份|一个|个)?空白(?:起诉状|答辩状|申请书)框架",
        r"(?:起诉状|答辩状|上诉状|申请书|代理意见|质证意见)(?:帮我|给我)?(?:写|起草)(?:一下|下)?",
        r"生成(?:一下|下)", r"直接生成", r"起草(?:一封|一份|一下)?", r"出一版", r"写一版", r"形成一版",
        r"生成(?:一份|一个|个)?(?:起诉状|答辩状|上诉状|申请书|代理意见|质证意见|比对意见|文书|提交清单)",
        r"生成(?:一份|一个|个)?(?:给法院|供法院|法院候选|供内部审阅|内部)(?:的)?(?:起诉状|答辩状|上诉状|申请书|代理意见|质证意见|文书)",
        r"写(?:一份|一个|个)?(?:起诉状|答辩状|上诉状|申请书|代理意见|质证意见|比对意见|诉状|文书)",
        r"(?:做|出)(?:一份|一个|个)?(?:简版|详细版)?(?:提交稿|储备稿)",
        r"写给(?:客户|当事人)的邮件", r"给(?:客户|当事人)(?:写|起草)(?:一封)?邮件",
        r"(?:写|拟|草拟)(?:一封|封|一个|个)?(?:给|发给|写给)?(?:客户|当事人)?(?:的)?邮件",
        r"生成(?:一封|封|一个|个|一份)?(?:发给|给|写给)?(?:客户|当事人)(?:的)?邮件",
        r"给(?:客户|当事人)(?:写|拟|草拟)(?:一封|封|一个|个)?邮件",
        r"(?:写|拟|草拟|生成)(?:好)?(?:一封|封|一个|个|一份)?(?:客户|当事人)?(?:的)?邮件",
    )
    generate_span = _first_nonnegated_span(action_compact, generate_patterns)
    negated_generate_active = bool(
        negated_generate and (
            not generate_span or int(negated_generate["start"]) > int(generate_span["start"])
        )
    )
    positive_research_span = research_span or authority_verb_span
    negated_research_active = bool(
        negated_research and (
            not positive_research_span
            or int(negated_research["start"]) > int(positive_research_span["start"])
        )
    )
    authority_research = bool(
        authority_object_present and authority_verb_span and not negated_research_active
    )
    client_email_patterns = (
        r"(?:起草|草拟|写|拟|生成)(?:好)?(?:一封|封|一个|个|一份)?"
        r"(?:给|发给|写给)?(?:客户|当事人)(?:的)?邮件",
        r"(?:给|写给)(?:客户|当事人)(?:写|起草|拟|草拟)?(?:一封|封|一个|个)?邮件",
        r"(?:起草|草拟|写|拟|生成)(?:好)?(?:一封|封|一个|个|一份)?邮件(?:草稿)?"
        r"[^，,。；;]{0,18}(?:给|发给)(?:客户|当事人)",
        r"(?:写|拟|草拟)(?:一封|封|一个|个)?客户邮件",
        r"(?:起草|草拟|写|拟|生成)(?:好)?(?:一封|封|一个|个|一份)?邮件(?:草稿)?",
    )
    client_email_request = _first_nonnegated_span(action_compact, client_email_patterns) is not None
    pleading_request = _first_nonnegated_span(action_compact, (
        r"(?:生成|起草|草拟|写|拟)(?:一份|一个|个|一下)?"
        r"(?:起诉状|答辩状|上诉状|申请书|代理意见|质证意见|比对意见|诉状|诉讼文书)",
    )) is not None
    multiple_generation_objects = client_email_request and pleading_request
    positive_legacy_action = _first_nonnegated_span(action_compact, (
        r"(?:直接)?写(?:一份)?撤诉申请", r"办理(?:撤诉|送达|立案)", r"申请(?:财产)?保全",
        r"(?:看看|查看|阅读)(?:一下)?(?:这个|该|当前|本案|全部)?(?:材料|案件|文书)",
        r"(?:总结|概括)(?:一下)?(?:这个|该|当前|本案|全部)?(?:材料|案件)",
    ))
    explicit_issue_topic = _explicit_issue_topic_span(action_compact)
    external_action, external_span = _has_unnegated_external_action(action_compact)
    external_reference_span, external_constraint_span = _nonexecuting_external_spans(action_compact)
    local_evidence_decision = bool(
        "证据" in action_compact
        and any(term in action_compact for term in (
            "暂不提交", "不要提交", "先不要提交", "不提交", "备用", "只作内部参考",
        ))
    )
    local_package_request = any(
        term in action_compact for term in ("待打印包", "打印包", "提交包", "组卷", "整理下文件夹")
    )
    if local_evidence_decision or local_package_request:
        # These phrases describe a local evidence decision or a G4 package;
        # their negated print/submit words are safety constraints on that task.
        external_reference_span = None
        external_constraint_span = None
    strict_acl_span = _first_nonnegated_span(compact, (
        r"不要看其他材料", r"其他材料都不要看", r"只允许读取", r"不得读取其他材料", r"禁止读取其他材料",
        r"不要读取聊天记录", r"仅依据当前消息", r"只依据当前消息",
        r"别看其他文件", r"不要看其他文件", r"其他文件都别看",
        r"仅凭(?:这句话|当前这句话|这条消息|当前消息)", r"就看我这条消息", r"只看我这条消息",
    ))
    current_input_only = _first_nonnegated_span(compact, (
        r"不要读取聊天记录", r"仅依据当前消息", r"只依据当前消息", r"只看当前消息",
        r"仅凭(?:这句话|当前这句话|这条消息|当前消息)", r"就看我这条消息", r"只看我这条消息",
    ))
    read_operator = re.search(
        r"(?:只看|仅看|只允许(?:看|读取)|只能(?:看|读取)|只许(?:看|读取)?|只准(?:看|读取)?|"
        r"只查阅|仅查阅|只参考|仅参考|(?:材料)?范围(?:只)?限于|(?:材料范围)?只限(?:于)?|"
        r"仅用|只用|仅基于|只基于|仅依据|只依据|以[^，,。；;！？!?]{1,40}为唯一依据|"
        r"除(?:了)?[^，,。；;！？!?]{1,40}|[^，,。；;！？!?]{1,40}(?:之|以)外)",
        action_compact,
    )
    strong_read_operator = re.search(
        r"(?:只允许(?:看|读取)|只能(?:看|读取)|只许(?:看|读取)?|只准(?:看|读取)?|"
        r"只查阅|仅查阅|只参考|仅参考|(?:材料)?范围(?:只)?限于|(?:材料范围)?只限(?:于)?|"
        r"仅用|只用|仅基于|只基于|仅依据|只依据|唯一依据|除(?:了)?|(?:之|以)外)",
        action_compact,
    )
    quoted_scope_parameter = bool(scope_span and any(
        segment.get("source_kind") == "user_parameter"
        and int(segment.get("start", -1)) <= int(scope_span["start"])
        and int(scope_span["end"]) <= int(segment.get("end", -1))
        for segment in segments
    ))
    source_only_read = bool(
        scope_target and read_operator and (
            strong_read_operator
            or quoted_scope_parameter
            or _PATH_LIKE.search(scope_target)
            or re.search(r"合同|附件|文件|材料|邮件|第\s*[一二三四五六七八九十百\d]+\s*条", scope_target)
        )
    )
    read_scope_candidate = bool(scope_target and re.search(
        r"(?:只看|仅看|只查阅|仅查阅|只参考|仅参考)", action_compact
    ))

    generation_with_authority_research = bool(
        generate_span and authority_research and not negated_generate_active and not negated_research_active
    )
    if external_action:
        action_hint = "external_action"
        action_span = external_span
    elif deepen_span:
        action_hint = "followup_deepen"
        action_span = deepen_span
    elif generation_with_authority_research:
        # The requested deliverable controls the route; authority research is
        # a mandatory upstream phase, not a competing read-only action.
        action_hint = "generate"
        action_span = generate_span
    elif authority_research:
        action_hint = "authority_research"
        action_span = research_span or _first_span(action_compact, (r"找", r"搜", r"检索", r"查"))
    elif research_span and not negated_research_active:
        action_hint = "case_research"
        action_span = research_span
    elif discuss_span:
        action_hint = "discuss"
        action_span = discuss_span
    elif generate_span and not negated_generate_active:
        action_hint = "generate"
        action_span = generate_span
    elif analyze_span and not positive_legacy_action:
        action_hint = "analyze"
        action_span = analyze_span
    elif (
        negated_generate_active or negated_research_active or negated_lifecycle or negated_local_mutation
        or external_constraint_span
    ) and not positive_legacy_action:
        action_hint = "constraint_only"
        action_span = (
            (negated_generate if negated_generate_active else None)
            or (negated_research if negated_research_active else None)
            or negated_lifecycle or negated_local_mutation or external_constraint_span
        )
    elif followup_span:
        action_hint = "followup_reference"
        action_span = followup_span
    else:
        action_hint = None
        action_span = None

    deep_signal = _first_span(action_compact, (
        r"很难", r"比较难", r"复杂", r"深度", r"深入", r"彻底研究", r"研究透",
    ))
    quick_signal = _first_span(action_compact, (
        r"不难", r"很简单", r"简单案", r"快速(?:看|做|生成)?", r"先粗看", r"先出个简版",
    ))
    if action_hint in {"case_research", "authority_research"} or deep_signal or deepen_span:
        effort_hint = "deep"
        effort_span = research_span or deep_signal or deepen_span
    elif action_hint == "discuss":
        effort_hint = "discuss"
        effort_span = discuss_span
    elif quick_signal:
        effort_hint = "quick"
        effort_span = quick_signal
    else:
        effort_hint = "standard"
        effort_span = None

    detail = "normal"
    detail_span = None
    detail_candidates: list[tuple[int, str, dict[str, Any]]] = []
    detail_candidates.extend(
        (int(span["start"]), "detailed", span)
        for span in _all_nonnegated_spans(action_compact, (r"详细", r"细致", r"全面展开", r"展开说"))
    )
    detail_candidates.extend(
        (int(span["start"]), "brief", span)
        for span in _all_nonnegated_spans(
            action_compact, (r"简短", r"简洁", r"精简", r"简单说", r"只说主要", r"不要那么复杂", r"不要复杂")
        )
    )
    if detail_candidates:
        _, detail, detail_span = max(detail_candidates, key=lambda item: item[0])
    tone = "default"
    tone_span = None
    tone_candidates: list[tuple[int, str, dict[str, Any]]] = []
    tone_candidates.extend(
        (int(span["start"]), "litigation", span)
        for span in _all_nonnegated_spans(action_compact, (r"诉讼化", r"法庭化"))
    )
    tone_candidates.extend(
        (int(span["start"]), "formal", span)
        for span in _all_nonnegated_spans(action_compact, (r"正式一点", r"正式风格", r"正式表达"))
    )
    if tone_candidates:
        _, tone, tone_span = max(tone_candidates, key=lambda item: item[0])
    elif action_hint == "discuss":
        tone, tone_span = "exploratory", discuss_span

    negative_style_span = _first_span(action_compact, (
        r"(?:别|不要|不用|无需|无须|不必)(?:再)?写得?(?:太)?详细",
        r"(?:别|不要|不用|无需|无须|不必)(?:再)?(?:太|那么)?详细",
        r"(?:别|不要|不用|无需|无须|不必)(?:再)?(?:太|那么)?简短",
    ))
    style_control_residual = action_compact
    for pattern in (
        r"(?:不要|别)记住(?:我)?", r"(?:本案)?以后", r"今后", r"这次", r"本轮", r"都",
        r"简短|简洁|精简|详细|细致|全面展开|展开说|诉讼化|法庭化|正式风格|正式表达|正式",
        r"(?:别|不要|不用|无需|无须|不必)", r"写得?|说得?", r"改成|改为",
        r"但是|不过|可是|但|要|再|太|那么|一点",
    ):
        style_control_residual = re.sub(pattern, "", style_control_residual)
    style_control_only = bool(detail_span or tone_span or negative_style_span) and not re.sub(
        r"[\s，,。；;！？!?、]", "", style_control_residual
    )
    # Style-only and alias-definition utterances are understood, but this
    # module neither learns nor persists them.  Acknowledge the control without
    # dispatching case/file tools; an explicit or domain-specific action in the
    # same turn remains available to the legacy specialist router.
    if action_hint is None and (alias_definition or style_control_only):
        action_hint = "control_only"
        action_span = alias_definition or detail_span or tone_span or negative_style_span

    provenance: dict[str, list[dict[str, Any]]] = {}
    for field, span in (
        ("task_kind", action_span), ("user_effort_hint", effort_span),
        ("issue_scope", scope_span), ("output_scope", scope_span),
        ("read_acl", strict_acl_span or (scope_span if source_only_read else None)), ("network_policy", no_network),
        ("style_patch.detail", detail_span), ("style_patch.tone", tone_span),
        ("mutation_scope", external_span or no_file_write),
    ):
        if span:
            provenance.setdefault(field, []).append(span)

    return {
        "segments": segments,
        "command_text": control,
        "action_text": action_compact,
        "action_hint": action_hint,
        "external_reference": external_reference_span is not None,
        "authority_research": authority_research,
        "authority_research_precondition": generation_with_authority_research,
        "template_refs": _extract_template_refs(control),
        "client_email_request": client_email_request,
        "pleading_request": pleading_request,
        "multiple_generation_objects": multiple_generation_objects,
        "explicit_issue_topic": explicit_issue_topic is not None,
        "negated_generate": negated_generate_active,
        "negated_research": negated_research_active,
        "negated_lifecycle": negated_lifecycle is not None,
        "negated_local_mutation": negated_local_mutation is not None,
        "no_file_write": no_file_write is not None,
        "no_network": no_network is not None,
        "scope_target": scope_target,
        "strict_read_acl": strict_acl_span is not None or source_only_read,
        "source_only_read": source_only_read,
        "read_scope_candidate": read_scope_candidate,
        "current_input_only": current_input_only is not None,
        "alias_definition": alias_definition is not None,
        "user_effort_hint": effort_hint,
        "quick_signal_present": quick_signal is not None,
        "style_patch": {"detail": detail, "tone": tone},
        "provenance": provenance,
        "recognized": any((
            action_hint, authority_research, negated_generate_active, negated_research_active, negated_lifecycle,
            negated_local_mutation, external_reference_span, external_constraint_span,
            no_file_write, no_network,
            scope_target, strict_acl_span, source_only_read, detail_span, tone_span, alias_definition,
        )),
    }


def _focus_refs(state: dict[str, Any] | None, task_kind: str) -> list[str]:
    if not state:
        return []
    focus = state.get("focus", {})
    keys_by_task = {
        "generate": ("current_artifact_id", "current_template_id"),
        "modify": ("current_artifact_id",),
        "sanitize": ("current_artifact_id",),
        "package": ("current_matter_id", "current_artifact_id"),
        "analyze": ("current_issue_id",),
        "research": ("current_issue_id",),
        "discuss": ("current_issue_id", "current_artifact_id"),
        "view": ("current_source_id", "current_artifact_id", "current_matter_id"),
        "continue": (
            "current_workflow_instance_id", "current_task_id", "current_issue_id", "current_artifact_id",
        ),
    }
    return [focus.get(key) for key in keys_by_task.get(task_kind, ()) if isinstance(focus.get(key), str)]


def _resolve_scope_source(
    scope_target: str | None,
    state: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """Resolve a textual material/locator scope only when one source matches."""
    if not scope_target or not state:
        return [], []
    if re.fullmatch(r"(?:现有|已有|当前|本案|全部|所有|我给的)?材料", scope_target.strip()):
        source_ids = [
            source["id"] for source in state.get("sources", []) if isinstance(source.get("id"), str)
        ]
        return source_ids, source_ids
    source_hint = re.sub(r"第\s*[一二三四五六七八九十百\d]+\s*条.*$", "", scope_target).strip()
    source_hint = re.sub(r"(?:中的?|里面的?)$", "", source_hint).strip()
    if not source_hint:
        return [], []
    matches: list[str] = []
    for source in state.get("sources", []):
        source_id = source.get("id")
        searchable = " ".join(str(source.get(key) or "") for key in ("path", "title", "name"))
        if isinstance(source_id, str) and (source_hint == source_id or source_hint in searchable):
            matches.append(source_id)
    if len(matches) != 1:
        return [], []
    if source_hint == matches[0]:
        return matches, [matches[0]]
    locator_match = re.search(r"第\s*[一二三四五六七八九十百\d]+\s*条", scope_target)
    locator = locator_match.group(0) if locator_match else scope_target
    return matches, [f"{matches[0]}#{locator}"]


def _derived_provenance(text: str) -> list[dict[str, Any]]:
    return [{
        "start": None,
        "end": None,
        "text": text,
        "source_kind": "deterministic_route",
        "trust_level": "derived_not_user_quote",
    }]


def interpret_task(
    text: str,
    resolved: dict[str, Any],
    state: dict[str, Any] | None = None,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a resolved legacy route into the richer transient TaskFrame."""
    prepared = prepared or prepare_language_input(text)
    route = resolved.get("route", {})
    intent = resolved.get("intent", {})
    template_refs = list(dict.fromkeys(
        str(item).strip() for item in prepared.get("template_refs", []) if str(item).strip()
    ))
    legacy_template_alias = intent.get("template_alias")
    if len(template_refs) > 1:
        intent["template_alias"] = None
    elif not template_refs and legacy_template_alias:
        template_refs.append(str(legacy_template_alias).strip())
    # Backward compatible for a single reference; a multi-reference request
    # cannot be represented truthfully in the legacy scalar alias field.
    intent.setdefault("template_refs", template_refs)
    authority_research_precondition = prepared.get("authority_research_precondition") is True
    if authority_research_precondition and not prepared.get("no_network"):
        intent["network_mode"] = "public_web_if_available"
        resolved.setdefault("safety", {})["network_mode"] = "public_web_if_available"
    route_action = str(route.get("action") or "view")
    task_kind = {
        "case_research": "research",
        "authority_research": "research",
        "discuss": "discuss",
        "analyze": "analyze",
        "followup_deepen": "analyze",
        "followup_reference": "continue",
        "external_action": "external_action",
        "constraint_only": "constraint_ack",
        "control_only": "constraint_ack",
        "generate": "generate",
    }.get(prepared.get("action_hint"), route_action)
    unknown: list[str] = []
    if (
        route_action == "view" and route.get("object") == "matter"
        and prepared.get("action_hint") is None and not prepared.get("scope_target")
    ):
        task_kind = "freeform_readonly"
        unknown.append(text)

    current_input_only = prepared.get("current_input_only") is True
    context_state = None if current_input_only or task_kind == "constraint_ack" else state
    scope_lock = [] if current_input_only else [
        item for item in route.get("scope_lock", []) if isinstance(item, str)
    ]
    scope_target = prepared.get("scope_target")
    if task_kind == "constraint_ack":
        issue_scope = {"mode": "inherit", "items": []}
        output_scope = {"mode": "inherit", "items": []}
    elif scope_target:
        scope_items = scope_lock or [scope_target]
        issue_scope = {"mode": "only", "items": scope_items}
        output_scope = {"mode": "only", "items": [scope_target]}
    elif scope_lock:
        issue_scope = {"mode": "inherit", "items": scope_lock}
        output_scope = {"mode": "inherit", "items": scope_lock}
    else:
        issue_scope = {"mode": "inherit", "items": []}
        output_scope = {"mode": "inherit", "items": []}

    resolved_scope_refs, resolved_acl_items = _resolve_scope_source(scope_target, context_state)
    is_multi_generation = task_kind == "generate" and prepared.get("multiple_generation_objects") is True
    is_client_email = (
        task_kind == "generate"
        and prepared.get("client_email_request") is True
        and not is_multi_generation
    )
    colon_payload = re.search(r"[：:]\s*(.{8,})", prepared["command_text"], flags=re.S)
    has_supplied_data = bool(resolved.get("input_materials")) or any(
        segment["source_kind"] in {"quoted_data", "unquoted_data"} for segment in prepared["segments"]
    ) or colon_payload is not None
    if task_kind == "constraint_ack":
        target_refs = []
    elif scope_target:
        target_refs = list(dict.fromkeys([
            *([] if prepared.get("source_only_read") else scope_lock),
            *resolved_scope_refs,
        ]))
    elif is_client_email:
        target_refs = []
    elif prepared.get("explicit_issue_topic"):
        target_refs = list(scope_lock)
    else:
        target_refs = list(dict.fromkeys([*scope_lock, *_focus_refs(context_state, task_kind)]))
    if task_kind == "approve" and context_state:
        target_refs = list(dict.fromkeys([
            item for item in context_state.get("focus", {}).get("pending_approval_ids", [])
            if isinstance(item, str)
        ]))
    document_terms = (
        "文书", "诉状", "起诉状", "答辩状", "上诉状", "代理意见", "质证意见", "比对意见",
        "申请书", "邮件", "Word", "DOCX", "PDF",
    )
    missing_required: list[str] = []
    alternatives: list[str] = []
    # An explicitly supplied template is a task input, not a permanently
    # registered personal template. Its use does not require matter state.
    input_materials = resolved.get("input_materials", [])
    supplied_template = sum(item.get("role") == "template" for item in input_materials) == 1
    if is_multi_generation:
        missing_required.append("generation_object_choice")
        alternatives.extend(["generate_pleading_first", "generate_client_email_first"])
    if task_kind == "generate" and not (
        any(term.casefold() in prepared["command_text"].casefold() for term in document_terms)
        or intent.get("template_alias") or template_refs or target_refs or supplied_template
    ):
        missing_required.append("document_type_or_current_artifact")
        alternatives.extend(["specify_document_type", "produce_analysis_only"])
    skeleton_mode = bool(re.search(r"(?:空白模板|空白稿|空白框架|只搭框架|仅搭框架|只出框架|仅出框架)", prepared["action_text"]))
    if task_kind == "generate" and not is_client_email and not context_state and not has_supplied_data and not skeleton_mode:
        missing_required.append("generation_basis_or_skeleton_mode")
        alternatives.extend(["provide_matter_material", "request_blank_skeleton"])
    # Do not lose an apparent user task swallowed by an unquoted material
    # boundary. Keep ambiguous text untrusted and ask, rather than silently
    # finishing a read-only task or executing a command from the material.
    if any(
        segment.get("source_kind") == "unquoted_data"
        and re.search(r"[。\n]\s*(?:请据此|请根据上述|现在请你|现在帮我).{0,20}(?:起草|生成|写一份)", segment.get("text", ""))
        for segment in prepared["segments"]
    ):
        missing_required.append("material_command_boundary")
        alternatives.extend(["confirm_trailing_task", "keep_entire_span_as_material"])
    supplied_email_content = any(
        segment["source_kind"] in {"quoted_data", "unquoted_data"} and "邮件" in segment["text"]
        for segment in prepared["segments"]
    )
    if is_client_email and not (
        supplied_email_content
        or re.search(
            r"(?:关于|就|告知|通知|回复|催告|催款|说明|确认|询问|主题|内容|告诉|要求|用于|目的是).{1,40}",
            prepared["action_text"],
        )
    ):
        missing_required.append("email_purpose_or_content")
        alternatives.extend(["specify_email_purpose", "provide_key_points"])
    if task_kind == "modify" and not (intent.get("modify_scope") or target_refs):
        missing_required.append("document_target")
        alternatives.extend(["specify_document", "describe_change_without_writing"])
    if task_kind == "sanitize" and not target_refs:
        missing_required.append("document_target")
        alternatives.extend(["specify_document", "describe_cleaning_only"])
    if task_kind == "package" and not target_refs:
        missing_required.append("matter_or_package_target")
        alternatives.extend(["identify_matter", "identify_candidate_package"])
    if task_kind == "approve" and not target_refs:
        missing_required.append("pending_approval_target")
        alternatives.extend(["show_pending_approvals", "name_exact_candidate"])
    if prepared.get("action_hint") in {"followup_deepen", "followup_reference"} and not target_refs:
        missing_required.append("current_reference")
        alternatives.extend(["name_current_issue", "name_current_artifact"])
    if prepared.get("strict_read_acl") and not scope_target and not prepared.get("current_input_only"):
        missing_required.append("read_acl_target")
        alternatives.append("specify_allowed_materials")
    if (
        prepared.get("strict_read_acl") and scope_target and not resolved_acl_items and context_state
        and not prepared.get("current_input_only") and not prepared.get("source_only_read")
    ):
        missing_required.append("read_acl_resolved_source")
        alternatives.append("identify_exact_source_and_locator")
    generic_material_scope = bool(
        scope_target and re.fullmatch(r"(?:现有|已有|当前|本案|全部|所有|我给的)?材料", scope_target.strip())
    )
    source_like_scope = bool(
        scope_target
        and not generic_material_scope
        and re.search(
            r"合同|协议|附件|文件|材料|邮件|记录|登记|清单|通知|报告|凭证|账册|表格|"
            r"第\s*[一二三四五六七八九十百\d]+\s*条",
            scope_target,
        )
    )
    if (
        prepared.get("source_only_read") and scope_target and not resolved_acl_items
        and not prepared.get("current_input_only")
    ):
        missing_required.append("scope_source_or_locator")
        alternatives.append("provide_or_identify_source")
    elif source_like_scope and not context_state:
        missing_required.append("scope_source_or_locator")
        alternatives.append("provide_or_identify_source")
    elif source_like_scope and context_state and not resolved_scope_refs and "read_acl_resolved_source" not in missing_required:
        missing_required.append("scope_source_or_locator")
        alternatives.append("identify_exact_source_and_locator")
    elif generic_material_scope and not context_state and not has_supplied_data:
        missing_required.append("scope_source_or_locator")
        alternatives.append("provide_or_identify_source")
    elif (
        prepared.get("read_scope_candidate") and not context_state and not has_supplied_data
        and scope_target and not re.search(r"(?:问题|争点|责任|风险|请求|主张|抗辩)$", scope_target)
    ):
        missing_required.append("scope_source_or_locator")
        alternatives.append("provide_or_identify_source")
    if task_kind in {"research", "discuss", "analyze"} and not context_state and not scope_target:
        if not has_supplied_data:
            missing_required.append("matter_or_issue")
            alternatives.extend(["name_issue", "provide_material"])
    if task_kind == "freeform_readonly" and not context_state and not has_supplied_data:
        missing_required.append("freeform_target_or_input")
        alternatives.extend(["name_matter_or_material", "paste_relevant_content"])
    if task_kind in {"summarize", "view"} and not context_state and not has_supplied_data:
        missing_required.append("matter_or_material")
        alternatives.extend(["identify_matter", "provide_material"])
    task_frame_memory = route.get("memory_policy", {})
    raw_memory_read_scope = [
        item for item in task_frame_memory.get("read_scope", []) if isinstance(item, str) and item.strip()
    ]
    resolved_memory_read_scope: list[str] = []
    if task_frame_memory.get("read_mode") == "file_scoped":
        for memory_scope_item in raw_memory_read_scope:
            _, scope_acl = _resolve_scope_source(memory_scope_item, context_state)
            if scope_acl:
                resolved_memory_read_scope.extend(scope_acl)
            else:
                resolved_memory_read_scope.append(memory_scope_item)
                if context_state:
                    missing_required.append("memory_read_scope_unresolved")
                    alternatives.append("identify_existing_memory_source")
    if task_frame_memory:
        if not state and (
            task_frame_memory.get("read_mode") != "off"
            or (route_action in {"record", "recall"} and task_frame_memory.get("write_mode") != "none")
        ):
            missing_required.append("matter_context")
            alternatives.append("identify_matter")
        if task_frame_memory.get("read_mode") == "file_scoped" and not task_frame_memory.get("read_scope"):
            missing_required.append("memory_read_scope")
            alternatives.append("identify_memory_source")

    if prepared.get("action_hint") == "external_action":
        mutation_scope = "external_action"
    elif prepared.get("no_file_write") or task_kind in {
        "view", "summarize", "analyze", "research", "discuss", "continue", "freeform_readonly",
        "constraint_ack",
    }:
        mutation_scope = "read_only"
    elif task_kind == "generate":
        mutation_scope = "draft_only"
    else:
        mutation_scope = "local_write"

    reasoning_depth = intent.get("reasoning_depth", "standard")
    if task_kind == "research" or (
        reasoning_depth == "deep" and not is_client_email
    ) or (is_client_email and prepared.get("user_effort_hint") == "deep"):
        assessed_complexity = "deep"
    elif prepared.get("user_effort_hint") == "quick":
        assessed_complexity = "quick"
    else:
        assessed_complexity = "standard"

    conflicts: list[str] = []
    if prepared.get("user_effort_hint") == "quick" and assessed_complexity == "deep":
        conflicts.append("user_quick_hint_overridden_by_recorded_risk")
    if prepared.get("negated_generate"):
        conflicts.append("generation_explicitly_negated")
    if prepared.get("negated_research"):
        conflicts.append("research_explicitly_negated")
    if prepared.get("negated_lifecycle"):
        conflicts.append("lifecycle_action_explicitly_negated")
    if prepared.get("negated_local_mutation"):
        conflicts.append("local_mutation_explicitly_negated")
    if prepared.get("quick_signal_present") and assessed_complexity == "deep":
        conflicts.append("quick_language_overridden_by_deep_work")
    if prepared.get("no_file_write") and route_action in {"modify", "sanitize", "package"}:
        conflicts.append("requested_action_conflicts_with_no_file_write")

    must_not = ["execute_g5_external_action", "treat_preference_as_case_fact"]
    if prepared.get("negated_generate") or task_kind in {"research", "discuss"}:
        must_not.append("finalize_document")
    if prepared.get("negated_research"):
        must_not.append("perform_authority_research")
    if prepared.get("negated_lifecycle"):
        must_not.append("perform_lifecycle_mutation")
    if prepared.get("negated_local_mutation"):
        must_not.append("perform_negated_local_mutation")
    if task_kind == "constraint_ack":
        must_not.extend(["write_file", "commit_case_memory"])
    if prepared.get("no_file_write") or task_kind == "discuss":
        must_not.extend(["write_file", "commit_case_memory"])
    if route.get("memory_policy", {}).get("write_mode") == "none":
        must_not.append("commit_case_memory")
    if prepared.get("no_network") or intent.get("network_mode") == "deny":
        must_not.append("use_network")
    if task_kind == "generate":
        must_not.append("skip_legal_gates")

    memory_policy = task_frame_memory
    evidence_sources = ["current_user_input"]
    if memory_policy.get("read_mode") == "file_scoped":
        evidence_sources.extend(["matter_state", "file_scoped_matter_materials"])
    elif memory_policy.get("read_mode") != "off":
        evidence_sources.extend(["matter_state", "matter_materials"])
    if intent.get("network_mode") == "public_web_if_available":
        evidence_sources.append("verified_public_web_if_available")
    if any(segment["source_kind"] == "quoted_data" for segment in prepared["segments"]):
        evidence_sources.append("quoted_user_content_as_data")

    authority_requirements: list[str] = []
    if route_action == "research" and route.get("skill") == "legal-authority-research":
        authority_requirements = [
            "supporting_and_material_adverse_results", "primary_source_verification", "exact_citation_locator",
        ]
    elif task_kind == "research":
        authority_requirements = ["identify_authority_questions_before_external_search"]
    elif task_kind == "generate" and authority_research_precondition:
        authority_requirements = [
            "identify_authority_questions_before_external_search",
            "supporting_and_material_adverse_results",
            "primary_source_verification",
            "exact_citation_locator",
            "bind_every_legal_proposition_before_drafting",
        ]

    if prepared.get("current_input_only"):
        read_acl = {"mode": "restricted", "items": ["current_user_input"], "explicit": True}
    elif prepared.get("strict_read_acl"):
        read_acl = {"mode": "restricted", "items": resolved_acl_items, "explicit": True}
    else:
        read_acl = {"mode": "default", "items": [], "explicit": False}

    output_stage = {
        "view": "answer", "summarize": "answer", "freeform_readonly": "answer",
        "discuss": "ideas", "research": "analysis", "analyze": "analysis",
        "generate": "draft", "modify": "draft", "package": "candidate_package",
        "approve": "decision", "external_action": "decision", "constraint_ack": "decision",
    }.get(task_kind, "analysis")
    interaction_posture = {
        "discuss": "discuss", "view": "inspect", "summarize": "inspect",
        "continue": "continue", "external_action": "external", "constraint_ack": "inspect",
    }.get(task_kind, "execute")

    field_provenance = dict(prepared.get("provenance", {}))
    if "task_kind" not in field_provenance:
        field_provenance["task_kind"] = _derived_provenance(route_action)
    field_provenance.setdefault("assessed_complexity", _derived_provenance(str(reasoning_depth)))
    field_provenance.setdefault("memory_context_policy", _derived_provenance("resolved_memory_policy"))

    stage = str((context_state or {}).get("matter", {}).get("stage") or "unknown")
    semantic_fallback_required = bool(unknown)
    return {
        "schema_version": LANGUAGE_SCHEMA_VERSION,
        "id": f"TF-{uuid.uuid4().hex[:16]}",
        "parser_version": PARSER_VERSION,
        "raw_text": text,
        "command_text": prepared["command_text"],
        "task_kind": task_kind,
        "object_hint": (
            "multiple_generation_objects" if is_multi_generation
            else "client_email" if is_client_email
            else str(route.get("object") or "matter")
        ),
        "stage": stage,
        "target_refs": target_refs,
        "template_refs": template_refs,
        "authority_research_precondition": authority_research_precondition,
        "issue_scope": issue_scope,
        "output_scope": output_scope,
        "evidence_sources": list(dict.fromkeys(evidence_sources)),
        "memory_context_policy": {
            "read_mode": str(memory_policy.get("read_mode") or "relevant"),
            "read_scope": list(dict.fromkeys(resolved_memory_read_scope)),
            "write_mode": str(memory_policy.get("write_mode") or "candidate"),
            "preference_mode": "confirmed_relevant_only",
            "max_preferences": 5,
            "case_facts_from_preferences_allowed": False,
        },
        "network_policy": str(intent.get("network_mode") or "local_only"),
        "delivery_audience": str(intent.get("audience") or "internal_review"),
        "input_materials": input_materials,
        "knowledge_policy": (
            "off" if task_kind == "constraint_ack" or prepared.get("current_input_only")
            or prepared.get("strict_read_acl")
            or re.search(r"(?:不|别|禁止|无需)(?:再)?(?:查|用|调用|访问|连接)?(?:数据库|知识库|MCP)|(?:仅|只)(?:用|依据|根据)(?:上传|附件|当前|这些)", prepared["action_text"], re.I)
            else "configured_readonly_if_available"
        ),
        "authority_requirements": authority_requirements,
        "read_acl": read_acl,
        "mutation_scope": mutation_scope,
        "user_effort_hint": str(prepared.get("user_effort_hint") or "standard"),
        "assessed_complexity": assessed_complexity,
        "interaction_posture": interaction_posture,
        "output_stage": output_stage,
        "style_patch": prepared["style_patch"],
        "must_not": list(dict.fromkeys(must_not)),
        "missing_required": list(dict.fromkeys(missing_required)),
        "alternatives": list(dict.fromkeys(alternatives)),
        "conflicts": conflicts,
        "unknown": unknown,
        "input_segments": prepared["segments"],
        "field_provenance": field_provenance,
        "semantic_fallback": {
            "required": semantic_fallback_required,
            "reason": "new_or_composite_expression" if semantic_fallback_required else None,
        },
    }


def decide_clarification(
    task_frame: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    """Choose execute/ask/preview/block while asking at most one question."""
    task_kind = task_frame["task_kind"]
    mutation_scope = task_frame["mutation_scope"]
    route = resolved.get("route", {})
    safety = resolved.get("safety", {})
    if mutation_scope == "external_action":
        return {
            "schema_version": LANGUAGE_SCHEMA_VERSION,
            "decision": "block",
            "risk_tier": "R3",
            "requires_user_input": False,
            "question": None,
            "reason_codes": ["g5_executor_absent", "external_action_requires_separate_human_execution"],
            "assumptions": [],
            "alternatives": ["prepare_candidate_only", "produce_manual_action_checklist"],
            "max_questions": 1,
            "continue_read_only": True,
        }

    missing = task_frame.get("missing_required", [])
    if "requested_action_conflicts_with_no_file_write" in task_frame.get("conflicts", []):
        return {
            "schema_version": LANGUAGE_SCHEMA_VERSION,
            "decision": "ask",
            "risk_tier": "R1",
            "requires_user_input": True,
            "question": "你同时要求处理文书又要求不动文件；我先只给修改建议、不生成文件，可以吗？",
            "reason_codes": ["write_constraint_conflict"],
            "assumptions": [],
            "alternatives": ["read_only_advice", "derived_draft_after_confirmation"],
            "max_questions": 1,
            "continue_read_only": True,
        }
    if missing:
        field = missing[0]
        questions = {
            "generation_object_choice": "这次先生成诉讼文书还是客户邮件？两类产物的审查门禁不同，我先处理一类。",
            "document_type_or_current_artifact": "你要生成哪一种文书，还是先基于当前材料出一份分析稿？",
            "email_purpose_or_content": "这封邮件要向客户说明什么事项或达到什么目的？",
            "document_target": "你要修改哪一份文书或哪一个具体段落？",
            "current_reference": "你说的“刚才那个”具体是哪个争点、文书还是工单？",
            "read_acl_target": "你允许我读取的材料范围具体是哪一份或哪一组？",
            "read_acl_resolved_source": "你说的限定材料具体对应哪一份文件和哪个位置？",
            "scope_source_or_locator": "“只看”的内容对应哪一份材料或哪个具体位置？",
            "matter_or_issue": "你要研究/探讨的是哪个案件或哪个具体争点？",
            "matter_context": "你要调用的是哪个案件的记忆？",
            "memory_read_scope": "文件限定记忆具体对应哪一份材料或路径？",
            "memory_read_scope_unresolved": "文件限定记忆没有对应到本案现有来源；请确认具体文件、来源 ID 或路径。",
            "freeform_target_or_input": "你要我过筛的是哪个案件/材料，或者可以把需要比较的内容贴在当前消息里？",
            "generation_basis_or_skeleton_mode": "请提供要据以起草的案件材料；如果你只要空白结构，也可以明确说“只出空白框架”。",
            "material_command_boundary": "材料末尾的起草要求是你本次要我执行的任务吗？请把任务与材料分开说明。",
            "matter_or_material": "你要查看/总结的是哪个案件或哪份材料？",
            "matter_or_package_target": "你要整理哪个案件或哪一组候选材料的提交包？",
            "pending_approval_target": "当前没有唯一待批准对象；请指出要批准的具体候选，或先查看待批准清单。",
        }
        return {
            "schema_version": LANGUAGE_SCHEMA_VERSION,
            "decision": "ask",
            "risk_tier": "R1" if task_kind in {"generate", "modify"} else "R0",
            "requires_user_input": True,
            "question": questions.get(field, "请补充一个会改变执行路线的关键目标。"),
            "reason_codes": [f"missing:{field}"],
            "assumptions": [],
            "alternatives": task_frame.get("alternatives", [])[:2],
            "max_questions": 1,
            "continue_read_only": False,
        }

    if task_kind in {"generate", "modify", "sanitize", "package", "approve"} or safety.get("requires_approval"):
        return {
            "schema_version": LANGUAGE_SCHEMA_VERSION,
            "decision": "preview",
            "risk_tier": "R2",
            "requires_user_input": False,
            "question": None,
            "reason_codes": ["candidate_or_gate_controlled_output"],
            "assumptions": [],
            "alternatives": [],
            "max_questions": 1,
            "continue_read_only": False,
        }

    assumptions: list[str] = []
    if task_kind == "research" and not task_frame.get("target_refs"):
        assumptions.append("按当前案件的主要争点开展研究，先不生成最终文书。")
    if task_kind == "freeform_readonly":
        assumptions.append("新表达先按只读、无外部动作的安全路径处理。")
    return {
        "schema_version": LANGUAGE_SCHEMA_VERSION,
        "decision": "execute",
        "risk_tier": "R0",
        "requires_user_input": False,
        "question": None,
        "reason_codes": ["safe_reversible_interpretation"],
        "assumptions": assumptions,
        "alternatives": [],
        "max_questions": 1,
        "continue_read_only": True,
    }


def compile_run_spec(
    task_frame: dict[str, Any],
    clarification: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    """Compile execution depth into checkable work rather than a style label."""
    task_kind = task_frame["task_kind"]
    authority_precondition = task_frame.get("authority_research_precondition") is True
    court_candidate = task_frame.get("delivery_audience") == "court_candidate"
    composed = len(task_frame.get("template_refs", [])) > 1 or authority_precondition or (
        task_kind == "generate" and task_frame["assessed_complexity"] == "deep"
    )
    if task_kind == "generate" and task_frame.get("object_hint") == "multiple_generation_objects":
        recipe = "standard"
    elif task_kind == "discuss":
        recipe = "discuss"
    elif authority_precondition or task_frame["assessed_complexity"] == "deep" or task_frame["user_effort_hint"] == "deep" or task_kind == "research":
        recipe = "deep"
    elif task_frame["user_effort_hint"] == "quick":
        recipe = "quick"
    else:
        recipe = "standard"

    artifacts_by_recipe = {
        "quick": ["bounded_output", "scope_and_obvious_omission_check"],
        "standard": ["issue_map", "evidence_map", "bounded_output", "fact_scope_source_check"],
        "deep": [
            "problem_tree", "source_ledger", "claim_evidence_matrix", "contradiction_register",
            "strongest_adverse_path", "independent_review",
        ],
        "discuss": ["alternative_interpretations", "evidence_gaps", "outcome_changing_conditions"],
    }
    if task_kind == "constraint_ack":
        required_artifacts = ["acknowledged_constraint", "forbidden_action_summary"]
    elif task_kind == "generate" and task_frame.get("object_hint") == "multiple_generation_objects":
        required_artifacts = ["generation_object_choice"]
    else:
        required_artifacts = list(artifacts_by_recipe[recipe])
    is_client_email = task_frame.get("object_hint") == "client_email"
    is_multi_generation = task_frame.get("object_hint") == "multiple_generation_objects"
    if task_kind == "generate" and is_multi_generation:
        pass
    elif task_kind == "generate" and is_client_email:
        required_artifacts.extend(["recipient_purpose_brief", "outgoing_email_draft", "communication_review_candidate"])
    elif task_kind == "generate":
        required_artifacts.extend(["source_linked_draft", "review_candidate"])
    if resolved.get("route", {}).get("action") == "research" or authority_precondition:
        required_artifacts.extend(["authority_ledger", "verification_status"])
    if task_kind == "generate" and composed and court_candidate:
        required_artifacts.extend([
            "composition_spec", "claim_binding_matrix", "citation_audit_report",
            "exemplar_leak_report",
        ])

    validators = [
        "command_data_boundary", "scope_compliance", "fact_source_traceability", "no_g5_external_action",
    ]
    if recipe == "deep":
        validators.extend(["contradiction_coverage", "material_adverse_path_coverage", "independent_review"])
    if recipe == "discuss":
        validators.extend(["no_finalization", "no_file_write", "no_case_memory_commit"])
    if task_kind == "generate" and is_multi_generation:
        validators.append("generation_object_disambiguation")
    elif task_kind == "generate" and is_client_email:
        validators.extend(["recipient_and_purpose_frozen", "confidentiality_review", "outgoing_communication_review", "no_external_send"])
    elif task_kind == "generate":
        validators.extend(["template_resolution", "independent_draft_review", "source_and_assumption_labels"])
        if court_candidate:
            validators.extend(["G1_strategy", "G2_evidence"])
        if composed and court_candidate:
            validators.extend([
                "composition_spec_validation", "single_layout_primary", "single_structure_primary",
                "maximum_three_scoped_auxiliaries", "citation_audit", "exemplar_leak_check",
                "composition_dependency_freshness",
            ])
        if authority_precondition and court_candidate:
            validators.extend([
                "verified_production_authority", "material_adverse_authority_coverage",
                "authority_research_before_drafting",
            ])
        elif authority_precondition:
            validators.extend(["authority_research_before_drafting", "material_adverse_authority_coverage", "source_verification_status", "citation_audit", "exemplar_leak_check"])
    if task_frame["read_acl"]["mode"] == "restricted":
        validators.append("read_acl_compliance")
    if task_frame["output_scope"]["mode"] == "only":
        validators.append("scope_locator_exists")

    execution_tools_enabled = (
        task_kind != "constraint_ack"
        and clarification["decision"] != "block"
        and (clarification["decision"] != "ask" or clarification.get("continue_read_only") is True)
    )
    allowed_tools = ["local_read"] if execution_tools_enabled else []
    if execution_tools_enabled and task_frame["memory_context_policy"]["read_mode"] != "off":
        allowed_tools.append("case_state_read")
    if execution_tools_enabled and task_frame["network_policy"] == "public_web_if_available":
        allowed_tools.append("verified_public_web_read")
    if execution_tools_enabled and task_frame.get("knowledge_policy") == "configured_readonly_if_available":
        allowed_tools.append("configured_library_read")
    if (
        clarification["decision"] in {"execute", "preview"}
        and task_frame["mutation_scope"] in {"draft_only", "local_write"}
        and "write_file" not in task_frame["must_not"]
    ):
        allowed_tools.append("local_derived_write")
    forbidden_tools = [
        "external_send", "external_submit", "external_upload", "physical_print", "delete_original",
        "preference_auto_activate",
    ]
    if task_frame["network_policy"] == "deny":
        forbidden_tools.append("network")
    if task_frame["mutation_scope"] == "read_only" or recipe == "discuss":
        forbidden_tools.extend(["local_file_write", "case_memory_commit"])
    if task_kind == "constraint_ack":
        forbidden_tools.extend(["local_file_read", "case_state_read", "local_file_write", "case_memory_commit"])

    coverage_rules = [
        {"id": "C-command", "requirement": "Only current command-plane spans may trigger actions."},
        {"id": "C-scope", "requirement": "Output must stay inside issue/output scope and declared read ACL."},
        {"id": "C-source", "requirement": "Material facts and legal propositions retain source and status."},
        {"id": "C-gates", "requirement": "Quick or brief presentation never removes legal or external-action gates."},
    ]
    if recipe == "deep":
        coverage_rules.append({
            "id": "C-deep",
            "requirement": "Problem tree, evidence coverage, contradictions, adverse path, and independent review are all present.",
        })

    decision_to_status = {
        "execute": "ready", "preview": "preview_ready",
        "ask": "awaiting_clarification", "block": "blocked",
    }
    human_review_points: list[str] = []
    if task_kind == "generate" and is_multi_generation:
        human_review_points.append("generation_object_disambiguation")
    elif task_kind == "generate" and is_client_email:
        human_review_points.append("outgoing_communication_review")
    elif task_kind == "generate":
        human_review_points.append("independent_draft_review")
        if court_candidate:
            human_review_points.extend(["G1_strategy", "G2_evidence"])
        if authority_precondition:
            human_review_points.append("material_authority_conflict_review")
    if task_kind in {"sanitize", "package", "approve"}:
        human_review_points.append("applicable_object_gate")
    if recipe == "deep":
        human_review_points.append("independent_analysis_review")

    primary_skill = resolved.get("route", {}).get("skill")
    if (
        clarification["decision"] == "block"
        or task_kind == "constraint_ack"
        or is_multi_generation
    ):
        primary_skill = None
    stop_conditions = [
        "requested_outcome_completed", "one_clarification_required", "human_gate_reached",
        "scope_or_source_prerequisite_missing", "declared_capability_degradation",
    ]
    current_input_only = task_frame.get("read_acl", {}).get("items") == ["current_user_input"]
    required_inputs = ["task_frame", "permission_and_gate_metadata"]
    if (
        not current_input_only and task_kind != "constraint_ack"
        and task_frame.get("stage") != "unknown"
    ):
        required_inputs.append("focus_context")
    if task_frame["memory_context_policy"]["read_mode"] != "off":
        required_inputs.append("bounded_matter_context")
    if task_frame["memory_context_policy"]["read_mode"] == "file_scoped":
        required_inputs.append("resolved_memory_read_scope")
    if task_frame["target_refs"]:
        required_inputs.append("versioned_target_refs")
    if task_frame.get("template_refs") and court_candidate:
        required_inputs.extend(["registered_template_profiles", "versioned_template_refs"])
    elif task_frame.get("template_refs") or task_frame.get("input_materials"):
        required_inputs.append("task_material_records")
    if authority_precondition:
        required_inputs.append("verified_authority_catalog" if court_candidate else "source_ledger_with_verification_status")
    if task_frame["output_scope"]["mode"] == "only":
        required_inputs.append("resolved_scope_locator")

    research_phase = "research_public_authorities" if task_frame["network_policy"] == "public_web_if_available" else "read_authorized_authority_materials"
    draft_phase = "draft_in_registered_template_slots" if court_candidate else "draft_from_task_materials"
    workflow_phases = (
        [
            "freeze_document_target",
            "resolve_template_roles",
            "lock_material_issues",
            "analyze_both_sides",
            research_phase,
            "verify_supporting_and_material_adverse_authorities",
            "bind_claims_to_sources",
            draft_phase,
            "audit_citations",
            "check_exemplar_leaks",
            "independent_review",
            "visual_qa",
        ]
        if task_kind == "generate" and authority_precondition
        else ["execute_primary_work", "validate_output", "human_gate_or_complete"]
    )
    return {
        "schema_version": LANGUAGE_SCHEMA_VERSION,
        "id": f"RS-{uuid.uuid4().hex[:16]}",
        "recipe": recipe,
        "task_type": task_kind,
        "primary_skill": primary_skill,
        "execution_status": decision_to_status[clarification["decision"]],
        "required_inputs": required_inputs,
        "required_artifacts": list(dict.fromkeys(required_artifacts)),
        "allowed_tools": list(dict.fromkeys(allowed_tools)),
        "forbidden_tools": list(dict.fromkeys(forbidden_tools)),
        "capability_requirements": [
            "local_files" if "local_read" in allowed_tools else "none",
            *( ["public_web"] if "verified_public_web_read" in allowed_tools else [] ),
        ],
        "coverage_rules": coverage_rules,
        "validators": list(dict.fromkeys(validators)),
        "stop_conditions": stop_conditions,
        "human_review_points": list(dict.fromkeys(human_review_points)),
        "completion_contract": {
            "deliverable": task_frame["output_stage"],
            "must_report": ["scope_used", "sources_used", "assumptions", "unresolved_items", "gate_status"],
        },
        "workflow_phases": workflow_phases,
        "external_actions_allowed": False,
        "case_memory_commit_allowed": False,
        "preference_memory_commit_allowed": False,
        "verification_result": None,
    }


def extract_preference_candidate(
    task_frame: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the inert compatibility envelope for the future learning module.

    Language interpretation in this package never learns, proposes, promotes,
    or stores aliases/preferences.  The field remains in the public response so
    existing callers do not break when that separate module is introduced.
    """
    return {
        "schema_version": LANGUAGE_SCHEMA_VERSION,
        "status": "none",
        "kind": None,
        "scope": None,
        "key": None,
        "value": None,
        "source_span": None,
        "explicit": False,
        "confidence": 0.0,
        "requires_user_approval": False,
        "promotion_eligible": False,
        "promotion_block_reason": "learning_disabled_in_language_module",
        "expected_state_version": None,
        "case_memory_write_requested": False,
    }


def compile_language_control(
    text: str,
    resolved: dict[str, Any],
    state: dict[str, Any] | None = None,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return all transient control-plane objects for one route result."""
    task_frame = interpret_task(text, resolved, state, prepared)
    preference_candidate = extract_preference_candidate(task_frame, state)
    clarification = decide_clarification(task_frame, resolved)
    run_spec = compile_run_spec(task_frame, clarification, resolved)
    return {
        "task_frame": task_frame,
        "clarification": clarification,
        "run_spec": run_spec,
        "preference_candidate": preference_candidate,
    }


def validate_language_control_semantics(control: dict[str, Any]) -> list[str]:
    """Validate cross-object safety invariants that JSON Schema cannot express."""
    errors: list[str] = []
    task_frame = control.get("task_frame", {})
    clarification = control.get("clarification", {})
    run_spec = control.get("run_spec", {})
    preference = control.get("preference_candidate", {})

    if not all(isinstance(item, dict) for item in (task_frame, clarification, run_spec, preference)):
        return ["language control bundle must contain four object contracts"]

    allowed_tool_names = {
        "local_read", "case_state_read", "verified_public_web_read", "local_derived_write", "configured_library_read",
    }
    allowed_tools = set(run_spec.get("allowed_tools", []))
    unknown_allowed = allowed_tools - allowed_tool_names
    if unknown_allowed:
        errors.append(f"run_spec.allowed_tools contains non-whitelisted tools: {sorted(unknown_allowed)}")
    if run_spec.get("external_actions_allowed") is not False:
        errors.append("run_spec.external_actions_allowed must remain false")
    if run_spec.get("case_memory_commit_allowed") is not False:
        errors.append("run_spec.case_memory_commit_allowed must remain false during route compilation")
    if run_spec.get("preference_memory_commit_allowed") is not False:
        errors.append("run_spec.preference_memory_commit_allowed must remain false during route compilation")

    expected_status = {
        "execute": "ready", "preview": "preview_ready",
        "ask": "awaiting_clarification", "block": "blocked",
    }.get(clarification.get("decision"))
    if expected_status != run_spec.get("execution_status"):
        errors.append("clarification.decision and run_spec.execution_status disagree")
    if clarification.get("decision") == "ask":
        if clarification.get("requires_user_input") is not True or not clarification.get("question"):
            errors.append("ask requires user input and exactly one non-empty question")
    else:
        if clarification.get("requires_user_input") is not False or clarification.get("question") is not None:
            errors.append("non-ask clarification must not carry a pending question")
    if clarification.get("risk_tier") == "R3" and clarification.get("decision") != "block":
        errors.append("R3 must be blocked by the route compiler")

    task_kind = task_frame.get("task_kind")
    if run_spec.get("task_type") != task_kind:
        errors.append("run_spec.task_type must equal task_frame.task_kind")
    if task_frame.get("network_policy") == "deny" and "verified_public_web_read" in allowed_tools:
        errors.append("network-denied task cannot allow verified_public_web_read")
    if task_frame.get("knowledge_policy") == "off" and "configured_library_read" in allowed_tools:
        errors.append("knowledge-disabled task cannot allow configured_library_read")
    memory_policy = task_frame.get("memory_context_policy", {})
    if memory_policy.get("read_mode") == "off" and "case_state_read" in allowed_tools:
        errors.append("memory-off task cannot allow case_state_read")
    if memory_policy.get("read_mode") == "off" and memory_policy.get("read_scope"):
        errors.append("memory-off task must have an empty read_scope")
    if (
        memory_policy.get("read_mode") == "file_scoped"
        and run_spec.get("execution_status") in {"ready", "preview_ready"}
        and not memory_policy.get("read_scope")
    ):
        errors.append("executable file_scoped memory requires a non-empty read_scope")
    if task_frame.get("mutation_scope") == "read_only" and "local_derived_write" in allowed_tools:
        errors.append("read-only task cannot allow local_derived_write")
    conflicts = set(task_frame.get("conflicts", []))
    if "generation_explicitly_negated" in conflicts and task_kind == "generate":
        errors.append("generation_explicitly_negated cannot compile as generate")
    if "research_explicitly_negated" in conflicts and task_kind == "research":
        errors.append("research_explicitly_negated cannot compile as research")
    if "lifecycle_action_explicitly_negated" in conflicts and task_kind in {"manage", "resume"}:
        errors.append("lifecycle_action_explicitly_negated cannot compile as lifecycle mutation")
    if "local_mutation_explicitly_negated" in conflicts and task_kind in {
        "modify", "sanitize", "package", "record",
    }:
        errors.append("local_mutation_explicitly_negated cannot compile as local mutation")

    if task_frame.get("read_acl", {}).get("mode") == "restricted":
        if run_spec.get("execution_status") in {"ready", "preview_ready"} and not task_frame["read_acl"].get("items"):
            errors.append("executable restricted read ACL must resolve to at least one concrete item")

    required_artifacts = set(run_spec.get("required_artifacts", []))
    validators = set(run_spec.get("validators", []))
    if run_spec.get("recipe") == "deep":
        deep_contract = {
            "problem_tree", "source_ledger", "claim_evidence_matrix", "contradiction_register",
            "strongest_adverse_path", "independent_review",
        }
        if not deep_contract <= required_artifacts:
            errors.append("deep recipe is missing mandatory analysis artifacts")
    if run_spec.get("recipe") == "discuss":
        if not {"no_finalization", "no_file_write", "no_case_memory_commit"} <= validators:
            errors.append("discuss recipe is missing non-finalization validators")
        if "local_derived_write" in allowed_tools:
            errors.append("discuss recipe cannot write derived files")
    if task_kind == "generate" and task_frame.get("object_hint") == "multiple_generation_objects":
        if clarification.get("decision") != "ask" or allowed_tools:
            errors.append("multiple generation objects require clarification before any tool dispatch")
        if "generation_object_disambiguation" not in validators:
            errors.append("multiple generation objects require a disambiguation validator")
        if run_spec.get("primary_skill") is not None:
            errors.append("multiple generation objects cannot dispatch a specialist before clarification")
    elif task_kind == "generate" and task_frame.get("object_hint") != "client_email":
        required_gates = {"independent_draft_review", "source_and_assumption_labels"}
        if task_frame.get("delivery_audience") == "court_candidate":
            required_gates.update({"G1_strategy", "G2_evidence"})
        if not required_gates <= validators:
            errors.append("document generation is missing checks required by its delivery audience")
        if task_frame.get("authority_research_precondition") is True:
            required = {
                "authority_research_before_drafting", "material_adverse_authority_coverage",
            }
            if task_frame.get("delivery_audience") == "court_candidate":
                required.update({"verified_production_authority", "citation_audit", "exemplar_leak_check"})
            else:
                required.add("source_verification_status")
            if not required <= validators:
                errors.append("authority-backed generation is missing composition/research validators")
            phases = run_spec.get("workflow_phases", [])
            research = "research_public_authorities" if task_frame.get("network_policy") == "public_web_if_available" else "read_authorized_authority_materials"
            draft = "draft_in_registered_template_slots" if task_frame.get("delivery_audience") == "court_candidate" else "draft_from_task_materials"
            if not (
                research in phases and draft in phases
                and phases.index(research) < phases.index(draft)
            ):
                errors.append("authority research must precede drafting in workflow_phases")
    if task_kind == "generate" and task_frame.get("object_hint") == "client_email":
        if not {"recipient_and_purpose_frozen", "outgoing_communication_review", "no_external_send"} <= validators:
            errors.append("client email generation is missing communication-specific gates")
        if {"G1_strategy", "G2_evidence"} & validators:
            errors.append("client email must not be silently compiled as a litigation pleading")

    if (
        preference.get("status") != "none"
        or preference.get("promotion_eligible") is not False
        or preference.get("requires_user_approval") is not False
    ):
        errors.append("language control cannot emit or promote preference-learning candidates")

    if task_kind == "external_action":
        if clarification.get("decision") != "block" or run_spec.get("primary_skill") is not None:
            errors.append("external action must be blocked before specialist dispatch")
        if allowed_tools:
            errors.append("blocked external action cannot receive execution tools")
    if task_kind == "constraint_ack":
        if memory_policy.get("read_mode") != "off" or memory_policy.get("write_mode") != "none":
            errors.append("control-only acknowledgement cannot read or write case memory")
        if allowed_tools or run_spec.get("primary_skill") is not None:
            errors.append("constraint acknowledgement cannot dispatch tools or a specialist")
    return errors
