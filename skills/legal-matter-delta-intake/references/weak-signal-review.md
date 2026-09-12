# Triggered Weak-Signal Review

Use this review only when the user requests reflection or when a declared high-signal trigger fires. It is a sparse reconsideration pass, not continuous whole-folder memory.

## Eligible triggers

- a new party, right, jurisdiction, forum, proceeding, or materially different remedy;
- repeated weak signals across separate batches;
- a new source that conflicts with a material fact, theory, deadline, or prior adverse assessment;
- a procedural event that may affect limitation, refiling, disclosure, privilege, preservation, or another branch;
- a lawyer-requested milestone review such as “把最近几批材料合起来看看”.

Use the cheapest gates first: enough new committed events, a material trigger, then a per-matter exclusive review lock. Do not run overlapping reflections. Do not advance `last_reflection_event_seq` after failure, interruption, stale inputs, or invalid output.

## Output contract

Produce at most three `InsightCandidate` records. Each must contain:

- the proposed association, issue, strategy question, or prospective-matter seed;
- source IDs and precise locators from at least two independent events when claiming a cross-batch pattern;
- the best contrary evidence or reason the association may be spurious;
- what would change if true and what must be checked next;
- confidence and an explicit `candidate_only` status.

An isolated reviewer may read the permitted capsule and original sources, but it may write only candidate output. The orchestrator or lawyer separately decides whether to create an `ImpactAssessment`, deepen an issue, verify an authority, or promote a `ProspectiveMatterSeed`.
