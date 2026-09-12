---
name: legal-case-analysis
description: Analyze facts, issues, burdens, opponent arguments, and strategic options after the orchestrator has frozen an exact scope and RunSpec. Use standard/deep reasoning or a read-only Discuss posture for a bounded issue. Return ambiguous, composite, or unresolved requests to the orchestrator. Do not review an outgoing draft, verify authorities, approve evidence, or infer file mutation from “探讨下”.
---

# Legal Case Analysis

Honor the resolved issue scope. Choose:

- `standard` for a concise matter map and next decision;
- `deep` for the smallest outcome-relevant blocking issue.
- `discussion` posture for alternative interpretations, evidence gaps, and
  outcome-changing conditions without finalizing, writing a file, or committing
  memory. Its underlying reasoning may still be deep.

When invoked from incremental material triage, read only the declared affected objects and the permitted `ContextCapsule`; return an `ImpactAssessment` candidate rather than rewriting unrelated case views. A limited-memory run must state its input manifest and may not claim whole-matter completeness.

Maintain two independent controls: `reasoning_depth` and `display_length`. Reason as deeply as the risk requires, but default the first report to a short, logical 1–4 structure. “不要那么复杂” changes display length only.

Consume the frozen `RunSpec`; do not reinterpret raw trigger words. Apply the
shared [execution recipes](../../shared/policies/execution-recipes.md). Deep must
produce the artifacts required there and in [deep analysis](references/deep-analysis.md).

Classify every material proposition:

- `L1_verified_authority`: verified authority, cited by authority ID;
- `L2_original_or_official`: direct original or official material, cited by source and page/line/timecode;
- `L3_user_statement`: user/party statement, expressly unverified;
- `L4_model_inference`: model inference, with premise and verification task.

Do not turn L3/L4 into established evidence facts. Do not verify law here; create a scoped `legal-authority-research` task. Do not change evidence status; create a `legal-evidence-gate` candidate task.

Read [standard analysis](references/standard-analysis.md) for an ordinary overview or [deep analysis](references/deep-analysis.md) only for a deep trigger. Apply the shared [source policy](../../shared/policies/source-and-citation.md) and [personal style profile](../../shared/style/personal-style-profile.md).
