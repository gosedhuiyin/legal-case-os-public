# Execution Recipes

Read [material access and learning](material-access-and-learning.md) for the shared task flow, source use, and current-task versus long-term learning rules. Select the deliverable before selecting depth; complexity never changes an internal draft into a filing package.

## Deliverable and completion

| Requested outcome | Required work | Completion |
|---|---|---|
| Ordinary “生成文书”, including a named/uploaded template | Default `internal_review`; exact inputs, key-field/risk check, editable draft, source and old-case-residue checks, necessary layout review | Deliver the draft and a separate concise gap/review record; no full case-state, permanent template registration, or G1/G2/G4 prerequisite |
| Explicit court/opponent-facing candidate | `court_candidate`; current G1 strategy, applicable current-stage G2 evidence, verified sources/authorities, independent review and clean outward prose | Deliver the candidate and review record; do not claim package approval |
| Filing package | Review-passed current artifacts, approved evidence, exact manifest and dependencies | G4 applies to this exact package; no external act |
| Client communication | Frozen recipient/purpose, `internal_review`, confidentiality and communication review | Deliver an unsent draft |

Use [human approval](human-approval.md) for the exact gates. A specific legal decision such as changing relief or granting special authority may need clarification or approval without blocking unrelated drafting.

## Depth

`reasoning_depth` remains `standard | deep` for case-state compatibility. The transient `RunSpec.recipe` is `quick | standard | deep | discuss`.

| Recipe | Work scaled to the requested outcome |
|---|---|
| `quick` | Selected template and current facts, bounded editable output, source/field check, obvious contradictions, old-case residue and necessary layout review |
| `standard` | Add an issue/evidence map when useful, resolve material gaps, then draft and validate the requested scope |
| `deep` | Add a problem tree, source comparison, material contradictions, strongest adverse path, conditional conclusions and independent review for the affected issues |
| `discuss` | Read-only alternatives, gaps and outcome-changing conditions; no file changes, memory commit or forced conclusion |

Simple work with sufficient selected inputs does not need database discovery, whole-matter analysis, or a complete `CompositionSpec`. Complex work follows the same flow with the necessary research/comparison steps. Use a full composition contract when several substantive inputs and template roles need coordinated bindings, especially for a formal court candidate.

Freeze a compact RunSpec with input scope, output audience, allowed mutations, necessary validators, real blockers, and the completion point. Optional style choices use reversible defaults. Keep factual uncertainty visible; it blocks only the affected asserted conclusion or formal use, not an otherwise useful internal draft.

All recipes preserve command/data isolation, source traceability, permission boundaries, truthful coverage and `external_actions_allowed=false`. Record `verification_result` only after executing the relevant checks. A route/plan is not proof that a file was generated or that research, review, approval or learning occurred.

## Bounded local file operations

Use these operations only after the user actively invokes the suite, within the current authorized task. “只改这里” and “加页码并回填” can produce route suggestions; the current AI must read the actual inputs and build the exact plan. The router does not execute changes or supply automatic semantic scope recognition. Keep `library` and `00-originals` read only; do not expand into scanning, model calls, case-state changes, new workflows or external actions.

| Requested change | Actual command and required binding | Completion and boundary |
|---|---|---|
| Inspect the original DOCX | `docx-inspect --file <source.docx> --json` | Read `source_sha256`, exact text, returned 1-based `target` and `editable/reason`; never guess visible paragraph numbering. |
| Change only named text | `docx-patch --file <source.docx> --plan <plan.json> --output-dir <new-task-dir> --json` | Plan binds `source_sha256` and `changes` with `target/expected_text/replacement_text`. Preserve other content; return `draft.docx`, separate `review.md` and manifest. |
| Number selected PDF pages and fill the existing catalog | `evidence-pages --plan <plan.json> --output-dir <new-task-dir> --json` | Plan binds catalog/PDF paths and hashes, ordered `items`, explicit inclusive physical `page_ranges`, exact `catalog_cell` and original text. One `page_map` drives numbered PDF, bookmarks and the original catalog cells. |
| Read or change the local current version | `delivery-current --store-dir <store> --json`; `delivery-publish --task-dir <task> --store-dir <store> --expected-current <revision> --json` | Save the current revision when starting work. First publication uses `none`; later publication must match that revision. Copy only manifest-listed, hash-verified files including `review.md`; atomically switch one pointer. |
| Restore an existing local version | `delivery-restore --store-dir <store> --version-id <saved-version-id> --expected-current <revision> --json` | Verify and select the existing bytes. Do not regenerate, rerender, clean again or overwrite user edits. A stale token requires comparing the newer result, not a blind token refresh. |

For DOCX plans, `target` is `{"kind":"paragraph","paragraph":2}` or `{"kind":"cell","table":1,"row":2,"column":3}`. The current source and parent manifest must agree before inheriting this task's `constraints`. Default merging retains `must_keep`, `avoid` and recorded `terminology`; an explicitly changed task requirement may use `constraints_mode: "replace"` with its `constraints_change_reason`. Do not convert this into permanent memory or an extra routine approval loop. Literal checks do not prove semantic correctness. Keep prior unresolved review details distinct from checks performed on the new derivative.

Only uniformly formatted ordinary paragraphs and single-paragraph cells are editable. Reject complex targets containing fields, tracked changes, links or other unsupported objects; reject table coordinates in merged, nested or irregular tables. Do not fall back to regenerating the document. PDF inputs need explicit, nonduplicated physical pages and cells; reject changed hashes, encrypted PDFs, selected pages with annotations/forms, unsupported geometry and this tool's already-numbered derivatives. Inspect real output pages before claiming visual review passed.

Version registration is a local output convenience, never `G4` or an external-action approval. Preserve the source `checks`, including `required` states, and keep clean outward prose separate from internal review files. See [command and JSON examples](../../docs/MATERIAL-TASKS.md#31-原-docx-只改指定位置).
