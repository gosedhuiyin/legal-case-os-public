# Preprocessing Contract

## Inputs

- exact source ID, hash, path, media type, and requested page/time range;
- permitted operations and output directory;
- expected language and locator scheme;
- local capability availability.

## Outputs

- a new derivative path, hash, media type, method, tool/version, and creation time;
- original-to-derived page, line, image, sheet, or timecode map;
- `ProcessingCoverage` totals, completed ranges, failures, and manual checks;
- confidence or uncertainty markers at the affected locations;
- no assertion about authenticity, legal meaning, admissibility, or filing status.

## Non-negotiable boundaries

- Do not overwrite, rename, move, crop, redact, enhance, or delete originals.
- Do not remove third-party watermarks, signatures, seals, headers, metadata, or content that may bear on authenticity.
- Keep OCR/transcript corrections as a new version with a change record.
- Label machine translation and preserve the source-language text and locator.
- Image-to-PDF conversion is a navigational derivative; preserve the source image hashes and order.
- Partial success is valid only when unprocessed ranges remain explicit.
