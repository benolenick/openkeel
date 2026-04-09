# Calcifer's Ladder V2 — Adaptive CLI Runtime

**Date:** 2026-04-08
**Status:** design draft
**Supersedes:** none
**Coexists with:** `calcifer_ladder_design_2026-04-08.md`
**Related:**
- `calcifers_ladder/critique_2026-04-08.md`
- `calcifers_ladder/architecture_scaffold_2026-04-08.md`
- `calcifers_ladder/conversation_shapes_and_escalation_2026-04-08.md`
- `calcifers_ladder/implementation_blueprint_2026-04-08.md`

---

## One-sentence frame

**Calcifer is an adaptive command-line runtime whose outer supervisory loop chooses the cheapest execution mode that can still make trustworthy progress, including full Claude-style inner agent loops when needed.**

This is a loop within a loop:

- **outer loop**: classify the work, decide context lifetime, choose mode, choose runner, evaluate, escalate
- **inner loop**: when selected, a Claude-style agent runtime performs model -> tool -> model iteration for a bounded step

---

## Why V2 Exists

V1 correctly centered the idea of:

- a pool of runners
- cost-aware selection
- per-iteration re-pick

But V1 under-specified:

- task/session state
- context lifetime
- execution modes
- evaluator contracts
- the difference between quick questions, bounded tasks, and strategic work

V2 keeps the good parts of V1 and makes the runtime model explicit.

---

## Non-negotiable constraints

1. **Proxy at :8787 stays the front door.** Claude Code should keep working through the existing user workflow.
2. **Claude remains in the pool.** The system must be able to degrade to a one-iteration Claude passthrough when that is the correct choice.
3. **Honest accounting only.** `one_metric` remains the source of truth.
4. **Organic to hardware.** No fixed machine tiers. Discovery and measurement determine what exists and what is viable.
5. **Do not over-agentize simple work.** Most turns should remain cheap.

---

## Core Design Principle

Do not build one giant long-lived agent session.

Build:

- disposable turn handling for simple work
- persistent task sessions for real work
- retrieval-backed memory
- explicit escalation between execution modes

The system should feel lightweight for small questions and powerful for large tasks.

---

## The Three Context Layers

### 1. Turn Context

Short-lived.

Used for:
- one-shot questions
- direct commands
- small semantic transformations

Default behavior:
- no persistent task
- no lingering agent
- discard after response

### 2. Task Context

Persistent for a bounded unit of work.

Contains:
- objective
- acceptance criteria
- retained evidence
- current plan
- current step
- task transcript
- budget, evaluator, and permission state

Used for:
- troubleshooting
- bug fixing
- audits
- refactors
- investigations
- strategic engineering work

### 3. Memory Context

Long-term reusable facts, retrieved on demand.

Examples:
- project facts
- machine facts
- prior solutions
- runner performance history
- user preferences

This is a support layer, not the live conversation.

---

## The Four Conversation Shapes

Every turn must first be classified into one of these shapes.

### 1. Instant Answer

Examples:
- "what does this function do?"
- "how do I check disk usage?"
- "summarize this error"

Default execution:
- direct
- semantic

### 2. Guided Troubleshooting

Examples:
- "why isn't my printer printing?"
- "why is wifi dropping?"
- "why is this service crashing?"

Default execution:
- semantic
- bounded task

### 3. Bounded Work Task

Examples:
- "fix this failing test"
- "audit this file"
- "summarize these logs"
- "patch this script"

Default execution:
- bounded task
- local bounded loop

### 4. Strategic Engineering Task

Examples:
- "design an adaptive intelligence ladder"
- "refactor this subsystem"
- "propose a rollout plan"

Default execution:
- planner
- full agent
- judge / delegation

Conversation shape is more important than keywords.

---

## The Execution Modes

These are the real ladder modes. Keep them explicit.

### Mode 1 — Direct

Deterministic commands and typed tools.

Examples:
- read a file
- run `git status`
- grep a pattern

### Mode 2 — Semantic

Cheap one-shot reasoning.

Examples:
- summarize output
- answer a lightweight question
- classify intent
- map natural language to one command

### Mode 3 — Bounded Task

A persistent task session exists, but the work is still linear and small.

Examples:
- troubleshooting tree
- narrow diagnosis
- log triage

### Mode 4 — Local Bounded Loop

A short iterative tool-using loop with a local or cheap runner.

