# Case-State Transaction Contract

This contract applies to persistent matter-state mutations and formal gate records. A detached internal draft uses task-local material/output records under [material access and learning](../../../shared/policies/material-access-and-learning.md); it does not require creating case-state. A draft record does not claim a formal approval.

## Canonical records

- `case-state.json` is the only mutable source of current truth and must validate against [the schema](../../../shared/schemas/case-state.schema.json).
- `audit-log.jsonl` is append-only proof of events. It never overrides or reconstructs current state by implication.
- Original sources are read-only. Derived files receive new artifact IDs, paths, hashes, input snapshots, and versions.
- Case-specific state and derived memory stay in the matter workspace. Global product memory contains only stable personal style/process preferences, never case facts, evidence, deadlines, strategy, or procedural state. Separately confirmed source-based learning assets follow the shared learning policy and are not a second case-state.
- `CurrentCaseView`, `ContextCapsule`, `ProceduralCapsule`, and search indexes are versioned derived artifacts with input manifests and `as_of_event_seq`; they never become a second truth source.

Use [the local CLI](../../../scripts/legal_case_os.py): `validate` before and after a transaction, `route` for a transient `TaskFrame`, clarification decision, compatible `IntentEnvelope`, and `RunSpec`, `approve-ok` for contextual approval, and `invalidate` when a source or upstream object changes. Compiling a route is read-only. Use only implemented commands; if an incremental or lifecycle command is not installed, return a truthful degradation instead of simulating a commit.

## Continuing-matter records

- `MaterialBatch` bounds one arrival transaction and advances its cursor only after a successful commit.
- `CaseEvent` preserves source, locator, event type, and separate occurrence/receipt/recording times.
- `MaterialTriageCard` records the candidate relevance route without promoting the source.
- `DeadlineRecord` remains `candidate` until the original instruction and applicable calculation are verified; it may later be `verified`, `dismissed`, `completed`, or `superseded` without deleting history.
- `ImpactAssessment` proposes explicit affected IDs and invalidations; it does not mutate them by implication.
- `ProspectiveMatterSeed` is dormant or candidate until lawyer promotion creates a separately identified matter with origin links.
- `WorkflowInstance` is durable legal business with dependencies, waiting/resume conditions, outputs, and completion proof.
- `RunAttempt` is one execution attempt. Failure, interruption, or kill does not complete its workflow.

`Matter.stage` remains a production/procedural posture, not a substitute for workflow status. Completed workflows/tasks are archived rather than erased.

## Child-workflow input

Pass only the necessary slice:

- matter ID, role, procedural stage, forum, objective, and scope locks;
- resolved `IntentEnvelope` and `FocusContext`, including `memory_mode`, exact `read_memory_scope[]`, `write_memory_mode`, and a loaded/omitted input manifest;
- frozen transient `TaskFrame` and `RunSpec`, including command/data spans, issue/output scope, read ACL, mutation scope, recipe, validators, human-review points, and stop conditions;
- relevant source, fact, issue, theory, evidence, authority, template, and artifact IDs with versions;
- current approvals, unresolved gates, processing coverage, and stale flags.

## Child-workflow return

Require:

- IDs and versions read;
- candidate additions or changes;
- source and page/line/timecode locators;
- direct-record versus inference classification;
- assumptions, processing gaps, and unresolved uncertainty;
- downstream dependencies and proposed invalidations;
- next workflow and whether a lawyer decision is required.
- for incremental work: batch/event/triage/deadline/impact/seed candidates and the last successfully committed cursor;
- for lifecycle work: workflow/task/run status, waiting/resume conditions, procedural-capsule inputs, and the event/result required for completion.

## Commit rules

`TaskFrame`, clarification output, and `RunSpec` are execution-control records,
not facts or evidence. They may be attached to a `RunAttempt` or audit details
after the contract is stable, but they do not enter canonical fact/evidence
collections. `preference_candidate` is an inert compatibility envelope fixed at
`status=none`; this language runtime does not create a record for any preference
or alias and never writes one to matter-local case memory. Explicit material-based learning uses its separate task/asset contract; an inert language envelope does not disable that operation.

1. Validate the input snapshot and returned candidate.
2. Reject an update that changes an approved object without a new version and decision.
3. Write the candidate to `case-state.json` atomically.
4. Append an audit event containing actor, time, action, object/version, before/after hash, and reason. An active approval additionally requires one `approval_granted` receipt whose gate, object hash, and canonical `scope_hash` match the Approval.
5. Mark dependent approvals, artifacts, and package manifests stale when an input changed materially.

For `write_memory_mode=none`, do not add or change semantic case records. For `candidate`, return a delta without committing it. For `commit`, write only eligible validated records; inferred facts, calculated deadlines, evidence/submission decisions, strategy changes, and new matters still require their own verification or lawyer gate. When concurrent work changed an input version, reject or rebase the candidate—never let the last conversation silently win.

An approval is usable only when gate, exact object ID, version or hash, procedural stage, status, intended action, canonical scope hash, audit receipt, and current audit-tail state hash all match. Commands that consume approval (`sanitize-docx`, `bundle`, and `print-sheet`) re-run semantic validation themselves.
