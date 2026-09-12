# Evidence Decision Model

## Required assessment

Record:

`id | version | source_refs | description | proposition | authenticity | legality | relevance | probative_strength | adverse_content | opens_new_issue | opponent_likely_use | duplication | timing | consequence_if_not_submitted | substitute | ai_recommendation | lawyer_decision | decision_reason | current_submission | reserve | internal_reference | adverse | confidential | opponent_known_status | disclosure_intent | intended_recipient | service_obligation_status | applicable_stage | procedural_authority_id | risk_assessment_artifact_id | status`

Each `source_refs` locator carries `record_kind=direct_record | user_statement | model_inference` so a statement or inference cannot masquerade as a direct record.

Use the schema enums exactly:

- `ai_recommendation`: `submit_now | reserve | internal_only | do_not_submit | needs_review`;
- `lawyer_decision`: `undecided | submit_now | reserve | internal_only | do_not_submit`;
- `status`: `candidate | approved | rejected | stale`;
- `opponent_known_status`: `known | likely_known | unknown | not_known`;
- `disclosure_intent`: `court_candidate | serve_all_parties | internal_only | undecided`;
- `service_obligation_status`: `verified_required | verified_not_required | requires_verification | not_applicable`.

The boolean views `current_submission`, `reserve`, `internal_reference`, `adverse`, and `confidential` must remain consistent with the decision and status.

## Lawyer decision tracks

- current submission: eligible only for the exact approved stage and source version;
- later reserve: preserved but excluded from the current package;
- internal reference: usable for analysis but excluded from submission;
- adverse/confidential hold: blocked pending an explicit risk decision;
- excluded or withdrawn: not eligible; preserve the decision history.

The AI recommendation may propose any track but cannot write the lawyer decision.

## Non-negotiable rules

- A source does not become evidence merely because it was uploaded, indexed, summarized, or mentioned in a draft.
- `G2_evidence` binds the exact evidence IDs, source hashes/versions, procedural stage, and decision.
- Privilege, confidentiality, authenticity, adverse-admission, disclosure, service, or adverse-use flags block automatic promotion.
- A new source version reopens evaluation and invalidates dependent drafts and package manifests.
- Packaging consumes only current-submission items with a valid current G2 approval.
- “packaged,” “printed,” and “filed” are not evidence decisions.
- No workflow may guarantee that the opponent will not see material. Restricted disclosure requires a verified procedure and lawyer decision.

When useful, return three views without changing status: concise current-submission list, detailed reserve analysis, and a pending-confirmation table.
