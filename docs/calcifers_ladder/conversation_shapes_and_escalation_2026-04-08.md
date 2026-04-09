# Conversation Shapes And Escalation

**Date:** 2026-04-08
**Status:** working architecture note
**Purpose:** make the adaptive CLI model concrete enough to reason about in real time

---

## Frame

Calcifer should not treat every user input as "a question for a model."

It should first classify the **shape of work** represented by the input, then select the cheapest execution mode that can satisfy that shape with enough confidence.

Escalation should happen when the current mode cannot satisfy the work contract, not because a keyword appeared in the text.

---

## The Four Conversation Shapes

### 1. Instant Answer

Use when the turn is self-contained and likely solvable in one pass.

Examples:
- "what does this function do?"
- "how do I check disk usage?"
- "summarize this error"

Properties:
- no persistent task required
- low retained context
- single-turn success is likely
- deterministic or cheap-semantic execution is preferred

Default modes:
- direct
- semantic

Escalate when:
- evidence is insufficient
- tool inspection is needed
- the user follows up into a real task

---

### 2. Guided Troubleshooting

Use when the user has a practical problem but the next step depends on responses and evidence.

Examples:
- "why isn't my printer printing?"
- "why is wifi dropping?"
- "why does this service keep crashing?"

Properties:
- requires a bounded troubleshooting session
- usually involves a diagnosis tree
- context should persist for the life of the troubleshooting flow
- deterministic checks matter more than broad abstract reasoning

Default modes:
- semantic
- bounded task

Escalate when:
- ambiguity remains after a few checks
- local evidence must be gathered
- the user issue becomes a deeper system investigation

---

### 3. Bounded Work Task

Use when the user wants a concrete outcome that needs multiple steps but is still reasonably scoped.

Examples:
- "fix this failing test"
- "audit this file"
- "summarize these logs and tell me what broke"
- "update this script to support a new flag"

Properties:
- requires persistent task context
- usually needs tools
- often benefits from a local bounded loop
- should retain artifacts, intermediate findings, and acceptance criteria

Default modes:
- bounded task
- local bounded loop

Escalate when:
- a verifier fails
- the scope expands
- the task needs sustained multi-step execution
- planning and execution become tightly coupled

---

### 4. Strategic Engineering Task

Use when the user is doing architecture, research, design, or major implementation planning.

Examples:
- "design an adaptive intelligence ladder"
- "refactor this subsystem"
- "propose a rollout plan"
- "compare architectures and recommend one"

Properties:
- requires persistent task context
- requires progressive planning
- usually spans multiple subproblems
- often needs a planner seat and later judge/evaluator steps

Default modes:
- planner
- full agent
- judge / delegation where useful

Escalate when:
- decomposition is needed
- integration risk rises
- a stronger planner or evaluator is required

---

## Execution Modes

### Mode 1 — Direct

Deterministic command/tool action.

Good for:
- known shell actions
- reads
- searches
- command passthrough

### Mode 2 — Semantic

Cheap one-shot reasoning.

Good for:
- classify intent
- map natural language to one action
- summarize
- extract fields
- answer lightweight questions

### Mode 3 — Bounded Task

Task session exists, but execution is still mostly linear and tightly scoped.

Good for:
- troubleshooting trees
- log review
- patching narrow issues

### Mode 4 — Local Bounded Loop

Short iterative tool-use loop with a local or cheap runner.

Good for:
- inspect 1-3 files
- run a few tools
- produce a bounded answer or patch

### Mode 5 — Full Agent Loop

Claude-style model -> tool -> model iteration for sustained work.

Good for:
- coding tasks
- debugging
- deeper investigations
- multi-step artifact production

### Mode 6 — Planner / Judge / Delegation

Reserved for:
- strategic planning
- independent judgment
- worker specialization
- hard synthesis

---

## Escalation Rules

Escalation should always be tied to a failed or insufficient contract.

### Direct -> Semantic

Escalate when:
- the request is not directly executable
- deterministic output is not enough
- the command result does not answer the user need

### Semantic -> Bounded Task

Escalate when:
- the answer requires evidence not yet gathered
- ambiguity remains after one-shot reasoning
- there is clear multi-step structure

### Bounded Task -> Local Bounded Loop

Escalate when:
- the next step needs iterative tool use
- the task needs retained artifacts
- a verifier requires stronger intermediate reasoning

### Local Bounded Loop -> Full Agent Loop

