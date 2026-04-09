# Calcifer Broker Sketch

## Goal

Turn `Opus + memory + tools + subagents` into a system that can pursue long-horizon tasks without losing coherence.

The core rule:

- `Opus` handles strategy, ambiguity, and completion judgment.
- `Broker` owns runtime state, routing, verification, and memory.
- `Workers` do bounded execution.
- `Verifiers` check evidence before upward summaries or completion claims.

The system should optimize for:

- cheapest competent action first
- explicit escalation only when justified
- resumability after interruption
- evidence-bearing progress, not prose claims

## Runtime Objects

These objects are the mandatory protocol between all layers.

### 1. `StatusPacket`

Compact current state passed upward or across turns.

```json
{
  "goal_id": "string",
  "user_goal": "string",
  "current_objective": "string",
  "success_criteria": ["string"],
  "constraints": ["string"],
  "relevant_facts": ["string"],
  "open_questions": ["string"],
  "last_directive_id": "string|null",
  "recent_evidence": ["string"],
  "candidate_next_actions": ["string"],
  "risk_level": "low|medium|high",
  "budget_state": {
    "latency_ms": 0,
    "tokens_in": 0,
    "tokens_out": 0,
    "tool_calls": 0
  }
}
```

### 2. `Directive`

Bounded instruction emitted by broker or Opus.

```json
{
  "directive_id": "string",
  "goal_id": "string",
  "parent_directive_id": "string|null",
  "assigned_rung": 0,
  "assigned_executor": "retriever|script|worker|planner|opus",
  "task": "string",
  "scope": "string",
  "allowed_tools": ["string"],
  "forbidden_actions": ["string"],
  "expected_output": "string",
  "success_test": ["string"],
  "timeout_seconds": 0,
  "max_cost_usd": 0,
  "must_escalate_if": ["ambiguity", "risk", "insufficient_evidence"],
  "write_policy": "none|workspace|approved_targets",
  "requires_verifier": true
}
```

### 3. `ExecutionReport`

Mandatory worker output.

```json
{
  "directive_id": "string",
  "executor": "string",
  "status": "succeeded|failed|blocked|partial",
  "summary": "string",
  "actions_taken": ["string"],
  "artifacts": [
    {
      "path": "string",
      "kind": "code|doc|test|log|output"
    }
  ],
  "evidence": [
    {
      "type": "command|test|diff|source|observation",
      "value": "string"
    }
  ],
  "confidence": 0.0,
  "known_unknowns": ["string"],
  "recommended_next_action": "string"
}
```

### 4. `EscalationDecision`

Broker-level routing judgment.

```json
{
  "directive_id": "string",
  "decision": "continue_local|retry_same_rung|change_rung|escalate_to_opus|ask_user|complete|abort",
  "reason": "ambiguity|risk|planning_complexity|insufficient_evidence|completion_check|budget_limit|policy_block",
  "target_rung": 0,
  "notes": "string"
}
```

### 5. `CompletionDecision`

Final satisfaction test.

```json
{
  "goal_id": "string",
  "status": "complete|incomplete|blocked",
  "matched_success_criteria": ["string"],
  "remaining_gaps": ["string"],
  "supporting_evidence": ["string"],
  "should_remember": ["string"]
}
```

## Ladder

The ladder is not metaphorical. It is a registry of executors with explicit contracts.

### Rung 0. Retrieval and reuse

- Inputs: known patterns, recent similar tasks, memory recall, templates
- Cost: near-zero
- Allowed actions: read memory, inspect cache, surface prior directives/reports
- Escalate when: no close match, stale result, conflicting history

### Rung 1. Deterministic execution

- Inputs: scripts, shell commands, test runners, static transforms
- Cost: low
- Allowed actions: narrow local tools, deterministic pipelines
- Escalate when: command fails repeatedly, result needs judgment, output is ambiguous

### Rung 2. Cheap bounded model

- Inputs: local or inexpensive model for narrow tasks
- Use cases: summarization, extraction, classification, first-pass decomposition
- Constraint: no open-ended autonomy, no broad write scope
- Escalate when: low confidence, contradiction, planning load increases

### Rung 3. Specialist worker

- Inputs: bounded subagent with scoped tools and output schema
- Use cases: code edit, research packet, browser action, environment diagnosis
- Constraint: one directive, one clear deliverable, evidence required
- Escalate when: blocked, dependency conflict, risk increased

