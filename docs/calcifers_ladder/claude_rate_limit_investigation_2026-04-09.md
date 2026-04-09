# Claude Rate Limit Investigation Context

**Date:** 2026-04-09
**Purpose:** handoff context for another agent to investigate why Claude/OpenKeel keeps hitting API rate limits
**Status:** active hypothesis note, not final diagnosis

## 1. Short Summary

The current best explanation is:

- Claude is being rate limited because premium-model requests are being made from **very large accumulated sessions**
- those requests are sometimes routed to **Sonnet** or **Opus**
- the request bodies are extremely large
- some of those turns also trigger large fresh `cache_create` writes
- repeated retries or repeated nearby calls after a `429` make the problem worse

This looks much more like a **bloated-context / premium-turn / repeated-call** problem than a simple “too many tiny requests” problem.

## 2. Main Evidence

### 2.1 Recent `429` traces are Sonnet requests

From `~/.openkeel/proxy_trace.jsonl`, recent failures include:

- `status=429`
- `model=claude-sonnet-4-6`
- `route_decision.source=fallback_heuristic`
- `route_decision.reason=default_sonnet`
- `n_messages=108`
- `body_chars≈235k`

Representative trace lines near the end of the file:

- `ts=1775756984.8395317`
- `ts=1775757089.8899753`

These are not small requests. They are large-context Sonnet turns.

### 2.2 Huge Opus requests are also happening

There are premium Opus turns with very large bodies and large fresh cache writes, for example:

- `model=claude-opus-4-6`
- `body_chars≈748,968`
- `cache_create=243,414`
- `out=9,684`
- `latency_ms≈179,696`
- route reason `matched:architect`

This strongly suggests that some “think hard / architect” prompts are being issued inside already-fat OpenKeel/Claude sessions.

### 2.3 The live proxy router is still mostly heuristic

Current live service config:

- file: `~/.config/systemd/user/token-saver-proxy.service.d/env.conf`
- `TSPROXY_NO_QWEN_ROUTER=1`

So the local classifier is currently disabled and the router is mostly:

- force Opus on keywords like `think hard`, `architect`, `security`
- cap to Sonnet on `quick`, `just`, `simple`
- route short/no-blocker turns to Haiku
- otherwise default to Sonnet

That logic lives in:

- `tools/token_saver_proxy.py`

### 2.4 Claude hooks are still active globally

