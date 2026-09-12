# Initial and Continuing Case Flows

The table below is the formal/persistent matter flow, not a prerequisite for every requested document. For ordinary generation, use [execution recipes](../../../shared/policies/execution-recipes.md): selected template/current materials → necessary field/risk check → draft → source/residue/layout review → editable internal_review delivery. Complex work adds targeted research/comparison and independent review; only the requested formal transition enters the corresponding gates.

| Stage | Specialist or output | Control |
|---|---|---|
| Inventory | `legal-material-intake` | Originals unchanged; source hash and coverage known |
| Derivation when needed | `legal-material-preprocessing` | OCR/transcription/translation failures remain visible |
| Preliminary view | `legal-case-analysis` standard mode | Short 1–4 point answer by default; deep flags preserved |
| Minimum verification | `legal-authority-research` plus `legal-evidence-gate` red-flag scan | No strategy approval on an unverified controlling premise |
| Strategy | Exact theory, relief/defense, and hardest issue candidate | `G1_strategy` |
| Research | Supporting and material adverse authorities | Material change reopens G1 |
| Evidence decision | Stage-specific candidate table | `G2_evidence`; AI cannot approve |
| Draft | `legal-document-drafting` | Conditional `G3_draft_plan` for complex/risky/long work |
| Independent review | `legal-document-review` | Stop on blocking defects |
| Compression if requested | `legal-draft-compressor`, then review again | Meaning and protected propositions preserved |
| Cleaning if requested | review → `SANITIZE` approval → derived copy → report → review again | Never clean originals or third-party evidence authenticity marks |
| Candidate package | `legal-filing-packager` | Review-passed artifacts and current G2 evidence only |
| Final candidate snapshot | Exact package manifest/hash | `G4_final` |
| External act | Print/send/upload/serve/file | Out of scope without separate `G5_external_action` |

Internal draft delivery may proceed without simulated or real G1/G2/G4. Explicit court candidates require current G1 and applicable G2; exact packages require G4. Reuse valid existing approvals whose inputs remain unchanged, and never perform an external act.

Role ownership stays explicit: specialist Skills are responsible for candidate work and checks; the orchestrator is responsible for state consistency and routing; the lawyer is accountable for strategy, evidence, sanitization, and final-package decisions; verified authorities and original sources are consulted records. No Skill is accountable for an external filing act.

## Incremental material flow

Use the following persistent-state flow only when ongoing matter management is actually requested. Receiving a file for one detached draft does not itself create a batch, deadline, workflow or memory commit.

Use this flow for a stable batch received after the matter already exists:

`MaterialBatch → mechanical intake/preprocessing → legal-matter-delta-intake → one material route → validate/commit candidate → rebuild affected derived views`

One material route means:

- no current action: inventory, event, and triage only;
- date: candidate deadline and verification task;
- existing-matter impact: explicit affected IDs and bounded invalidation/analysis;
- evidence: `legal-evidence-gate`, with no submission before G2;
- procedure: create/resume a lifecycle workflow;
- possible new matter: dormant/candidate seed, then lawyer promotion into a separate matter.

Do not re-run the whole case merely because a new source exists. An untriaged current batch may block a current filing package while leaving unrelated historic work available. Cursor/version mismatch, failed processing, or an unstable write stops the transaction without advancing the batch cursor.

## Procedural lifecycle flow

`CaseEvent/request → WorkflowInstance → ProceduralCapsule → task/run → specialist output → gate or waiting_external → resume event → completion proof`

The capsule should normally contain identity, court/case number, branch and stage, exact instruction/source, dates, authority, forms, tasks, approvals, outputs, and blockers—not the whole evidentiary record. Escalate a bounded issue only when it can change the procedure.

A workflow may survive many conversations and failed runs. Resume it from canonical state and snapshots, never from “continue the chat.” Candidate packaging may reach G4; no flow here performs printing, service, sending, upload, login, or filing without separate G5 authority.
