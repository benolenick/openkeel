# Calcifer's Ladder — Organic Capability Router

**Date:** 2026-04-08
**Status:** design locked, implementation not started
**Supersedes the design sections of:** `calcifers_ladder_handoff_2026-04-07.md`
**Related:** `../openkeel/calcifer/` (brain.py, chat.py exist), `../tools/token_saver_proxy.py` (:8787, live)

---

## One-sentence frame

**Claude CLI wraps a loop around Anthropic's brain. Calcifer wraps a loop around a pool of brains, one of which is Anthropic's.**

We are rebuilding the Claude Code agent loop — but thinner — so that each iteration of the loop can independently pick which brain runs that step. Brains are discovered, benchmarked, and ranked by cost under a per-job quality floor. Local models, Haiku, Sonnet, and Opus are all just entries in the same pool.

---

## Non-negotiable constraints

1. **Organic to hardware.** Must run on a laptop with no GPU and on a workstation with 3070 + 2×3090 + 200GB RAM. No manual config. No tier table. System detects what it has.
2. **Claude CLI never gets cut out entirely.** It remains one brain in the pool. For jobs only Opus can clear, the loop becomes a one-iteration passthrough and we don't make things worse.
3. **Honest accounting.** `one_metric` (`python3 -m openkeel.token_saver.one_metric`) remains the only savings truth. No counterfactual "N tokens saved" claims.
4. **Proxy at :8787 stays the front door.** Claude Code keeps working as-is. User workflow does not change.

---

## The three primitives

### Runner

Anything that can execute an inference step. Discovered, not declared.

```python
@dataclass
class Runner:
    id: str                      # "ollama:qwen2.5-coder:32b@gpu1"
    device: str                  # "gpu:0" | "gpu:1" | "cpu" | "remote:jagg" | "cloud:anthropic"
    kind: str                    # "local" | "cloud"
    vram_need_mb: int
    ram_need_mb: int
    context_limit: int
    concurrency: int
    cost_per_1k: float           # 0.0 for local; pool_unit weight for cloud
    warm: bool
    measured: dict[str, TaskMeasurement]   # task_class -> {p50, p95, quality, n}
```

**Cloud runners** are registered with seeded quality priors:
- `cloud:haiku`   cost=0.04, seeded quality 0.65-0.75 by task class
- `cloud:sonnet`  cost=0.20, seeded quality 0.80-0.90
- `cloud:opus`    cost=1.00, seeded quality 0.93-0.97

Weights come from `one_metric` so cost math stays consistent with the truth meter.

### Job

A step to be executed, described by what it needs — not who should do it.

```python
@dataclass
class Job:
    task_class: str              # one of a fixed taxonomy (below)
    context_size_tokens: int
    quality_floor: float         # 0..1 minimum acceptable quality
    latency_ceiling_s: float
    replacement_safe: bool       # may a local REPLACE Claude's turn, or only SHRINK?
    success_check: str           # written by the directive-setter (usually Opus)
    directive_from: str          # "opus" | "user" | "self"
```

**Fixed task class taxonomy** (start here, extend only with evidence):

- `summarize_tool_output`
- `summarize_long_doc`
- `classify_intent`
- `answer_qa`
- `code_edit_small`
- `code_edit_refactor`
- `plan_strategic`
- `judge_verify`
- `extract_structured`

### Router

One function. Dumb on purpose. ~80 lines.

```
pick(job) -> runner:
    candidates = [r for r in registry if
                  r.measured[job.task_class].quality >= job.quality_floor
                  and r.measured[job.task_class].p95 <= job.latency_ceiling_s
                  and r.context_limit >= job.context_size_tokens]
    candidates.sort(key=lambda r: (r.cost_per_1k, -int(r.warm)))
    return candidates[0]  # cheapest that clears the bar; warm breaks ties
```

After running, measurement is updated EWMA-style. If `success_check` fails, `job.quality_floor += 0.1` and we pick again.

---

## The loop (the thing we're rebuilding)

