# Token Saver — Strategy v2 (Guiding Star)

**Supersedes:** `/tmp/token_saver_strategy_draft.md`, `docs/token_saver_cache_reduction_ideas.md`
**Date:** 2026-04-07
**Feedback incorporated from:** Gemini, Codex, Claude devil's-advocate agent, proxy-implementation-research agent. Live source inspection of `/tmp/cc_src/claude-code-main/src/services/api/claude.ts`.

---

## North Star

Minimize **normalized cost per *completed task*** — not per turn.

> `cost_units = 1·input + 0.1·cache_read + 1.25·cache_creation + 5·output` (5m TTL)
> `cost_units = 1·input + 0.1·cache_read + 2.0·cache_creation + 5·output` (1h TTL)

Optimizing cost/turn while making Claude dumber is a trap: saved 20% on cache_reads, spent 40% on retries. Always measure per task.

## The counterintuitive cost leverage ranking

One token of **output today** becomes cache_creation next turn (1.25×) and then cache_read on every subsequent turn (0.1× × N). Amortized over a 200-turn session:

| Token added to... | Immediate cost | Amortized cost over 200 turns |
|---|---|---|
| output (Claude writes) | **5.0×** | 5.0 + 1.25 + (0.1 × 200) = **~26×** |
| cache_creation (new prefix block) | 1.25× | 1.25 + (0.1 × 200) = **~21×** |
| cache_read (stable prefix byte) | 0.1× | 0.1 × 200 = **20×** |

**So `output` is ~26× leverage, `cache_creation` is ~21×, `cache_read` is ~20×.** They converge. The dominant term in all three is *stays in prefix × future turns*. The game is: **don't put anything in the prefix that you'll be paying for 200 times.**

## Cost math that actually works (Codex + devil's advocate derivation)

**Break-even for retroactive prefix eviction** (when is it worth rewriting old history?):
```
future_turns_required > ((cache_creation_price - cache_read_price) × invalidated_tokens)
                        ───────────────────────────────────────────────────────────────
                        (cache_read_price × removed_tokens_per_future_turn)
```

Plugged in for 5m TTL (price delta 1.15):
```
future_turns > 11.5 × (invalidated_tokens / removed_tokens_per_turn)
```

**Worked example:** evict 12K tokens of prefix, which invalidates 60K tokens of tail cache.
- `11.5 × 60000 / 12000 = 57.5` more turns needed to pay back.
- Normal sessions don't reach this.

**1h TTL is NOT free.** Price delta becomes 1.9: break-even = `19 × (invalidated/removed)`. 1h cache is better for idle gaps but *worse* when rewriting.

**The only safe retroactive rewrite is one that coincides with natural cache TTL expiry.** If the cache was about to expire anyway, invalidation cost ≈ 0.

## Hard constraints (load-bearing, cannot be violated)

1. **State drift is a killer** (Gemini). The CLI maintains its own in-memory conversation model for token counting and internal pruning. Any proxy rewrite that silently drops bytes will desync the CLI's counter from reality → the CLI may skip its own compactor trigger or reference orphaned `tool_use_id`s → **400 errors**. Mitigation: proxy rewrites must preserve every `tool_use_id` referenced by a later `tool_result`, and must never change total message count.
2. **tool_use/tool_result pair integrity** (proxy researcher). Mismatched or missing pairs → 400. Evictions happen in pairs only.
3. **Cache_control cap = 4 markers per request** (Anthropic hard limit, from `claude.ts:3078`). CLI places exactly one on messages + up to 3 on system/tools. If we rewrite, we must re-normalize.
4. **Streaming is always on**, hardcoded (`claude.ts:1824`). Proxy must use `aiter_raw` stream passthrough, never buffer.
5. **Failure mode = byte-identical passthrough.** If any rewrite path throws, fall through verbatim. No exceptions.
6. **Latency budget: rewrite pipeline ≤ 200ms or bypass.** $0.04 saved is not worth $2.00 of developer flow.
7. **Deterministic wins before lossy ones.** Exact hashes before embeddings before LLM distillation.
8. **Observability before optimization.** Every % target in the old draft is guessing. Measure real prefix composition first.

