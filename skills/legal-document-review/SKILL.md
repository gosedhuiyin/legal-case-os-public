---
name: legal-document-review
description: Review a Chinese legal draft or cleaned derivative against its actual sources for factual support, attribution, logic, admissions, consistency, staleness, old-case residue and layout. Scale checks to internal drafts versus explicit court candidates; use independent review for formal or complex work. Do not silently rewrite the reviewed source or analyze opponent filings as though they were the user's final draft.
---

# Legal Document Review

Review the exact artifact and its actual input records. A detached `internal_review` draft may use task-local sources without case-state or permanent template registration. For a persistent/formal matter, check the relevant current case-state and gate bindings. Apply [material access and learning](../../shared/policies/material-access-and-learning.md), [execution recipes](../../shared/policies/execution-recipes.md) and [human approval](../../shared/policies/human-approval.md).

Scale review to the outcome. A simple internal draft needs source/field consistency, meaningful risk checks, old-case leakage and necessary layout inspection; absent G1/G2/G4 alone does not block its delivery. An explicit court candidate needs current strategy and evidence gates, verified formal citations, independent substantive review and clean outward prose. A single candidate does not need G4 until it becomes part of an exact submission package.

For complex or formal work, use a fresh reviewer context when available, passing only the exact artifact, relevant source/state slice and task. Otherwise separate the review pass and state its independence limit. `preflight` in [the local CLI](../../scripts/legal_case_os.py) inspects file-level traces; it does not prove legal or strategic correctness.

Verify each used template/example by exact source identity/hash and permitted task use. Check applicable field/structure rules or a current approved profile; require a full CompositionSpec only where the chosen complex production workflow uses it. Apply `composition-validate`, `citation-audit` and `exemplar-leak-check` to their supported inputs; do not report a pass for an unexecuted or inapplicable validator.

Distinguish a useful observable method or attributed old-case opinion from current facts, adopted conclusions and verified authority. Confirm that reused learning still fits current premises and has not introduced old-party details. Missing backend access limits coverage; it does not prove the library contains no adverse material.

Return the material findings with exact artifact/source locations, separating blocking defects, lawyer decisions and optional wording improvements. Keep the report proportional: a brief findings list can suffice for a simple internal draft; complex/formal work uses the fuller [review contract](references/review-contract.md).

Never overwrite the reviewed artifact. Authorized corrections produce a new candidate and a scoped re-review. A court candidate must contain no internal ledgers, profile records, risk notes, AI commentary, unresolved placeholders or unapproved document residue; preserve these in separate internal records.

For requested residue/watermark cleaning, follow [sanitization review](references/sanitization-review.md) and [original protection](../../shared/policies/sanitization-and-originals.md). The existing SANITIZE command's exact approval requirements remain; it is not required merely because an ordinary new internal draft is being generated.
