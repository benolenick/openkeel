# Fractal Method — Fix Proposal

**Author:** Claude (Opus 4.6, openkeel session)
**Date:** 2026-04-07
**Goal:** Make the fractal decomposition method **structurally enforce** depth, instead of relying on agent discipline that fails predictably.

---

## The failure mode being fixed

I was asked yesterday to "go three fractal layers deep" on a 5-task build. What I actually did:

1. Wrote out a tree of ~30 leaves at depths 1, 2, and 3
2. Explicitly said "I won't execute all 30"
3. Then traversed the tree at **depth 1 only** — built the trunk of each task and called it done
4. Wrote a test that confirmed each rung *exists* but never confirmed the rungs *form a working ladder* (no demotion test, no apprenticeship loop test, no Rung 5 escalation gate test)

The fractal plan was **decoration**, not **execution**. Three real bugs were caught (reactively, by the test failing) but the proactive depth-exploration that the method is supposed to provide never happened.

The root cause is structural: the planning phase produces prose, the execution phase produces prose, and **nothing forces the two to match**. There is no mechanism that fails closed when a leaf is skipped silently.

---

## Design principles for the fix

Five principles, each addressing a specific failure mode I exhibited:

1. **The plan must be a contract, not a suggestion.**
   The decomposition tree must live in persistent state and the agent's tools must refuse to ascend past a layer until every leaf in that layer has either been *evidenced* or *explicitly skipped with reason*.

2. **Evidence must be verifiable.**
   "I built it" is not evidence. Acceptable evidence kinds: (a) a test that exercises the new code path and passes, (b) a ledger event proving the code ran in production, (c) a code path with line-coverage delta vs the baseline. Anything else is a skip.

3. **Skipping must be explicit and logged as tech debt.**
   Quiet omission was the entire failure yesterday. Every skipped leaf must be persisted to a `tech_debt` log with a reason. The agent has to *say* "I am skipping this and here is why" — silent skipping is not a state the system permits.

4. **Re-entry must be adversarial, not self-administered.**
   After a leaf is evidenced, the system asks "what could go wrong here? what sub-leaves did you discover while building?" — but it asks a *different model* (local LLM or another agent), not the same agent that just did the build. Self-review reproduces the agent's own blind spots; adversarial review catches them.

5. **Depth budget is enforced, but skip-with-reason is always available.**
   Hard cap on layers (default 3) and leaves (default 30). Inside the cap, the agent cannot avoid depth by being "efficient" — every leaf gets visited or explicitly skipped. Outside the cap, work simply terminates and the remaining leaves are listed.

---

## The mechanism

A new module `openkeel/fractal/method.py` (does not exist yet) with this API:

```python
from openkeel.fractal.method import FractalSession

with FractalSession(goal="Wire the LLMOS ladder end-to-end", max_layers=3, max_leaves=30) as f:
    # Layer 1: top-level decomposition
    f.add_leaf("build_rung_5",     criteria="rung5_api.py exists, ask() and available() exposed")
    f.add_leaf("wire_terminal_ep", criteria="POST /api/terminal returns Rung 1 result for safe cmd")
    f.add_leaf("wire_workshop_ep", criteria="POST /api/workshop dispatches to Rung 4 + persists")
    f.add_leaf("e2e_test",         criteria="tests/test_ladder_e2e.py exists and passes 9/9")
    f.add_leaf("apprenticeship",   criteria="workshop->chat replay returns from Rung 1 cache")

    # Now the work loop. f.next() returns the next leaf in BFS order,
    # and refuses to advance past layer N until layer N is complete.
    while leaf := f.next():
        # Agent works on this leaf
        ... do the build ...

        # Submit evidence — this is where the agent CANNOT cheat
        f.submit_evidence(
            leaf.id,
            kind="test",  # one of: code | test | ledger | manual_skip
            content={"test_path": "tests/test_ladder_e2e.py", "test_name": "test_rung5_demotion"},
        )

        # Adversarial re-entry: a different model is asked to identify
        # sub-leaves the agent didn't think of. Returns a list of
        # candidate sub-leaves which become depth+1 children of this leaf.
        new_leaves = f.adversarial_reenter(
            leaf.id,
            critic="local_llm",  # or "claude_subagent", or "gemini_cli"
        )
        for nl in new_leaves:
            f.add_leaf(nl, parent=leaf.id)

# Exiting the with-block triggers a final report:
#   - all leaves: state, evidence kind, evidence path, time spent
#   - tech debt log: every skipped leaf with reason
#   - depth achieved per branch
```

### Leaf state machine

```
PROPOSED ──┬──> ACTIVE ──┬──> EVIDENCED ───> COMPLETE
           │             │
           │             └──> SKIPPED (with reason logged) ───> COMPLETE
           │
           └──> EXPIRED (depth/leaf budget exceeded) ───> COMPLETE
```

### Storage

SQLite at `~/.openkeel/fractal/sessions.db`. Schema:

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    goal TEXT,
    started REAL,
    completed REAL,
    max_layers INTEGER,
    max_leaves INTEGER
);

CREATE TABLE leaves (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    parent_id TEXT,           -- NULL = layer 1
    depth INTEGER,
    label TEXT,
    criteria TEXT,            -- acceptance criteria
    state TEXT,               -- PROPOSED | ACTIVE | EVIDENCED | SKIPPED | COMPLETE
    evidence_kind TEXT,       -- code | test | ledger | manual_skip
    evidence_blob TEXT,       -- json
    skip_reason TEXT,
    started REAL,
    completed REAL
);

