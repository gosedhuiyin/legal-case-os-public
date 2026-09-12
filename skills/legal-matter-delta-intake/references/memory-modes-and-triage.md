# Case-Memory Modes and Delta Triage

## Storage model

Keep case-specific memory in the matter's `_case-state/` area:

- `case-state.json`: the only mutable current truth, including `MemoryState` cursors and derived-view pointers;
- `audit-log.jsonl`: append-only transaction receipts;
- derived current view, topic indexes, context capsules, and search indexes: short, replaceable, and rebuildable from canonical records and original sources;
- `RunAttempt` logs: execution evidence, not case facts.

Global memory may contain only stable personal style and process preferences. It must not contain client facts, evidence contents, case strategy, deadlines, or current procedural status. A conversation is a work surface, not a competing memory store.

## Read modes

Record the mode and the exact loaded/omitted object manifest in the run snapshot.

| `memory_mode` | Load | Do not do |
|---|---|---|
| `off` | Current prompt, explicit attachments/paths, and the minimum control metadata needed to enforce permissions and gates | Load substantive case memory or claim whole-matter completeness |
| `file_scoped` | Sources in `read_memory_scope[]`, their versions, direct derivatives, and directly linked records; obey an explicit stricter scope | Expand by semantic similarity into unrelated records |
| `relevant` | Short `CurrentCaseView`, the target workflow/object, directly related facts/events/issues, material counterevidence, and unresolved deadlines, within a recorded budget | Dump the whole case or omit known adverse material merely because it is inconvenient |
| `reflect` | Relevant-mode inputs plus bounded cross-batch events, unresolved tensions, dormant seeds, superseded views, and contrary evidence | Treat brainstorming as an approved conclusion or silently rewrite canonical state |

`relevant` is the default for ordinary case-connected work. “这段不用关联记忆” selects `off`. “只看和这个文件地址有关的记忆” selects `file_scoped` and puts the resolved source IDs in `read_memory_scope[]`. “调用记忆模块，看有无关联或启发” selects `reflect`.

When a narrow mode cannot support a safe court-facing conclusion, preserve the user's read boundary, mark the output limited-scope, and stop before the affected approval or filing gate. Do not silently broaden the scope.

## Write modes

| `write_memory_mode` | Effect |
|---|---|
| `none` | Do not add or change semantic case records. A strictly necessary technical run/audit receipt may record scope and outcome status, not new substantive content. |
| `candidate` | Return a `MemoryDeltaCandidate`; do not change canonical facts, evidence, strategy, deadlines, or workflows. |
| `commit` | Validate and atomically write only eligible source-linked records, then append the audit receipt. Required legal approvals remain separate. |

If the user has not clearly authorized semantic persistence, default to `candidate` for inference, impact, and reflection results. Directly requested case administration may use `commit` for mechanical batch/event/workflow records, but an inferred fact, calculated deadline, submission decision, strategy change, or new matter still requires its own verification or gate.

## Delta objects

- `MaterialBatch`: one bounded arrival transaction, with source IDs, received time/channel, deduplication identity, stable-processing boundary, status, and committed cursor.
- `CaseEvent`: a source-linked occurrence or instruction; keep `occurred_at`, `received_at`, and `recorded_at` distinct and preserve uncertainty.
- `MaterialTriageCard`: one material's relevance and one primary `action_route`: `no_action`, `deadline_only`, `current_matter_analysis`, `evidence_review`, `workflow`, `prospective_matter`, or `human_review`; record secondary routes separately.
- `DeadlineRecord`: status `candidate`, `verified`, `dismissed`, `completed`, or `superseded`; retain the exact original wording and locator, issuing authority/channel, received time, timezone, computation basis, and verifier. AI extraction alone creates only `candidate`.
- `ImpactAssessment`: batch/source IDs, affected and stale object IDs, counterevidence IDs, impact level, deep-analysis flag, candidate/applied/rejected status, and lawyer-review state. It cannot mutate dependents by itself.
- `ProspectiveMatterSeed`: status `dormant`, `candidate`, `approved`, `promoted`, `rejected`, or `stale`, with source/event links, support, counterevidence, decision, and origin/promotion links. Promotion creates a separately approved matter; it never happens from triage alone.

`CurrentCaseView` and `ContextCapsule` are derived artifacts with an `as_of_event_seq` and input manifest. They summarize canonical records but never override them.

## Triage safeguards

1. Preserve a low-relevance item in the inventory; do not fabricate a legal use.
2. Route a date independently from the rest of an otherwise unimportant document.
3. Record “客户坚持提交” as a source-linked client instruction while leaving legal relevance and the AI recommendation unchanged; only `legal-evidence-gate` can advance a submission status after lawyer decision.
4. Mark only declared downstream dependents stale when a material change is committed.
5. If a cursor, source version, or prior input snapshot is missing or inconsistent, reconcile or stop. Never guess that no change occurred.
