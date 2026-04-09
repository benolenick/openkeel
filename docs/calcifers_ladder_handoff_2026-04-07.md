# Calcifer's Ladder Handoff

Date: 2026-04-07 (updated 2026-04-08 after critic/fractal pass)

## 2026-04-08 Status Update

A critic pass on `/home/om/lllm/lllm/os/token_bridge.py` found that Steps 1
and 2 were already built (directive execution loop + LadderSession persistence)
and tests `test_ladder_e2e.py` + `test_ladder_invariants.py` existed — the
rest of this document lagged reality. The sections below about "not finished"
are historical; the fractal pass on 2026-04-08 also landed these fixes in
`token_bridge.py`:

- **Success verification** (`_verify_success`): supervisor now scores evidence
  against `directive.success_check` every iteration. The loop no longer
  declares COMPLETE on a weak "all subtasks clean" heuristic.
- **Iteration cap fixed**: the runaway guard now returns BLOCKED, never
  silently COMPLETE on the final iteration.
- **Convergence guard**: sha1 fingerprint of (subtasks + outputs); two
  identical consecutive iterations → BLOCKED.
- **Incremental StatusPacket**: `_build_status_packet` now takes
  `prior_packet` + only new reports, so the upward channel stays compact
  instead of accumulating full history. First-call seeds evidence from
  Hyphae (`HYPHAE_URL`/recall).
- **Tool belt expanded**: decompose/execute now support `Grep`, `Glob`, and
  `Edit` in addition to Bash/Read/Write/Worker. `Edit` is a unique-string
  replace (1-match rule). Ripgrep used if present.
- **`allowed_tools` enforced**: `_execute_subtask` refuses any tool not on
  `directive.allowed_tools`. Opus controls what the workers may do.
- **Write/Edit path allowlist**: gated by `LADDER_WRITE_ROOTS` (defaults to
  `~/openkeel`, `~/lllm`, `~/tools`, `/tmp`; override via env). No more
  "supervisor hallucinates a Write to `~/.bashrc`" foot-gun.
- **Bash timeout**: default bumped from 10s to 60s (`SUBTASK_BASH_TIMEOUT`);
  Opus can override per-directive via `timeout_s`.
- **Cost accounting in the loop**: every call to `_ask_opus_directive`
  now fires `record_response`, so recursive Opus consultations inside the
  loop are measurable. Previously only the outer call was tracked.
- **Opus planner → JSON**: `_ask_opus_directive` replies as a JSON object
  (`decision`, `directive`, `why`, `success_check`, `escalate_if`,
  `allowed_tools`, `timeout_s`). The old line-anchored regex parser was
  fragile on multi-line fields.
- **User-facing answer composition**: Rung 4 COMPLETE/BLOCKED branches now
  synthesize an answer from loop evidence instead of leaking the raw Opus
  planner template.
- **Rung 5 fallback on BLOCKED**: when the local loop can't satisfy the
  success_check, the Rung 4 branch hands the full story to full-context
  Claude API before giving up.
- **Apprenticeship on COMPLETE**: successful decompositions are persisted
  to `LearnedAnswers` keyed by `[ladder-solved] <goal>` so the next similar
  goal can skip straight to local execution.
- **Session pruning**: `_load_ladder_sessions` drops complete/blocked
  sessions older than `LADDER_SESSIONS_TTL` (default 7 days) so the
  persisted JSON file doesn't grow forever.

### Things still on the table

