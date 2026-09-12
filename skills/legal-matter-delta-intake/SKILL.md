---
name: legal-matter-delta-intake
description: Triage new or fragmented materials in an existing Chinese legal matter, connect them to prior case records with a bounded memory scope, and produce source-linked event, deadline, impact, or prospective-matter candidates. Use for “又来了一批材料”“只看这个文件有关的记忆”“调用记忆看看有没有关联”. Do not replace first-time mechanical intake, decide evidence submission, or perform external actions.
---

# Legal Matter Delta Intake

Process a stable `MaterialBatch`, not an ever-growing folder. Originals are read-only; file discovery, hashing, deduplication, pagination, and OCR remain with `legal-material-intake` and `legal-material-preprocessing`.

For each batch:

1. Resolve `memory_mode`, `read_memory_scope`, and `write_memory_mode` from the `IntentEnvelope` before loading substantive case records.
2. Create source-linked `CaseEvent` and `MaterialTriageCard` candidates. Separate what happened, when it happened, when it was received, and when it was recorded.
3. Route only the material consequence: no current action, deadline candidate, existing-matter impact, evidence evaluation, procedural workflow, or prospective new matter.
4. Return a bounded delta with sources, exact locators, direct-record versus inference labels, affected object IDs, uncertainty, and the next required decision.
5. Commit only through the orchestrator after validation. Advance the batch cursor only after a successful atomic commit; duplicate delivery must be idempotent by source hash and batch identity.

## Non-negotiable boundaries

- Case memory lives inside the matter workspace. `case-state.json` remains canonical; short views, indexes, capsules, and search data are derived and rebuildable. Never store a second authoritative case history in chat or global product memory.
- Do not copy source text or conversation transcripts into memory merely to make them searchable. Store stable IDs, source paths/hashes, locators, relationships, decisions, and narrowly necessary summaries.
- A triage result, weak signal, model inference, or `ProspectiveMatterSeed` is not a fact, evidence decision, strategy approval, or new matter. Promotion requires its own validation and any applicable lawyer gate.
- Low legal relevance does not erase a court date, service fact, client instruction, confidentiality concern, or retention need.
- `write_memory_mode=commit` never bypasses `G1_strategy`, `G2_evidence`, deadline verification, sanitization approval, `G4_final`, or the prohibition on `G5_external_action`.
- Language aliases, formatting/style preferences, collaboration habits, and route-correction feedback are not case events or case facts. Send them only to the separate preference-candidate interface; never write them into `case-state.json` or matter-local memory.
- Do not continuously rescan the whole matter. Queue arrivals during an active transaction, wait for stable writes, coalesce them into a batch, and classify retryable versus permanent failures.

Read [memory modes and triage](references/memory-modes-and-triage.md) whenever case memory may be read or written. Read [weak-signal review](references/weak-signal-review.md) only for `memory_mode=reflect` or a possible new-matter route.