```python
def run_turn(messages, origin_directive=None) -> Response:
    job = classify_job(messages, origin_directive)
    iterations = 0
    MAX_ITER = 12

    while iterations < MAX_ITER:
        runner = router.pick(job)
        response = runner.run(messages, tools=TOOLS)

        if response.tool_calls:
            for call in response.tool_calls:
                result = execute_tool(call)           # Read/Write/Bash/Grep/Edit/Glob
                result = maybe_shrink(result, job)    # tiny-runner digest if big
                messages.append(result)
            iterations += 1
            continue

        if success_check_passes(response, job):
            return response
        else:
            job.quality_floor = min(0.99, job.quality_floor + 0.1)
            iterations += 1
            continue

    return escalate_to_cloud(messages)   # final fallback: single Opus pass
```

Per-iteration brain re-pick is the whole game. A single user turn can span 8 local steps → 1 Sonnet step → 2 local steps → done.

---

## Discovery

New module: `openkeel/calcifer/discovery.py`.

Runs at startup and every N minutes. Probes with no hardcoded assumptions:

| Probe | What it finds |
|---|---|
| `nvidia-smi` / `rocm-smi` / `system_profiler` | GPUs + VRAM |
| `psutil.virtual_memory()` | RAM total/free |
| `GET http://localhost:11434/api/tags` | Ollama models |
| scan `~/.config/systemd` + process list | llama.cpp servers, vllm |
| ping configured remotes | jagg, kagg GPUs |
| `ANTHROPIC_API_KEY` + `which claude` | Cloud runners |
| `/opt/homebrew` + `mlx` import check | Apple Silicon runners |

Each probe emits candidate runners. Candidates become real Runners only after passing the **benchmark gate**.

---

## Benchmark gate

New module: `openkeel/calcifer/probes.py` + `probes/fixtures/`.

First time a runner is seen, run a ~30s probe suite:

- 5 summaries of fixed documents → latency + quality-vs-reference
- 5 Q&A pairs with known answers → accuracy
- 3 code-edit tasks with known diffs → correctness
- 2 judge tasks with known verdicts → agreement

Results populate `runner.measured`. After this, every real turn updates the same fields via EWMA. Capability decay and recovery are automatic — no config, no feature flags.

---

## Organic hardware handling (examples, not a tier table)

These are **outcomes**, not plans. The same discovery+benchmark code produces them from nothing.

| Environment | What discovery finds | What the router ends up doing |
|---|---|---|
| Laptop, 16GB RAM, no GPU, no local models | `cloud:haiku`, `cloud:sonnet`, `cloud:opus` | Pure cloud routing by quality floor. Still wins from haiku/sonnet downgrade. |
| Laptop, 16GB RAM, no GPU, qwen2.5:0.5b on CPU | + 1 CPU runner | Tiny runner handles summarize/classify. Cloud handles the rest. |
| 3070 + 24GB RAM, qwen2.5:3b | + 1 GPU runner | 3b does hot shrinking and judging. Cloud for real work. |
| 3070 + 2×3090 + 200GB RAM + 4 ollama models | + up to 5-6 runners across 3 GPUs + CPU | Full mix. Small on 3070, 32b on 3090#1, 27-70b on 3090#2 or CPU for judges. |

200GB RAM is not a footnote: it unlocks CPU-hosted 70B-q4 via llama.cpp for non-latency-critical jobs. Discovery sees the RAM, the probe suite measures the CPU runner honestly, the router picks it when it wins.

---

## Keeping Claude in the loop (two seats)

Claude doesn't get a special code path. Both seats are expressed as jobs with high quality floors:

- **Planner seat** — new user goal → `Job(task_class=plan_strategic, quality_floor=0.95)`. Only Opus clears. Opus writes a Directive with `success_check`.
- **Judge seat** — local loop produced a candidate answer → `Job(task_class=judge_verify, quality_floor=0.80)`. Haiku or a strong local clears. If none clear, Opus judges.

The success_check Opus writes at the planner seat is Claude's leash — locals can't declare victory unless they satisfy criteria Opus set.

---

## Three levers for "pick and choose"

1. **Global cost aggression** — `LADDER_COST_AGGRESSION` env var, 0..1. 0 = maximally cheap-that-clears-floor, 1 = maximally safe (always top-of-class). Default 0.2.
2. **Per-task overrides** — optional `~/.config/calcifer/task_overrides.yaml`:
   ```yaml
   code_edit_refactor: { min_runner: sonnet }
   summarize_tool_output: { max_runner: haiku }
   plan_strategic: { pin: opus }
   ```
