# Template and Artifact Rules

## Template resolution

There are two permanent registries and a separate current-task source lane. A user-selected upload or local file may be used immediately with exact hash/role/field records, without permanent approval. A knowledge-library hit is source discovery, not permanent template activation. Use [material access and learning](../../../shared/policies/material-access-and-learning.md) and [network degradation](../../../shared/policies/network-degradation.md).

The permanent registries and modes remain:

1. Project-original fillable templates live in `shared/templates/template-registry.json` and contain declared placeholders. Resolve and render them with `template-fill`.
2. Lawyer-approved personal templates live in `library/模板库/` and are indexed by `_registry/template-catalog.json`. Resolve them through `legal-template-curator`: `fillable_clone` uses an approved `FormProfile + FillPlan` and `template-fill-docx`; `reference` uses a `WritingProfile`; `hybrid` combines protected mechanical slots with a declared substantive body. Never fill an old final document in place or treat its prior facts as placeholders.

Resolve an alias to one `Template` containing:

`id | name | aliases | document_type | path | source | license.id | license.status | license.notice | sha256 | version | status | usage_mode | profile_id | fixed_fields | editable_fields | required_fields`

For permanent registry-based generation, only `license.status=verified` and `status=active` are eligible. A hash/version mismatch, unknown or restricted license, expired or blocked status, ambiguous alias, or missing required field stops named-template generation.

For an activated personal profile's production mode, require `approved_final=true`, `authorization.status=verified`, `status=active`, a supported local file, a matching SHA-256, an approved mode-specific profile, and the complete prohibited-transfer set. A `reference` additionally requires declared `reusable_aspects`; `fillable_clone` requires an approved non-stale `FillPlan`; `hybrid` requires explicit fixed and substantive-body boundaries. If the same spoken alias matches more than one registry or suite, clarify rather than applying precedence silently.

Fixed fields must not be changed silently. Record every filled field and unresolved placeholder. Use `template-fill` in [the local CLI](../../../scripts/legal_case_os.py) and save a new artifact.

## Artifact record

Use the schema values:

- `audience`: `discussion | internal_review | court_candidate`;
- `status`: `draft | reviewed | approved | stale`;
- `review_status`: `not_reviewed | passed | failed | needs_review`.

Record `input_snapshot` entries with object ID, version, and hash, plus output path/hash, template ID/version, creation reason, and stale reason. A changed input invalidates rather than overwrites the artifact.

When a personal reference is used, also record whether it was stylistically similar, legally similar, both, or neither. Legal similarity never upgrades old facts, evidence, authorities or outcomes into verified current-case inputs. An attributed prior opinion may be analyzed or adapted after comparing its premises, adverse limits and verification status.

A single reference template need not create a complete CompositionSpec. Coordinate complex task-local inputs in a source/argument map; when using the formal multi-input production workflow, bind the artifact to its validated CompositionSpec and ClaimBindings. Keep these internal objects outside outward prose and retain their versions/hashes.

## Minimal modification

For “只改××”:

1. bind the exact source artifact/version and allowed sections, paragraphs, fields, or line ranges;
2. make no stylistic cleanup outside that scope;
3. create a derived artifact and a change list;
4. run `min-diff` in [the local CLI](../../../scripts/legal_case_os.py);
5. fail if any unauthorized change remains, then route the new version to review.