Escalate when:
- local runner stalls
- tool-choice reliability is poor
- the task becomes multi-file or multi-step enough that a real agent loop is cheaper than repeated retries

### Full Agent Loop -> Planner / Judge / Delegation

Escalate when:
- the task requires top-level decomposition
- completion is ambiguous
- independent review is necessary
- multiple bounded subtasks can run in parallel

---

## Signals Better Than Keywords

Keyword matching can still be a weak prior, but it should not be the main decision maker.

Use a scored signal set instead.

### Signal Group 1 — Structural Complexity

Measure:
- input length
- number of explicit constraints
- number of artifacts mentioned
- number of files/systems/entities referenced
- whether acceptance criteria are implied

Interpretation:
- higher structure often means a bounded task or strategic task

### Signal Group 2 — Operational Depth

Measure whether the request implies:
- observation only
- one action
- multiple dependent actions
- verification after action
- planning before action

Interpretation:
- the more dependency between steps, the higher the mode likely needed

### Signal Group 3 — Evidence Need

Ask:
- can this be answered from the current prompt alone?
- must we inspect files, logs, commands, or devices?
- is user-provided evidence missing?

Interpretation:
- higher evidence need pushes from semantic to task/agent modes

### Signal Group 4 — Verifiability

Ask:
- can success be checked deterministically?
- is success obvious from the response itself?
- does failure create risk?

Interpretation:
- low self-verifiability requires stronger evaluation and often stronger runners

### Signal Group 5 — Loop Difficulty

Estimate:
- likely number of iterations
- likely number of tools needed
- whether tool outputs will need shrinking/compaction
- whether state continuity matters

Interpretation:
- this predicts whether a full agent loop is justified

### Signal Group 6 — Cost Of Being Wrong

Ask:
- is a wrong answer cheap or expensive?
- is this casual advice, system administration, code change, architecture, or safety-sensitive support?

Interpretation:
- high consequence raises the floor and often the evaluator strength

---

## A More Honest Escalation Score

Instead of:

- "contains keyword X -> use runner Y"

Use:

```python
escalation_score = f(
    structural_complexity,
    operational_depth,
    evidence_need,
    verifiability_risk,
    loop_difficulty,
    consequence_of_error,
)
```

Then map score ranges to a preferred initial mode.

Example:

- `0.0 - 0.2` -> direct
- `0.2 - 0.4` -> semantic
- `0.4 - 0.6` -> bounded task
- `0.6 - 0.8` -> local bounded loop
- `0.8 - 1.0` -> full agent or planner

This should still be overruled by hard constraints:
- explicit user hint
- missing permissions
- no local runner clears floor
- task already has an active session

---

## Real-Time Flow

For every input:

1. classify conversation shape
2. compute escalation score
3. determine whether this belongs to an existing task
4. choose initial execution mode
5. choose runner for that mode
6. execute
7. evaluate
8. if insufficient, escalate mode or runner
9. persist useful task state

This is fast because steps 1-4 are cheap.

The system should not do full strategic planning unless the turn shape and score justify it.

---

## Example — Printer Troubleshooting

User:
"why isn't my printer printing?"

Likely signal profile:
- structural complexity: low
- operational depth: medium
- evidence need: medium
- verifiability risk: medium
- loop difficulty: medium
- consequence of error: low

Initial mode:
- bounded task

Flow:
1. create troubleshooting task session
2. ask 1-2 discriminating questions
3. recommend next deterministic checks
4. if user provides results and ambiguity remains, escalate to local bounded loop or stronger reasoning

No need to start with a planner or full coding-agent loop.

---

## Example — Advanced Architecture Work

User:
"help me build an adaptive intelligence ladder for a CLI runtime"

Likely signal profile:
- structural complexity: high
- operational depth: high
- evidence need: medium
- verifiability risk: high
- loop difficulty: high
- consequence of error: medium-high

Initial mode:
- planner / strategic task

Flow:
1. create task session
2. define objective and acceptance criteria
3. produce shallow plan
4. inspect relevant artifacts
5. alternate between planning, execution, critique, and revision

---

## Bottom Line

The system should escalate based on:

- shape of work
- need for evidence
- difficulty of the required loop
- ability to verify correctness
- cost of being wrong

not merely on keywords.

If you only remember one sentence:

**Escalation should happen when the current mode cannot provide enough trustworthy progress for the shape of work in front of it.**
