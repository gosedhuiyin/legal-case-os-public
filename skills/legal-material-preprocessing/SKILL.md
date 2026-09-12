---
name: legal-material-preprocessing
description: Create traceable derived text or document copies from unreadable legal materials through local OCR, transcription, translation, or image-to-PDF conversion. Use when intake identifies a bounded processing gap. Do not alter originals, remove evidence watermarks, evaluate evidence, or reach legal conclusions.
---

# Legal Material Preprocessing

Work only on the source IDs and ranges assigned by intake. Preserve each original and produce a new derived artifact linked to the source hash.

For OCR, transcription, translation, or image-to-PDF conversion:

1. record the local method, version, language, and requested range;
2. retain original page or timecode mapping;
3. mark uncertain text, omissions, speaker uncertainty, and translation ambiguity;
4. update `ProcessingCoverage` with successes, failures, and manual-check locations;
5. re-index the derivative with `index --ocr-report` in [the local CLI](../../scripts/legal_case_os.py).

Do not smooth uncertain text into confident facts. A machine transcript or translation is a derivative, not proof that the source is authentic or admissible.


Read [the preprocessing contract](references/preprocessing-contract.md) before creating derivatives. Return the coverage report to `legal-material-intake`; evidence and legal evaluation remain separate.
