# Calcifer Routing Matrix

**Date:** 2026-04-08
**Status:** working design reference
**Purpose:** define how Calcifer should route work across local models, Haiku, Sonnet, Opus, and full Claude-style agent execution
**Related:**
- `calcifer_ladder_design_v2_2026-04-08.md`
- `calcifers_ladder/conversation_shapes_and_escalation_2026-04-08.md`
- `calcifers_ladder/implementation_blueprint_2026-04-08.md`

---

## One-sentence rule

**Use the cheapest intelligence that can make trustworthy progress, and reserve higher cloud intelligence for judgment, decomposition, and ambiguous work.**

---

## What This Matrix Optimizes For

Calcifer should balance:

- cost
- latency
- reliability
- judgment quality
- tool-loop strength
- consequence of error

This means:

- local models should do most cheap labor
- Haiku should handle lightweight cloud reasoning
- Sonnet should handle strong routine execution
- Opus should handle hard decisions and strategic judgment
- Claude-style full agent execution should be reserved for tasks that truly need sustained tool use

---

## The Available Intelligence Layers

### 1. Deterministic Tools

Examples:
- shell commands
- file reads
- grep/search
- tests
- lint/typecheck
- diff inspection

Best for:
- direct execution
- verification
- evidence gathering

Do not use a model if a deterministic tool can answer the question directly.

### 2. Local LLMs

Examples:
- local small/medium coding or reasoning models

Best for:
- cheap summarization
- local classification
- bounded repo inspection
- narrow patch drafting
- repetitive operator work

Local models should be treated as cheap workers, not final authorities.

### 3. Haiku

Best for:
- lightweight cloud reasoning
- semantic transforms
- basic troubleshooting guidance
- low-stakes structured extraction
- fallback when local quality is too weak but the task still does not justify Sonnet

Haiku is the cloud semantic worker, not the architect.

### 4. Sonnet

Best for:
- strong routine execution
- standard coding/debugging
- bounded multi-step tool work
- robust synthesis after evidence gathering
- medium-stakes judgment when deterministic checks are strong

Sonnet is the default strong executor.

### 5. Opus

Best for:
- difficult classification
- strategic planning
- ambiguous escalation decisions
- arbitration between competing plans
- final judgment when deterministic checks are insufficient
- architecture and design choices

Opus should be the decider, not the default typist.

### 6. Claude-Style Full Agent Loop

This is not just a model tier. It is an execution pattern:

- model -> tool -> model -> tool -> result

It may run on Sonnet or Opus depending on the task, but the important distinction is that it is a sustained inner agent loop.

Use it when the task needs:

- iterative inspection
- tool use across many steps
- durable bounded session state
- multi-file or multi-command execution

---

## Conversation Shape To Default Route

| Conversation shape | Default route | Escalate to | Notes |
|---|---|---|---|
| Instant answer | Deterministic tools, local LLM, or Haiku | Sonnet only if evidence or synthesis becomes nontrivial | Most turns should stay here |
| Guided troubleshooting | Haiku or local model plus deterministic checks | Sonnet if ambiguity persists; Opus only if the diagnosis tree breaks down | Usually interactive and bounded |
| Bounded work task | Local bounded loop or Sonnet | Claude-style full agent loop if iteration depth grows; Opus if judgment is hard | Main operational mode |
| Strategic engineering task | Sonnet plus Opus planning/judgment | Full agent loop and Opus judge/governor | Persistent task context expected |

---

## Model Role Matrix

