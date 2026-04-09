# Implementation Blueprint — Calcifer Adaptive CLI Loop

**Date:** 2026-04-08
**Status:** build blueprint
**Audience:** another agent or engineer implementing the system
**Purpose:** provide a complete architecture and delivery plan for an adaptive command-line runtime that escalates intelligence usage based on the shape of work

---

## 1. Mission

Build an adaptive CLI runtime that:

- answers simple requests cheaply
- creates persistent task sessions for real work
- escalates reasoning and model strength only when justified
- can invoke a Claude-style inner agent loop as one execution mode
- preserves the current proxy front door and honest accounting

This is **not** a single always-on agent.

It is:

- an **outer supervisory loop**
- over several **execution modes**
- with a **stateful task/session model**
- and optional **inner agent loops**

---

## 2. Core Design

### 2.1 The Loop Within The Loop

The system has two levels:

#### Outer loop

Owns:
- turn ingestion
- task creation / continuation
- conversation-shape classification
- escalation scoring
- execution-mode selection
- runner selection
- evaluation
- persistence
- final judgment

#### Inner loop

Used only when needed.

A chosen runner may execute a Claude-style tool loop:
- model proposes action
- tool executes
- result is appended
- model continues

The outer loop remains authoritative. The inner loop never owns the task.

---

## 3. Context Model

The system must use three context layers.

### 3.1 Turn Context

Short-lived.

Used for:
- one-shot questions
- direct commands
- brief semantic transforms

Cleared after response unless a task is created.

### 3.2 Task Context

Persistent across turns for a bounded job.

Contains:
- objective
- acceptance criteria
- scope
- current plan
- current step
- retained evidence
- session transcript
- budget / permissions / evaluator state

### 3.3 Memory Context

Long-term reusable facts retrieved on demand.

Contains:
- project conventions
- infra facts
- learned runner measurements
- user preferences
- historical solutions

Do not keep this permanently in live prompt context. Retrieve only when relevant.

---

## 4. Conversation Shapes

Every incoming turn must be classified into one of these shapes.

### 4.1 Instant Answer

Examples:
- what does this function do
- how do I check disk space
- summarize this error

Preferred modes:
- direct
- semantic

### 4.2 Guided Troubleshooting

Examples:
- printer not printing
- service crashing
- wifi dropping

Preferred modes:
- semantic
- bounded task

### 4.3 Bounded Work Task

Examples:
- fix this test
- audit this file
- summarize these logs
- update this script

Preferred modes:
- bounded task
- local bounded loop

### 4.4 Strategic Engineering Task

Examples:
- design an intelligence ladder
- refactor a subsystem
- propose architecture
- plan rollout

Preferred modes:
- planner
- full agent
- judge / delegation

---

## 5. Execution Modes

These are the real ladder modes. Keep them explicit.

### Mode 1 — Direct

Deterministic commands and tools.

Examples:
- file read
- grep
- safe shell command
- status query

### Mode 2 — Semantic

Cheap one-shot reasoning without a long-lived tool loop.

Examples:
- classify intent
- summarize output
- answer lightweight question
- map NL to a command

### Mode 3 — Bounded Task

Persistent task session exists, but the work is still linear and small.

Examples:
- troubleshooting tree
- narrow diagnosis
- artifact triage

### Mode 4 — Local Bounded Loop

Short iterative tool-use loop with a local or cheap runner.

Examples:
- inspect several files
- produce a small patch
- gather evidence from commands and summarize

### Mode 5 — Full Agent Loop

A sustained Claude-style tool loop.

Examples:
- debugging
- multi-file coding
- deep investigation
- long iterative execution

### Mode 6 — Planner / Judge / Delegation

Used for:
- strategic planning
- evaluation of results
- stronger synthesis
- bounded workers / sidecars

---

## 6. Primary Data Structures

These are the minimum primitives the implementation must define.

### 6.1 Turn