3. **Per-turn hint** — prefix in user text: `@haiku`, `@sonnet`, `@opus`, `@local`. Sets floor + filter for one turn.

All three are pre-router filters. None touch the core algorithm.

---

## Three turn walkthroughs (the mental model anchor)

### A — "what does auth_middleware.py do?" (local-only)
1. Proxy :8787 intercepts.
2. `classify_job` → `answer_qa`, floor 0.65, latency 10s.
3. Router picks `ollama:qwen2.5-coder:32b@gpu1` (clears 0.65, cost 0).
4. Loop iter 1: 32b emits `Read("auth_middleware.py")`. Executed. 120 lines.
5. Loop iter 2: 32b emits text answer.
6. Judge (3b on 3070) verifies against `success_check`. Pass.
7. Return to Claude Code. **Anthropic turns: 0.**

### B — "audit auth_middleware.py for security issues" (mixed)
1. Proxy intercepts. `classify_job` → "audit"/"security" keyword → floor 0.95.
2. Router picks `cloud:opus` (only one clearing 0.95). Opus emits Read + Grep.
3. Tools executed. Grep result is 3KB → `maybe_shrink` via 3b on 3070 → 400 tokens.
4. Loop iter 2: this is synthesis, not planning. floor drops to 0.85 for this step.
5. Router picks `cloud:sonnet` (clears 0.85, 5× cheaper than Opus).
6. Sonnet writes the audit report. Judge passes.
7. Return to Claude Code. **Anthropic turns: 1 Opus + 1 Sonnet** (vs. today's 2-3 Opus).

### C — "ultrathink the observer refactor" (passthrough)
1. Proxy intercepts. `classify_job` → "ultrathink" → floor 0.98.
2. Router picks `cloud:opus`. No local step possible.
3. One-iteration passthrough. Loop overhead: ~5ms.
4. Return to Claude Code. **Anthropic turns: 1 Opus** (same as today).

---

## What we keep vs. what we throw out

**Keep:**
- `tools/token_saver_proxy.py` as the front door (gut the keyword router, keep the HTTP shell)
- `openkeel/token_saver/one_metric.py` as the only truth meter
- Calcifer's `Directive`, `ExecutionReport`, `StatusPacket`, `LadderTrace` dataclasses
- Calcifer's tool executor (Read/Write/Bash/Grep/Edit/Glob, allowlists, bash timeout)
- Hyphae for measurement persistence + learned-answer cache

**Throw out:**
- `MICRO_WORKER_MODEL` / `WORKER_MODEL` / `SUPERVISOR_MODEL` constants in `token_bridge.py`
- `classify_rung()` → replaced by `classify_job()` returning a Job spec
- Proxy keyword router → replaced by `router.pick(job)`
- The "R0/R1/R2" mental model — ladder is implicit in cost ordering

---

## File layout (proposed)

```
openkeel/calcifer/
├── __init__.py                  # existing
├── brain.py                     # existing, unchanged for now
├── chat.py                      # existing, unchanged for now
├── discovery.py                 # NEW — probe hardware + services, emit candidates
├── probes.py                    # NEW — benchmark gate, fixture-based
├── probes/
│   └── fixtures/                # NEW — summarize/qa/code-edit/judge references
├── registry.py                  # NEW — Runner + RunnerRegistry (live state)
├── router.py                    # NEW — pick(job) -> runner, ~80 lines
├── loop.py                      # NEW — run_turn(messages) -> response
├── classify.py                  # NEW — classify_job(messages) -> Job
├── tools.py                     # MOVED from token_bridge — Read/Write/Bash/Grep/Edit/Glob
├── success_check.py             # NEW — verify(response, job) -> bool
└── measurements.py              # NEW — EWMA update of runner.measured

tools/token_saver_proxy.py       # MODIFIED — calls loop.run_turn() instead of keyword routing
openkeel/token_saver/one_metric.py  # UNCHANGED
```

LLMOS `/api/chat` will eventually call the same `loop.run_turn()`; that merge is a later step.

---

## Build order (smallest viable slice first)

Each step is independently useful and independently ship-able. Stop at any step and you still have working software.

### Step 1 — Discovery, read-only
Build `discovery.py` + a CLI: `python3 -m openkeel.calcifer.discovery`. Prints a table of candidate runners it found. No routing yet. No benchmarks yet. **User-visible outcome:** you see your actual pool.

### Step 2 — Benchmark gate
Build `probes.py` + fixtures. CLI: `python3 -m openkeel.calcifer.probes --all`. Runs the suite against every candidate, fills `measured`, saves to disk. **User-visible outcome:** you see which runners are actually good at what, on your hardware.

### Step 3 — Registry + router, offline test
Build `registry.py` + `router.py`. CLI: `python3 -m openkeel.calcifer.router --simulate <job.json>`. Feeds synthetic jobs in, prints which runner would be picked and why. No real inference. **User-visible outcome:** you can verify the router's picks make sense before trusting it with real traffic.

### Step 4 — Cloud-only router wired to proxy
`token_saver_proxy.py` calls `router.pick` but the pool is only `{haiku, sonnet, opus}`. Kills the keyword router. **User-visible outcome:** immediate measurable savings on `one_metric` from cloud→cloud downgrade alone. This is the biggest single win and it doesn't require any local loop at all.

### Step 5 — Loop skeleton + local shrinking
Build `loop.py` with one job class supported: `summarize_tool_output`. Local 3b shrinks tool results before they reach cloud. **User-visible outcome:** context-size savings stack on top of step 4.

### Step 6 — Full local agent loop
Extend `loop.py` to run local models with tool calling. Start with `answer_qa` only. Measure honestly — if qwen fails too often, the measurement system de-ranks it and no harm done. **User-visible outcome:** some turns never leave the machine.

### Step 7 — Merge with LLMOS `/api/chat`
Collapse the LLMOS chat path to call `loop.run_turn`. Two front doors, one loop. **User-visible outcome:** Calcifer's Ladder is finally one system, not two.

---

## Open questions (to resolve during Step 1-2, not now)

- **How are cloud runners benchmarked?** Running the probe suite against Opus on first install costs ~$0.50 of actual money. Do we skip cloud benchmarks and rely on seeded priors? (Leaning yes, re-benchmark opportunistically from real traffic.)
- **Where does measurement state live?** SQLite in `~/.calcifer/measurements.db` vs. Hyphae. Hyphae is tempting but Hyphae outages shouldn't break routing. (Leaning SQLite.)
- **What's the first honest success_check implementation?** For `answer_qa`, "does the local 3b judge agree" is cheap but circular if the same 3b is also the answerer. Probably need a minimum of two distinct runners involved in judge chains.
- **Tool calling format normalization.** Ollama's tool-calling output format differs from Anthropic's. Need a shim. ~50 lines.
- **What counts as "replacement_safe"?** Conservative heuristic: only if this is the FIRST turn of a conversation AND no prior tool calls exist AND no plan state has been established. Otherwise shrink-only.

---

## Risks, honestly

1. **Local models are not yet good at running agent loops.** Below qwen2.5-coder:32b, don't even try the full-loop path. Start with cloud→cloud downgrade (step 4) where the wins are certain.
2. **First-pass cloud quality priors are guesses.** The first few hundred turns may mis-route. The success_check → escalate loop has to exist from day one or bad picks go unnoticed.
3. **Latency ceilings must be honest.** Haiku is fast, Opus is slow. If mid-loop turns don't enforce tight latency, the router will happily pick a cheap slow model and the agent feels sluggish.
4. **Discovery false positives.** A crashed Ollama server might still respond to `/api/tags`. Benchmark gate catches this — a runner that fails probes doesn't enter the pool.

---

## The one thing not to forget after context compression

The design is **organic capability pool + cost-ranked router + success-check feedback loop**. It is NOT a 5-tier hardware table, it is NOT a ladder with fixed rungs, and it is NOT a full rebuild of Claude CLI — only the loop shell and the router. Tools, directives, status packets, and the proxy front door already exist. Start with discovery, then cloud-only routing, then layer local on top.

If you only remember one sentence: **per-iteration brain re-pick, ranked by cost under a per-job quality floor, with honest measurement feeding back into the ranking.**
