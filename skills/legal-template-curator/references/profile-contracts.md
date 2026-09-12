# Profile Contracts

These are permanent catalog and registered mechanical-profile contracts. Task-local use of a user-selected file does not require activation or permanent registration; follow [material access and learning](../../../shared/policies/material-access-and-learning.md). A compact temporary source/field/style record can serve a simple internal draft. It must not claim to be an approved reusable profile.

## Admission and identity

Every profile binds one exact source path, SHA-256, source format, authorization record, document type, version, status, and `usage_mode`. A profile is a derived instruction artifact, not a replacement for the source. Keep draft, approved, active, superseded, and retired states distinct.

Use the contracts in `shared/schemas/` as the machine authority:

- `FormProfile` describes a form's immutable layout and fillable regions;
- `WritingProfile` describes transferable structure, layout, style, rhetoric, annotation, and length characteristics;
- `FillPlan` binds each proposed form value or blank to a profile slot, source, decision basis, and approval state;
- `CompositionSpec` freezes document goal, template roles, issue analysis, claim bindings, precedence, input snapshots, and forbidden transfers;
- `ClaimBinding` binds one proposed factual or legal proposition to confirmed facts, approved evidence, or verified authority with exact locators and object hashes, including material adverse treatment.

## Three usage modes

`fillable_clone` is for format-sensitive forms. It requires a `FormProfile`, an approved `FillPlan`, a derived output, structure comparison, and visual QA.

A permanently active `reference` profile binds writing approved for its declared reusable use. Routine retrieval loads that WritingProfile and necessary source excerpts. Task-local reference analysis can use an authorized selected draft or opponent document with its actual role/status preserved; it does not activate a profile.

`hybrid` is the contract for documents with protected mechanical regions and a declared substantive body. It requires both slot boundaries and a writing profile. The TEST-ONLY engine can replace complete registered paragraph blocks while preserving donor formatting; production activation/execution remains blocked until the real profile's anchors, render baseline, and lawyer approval are complete. Text outside the approved editable body must remain fixed.

## Registration rules

`template-distill` may produce only a draft profile. `template-register` may activate it only when:

- the source is authorized and lawyer-approved for the declared use;
- the current hash matches the recorded hash;
- reusable style/method/conditional-viewpoint aspects and their limits are explicit; old-case facts never become current facts and attributed opinions do not become verified authority automatically;
- required profile fields and render baseline are complete for the selected mode;
- conflicts or inferred high-risk rules have received explicit approval.

Registration does not approve case facts, legal authority, evidence, strategy, a generated draft, or a filing package. Source or profile changes create a new version and stale dependents; they never mutate the previous record in place.
