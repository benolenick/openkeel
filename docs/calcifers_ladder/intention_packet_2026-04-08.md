# Intention Packet

## Status

Proposed runtime object for Calcifer/OpenKeel.

This term was introduced in architecture discussion as an extension to the existing ladder protocol. It is not being claimed here as an established industry term, and it was not recovered from prior Hyphae project memory.

It is meant to complement the current runtime objects:

- `Directive`
- `ExecutionReport`
- `StatusPacket`
- `EscalationDecision`
- `CompletionDecision`

## One-Sentence Definition

`IntentionPacket` is the structured representation of what the system must not betray while trying to help the user.

## Short Mental Model

- `IntentionPacket`: what must be preserved
- `StatusPacket`: what is true now
- `Directive`: what to do next

If `Opus` is the conductor, `IntentionPacket` is the score.

## Why This Exists

Most agent systems do not fail because they cannot generate plans. They fail because they optimize the wrong thing.

Common failure pattern:

1. The user wants outcome A.
2. The planner converts that into task B.
3. Subagents optimize local completion of B.
4. The system technically completes B.
5. The user feels the real goal was missed or betrayed.

This happens when the runtime has prompts, plans, memory, and tools but no explicit object for:

- what the user actually meant
- which constraints are sacred
- which tradeoffs are allowed
- which shortcuts are forbidden
- what kind of "successful" result would still feel like failure

## Core Role In The Stack

The runtime needs three layers:

### Intent Layer

Stable, normative, high-level meaning.

Object:

- `IntentionPacket`

Questions answered:

- What is the user actually trying to achieve?
- Why does it matter?
- What must not be sacrificed?
- What failure would feel like betrayal even if the task was technically completed?

### State Layer

Dynamic, descriptive task state.

Object:

- `StatusPacket`

Questions answered:

- What is currently true?
- What evidence exists?
- What changed?
- What uncertainties remain?

### Action Layer

Immediate operational moves.

Objects:

- `Directive`
- `ExecutionReport`

Questions answered:

- What should be done next?
- What was attempted?
- What evidence supports the result?

Control stack:

`Intent -> State -> Action -> Evidence -> Judgment`

## Formal Definition

An `IntentionPacket` is a structured representation of the user’s intended outcome and invariant constraints that should survive replanning, delegation, interruption, and tool changes.

It is not the same as:

- a prompt
- a task description
- a plan
- a checklist
- a status summary

It is the object that tells the runtime what must remain true while everything else adapts.

## Intention Packet v1 Schema

```json
{
  "goal_id": "string",
  "user_request": "string",
  "intended_outcome": "string",
  "why_it_matters": "string",
  "must_preserve": ["string"],
  "allowed_tradeoffs": ["string"],
  "forbidden_tradeoffs": ["string"],
  "success_shape": ["string"],
  "failure_shape": ["string"],
  "preference_signals": {
    "autonomy_level": "low|medium|high",
    "speed_vs_quality": "speed|balanced|quality",
    "cost_sensitivity": "low|medium|high",
    "communication_style": "minimal|normal|rich"
  },
  "ambiguities": ["string"],
  "assumptions_in_force": ["string"],
  "revision_policy": {
    "who_may_update": "opus_only|broker_with_escalation",
    "must_escalate_on_conflict": true
  },
  "confidence": 0.0
}
```

## Field Semantics

### `goal_id`

Stable identifier linking intention, status, directives, and completion.

### `user_request`

Literal or normalized request text. This keeps the original surface form visible so the system can compare what the user said against what the runtime inferred.

### `intended_outcome`

Best concise answer to:

"What change in the world is the user actually trying to cause?"

This should be outcome-oriented, not method-oriented.

Bad:

- "Write a script"

Better:

- "Automate the recurring workflow safely so the user no longer has to do it manually"

### `why_it_matters`

The underlying reason for the request. This helps the system choose between superficially valid plans.

### `must_preserve`

Non-negotiable invariants. If a directive conflicts with these, the broker should escalate rather than proceed.

Examples:

- data integrity
- sender reputation
- legal compliance
- privacy
- reversibility
- readability of final code

### `allowed_tradeoffs`

Sacrifices the user would likely accept.

Examples:

- slower rollout
- narrower first version
- more manual review
- higher latency

### `forbidden_tradeoffs`

Shortcuts the system must not take even if they appear efficient.

Examples:

- spammy behavior
- unsafe deployment
- opaque hacks
- destructive commands without approval

### `success_shape`

Qualitative pattern of a satisfying result.

Examples:

- resumable
- observable
- controllable
- understandable by future agents

### `failure_shape`

Qualitative betrayal pattern. This is one of the most important fields.

Examples:

- "works but burns reputation"
- "automates the task but creates opaque fragility"
- "solves today while making maintenance worse"

### `preference_signals`

Execution-style preferences, inferred or explicit.

Examples:

- high autonomy
- quality-first
- low cost sensitivity
- minimal communication

### `ambiguities`

