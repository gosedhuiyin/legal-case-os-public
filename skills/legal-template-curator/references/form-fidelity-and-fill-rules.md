# Form Fidelity and Fill Rules

These fidelity checks also apply to a task-local uploaded DOCX. Permanent catalog registration is not a prerequisite for a one-off internal draft; map the selected file's actual fields and permitted edits in a temporary task record. Existing production FillPlan/structural commands retain their registered-profile requirements. If that command cannot safely handle the source, use a supported document-editing route and disclose the extent of fidelity actually checked.

## Slot classifications

Classify every editable cell, paragraph, or repeatable row as exactly one of:

- `fixed_locked`: fixed text or layout; never fill or rewrite;
- `required_verified`: must be supplied from a direct verified source;
- `conditional_verified`: fill only when its registered condition is established;
- `optional_verified`: fill only when a reliable value exists; absence is not an error;
- `derived_needs_confirmation`: may be inferred, but must appear on the consolidated confirmation list;
- `lawyer_decision_required`: fee method/amount, general or special authority, settlement, withdrawal, waiver, or comparable legal decision; never infer as final;
- `manual_blank`: signature, seal, on-site date, approval opinion, or other manual field; generation must leave it blank;
- `repeatable`: a registered standard row/block rule with an exact prototype subtree hash, insertion anchor, item slots, and min/max count. The TEST-ONLY structural engine can exercise this contract; production use remains blocked until a real profile is calibrated and approved.

Every `FillPlan` entry must record the proposed value or intentional blank, source locator or rule, confidence/basis, and approval state. A missing source is not cured by plausible inference. Conflicting sources trigger one consolidated high-risk question; harmless optional blanks remain blank without interruption.

## Format preservation

Generate from the actual selected DOCX and edit only the stable text regions justified by current instructions and sources; when using a registered production profile, preserve its approved mutation set. Do not recreate the form from Markdown. Preserve table geometry and merges, widths, row heights, paragraph/run styling, images, headers, footers, sections, page-number fields, and signature/seal locations. A production row clone stops unless the exact prototype row, allowed item slots, insertion anchor, count limits, transform-aware structural diff, and page render have all been registered and approved; TEST-ONLY success is not approval.

After filling:

1. compare original and derivative OOXML structure outside the approved mutation set;
2. verify every filled value and intentional blank; list unresolved fields separately for internal review. A court candidate must not contain unresolved required fields, internal markers or unapproved document residue;
3. render the generated form and inspect every page for blank pages, clipping, overlap, missing glyphs, pagination, table drift, and signature-area damage;
4. test normal, blank, and long-value boundaries before activating a reusable form profile.

## Legacy `.doc`

Never overwrite a legacy source. Convert through the supported LibreOffice short-path workflow to a learning-only `.docx`, record original and derivative SHA-256 values and the conversion tool/version, then compare page count and rendered layout. A failed or materially shifted conversion blocks `fillable_clone`; retain the `.doc` as the immutable source and require manual normalization.
