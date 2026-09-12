# Intent and Focus Resolution

For ambiguous, compound, negated, scoped, or changing expressions, first apply
[task interpretation and clarification](task-interpretation-and-clarification.md).
Its transient `TaskFrame` preserves candidates, source spans, conflicts, and
unknown language. Freeze only the selected business meaning into
`IntentEnvelope`; attach the executable depth, permissions, validators, and stop
conditions to `RunSpec`.

## IntentEnvelope

Record:

- `action`: `查看 | 总结 | 分析 | 检索 | 生成 | 修改 | 清洁 | 组卷 | 批准 | 召回 | 记录 | 办理 | 恢复`;
- `object`: `案件 | 材料 | 材料批次 | 争点 | 证据 | 文书 | 模板 | 提交包 | 案件记忆 | 期限 | 工单 | 潜在案件`;
- `reasoning_depth` and independently `display_length`;
- `audience`: discussion, internal_review, or court_candidate;
- `output_formats`: chat, markdown, docx, or pdf;
- `template_alias`, `read_scope`, `network_mode`, and `modify_scope`;
- `memory_mode`: `off | file_scoped | relevant | reflect`;
- `read_memory_scope[]`: resolved source, directory, object, issue, workflow, or event IDs permitted for memory retrieval;
- `write_memory_mode`: `none | candidate | commit`;
- `scope_lock` for the exact issue IDs an analysis may cover, and `analysis_lens` (`neutral` or `strongest_adverse_path`).

Keep public `network_mode` separate from `knowledge_policy`. Intake/preprocessing need not start public searches; an authorized research task may use `public_web_if_available` unless prohibited. An authorized user-configured read-only knowledge base remains available within its existing scope without asking again. New proprietary services or credentials require their own authorization. Follow [network degradation](../../../shared/policies/network-degradation.md) and distinguish inaccessible backend from no results.

Default `memory_mode=relevant` only for an identified existing matter. For a detached document or expressly memory-free request, use `off`. Default inferred associations and reflections to `write_memory_mode=candidate`; never interpret `commit` as permission to bypass a verification or approval gate.

## Common speech

The table below contains stable route examples, not a self-learning phrase list.
New aliases must first pass the versioned language-control regression contract;
one recent utterance cannot rewrite these rules.

| User language | Resolution |
|---|---|
| “看一下这个材料”“大致内容是什么” | intake/preprocess as needed, then concise source-linked summary |
| “不要那么复杂，告诉我主要问题” | short presentation; retain any required deep issue internally |
| “只分析时效这个争点，其他先不展开” | resolve an exact issue ID into `scope_lock`; never silently expand to the whole matter |
| “站在对方角度看” | set `analysis_lens=strongest_adverse_path` and expose the opponent's best route, evidence gap, and outcome-changing fact |
| “这个事你了解得怎么样” | current-status summary: coverage, known core, blockers, and next gate |
| “又来了一批材料”“看看这些新材料有没有影响” | create or resolve one stable `MaterialBatch`; mechanical intake first, then `legal-matter-delta-intake` with `memory_mode=relevant` |
| “调用下记忆模块，看下有没关联或启发，下一步怎么做” | `action=召回`, `object=案件记忆`, `memory_mode=reflect`, `write_memory_mode=candidate`; return bounded, sourced insight candidates and next checks |
| “这段不用关联记忆了，直接××” | `memory_mode=off`, `write_memory_mode=none`; use only the current prompt and explicit inputs, while retaining control/gate checks |
| “不要管其他记忆，只看和××文件地址有关的记忆” | resolve the exact source path/hash, set `memory_mode=file_scoped` and `read_memory_scope[]`; do not silently broaden by similarity |
| “本轮可以读记忆，但不要写入” | keep the resolved read mode and set `write_memory_mode=none` |
| “把这个结论记成候选，先不要改变案件状态” | `action=记录`, `write_memory_mode=candidate`; no canonical semantic mutation |
| “把这条法院要求正式记入案件” | `action=记录`, `write_memory_mode=commit`; commit a source-linked event only after identity/authority validation, and keep an extracted date as candidate until verified |
| “法院说要撤诉，直接做手续”“调取审计报告” | `action=办理`, `object=工单`; route to `legal-case-lifecycle-manager` with a minimal procedural capsule |
| “继续上次那个工单”“审计报告拿到了，继续” | `action=恢复`; resume only one current, version-matching workflow whose wait condition is met |
| “客户虽然坚持提交” | record the instruction event without changing legal relevance or submission status; route the decision to `legal-evidence-gate` |
| “帮我找支持案例” | authority research for supporting and material adverse results; never hide the latter |
| “按××套件模板生成××文件”“依照××号生成诉状和手续” | resolve the exact supplied file or named asset; use it task-locally when authorized, otherwise resolve a current catalog/library source; load only requested components; default ordinary generation to an internal editable draft |
| “只改这一段/这个数字” | set an explicit modification scope and protect everything outside it |
| “去掉不能给法院看的内容、备注和水印” | review first; substantive language returns to drafting/evidence gate, while approved non-substantive residue follows derived-copy sanitization → report → review |
| “顺便做手续、整理文件夹” | prepare only the requested document or organization result; do not infer a filing package or print sheet unless requested; never move/delete originals or submit |

## FocusContext

A detached task may resolve a unique supplied template, requested document and input_materials without persistent FocusContext. Record the delivery audience separately from complexity: ordinary generation is internal_review, explicit formal court use is court_candidate, and package approval is separate. Source-based current-task learning and confirmed reusable assets follow [material access and learning](../../../shared/policies/material-access-and-learning.md).

Track current matter, proceeding branch, material batch, source, event, issue, artifact, workflow, task, template, output mode, read/write/network scope, memory modes, recently displayed candidates, and the exact pending approval object.

Resolve “这个”“刚才那个” only when one unchanged object is salient within the same matter and stage. Otherwise ask a narrow clarification; do not guess across matters, stages, or similarly named versions.

Resolve “ok/可以/就这样” as approval only when:

1. exactly one pending object was just displayed as awaiting approval;
2. its ID/version/hash and relevant inputs have not changed;
3. the requested gate and consequences were stated;
4. the speaker is authorized for that decision.

If no object, multiple objects, a stale candidate, or a crossed stage exists, do not approve. Use `approve-ok` in [the local CLI](../../../scripts/legal_case_os.py) to enforce this rule.

“很简单” requests the standard track and shorter presentation. It cannot clear a recorded deep trigger. “不要复杂” never reduces internal reasoning depth by itself.

## Memory-scope resolution

- `off` prohibits loading substantive case memory; minimum permission, identity, and gate metadata may still be checked. Do not claim the result reflects the whole matter.
- `file_scoped` loads only resolved files and directly linked records. If a safe court-facing conclusion needs broader review, preserve the user's scope and stop before the affected gate rather than expanding silently.
- `relevant` loads a bounded task capsule, including material counterevidence and unresolved deadlines. Record an input manifest and what categories were omitted.
- `reflect` permits bounded cross-batch association and contrary-evidence review, but its output remains candidate-only until separately committed or approved.

Before closing a turn, distinguish the conversational answer, the candidate memory delta, the canonical commit, and any approval request. Never imply that producing one performed the others.
