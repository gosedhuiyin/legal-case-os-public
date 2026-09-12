---
name: legal-material-intake
description: Inventory, hash, deduplicate, paginate, and index exact files for a Chinese legal matter while preserving originals. Use for first intake, a bounded new-material batch, or a processing-coverage question. Let the orchestrator route semantic delta questions such as “这些新材料有什么影响”. Do not OCR/translate media, analyze legal issues, or decide which sources should be filed.
---

# Legal Material Intake

Build a traceable inventory before relying on content. Use `index` in [the local CLI](../../scripts/legal_case_os.py) for deterministic hashes and index updates.

For each source, record identity, original path, content hash, file type, page count or duration, version relation, readability, processing status, and stable page/line/timecode locators. Compare hashes rather than filenames; same names do not prove sameness.

Keep originals read-only. Indexing a source does not create an evidence item and never implies submission. Record unknown metadata as unknown rather than inferred.

If text cannot be reliably read, create a bounded task for `legal-material-preprocessing`. If content is readable and the user asked “看一下” or “大致内容”, return the intake result to the orchestrator: use `legal-case-analysis` for first-time or already-scoped merits work, and `legal-matter-delta-intake` when new material must be related to an existing matter. Never let indexing itself update semantic case memory.

Read [the intake output contract](references/output-contract.md) when creating or updating source and coverage records. Apply the shared [staleness policy](../../shared/policies/version-and-staleness.md) whenever a source hash or pagination changes.
