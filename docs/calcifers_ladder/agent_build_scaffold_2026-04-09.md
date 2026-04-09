# Calcifer Ladder Agent Build Scaffold

**Date:** 2026-04-09
**Status:** implementation scaffold
**Audience:** an agent or engineer who will build the runtime
**Primary decision:** pursue the ground-up broker runtime; treat Claude CLI modifications as a temporary bridge, fallback, and measurement harness

## 1. Goal

Build the system so the user talks to **Opus only**, while Opus acts only as:

- interpreter of user intent
- coordinator of work
- escalation/judgment seat
- translator of outcomes back to the user

Everything else should happen below Opus using the cheapest acceptable execution mode.

The target behavior is:

1. User speaks to Opus.
2. Opus derives intent and acceptance criteria.
3. A broker chooses the least capable acceptable execution method.
4. Workers execute the task.
5. Workers return compressed status, not raw transcripts by default.
6. Opus reviews that status and either:
   - declares completion,
   - asks for another bounded step,
   - escalates,
   - or requests raw evidence by exception.
7. Opus translates the state back to the user in high-level language.

## 2. Architectural Decision

Two approaches were considered:

- **Top-down:** keep modifying Claude CLI so it delegates as much as possible.
- **Ground-up:** build a new brokered CLI/runtime and use Claude CLI as one optional premium runner.

The recommendation is:

- **Mainline:** ground-up runtime in `openkeel/calcifer/`
- **Bridge only:** keep Claude CLI hooks/proxy alive for continuity, measurement, and fallback

Reason:

- the top-down path cannot fully separate Opus from execution because Claude CLI still owns the session, transcript, tool loop, and much of the token economics
- the ground-up path gives us control over task state, escalation, routing, context boundaries, and summary compression
- the desired behavior requires Opus to be a seat inside the system, not the shell that the whole system is trapped inside

## 3. Non-Negotiables

1. **Opus is not the laborer.**
   Opus should not do default file inspection, log grinding, grep review, or raw execution unless the step explicitly requires top-rung judgment.

2. **Cheap first, escalate by contract failure.**
   Do not escalate because of decorative keywords alone. Escalate because the current mode cannot satisfy the step contract.

3. **Raw evidence does not climb upward by default.**
   Workers emit structured status packets. Raw tool output is available only on demand.

4. **Claude CLI stays available as a runner, not as the runtime.**
   The existing proxy and hooks remain transitional infrastructure.

5. **One honest metric remains in force.**
   `python3 -m openkeel.token_saver.one_metric` is still the truth meter for cost effectiveness.

## 4. Target Runtime Shape

The system should have four layers.

### 4.1 User / Opus Layer

Responsibilities:

- understand what the user actually wants
- preserve intent and invariants
- decide whether current progress is acceptable
- translate internal state back into user-facing language

Inputs Opus should see by default:

- `IntentionPacket`
- current `TaskSession` summary
- latest `StatusPacket`
- explicit completion or escalation question

Inputs Opus should **not** see by default:

- raw bash output
- raw grep output
- full file bodies
- long worker transcripts
- internal retry chatter

### 4.2 Broker Layer

Responsibilities:

- own task/session state
- classify work shape
- derive next `StepSpec`
- choose execution mode
- choose runner
- enforce summary-up contracts
- decide when to ask Opus for planning or judgment

This is the actual center of the runtime.

### 4.3 Worker Layer

Responsibilities:

- perform bounded execution
- use tools within an allowed scope
- emit structured status packets

Worker examples:

- direct deterministic tool runner
- local semantic runner
- local bounded-loop runner
- Sonnet full-agent runner
- Opus planner/judge runner

### 4.4 Evidence / Compression Layer

Responsibilities:

- compress tool outputs
- normalize worker results
- summarize results for the broker
- summarize broker state upward for Opus

This layer prevents context blowup and keeps Opus out of the weeds.

## 5. Core Runtime Contracts

These must be implemented first and used consistently.

### 5.1 `IntentionPacket`

Purpose:

- capture what the system must not betray while helping the user

Minimum fields:

- `goal_id`
- `user_request`
- `intended_outcome`
- `why_it_matters`
- `must_preserve`
- `allowed_tradeoffs`
- `forbidden_tradeoffs`
- `success_shape`
- `failure_shape`
- `preference_signals`
- `ambiguities`
- `assumptions_in_force`
- `confidence`

Reference:

- `docs/calcifers_ladder/intention_packet_2026-04-08.md`

### 5.2 `Task`

Purpose:

- persistent bounded job record

Minimum fields:

- `id`
- `title`
- `objective`
- `scope`
- `acceptance_criteria`
- `status`
- `created_at`
- `updated_at`
- `summary`