Examples:
- inspect 1-3 files and answer
- gather several command outputs and synthesize
- produce a narrow patch

### Mode 5 — Full Agent Loop

A Claude-style inner loop used for sustained tool use.

Examples:
- multi-step debugging
- multi-file coding
- deeper investigations

### Mode 6 — Planner / Judge / Delegation

Used for:
- strategic planning
- judging outputs
- strong synthesis
- bounded workers / sidecars

---

## Signals For Escalation

The system must not rely primarily on keywords.

Escalation should be driven by a scored profile built from:

- **structural complexity**
- **operational depth**
- **evidence need**
- **verifiability risk**
- **loop difficulty**
- **consequence of error**

This yields an `EscalationProfile`, which suggests the initial execution mode.

Suggested initial mapping:

- `0.0 - 0.2` -> direct
- `0.2 - 0.4` -> semantic
- `0.4 - 0.6` -> bounded task
- `0.6 - 0.8` -> local bounded loop
- `0.8 - 1.0` -> full agent or planner

Hard overrides can still apply:

- existing active task session
- explicit user hint like `@local`, `@sonnet`, `@opus`
- no runner clears quality floor
- missing tool permissions

---

## When Escalation Happens

Escalation does not happen because a request sounds "important."

Escalation happens when the current mode cannot provide enough trustworthy progress.

Typical triggers:

- no answer produced
- insufficient evidence
- deterministic verifier fails
- model judge returns inconclusive
- required tools are unavailable in the current mode
- local loop stalls
- iteration budget is exhausted
- scope expands beyond the current step contract

This keeps the system explainable.

---

## The Main Runtime Objects

### Turn

```python
@dataclass
class Turn:
    user_input: str
    cwd: str
    timestamp: float
    explicit_task_id: str | None = None
```

### Task

```python
@dataclass
class Task:
    id: str
    title: str
    objective: str
    scope: str
    acceptance_criteria: list[str]
    status: str
    summary: str = ""
```

### TaskSession

```python
@dataclass
class TaskSession:
    task_id: str
    messages: list[dict]
    retained_artifacts: list[dict]
    current_plan: list[str]
    current_step: str | None
    active_mode: str | None
    active_runner: str | None
    budget_state: dict
    permission_state: dict
    evaluator_state: dict
```

### StepSpec

This replaces the overloaded V1 `Job` as the per-step execution contract.

```python
@dataclass
class StepSpec:
    step_kind: str
    task_class: str
    quality_floor: float
    latency_ceiling_s: float
    replacement_mode: str       # passthrough | shrink_only | replace_safe
    allowed_tools: list[str]
    requires_task_context: bool
    needs_evaluator: bool
    acceptance_contract_id: str | None
```

### EvaluatorSpec

This replaces free-text `success_check` as the primary verifier contract.

```python
@dataclass
class EvaluatorSpec:
    id: str
    deterministic_checks: list[str]
    judge_task_class: str | None
    min_judge_quality: float
    escalation_policy: str
```

### Runner

```python
@dataclass
class Runner:
    id: str
    kind: str
    device: str
    context_limit: int
    concurrency: int
    cost_per_1k: float
    warm: bool
    supports_permissions: bool
    tool_call_reliability: float
    structured_output_reliability: float
    judge_independence_group: str
    measured: dict[str, TaskMeasurement]
```

---

## The Outer Supervisory Loop

This is the actual center of the design.

```python
def handle_turn(turn: Turn) -> Response:
    task = resolve_or_create_task(turn)
    session = load_or_init_session(task, turn)

    profile = build_escalation_profile(turn, task, session)
    step = classify_next_step(turn, task, session, profile)

    while True:
        runner = router.pick(step, session, profile)
        result = execute_step(step, runner, task, session)
        session = apply_step_result(session, result)

        evaluator = select_evaluator(step, task, session)
        verdict = run_evaluator(evaluator, step, result, task, session)

        if verdict.kind == "pass" and task_is_complete(task, session):
            persist_task_and_session(task, session)
            return finalize_response(task, session, result)

        if verdict.kind == "pass":
            step = derive_next_step(task, session)
            continue

        if verdict.kind == "inconclusive":
            step = escalate_step(step, task, session)
            continue

        if verdict.kind == "fail":
            step = recover_or_escalate(step, task, session)
            continue
```

The outer loop owns:

- task continuity
- context lifetime
- mode selection
- runner selection
- evaluation
- escalation
- persistence

---