Unresolved interpretation risks. High ambiguity should increase escalation and reduce overconfident planning.

### `assumptions_in_force`

Current inferred assumptions used by the runtime because the user did not specify them directly.

### `revision_policy`

Defines who can revise intention and when.

Recommended default:

- broker drafts
- `Opus` confirms or revises
- workers may flag conflicts
- workers may not silently reinterpret

### `confidence`

How strongly the system believes the packet matches the user's real intention.

Low confidence should make the runtime more conservative.

## Relationship To Other Runtime Objects

### `IntentionPacket` vs `StatusPacket`

`IntentionPacket` is stable and normative.

It says:

- what should be protected
- what outcome matters
- what kind of betrayal is unacceptable

`StatusPacket` is dynamic and descriptive.

It says:

- what happened
- what changed
- what evidence exists
- what might happen next

### `IntentionPacket` vs `Directive`

`Directive` is local and bounded.

A directive is valid only if it:

- serves the intended outcome
- does not violate `must_preserve`
- does not exploit a forbidden tradeoff
- does not trigger a known `failure_shape`

### `IntentionPacket` vs `CompletionDecision`

Completion should not ask only whether the task was completed.

It should ask:

- was the intended outcome achieved?
- were invariants preserved?
- were failure shapes avoided?

## Runtime Placement

Recommended sequence:

1. User request arrives.
2. Broker drafts `IntentionPacket`.
3. `Opus` validates or revises it.
4. Broker recalls context and builds `StatusPacket`.
5. `Opus` emits `Directive`.
6. Worker executes and returns `ExecutionReport`.
7. Verifier checks both:
   - directive compliance
   - intention alignment
8. Broker updates state.
9. `Opus` decides next move or completion.

In short:

`User -> Broker Drafts Intent -> Opus Confirms Intent -> Broker Routes State -> Workers Execute -> Verifiers Check -> Opus Judges`

## Fit With "Opus As Conductor"

This fits the existing Calcifer Ladder framing cleanly.

Project memory already frames:

- `Opus` as foreman / principal engineer / conductor
- lower rungs as apprentices, technicians, and clerks

That model implies there should be something stable that `Opus` is conducting toward.

Without `IntentionPacket`, `Opus` conducts:

- tasks
- plans
- summaries

With `IntentionPacket`, `Opus` conducts:

- fidelity to the user's actual aim

Conductor analogy:

- `IntentionPacket` = score
- `StatusPacket` = what the orchestra currently sounds like
- `Directive` = what section should do next
- `ExecutionReport` = what was played
- `CompletionDecision` = whether the performance actually satisfied the piece

## Governance Rule

Recommended policy:

- only `Opus` may author or materially revise `IntentionPacket`
- broker may draft it initially
- workers may read it
- workers may flag tension or conflict
- workers may not silently reinterpret it

This preserves conductor authority and prevents local agents from mutating the mission.

## Broker Integration

The broker should use `IntentionPacket` for four things:

### 1. Routing

If `must_preserve` includes safety, reversibility, or reputation, the broker should prefer slower but more verifiable paths.

### 2. Escalation

Escalate if:

- a proposed action conflicts with `must_preserve`
- a shortcut would exploit a forbidden tradeoff
- a likely result matches `failure_shape`
- ambiguity is high enough that the intended outcome is unclear

### 3. Verification

Verifiers should test not only:

- did the worker satisfy the directive?

but also:

- did the worker create intention risk?
- did the worker optimize a local metric at the expense of the real outcome?

### 4. Completion

Completion requires:

- success criteria met
- intention preserved
- no forbidden tradeoff taken
- no failure shape triggered

## Directive Extension

Each directive should carry a reduced intention slice:

```json
{
  "directive_id": "string",
  "task": "string",
  "success_test": ["string"],
  "intention_checks": [
    "Does not violate must_preserve constraints",
    "Does not exploit a forbidden tradeoff",
    "Escalate if likely to trigger any failure_shape"
  ]
}
```

This keeps alignment operational without forcing every worker to absorb the full packet on every turn.

## Execution Report Extension

Workers should report intention risk explicitly:

```json
{
  "directive_id": "string",
  "status": "succeeded|failed|blocked|partial",
  "evidence": ["string"],
  "intention_risks": [
    "This implementation improves speed but reduces transparency",
    "This result meets the narrow task but may reduce reversibility"
  ],
  "alignment_confidence": 0.0
}
```

This matters because workers often see local tension before `Opus` does.

## Completion Decision Extension

Completion should include intention alignment:

```json
{
  "goal_id": "string",
  "status": "complete|incomplete|blocked",
  "matched_success_criteria": ["string"],
  "intention_alignment": {
    "preserved_must_preserve": ["string"],
    "avoided_failure_shape": ["string"],
    "tensions_remaining": ["string"]
  }
}
```

## Example: Autonomous Outreach

User request:

- "Set up an autonomous outreach system for leads."

Naive interpretation:

- send messages automatically at scale

Better `IntentionPacket`:

```json
{
  "goal_id": "lead_outreach_001",
  "user_request": "Set up an autonomous outreach system for leads.",
  "intended_outcome": "Create a scalable lead generation loop that produces real conversations without harming reputation or losing operator control.",
  "why_it_matters": "The user wants pipeline growth that remains sustainable and reviewable.",
  "must_preserve": [
    "sender reputation",
    "legal compliance",
    "auditability",
    "operator pause/resume control"
  ],
  "allowed_tradeoffs": [
    "slower rollout",
    "smaller initial batch",
    "more review overhead"
  ],
  "forbidden_tradeoffs": [
    "spammy behavior",
    "opaque automation",
    "unbounded sending"
  ],
  "success_shape": [
    "controlled sending",
    "clear audit trail",
    "measurable reply pipeline",
    "human override"
  ],
  "failure_shape": [
    "technically automated but damages deliverability",
    "high volume with low trust",
    "system cannot be safely paused"
  ],
  "preference_signals": {
    "autonomy_level": "high",
    "speed_vs_quality": "quality",
    "cost_sensitivity": "medium",
    "communication_style": "normal"
  },
  "ambiguities": [
    "target channels not yet specified",
    "compliance jurisdiction not yet specified"
  ],
  "assumptions_in_force": [
    "email-first workflow",
    "staged rollout is acceptable"
  ],
  "revision_policy": {
    "who_may_update": "opus_only",
    "must_escalate_on_conflict": true
  },
  "confidence": 0.72
}
```

Effect on routing:

- staged rollout instead of blast volume
- stronger observability
- throttling and pause/resume
- stricter verifier standards

## Example: Coding Task

User request:

- "Fix the bug quickly."

Naive interpretation:

- patch the symptom as fast as possible

Possible better intention:

- restore correct behavior without making the codebase more fragile

Potential `must_preserve`:

- no data corruption
- readability
- testable behavior

Potential `allowed_tradeoffs`:

- partial fix now, fuller cleanup later

Potential `forbidden_tradeoffs`:

- silent regressions elsewhere
- opaque hacks that future agents cannot reason about

## What This Prevents

`IntentionPacket` is meant to reduce:

- solving the wrong problem well
- literal compliance with contextual betrayal
- local worker optimization that damages the global outcome
- over-optimizing for cost or speed when care and reliability matter more
- completion claims that ignore qualitative failure

## Failure Modes Of The Concept

### 1. Over-interpretation

The system infers too much and "understands" intentions the user never had.

Mitigations:

- keep `user_request` visible
- track `confidence`
- surface `ambiguities`
- revise conservatively

### 2. Frozen Bad Intention

If the packet is wrong and treated as sacred, the runtime becomes rigid around a mistaken interpretation.

Mitigations:

- explicit revision policy
- escalate when repeated execution tension appears

### 3. Intention Bloat

If everything is captured, the packet becomes another prompt blob.

Mitigation:

- keep it short
- focus on invariants, tradeoffs, and betrayal patterns

### 4. Decorative Intention

The packet exists but is not used operationally.

Mitigations:

- inject `intention_checks` into directives
- require `intention_risks` in reports
- include intention in completion judgment

## Suggested Operating Rules

### Rule 1

Do not create or revise `IntentionPacket` casually. It is a mission object, not a log line.

### Rule 2

If a directive conflicts with `must_preserve`, escalate.

### Rule 3

If a shortcut would trigger `failure_shape`, escalate.

### Rule 4

If worker output technically satisfies the directive but raises intention risk, do not auto-complete.

### Rule 5

If repeated execution tension appears, revisit intention before revisiting plan.

This matters because many systems re-plan endlessly when the real problem is that the intent model was wrong.

## Minimal Production Version

If this needs to start lean, the first production version could be:

```json
{
  "goal_id": "string",
  "user_request": "string",
  "intended_outcome": "string",
  "must_preserve": ["string"],
  "forbidden_tradeoffs": ["string"],
  "success_shape": ["string"],
  "failure_shape": ["string"],
  "ambiguities": ["string"],
  "confidence": 0.0
}
```

That is enough to begin routing and verification against intention without overbuilding.

## Best System-Level Definition

The runtime should treat `IntentionPacket` as the normative layer above planning: plans may change, tools may change, workers may change, and status may change, but the packet defines the invariant user-aligned objective and constraints that all lower layers must respect.

## Merge Questions For Other Agents

If this is being integrated with another architecture pass, the important questions are:

1. Should `IntentionPacket` be first-class in the runtime, or initially metadata on `StatusPacket`?
2. Should only `Opus` be allowed to revise it, or should the broker have limited revision rights?
3. Which `Directive` fields should carry distilled intention constraints?
4. Which verifier rules should explicitly test intention violation?
5. When should a task be escalated because the plan satisfies the literal request but not the intended outcome?

## Bottom Line

This concept fits the current Calcifer/OpenKeel architecture well.

It does not replace the existing runtime objects.

It supplies the missing top layer that makes those objects answerable to the user's actual aim rather than only to local task completion.
