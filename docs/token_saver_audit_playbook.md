# Token Saver — Audit Playbook

**Purpose:** When someone says "is the token saver actually working?" or "where are my tokens going?", read this FIRST. It captures everything learned during the 2026-04-07 audit so future agents stop re-discovering the same confusions.

---

## The mental model (read this before anything else)

### Where tokens actually go

Every turn, Claude Code sends the **entire conversation history** to `api.anthropic.com`. Not just your latest message. The full stack. Every turn. This is non-negotiable — it's how the API works.

```
Turn N request = [system prompt + tool defs]
               + [CLAUDE.md + session-start hook output]
               + [every prior user msg]
               + [every prior assistant msg]
               + [every prior tool_use + tool_result]
               + [your new message]
```

Anthropic caches the stable prefix (prompt caching). Cached re-reads are charged at **10% price** (`cache_read`). Newly cached content is **125% price**, once (`cache_creation`). Uncached content is **100%** (`input`). Output is **500%** (`output_tokens`).

### The counterintuitive part

Because cache_read is so cheap, people assume it's "free." **It isn't.** Check `~/.openkeel/token_ledger.db`:

```bash
sqlite3 ~/.openkeel/token_ledger.db "
SELECT 'input', sum(input_tokens) FROM billed_tokens
UNION ALL SELECT 'cache_creation', sum(cache_creation) FROM billed_tokens
UNION ALL SELECT 'cache_read', sum(cache_read) FROM billed_tokens
UNION ALL SELECT 'output', sum(output_tokens) FROM billed_tokens;
"
```

As of 2026-04-07, lifetime totals were:
- input: 1.8M
- cache_creation: 313M
- **cache_read: 18.1B** ← dominates the bill
- output: 30M

Normalize to cost (multiply by price ratio):
| Bucket | × price | cost units |
|---|---|---|
| cache_read | × 0.1 | **1811** (~70%) |
| cache_creation | × 1.25 | 391 |
| output | × 5 | 150 |
| input | × 1 | 1.8 |

**Takeaway:** your bill is dominated by cache_read of accumulated history. The "41% context used" you see in the UI is literally the stack that gets re-sent every turn at cache_read rates.

### What's actually in that stack (ranked)

1. **Tool results** (50-80%) — outputs of Read, Bash, Grep. The biggest leak.
2. **Assistant replies** (10-20%) — Claude's code blocks and explanations.
3. **System prompt + tool defs** (~12K, fixed) — cached, untouchable.
4. **CLAUDE.md + memory index** (5-10K) — shipped per session, editable.
5. **Session-start hook output** (~5-10K) — project map, Hyphae recall. Editable via hooks config.
6. **User messages** — usually small.

---

## The THREE levers for real savings (everything else is noise)

1. **Reduce what *enters* history.** PreToolUse hooks that block large outputs and return compressed versions. The existing token saver attacks this. Ceiling: modest.
2. **Remove things *from* history.** `/compact`, fresh sessions, or a proxy that rewrites old tool_results before the API sees them. **Biggest lever.** Ceiling: 50-70%.
3. **Shrink *fixed* per-turn injections.** CLAUDE.md, memory index, session-start hooks. Every KB here multiplies across every turn.

If you are designing a fix and it doesn't map to one of these three, it probably isn't saving tokens.

---

## Where hooks CAN and CANNOT help

| Spot | Can modify what Claude sees? | Real savings? |
|---|---|---|
| `PreToolUse` (before tool runs) | **YES** — can block/rewrite the call | Yes, this works |
| `PostToolUse` (after tool runs) | **NO** — Claude already saw the result | **Zero**. Only useful for logging/telemetry/pre-caching |
| `UserPromptSubmit` | YES, can inject additionalContext | *Adds* tokens, doesn't save |
| `SessionStart` | YES, can inject additionalContext | *Adds* tokens, doesn't save |
| Nothing (conversation history) | **NO** | Hooks are blind to history. Needs a proxy. |

