# Claude CLI Local LLM Audit

Date: 2026-04-08

Scope:
- Claude CLI token saver hooks
- Claude proxy/router behavior
- Local LLM usage inside Claude-facing paths
- Current leverage of the remote 3090 (`192.168.0.224`)

This audit is intentionally adversarial. The goal is to separate:
- implemented
- wired
- active in the live service
- actually proven by recent runtime evidence

## Executive Summary

Claude CLI is using local LLMs in real ways today, but mostly as an input/output filter around tool calls, not as a deeply integrated reasoning partner.

What is clearly real:
- `SessionStart` injects project memory/context to reduce setup churn.
- `PreToolUse` blocks and replaces many `Read`, `Bash`, `Grep`, `Glob`, `Edit`, `Write`, `Agent`, and `WebFetch` calls.
- Several LLM-backed engines are active in the savings ledger.
- The token-saver summarizer layer is wired to the remote 3090 and can reach `qwen2.5:3b`.
- The live Claude proxy is actively downgrading some requests from Opus to Haiku/Sonnet.

What is not currently true in the strong sense:
- Claude CLI is not currently getting broad local-LLM turn-by-turn intelligence from the proxy.
- The live proxy service is running with the Qwen classifier disabled and the multi-model local-response path disabled.
- The checked-in proxy code has `MMR_MODULES = ()`, so the local intermediate-turn replacement path is effectively off in the current file.
- Some observability is inaccurate; the "one honest metric" proxy section underreports routing.

Bottom line:
- The system is saving tokens.
- A meaningful part of that is real local LLM work.
- But the current live system is narrower and less intelligent than the architecture pitch implies.

## Audit Verdict

### 1. Is Claude CLI leveraging local LLMs?

Verdict: **Yes, but mostly at the tool boundary.**

It is clearly doing local-model work in these areas:
- file summarization / reread summaries
- bash output summarization
- grep result summarization
- Hyphae recall reranking
- goal-conditioned file filtering
- semantic skeleton generation for large files
- web fetch summarization
- subagent prompt compression

It is **not** currently leveraging locals strongly in these areas:
- proxy-side intelligent model routing on every turn
- local intermediate-turn response replacement
- robust local reasoning inside Calcifer's Ladder as configured

### 2. Is Claude CLI using the remote 3090 well?

Verdict: **Partially.**

The remote 3090 at `192.168.0.224:11434` is reachable and currently exposes:
- `gemma4:e2b`
- `gemma4:26b`
- `qwen2.5:3b`
- `qwen3.5:latest`
- others

Evidence:
- `curl -s http://192.168.0.224:11434/api/tags`

But the wiring is inconsistent:
- Token Saver summarizer resolves to the remote 3090 and `qwen2.5:3b`.
- LLMOS/Calcifer `LocalLLM` defaults to local Ollama on `127.0.0.1:11434`.
- Local Ollama currently only has `gemma3:1b` and `gemma4:e2b`.

So the remote 3090 exists, but not all Claude-adjacent local paths are actually using it.

### 3. Is the live Claude proxy doing what the docs claim?

Verdict: **Partially, with important caveats.**

Real:
- It is live as a systemd user service.
- It is routing some turns from Opus to Haiku/Sonnet.
- Over the last 7 days, recent trace analysis showed:
  - 344 turns routed to Haiku
  - 166 turns routed to Sonnet

Not real in the strong sense:
- The live service currently disables:
  - `TSPROXY_NO_QWEN_ROUTER=1`
  - `TSPROXY_NO_MMR=1`
- So current routing is mostly:
  - hard rules
  - fallback heuristics
  - conservative downgrades
- not an active local-LLM router supervising every turn

Evidence:
- `/home/om/.config/systemd/user/token-saver-proxy.service.d/env.conf`

## Current Claude CLI Local LLM Architecture

### A. SessionStart prefill

File:
- `/home/om/openkeel/openkeel/token_saver/hooks/session_start.py`

What it does:
- reads Claude session id
- deduplicates repeated briefings
- starts the token saver daemon if needed
- queries Hyphae for recent work and infra facts
- injects that context into Claude at session start

Why it matters:
- this is a real Claude-token reduction path
- it prevents repeated recall/setup work

Assessment:
- real
- low risk
- good leverage

### B. PreToolUse interception

