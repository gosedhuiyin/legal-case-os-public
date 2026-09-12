# Filing Package Contract

## Inputs

- exact artifact IDs/hashes with `audience=court_candidate` and `review_status=passed`;
- any sanitization report with `rereview_status=passed`;
- exact approved evidence IDs/source versions for the current stage;
- verified party, forum, case, service, deadline, signature, seal, and attachment data;
- current procedural-authority records;
- lawyer instructions for copies, color, duplex, binding, and delivery preparation.

## Candidate outputs

- new candidate directory and deterministic filenames;
- pleading and procedural-form set;
- evidence directory, booklet order, page numbers, and bookmarks;
- original-source-page to booklet-page map;
- signature, seal, date, service, and attachment checklist;
- copy/color/duplex/binding print production sheet;
- included/excluded difference report;
- package manifest with every file ID/version/hash and unresolved blocker;
- overall package hash and `G4_final` pending object.

## Stop conditions

Stop if:

- an included evidence item lacks current-stage current-version G2 approval;
- an artifact or approval is stale;
- a court candidate has not passed review or post-sanitization re-review;
- party, case, deadline, service, or document data conflict;
- a required signature, attachment, procedural form, or template field is missing;
- a court-specific requirement lacks a current verified authority record;
- any input is labeled `TEST-ONLY` in production mode.

“整理文件夹” always means build a new candidate structure. It never authorizes moving or deleting source folders.

`G4_final` approves only the exact package snapshot. Printing, sending, uploading, service, filing, login, and submission require a separate, precise `G5_external_action` authorization and are not performed by this Skill.