| Role | Deterministic tools | Local LLM | Haiku | Sonnet | Opus | Claude-style full agent |
|---|---|---|---|---|---|---|
| Direct execution | Primary | Rare | Rare | Rare | No | No |
| Cheap classification | No | Primary | Secondary | Rare | Rare | No |
| Semantic QA | No | Primary | Primary | Secondary | Rare | No |
| Guided troubleshooting | Secondary | Primary | Primary | Secondary | Rare | Rare |
| Repo scouting | Secondary | Primary | Secondary | Secondary | Rare | Rare |
| Patch drafting | No | Primary | Rare | Primary | Rare | Secondary |
| Routine coding task | Secondary | Secondary | Rare | Primary | Rare | Secondary |
| Multi-step debugging | Secondary | Secondary | Rare | Primary | Secondary | Primary |
| Strategic planning | No | Rare | Rare | Secondary | Primary | Secondary |
| Escalation decision | No | Rare | Rare | Secondary | Primary | No |
| Ambiguous judgment | No | Rare | Rare | Secondary | Primary | No |
| Final verification | Primary | Rare | Rare | Secondary | Secondary | No |
| Long-horizon execution | Secondary | Rare | Rare | Secondary | Secondary | Primary |

Interpretation:

- `Primary` means preferred default for that role
- `Secondary` means acceptable fallback or support role
- `Rare` means only use if constraints force it
- `No` means do not use for that role

---

## The Real Escalation Signals

Do not route based mainly on keywords.

Calcifer should score each step on these dimensions:

### 1. Structural Complexity

How much hidden structure exists?

Low:
- answer one question
- run one command

High:
- design a subsystem
- compare alternative architectures
- debug a multi-component failure

### 2. Operational Depth

How many dependent steps are likely?

Low:
- one read, one answer

High:
- inspect, test, patch, retest, compare, revise

### 3. Evidence Need

How much proof is needed before answering?

Low:
- casual explanation

High:
- claims about code, systems, failures, architecture, policy

### 4. Verifiability Risk

How hard is it to verify the answer deterministically?

Low:
- shell output
- test pass/fail

High:
- architecture quality
- adequacy of a plan
- ambiguous troubleshooting diagnosis

### 5. Consequence Of Error

How bad is a wrong answer?

Low:
- convenience question

High:
- code changes
- system operations
- major design choices

### 6. Loop Difficulty

How likely is it that the task needs an iterative tool loop?

Low:
- direct answer

High:
- debugging
- coding
- investigation

---

## Suggested Escalation Bands

This can be implemented as a weighted score or a policy table. The important thing is the banding.

### Band A — Minimal

Characteristics:
- low complexity
- low consequence
- low evidence need

Route:
- deterministic tools first
- local model or Haiku if semantic help is needed

Examples:
- "what does this command do?"
- "show disk usage"

### Band B — Lightweight

Characteristics:
- small ambiguity
- still mostly one-turn
- low to medium evidence need

Route:
- local model or Haiku
- deterministic checks where possible

Examples:
- "explain this stack trace"
- "why might this config fail?"

### Band C — Operational

Characteristics:
- multi-step but bounded
- evidence matters
- some iteration expected

Route:
- local bounded loop first if cheap and reliable enough
- Sonnet for stronger bounded execution

Examples:
- "fix this failing test"
- "inspect these logs and tell me what broke"

### Band D — High-judgment

Characteristics:
- ambiguity remains after evidence gathering
- multiple valid approaches exist
- quality of judgment changes outcomes

Route:
- Sonnet for evidence gathering and synthesis
- Opus for planning, arbitration, escalation, or final judgment

Examples:
- "which refactor path should we take?"
- "is this design actually sound?"

### Band E — Strategic / Long-horizon

Characteristics:
- architecture or long-running task
- decomposition required
- execution and judgment both matter

Route:
- Opus for planning/governor decisions
- Sonnet or Claude-style full agent loop for execution
- deterministic verifiers wherever available

Examples:
- "design Calcifer's Ladder"
- "plan and execute a careful subsystem migration"

---

## Default Routing Policy

### Deterministic First

If a direct command or typed tool can answer the request, use that first.

Examples:
- `git status`
- `rg`
- file read
- tests

### Local Before Cheap Cloud

If the work is semantic but cheap, try a local model first when:

- latency is good
- quality is acceptable
- stakes are low to medium

