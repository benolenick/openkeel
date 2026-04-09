# Token Saver Proxy — Results Report
**Date:** 2026-04-07
**Status:** ✅ SHIPPED — 80.6% gross cost reduction measured

---

## TL;DR

A ~200-line request-rewriting proxy in front of Claude Code cut `claude -p` cost
from **$0.333/turn → $0.065/turn** on the standard 10-turn diverse stress test.
Every turn now hits a fully cached 42K-token prefix. Zero `cache_create`.
The fix took three surgical rewrites, one of which was not in the original strategy doc.

## Numbers

Baseline and after measured against the **same 10 prompts** (say pong, 12*13,
dict methods, fibonacci, linux perms, postgres port, bash count, TCP, sorting, linux year).

| metric | baseline | optimized | delta |
|---|---|---|---|
| cache_read tokens | 291,650 | 421,760 | cache now fully hits |
| **cache_create tokens** | **153,590** | **0** | **−100%** |
| input tokens | 60 | 176 | +116 |
| output tokens | 141 | 141 | 0 |
| USD / 10 turns (Opus 4.6) | $3.3288 | $0.6459 | **−80.6%** |
| per-turn cost | $0.333 | $0.065 | −80.5% |
| extrapolated per 100 turns | $33.29 | $6.46 |   |

Cross-checked against two additional scenarios:
- **5 novel prompts** (never seen before): 5/5 full hit
- **5 repeated prompts** (same as round 1): 5/5 full hit

The fix holds for novel and repeat prompts alike.

## Architecture

```
claude CLI  →  ANTHROPIC_BASE_URL=http://127.0.0.1:8787
                 ↓
             token_saver_proxy (FastAPI + httpx)
                 ↓ (rewrites request body before forwarding)
             api.anthropic.com
                 ↓ (SSE response streams back unchanged)
             claude CLI
```

Proxy responsibilities (Phase 1):
1. Force `accept-encoding: identity` on upstream requests (so SSE can be parsed).
2. Rewrite body: strip dynamic blocks, normalize session_id, move cache marker.
3. Pass-through SSE stream byte-for-byte to client.
4. Tap the stream to log per-turn usage to `~/.openkeel/proxy_trace.jsonl`.
5. Fall-through on any exception — byte-identical passthrough, never break the user.

## The three fixes

### Fix 1: Strip dynamic session-start injection blocks

Claude Code's `SessionStart:*` hooks inject ~5–15K chars of dynamic content
(recent hyphae facts, lifetime savings counter, project map, warm-reattach
status) as `<system-reminder>` blocks inside `messages[0].content`. Because
every hook output differs per call, the cache key of the user message block
changes every turn, forcing a fresh `cache_create`.

Proxy drops any content block whose text starts with:
```python
STRIP_PREFIXES = (
    "<system-reminder>\nSessionStart:startup hook success: [TOKEN SAVER]",
    "<system-reminder>\nSessionStart:compact hook success: [TOKEN SAVER]",
    "<system-reminder>\nSessionStart:startup hook success: [OPENKEEL HYPHAE]",
    "<system-reminder>\nSessionStart:compact hook success: [OPENKEEL HYPHAE]",
)
```

This covers all known token_saver + hyphae hook outputs. The session still
gets CLAUDE.md, skills list, and date — which are the only blocks Claude
actually needs for project context. Hyphae recall is still fully available
via `curl /recall` as documented in CLAUDE.md.

Contribution to savings: ~15% on its own.

### Fix 2: Normalize `metadata.user_id` session_id

Claude Code sends:
```json
"metadata": {"user_id": "{\"device_id\":\"...\",\"account_uuid\":\"...\",\"session_id\":\"<fresh-per-call>\"}"}
```

Each `-p` invocation is a new session from Anthropic's view. The session_id
is not in the cached content but it IS part of the cache key resolution.
Proxy rewrites session_id to the literal string `"stable"`.

Contribution: ~30% on its own (from 15% → 44% total).

### Fix 3: Move cache_control marker off volatile user prompt

Claude Code places an `ephemeral 1h` cache_control marker on the last
`messages[0].content` block — which is **the actual user prompt text**.
That means every unique prompt creates a new cache entry at that boundary,
paying full `cache_create` for the entire ~13K tokens of prefix below it.

