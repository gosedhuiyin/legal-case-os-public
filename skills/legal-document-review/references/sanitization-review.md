# Sanitization Review Protocol

Use this exact order:

1. Independently review the source artifact and run `preflight`.
2. Propose an exact removal list with item IDs, kinds, locations, reasons, and ownership/authority basis.
3. Obtain a `SANITIZE` lawyer approval for those exact items.
4. Run `sanitize-docx` in [the local CLI](../../../scripts/legal_case_os.py) to create a derived copy; preserve and re-verify the source hash.
5. Record a `SanitizationReport` and run `preflight` plus substantive/minimal-difference review on the derivative.
6. Mark `rereview_status` passed only after all blocking findings are cleared.

The report includes source/derived artifact IDs and hashes, approved item IDs, every attempted item and result, blocked items, and re-review status. Item results are `removed | blocked | not_found`; re-review is `required | passed | failed`.

The approval JSON must identify approval ID, source artifact, approval-file artifact, independent pre-review, approver, approval time, and exact `approved_targets`. Every target—not merely the top level—requires `ownership=self_generated`, a non-empty authority basis, OOXML location, pre-cleaning part hash, and a type-specific exact value, text hash, or expected count. The canonical target-list hash must match `approval_set_sha256`. For stateful work, a `SANITIZE` Approval must bind source, pre-review, and approval file in its exact scope and have a matching append-only approval receipt. Hidden text, unresolved placeholders, third-party evidence, and uncertain ownership must block the command. A successful run still sets `rereview_status=required` until this Skill reviews the derivative.

Potentially removable only from a self-generated draft recorded as `ownership=self_generated` with known authority:

- internal comments and review markers;
- tracked-change residue after the intended final text is established;
- internal IDs and source-control fields;
- unresolved placeholders only after their substantive resolution;
- draft watermarks and non-substantive metadata the user is authorized to remove.

Always block automatic removal of:

- a third-party evidence watermark, signature, seal, timestamp, header, metadata, or authenticity marker;
- unfavorable or contradictory substantive content;
- anything whose deletion could alter evidentiary authenticity, context, meaning, service duties, or disclosure obligations.

A substantive removal from evidence routes back to `legal-evidence-gate` and may require a new evidence version; it is not sanitization.