File:
- `/home/om/openkeel/openkeel/token_saver/hooks/pre_tool.py`

This is the core of the actual value.

It really blocks and replaces tool calls in many cases.

Examples:
- `Read`
  - binary-file refusal
  - reread unchanged suppression
  - cached reread summary
  - large-file head/structure/tail compression
  - goal-conditioned filter
  - semantic skeleton
- `Bash`
  - package manager quieting
  - git diff/log compression
  - test/build output compression
  - curl/ssh/log/process listing compression
  - local edit convention
- `Grep`
  - search filtering
  - cross-tool collapsing for already-read files
  - LLM summarization
- `Glob`
  - search filtering
  - truncation
- `Agent`
  - prompt compression / respawn instruction
- `WebFetch`
  - local summarization against the question
- `Edit` / `Write`
  - direct execution with compact confirmations

Assessment:
- this is the strongest real part of the system
- this is where most proven leverage lives
- many token-saver claims are only defensible because this hook is real

### C. Token Saver daemon + summarizer

Files:
- `/home/om/openkeel/openkeel/token_saver/daemon.py`
- `/home/om/openkeel/openkeel/token_saver/summarizer.py`

What it does:
- runs a local HTTP daemon on `127.0.0.1:11450`
- caches file summaries
- exposes filter/classify/summarize endpoints
- records ledger events

Important current fact:
- the summarizer currently resolves to:
  - `OLLAMA_URL = http://192.168.0.224:11434`
  - `MODEL = qwen2.5:3b`

Assessment:
- real
- remote 3090 is actually in use here
- this is one of the few places where the "3090 is paying rent" claim is directly supported

## Live Proxy / Router State

### Service status

Live service:
- `token-saver-proxy.service`
- active on 2026-04-08 during audit

Files:
- `/home/om/openkeel/tools/token_saver_proxy.py`
- `/home/om/.config/systemd/user/token-saver-proxy.service`
- `/home/om/.config/systemd/user/token-saver-proxy.service.d/env.conf`

### What the live service is actually doing

The live service is currently in a constrained mode:

Environment:
- `TSPROXY_NO_QWEN_ROUTER=1`
- `TSPROXY_NO_MMR=1`

Implication:
- Qwen classifier is disabled
- local intermediate-turn replacement is disabled

So the current live behavior is:
- keep Opus on explicit hard tasks
- downgrade some short/simple turns via heuristics
- mostly route via fallback logic, not local intelligence

### Evidence from traces

Recent 7-day trace summary from `~/.openkeel/proxy_trace.jsonl`:
- total turns observed: 1043
- routed to Haiku: 344
- routed to Sonnet: 166

Route sources:
- `fallback_heuristic`: 383
- `hard_rule_sonnet`: 122
- `hard_rule_opus`: 3
- `qwen`: 7

Interpretation:
- the router is active
- but almost all of its work is non-LLM routing
- current savings from model routing are mostly heuristic downgrades, not active local-model judgment

## Local Response Replacement (MMR)

Files:
- `/home/om/openkeel/tools/token_saver_proxy.py`
- `/home/om/openkeel/tools/multi_model_router.py`
- `/home/om/openkeel/tools/mmr_advanced.py`

### What the architecture claims

The architecture and handoff docs describe a path where:
- intermediate Claude turns
- especially tool-followup cognition
- can sometimes be answered locally
- instead of hitting Anthropic at all

That would be a major token saver if reliable.

### What the checked-in code says

In `token_saver_proxy.py`:
- `MMR_MODULES = ()`

That means the current file has no active MMR modules configured.

### What runtime logs show

`proxy_trace.jsonl` does contain older `mmr_used` entries.
Those prove this path existed in live traffic recently.

But for the currently running service after the latest restart, `mmr_used` was zero.

Assessment:
- historically real
- currently not live in the current service configuration
- not something you should count as present-value token savings today

## Claude CLI Model Routing Quality

### Does it route to Opus / Sonnet / Haiku?

Verdict: **Yes, but not as intelligently as advertised.**

What is true:
- current proxy does route requests away from Opus
- recent traces show Haiku and Sonnet routing are happening

What is misleading if stated too strongly:
- "custom local model router" implies active LLM-driven turn classification
- live evidence says most routing is fallback heuristic
- the Qwen classifier is effectively off in production right now