**Critical:** any "savings" recorded from a `PostToolUse` hook are **structurally incapable of reducing Claude's context**. They are at best "compressibility observed after the fact" — useful for prioritizing which PreToolUse engines to build next, but never real.

---

## Known accounting bugs in the existing saver (as of 2026-04-07)

### Bug 1: `edit_trim` formula is inflated
**File:** `openkeel/token_saver/hooks/pre_tool.py:1416`
```python
saved = len(old_string) + len(new_string) + len(content)  # ← wrong
```
- `len(content)` counts the whole file as "what Edit would have cost", but Edit returns a diff snippet (a few hundred bytes), not the file.
- `len(old_string) + len(new_string)` were already paid as Claude's **output** tokens when it generated the tool call. You cannot refund output tokens by intercepting the tool call.
- **Real inflation: 3-10×.** (Initial audit claimed 50-75× but that was over-indexed on hook-return-size vs. downstream context cost.)
- Accounts for ~4M of the claimed 9.4M "lifetime savings".

**Honest replacement:**
```python
saved = min(len(content), 2000 + 2 * len(old_string)) - len(hook_response)
```

### Bug 2: `post_tool.py` writes phantom `saved_chars`
**File:** `openkeel/token_saver/hooks/post_tool.py:184` (`handle_bash`)
- The hook's own docstring (line 13) admits: *"PostToolUse hooks CANNOT modify tool output — Claude already saw it."*
- Yet `handle_bash` still writes `saved_chars` from `compress_output(...)` to the ledger.
- **Every `bash_output` event's `saved_chars` is fiction.** ~245K tokens of phantom savings.

### Bug 3: session-ID mismatch between ledger and billed_tokens
**Files:** `openkeel/token_saver/ledger.py:38`, `billed_tracker` module
- `savings` table uses 12-char hex IDs, intentionally process-scoped (one per hook process lifetime).
- `billed_tokens` table uses real Claude Code session UUIDs.
- **The tables cannot be joined.** "50% lifetime savings" is computed entirely inside the savings table and never reconciled against real billing.
- **Mitigation already built-in:** `ledger.py:38` reads `TOKEN_SAVER_SESSION` env var. Set it to the Claude session UUID from hook stdin and the tables can join.

### Non-bug: the CLI report IS honest
**File:** `openkeel/token_saver/report.py:99-104`
```
ACTUAL SAVINGS (pre-tool interceptions that reduced context):
  Tokens saved by blocking/rewriting: ...
TRACKING ONLY (measured but not intercepted — PostToolUse can't modify output):
  Tokens that could be saved with pre-tool filters: ...
```
The CLI report already splits actual from tracking-only. The misleading "50% lifetime" headline comes from **`dashboard.py`** (the tkinter/web UI), which sums both columns. Fix is in the display layer, not the ledger.

**However** — the CLI's `actual_types` whitelist is too narrow (only `cache_hit`, `command_rewrite`, `bash_compress`). It excludes other legitimate pre-tool interceptions like `large_file_compress`, `recall_rerank`, `local_edit`, `diff_compress`, `write_trim`, `v4_semantic_skeleton`. Those should be added.

---

## Things previous agents got wrong (don't repeat)

1. **"`file_read` has zero compression"** — FALSE. `pre_tool.py:386` has `handle_read` which fires `cache_hit`, `v4_semantic_skeleton`, `goal_filter`, `large_file_compress`, `recall_rerank`. The ledger's `file_read` rows with 0 saved are just raw-read logging; actual savings are recorded under different event_types on the *next* read.
2. **"edit_trim inflated 50-75×"** — overstated. Real inflation is ~3-10×. The initial test conflated hook-return-size with downstream context cost.
3. **"post_tool is useless"** — WRONG. It does legitimate work: predictive pre-caching, v5 error-loop detection, Hyphae file skeletons, read-log for cross-session deduping. Just don't trust its `saved_chars` numbers.
4. **"session IDs are a bug"** — they're intentionally process-scoped (documented at `ledger.py:38`). The bug is labeling them "lifetime savings" in the dashboard.
5. **"We should compress file_edit"** — misdiagnosis. The 0-saved rows under `file_edit` are just logging of the raw event. The real gap is that `edit_trim`'s gate is too high (>6KB OR >150 chars), so small edits bypass it.

