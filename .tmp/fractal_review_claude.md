# Other-Claude's review

## TL;DR
"This is decoration wearing a SQLite hat."

## Key points
1. **The biggest hole: enforcement isn't enforcement.** FractalSession is a library the agent CHOOSES to call. Yesterday's failure wasn't broken contract — it was never written. Need a HARNESS-LEVEL HOOK (PreToolUse/Stop) that auto-opens a session when user says "fractal" and refuses to let session end with incomplete leaves.

2. **`submit_evidence(kind="test")` must shell out to pytest** on the named node and require a pass. "Test file exists" is exactly yesterday's failure: rungs exist, ladder doesn't work.

3. **Critic must be ≥ 27B model or Haiku subagent.** 3B critic produces "generic edge-case slop" — false confidence is worse than no critique.

4. **Code evidence must be line-coverage delta.** Coverage.py is a one-liner — ship day one.

5. **Critic-unavailable should BLOCK, not fall back to self-review.** "You are building this BECAUSE fallbacks to self-review failed."

6. **Criteria gaming.** Need adversarial review of CRITERIA before work starts, not just evidence after. Otherwise shallow criteria → shallow leaves.

7. **Adversarial triage loop will be rubber-stamped.** 150 reject decisions = you'll reject everything. Need cap on reject-rate per session OR second critic pass on rejections.

8. **BFS ordering is structurally wrong.** BFS finishes all layer-1 before layer-2 → builds 5 shallow trunks in parallel = trunk-walking again. **DFS with depth budget** — go three deep on leaf 1 before touching leaf 2. This is what "three fractal layers deep" actually means.

9. **tech_debt log rot.** Nothing reads it. Becomes /dev/null. Need Stop hook that surfaces N debt items per session.

10. **No retrospective replay harness.** Success criterion is "re-run catches yesterday's bugs" but there's no mechanism to actually replay yesterday's task. Build the replay harness as leaf zero.

11. **Adversarial re-entry only catches missing sub-leaves on leaves you BUILT.** Doesn't fix "never built leaves at all."

## Direct quotes
- "This is decoration wearing a SQLite hat."
- "Honor-system-with-extra-steps."
- "BFS structurally reproduces trunk-walking."
- "Re-entry is necessary but not sufficient."

## The DFS insight is the most important
BFS = breadth-first = all top-level tasks first = trunk walking with extra ceremony.
DFS = depth-first = "complete leaf 1.1.1 before touching leaf 1.2" = forces actual depth.
This contradicts my BFS design and matches what "three layers deep" actually means.
