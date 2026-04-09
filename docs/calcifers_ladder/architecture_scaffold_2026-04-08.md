# Architecture Scaffold — Adaptive CLI Loop

**Date:** 2026-04-08
**Status:** scaffold
**Purpose:** replace the current mental model with a concrete architecture for an adaptive command-line agent that escalates intelligence usage based on the job

---

## One-sentence frame

**Calcifer is an outer supervisory CLI loop that chooses the cheapest sufficient execution mode for each step, including full Claude-style inner agent loops when needed.**

This is a loop within a loop:

- **outer loop**: classify, route, budget, persist, judge, escalate
- **inner loop**: a chosen agent runtime does model -> tool -> model iteration

---

## Core Principle

Do not build "one giant agent session."

Build:

- cheap direct paths for simple work
- bounded task sessions for real work
- retrieval-backed memory for continuity
- strong-agent inner loops only when a step demands them

---

## The Three Context Layers

### 1. Turn Context

Short-lived. For quick questions and one-shot actions.

Examples:
- "what does this function do?"
- "show git status"
- "summarize this error"

Default behavior:
- no persistent task
- no lingering agent
- discard after response

### 2. Task Context

Persistent for a bounded piece of work.

Examples:
- fix a bug
- audit a file
- refactor a module
- investigate a deployment issue

This context persists for the life of the task and includes:

- objective
- acceptance criteria
- retained evidence
- current plan
- active artifacts
- task-scoped transcript

### 3. Memory Context

Long-term reusable facts, retrieved on demand.

Examples:
- project conventions
- infrastructure facts
- prior solutions
- runner measurements
- user preferences

This is support context, not the live transcript.

---

## Main Objects

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
    status: str              # new | in_progress | blocked | done | abandoned
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
    active_runner: str | None
    budget_state: dict
    permission_state: dict
    evaluator_state: dict
```

### StepSpec

`StepSpec` is the real unit of execution. It replaces the overworked `Job`.

```python
@dataclass
class StepSpec:
    step_kind: str           # direct | semantic | local_loop | full_agent | judge | planner
    task_class: str
    quality_floor: float
    latency_ceiling_s: float
    replacement_mode: str    # passthrough | shrink_only | replace_safe
    allowed_tools: list[str]
    requires_task_context: bool
    needs_evaluator: bool
    acceptance_contract_id: str | None
```

### EvaluatorSpec

```python
@dataclass
class EvaluatorSpec:
    id: str
    deterministic_checks: list[str]
    judge_task_class: str | None
    min_judge_quality: float
    escalation_policy: str   # retry | escalate_runner | escalate_mode | fail
```

### Runner

Extend the current runner concept:

```python
@dataclass
class Runner:
    id: str
    kind: str
    device: str
    context_limit: int
    cost_per_1k: float
    warm: bool
    concurrency: int
    supports_permissions: bool
    tool_call_reliability: float
    structured_output_reliability: float
    judge_independence_group: str
    measured: dict[str, TaskMeasurement]
```

---

## Execution Modes

Calcifer should keep explicit execution modes. This is the real ladder.

### Mode 1 — Direct

Deterministic command or tool invocation.

Examples:
- `git status`
- file read
- grep
- known-safe utility actions

### Mode 2 — Semantic

Cheap reasoning for one-shot interpretation.

Examples:
- classify intent
- produce one command
- summarize tool output
- extract structured fields

### Mode 3 — Local Bounded Loop

A short tool-using loop with a local or cheap runner.

Examples:
- inspect 1-3 files and answer
- small refactor with narrow scope
- summarize several command outputs

### Mode 4 — Full Agent Loop

A Claude-style inner loop:
- model emits tool calls
- tools execute
- results appended
- repeat until terminal

Use when the step requires sustained tool use and stateful iteration.

### Mode 5 — Planner / Judge / Delegation

Reserved for:
- high-level planning
- evaluation of candidate work
- specialized sub-agents
- expensive cloud brains

---

## Outer Supervisory Loop

```python
def handle_turn(turn: Turn) -> Response:
    task = task_manager.resolve_or_create(turn)
    state = session_store.load_or_init(task, turn)

    step = classify_next_step(turn, task, state)

    while True:
        runner = router.pick(step, state)
        result = execute_step(step, runner, state)
        state = apply_result(state, result)

        verdict = evaluate_step(step, result, state)

        if verdict == "pass" and task_is_complete(state):
            persist_and_close(task, state)
            return finalize(state)

        if verdict == "pass":
            step = derive_next_step(task, state)
            continue

        if verdict == "inconclusive":
            step = escalate_step(step, state)
            continue

        if verdict == "fail":
            step = recover_or_escalate(step, state)
            continue
