# Exemplar Record

Use two distinct records under [material access and learning](../../../shared/policies/material-access-and-learning.md): a task-local source/observation record for immediate selected-file use, and a specifically approved versioned asset for later reuse. Optional catalog/profile/approval fields may be absent in the task-local record; do not invent them.

Record:

`exemplar_id | personal_template_catalog_id | writing_profile_id | artifact_id | version_or_hash | approval_id | document_type | procedural_stage | party_role | audience | rhetorical_function | structure_tags | layout_tags | tone_tags | length | citation_density | approved_excerpt_locator | reusable_pattern | nontransferable_facts | confidentiality_scope | style_score | legal_similarity_score`

Permanent catalog admission rules (not a precondition for current-task analysis):

- Only a lawyer-approved final artifact or explicitly approved excerpt enters the corpus.
- The approval and artifact version must still match.
- Another client's document requires an authorized, appropriately separated use scope.
- Old names, dates, amounts, claims, evidence, authorities, and case conclusions never transfer automatically.
- Unapproved AI drafts, discussion drafts, notes and opponent filings are not automatically admitted as approved final templates. They may be inspected for observable methods, style or attributed arguments in an authorized task, with their actual role and verification state retained. Any long-term learned asset requires separate exact-content confirmation.
- A named personal template must resolve to one active catalog entry whose authorization, file path, version, and SHA-256 all validate; library-wide fuzzy loading is forbidden.
- Routine retrieval returns the approved `WritingProfile` plus exact locators. Raw source text is loaded only for a declared excerpt and may not be re-distilled or silently update the profile.

Retrieval returns source-linked observations, not an indiscriminate whole-library dump. Record style, method or viewpoint separately; include the speaker/role, applicable premises, adverse conditions and verification status for a viewpoint. A user-selected old draft can guide this task immediately after its version/hash and intended aspects are recorded. Only specifically confirmed reusable content and minimal approved excerpts may be saved.
