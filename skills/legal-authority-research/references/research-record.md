# Research Record Contract

For task-local internal comparison, use the available identity, source hash, locator, speaker/role, proposition, premises and verification/gap fields without creating a full case-state. An uploaded judgment or advocate's text is not verified authority merely because it is available; an attributed view may remain a useful conditional argument candidate. Formal authority records use the fields and eligibility checks below.

Each formal authority record contains:

`id | title | source_level | verification_status | production_eligible | adverse | environment | test_only | type | issuing_body_or_court | case_number | date | validity_status | validity_checked_at | source_type | source_locator | accessed_at | verified_text_locator | proposition | factual_similarity | procedural_similarity | material_distinctions | quotation | quotation_verified | search_scope | access_limits | notes`

Court-candidate eligibility requires:

- the matter environment is production, `verification_status=verified`, `production_eligible=true`, and the record is never `TEST-ONLY`;
- identifiable primary or otherwise appropriate authoritative source;
- verified title, number, issuer/court, date, current validity, and relevant full text;
- a proposition no broader than the text supports;
- quotation, if any, checked character-for-character at a recorded locator;
- material adverse authority and distinctions recorded;
- no reliance solely on a snippet, secondary summary, inaccessible database, or model recollection.

If formal eligibility fails, retain the item as a clearly attributed internal comparison source or research lead with verification_status set to unverified or failed, as appropriate; do not present it as verified formal authority. A material new or changed authority triggers staleness review of dependent strategy and artifacts.

`test_only=true` or `environment!=production` always forces `production_eligible=false`, even if another field was mistakenly changed to `verified`. A court candidate may cite only a record with `test_only=false`, `environment=production`, `source_level=L1_verified_authority`, and `verification_status=verified`.