---

## How to audit a "savings" claim honestly

### Step 1: check the billed_tokens table (ground truth)
Never trust the dashboard's aggregated claim. Always check against Claude Code's actual reported usage:

```bash
sqlite3 ~/.openkeel/token_ledger.db "
SELECT timestamp, session_id, input_tokens, cache_creation, cache_read, output_tokens
FROM billed_tokens
ORDER BY timestamp DESC LIMIT 20;"
```

### Step 2: check the savings table grouped by event_type
```bash
sqlite3 ~/.openkeel/token_ledger.db "
SELECT event_type, count(*), sum(original_chars)/4 as orig_tok, sum(saved_chars)/4 as saved_tok
FROM savings GROUP BY event_type ORDER BY 4 desc;"
```

### Step 3: know which event_types are real vs. phantom vs. measurement
| Event type | Source | Real? |
|---|---|---|
| `cache_hit`, `bash_compress`, `diff_compress`, `edit_trim` (but inflated), `grep_compress`, `glob_compress`, `large_file_compress`, `local_edit`, `recall_rerank`, `v4_semantic_skeleton`, `write_trim`, `webfetch_compress` | `pre_tool.py` | **Real** (blocks + rewrites) |
| `bash_output`, `grep_output`, `glob_output`, `file_read`, `file_edit`, `file_write`, `agent_spawn`, `session_start`, `session_reattach`, `predictive_warm` | `post_tool.py` or startup | **Phantom or logging only**. saved_chars is not real savings. |
| `conversation_compress`, `subagent_compress` | varies | Check the specific recorder |

### Step 4: verify a specific event with a direct hook test
The ONLY unambiguous test: run the hook as a subprocess with a known input, measure its stdout (what Claude actually sees), compare to what Claude would see without the hook.

```python
import json, subprocess
payload = {"tool_name": "Edit", "tool_input": {...}}
p = subprocess.run(
    ["python3", "openkeel/token_saver/hooks/pre_tool.py"],
    input=json.dumps(payload), capture_output=True, text=True,
)
print("bytes Claude receives:", len(p.stdout))
```

If the hook returns `{"decision": "block", "reason": "..."}`, Claude sees `reason`. That's B. To measure A (baseline), invoke the tool without the hook — for tools where you can't bypass the hook in-session, look up Claude Code's actual tool response format.

### Step 5: compute real savings ratio
```python
# Real savings, for a specific time window
real_saved = (A_bytes - B_bytes) // 4  # chars to tokens
real_billed = sum(input + cache_creation + output) from billed_tokens
savings_ratio = real_saved / (real_saved + real_billed)
```
If `savings_ratio > 0.5`, **you're doing something wrong**. That's never true in practice.

---

## Common confusions (cheat sheet)

| Question | Quick answer |
|---|---|
| "Why is cache_read so huge?" | Every turn resends full history. Anthropic caches the prefix. Charged at 10% but volume is enormous. |
| "Why is the context at 41% but bill isn't scary?" | Because cache_read is 10% price. But it still dominates the bill. |
| "Are PostToolUse hooks saving tokens?" | **No.** Claude already saw the tool result. They can log/pre-cache but never reduce context. |
| "Can I edit the conversation history?" | Not mid-session (in memory). On `--resume` yes, via JSONL files. Server-side Anthropic cache: never. |
| "Does Claude Code send the whole conversation each turn?" | Yes. That's how the API works. Cache just makes it cheaper to re-send the prefix. |
| "Is the `savings` table ground truth?" | No. `billed_tokens` is. Always cross-check. |
| "Are session IDs in `savings` a bug?" | No, intentionally process-scoped. But don't call it "lifetime savings" in the dashboard. |
| "Should I build an LLM-based compressor for file reads?" | Already exists in `pre_tool.py`. Don't duplicate. |
| "What's the single biggest lever?" | `/compact` and the proxy. Both attack accumulated history. |

