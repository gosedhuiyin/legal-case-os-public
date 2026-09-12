---
name: legal-filing-packager
description: Assemble review-passed Chinese litigation documents, approved evidence, procedural forms, manifests, pagination, and a print production sheet into a new filing-candidate directory. Use for “顺便做手续/整理文件夹/做待打印包但不要提交”. Do not draft substance, clean evidence, move originals, print, send, upload, serve, or file.
---

# Legal Filing Packager

Consume only:

- review-passed, non-stale court-candidate artifacts;
- current-stage evidence with an exact current `G2_evidence` approval;
- current verified party, forum, service, deadline, and procedural-rule data;
- registered active templates and review-passed procedural forms.

For template-generated forms, also require the exact active `FormProfile`, approved non-stale `FillPlan`, source/derivative hashes, structural-diff pass, and page-render QA pass. For `hybrid` or composed documents, require the matching validated `CompositionSpec` and completed citation/leak review.

If a required form is missing, create a scoped drafting task; do not improvise it inside packaging.

Create a new candidate directory. Copy or derive eligible files; never move, delete, rename, or overwrite originals. Use `name`, `bundle`, and `print-sheet` in [the local CLI](../../scripts/legal_case_os.py) for deterministic filenames, package assembly, numbering, manifests, and production instructions.

Packaging may paginate copies, create bookmarks, map source pages to bundle pages, and generate checklists. It may not edit, polish, sanitize, redact, or change substantive content.

Return the exact package hash/manifest as a `G4_final` candidate. Even an approved G4 package does not authorize printing, sending, uploading, service, or filing; those are distinct `G5_external_action` acts outside this source-only workflow.

Read [the filing package contract](references/filing-package-contract.md). Apply the shared [external-action boundary](../../shared/policies/external-actions-and-scope.md), [sanitization and original-protection](../../shared/policies/sanitization-and-originals.md), [approval](../../shared/policies/human-approval.md), [source](../../shared/policies/source-and-citation.md), and [staleness](../../shared/policies/version-and-staleness.md) policies.
