---
name: legal-template-curator
description: Select, inspect and apply Chinese legal templates for the current task, and separately curate approved reusable form or writing profiles. Use for uploaded or named templates, form fields, suites, template learning and multi-input composition. Do not turn prior-case facts or attributed opinions into verified current-case facts or authority, decide lawyer-only fields, or execute filing actions.
---

# Legal Template Curator

Keep source files immutable and follow [material access and learning](../../shared/policies/material-access-and-learning.md). Distinguish current-task use from permanent library registration. A user-selected upload or local template may be inspected, summarized and used now without an active catalog entry, a permanent profile or a complete case-state. Its temporary record does not imply long-term approval.

## Choose the appropriate scope

- **Current task:** bind the exact file/hash, intended use, reusable features and current input fields. Use an existing verified profile when it matches, otherwise make a compact task-local summary. For simple reference drafting, do not create a complete CompositionSpec just to use one template.
- **Reusable library asset:** resolve an exact active catalog/profile version or separately distill and register a new version after the specific reusable content is approved. A file sitting in `00-待登记` is not automatically a permanent asset.
- **Complex composition:** assign primary/auxiliary roles and source bindings when multiple substantive inputs need coordination. A formal court candidate using the production composition commands must satisfy their current state, source and gate contracts.

An authorized user-configured read-only knowledge base may locate templates through `find_template`, source information and original-file reading. Match old SOP profiles by source hash and their own approved version, not by filename. A new knowledge-library version never inherits old profile approval automatically. With sufficient supplied files, skip discovery; backend failures follow [network degradation](../../shared/policies/network-degradation.md).

## Apply the source

For a format-sensitive form, distinguish locked text, verified values, conditional/optional values, lawyer decisions and intentional manual blanks. Preserve the real DOCX and edit only justified regions in a derived copy; inspect structure and rendered pages. Missing high-impact inputs need a targeted decision; harmless optional blanks need no questionnaire.

For reference writing, extract observable structure, style, reasoning methods and conditional, attributed opinions. Recompute their applicability from current materials. Old names, amounts, facts, evidence and outcomes never become current-case facts merely by copying; an attributed prior argument may be compared or adapted after its premises and verification status are made clear.

Use [the local CLI](../../scripts/legal_case_os.py) and its [operator guide](../../docs/OPERATOR-GUIDE.md) for implemented material, draft, learning, profile and fill operations. Do not claim automatic profile approval, semantic learning or format fidelity from a parser or successful command alone. Existing `fillable_clone`, `reference` and `hybrid` production commands retain their documented validation requirements; a blocked mechanical mode does not prevent ordinary task-local reference drafting.

## Completion and boundaries

Deliver the requested temporary template interpretation/fill input to drafting, or the specifically requested reusable candidate. Apply learning in this task immediately; save a new reusable version only after explicit confirmation of its exact contents. Registration and learning do not approve strategy, submitted evidence, a court candidate or a filing package.

Keep profiles, source bindings, risk notes and confirmation records outside outward-facing documents. Upstream changes invalidate the affected profile/fill/composition bindings; retain prior versions. TEST-ONLY calibration proves only synthetic behavior and cannot activate a real mechanical template.

- [Profile contracts](references/profile-contracts.md): permanent distillation, registration and versioning.
- [Form fidelity](references/form-fidelity-and-fill-rules.md): actual DOCX slots, structural edits and legacy conversion.
- [Complex composition](references/complex-composition.md): coordinated multiple inputs and formal CompositionSpec.
- Apply [approval](../../shared/policies/human-approval.md), [source](../../shared/policies/source-and-citation.md), [original protection](../../shared/policies/sanitization-and-originals.md) and [staleness](../../shared/policies/version-and-staleness.md) where applicable.
