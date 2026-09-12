# Workflow, Run, and Procedural Capsule

## Durable objects

### WorkflowInstance

A workflow is the long-lived legal business item. Record:

- matter and proceeding-branch IDs, workflow type, title, trigger-event ID, and responsible owner;
- status `pending`, `in_progress`, `waiting_external`, `waiting_approval`, `completed`, `cancelled`, or `failed`;
- prerequisites, task IDs, dependency links, current deadline IDs, required gates, and intended outcome;
- `waiting_for`, `resume_condition`, last successful event/run, current output IDs, version, and stale state.

Do not declare completion from a generated draft alone. Link completion to an appropriate event or result, such as a lawyer-approved withdrawal candidate, verified receipt, received audit report, or recorded court response.

### Task

A task is one business step under a workflow. It needs an owner, status (`pending`, `in_progress`, `waiting_external`, `waiting_approval`, `done`, `blocked`, `cancelled`, or `failed`), dependencies, due date if any, `waiting_for`, `resume_condition`, output IDs, and the event that proves completion. Claim or update it atomically. Completed tasks remain archived and queryable.

### RunAttempt

A run is one bounded AI/script execution. Record its input snapshot, memory/read scope, write mode, status (`pending`, `running`, `completed`, `failed`, or `killed`), commit status, output IDs, error/degradation, and cursor. Retrying creates a new run; it does not overwrite the failed attempt or imply the workflow succeeded.

## ProceduralCapsule

Load only the minimum needed for the current workflow:

- matter identity, parties/role, court, case number, proceeding branch, and production stage;
- exact triggering instruction/event with source, locator, authority/channel, and verification state;
- verified and candidate dates, applicable gate, required forms, current outputs, approvals, and blockers;
- workflow tasks, dependencies, waiting state, resume condition, and recent action/result events;
- only the substantive facts or evidence expressly declared necessary for this procedure.

The capsule is a derived artifact with input IDs/versions and `as_of_event_seq`. Rebuild it when an input changes; never use it to overwrite canonical records.

## Typical routing

| Request | Default path | Escalate when |
|---|---|---|
| “法院说要撤诉，做手续” | event → withdrawal workflow → procedural capsule → drafting/review/package candidate | refiling, limitation, costs, related claims, or authority is uncertain |
| “调取审计报告” | workflow → request/coordination task → `waiting_external` → resume on receipt | scope, privilege, confidentiality, completeness, or evidentiary use is disputed |
| “查看补充证据” | new batch → delta triage → impact/evidence route | a material fact, issue, deadline, or approved artifact may change |
| “做财产保全” | workflow → prerequisites/security/deadline capsule → candidate forms | amount, asset clues, security, urgency, forum, or liability basis is uncertain |
| “继续上次那个” | resume only one uniquely matching current workflow and version | multiple candidates, changed inputs, expired deadline, or stale output exists |

## Waiting and recovery

- While waiting, schedule at most a bounded read-only check or reminder. A timer never authorizes an external message or filing.
- Resume from canonical workflow state and input/output snapshots, not from conversational prose.
- If the prior run ended mid-tool call, has no durable output, or its cursor is inconsistent, mark it interrupted and retry from the last successful transaction boundary.
- Queue new material events while a workflow transaction is active; after commit, evaluate whether they satisfy the resume condition or invalidate an output.