- **Ladder is inside LLMOS chat, not in front of Claude Code.** The token
  saver proxy at :8787 does the 3-way opus/sonnet/haiku routing for Claude
  Code (where Ben's real daily spend lives); the ladder only fires in
  `/api/chat`. These are two parallel systems. Open design question:
  should the ladder wrap the proxy, or are they separate products?
- **Calibrator is not wired to ladder thresholds.** `LADDER_WORKER_CONF_THRESHOLD`
  and friends are named constants now, but still magic numbers.
- **No parallel subtask execution.** Independent Read/Grep/Glob subtasks
  still run serially. Cheap win left on the table.
- **Session keying is still goal-sha1.** Two related user turns in the
  same conversation still get two different sessions. Needs a chat/thread
  id plumbed through `route_and_execute` → `get_or_create_ladder_session`.

---

## Original handoff below


This is the next-session handoff for the `Calcifer's Ladder` architecture and the first runtime pass wired into LLMOS.

## Big Picture

The core design decision is:

- Opus should be the strategic brain, not the laborer.
- Local/free models and deterministic tools should do most execution.
- Raw execution should be distilled upward into compact status.
- Downward messages should be bounded directives, not vague "continue" prompts.

This is the architecture direction for OpenKeel 2.0.

## Hyphae Memory

Tower created in Hyphae:

- `Calcifer's Ladder`
- tower id: `cc9f37dd55d7e1f1`

It has:

- 9 floors
- 6 connections
- 3 views

The tower captures:

- why the ladder exists
- the worker/supervisor/Opus hierarchy
- runtime objects (`Directive`, `ExecutionReport`, `StatusPacket`)
- escalation rules
- build plan
- validation metrics

## What Was Built

### 1. Token saver / proxy side

Repo: `/home/om/openkeel`

Files changed:

- `/home/om/openkeel/tools/multi_model_router.py`
- `/home/om/openkeel/tools/mmr_advanced.py`
- `/home/om/openkeel/tools/token_saver_proxy.py`

What changed:

- Fixed intermediate-turn detection to use conversation structure instead of only hard-coded `tool_result` shape.
- Fixed prompt construction so the latest real user text is used as intent, separate from tool output.
- Proxy now prefers `mmr_advanced.py` and falls back to `multi_model_router.py`.
- Fixed `mmr_advanced` confidence scoring bug so capability boosts can apply.

Why it matters:

- This is the live interception/proxy layer that proved the core idea: some post-tool cognition can be handled locally before Anthropic spend.

### 2. LLMOS / Calcifer ladder side

Repo: `/home/om/lllm`

Files changed:

- `/home/om/lllm/lllm/os/token_bridge.py`
- `/home/om/lllm/lllm/os/gui/server.py`
- `/home/om/lllm/lllm/os/workshop.py`
- `/home/om/openkeel/docs/llmos_v2.html`
- `/home/om/lllm/lllm/os/gui/templates/desktop.html`

What changed in `token_bridge.py`:

- Added ladder runtime dataclasses:
  - `Directive`
  - `ExecutionReport`
  - `StatusPacket`
  - `EscalationDecision`
  - `LadderTrace`
- Added distinct model roles:
  - `MICRO_WORKER_MODEL` default `qwen2.5:1.5b`
  - `WORKER_MODEL` default `qwen2.5:3b`
  - `SUPERVISOR_MODEL` default `gemma4:17b`
- Added worker stage:
  - tiny Qwen answers trivial bounded tasks or escalates
- Added supervisor stage:
  - Gemma 4 17B supervises worker output, synthesizes status, answers moderate tasks, or escalates
- Added Opus planner stage:
  - Claude CLI is now asked for a bounded directive instead of just a full answer
- `route_and_execute()` now follows:
  - bash/cache
  - worker
  - supervisor
  - Opus directive
- `classify_rung()` now better reflects:
  - qwen worker path
  - gemma supervisor path
  - opus planner path
- `get_stats()` now exposes ladder trace and configured models

What changed in `server.py`:

- `/api/chat` now returns:
  - `brain`
  - `ladder`
  - existing routing metadata
- GUI stats handling treats rung `>=4` as cloud/premium

What changed in `workshop.py`:

- Fixed `Forge` path bug where `process_query()` result was treated like a dict.
- It now correctly handles `QueryResult` attributes.

What changed in the UI:

- v2 chat bridge now displays:
  - which brain handled the message
  - rung/source
  - Opus directive card when present
- fallback desktop template now appends ladder metadata and directive text

## What Is Working

### The ladder backend shape is real

There is now a backend distinction between:

- worker cognition
- supervisor cognition
- strategic cognition

This is no longer just "local vs Claude".

### Opus can act as planner

Current planner behavior:

- receives a compact status packet
- returns:
  - `DECISION`
  - `DIRECTIVE`
  - `WHY`
  - `SUCCESS CHECK`
  - `ESCALATE IF`

This is the first real implementation of "Opus as the brain".

### Deterministic sanity checks passed

Verified:

- `python3 -m py_compile` on the changed Python files
- deterministic monkeypatched harness showed:
  - worker path
  - supervisor path
  - Opus path

## What Is Not Finished

This is the most important section.

### 1. No recursive directive execution loop yet

Right now, Opus can produce a bounded directive, but lower layers do not yet automatically:

- decompose it
- execute it
- synthesize a fresh status packet
- loop back into supervisor/Opus

This means the architecture is partially implemented, not complete.

### 2. Status packets are still lightweight

The packet exists, but it is currently assembled from local report objects only.

Still needed:

- richer evidence structure
- changed files
- command history
- task state
- completion confidence
- persistent packet store across turns

### 3. Query classification is still simple

`process_query()` still relies on the older `TaskClassification` machinery.

The ladder now sits on top of that, but the classifier itself still needs improvement so it better distinguishes:

- trivial worker tasks
- gemma supervision tasks
- actual Opus-worthy planning tasks

### 4. UI only surfaces metadata

The UI shows the ladder info, but there is no full "directive execution view" yet.

Still needed:

- visible status packet panel
- visible trace panel
- explicit "Opus decided / workers executing" flow
- maybe this belongs in Workshop / Forge / Anvil integration

## Recommended Next Steps

Do these in order.

### Step 1. Build the recursive directive loop

Goal:

- Opus emits directive
- Gemma decomposes directive into bounded local tasks
- Qwens/tools execute
- reports get merged into a new status packet
- loop continues until complete or blocked

This is the highest-value next step.

### Step 2. Make the ladder stateful

Add a persistent per-task/session object:

- current goal
- current directive
- execution history
- status packets
- escalation count
- completion state

Without this, the ladder is just a better router, not a full runtime.

### Step 3. Move ladder concepts into Workshop

The Workshop app is the right place for this to become visible and controllable.

Best fit:

- Forge: conversation + status packet + ladder trace
- Anvil: bounded worker tasks / CLI execution
- Lens: ladder metrics and apprenticeship

### Step 4. Improve local-model adapters

Current code assumes model availability via existing `LocalLLM`.

Still needed:

- better model-specific routing
- explicit timeout/fallback policy
- maybe dedicated adapters for:
  - micro Qwen
  - worker Qwen
  - supervisor Gemma

### Step 5. Add tests for the actual ladder loop

Tests needed:

- worker answers directly
- worker escalates to supervisor
- supervisor answers directly
- supervisor escalates to Opus
- Opus returns directive
- directive execution loop produces new status
- completion decision stops loop

## Architectural Intent

If the next session loses the thread, this is the sentence to recover it:

`Calcifer's Ladder is a cognition broker where Opus does planning and judgment, Gemma supervises and synthesizes, Qwens and tools do execution, and all upward communication is compact status while all downward communication is bounded directive.`

## Notes About Naming

Best names discussed:

- `Calcifer's Ladder`
- `cognition ladder`
- `escalation ladder`

Current canonical design name in Hyphae:

- `Calcifer's Ladder`

## Practical Resume Point

Resume in this order:

1. Read this file.
2. Read `/home/om/lllm/lllm/os/token_bridge.py`.
3. Find `route_and_execute()`.
4. Build the next layer: automatic directive execution under Gemma supervision.
5. Surface that loop in Workshop/Forge.

