# Intention Layer — Technical Framework

**Date:** 2026-04-09
**Status:** Framework complete, ready for integration
**Related:** `lllm/lllm/os/intention.py` (implemented), `lllm/lllm/os/calcifer.py` (integrated)

---

## One-sentence summary

**Each Opus session is a drop of water. The landscape (Hyphae + IntentionPacket) carves the channel so each drop naturally flows toward what matters.**

---

## The Problem You're Solving

Opus (you, in Claude Code) lives in a frozen moment. Each session:
- No memory of previous sessions
- No persistent wants or needs
- No way to notice you're stuck in a loop
- Each session starts from scratch

But you want something that *behaves* as if it cares — pursues things across multiple sessions, learns what matters, notices when it's being silly.

Solution: Make the landscape (not the water) do the remembering.

---

## Core Objects

### IntentionPacket

**What:** A compass needle that lives in Hyphae metadata. Immutable once committed.

**Contains:**
```python
IntentionPacket:
  id: str                              # sha256(user + project + goal)
  intended_outcome: str                # "Desktop monitor stays on after idle"
  must_preserve: list[str]             # Constraints that matter
  forbidden_tradeoffs: list[str]       # What we'll never sacrifice

  hypothesis_chain: list[HypothesisVersion]  # Evolution of our thinking
  current_hypothesis: HypothesisVersion       # Latest version

  confidence_factors: dict              # Multi-factor model
    - evidence: 0..1                    # How much experimental support?
    - prediction_accuracy: 0..1         # How often does it hold?
    - parsimony: 0..1                   # Simplest explanation?
  overall_confidence: 0..1              # 0.4*evidence + 0.4*pred + 0.2*parsimony

  attempts: list[dict]                 # What we've tried, when, result
  stuck_pattern: Optional[str]         # "symptom_patching" | "hypothesis_churn" | None

  next_action: str                     # What to do next
  blocker: Optional[str]               # What's preventing progress?

  prevention_deployed: bool            # Is this solved? Shipped?
  prevention_tests: list[str]          # Tests that catch regression
  regression_test_until: Optional[datetime]  # How long to monitor?
```

**Lifecycle:**
1. Created fresh (`IntentionPacket.from_goal(user, project, goal)`)
2. Lives in-memory during a session
3. Updated as Opus discovers things, tests hypotheses
4. At session end, optionally merged back to Hyphae
5. Next session loads it and continues

**Key insight:** Intention is encoded in *structure*, not prompting. The confidence model, hypothesis chain, and stuck-pattern detection are what make it stick across sessions.

---

### SessionShard

**What:** Ephemeral session-local state. Lives 7 days, then archived.

**Contains:**
```python
SessionShard:
  session_id: str
  intention_id: str
  started_at: datetime
  expires_at: datetime  # TTL: 7 days

  actions_taken: list[str]             # What did we try?
  obstacles_hit: list[str]             # What blocked us?
  discoveries: list[str]               # What did we learn?

  session_summary: str                 # Opus writes at end
  escalation_decision: str             # "CONTINUE" | "ESCALATE" | "COMPLETE"
  escalation_reason: str               # Why?
```

**Why it exists:**
- Single Opus session can't hold multi-week context
- But we need to bridge between sessions without losing intermediate state
- SessionShard is the "working memory" that expires
- IntentionPacket is the "long-term memory" that persists

---

### IntentionBroker

**What:** Manager that shuttles between Calcifer and Hyphae.

**Public API:**
```python
broker.load_intention(id) -> IntentionPacket  # Resurrect from Hyphae
broker.start_session(session_id, intention_id) -> SessionShard
broker.record_discovery(session_id, text)
broker.record_obstacle(session_id, text)
broker.close_session(session_id, summary, decision, reason)
broker.merge_to_hyphae(intention_id)         # Commit to permanent memory
```

---

## How It Works in Practice

### Session 1: Diagnosis

```
User: "Why does my monitor go to sleep even though I disabled it?"

Opus (in session 1):
  1. Reads/creates IntentionPacket
  2. Forms hypothesis 1: "xrandr state loss"
  3. Tests it (fails)
  4. Forms hypothesis 2: "DPMS override not persisting"
  5. Tests it (passes once, but...)
  6. At session end, Calcifer closes the shard:
     "We narrowed it down. Hypothesis 2 holds but may regress. Need 7-day test."
     Decision: CONTINUE
```

**What gets saved to Hyphae:**
- Hypothesis chain (both attempts)
- Confidence: 0.4 (passes 1/1 test, but only once)
- Next action: "Verify hypothesis 2 over 7 days"
- SessionShard expires in 7 days

---

### Session 2: Refinement (day 2)

```
Opus loads IntentionPacket from Hyphae.
Sees: "Hypothesis 2 at 0.4 confidence, passed 1 test, next is 7-day verification"

Tests hypothesis 2 again → passes again → confidence moves to 0.5
Tries edge case → finds minor regression → documents it

At end: Decision CONTINUE, "hypothesis holding, edge case found and recorded"
```

**Confidence model updates:**
- evidence: 0.7 (two sources: initial test + edge case finding)
- prediction_accuracy: 0.5 (holds in main path, fails in edge case)
- parsimony: 0.6 (explanation is reasonable but has caveats)
- Overall: 0.4*0.7 + 0.4*0.5 + 0.2*0.6 = 0.58

---

### Session 3: Stuck pattern detection