### Rung 4. Planner/synthesizer

- Inputs: medium/high intelligence model, compressed status only
- Use cases: revise plan, reconcile contradictions, restructure task graph
- Constraint: should issue directives, not do raw labor by default
- Escalate when: architecture changes, completion judgment needed, novel ambiguity remains

### Rung 5. Opus

- Inputs: highest-cost full-context reasoning
- Use cases: strategic reframing, completion decision, safety/risk arbitration, complex synthesis
- Constraint: avoid direct tool labor except when escalation explicitly requires it

## Broker State Machine

The runtime loop:

1. `IngestGoal`
- parse user goal
- normalize constraints
- extract success criteria

2. `RecallContext`
- fetch relevant Hyphae facts
- fetch recent directives/reports for same goal or project
- build initial `StatusPacket`

3. `ClassifyWork`
- decide whether the next step is retrieval, deterministic, bounded-model, specialist, planner, or Opus work
- choose initial rung

4. `IssueDirective`
- emit one bounded `Directive`
- attach cost/time limits and escalation clauses

5. `Execute`
- run selected executor
- collect `ExecutionReport`

6. `Verify`
- run verifier checks on evidence and success tests
- downgrade confidence if evidence is weak or circular

7. `Route`
- produce `EscalationDecision`
- either continue locally, switch rung, escalate, ask user, or finish

8. `Snapshot`
- update working state
- emit a new `StatusPacket`
- persist durable facts only

9. `CompleteOrLoop`
- run `CompletionDecision` if success criteria seem met
- otherwise issue the next directive

## Verifier Layer

No report is accepted upward without verification.

Verifier responsibilities:

- check that directive scope was followed
- reject evidence that only proves file existence
- reject self-referential claims such as "done because code says done"
- compare expected output vs actual artifact
- detect contradictions against prior reports or memory
- assign calibrated confidence

Examples:

- code task: tests, lint, static checks, diff inspection, artifact existence
- research task: source count, contradiction scan, recency check
- ops task: command outputs, health checks, rollback verification

## Memory Policy

Hyphae should store durable knowledge, not every event.

Remember:

- stable project facts
- user preferences
- successful strategies
- repeated failure modes
- key decisions and why they were made
- task state transitions worth resuming

Do not remember:

- transient logs
- low-signal tool chatter
- speculative plans that were abandoned
- redundant summaries

## Anti-Failure Rules

### 1. Cheap-first

The broker should prefer the cheapest rung that can plausibly satisfy the directive.

### 2. Evidence-first

Workers never return plain "done". They return evidence or they fail verification.

### 3. Escalation reasons are typed

Every escalation must name one reason:

- ambiguity
- risk
- planning complexity
- insufficient evidence
- completion check
- budget limit
- policy block

### 4. Opus consumes summaries, not raw exhaust

By default, Opus sees `StatusPacket`, not full tool traces.

### 5. Resume is first-class

At any point, the broker must be able to reconstruct:

- current objective
- latest trusted evidence
- unresolved blockers
- next recommended action

## Minimal Implementation Plan

### Phase 1. Define schemas

- implement the five protocol objects as typed dataclasses or pydantic models
- reject malformed reports at runtime

### Phase 2. Build broker loop

- implement the state machine
- add rung registry and routing policy

### Phase 3. Add verifier hooks

- require verifier pass before summaries are promoted upward
- log weak evidence and confidence downgrades

### Phase 4. Connect memory

- Hyphae recall on ingest
- controlled remember on completion, failure, and major decisions

### Phase 5. Add metrics

- escalation frequency by reason
- fraction of work solved below Opus
- verifier rejection rate
- completion-without-reopen rate
- resume success rate

## Open Questions

- How does the broker calibrate confidence across heterogeneous workers?
- Should verifier capability ever exceed worker capability by policy?
- When should the broker request a second independent worker before escalating?
- How should long-running goals be partitioned across kanban tasks vs internal directives?

## Summary

The holy grail version is not "Opus with helpers."

It is a brokered runtime where:

- Opus thinks strategically
- workers execute bounded directives
- verifiers gate upward trust
- memory preserves durable context
- the ladder selects the cheapest competent path

If this design fails, it will likely fail in one of four places:

- weak verifier discipline
- noisy memory writes
- fuzzy rung boundaries
- uncontrolled escalation back to Opus