### 5.3 `TaskSession`

Purpose:

- live state for the active bounded job

Minimum fields:

- `task_id`
- `messages`
- `retained_artifacts`
- `current_plan`
- `current_step`
- `active_runner`
- `budget_state`
- `permission_state`
- `evaluator_state`
- `last_status_packet`

### 5.4 `StepSpec`

Purpose:

- single executable unit of work

Minimum fields:

- `step_id`
- `step_kind`
- `task_class`
- `quality_floor`
- `latency_ceiling_s`
- `replacement_mode`
- `allowed_tools`
- `requires_task_context`
- `needs_evaluator`
- `acceptance_contract`
- `escalation_policy`

### 5.5 `StatusPacket`

Purpose:

- default output from every worker step

Minimum fields:

- `step_id`
- `objective`
- `actions_taken`
- `artifacts_touched`
- `result_summary`
- `acceptance_checks`
- `uncertainties`
- `risks`
- `needs_escalation`
- `recommended_next_step`
- `raw_evidence_refs`

Rule:

- every worker returns this, even if the step failed

### 5.6 `CompletionDecision`

Purpose:

- broker or Opus-level decision about next state

Minimum values:

- `done`
- `continue`
- `retry`
- `escalate_runner`
- `escalate_mode`
- `request_raw_evidence`
- `blocked`

## 6. Execution Modes

These are the explicit ladder modes the broker may choose from.

### Mode 1: Direct

Examples:

- read file
- grep
- glob
- safe shell status command

Use when:

- deterministic action likely answers the need directly

### Mode 2: Semantic

Examples:

- classify
- summarize
- explain a small result
- map NL to one action

Use when:

- cheap one-shot reasoning is sufficient

### Mode 3: Bounded Task

Examples:

- narrow troubleshooting
- log triage
- scoped file audit

Use when:

- persistent task context is needed, but the work is still mostly linear

### Mode 4: Local Bounded Loop

Examples:

- inspect 1-3 files
- run a few tools
- produce a bounded diagnosis or patch

Use when:

- short iterative execution is needed and a local/cheap runner is acceptable

### Mode 5: Full Agent Loop

Examples:

- deeper debugging
- multi-file coding
- sustained tool iteration

Use when:

- retries and step chaining make a true loop cheaper than repeated one-shot orchestration

### Mode 6: Planner / Judge / Delegation

Examples:

- architecture decisions
- conflict resolution
- task decomposition
- completion judgment

Default seat:

- Opus

## 7. Escalation Policy

Escalation must be driven by contract failure or insufficient confidence.

Escalate when:

- acceptance check failed
- uncertainty remains too high
- scope expanded beyond current `StepSpec`
- local runner stalled or produced low-reliability output
- consequence of error is high enough that stronger judgment is justified

Do not escalate merely because:

- a keyword like `design` or `quick` appeared
- the current runner is less prestigious

## 8. Recommended Initial Runner Set

Keep the first implementation narrow.

### 8.1 Direct Tool Runner

Responsibilities:

- deterministic reads/searches/bash

### 8.2 Local Semantic Runner

Responsibilities:

- cheap summarization/classification/explanation

### 8.3 Local Bounded Loop Runner

Responsibilities:

- short tool-using local loop for narrow tasks

### 8.4 Sonnet Runner

Responsibilities:

- normal coding/debugging execution

### 8.5 Opus Runner

Responsibilities:

- planning
- judgment
- exception handling
- final translation to user

Constraint:

- Opus should not be the default executor

## 9. File Scaffold

This is a recommended minimal build layout under `openkeel/calcifer/`.

### New files

- `contracts.py`
  - dataclasses for `IntentionPacket`, `Task`, `TaskSession`, `StepSpec`, `StatusPacket`, `CompletionDecision`
- `broker.py`
  - top-level orchestration loop
- `session_store.py`
  - task/session persistence
- `step_deriver.py`
  - derive next `StepSpec` from turn + session state
- `runner_registry.py`
  - runner registration and selection
- `executors/direct_runner.py`
  - deterministic tool execution
- `executors/semantic_runner.py`
  - cheap one-shot semantic execution
- `executors/local_loop_runner.py`
  - bounded local loop
- `executors/sonnet_runner.py`
  - Claude Sonnet adapter
- `executors/opus_runner.py`
  - planner/judge adapter
- `status_summarizer.py`
  - normalize worker outputs into `StatusPacket`
- `evaluator.py`
  - acceptance check evaluation
- `handoff.py`
  - temporary bridge between existing proxy/hooks and the new broker

### Existing files likely to reuse or refactor

- `classify.py`
  - may remain the work-shape / escalation scorer