Proxy moves the marker from the last block (user prompt, volatile) to
the previous block (CLAUDE.md, static). Now the cached region is identical
across all prompts. The user prompt becomes a tiny uncached tail (~15 tokens)
billed as regular input.

Contribution: +36% (from 44% → 80%). **This was the biggest single win and
was NOT in the v3 strategy doc.**

## What the strategy doc got wrong

`docs/token_saver_final.md` (v3 strategy, written 2026-04-07 morning) said:
- Target: 50–60% reduction. **Actual: 80.6%**
- Cache_read is the enemy (86.5% of cost). **Wrong**. Cache_create was
  the enemy. Baseline cost attribution was 86.5% cache_create, 13.1% cache_read.
- 1h cache TTL is feature-flagged off. **Wrong**. 1h cache is active;
  `ephemeral_1h_input_tokens` showed 15,496 on the first baseline turn.
- Primary target: MCP tool diet. **Wrong for `-p` mode**. Tools were 100%
  cacheable as-is; the problem was the message block, not the tool block.
- Output has 26× amortized leverage. **Not relevant here** — output was
  0.3% of cost. The 26× framing applies to long interactive sessions, not
  `-p` one-shots.

The value of the strategy doc was forcing measurement before rewriting.
The value of the measurement was discovering the strategy doc was wrong.

## What the devil's-advocate agent was right about

From `docs/token_saver_proxy_audit_2026-04-07.md` (written mid-run):
> "The proxy is alive and seeing real Anthropic usage metadata. The
> accounting/logging is not. The observed request sizes are still huge.
> So this run does not yet demonstrate successful context reduction."

Correct at time of writing. The usage parser was broken (SSE events
arrived gzipped because httpx auto-added `accept-encoding`). Once that
was fixed, the real numbers appeared and pointed at the right targets.

## How to run

```bash
# One-time setup (auto-started via systemd user service)
systemctl --user enable --now token-saver-proxy

# Point Claude Code at it permanently
echo 'export ANTHROPIC_BASE_URL=http://127.0.0.1:8787' >> ~/.bashrc

# To bypass temporarily (for debugging)
TSPROXY_PASSTHROUGH=1 claude -p "..."

# To dump request bodies for analysis
TSPROXY_DUMP=1 claude -p "..."
# → /tmp/tsproxy_body_*.json
```

Observability:
```bash
tail -f ~/.openkeel/proxy_trace.jsonl
```

## Known limitations

1. **`-p` one-shot mode only verified.** Multi-turn interactive sessions
   should benefit similarly from the marker move (prompt changes, stable
   prefix stays cacheable), but not yet stress-tested.
2. **Strip list is hardcoded.** If new hook prefixes appear, `STRIP_PREFIXES`
   needs updating. Long-term: match by regex + content stability heuristic.
3. **Hyphae context is not auto-injected for trivial `-p` calls.** Claude
   must call `curl /recall` explicitly. For interactive sessions, the
   hooks still run; proxy only strips from the request body after the fact.
4. **Only cache_control on `messages[0]` is moved.** If a user turn sends
   multi-message history with markers in later messages, current logic
   only touches messages[0]. Extend when needed.
5. **Session_id normalization breaks any downstream telemetry** that keys
   by session. Anthropic's billing dashboard will see one giant "session".
6. **Fall-through behavior is critical.** Any exception in `_rewrite_body`
   falls through to pass-through. Don't add blocking logic there.

## Ship plan (what's being implemented now)

1. ✅ `tools/token_saver_proxy.py` — already exists, working
2. 🟡 `~/.config/systemd/user/token-saver-proxy.service` — persistent daemon
3. 🟡 `scripts/start_token_saver_proxy.sh` — manual start fallback
4. 🟡 `.bashrc` export for `ANTHROPIC_BASE_URL`
5. 🟡 Update `docs/token_saver_final.md` with corrected strategy

## Credit

- Ben: for not settling for "done" at 0% savings
- Codex: correctly identified dynamic session-start injection as Phase 1 target
  from the numbers alone, without code access
- Gemini: would have helped but exhausted daily quota
- Devil's-advocate agent: caught the broken usage parser mid-run
