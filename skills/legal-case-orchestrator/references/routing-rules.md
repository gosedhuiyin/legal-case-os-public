# Standard and Deep Routing

Route from the frozen `TaskFrame + IntentEnvelope + RunSpec`, never from a raw
keyword after compilation. Explicit current action and negation outrank incidental
legal nouns: “研究下，只看送达问题” is an analysis/research request, not a
lifecycle action merely because it contains “送达”. If the frozen fields conflict,
return to the one-question clarification gate rather than letting a specialist
reinterpret the original utterance.

## Select the work class first

Use [execution recipes](../../../shared/policies/execution-recipes.md) for the requested deliverable. Sufficient uploaded/local template and facts can go directly to a lightweight template check and internal drafting; database health, permanent registration and whole-matter state are not prerequisites.

| Situation | Route |
|---|---|
| Requested whole-matter intake or mechanical file identity/coverage work | `legal-material-intake`, then preprocessing if needed |
| New or fragmented material in an existing matter; requested memory association, date extraction, impact, or possible new case | `legal-matter-delta-intake` |
| Withdrawal, preservation, supplemental filing, report retrieval, court formality, waiting, or resuming cross-conversation work | `legal-case-lifecycle-manager` |
| Bounded merits/procedure question requiring legal reasoning | `legal-case-analysis` in standard or deep mode |
| Simple requested document with sufficient selected inputs | Task-local template/field review → `legal-document-drafting` → scoped checks and editable delivery |
| Complex document combining templates and case materials | Same drafting flow plus affected issue analysis, source comparison, strongest adverse path and independent review |

The delta skill answers “what does this new material mean, where does it belong, and what might it change?” The lifecycle skill answers “what legal work is open, what is it waiting for, and how does it safely resume?” Mechanical intake and specialist analysis retain their existing boundaries.

## Standard indicators

- Parties, role, objective, procedural stage, and material facts are substantially settled.
- Sources can be located and processing coverage is sufficient.
- The document type and requested outcome are known.
- No unresolved issue blocks the requested asserted conclusion; an internal draft may retain identified gaps or hypotheses without implying verification.
- No unresolved authenticity, privilege, adverse-admission, limitation, jurisdiction, timing, or disclosure risk controls the result.

## Deep issue triggers

- Material dates, events, documents, or inferences conflict.
- Competing legal relationships, claims, parties, forums, limitation rules, or procedural routes could change the outcome.
- A key communication or item is both favorable and materially adverse.
- Authorities conflict, remain unverified, or turn on factual distinction.
- Technical, accounting, causation, expert, authenticity, or chain-of-custody analysis is material.
- The apparently simple route depends on a hidden or unverified premise.

Route the smallest outcome-relevant issue to `legal-case-analysis` deep mode. Record its question, blocking status, evidence and authority tasks, conclusion, uncertainty, return condition, and lawyer decision. Resume the affected formal assertion when its return condition is met; continue independent sections and deliver useful internal work with the unresolved issue recorded.

`reasoning_depth` may be `standard` or `deep`; `display_length` may independently be `brief`, `normal`, or `detailed`. User language about brevity changes only the latter unless the lawyer expressly narrows the substantive question.

The lawyer may override classification, but the system must preserve visible high-risk flags and their consequences.

## Incremental escalation triggers

Route an ordinary new item no farther than indexing and a `no_action` triage card. Route date-only content to a candidate `DeadlineRecord`. Create an `ImpactAssessment` only when a source could affect declared facts, issues, theories, evidence, approvals, artifacts, packages, or workflows.

Use `memory_mode=reflect` only on express request or a declared high-signal trigger: a new party/right/jurisdiction/proceeding/remedy, repeated weak signals across batches, a material contradiction, or a procedure that may affect limitation, refiling, privilege, preservation, or another branch. Reflection returns at most three source-linked candidates with contrary evidence; it does not approve a strategy or create a new matter.

For a procedural shortcut, load a `ProceduralCapsule` and avoid the merits record unless an outcome-changing issue requires escalation. Withdrawal/refiling/limitation, preservation amount/security, and report scope/privilege are examples of bounded escalations rather than reasons to reread the entire case.