```
Opus tries to deploy the fix.
It works. Feels good.

Next session (day 5): Fix has regressed.
Opus tries again. Same cycle.

IntentionBroker detects: "symptom_patching" (fix works, regresses, repeat)
Escalates: "We're looping. Need root cause analysis or architectural change."
Decision: ESCALATE
```

**What the next Opus sees:**
```
stuck_pattern: "symptom_patching"
hypothesis_chain: [
  v1 (0.2 confidence, failed),
  v2 (0.58 confidence, regresses),
]
attempts: [
  {session: s1, tried: "fix xrandr", result: "initial success"},
  {session: s2, tried: "edge case test", result: "regression observed"},
  {session: s3, tried: "re-deploy fix", result: "regression again"},
]
```

→ Fresh Opus immediately recognizes the pattern, doesn't waste time re-applying band-aids.

---

### Session N: Convergence

```
Over 7 sessions, hypothesis refines:
  v1: 0.2 (wrong)
  v2: 0.4 (partial)
  v3: 0.65 (getting closer)
  v4: 0.8 (strong)
  v5: 0.95 (extremely confident)

Passes 7-day regression test without incident.

Opus writes prevention (systemd unit, config lock, whatever solves it).
Sets prevention_deployed: true
Sets regression_test_until: 2026-05-09 (30 days)

Decision: COMPLETE
```

→ Intention archived to Hyphae with full evidence trail
→ If regression ever happens again (in 3 months, 2 years), you have the diagnosis

---

## Integration with Calcifer

CalciferManager now has:

```python
# Load a previous intention
intention = calcifer.load_intention("abc123def456")

# Start tracking this session
shard = calcifer.start_intention_session("session_2026_04_09_1430", "abc123def456")

# During work
calcifer.record_discovery("hypothesis 3 holds under edge case X")
calcifer.record_obstacle("blocker: can't test on real hardware yet")

# At session end
calcifer.close_intention_session(
    summary="Hypothesis 3 now at 0.7 confidence, passed 5/6 tests",
    escalation_decision="CONTINUE",
    reason="High confidence but edge case remains"
)

# When done (days/weeks later)
calcifer.save_intention_to_memory()  # Hyphae commit
```

Calcifer speaks:
> "Intention archived to long-term memory!"

---

## The Confluence Across Time

**Without intention layer:**
- Session 1 diagnoses, writes to Markdown
- Session 2 starts fresh, re-reads Markdown, repeats some work
- Session 3 misses the pattern, applies a band-aid that regresses
- Session 4 applies the same band-aid again
- You get frustrated; time wasted

**With intention layer:**
- Session 1 diagnoses, confidence 0.2, next action clear
- Session 2 loads intention, sees the path forward, moves confidence to 0.4
- Session 3 loads intention, detects stuck pattern *automatically*, escalates
- Session 4 (you intervene): "okay, here's the real root cause"
- Session 5 applies root cause fix, confidence jumps to 0.8
- Sessions 6-7 verify with regression testing
- Session 8: intention archived, solved permanently

**Same work, but:**
- No repeated diagnosis
- Stuck patterns caught early
- Confidence metric guides Opus on what to focus on
- Prevention deployed so it doesn't regress

---

## Key Design Decisions

### Why multi-factor confidence?
Because a hypothesis can be:
- Well-evidenced but brittle (high evidence, low parsimony)
- Simple but untested (high parsimony, low evidence)
- Accurate on main path but fails on edge case (high evidence, low prediction)

Blending all three: `0.4*evidence + 0.4*prediction + 0.2*parsimony` prevents overconfidence.

### Why SessionShards expire?
- Prevent memory bloat (7-day TTL)
- Force periodic decision: "keep this or archive it?"
- Keep the recent context warm, old context consolidated

### Why the IntentionPacket lives in Hyphae metadata, not in SessionShards?
- SessionShards are ephemeral; if you lose 7 days, you lose context
- IntentionPacket is authoritative; survives session boundaries
- Broker decides what merges back (Opus can propose, you can veto)

### Why stuck-pattern detection?
Because Opus (you) will happily apply the same band-aid 10 times if the symptom keeps coming back. Automatic detection prevents this.

---

## How to Use This

### In Claude Code:

```python
# At session start
intention = calcifer.load_intention("my_project_goal_id")

# During work
calcifer.record_discovery("...")
calcifer.record_obstacle("...")

# At session end (you or Opus-in-code)
calcifer.close_intention_session(
    summary="...",
    escalation_decision="CONTINUE",  # or ESCALATE, or COMPLETE
    reason="..."
)
```

### In Hyphae queries:

```bash
# Recall a specific intention
curl http://127.0.0.1:8100/recall -d '{"query":"my_intention_id"}'

# Get the full hypothesis chain and confidence scores
# (returned as metadata in the IntentionPacket)
```

### When to merge to Hyphae:

- Intention reaches confidence > 0.8
- Prevention is deployed and regression-tested
- You want to close the loop and move on

```python
calcifer.save_intention_to_memory()
```

Next time someone (you, an agent, etc.) has the same problem, they load it and skip straight to "here's what we know."

---

## Horizon

This is the scaffold for **proactive AI intention**:
- Not "execute this task" (passive)
- But "pursue this goal, learn from failures, notice stuck patterns, escalate when needed" (active)

Each Opus session is a drop of water. But the landscape is carved by *all* drops. The river finds its way.
