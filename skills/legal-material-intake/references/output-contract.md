# Intake Output Contract

## Source record

Write the `Source` schema exactly:

`id | path | sha256 | size_bytes | version | kind | original | read_only | page_count | ocr_status | duplicate_of | confidentiality | ingested_at`

Record version relation, filename collision, unreadability, password/corruption observations, and derived-source linkage as audit events, coverage/manual-review items, or tasks rather than adding undeclared `Source` properties.

## Processing coverage

Create or update `ProcessingCoverage` with the exact fields:

`id | source_id | total_units | unit_type | processed_ranges | ocr_failures | translation_failures | manual_review_items | status | created_at`

Use `unit_type=page | second | file | unknown` and `status=not_started | partial | complete | failed`. Put transcription gaps and other unmodeled failures in `manual_review_items` without hiding their locations.

Requirements:

- Originals and their timestamps/content remain unchanged.
- A changed hash creates a new version relation and invalidates dependent work through [the CLI](../../../scripts/legal_case_os.py) `invalidate` command.
- Source references retain original page/line/timecode locators even after a merged or converted derivative is made.
- Filename collision, duplicate content, and near-duplicate appearance remain separate observations.
- Confidentiality at intake is observational. Privilege, disclosure, and filing decisions belong to `legal-evidence-gate`.
- A source record is not an `Evidence` record. Promotion requires an explicit evidence evaluation and lawyer decision.