```python
@dataclass
class Turn:
    user_input: str
    cwd: str
    timestamp: float
    explicit_task_id: str | None = None
    front_door: str = "proxy"   # proxy | api_chat | cli | other
```

### 6.2 Task

```python
@dataclass
class Task:
    id: str
    title: str
    objective: str
    scope: str
    acceptance_criteria: list[str]
    status: str                 # new | in_progress | blocked | done | abandoned
    created_at: float
    updated_at: float
    summary: str = ""
```

### 6.3 TaskSession

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
    last_activity_at: float
```

### 6.4 StepSpec

`StepSpec` is the execution contract for one step.

```python
@dataclass
class StepSpec:
    step_kind: str              # direct | semantic | bounded_task | local_loop | full_agent | planner | judge
    task_class: str
    quality_floor: float
    latency_ceiling_s: float
    replacement_mode: str       # passthrough | shrink_only | replace_safe
    allowed_tools: list[str]
    requires_task_context: bool
    needs_evaluator: bool
    acceptance_contract_id: str | None
```

### 6.5 EvaluatorSpec

```python
@dataclass
class EvaluatorSpec:
    id: str
    deterministic_checks: list[str]
    judge_task_class: str | None
    min_judge_quality: float
    escalation_policy: str      # retry | escalate_runner | escalate_mode | fail
```

### 6.6 Runner

```python
@dataclass
class Runner:
    id: str
    kind: str                   # local | cloud | remote
    device: str
    context_limit: int
    concurrency: int
    cost_per_1k: float
    warm: bool
    supports_permissions: bool
    tool_call_reliability: float
    structured_output_reliability: float
    judge_independence_group: str
    measured: dict[str, "TaskMeasurement"]
```

### 6.7 TaskMeasurement

```python
@dataclass
class TaskMeasurement:
    p50_s: float
    p95_s: float
    quality: float
    success_rate: float
    n: int
```

### 6.8 EscalationProfile

```python
@dataclass
class EscalationProfile:
    conversation_shape: str
    structural_complexity: float
    operational_depth: float
    evidence_need: float
    verifiability_risk: float
    loop_difficulty: float
    consequence_of_error: float
    escalation_score: float
```

---

## 7. Routing And Escalation

### 7.1 Do Not Use Keywords As The Main Driver

Keywords are allowed only as weak priors.

Primary routing must use a scored profile built from:

- structural complexity
- operational depth
- evidence need
- verifiability risk
- loop difficulty
- consequence of error

### 7.2 Escalation Score

Implement a cheap classifier:

```python
def build_escalation_profile(turn: Turn, task: Task | None, session: TaskSession | None) -> EscalationProfile:
    ...
```

The classifier can initially be heuristic. It does not need a model at first.

Suggested score bands:

- `0.0 - 0.2` -> direct
- `0.2 - 0.4` -> semantic
- `0.4 - 0.6` -> bounded task
- `0.6 - 0.8` -> local bounded loop
- `0.8 - 1.0` -> full agent or planner

### 7.3 Hard Overrides

Hard constraints override score bands:

- explicit user hint like `@local`, `@sonnet`, `@opus`
- existing active task session
- no local runner clears the quality floor
- missing tool permissions
- forced passthrough mode

### 7.4 Escalation Trigger

Escalation must happen when the current mode cannot produce enough trustworthy progress.

Trigger conditions:

- no result
- insufficient evidence
- deterministic verifier failed
- judge returned inconclusive
- step required tools unavailable in current mode
- local loop stalled
- iteration budget exhausted

---

## 8. Evaluation

### 8.1 Deterministic First

Always prefer deterministic evaluation when available.

Examples:
- command success
- tests passed
- diff matched expectation
- schema validation
- grep assertions
- presence/absence of required artifacts

### 8.2 Judge Second

Use model judging only when deterministic checks are insufficient.

Rules:
- avoid self-judging when possible
- judge should be in a different independence group than the worker
- expensive judge only when ambiguity remains

### 8.3 Verdict Types

All evaluators must return one of:

- `pass`
- `inconclusive`
- `fail`

And a reason payload.

---

## 9. Outer Supervisory Loop

This is the core orchestrator.

```python
def handle_turn(turn: Turn) -> dict:
    task = resolve_or_create_task(turn)
    session = load_or_init_session(task, turn)

    profile = build_escalation_profile(turn, task, session)
    step = classify_next_step(turn, task, session, profile)

    while True:
        runner = pick_runner(step, session, profile)
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

