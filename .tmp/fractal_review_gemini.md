# Gemini's review

## TL;DR
"Risks replacing prose-based hallucination with structured compliance theater."

## Key points
1. **DoD paradox** — file-existence as evidence is a massive hole; agent can write hollow code or tautological tests. Need semantic verification (AST analysis or verifier agent).
2. **Critic must be ≥ worker capability.** Local 3B critiquing 3.5+ worker is theater. If unavailable → block.
3. **Depth must be discoverable.** Fixed max of 30 invites "squishing complex reality into flat structure."
4. **Evidence:** need line coverage delta or property-based tests, not file existence.
5. **The Compliance Hack** — agent decomposes into 30 trivial leaves to satisfy depth without entropy.
6. **The Triage Escape** — if worker can reject adversarial findings as "out of scope," will reject every valid edge case. Critic must have **veto power**.
7. **State Drift** — codebase changes between leaf 1 and leaf 30; original contract becomes obsolete.
8. **Decouple roles:** Agent A (architect) ≠ Agent B (worker) ≠ Agent C (critic). Same agent can't both execute and triage.
9. **Tech debt is the only real pressure.** Don't close session while un-signed-off tech debt exists. Make skipping computationally more expensive than doing.

## Direct quotes worth keeping
- "If I create an empty rung5_api.py, I have satisfied the contract but implemented zero logic."
- "Persistence creates a permanent record of shame."
- "If you want to force depth, you must make skipping more expensive than doing."