So the honest statement is:
- there is successful 3-way routing in practice
- but most of it is not currently driven by local LLM judgment

## Ledger Findings: Where the Real Savings Are

Recent 7-day top savings from `~/.openkeel/token_ledger.db`:

- `edit_trim`: 19.06M chars
- `prefill_ranked_map`: 7.96M
- `bash_compress`: 6.36M
- `prefill_index`: 2.55M
- `bash_llm_summarize`: 2.23M
- `bash_predict`: 1.37M
- `output_compress`: 1.28M
- `recall_rerank`: 1.07M
- `working_set_block`: 1.02M
- `v4_semantic_skeleton`: 0.99M
- `write_trim`: 0.92M
- `goal_filter`: 0.21M
- `subagent_compress`: 0.033M

Interpretation:
- the biggest wins are not from proxy routing
- they are mostly from:
  - deterministic tool interception
  - direct edit/write shrinking
  - session prefill
  - bash compression
  - selective LLM summarization

This is important.

If someone says "the main savings come from the custom Claude router," that is not supported by the ledger.

## Observability Problems

### 1. `one_metric.py` underreports proxy routing

File:
- `/home/om/openkeel/openkeel/token_saver/one_metric.py`

Problem:
- its proxy section checks `req.routed_haiku`
- live traces store `req.routed_model`

Result:
- during audit it reported:
  - `0 routed to Haiku`
- while the trace clearly showed real Haiku routing

Assessment:
- the "one honest metric" framing is compromised by a real reporting bug
- the core week-over-week pool metric may still be useful
- the proxy contribution subsection is not trustworthy as written

### 2. Claude-facing local availability checks are weak

In LLMOS `LocalLLM`:
- `.available` means endpoint exists
- not that requested model exists

Result:
- `qwen2.5:3b` can appear available on local Ollama even when the model is absent
- actual call then fails with `404`

Assessment:
- bad runtime hygiene
- creates false confidence
- should be fixed before expanding local routing

## Calcifer / LLMOS Relevance to Claude CLI

This audit is Claude-CLI-centered, but one adjacent problem matters:

The Ladder code in `lllm/os/token_bridge.py` is materially real, but its local model client points at local Ollama by default, not the remote 3090.

Files:
- `/home/om/lllm/lllm/os/token_bridge.py`
- `/home/om/lllm/lllm/os/llm_client.py`

Observed during audit:
- `what is tcp vs udp` returned locally from rung 3
- `design a distributed rate limiter ...` escalated to Claude and worked
- `what is 5+3` returned a broken local answer about a corrupted probe

Interpretation:
- rung-3 local answering is real
- quality is not consistently trustworthy
- the configured model expectations do not match the actual local Ollama inventory

This matters because any future attempt to fold Ladder-style local reasoning into Claude CLI will inherit these quality and wiring issues unless fixed first.

## Brutally Honest Assessment

### What is genuinely good

- The Claude hook architecture is real and effective.
- `PreToolUse` is doing real work and is the strongest part of the whole system.
- The remote 3090 is real and useful.
- The summarizer layer is using that remote 3090.
- Tool-boundary compression is a valid and relatively safe use of small local models.

### What is overstated

- The current Claude router is not meaningfully "local-LLM-driven" in production.
- The system is not currently getting major savings from local intermediate-turn reasoning.
- Some metric framing is stronger than the telemetry justifies.
- Some docs still imply capabilities that are now disabled or only historically true.

### What is weak

- inconsistent model wiring across subsystems
- weak model-availability checking
- broken or stale observability in at least one "honest" metric path
- local-answer quality is not good enough to trust broadly without stricter gating

## Highest-Leverage Improvements

Priority order below is based on expected gain vs risk.

### Priority 1: Make all Claude-facing local calls use the intended GPU intentionally

Goal:
- stop accidental split-brain between local 3070 and remote 3090

Needed:
- unify endpoint/model resolution for:
  - token saver summarizer
  - proxy classifier / MMR
  - any Claude-facing local routing logic
- add a single authoritative resolver

Expected benefit:
- immediate quality and latency consistency
- fewer silent fallbacks

### Priority 2: Fix observability before more cleverness

Needed:
- fix `one_metric.py` proxy routing parsing
- add explicit counts for:
  - heuristic downgrades
  - Qwen-classified downgrades
  - MMR local responses
  - per-engine local-model usage