Current `~/.claude/settings.json` includes:

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`

This means many OpenKeel sessions are still carrying dynamic injected context and hook-layer behavior even before the proxy routing decision is made.

## 3. Files To Inspect

### Routing / request rewrite

- `tools/token_saver_proxy.py`
- `~/.config/systemd/user/token-saver-proxy.service.d/env.conf`

Important functions / sections:

- `_classify_model(...)`
- `_rewrite_body(...)`
- hard keyword rules
- fallback heuristic
- history eviction / tool diet

### Hook and session context

- `~/.claude/settings.json`
- `openkeel/token_saver/hooks/session_start.py`
- `openkeel/token_saver/hooks/pre_tool.py`
- `openkeel/token_saver/hooks/post_tool.py`
- `CLAUDE.md`

### Ladder / broker path

- `openkeel/calcifer/ladder_chat.py`
- `openkeel/calcifer/broker_gui_adapter.py`
- `openkeel/calcifer/broker.py`
- `openkeel/calcifer/opus_planning_agent.py`
- `openkeel/calcifer/opus_judgment_agent.py`
- `openkeel/calcifer/governor.py`

## 4. Best Guesses

### Guess 1: Context bloat is the main cause

This is the strongest hypothesis.

Why:

- `429` requests are happening on turns with very large `body_chars`
- message counts are high
- tool arrays and system prompt content are large
- the proxy traces show huge request bodies even on non-Opus turns

Translation:

- Claude is not being rate limited because the user typed a lot
- Claude is being rate limited because each premium request is dragging a lot of accumulated session state

### Guess 2: The fallback Sonnet path is catching too many large-context turns

The router currently defaults to Sonnet when the turn is not tiny and does not match a special case.

That is dangerous when the request body is already huge.

A plausible failure mode:

1. session grows large
2. user asks a normal coding question
3. router says `default_sonnet`
4. Sonnet request goes out with 200k+ chars
5. 429

### Guess 3: “Think hard” / “architect” in normal OpenKeel sessions is expensive enough to trip limits by itself

The worst Opus traces line up with hard-rule Opus routing triggered by phrases like:

- `think hard`
- `architect`

If those are issued inside the old general OpenKeel Claude session instead of a separate minimal planner context, they can produce very large Opus requests and huge fresh cache writes.

### Guess 4: Repeated retries after 429 are worsening the issue

There are nearby repeated `429`s in the trace.

That suggests insufficient backoff or repeated manual retries against the same underlying context shape.

### Guess 5: The current Ladder architecture is not the main source of the 429s yet

The ladder/broker work may contribute some calls, but the trace pattern looks more like:

- old OpenKeel Claude sessions
- proxy routing on large accumulated context

than like the new Ladder window by itself.

## 5. Important Conceptual Clarification

Inside a normal Claude Code Opus session:

- tool execution itself is local/shell work
- but the **model loop around those tools is still Opus**

So if Opus is the active model and it keeps seeing tool results, the expensive part is still Opus-context reasoning around those results.

The proxy can:

- reroute whole requests
- trim request bodies
- compress some outputs

But it cannot fully turn “Opus session with tools” into “Opus plans, cheaper models execute” inside stock Claude Code.

That deeper split requires:

- delegation boundaries
- separate execution seats
- or a custom MCP/delegate path

## 6. Concrete Things Another Agent Should Check

1. Measure how often `status=429` occurs by model over the last 24h from `~/.openkeel/proxy_trace.jsonl`.
2. Compute average and p95 `body_chars` for successful vs rate-limited Sonnet turns.
3. Check whether `n_messages` or `body_chars` is the stronger predictor of 429.
4. Inspect whether repeated `429`s are clustered by session / same user turn.
5. Verify how much of request size is coming from:
   - system prompt
   - tools array
   - conversation history
   - tool results
6. Determine whether history eviction is firing often enough in `token_saver_proxy.py`.
7. Check whether Sonnet/Opus routing should be hard-blocked above a request-size threshold.
8. Check whether `CLAUDE.md` or SessionStart injection is causing excessive dynamic cache churn.
9. Verify whether Ladder planner/judge subprocess calls are isolated from the old giant OpenKeel Claude session, or whether users are still doing “think hard” in the old session and bypassing the intended planner path.

## 7. Most Likely Fixes

### Immediate

- Add a hard safety threshold in the proxy:
  - if `body_chars` is above a cap, do not route to Sonnet/Opus unless explicitly forced
- Add backoff / cooldown after `429`
- Avoid `think hard` / `architect` in the old general OpenKeel session

### Near-term

- Route premium planning through a **separate minimal planner context**
- Reduce session bloat with stronger compaction/eviction before premium turns
- Re-enable a better classifier only after it is evaluated in shadow mode

### Longer-term

- Move toward the “Opus talks/plans/delegates, others execute” architecture outside the stock Claude loop

## 8. Suggested Queries / Commands

Inspect latest failures:

```bash
tail -n 80 ~/.openkeel/proxy_trace.jsonl
```

Find all `429`s:

```bash
rg '"status": 429' ~/.openkeel/proxy_trace.jsonl
```

Inspect routing code:

```bash
sed -n '120,360p' ~/openkeel/tools/token_saver_proxy.py
```

Inspect live router config:

```bash
cat ~/.config/systemd/user/token-saver-proxy.service.d/env.conf
```

Inspect hooks:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/'.claude'/'settings.json'
print(json.dumps(json.loads(p.read_text()).get('hooks', {}), indent=2))
PY
```

## 9. Bottom Line

The current best diagnosis is:

- **Claude is being rate limited because premium requests are being made from bloated sessions**
- **the router is still simple enough to send some of those big turns to Sonnet/Opus**
- **the old OpenKeel Claude session and the new Ladder/planner architecture are still coexisting, which makes it easy to trigger expensive premium turns in the wrong context**
