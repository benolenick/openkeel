# Token Saver — Final Strategy (v3)

**Supersedes:** `token_saver_strategy_v2.md`, `token_saver_cache_reduction_ideas.md`, `/tmp/token_saver_strategy_draft.md`
**Keep reading:** `token_saver_audit_playbook.md` (still current, captures the mental model)
**Date:** 2026-04-07
**Fed by:** live audit of `~/.openkeel/token_ledger.db`, `/tmp/cc_src/claude-code-main/src/services/api/claude.ts`, and 5 critic perspectives (Gemini, Codex, devil's-advocate agent, proxy-researcher agent, Explore agent) plus web research across 13 production projects.

---

## TL;DR

**The goal:** cut cost per *completed task* by **50–60%** without making Claude dumber.

**The lever:** `cost ≈ 1·input + 0.1·cache_read + 1.25·cache_creation + 5·output`, where output has **~26× amortized leverage** because every output token becomes cache_creation next turn and cache_read forever after. Attack the prefix, attack the output, attack the tool-definition bloat. Do NOT trust your own dashboard until it reconciles against `billed_tokens`.

**The method:**
1. Measure first (observability proxy, no rewriting).
2. Kill static fat (tool schema diet via MCP compression — Atlassian pattern, proven 17.6K→500 tokens).
3. Exact-hash dedup of repeat tool calls (zero risk).
4. Output-side history stripping (highest amortized ROI).
5. Haiku routing for trivial turns (1/12 cost on 30-40% of turns).
6. Retroactive eviction only with break-even math, only at TTL boundaries.
7. Qwen distillation only as last resort, only session-start batches.

**The path:** mostly a small proxy (~200 LOC) + one MCP compressor + CLAUDE.md surgery. Not the rewrite-everything moonshot of the v1 draft.

---

## What the research validated, invalidated, and surprised us with

### ✅ Validated
- **Cache reads ≈ 10% input cost.** Anthropic official docs confirm.
- **Tool definitions are massive cache leaks.** Multiple sources: GitHub MCP alone ≈ 55K tokens of tool defs. Atlassian shipped mcp-compressor which takes 94 tools from 17.6K → 500–3.9K tokens. **Production-proven.** Link: atlassian.com/blog/developer/mcp-compression-preventing-tool-bloat-in-ai-agents
- **ANTHROPIC_BASE_URL works for normal sessions.** `musistudio/claude-code-router` and `router-for-me/CLIProxyAPI` are production proxies that rewrite the URL. The Explore agent was too cautious — we're fine. Still run socat as cheap sanity check.
- **No one ships history eviction.** Gap in market is real — our thesis survives.
- **Qwen/LLMLingua distillation in hot path is a trap.** Confirmed by every production project. Latency cost is real; the "20×" numbers are for non-agentic data. Offline session-start use only.
- **Cache_control placement: one marker at end of static content, let Anthropic find longest match.** Simpler than our v2 assumed.

### ❌ Invalidated
- **"1h cache TTL is a free win."** Server-gated via GrowthBook feature flag (`tengu_prompt_cache_1h_config`), not user-configurable. Also: 1h TTL makes cache invalidation worse (price delta 1.9 vs 1.15). Kill this idea.
- **"Proxy is the only substrate with full leverage."** False. MCP is the right seam for tool diet (proven by Atlassian). Proxy is needed only for history-tail work and model routing.
- **"Aggressive history eviction on every turn."** Math says break-even is ~57 turns for typical eviction. Only worth it in batches, at natural cache TTL boundaries, in very long sessions.
- **"Async Qwen distiller mid-session."** 25s per 5K token block + rewriting invalidates cache. Math doesn't work.
- **"Already saw this" blocking guard.** Demonstrably breaks on Claude-modified files. The existing `v4_semantic_skeleton` blocked the devil's-advocate agent during research. Passive hints only.
- **The 9.4M "lifetime savings" claim in the existing dashboard** is ~2× inflated, mostly from `edit_trim` counting whole-file content as baseline. Already fixed this session.

### 🎯 Surprises
- **Output has ~26× amortized leverage.** One token of output becomes cache_creation (1.25×) once, then cache_read (0.1× × ~200 turns) forever. This framing is apparently novel — no paper articulates it. **Attacking Claude's own verbosity is higher ROI than evicting old tool_results.**
- **Deferred tools is already built into the CLI.** `useToolSearch` + `defer_loading: true` on tool schemas. We might get partial tool diet via config, not code. Needs investigation.
- **Claude Code already does some cleanup.** `normalizeMessagesForAPI()` at `claude.ts:1266` strips advisor blocks, tool_reference blocks (when tool search disabled), and >100 media items. Don't reinvent.
- **`runForkedAgent` shares cache across compaction and main thread.** Anthropic is already squeezing savings we didn't know about.
- **Beta header latches are sticky-on.** Once a beta header goes out, it's stuck for the session. Cache-key flips mid-session burn ~20K tokens. Proxy must never reorder/strip/case-flip headers.
- **"89% savings" and "47% savings" claims are hype.** `CLIProxyAPI`'s headline is quota arbitrage across free Gemini/Qwen backends, not same-model compression. Cursor's number is vendor marketing. Discount all public claims 2-3×.

---

## The final architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Claude Code CLI                            │
│                                                                 │
│  - reads CLAUDE.md (trimmed to 500B core + lazy reference)      │
│  - reads session-start hooks (already trimmed 2026-04-07)       │
│  - routes all tool calls through PreToolUse dispatcher hook     │
│                                                                 │
│   PreToolUse ─┬─ exact-hash dedup (SQLite, no vec)              │
│               ├─ Read: offset/limit nudge, semantic skeleton    │
│               ├─ Bash: pkg/test/build/diff compressor (unchanged)│
│               ├─ Edit: trim + honest accounting (fixed today)   │
│               └─ passive "already saw this" HINT only           │
│                                                                 │
└─────────────────┬───────────────────────────────────────────────┘
                  │ HTTPS /v1/messages (SSE streaming)
                  │ ANTHROPIC_BASE_URL=http://127.0.0.1:8787
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│            Token Saver Proxy (127.0.0.1:8787)                   │
│                                                                 │
│  Request rewriter pipeline (each stage try-safe, fall-through): │
│  1. measurement tap  → logs pre/post usage to proxy_trace.jsonl │
│  2. system prompt regex trim (once/session, cache_ctrl re-place)│
│  3. output-side stripper (drop assistant preamble >N turns old) │
│  4. Haiku router (trivial turns only, model field rewrite)      │
│  5. retroactive eviction (only at TTL boundaries, batched)      │
│                                                                 │
│  Non-negotiables:                                               │
│  - tool_use/tool_result pair integrity                          │
│  - beta header passthrough (exact case, order)                  │
│  - max 4 cache_control markers                                  │
│  - SSE aiter_raw, never buffer                                  │
│  - exception → byte-identical passthrough                       │
│  - latency budget 200ms or bypass                               │
└─────────────────┬───────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│            MCP Tool Compressor (co-located)                     │
│                                                                 │
│  - wraps big MCP servers (GitHub MCP etc.)                      │
│  - tiered tool-description compression (Atlassian pattern)      │
│  - 17.6K → 500-3900 tokens per server                           │
│  - loaded via `claude mcp add` config, not proxy                │
└─────────────────────────────────────────────────────────────────┘
                  │
                  ▼
      ┌───────────────────────────┐
      │    api.anthropic.com      │
      └───────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Observability & Measurement (always on)                       │
│                                                                 │
│  proxy_trace.jsonl  ── real request usage per turn              │
│  ~/.openkeel/token_ledger.db → billed_tokens table (ground      │
│                                truth)                          │
│  cost_units_per_task  (primary KPI)                             │
│  20-session replay corpus  (shipping gate for any lossy change) │
└─────────────────────────────────────────────────────────────────┘
```

---

## The phased build plan (final)

### Phase -1 — Verify the proxy path (5 minutes, blocking)
```bash
# Terminal 1
socat -v TCP-LISTEN:9999,reuseaddr,fork OPENSSL:api.anthropic.com:443,verify=0
# Terminal 2
ANTHROPIC_BASE_URL=http://127.0.0.1:9999 claude --print "hello"
# PASS: Terminal 1 shows headers + body → proxy path works → proceed
# FAIL: no traffic → pivot to CLI fork fallback (v2.1)
```

### Phase 0 — Observability Only (1 day, ~80 LOC)
**Ship zero rewrites.** Just measure.

Files to create:
- `tools/token_saver_proxy.py` — FastAPI + httpx, ~80 LOC
- `tools/token_saver_proxy/trace_writer.py` — SSE tap, parses `message_delta` usage, writes `~/.openkeel/proxy_trace.jsonl`
- `tools/token_saver_proxy/body_sampler.py` — one sampled full request body per session (sanitized — strip Authorization)

**Exit criteria:** 20+ real sessions captured. Compute actual prefix composition: `system_prompt_bytes / tool_def_bytes / claude_md_bytes / history_bytes / output_in_history_bytes`. THIS is what decides Phase 1 priorities. Nothing ships before this data exists.

### Phase 1 — Deterministic Static Wins (2 days)
**No proxy rewrites. No lossy changes.**

1. **MCP tool compressor** — steal Atlassian's tiered compression pattern. Wrap each MCP server in a compressor that serves condensed tool descriptions. Targets: any MCP with >5K tokens of defs. **Expected win: 5-15K tokens per session from tool-def bloat alone.** Proven in production by Atlassian Rovo.
2. **CLAUDE.md tiered split** — 500-byte "always on" core + 5KB lazy reference injected only on trigger words (localedit, hyphae, kanban). Current CLAUDE.md is ~8KB; lazy-load most of it. **Expected: 1.5-3K tokens/turn saved after turn 1.**
3. **Subagent tool subsets** — Explore subagents get Read/Grep/Glob only. Task agents get Edit/Bash on top. Already partially shipped via hook config.
4. **Session-start hook trim** — **already shipped** this session (~750 tokens/session).
5. **Behavioral CLAUDE.md rules** — "Read with offset/limit for files >5KB, Grep before Read, small bash outputs." Stop-hook nag when violated.
6. **Investigate `useToolSearch` / `defer_loading`** — may give us partial tool diet via CLI config instead of MCP code. 1 hour investigation.

**Exit criteria:** measurable 10-15% drop in `cache_read_input_tokens / turn` after turn 10, across 20+ sessions.

### Phase 2 — Exact-Hash Dedup Dispatcher (2 days)
At the PreToolUse layer, not the proxy (avoids state drift risk).

- SQLite keyed by `(tool_name, content_hash, freshness_key)`
- Read key: `(path, mtime, size)` → content hash
- Bash key: `(command_hash, cwd, stdout_hash, exit_code)`
- Grep key: `(pattern, path_glob, output_hash)`
- Any Edit/Write invalidates file hashes for affected paths.
- **Passive hint only** when freshness changed. Never block.

**Exit criteria:** false-positive rate < 1% on 20 captured sessions.

### Phase 3 — Proxy Output-Side Compression (3 days)
**The highest-leverage target per the 26× amortization math.**

- Proxy walks `messages[]` in every request
- For assistant turns > N old: strip reasoning preamble while preserving `tool_use` calls exactly
- Preserve all `tool_use_id` references
- Never touch the last 3 turns (stability boundary)
- Re-place cache_control markers after rewrite
- Fall through on any exception

**Exit criteria:** 10%+ cost reduction on long sessions without quality regression (retry rate unchanged).

### Phase 4 — Haiku Routing (2 days)
**New idea, high ROI if the classifier works.**

- Turn complexity classifier: trivial = single tool call, short input, no code generation, no reasoning
- Trivial turn detected → proxy rewrites `model: "claude-haiku-4-5"` before forwarding
- Gate on retry rate — if Haiku turns cause retries, roll back
- Expected: 30-40% of turns are trivial (kanban curls, existence checks, simple greps), at 1/12 the cost

**Exit criteria:** 10-20% cost reduction without retry rate increase.

### Phase 5 — Retroactive Eviction (only if still needed, 4 days)
**Strict break-even gating.** Apply only when:
- `future_turns × removed_tokens_per_turn > 11.5 × invalidated_tokens` (5m TTL) OR
- `future_turns × removed_tokens_per_turn > 19 × invalidated_tokens` (1h TTL)
- Or at natural cache TTL boundary (invalidation cost ≈ 0)
- Evict in pairs only (tool_use + tool_result together)
- Batched, never incremental

### Phase 6 — Qwen Distiller (last resort, 3 days)
**Only for session-start batch rewrites.** Never mid-session. Never synchronous on the hot path. Must beat baseline on the 20-session replay corpus.

---

## What a successful session will look like

Once Phase 0-2 is shipped, startup in terminal:

```
$ ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
[token-saver-proxy] listening on 127.0.0.1:8787 → api.anthropic.com
[token-saver-proxy] passthrough mode (rewriting disabled: TOKEN_SAVER_PROXY_MODE=passthrough)
[token-saver-proxy] trace → ~/.openkeel/proxy_trace.jsonl
[token-saver] dispatcher dedup cache warm (137 entries, 2 days TTL)

[TOKEN SAVER] Session started (id=8ac21f...). Baseline prefix: 18,432 tokens
  system prompt:  6,120 tok
  tool defs:      4,891 tok  (compressed by mcp-compressor from 21,340 tok)
  CLAUDE.md:        680 tok  (core only, 4.8K deferred)
  prior hooks:    6,741 tok

[turn 1]  usage: input=312 cache_cr=5,021 cache_rd=18,432  cost=7,868u  (baseline)
[turn 2]  usage: input=44  cache_cr=1,802 cache_rd=23,189  cost=4,573u  ← cache warm
[turn 3]  usage: input=78  cache_cr=891   cache_rd=24,070  cost=3,526u
[turn 4]  [DEDUP HIT] Read /path/to/file.py → served from dispatcher cache (skipped 2,340 tok)
[turn 4]  usage: input=88  cache_cr=142   cache_rd=23,782  cost=2,780u
...
[turn 30] cumulative: 147,890 cost_units  (vs 312,400 baseline → 52.6% saved)
```

Periodic health check:
```
$ python -m openkeel.token_saver.proxy.status
Token Saver Proxy — status
  Uptime: 3h 14m
  Requests: 284 (zero 400s)
  Average latency overhead: 47ms
  Rewrites applied: 12 (system_prompt=1, output_strip=9, haiku_route=2)
  Fall-throughs: 0
  Current session savings: 46.2%  (measured against billed_tokens)
```

---

## Success criteria (honest targets)

| Phase | Feature | Expected savings (cumulative) |
|---|---|---|
| Phase 0 | Observability only | 0% (measurement only) |
| Phase 1 | MCP tool diet + CLAUDE.md tier + behavioral | 15–20% |
| Phase 2 | Exact-hash dedup | 20–25% |
| Phase 3 | Proxy output stripping | 30–35% |
| Phase 4 | Haiku routing | 40–50% |
| Phase 5 | Retroactive eviction (if needed) | 45–55% |
| Phase 6 | Qwen distiller (if needed) | 50–60% |

**Ceiling: 50-60% on typical long sessions.** Anyone promising more is doing quota arbitrage (CLIProxyAPI) or vendor marketing (Cursor). Our number is what the cache math supports.

---

## Known open questions (park for next session)

1. Does `ANTHROPIC_BASE_URL` route cleanly? Verify with socat (~5 min).
2. Does `useToolSearch` deferred-tool mechanism give us partial diet for free?
3. What's the actual tool-def byte share in a real Phase 0 trace? (drives Phase 1 priority)
4. Can we auto-classify "task completion" for the task-based KPI, or do we annotate manually?
5. Does the Atlassian mcp-compressor pattern work with Anthropic's MCP or is it Atlassian-specific?

---

## What we're NOT building (and why)

- ❌ Mid-session Qwen distillation — latency kills it
- ❌ History eviction on every turn — cache invalidation math doesn't work
- ❌ Active "already saw this" blocking — derails legitimate re-reads
- ❌ 1h cache TTL forcing — server-gated, not our lever
- ❌ Our own compaction replacement — CLI hardcodes Haiku for compaction
- ❌ Tool schema rewriting via proxy — state drift risk; use MCP instead
- ❌ sqlite-vec semantic dedup in Phase 2 — premature; exact hash is enough
- ❌ LLMLingua in hot path — wrong domain, latency cost real

---

## Next session kickoff (paste this)

```
Resume token saver Phase 0. Previous work: docs/token_saver_final.md
First: run socat test for ANTHROPIC_BASE_URL routing.
Then: build tools/token_saver_proxy.py per spec in Final doc.
Then: run 5 real sessions through it and compute actual prefix composition.
Do NOT ship any rewrites until prefix composition is measured.
```

---

## References (things we're stealing from)

| Project | What we steal | URL |
|---|---|---|
| Atlassian mcp-compressor | Tiered tool-description compression (17.6K→500) | atlassian.com/blog/developer/mcp-compression-preventing-tool-bloat-in-ai-agents |
| Aider repo map | tree-sitter + graph rank into fixed token budget | aider.chat/docs/repomap.html |
| lazy-mcp (voicetreelab) | MCP deferred schema loading pattern | github.com/voicetreelab/lazy-mcp |
| claude-code-router | Confirms `ANTHROPIC_BASE_URL` production path | github.com/musistudio/claude-code-router |
| Anthropic prompt-caching docs | One breakpoint, longest-match, 10% read cost | platform.claude.com/docs/en/build-with-claude/prompt-caching |

## References (things we're avoiding)

| Thing | Why |
|---|---|
| CLIProxyAPI "89% savings" | Quota arbitrage across free backends, not compression |
| Cursor "47% dynamic context" | Vendor marketing, no independent verification |
| LLMLingua "20× compression" | Benchmarks are non-agentic; latency cost real |
| Our own old dashboard "50% lifetime" | Inflated by broken `edit_trim` formula — fixed |

---

*Final strategy signed off by: main Claude, 5 critic agents (Gemini + Codex + devil's-advocate + proxy-researcher + Explore + web-research), 2026-04-07. Load this doc as context for the next session to resume.*