## The Inner Agent Loop

The inner loop is a bounded execution engine, not the top-level owner.

When selected, it receives:

- current bounded step objective
- current task/session context
- allowed tools
- permission mode
- budget
- acceptance contract

It may iterate internally:

- model emits tool call
- tool executes
- result appended
- model continues

The outer loop still decides:

- when the inner loop is invoked
- what step it works
- whether the result is accepted
- whether to continue or escalate

---

## Evaluation Model

### Deterministic first

Whenever possible, verify with:

- command exit status
- tests
- diff checks
- schema validation
- grep assertions
- artifact presence checks

### Judge second

Use model judging only when deterministic checks are insufficient.

Rules:

- avoid self-judging when possible
- use a different runner group for judging when practical
- use stronger judges only when ambiguity remains

Evaluator verdicts must be:

- `pass`
- `inconclusive`
- `fail`

---

## Discovery And Measurement

### Discovery

Probe for:

- GPUs
- RAM
- local inference services
- cloud availability
- remote runners

### Benchmark gate

A discovered candidate becomes a real runner only after it passes benchmarking.

Benchmark at least:

- summaries
- QA
- code edits
- judging
- bounded tool-loop reliability

### Measurement updates

Update runner measurements from real traffic using EWMA-style rolling updates.

Store routing-critical measurements locally first, not only in Hyphae.

---

## Tool System

Use typed tools with explicit contracts.

Initial required tools:

- `read_file`
- `search_text`
- `glob_files`
- `run_command`
- `apply_patch`
- `web_fetch`
- `hyphae_recall`
- `hyphae_remember`

Every tool should define:

- input schema
- safety class
- result limits
- execution function

---

## Permission Model

Permissions are part of the architecture, not a later add-on.

Minimum required behaviors:

- tool allow/deny by step
- safe vs dangerous command policy
- non-interactive auto-deny path
- clear fallback when permission blocks the current mode

---

## Task Lifecycle

### Create a task when:

- multiple dependent steps are implied
- verification is required
- artifacts or plan state must persist

### Continue a task when:

- explicit task id is provided
- active session clearly matches the turn
- the follow-up depends on retained context

### Compact a task when:

- transcript grows large
- only summary + retained artifacts are needed

### Close a task when:

- acceptance criteria are met
- user ends it
- it is abandoned

---

## What Most Conversations Should Look Like

The system should not feel heavy by default.

Expected distribution:

- many turns: direct or semantic
- some turns: troubleshooting or bounded tasks
- fewer turns: local bounded loops
- fewest turns: planner/full-agent/delegation

This is how the system can serve both:

- someone asking why their printer is not printing
- someone designing an advanced intelligence ladder

without forcing both through the same heavy pathway.

---

## Front Doors

All front doors should converge on the same runtime.

Initial:

- `tools/token_saver_proxy.py`
- future `LLMOS /api/chat`
- future direct CLI front end

All should call:

```python
calcifer.api.handle_turn(...)
```

---

## Suggested Module Layout

```text
openkeel/calcifer/
├── state.py
├── steps.py
├── runners.py
├── discovery.py
├── measurements.py
├── router.py
├── classify.py
├── evaluators.py
├── tools.py
├── permissions.py
├── loop.py
├── inner_agent.py
├── transcript.py
├── memory.py
└── api.py
```

---

## Delivery Strategy

1. Build state primitives and conversation-shape classification.
2. Build direct + semantic execution.
3. Build discovery + runner registry + measurements.
4. Build bounded task sessions.
5. Build local bounded loop.
6. Build full inner agent adapter.
7. Build planner/judge/delegation.
8. Merge all front doors on the same outer loop.

---

## What V2 Preserves From V1

V2 keeps these V1 strengths:

- organic hardware discovery
- runner pool model
- measurement-based routing
- cost-aware selection
- Claude staying in the pool
- proxy remaining the front door

V2 changes the center of gravity:

from:
- runner routing as the primary idea

to:
- adaptive task-aware runtime, with runner routing inside it

---

## Final Summary

Calcifer's Ladder V2 is:

- a **task-aware supervisory CLI runtime**
- with **explicit conversation shapes**
- with **three context layers**
- with **scored escalation**
- with **persistent task sessions**
- with **typed tools and evaluators**
- and with **Claude-style inner loops used only when justified**

If only one sentence survives:

**The outer loop owns the task and chooses the cheapest mode that can still make trustworthy progress.**
