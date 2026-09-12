---
name: legal-case-lifecycle-manager
description: Manage long-running procedural work inside an existing Chinese legal matter as resumable workflows, such as withdrawal, preservation, supplemental evidence, audit-report retrieval, court-requested formalities, or “继续上次的工单”. Use when work spans conversations, waits on an external event, or needs deadlines and dependencies. Do not decide substantive evidence or perform filing, sending, printing, or other external actions.
---

# Legal Case Lifecycle Manager

Represent real legal work as a durable `WorkflowInstance`; represent each AI or script execution as a separate `RunAttempt`. A failed, interrupted, or killed run never completes the legal work.

For a lifecycle request:

1. Resolve the exact matter, proceeding branch, triggering `CaseEvent`, and existing workflow. Resume an unchanged workflow when uniquely identified; otherwise create a candidate workflow rather than guessing.
2. Load a minimal `ProceduralCapsule` by default. Do not read the evidentiary record merely because it exists.
3. Identify prerequisites, tasks, dependencies, deadline state, waiting party/event, resume condition, current outputs, and the next gate.
4. Escalate only the smallest substantive issue whose answer could change the procedure; return to the workflow after that issue reaches its defined condition.
5. Save the transaction atomically. Mark a task complete only from a successful result linked to the current inputs; archive completed work instead of deleting it.
6. Stop at a missing prerequisite, ambiguous authority, lawyer gate, `waiting_external`, or failure. Never infer or execute `G5_external_action`.

Read [workflow and procedural capsule](references/workflow-and-procedural-capsule.md) before creating, resuming, waiting, or completing a workflow. Memory access and persistence still follow the `IntentEnvelope` modes defined by `legal-matter-delta-intake`; a procedural request normally uses `memory_mode=relevant` with a capsule-sized scope.

## Non-negotiable boundaries

- `Matter.stage` describes the production posture; it is not a task tracker. Multiple workflows may coexist without rewriting that stage.
- Preserve atomic pairs: source and extracted event, court instruction and deadline candidate, client instruction and lawyer decision, candidate and approval, action and result.
- A court or client request must retain sender/channel, authority, original wording, locator, and verification state. Do not trust an unverified message merely because it arrived in the case folder.
- Withdrawal, preservation, report retrieval, and supplemental filing are not automatically “simple.” Escalate refiling/limitation, amount/security, scope/privilege, admissibility, or contradiction issues when outcome-relevant.
- Packaging may produce a candidate package or print production sheet only. Actual printing, sending, uploading, service, login, or filing remains outside scope without a separately authorized `G5_external_action`.