Responsibilities:

- task continuity
- context lifetime
- mode selection
- runner selection
- evaluation
- escalation
- persistence

---

## 10. Inner Agent Loop

The inner loop is used only in certain modes.

### 10.1 Contract

The outer loop calls an inner agent adapter with:

- bounded step objective
- task/session context summary
- allowed tools
- permission mode
- budget
- acceptance contract

### 10.2 Allowed Implementations

Examples:
- Anthropic Claude-style agent loop
- local model agent loop
- remote agent runtime

### 10.3 Rule

The inner loop may work the step.
It may not redefine the task.

The outer loop remains in charge of:
- task ownership
- persistence
- evaluation
- escalation

---

## 11. Permissions

Permissions are mandatory in the architecture.

Minimum required behaviors:

- know which tools are allowed for a step
- know when a prompt/approval is needed
- know when the step must downgrade or fail because permission is unavailable

Initial implementation can be simple:

- allowlists per tool
- safe vs dangerous shell commands
- non-interactive auto-deny path

Do not postpone permission architecture entirely. It is part of the loop.

---

## 12. Tool System

Use typed tools with stable contracts.

Initial tool set:

- `read_file`
- `search_text`
- `glob_files`
- `run_command`
- `apply_patch`
- `web_fetch`
- `hyphae_recall`
- `hyphae_remember`

Optional later:

- `kanban_list`
- `kanban_update`
- `spawn_worker`
- `judge_result`

Every tool should expose:

- name
- input schema
- safety level
- max result size
- execution function

---

## 13. Discovery And Runner Registry

### 13.1 Discovery

Probe for:
- local GPUs
- RAM
- Ollama models
- llama.cpp / vLLM servers
- cloud availability
- remote runners

### 13.2 Benchmark Gate

A candidate becomes a registered runner only after a benchmark pass.

Must measure:
- summaries
- QA
- code edit correctness
- judge agreement
- bounded tool-loop reliability

### 13.3 Measurement Store

Store runner measurements locally first.

Recommendation:
- SQLite for primary storage
- Hyphae for optional supporting memory, not routing-critical state

---

## 14. Task Lifecycle

### 14.1 Create

Create a task when:
- the request implies multiple dependent steps
- verification is required
- the system must retain artifacts or plan state

### 14.2 Continue

Continue a task when:
- explicit task id is provided
- active session exists and the turn obviously belongs to it
- user follow-up depends on the existing work

### 14.3 Compact

Compact long sessions into:
- task summary
- retained artifacts
- current plan
- open issues

Do not carry full raw transcript forever.

### 14.4 Close

Close a task when:
- acceptance criteria are satisfied
- user ends it
- it is abandoned

---

## 15. What Most Conversations Should Look Like

Expected shape:

- many turns: instant answer
- some turns: troubleshooting or bounded tasks
- fewer turns: local bounded loops
- fewest turns: planner/full-agent/delegation

The system should feel lightweight most of the time.

It should only feel "agentic" when the work really is agentic.

---

## 16. Real-Time Behavior

For every incoming request:

1. check whether it belongs to an existing task
2. build escalation profile
3. classify next step
4. choose mode
5. choose runner
6. execute
7. evaluate
8. escalate if needed
9. persist useful state

This path must be cheap at the front.

