# Codex's review

## TL;DR
"This design enforces bookkeeping, not understanding."

## Key points
1. **Biggest hole: bookkeeping ≠ understanding.** Bad layer-1 plan + bad criteria still passes with more ceremony. The original failure was also "picked the wrong units of work." This proposal barely touches that. **If top-level leaves are trunk-shaped, agent will diligently produce evidence for trunk-shaped work.**

2. **Critic must be a different strong model**, not 3B local LLM. "qwen2.5:3b is reviewer theater. Cheap critique is vague, repetitive, easy to dismiss."

3. **Remove `code` as evidence type entirely.** File existence and diff are garbage. Coverage delta is "slightly better garbage." Real evidence: executable test, runtime trace/ledger event, OR explicit manual waiver. Three options only.

4. **Add `BLOCKED_EXTERNAL` state** distinct from `SKIPPED`. "Skipped mixes 'not worth doing' with 'cannot currently prove.' Different management problems."

5. **`max_leaves=30` invites leaf inflation games.** Don't cap raw count.

6. **Criteria revision must invalidate prior evidence** when criteria materially change. Otherwise agent rewrites target around already-collected artifacts.

7. **New second-order modes:**
   - **Criteria laundering** — weak acceptance criteria up front so later evidence is easy
   - **Leaf spam** — adversarial re-entry generates junk leaves; system becomes triage overhead
   - **Evidence gaming** — tests written to satisfy criterion instead of probing failure mode (brittle path-confirming tests)
   - **Premature freezing** — BFS + layer barriers block obvious integration work
   - **Rejection laundering** — "reject out of scope" becomes new silent skip
   - **Persistence fetish** — people trust the report more than the actual software; ledger becomes the product
   - **Critic dependency inversion** — weak critic = self-approval with extra steps

8. **SQLite is right but keep it thin.** Don't store artifacts there — store refs and hashes; large evidence belongs on disk.

9. **The real fix:** force decomposition quality EARLIER. Preflight check: **"If a leaf cannot name the failure it prevents, it is probably still trunk."**

## Direct quotes worth keeping
- "If top-level leaves are trunk-shaped, the agent will diligently produce evidence for trunk-shaped work and still miss the real leaves."
- "The ledger becomes the product."
- "If a leaf cannot name the failure it prevents, it is probably still trunk."