---

## Architecture: the three storage layers

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Anthropic server-side prompt cache                       │
│    - ~5 min TTL, keyed by prefix hash                       │
│    - NOT editable                                           │
│    - Drives cache_read billing                              │
└─────────────────────────────────────────────────────────────┘
                            ↑
                   HTTPS /v1/messages
                            ↑
┌─────────────────────────────────────────────────────────────┐
│ 2. Claude Code in-memory conversation (JS/Node process)     │
│    - Built from JSONL on startup/resume                     │
│    - Mutated per turn                                       │
│    - Hooks CANNOT see or modify this                        │
│    - ONLY a proxy (ANTHROPIC_BASE_URL) can intercept        │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│ 3. ~/.claude/projects/<project>/<session>.jsonl             │
│    - Source of truth on `--resume`                          │
│    - Editable — but only takes effect on resume             │
│    - Good target for a "transcript trimmer" tool            │
└─────────────────────────────────────────────────────────────┘
```

---

## Token saver hook dataflow (current)

```
User types message
    │
    ▼
SessionStart/UserPromptSubmit hooks → inject context (adds tokens)
    │
    ▼
Claude Code builds request → sends to API
    │
    ▼
Claude responds with tool_use(s)
    │
    ▼
For each tool:
    ├─ PreToolUse hook (pre_tool.py)
    │     ├─ block & rewrite? → real savings
    │     └─ allow → tool runs
    │
    ├─ Tool executes
    │
    └─ PostToolUse hook (post_tool.py)
          ├─ log metrics (NOT savings)
          ├─ pre-cache next reads
          └─ v5 error loop detection
    │
    ▼
Tool result added to conversation (FOREVER, until /compact)
    │
    ▼
Next turn → everything above is re-sent to API at cache_read rates
```

---

## The proxy approach (the real fix)

Since hooks cannot touch conversation history and server cache is untouchable, the only way to attack the 70% cost bucket is to sit between Claude Code and the API:

```
Claude Code → http://127.0.0.1:9000 → api.anthropic.com
                     ↑
              Token saver proxy
              - walks messages[]
              - rewrites old tool_result blocks
              - forwards to real API
```

See `tools/token_saver_proxy.py` for the implementation. Launch with:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9000
claude
```

The proxy is the **only** mechanism that can:
- See the full conversation
- Rewrite old tool_results
- Measure real pre/post token counts per request
- A/B test savings by toggling the env var

Everything else — hook-level engines, dashboard fixes, session ID threading — is worthwhile but modest compared to what the proxy unlocks.

---

## Glossary

- **cache_read**: re-reading content already in Anthropic's prompt cache. 10% price. Dominates the bill because of volume.
- **cache_creation**: adding new content to the cache. 125% price, once.
- **input**: uncached input. 100% price. Small for well-cached sessions.
- **output**: Claude's response tokens. 500% price.
- **conversation history**: the full stack of messages sent on every turn. Grows monotonically until `/compact` or new session.
- **context window**: the 200K-token limit. Full == forced compact or fail.
- **tool_result**: a `user` role message block containing the output of a tool call. These are the biggest filler in history.
- **PreToolUse hook**: fires BEFORE a tool runs. Can block/rewrite. **Only** place with real-savings leverage at the hook level.
- **PostToolUse hook**: fires AFTER a tool runs. Cannot modify what Claude saw. Useful for logging only.
- **Ledger**: `~/.openkeel/token_ledger.db`. Two tables: `savings` (hook self-reports) and `billed_tokens` (real Anthropic usage).
- **Proxy**: local HTTP server intercepting `/v1/messages`. The only way to rewrite conversation history.

---

*Last updated: 2026-04-07. If you're reading this during a "where did my tokens go" investigation and something here is wrong, update it. Don't re-discover the same thing.*