```

This loop owns:

- context lifetime
- task continuity
- escalation
- acceptance
- final judgment

---

## Inner Agent Loop

When the chosen mode is `full_agent`, Calcifer invokes a Claude-style runtime with:

- current messages
- current step objective
- tool registry
- permission state
- budget
- current acceptance contract

That runtime may itself iterate many times.

The key rule:

**the inner loop does not own the task.**

The outer loop still decides:

- when the inner loop starts
- what bounded step it receives
- what tools/policies it gets
- whether its result is accepted

---

## Context Lifetime Rules

### Quick Question

If the turn is informational and self-contained:

- use Turn Context only
- do not create persistent TaskSession
- clear after response

### Real Task

If the turn implies multiple steps, verification, or ambiguity:

- create or continue a Task
- keep TaskSession alive
- retain only useful artifacts
- compact stale transcript into summary when needed

### Memory

Retrieve memory into a task only when relevant.

Do not carry memory as a permanent prompt tail.

---

## Planning Model

Do not front-load a giant master plan.

Use progressive planning:

1. define objective
2. define acceptance criteria
3. gather minimal context
4. create a short plan
5. execute one or two steps
6. re-plan based on evidence

This is how big tasks remain tractable.

---

## Evaluator Model

Always prefer deterministic checks first.

Examples:
- command exit status
- test pass/fail
- diff match
- schema validation
- grep assertions

Use model judges only when deterministic checks cannot answer the question.

Judge rules:
- judge should be independent from the worker when possible
- local model judging itself is weak evidence
- expensive judge only when uncertainty remains

---

## Router Contract

The router should pick against `StepSpec`, not against a whole task.

Inputs:
- step requirements
- current state
- runner capability
- quality floor
- latency ceiling
- cost aggression
- replacement mode

Output:
- chosen runner
- reason for selection

Routing should consider more than cost and quality:

- tool-call reliability
- supports step mode
- can act as judge
- cold start
- structured output reliability
- permission compatibility

---

## Suggested File Layout

```text
openkeel/calcifer/
├── __init__.py
├── state.py              # Turn, Task, TaskSession, persistence
├── steps.py              # StepSpec, EvaluatorSpec, task classes
├── runners.py            # Runner + registry
├── discovery.py          # hardware/service discovery
├── measurements.py       # runner measurements + EWMA updates
├── router.py             # pick(step, state) -> runner
├── classify.py           # classify_next_step(...)
├── evaluators.py         # deterministic + judge evaluators
├── loop.py               # outer supervisory loop
├── inner_agent.py        # adapter to Claude-style agent loop
├── tools.py              # typed tools
├── permissions.py        # permission model and policy
├── transcript.py         # task/session transcript + compaction
├── memory.py             # Hyphae retrieval/store bridge
└── api.py                # shared entrypoints for proxy and future UI
```

Front doors:

```text
tools/token_saver_proxy.py   -> calcifer.api.handle_turn(...)
LLMOS /api/chat              -> calcifer.api.handle_turn(...)
future CLI                   -> calcifer.api.handle_turn(...)
```

---

## Build Order

### Step 1

Build state primitives:
- `Turn`
- `Task`
- `TaskSession`

### Step 2

Build discovery + runner registry + measurements.

### Step 3

Build direct and semantic modes only.

### Step 4

Build evaluator layer and task persistence.

### Step 5

Build local bounded loop.

### Step 6

Build full inner agent adapter.

### Step 7

Merge proxy and `/api/chat` onto the same outer loop.

---

## The Right Mental Model

Calcifer is not "a router over model calls."

Calcifer is:

- a **task-aware supervisory runtime**
- with **explicit context lifetimes**
- with **step-level escalation**
- with **typed tools**
- with **structured evaluation**
- and with **inner agent loops used as bounded execution engines**

If you only remember one sentence:

**The outer loop owns the task; the inner loop only works a step of the task.**
