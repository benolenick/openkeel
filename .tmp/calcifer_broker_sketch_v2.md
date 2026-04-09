# Calcifer Broker Sketch v2

## Design Intent

`v1` established the basic brokered runtime:

- `Opus` handles strategy and judgment
- `Broker` owns routing and state
- `Workers` execute bounded directives
- `Verifiers` gate trust

`v2` corrects the main failure modes:

- verifier theater
- summarization drift
- cheap-loop churn
- weak resume semantics
- noisy memory writes

The core goal remains the same:

Build a runtime where high intelligence is used sparingly, but never blinded.

## Core Principles

### 1. Cost efficiency is subordinate to correctness

The system should prefer the cheapest competent path, but must stop optimizing for cost the moment verification, progress, or state integrity degrades.

### 2. Upward trust requires evidence

No executor can advance the task using prose-only claims.

### 3. Summaries are default, not prison

Opus should consume compressed state by default, but the broker must support targeted raw-evidence inspection when confidence drops.

### 4. Resume requires environment awareness

Task continuity depends on more than reports. The runtime must track filesystem and environment state that materially affects execution.

### 5. Memory should capture surprise, not chatter

The system should store information that changed the plan, contradicted prior assumptions, or proved durable.

## Runtime Objects

The five protocol objects remain, but `v2` adds stronger fields.

### 1. `StatusPacket`

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
  "progress_velocity": "advancing|stalled|regressing",
  "state_checkpoint": {
    "workspace_fingerprint": "string",
    "env_fingerprint": "string",
    "dependency_fingerprint": "string"
  },
  "budget_state": {
    "latency_ms": 0,
    "tokens_in": 0,
    "tokens_out": 0,
    "tool_calls": 0,
    "usd_spend": 0
  }
}
```

### 2. `Directive`

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
  "evidence_standard": "filesystem|test_suite|semantic_review|source_backed|runtime_probe",
  "timeout_seconds": 0,
  "max_cost_usd": 0,
  "max_retries_same_rung": 0,
  "must_escalate_if": [
    "ambiguity",
    "risk",
    "insufficient_evidence",
    "state_drift",
    "stalled_progress"
  ],
  "write_policy": "none|workspace|approved_targets",
  "requires_verifier": true
}
```