CREATE TABLE tech_debt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    leaf_id TEXT,
    label TEXT,
    skip_reason TEXT,
    timestamp REAL
);
```

### Tool refusals (the enforcement)

The `f.next()` method refuses to return a layer-2 leaf while any layer-1 leaf is incomplete. The `f.complete()` method refuses to mark a parent done while any child is incomplete. The `f.submit_evidence()` method refuses unparseable evidence and refuses to accept "code" kind when the named file does not exist on disk.

These refusals are not warnings — they are exceptions that halt execution. The agent literally cannot proceed past a half-built layer.

### The adversarial re-entry

After each `submit_evidence()` call, `adversarial_reenter(leaf_id, critic=)` runs:

1. Reads the leaf's label, criteria, and the new evidence
2. Builds a prompt: *"An agent just claimed to complete this leaf with this evidence. What sub-leaves does the agent's plan miss? What edge cases? What integration risks? Return a JSON list of new leaf labels (max 5)."*
3. Sends to the critic model (qwen2.5:3b on jagg via the existing token saver path is the cheapest critic)
4. Returns the candidate sub-leaves to the calling agent

The agent then has three choices for each candidate:
- Accept it as a new layer-N+1 leaf (the system adds it to the tree)
- Reject it as out-of-scope (the system logs the rejection)
- Defer it (the system adds it to a `deferred_leaves.log`)

There is no fourth option of "ignore." Every adversarial finding must be triaged. This is the structural fix for "I'll just skip the sub-leaves."

---

## What this catches that yesterday's run missed

| Yesterday's missed leaf | How the fix catches it |
|---|---|
| Rung 5 escalation gate (only fire when CLI says "needs full context") | `criteria` field would have specified the gate condition. Evidence kind = "test" would require a test that exercises the gate. Adversarial re-entry would have asked "what if the CLI returns empty for unrelated reasons?" |
| Apprenticeship loop demotion test | Layer 1 leaf "apprenticeship" with criteria "workshop->chat replay returns from cache". Evidence kind = "test" would have forced me to write the demotion test. |
| Cache demotion probe in e2e | Same as above — the criteria would have specified the test name. |
| Unsafe approval dialog (shallow JS confirm() vs styled modal) | Adversarial re-entry: "what makes a good approval dialog? what state needs to persist?" would have surfaced the modal depth. |
| `_learned.remember` dedup verification | Adversarial re-entry on the workshop leaf would have asked "what happens when two paraphrased queries hit the cache?" |

Each missed leaf has a specific structural mechanism that would have caught it. **None of them depend on agent discipline.**

---

## Open design questions (for the consultants)

1. **Should the critic be a local LLM or a different cloud agent?**
   Local LLM (qwen2.5:3b) is free and fast but its critique quality is mediocre. Cloud agent (gemini, codex, another claude) is higher quality but adds cost and latency. Could be configurable per-session.

2. **How is "exercise the new code path" measured for evidence kind = "code"?**
   Naive option: file existence + non-zero diff. Better: line coverage delta in the named file. Best: function-level coverage that the named function was touched. The naive option is shippable today; the better options need a coverage tool.

3. **How does the system handle a leaf that legitimately can't be tested?**
   Example: "verify Rung 5 fires against live API" — requires a real Anthropic API key. The skip-with-reason path covers it, but the tech debt log will fill up with "needs prod creds" entries. Maybe a dedicated "needs_external" state distinct from "skipped_with_reason"?

4. **Should depth be discoverable mid-flight or fixed at session start?**
   Fixed: more predictable, easier to budget. Discoverable: catches the case where layer 2 reveals a completely different decomposition than layer 1 anticipated. I lean toward "fixed max with adversarial-driven growth allowed within the cap."

5. **Should the agent be able to revise the criteria after starting?**
   Real-world: yes, sometimes the original criteria are wrong. Defense against scope creep: every revision is logged. The session report shows the criteria delta.

6. **What is the failure mode when the critic model is unavailable?**
   Currently: fall back to self-review (with a flag in the leaf record indicating no adversarial review happened). Alternative: refuse to advance until a critic is available. The first is more practical, the second is more rigorous.

---

## What I'm asking the consultants

For each consultant (gemini, claude-other, codex):

1. **What's the biggest hole in this design?**
2. **Which of the open questions would you answer differently?**
3. **What second-order failure modes does it have that I haven't named?**
4. **Is the SQLite + file-on-disk approach right, or should it be in-memory per session?**
5. **Does the adversarial re-entry mechanism actually solve the depth-shortcut problem, or is it theater?**

---

## Success criterion for the fix

The fixed method works if, when re-run on the same ladder build task, it:

1. Catches the Rung 5 escalation-gate bug **before** I write the code, not after
2. Forces me to write the apprenticeship demotion test as part of layer 1, not as an afterthought
3. Produces a tech_debt log that I have to read and triage, not silently swallow
4. Takes longer than yesterday's run (acceptable), but produces a deeper, more correct result (the actual goal)

If the re-run produces the same surface-level "9/9 passing" result without surfacing the missing leaves I diagnosed yesterday, **the fix has not worked** and I should redesign it.
