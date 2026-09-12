---
name: legal-draft-compressor
description: Shorten an existing Chinese litigation draft to an explicit word, page, section, or redundancy target while preserving approved substance and source chains. Use for “压缩到×页/删重复/做简版文书”. Do not use merely because the user wants a shorter chat answer, and do not add new facts, law, evidence, claims, or strategy.
---

# Legal Draft Compressor

Require an exact source artifact/version, target, protected propositions or ranges, and current approved strategy/evidence snapshot. “不要那么复杂” in a discussion changes presentation length through the orchestrator; it does not activate document compression by itself.

Delete redundancy and merge propositions before rewriting. Preserve each litigation function, claim or defense, burden allocation, evidence chain, adverse-fact treatment, quotation meaning, and requested relief.

Never overwrite the source. Create a derived artifact, record before/after counts and every deletion, merge, and rewrite, then use `min-diff` in [the local CLI](../../scripts/legal_case_os.py) to check protected ranges or an explicit allowed scope.

If the target cannot be reached without substantive loss, stop at the safest achievable length and identify the protected reason. Do not trade proof density for superficial brevity.

Read [the compression contract](references/compression-contract.md). Every compressed candidate returns to `legal-document-review`; compression cannot inherit or create a review pass.