- `intention.py`
  - may seed `IntentionPacket`
- `simple_router.py`
  - useful only as a transitional source of routing heuristics
- `governor.py`
  - likely partial precursor to planner/judge orchestration
- `brain.py`
  - existing context helpers may be reusable
- `conductor.py`
  - may contain concepts to absorb into the broker

## 10. Build Phases

### Phase 0: Transitional Stance

Objective:

- keep current Claude CLI hooks/proxy running as-is
- do not attempt a flag day

Deliverables:

- clear statement in code/docs that Claude CLI is temporary infrastructure, not the target runtime

### Phase 1: Contracts First

Objective:

- create the core runtime dataclasses and use them everywhere new work happens

Deliverables:

- `contracts.py`
- unit tests for serialization and invariants

Done when:

- the broker and runners can exchange only contract objects, not ad hoc dicts

### Phase 2: Minimal Broker

Objective:

- accept a user turn and produce one `CompletionDecision`

Deliverables:

- `broker.py`
- `session_store.py`
- `step_deriver.py`
- `runner_registry.py`

Done when:

- the system can run a simple turn through the broker without depending on Claude CLI internals

### Phase 3: First Vertical Slice

Objective:

- make one end-to-end path real

Path:

- user turn
- `IntentionPacket`
- `TaskSession`
- `StepSpec`
- direct or semantic worker
- `StatusPacket`
- broker decision
- Opus-facing summary

Done when:

- the user can ask a simple question and the system answers through the broker with no raw evidence leaking upward by default

### Phase 4: Bounded Execution

Objective:

- support narrow real work

Deliverables:

- direct runner
- semantic runner
- local bounded loop runner
- evaluator logic

Done when:

- a bounded task can complete with retained state and compressed summaries

### Phase 5: Premium Seats

Objective:

- integrate Sonnet and Opus as explicit seats

Deliverables:

- Sonnet execution adapter
- Opus planner/judge adapter
- exception path for raw evidence request

Done when:

- Opus sees summaries by default and only pulls raw evidence intentionally

### Phase 6: Bridge Existing Stack

Objective:

- let the current proxy/hooks hand work to the broker when appropriate

Deliverables:

- `handoff.py`
- minimal compatibility path from current front door to new runtime

Done when:

- the old stack can route selected turns into the new broker without breaking current workflows

## 11. Acceptance Criteria For The First Real Milestone

Milestone:

- Opus becomes a real planner/judge seat for a narrow path

Required:

1. user turn enters the broker
2. broker creates `IntentionPacket`
3. broker creates or resumes `TaskSession`
4. broker derives `StepSpec`
5. non-Opus runner executes
6. runner returns `StatusPacket`
7. broker asks Opus only for planning/judgment/translation
8. final user-facing answer comes from Opus

Not required yet:

- full replacement of current Claude CLI workflow
- broad model discovery
- perfect routing heuristics
- all worker types

## 12. Risks

### Risk 1: Opus context pollution returns

Failure pattern:

- raw outputs or long worker chatter leak back into Opus by convenience

Countermeasure:

- make `StatusPacket` the only legal default upward interface

### Risk 2: broker becomes a thin wrapper around Sonnet

Failure pattern:

- broker always escalates because contracts are too vague

Countermeasure:

- keep `StepSpec` narrow
- enforce confidence and acceptance checks

### Risk 3: top-down legacy work captures the roadmap again

Failure pattern:

- more time is spent on Claude CLI middleware than on the broker runtime

Countermeasure:

- no major new investment in hook/proxy cleverness unless it directly supports the broker

### Risk 4: summaries become too lossy

Failure pattern:

- Opus cannot judge correctly because packets omit critical evidence

Countermeasure:

- include `uncertainties`, `risks`, and `raw_evidence_refs`
- add explicit raw-evidence pull path

## 13. What The Next Agent Should Do First

1. Create `openkeel/calcifer/contracts.py`
2. Create `openkeel/calcifer/broker.py`
3. Create `openkeel/calcifer/session_store.py`
4. Implement one end-to-end path:
   - simple question
   - semantic runner
   - `StatusPacket`
   - Opus translation
5. Write tests proving raw evidence is not automatically passed upward

## 14. Scope Boundary

This scaffold does **not** require the next agent to:

- delete the current proxy
- remove existing Claude hooks
- solve all routing optimization immediately
- build a full polished CLI before the runtime exists

This scaffold **does** require the next agent to:

- build the brokered runtime as the new center of gravity
- make Opus a seat instead of the loop
- enforce structured status passing
- preserve honest accounting

## 15. One-Sentence Summary

Build a brokered Calcifer runtime where Opus owns intent and judgment, cheap workers own execution, and only compressed status rises upward by default.