Do not invoke the full inner agent loop until the score, task shape, and evaluator contract justify it.

---

## 17. Required Modules

Implement these modules in `openkeel/calcifer/`:

```text
state.py
steps.py
runners.py
discovery.py
measurements.py
router.py
classify.py
evaluators.py
tools.py
permissions.py
loop.py
inner_agent.py
transcript.py
memory.py
api.py
```

### Module responsibilities

#### `state.py`
- `Turn`
- `Task`
- `TaskSession`
- persistence helpers

#### `steps.py`
- `StepSpec`
- `EvaluatorSpec`
- task class taxonomy

#### `runners.py`
- `Runner`
- registry
- runner lookup and status

#### `discovery.py`
- hardware/service probing
- candidate runner discovery

#### `measurements.py`
- benchmark results
- EWMA updates
- score history

#### `router.py`
- select runner for a given `StepSpec`

#### `classify.py`
- conversation shape classifier
- escalation profile builder
- next-step classifier

#### `evaluators.py`
- deterministic checks
- judge orchestration
- verdict normalization

#### `tools.py`
- typed tool registry
- tool execution helpers

#### `permissions.py`
- policy model
- shell safety checks
- approval handling

#### `loop.py`
- outer supervisory loop

#### `inner_agent.py`
- adapter to Claude-style tool loop
- local loop adapter

#### `transcript.py`
- task/session transcript
- compaction and summary

#### `memory.py`
- Hyphae bridge

#### `api.py`
- single entrypoint for proxy, UI, and future CLI

---

## 18. Front Doors

All external entrypoints should converge on the same runtime.

Initial front doors:

- `tools/token_saver_proxy.py`
- future `LLMOS /api/chat`
- future direct CLI entrypoint

All should call a shared function:

```python
calcifer.api.handle_turn(...)
```

---

## 19. Delivery Phases

### Phase 1 — State And Classification

Build:
- state primitives
- conversation shapes
- escalation score
- task create/continue logic

No inner agent loop yet.

### Phase 2 — Direct + Semantic Modes

Build:
- direct mode
- semantic mode
- deterministic evaluators

### Phase 3 — Discovery And Runner Registry

Build:
- discovery
- runner registry
- measurements storage

### Phase 4 — Bounded Task Mode

Build:
- persistent task sessions
- retained artifacts
- transcript compaction

### Phase 5 — Local Bounded Loop

Build:
- local tool loop
- bounded-loop benchmark suite

### Phase 6 — Full Inner Agent Adapter

Build:
- Claude-style inner loop adapter
- bounded step handoff
- result normalization

### Phase 7 — Planner / Judge / Delegation

Build:
- planner seat
- judge seat
- optional worker sidecars

### Phase 8 — Merge Front Doors

Make proxy and `/api/chat` share the same loop runtime.

---

## 20. Non-Negotiable Build Rules

1. Do not let the router become the whole architecture.
2. Do not skip task/session state.
3. Do not rely mainly on keywords.
4. Do not let inner agent loops own top-level tasks.
5. Do not use model judging when deterministic checks are available.
6. Do not keep unbounded transcript context.
7. Do not make local runners replace stronger loops unless the step is explicitly `replace_safe`.

---

## 21. Minimum Success Criteria

The build is successful when:

- simple questions do not create heavy sessions
- multi-step tasks do create persistent task sessions
- escalation is explainable from the score/profile/evaluator contract
- local bounded loops can solve some tasks fully
- full agent loops are used only when justified
- proxy front door still works
- accounting remains honest

---

## 22. Summary

Build Calcifer as:

- a **task-aware supervisory CLI runtime**
- with **explicit conversation shapes**
- with **scored escalation**
- with **persistent task sessions**
- with **typed tools and evaluators**
- and with **Claude-style inner loops used as bounded execution engines**

If only one sentence survives:

**The outer loop owns the task, and chooses the cheapest mode that can still make trustworthy progress.**