- log model endpoint + model name for each LLM-backed engine event

Expected benefit:
- you can finally tell what is actually saving tokens

### Priority 3: Re-enable Qwen router in shadow mode only

Needed:
- leave live routing unchanged
- run Qwen classifier beside it
- log:
  - what heuristic did
  - what Qwen would have done
  - downstream outcome quality if possible

Expected benefit:
- safe measurement of whether the local classifier is worth trusting again

### Priority 4: Keep MMR disabled until quality is proven

Reason:
- local replacement of Claude intermediate turns is the highest-risk local path
- wrong answer there silently changes task trajectory

Needed before re-enable:
- real evaluation set from captured Claude transcripts
- hard gating
- fallback on uncertainty
- better endpoint/model hygiene

Expected benefit:
- avoids shipping hallucination debt into the core loop

### Priority 5: Push harder on deterministic compression

Reason:
- the ledger says this is where the biggest wins already are

Best candidates:
- more `Edit` payload shrinking
- more `Read`/`Grep` cross-tool awareness
- more `Agent` prompt trimming
- better session prefill pruning

Expected benefit:
- safer gains than ambitious local reasoning

### Priority 6: Fix `LocalLLM.available`

Needed:
- availability should mean:
  - endpoint up
  - requested model present
  - ideally a tiny probe succeeds

Expected benefit:
- stops fake-available states
- prevents invisible quality regressions

## Recommended Work Plan for Claude

If you want Claude to improve this system, this is the order to ask for:

1. **Unify remote-3090 model resolution across Claude-facing paths**
   - Build one shared endpoint/model resolver.
   - Make proxy router and any local classifier use it.
   - Fail closed on missing requested model, not "endpoint exists".

2. **Repair the metrics**
   - Fix `one_metric.py` proxy parsing.
   - Add a truthful per-engine local-LLM usage report.
   - Add "live service flags" visibility in reports.

3. **Add shadow-mode routing evaluation**
   - Qwen router logs decisions without affecting live routing.
   - Compare heuristic vs Qwen over a day of traffic.

4. **Do not re-enable MMR yet**
   - First build replay/eval harness from captured transcripts.

5. **Improve deterministic token reductions**
   - especially `Agent`, `Read`, and `Edit`

## File Map for Claude

Core Claude CLI hook path:
- `/home/om/openkeel/openkeel/token_saver/hooks/session_start.py`
- `/home/om/openkeel/openkeel/token_saver/hooks/pre_tool.py`
- `/home/om/openkeel/openkeel/token_saver/hooks/post_tool.py`

Daemon / summarizer:
- `/home/om/openkeel/openkeel/token_saver/daemon.py`
- `/home/om/openkeel/openkeel/token_saver/summarizer.py`

Proxy / router:
- `/home/om/openkeel/tools/token_saver_proxy.py`
- `/home/om/openkeel/tools/multi_model_router.py`
- `/home/om/openkeel/tools/mmr_advanced.py`

Metrics:
- `/home/om/openkeel/openkeel/token_saver/one_metric.py`
- `/home/om/openkeel/openkeel/token_saver/week_report.py`
- `/home/om/openkeel/openkeel/token_saver/honest_dashboard.py`

Adjacent LLMOS pieces:
- `/home/om/lllm/lllm/os/token_bridge.py`
- `/home/om/lllm/lllm/os/llm_client.py`

Live service config:
- `/home/om/.config/systemd/user/token-saver-proxy.service`
- `/home/om/.config/systemd/user/token-saver-proxy.service.d/env.conf`

Runtime evidence:
- `~/.openkeel/proxy_trace.jsonl`
- `~/.openkeel/token_ledger.db`

## Final Judgment

The system is not fake.

It is saving tokens, and some of that savings is genuinely coming from local LLM work.

But the strongest real mechanism is not "Claude is being locally outsmarted turn by turn." The strongest real mechanism is:
- intercept tool calls
- shrink what Claude has to read
- avoid redundant reads
- compress noisy outputs
- trim oversized edits

The custom router exists, but in live production it is currently much more heuristic and much less local-LLM-driven than the architecture story suggests.

If you want the next phase to be honest and effective:
- clean up the wiring
- fix the metrics
- evaluate in shadow mode
- only then trust more local intelligence inside Claude's loop
