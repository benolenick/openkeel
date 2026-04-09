# Intention Packet Notes

## What This Is

`IntentionPacket` is a proposed runtime object for Calcifer/OpenKeel.

It is not a recalled existing project term from Hyphae, and it is not being presented as a standard industry framework. It was introduced here as a useful addition to the existing runtime protocol that already includes:

- `Directive`
- `ExecutionReport`
- `StatusPacket`
- `EscalationDecision`
- `CompletionDecision`

The purpose of `IntentionPacket` is to represent the invariant meaning of the user's request across changing plans, subagents, tools, and time.

Short version:

- `IntentionPacket`: what must be preserved
- `StatusPacket`: what is currently true
- `Directive`: what to do next

If `Opus` is the conductor, `IntentionPacket` is the score.

## Why It Exists

Most agent systems fail not because they cannot plan, but because they optimize the wrong thing.

Typical failure pattern:

- user asks for outcome A
- planner converts it into task B
- subagents optimize local completion of B
- system technically finishes B
- user feels the real goal was missed or betrayed

This happens because most runtimes have:

- prompt
- planner
- memory
- tools

but no explicit object for:

- what the user really meant
- which constraints are sacred
- which tradeoffs are allowed
- what kind of failure would still count as betrayal even if the task was technically completed

`IntentionPacket` exists to make that explicit.

## Core Concept

The system needs three different layers:

### 1. Intent Layer

Stable, high-level meaning.

Object:

- `IntentionPacket`

Questions answered:

- What is the user actually trying to achieve?
- Why does it matter?
- What must not be sacrificed?
- What kind of result would feel wrong even if technically valid?

### 2. State Layer

Current reality of the task.

Object:

- `StatusPacket`

Questions answered:

- What is known now?
- What just changed?
- What evidence exists?
- What are the open uncertainties?

### 3. Action Layer

Immediate operational moves.

Objects:

- `Directive`
- `ExecutionReport`

Questions answered:

- What should be done next?
- What was attempted?
- What evidence supports it?

This gives the control stack:

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

Stable identifier used to bind intention, status, directives, and completion decisions.

### `user_request`

Literal text or normalized user request.

This preserves the original surface form so later reasoning can compare:

- what the user said
- what the system inferred

### `intended_outcome`

Best concise answer to:

"What change in the world is the user actually trying to cause?"

This should be outcome-oriented, not method-oriented.

Bad:

- "Write a script"

Better:

- "Automate the recurring workflow safely so the user no longer has to do it manually"

### `why_it_matters`

Captures the causal or motivational substrate beneath the request.

This matters because two plans that both satisfy the surface task may diverge heavily once the underlying reason is known.

### `must_preserve`

Non-negotiable invariants.

These are the strongest fields in the packet. If a directive conflicts with these, the broker should escalate rather than proceed.

Examples:

- data integrity
- sender reputation
- legal compliance
- user privacy
- reversibility
- readability of the final code

### `allowed_tradeoffs`

Things the user would plausibly accept sacrificing in order to reach the intended outcome.

Examples:

- slower rollout
- narrower first version
- more manual review
- higher latency

### `forbidden_tradeoffs`

The opposite of allowed tradeoffs.

These are shortcuts the system must not take even if they would optimize cost, speed, or local success rate.

Examples:

- spamming people to get reply volume
- pushing unsafe code just to ship faster
- using destructive commands without explicit approval

### `success_shape`

Qualitative completion pattern.

This is not just "tests pass." It describes what a satisfying result looks like.

Examples:

- can be resumed cleanly
- has audit trail
- minimizes human babysitting
- is understandable by future agents

### `failure_shape`

Qualitative betrayal pattern.

This is one of the most important fields because it identifies failure modes that can hide behind technical completion.

Examples:

- "works but burns reputation"
- "automates the task but creates opaque fragility"
- "solves today's issue while making future maintenance worse"

### `preference_signals`

Execution style preferences inferred or stated by the user.

These are not as strong as `must_preserve`, but they steer routing.

Examples:

- high autonomy
- quality-first
- low cost sensitivity
- minimal communication

### `ambiguities`

Open semantic gaps or unresolved interpretation risks.

If ambiguities remain high, Opus should not over-commit to a narrow plan.

### `assumptions_in_force`

Inferences the runtime is currently using because the user did not specify them.

This is important for later revision.

### `revision_policy`

Control over who can change intention and under what conditions.

Recommended default:

- only `Opus` may materially revise intention
- workers can only flag misalignment

### `confidence`

How strongly the system believes this packet matches the user's real intention.

Low confidence should make the system more conservative and escalation-prone.

## Relationship To Existing Runtime Objects

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

It should be generated under the authority of intention.

A directive is valid only if it:

- serves the intended outcome
- does not violate `must_preserve`
- does not trigger `failure_shape`

### `IntentionPacket` vs `CompletionDecision`

Completion should not be based only on task completion.

It should ask:

- was the intended outcome achieved?
- were the invariants preserved?
- did the process avoid the failure shapes?

## Runtime Placement

The clean runtime sequence is:

1. User request arrives
2. Broker drafts `IntentionPacket`
3. Opus validates or revises it
4. Broker recalls context and builds `StatusPacket`
5. Opus emits a `Directive`
6. Worker executes and returns `ExecutionReport`
7. Verifier checks both:
   - directive compliance
   - intention alignment
8. Broker updates state
9. Opus decides next move or completion

In short:

`User -> Broker Drafts Intent -> Opus Confirms Intent -> Broker Routes State -> Workers Execute -> Verifiers Check -> Opus Judges`

## Why It Fits The "Opus As Conductor" Model

Yes, this fits the existing design cleanly.

Existing framing from project memory:

- Opus is foreman / principal engineer / conductor
- lower rungs are apprentices, technicians, and clerks

That pattern already implies there must be something stable that Opus is conducting toward.

Without `IntentionPacket`, Opus conducts:

- tasks
- plans
- summaries

With `IntentionPacket`, Opus conducts:

- fidelity to the user’s actual aim

This is a more correct conductor model.

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

## Broker Integration Pattern

The broker should use `IntentionPacket` for:

### 1. Routing

Example:

- if `must_preserve` includes safety, reversibility, or reputation, the broker should prefer slower but more verifiable plans

### 2. Escalation

Escalate if:

- a proposed action conflicts with `must_preserve`
- a shortcut would trigger `failure_shape`
- ambiguities make the intended outcome unclear

### 3. Verification

Verifiers should test not just "did the worker satisfy the directive?" but also:

- did the worker create intention risk?
- did the worker optimize a local metric that violates the intended outcome?

### 4. Completion

Completion requires:

- success criteria met
- intention preserved
- no forbidden tradeoff taken
- no failure shape triggered

## Directive Extension

Each directive should carry a reduced intention slice.

Possible extension:

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

This avoids every worker needing the full packet every time while still preserving alignment.

## Execution Report Extension

Workers should report intention risk explicitly.

Possible extension:

```json
{
  "directive_id": "string",
  "status": "succeeded|failed|blocked|partial",
  "evidence": ["string"],
  "intention_risks": [
    "This implementation improves speed but reduces transparency",
    "This result meets the narrow task but may violate reversibility"
  ],
  "alignment_confidence": 0.0
}
```

This is useful because a worker often notices local tension before Opus sees it.

## Completion Decision Extension

Completion should include intention alignment explicitly.

Possible extension:

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

## Example 1: Autonomous Outreach

User request:

- "Set up an autonomous outreach system for leads."

Naive system interpretation:

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

Why this matters:

Without intention modeling, the system may choose the fastest apparent route:

- aggressive sending
- poor gating
- minimal logs

With intention modeling, the routing changes:

- staged rollout
- stronger observability
- throttling
- pause/resume controls
- stricter verifier checks

## Example 2: Coding Task

User request:

- "Fix the bug quickly."

Naive interpretation:

- patch the failing path as fast as possible

Possible better intention:

- restore working behavior without making the codebase more fragile

Potential `must_preserve`:

- no data corruption
- readability
- testable behavior

Potential `allowed_tradeoffs`:

- partial fix now, fuller refactor later

Potential `forbidden_tradeoffs`:

- silent regression elsewhere
- opaque hack that no future agent understands

This changes behavior significantly:

- broker may still route quickly
- but verifier requires tests or semantic review
- completion cannot be declared solely because one symptom disappeared

## What It Prevents

`IntentionPacket` is intended to reduce:

- solving the wrong problem well
- literal compliance with contextual betrayal
- local worker optimization that damages global outcome
- over-optimizing for cost/speed when care/reliability matters more
- completion claims that ignore qualitative failure

## Failure Modes Of This Concept

This object can also go wrong. Main risks:

### 1. Over-interpretation

The system may infer too much and "understand" intentions the user never had.

Mitigation:

- track `confidence`
- surface `ambiguities`
- keep `user_request` visible
- let Opus revise conservatively

### 2. Frozen bad intention

If the packet is wrong and treated as sacred, the whole runtime becomes rigid around a mistaken interpretation.

Mitigation:

- explicit revision policy
- escalate on repeated tension between results and intention

### 3. Intention bloat

If every nuance is captured, the packet becomes too large and turns into another prompt blob.

Mitigation:

- keep it short
- focus on invariants, tradeoffs, and betrayal patterns

### 4. Workers ignoring it

If intention is stored but not operationalized, it becomes decorative.

Mitigation:

- inject `intention_checks` into directives
- require `intention_risks` in reports
- include intention in completion judgment

## Suggested Operational Rules

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

This is important:

Many systems re-plan endlessly when the real problem is that the intent model is wrong.

## Practical Minimal Version

If you want to keep this very lean, the first production version could be only:

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

## Best One-Sentence Definition

`IntentionPacket` is the structured representation of what the system must not betray while trying to help the user.

## Best System-Level Definition

The runtime should treat `IntentionPacket` as the normative layer above planning: plans may change, tools may change, workers may change, and status may change, but the packet defines the invariant user-aligned objective and constraints that all lower layers must respect.

## Recommendation For The Other Agent

If another agent is integrating this with your current work, the most important questions are:

1. Should `IntentionPacket` be a first-class object in the runtime, or initially just metadata attached to `StatusPacket`?
2. Should only Opus be allowed to revise it, or should the broker have limited revision rights?
3. Which directive fields should carry distilled intention constraints?
4. Which verifier rules should explicitly test for intention violation?
5. At what point should a task be escalated because the current plan satisfies the literal task but not the intended outcome?

## Bottom Line

This concept fits the current Calcifer/OpenKeel architecture well.

It does not replace:

- `Directive`
- `StatusPacket`
- `ExecutionReport`

It supplies the missing top layer that makes those objects answerable to the user's actual aim rather than only to local task completion.