Move to Haiku when:

- local reliability is weak
- formatting/consistency matters more
- the result is user-facing enough that local drift is annoying

### Haiku Before Sonnet For Lightweight Reasoning

Use Haiku for:

- small semantic classification
- low-stakes Q&A
- lightweight troubleshooting guidance
- simple extraction and summarization

Do not use Haiku for:

- important coding
- complicated debugging
- architecture judgment

### Sonnet As The Strong Default Worker

Use Sonnet when the task needs:

- robust coding help
- deeper synthesis
- bounded multi-step reasoning
- reliable tool use

Sonnet should be the default strong operator before escalating to Opus.

### Opus As Governor

Use Opus when the task needs:

- plan selection
- decomposition
- arbitration
- escalation decisions
- ambiguous acceptance judgment
- architecture/design reasoning

Do not spend Opus on:

- basic summaries
- obvious fixes
- repetitive repo reads
- direct shell work

### Full Claude-Style Agent Loop Only When Iteration Is The Point

Use a full inner agent loop when:

- the task clearly needs multiple tool actions
- session state must persist within the step
- retries and intermediate reasoning are cheaper than repeated restarts

This loop should usually run on Sonnet first.

Escalate the full agent loop to Opus only when:

- planning and execution are tightly coupled
- repeated Sonnet attempts fail
- the execution itself contains difficult strategic judgment

---

## Practical Examples

### Example 1: "Show me the biggest files in this repo"

Route:
- deterministic tools

Why:
- zero need for model judgment

### Example 2: "What does this function do?"

Route:
- local LLM first
- Haiku fallback if local answer quality is weak

Why:
- simple semantic explanation

### Example 3: "Why isn't my printer printing?"

Route:
- Haiku or local semantic troubleshooter
- deterministic checks as the conversation narrows
- Sonnet only if the diagnosis remains ambiguous after a few rounds

Why:
- interactive troubleshooting, but not automatically high intelligence

### Example 4: "Fix this failing test"

Route:
- deterministic evidence gathering
- local bounded loop or Sonnet
- full Claude-style loop if multiple inspection/patch/test iterations are clearly needed
- Opus only if root cause or repair strategy becomes ambiguous

Why:
- execution-heavy, not judgment-heavy at first

### Example 5: "Design an adaptive intelligence ladder"

Route:
- Opus for framing, decomposition, and judgment
- Sonnet or local workers for drafting and bounded follow-up work

Why:
- strategic planning is the valuable part

### Example 6: "Should we keep task sessions alive after quick questions?"

Route:
- Opus governor or Sonnet if the stakes are lower and prior patterns are already clear

Why:
- this is a design decision, not routine execution

---

## Governor Triggers

These are the moments when Calcifer should consider consulting Opus.

- classification confidence is low
- the task spans multiple valid plans
- deterministic verification is impossible or inconclusive
- the current runner failed twice
- the scope is expanding
- the task has moved from execution to design
- the task has medium or high consequence of error
- acceptance depends on qualitative judgment
- context compaction risks losing critical reasoning

If none of these are true, do not call Opus.

---

## Anti-Patterns

Do not do these:

- send every user turn to the strongest model
- use Opus for routine summaries
- use Haiku for architecture
- use local models as final judges on high-stakes tasks
- turn every multi-step task into a full agent loop immediately
- treat all runners as interchangeable

---

## Operational Summary

The intended behavior is:

1. Prefer deterministic tools when possible.
2. Prefer local models for cheap labor.
3. Use Haiku for lightweight cloud semantics.
4. Use Sonnet as the default strong worker.
5. Use Opus for hard decisions, planning, and ambiguous judgment.
6. Use full Claude-style agent loops only when sustained iterative execution is actually needed.

This is the intended balance:

- **local = labor**
- **Haiku = lightweight cloud reasoning**
- **Sonnet = strong operator**
- **Opus = governor**
- **Claude-style full agent = sustained execution pattern**