### 3. `ExecutionReport`

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
      "type": "command|test|diff|source|observation|probe",
      "value": "string"
    }
  ],
  "state_delta": {
    "files_touched": ["string"],
    "env_changed": ["string"],
    "dependencies_changed": ["string"]
  },
  "confidence": 0.0,
  "known_unknowns": ["string"],
  "recommended_next_action": "string"
}
```

### 4. `EscalationDecision`

```json
{
  "directive_id": "string",
  "decision": "continue_local|retry_same_rung|change_rung|escalate_to_opus|ask_user|complete|abort",
  "reason": "ambiguity|risk|planning_complexity|insufficient_evidence|completion_check|budget_limit|policy_block|state_drift|stalled_progress",
  "target_rung": 0,
  "requires_raw_context": false,
  "notes": "string"
}
```

### 5. `CompletionDecision`

```json
{
  "goal_id": "string",
  "status": "complete|incomplete|blocked",
  "matched_success_criteria": ["string"],
  "remaining_gaps": ["string"],
  "supporting_evidence": ["string"],
  "surprises_learned": ["string"],
  "should_remember": ["string"]
}
```

## Ladder v2

The ladder still routes by cheapest competent path, but every rung now has explicit verification and churn limits.

### Rung 0. Retrieval and reuse

- Role: memory recall, cache hit, prior artifact retrieval, template lookup
- Allowed output: relevant fact set, candidate plan reuse, prior failure warnings
- Failure mode: stale memory or false analogies
- Guardrail: retrieval results must be marked as reused, not treated as fresh evidence

### Rung 1. Deterministic execution

- Role: shell commands, scripts, static transforms, tests, probes
- Allowed output: directly inspectable evidence
- Failure mode: environment mutation, partial state changes, script brittleness
- Guardrail: every rung-1 directive must emit `state_delta`

### Rung 2. Cheap bounded model

- Role: extraction, triage, classification, compression, first-pass decomposition
- Allowed output: candidate structure, never final high-stakes judgment
- Failure mode: false confidence, shallow verification, over-compression
- Guardrail: cannot close tasks and cannot verify semantically complex outputs alone

### Rung 3. Specialist worker

- Role: bounded expert execution
- Allowed output: artifact plus evidence
- Failure mode: local optimization that misses system-level consequences
- Guardrail: write scope must be explicit, and verifier standard must be declared up front

### Rung 4. Planner/synthesizer

- Role: re-plan, reconcile contradictions, reshape task graph, audit stalled work
- Allowed output: new directives, escalations, synthesis
- Failure mode: making strategic decisions on over-thinned context
- Guardrail: may request raw evidence slices when confidence or coherence drops

### Rung 5. Opus

- Role: architecture, arbitration, intention disambiguation, completion judgment, novel problem solving
- Allowed output: strategic directives, judgment, exceptions
- Failure mode: becoming the default laborer
- Guardrail: broker must log why Opus was needed and whether lower rungs should be improved

## Domain Verifier Registry

`v1` was too generic. `v2` requires domain-specific verification policies.

### Code verifier

Must include some combination of:

- targeted tests
- build or typecheck
- diff inspection
- runtime probe for changed behavior
- semantic review by a model at least comparable to the worker for complex changes

File existence is never enough.

### Research verifier

Must include:

- source-backed claims
- contradiction scan
- recency check where relevant
- confidence downgrade when sources disagree

### Operations verifier

Must include:

- command output
- service or endpoint probe
- rollback note when mutation occurred

### Memory verifier

Before writing to Hyphae, check:

- is it durable?
- is it surprising?
- is it non-duplicative?
- will retrieval of this fact improve future routing or judgment?

## Raw Context Escape Hatch

`v1` said Opus sees summaries by default. `v2` keeps that, but adds controlled raw-context access.

The broker must support:

- fetching full `ExecutionReport` evidence for a directive
- fetching selective tool traces for a stalled or contradictory branch
- fetching diff slices or source snippets when strategic judgment depends on detail

Rule:

- summaries are the normal mode
- raw context is available on demand
- raw context fetches are targeted and logged

This prevents strategic blindness without drowning Opus in exhaust.

## Anti-Churn Controls

`v1` lacked a strong answer to cheap-loop hell. `v2` adds explicit progress controls.

### Progress velocity

Each loop classifies progress as:

- `advancing`
- `stalled`
- `regressing`

The classification is based on:

- new evidence produced
- success criteria coverage
- contradiction count
- net movement in state checkpoint

### Escalation on stalled progress

The broker must escalate when:

- the same rung fails more than `N` times
- evidence repeats without increasing confidence
- the task graph has not advanced after `K` directives
- budget burn rate exceeds expected value for current rung

This ensures the system does not mistake cheap repetition for thrift.

## State Checkpointing

Resume must include the environment, not just the narrative.

Each major loop writes a `state_checkpoint` that can include:

- hash of key tracked files
- branch/commit identity
- dependency manifest hash
- selected environment variable fingerprint
- active service/process markers when relevant

If checkpoint drift is detected unexpectedly:

- the broker marks `state_drift`
- planner audit is triggered
- prior directives can be invalidated

## Dependency and Invalidation Model

`v1` under-specified inter-task dependency changes.

`v2` adds:

- every directive may declare upstream dependencies
- if a directive changes assumptions relied on by later directives, dependent directives become `stale`
- stale directives must be re-planned before execution

This prevents the system from executing yesterday’s valid plan against today’s modified state.

## Memory Policy v2

The memory rule is now:

Remember on surprise, durable decision, or repeated pattern.

Write to Hyphae when one of these is true:

- a previous assumption was wrong
- a stable project fact was discovered
- a strategy reliably worked
- a failure mode repeated or caused expensive churn
- a user preference materially affects future routing

Do not write when:

- the fact is temporary
- the information duplicates recent memory
- the plan was speculative and abandoned
- the event did not alter routing or understanding

## Broker State Machine v2

1. `IngestGoal`
- parse user goal
- extract explicit and inferred success criteria

2. `RecallContext`
- query Hyphae
- retrieve recent task graph and prior failure patterns

3. `BuildCheckpoint`
- snapshot key workspace and environment fingerprints

4. `ClassifyWork`
- select initial rung and verifier standard

5. `IssueDirective`
- emit bounded directive with retry limit, evidence standard, and escalation triggers

6. `Execute`
- run executor
- collect report and `state_delta`

7. `Verify`
- run domain verifier
- compare evidence against directive success test

8. `MeasureProgress`
- update progress velocity
- calculate budget burn rate
- detect repeated failure loops

9. `DetectDrift`
- compare new checkpoint to expected delta
- mark stale downstream directives if assumptions changed

10. `Route`
- continue, change rung, escalate, ask user, or abort

11. `SnapshotAndRemember`
- emit new `StatusPacket`
- write only surprising or durable facts to memory

12. `CompleteOrLoop`
- if criteria met, run completion judgment
- otherwise continue with next directive

## Required Metrics

These metrics tell you whether the architecture is actually working.

- fraction of tasks completed below Opus
- verifier rejection rate by rung and task type
- mean retries before escalation
- stalled-progress escalations per goal
- raw-context fetch frequency
- reopen rate after claimed completion
- memory write acceptance vs rejection
- drift incidents per 100 directives

## Remaining Open Problems

- How to calibrate confidence across different worker classes
- When a second independent worker is cheaper than escalating to Opus
- How to represent intention explicitly rather than inferring it from user text and success criteria
- How much autonomy to allow before requiring human confirmation on multi-step goals

## Summary

`v2` keeps the laddered architecture but removes the weakest assumptions.

The key improvements are:

- domain verifier registry
- raw-context escape hatch
- progress-velocity escalation
- state checkpointing
- dependency invalidation
- surprise-based memory writes

If this still fails in production, the likely reasons are:

- intention was mis-modeled at the top
- verifier standards were too weak for the domain
- checkpointing missed relevant state
- Opus was either escalated too late or used too often