## What the critics killed

- **"Already saw this" *blocking* guard** — Claude legitimately re-reads files it just modified. A block is always a derailment risk. Keep only **passive hints** ("you read this 3 turns ago — re-read?"), never a block. Confirmed by the devil's advocate being blocked twice by the existing `v4_semantic_skeleton` guard while researching this doc.
- **Async qwen mid-session distiller** — 25s per 5K token block; by the time it returns, the unrewritten version is already server-cached; rewriting it invalidates the tail. Math never works. Kill.
- **"History eviction on every turn"** — math says ~57 future turns to pay back a typical eviction. Only ever do eviction at session end or just before cache TTL natural expiry.
- **"sqlite-vec for semantic dedup" as step 1** — premature. Exact hash-table dedup catches most duplicates at zero risk. Embeddings come later if needed.
- **"Tool schema diet = 20% of cache_read" target** — unverified number. Don't claim until instrumentation measures it.
- **"3 sessions to validate"** — anecdote, not signal. Minimum 20 captured sessions before any lossy change ships.

## Missing ideas (added by critics)

- **Haiku routing for trivial turns** (devil's advocate) — 1-tool checks, kanban curls, file existence tests. Haiku is ~1/12 the cost. Route by turn-complexity heuristic via `ANTHROPIC_MODEL` env or proxy model-field rewrite.
- **Output-side verbosity caps** — cut assistant preamble/reasoning from history before re-send. Because of the 26× amortization, this is probably highest-ROI.
- **System prompt regex trim** (Gemini) — not just tool descriptions, the CLI's own verbose instructions. Easy 1K tokens.
- **Exact-hash dedup table before sqlite-vec** (Codex) — plain SQLite keyed by content_hash + path_hash + mtime. Catches repeat Reads/Greps/identical Bash output at zero risk.
- **Trace capture + offline replay harness** (Codex) — before any lossy rewrite ships, have a test suite of real captured sessions we can replay against proxy variants to measure delta.
- **Freshness model for pointers** — path + content_hash or mtime for file tools; command + cwd + env + stdout_hash + exit_code for bash. Without this, "already saw" breaks on any modified file.
- **Subagent accounting separate from main session** (Codex) — subagent spawns rebuild prefix from scratch; measuring them merged hides the real cost.
- **MCP / bridge seam as an alternative to proxy** (Gemini) — the `src/bridge/` directory is an in-process intercept point. Can diet tool_results *before* CLI history → sidesteps state drift. Use MCP for tool schema work, proxy for history-tail work. **Proxy is not "the only path."**

## Phased build plan (replaces old draft)

### Phase 0 — Observability Only (1 day)
**Goal:** know where the tokens actually go. Ship zero rewrites.

1. **Bare passthrough proxy** at `127.0.0.1:8787`. Python/FastAPI/httpx, ~50 LOC. Stream passthrough via `aiter_raw`. Falls through on any exception.
2. **Measurement tap:** parse `message_delta` SSE event for `usage.input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`. Log per-request to `~/.openkeel/proxy_trace.jsonl`.
3. **Request-body dump** (sanitized — strip API key): one sampled request per session, full messages[] body. Used to compute actual prefix composition (system / tools / CLAUDE.md / history).
4. **Test protocol:** (a) `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude` — 100-turn session, assert zero 400s. (b) Diff raw bytes vs direct run. (c) Kill proxy mid-stream, CLI should error cleanly.
5. **Verify `ANTHROPIC_BASE_URL` actually routes through the proxy** — one-line socat test before any Python code.

**Gate:** 20+ real sessions captured before moving to Phase 1.

### Phase 1 — Deterministic Static Wins (2 days)
All trimming at session-start / CLAUDE.md / tool dispatcher. No history rewrites, no proxy mutations.

1. **CLAUDE.md tiered split** — ~500-byte always-on core + ~5KB reference loaded only on trigger.
2. **Session-start hook trim** — already shipped 2026-04-07 (~750 tokens/session).
3. **System prompt regex trim** — strip verbose CLI identity/instructions via proxy `system` block rewrite (once per session, at first request). Re-place cache_control on the trimmed system block. Target: 500-1000 tokens.
4. **Minimal subagent tool sets** — Explore agents get Read/Grep/Glob only.
5. **Behavioral nudges** — CLAUDE.md rule: "Read with offset/limit for files >5KB, Grep before Read, small bash outputs." Stop-hook nag when Claude reads a full big file without limit.

**Gate:** measurable reduction in `cache_read_input_tokens / turn` after turn 10, across 20+ sessions.

### Phase 2 — Exact-Hash Deduplication (2 days)
Dispatcher-level (not proxy). Catch genuine duplicate tool calls.

1. **SQLite hash table** keyed by (tool_name, content_hash, freshness_key).
   - Read: (path, mtime, size) → content_hash
   - Bash: (command, cwd, stdout_hash, exit_code)
   - Grep: (pattern, path_glob, output_hash)
2. **On tool_use match** → inject `[TOKEN SAVER] duplicate call — see turn N, file unchanged]` instead of running. Only on exact freshness match.
3. **Freshness invalidation:** any Write/Edit/Bash-that-might-modify invalidates file hashes for affected paths.
4. **Passive hints only** when a file was *recently* read but freshness changed: "you read this 3 turns ago, contents differ — re-read?"

**Gate:** false-positive rate < 1% on 20 captured sessions. If any test produces a derailment, roll back.

### Phase 3 — Tool Schema Diet (3 days)
Only after Phase 0 measurements prove the actual byte share of tool defs.

1. **Measure:** what % of cache_read is tool defs? (need Phase 0 data first)
2. **If > 15%:** implement via MCP server that replaces verbose built-in tool definitions with minimal schemas. This sidesteps state drift — CLI never sees the verbose version.
3. **If < 15%:** deprioritize. The payoff isn't there.

### Phase 4 — Output-Side Compression (3 days)
Attack the sneaky highest-leverage target.

1. **Strip assistant preamble/reasoning from history** before re-send via proxy `messages[]` rewrite. Only touches assistant turns that are > N turns old. Preserve all `tool_use` calls.
2. **Cap reasoning re-send**: thinking blocks can be dropped from history entirely (Anthropic handles this natively in some configs — verify).
3. Subject to the break-even math: only worth it if session is > 50 turns.

### Phase 5 — Haiku Routing (2 days)
New idea from devil's-advocate.

1. **Classify turn complexity** pre-dispatch: trivial = single tool call, short input, no code gen. Complex = multi-tool, code gen, reasoning.
2. **Route trivial turns to Haiku** via proxy model-field rewrite. Haiku ≈ 1/12 cost.
3. Gate on quality: if Haiku turns cause retries, roll back.

### Phase 6 — Retroactive Eviction (if still needed, 4 days)
Only if Phases 0-5 don't hit cost target. Strict break-even gating.

1. Only evict batches of >10K tokens at once.
2. Only when break-even future_turns < 30.
3. Only at natural cache TTL boundaries.
4. Only in pairs (tool_use + tool_result together).
5. Preserve all tool_use_ids referenced downstream.
6. Re-place cache_control markers after rewrite.

### Phase 7 — Qwen Distiller (last resort, 3+ days)
Only if deterministic wins plateau. Strict offline evaluation first: replay 20+ sessions, measure quality delta, compare to baseline. Never mid-session sync. Only on session-start batch rewrites of CLAUDE.md / tool defs.

## Measurement protocol (the shipping gate)

**Primary KPI:** cumulative normalized session cost, averaged over 20+ captured sessions per category (coding, debugging, search-heavy).

**Secondary KPIs:**
- `cache_read_input_tokens / turn` after turn 10 (post-warmup slope)
- p95 turn latency
- `turns_to_completion` per task category
- duplicate tool call rate
- tool call retry rate (quality signal)

**Instrumentation schema** per request (logged in `~/.openkeel/proxy_trace.jsonl`):
```json
{
  "ts": 1775590000,
  "session_id": "...",
  "subagent_id": null,
  "turn": 42,
  "prefix_hash": "...",
  "tool_set_hash": "...",
  "claude_md_hash": "...",
  "cache_ttl": "5m",
  "usage": {"input": 450, "cache_creation": 2100, "cache_read": 180000, "output": 890},
  "latency_ms": 2341,
  "rewrite_applied": null
}
```

**Offline replay harness:** capture 20 real sessions in Phase 0. Build a replay tool that feeds them through the proxy with different rewrite configurations. Every lossy change must improve the primary KPI on this corpus before shipping.

## Success criteria

| Phase | Target | Cumulative |
|---|---|---|
| Phase 0 (observability) | 0% (measurement only) | 0% |
| Phase 1 (static trim) | 10% | 10% |
| Phase 2 (exact dedup) | 10% | 20% |
| Phase 3 (tool diet, if payoff proven) | 10-15% | 30-35% |
| Phase 4 (output stripping) | 10% | 40-45% |
| Phase 5 (Haiku routing) | 10-20% (variable by workload) | 50-65% |
| Phase 6 (eviction, if needed) | 5% | 55-70% |
| Phase 7 (qwen, if needed) | 5% | 60-75% |

Honest ceiling: **50-60% cost reduction** on typical long sessions. Higher percentages only on Haiku-friendly workloads. Anyone promising >70% is lying or hasn't done the math.

## Architectural decisions (final)

1. **Proxy is a measurement tap in Phase 0. Becomes a rewriter only in Phase 4+.** Not the "only path" — just one tool.
2. **Most wins are in the CLI/MCP/hook layer**, not the proxy. Proxy is needed only for history mutation + model routing.
3. **Exact dedup before semantic dedup**, always.
4. **LLM distillation is last resort**, not first.
5. **Every rewrite must be gated on offline replay** against 20+ real sessions.
6. **No lossy rewrites ship without a rollback switch** (env var `TOKEN_SAVER_PROXY_MODE=passthrough` reverts to Phase 0 behavior).

## Open questions (blockers before Phase 1)

1. Does `ANTHROPIC_BASE_URL` actually route CLI→proxy cleanly? Test with socat first.
2. Where exactly in `claude.ts` does cache_control placement logic live? `claude.ts:3078` per proxy researcher.
3. Is there a bridge/MCP seam that lets us sidestep state drift for tool definitions? Gemini says yes via `src/bridge/`, needs verification.
4. Can we measure "task completion" automatically for the task-based KPI, or do we need manual annotation?
5. What's the 5m vs 1h cache TTL actually set to in CLI? Is it configurable?

## Glossary additions (extend `audit_playbook.md`)

- **State drift:** when proxy-side message rewrites desync the CLI's in-memory conversation model from what the API received. Leads to 400 errors or skipped compaction.
- **Pair integrity:** every `tool_use` must have a matching `tool_result` with identical `tool_use_id`. Violated = 400.
- **Cache breakpoint:** a `cache_control: {type: "ephemeral"}` marker. Max 4 per request. Placed on the last content block of stable sections.
- **Amortized leverage:** `immediate_cost + (cache_read_price × expected_remaining_turns)`. The "true" cost of any token placed in the prefix.

---

*v2 author: Claude main, synthesizing Gemini + Codex + devil's-advocate-agent + proxy-research-agent feedback on 2026-04-07. Supersedes all prior token saver strategy documents.*
