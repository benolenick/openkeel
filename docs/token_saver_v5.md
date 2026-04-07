# Token Saver v5 — Reference

**Status:** shipped 2026-04-07. Additive layer on top of v3/v4/v4.5; does not
replace them. 1,634 LOC, 63 tests passing (27 unit + 36 stress). Disable
globally with `TOKEN_SAVER_V5=0`.

---

## What v5 is

v5 is **not a rewrite**. It's a set of small, tested, surgical interventions
that fix the critical bugs v3/v4 shipped with, plus the two highest-leverage
phase-2 capabilities. Each module is a pure utility that v3's hooks call at
specific hotspots. If v5 is removed, v3/v4 behavior reverts unchanged.

Design principles learned from v4:

1. **Fail-open but VISIBLE.** Every swallowed exception hits
   `~/.openkeel/logs/token_saver_debug.log` as structured JSONL, so failures
   are no longer invisible.
2. **No new engines unless wired into a live hook.** v4 shipped 5 engines
   (`error_distiller`, `hybrid_recall`, `subagent_offload`, `edit_shrinker`,
   `pre_compactor`) that are imported nowhere. v5 adds nothing dead.
3. **Every module has tests.** The token saver had ~222 lines of tests
   before v5 (for `local_edit` only). v5 adds 27 unit tests + 36 stress
   checks covering every new module and every patched hotspot.
4. **Every feature has a config flag** with a safe default.
5. **Never compress structured data with a text LLM.** JSON/HTML/CSV
   outputs are bypassed unconditionally.
6. **Honest accounting over optimistic accounting.** Measurement comes
   before optimization.

---

## Layout

```
openkeel/token_saver_v5/
├── __init__.py              — version + public exports
├── config.py                — centralized env-var config with reload()
├── debug_log.py             — structured JSONL log for swallowed exceptions
├── json_guard.py            — detects structured output to prevent corruption
├── hook_chatter.py          — compact status line formatters (built, not yet wired)
├── localedit_verify.py      — real unified diff + AST parse + auto-rollback
├── error_loop.py            — N-strike error fingerprinting + session memory
├── deferred_context.py      — relevance-gated session-start dump (off by default)
├── billed_tracker.py        — ground-truth billed-token tracker from transcripts
└── tests/
    ├── __init__.py
    └── test_v5_smoke.py     — 27 tests across all modules
```

v5 also writes surgical patches into three v3/v4 files (they live in the
existing modules, not in v5/):

- `openkeel/token_saver/hooks/pre_tool.py` — calls `json_guard` in
  `_run_and_compress`
- `openkeel/token_saver/engines/local_edit.py` — calls `localedit_verify`
  after writing
- `openkeel/token_saver/hooks/post_tool.py` — calls `error_loop.observe` +
  `billed_tracker` via the Stop hook
- `openkeel/token_saver_v4/engines/goal_reader.py` — bypass for code files
- `openkeel/token_saver/hooks/stop.py` — new, calls `billed_tracker.process_stop_hook`

---

## Bugs v5 fixes

| # | Bug | Before | After | Where |
|---|-----|--------|-------|-------|
| 1 | Bash LLM summarizer corrupted JSON / HTML / CSV | Observed ghost duplicates, dropped commas/braces on `curl | jq` output | `pre_tool._run_and_compress` calls `json_guard.looks_structured`; structured passes through unmodified | `json_guard.py` + patch in `pre_tool.py` |
| 2 | 28 `except Exception: pass` sites were invisible passthroughs | Daemon-down / Ollama-down / model-missing all silent | Every swallow writes one JSON line to `~/.openkeel/logs/token_saver_debug.log` with timestamp, site, tool, error class, traceback | `debug_log.py` |
| 3 | LocalEdit returned fake "X lines changed" summaries from the LLM's self-report | `6 lines changed` when editing ~25, etc. | Real unified diff, real line count. AST-parses .py files after write; auto-rollback from `.localedit.bak` on syntax break. | `localedit_verify.py` + patch in `local_edit.py` |
| 4 | v4 `goal_reader` stripped Python code silently at 89% output-to-input ratio | `monitor_cron.py` came back with `HEALTH_CHECKS` entries and `subprocess.run()` calls deleted | Hard bypass on any file with a code extension (`.py .js .go .rs .c .cc .h .sh .yaml .json .html .sql` etc). Returns `code_file_bypass` reason. | patch in `v4/engines/goal_reader.py` |
| 5 | 0-byte `openkeel/token_saver/audit.db` orphan from older architecture | Nothing read or wrote to it; monitor cron's token_saver check pointed at it and perpetually reported DOWN | Deleted the file; added to `.gitignore`; repointed monitor cron to `~/.openkeel/token_ledger.db` (the active ledger) | `.gitignore` + `monitor_cron.py` |
| 6 | `error_loop` false-positives on every successful bash output | 80+ fingerprints per session from successful commands | Gate via `looks_like_failure()` — requires Python exception class, unix error token, or HTTP error code. | `error_loop.py` |
| 7 | `error_loop` state file grew unboundedly | No size cap, only TTL-based eviction | Hard cap of `MAX_ENTRIES = 200` with eviction of oldest `last_seen` entries | `error_loop.py` |

All seven are pinned by tests.

---

## New capabilities

### `debug_log` — structured exception visibility

```python
from openkeel.token_saver_v5.debug_log import note, swallow

try:
    ...
except Exception as e:
    swallow("site_name", tool="Bash", error=e, extra={"cmd": command})

# or for a non-exception observation:
note("json_guard", "bypassed JSON output", tool="Bash")
```

Entries land as JSON lines in `~/.openkeel/logs/token_saver_debug.log`.
Read with `debug_log.tail(n)` from code, or `tail -f` from shell. First
time the token saver has meaningful failure visibility.

### `json_guard` — structured output bypass

Detects JSON (object/array, nested, truncated), NDJSON, HTML, XML, CSV, TSV.
Wired into `pre_tool._run_and_compress` so any structured output passes
through unmodified. Conservative: prose-wrapped JSON is NOT bypassed, so
LLM still handles "Here is the response: {...}" correctly.

### `localedit_verify` — honest diffs + syntax safety

Replaces the LLM's fake "X lines changed" self-report with a real unified
diff. For `.py` files, AST-parses the result after write and rolls back
from `.localedit.bak` if syntax is broken. Catches bad edits from the
small 3B model before they propagate.

```python
from openkeel.token_saver_v5.localedit_verify import verify_edit

result = verify_edit(path, old_content, new_content,
                     backup_path=f"{path}.localedit.bak",
                     require_py_valid=True)
if not result.ok:
    # result.rolled_back == True if we restored from backup
    print(f"rejected: {result.reason}")
```

### `error_loop` — multiplicative savings on retries

When the agent hits the same class of error 3+ times, returns a nudge
string the next PreToolUse can surface. This is the only optimization
that compounds across turns — saves iterations, not per-call volume.

Fingerprinting is noise-resistant: `/tmp/abc123.txt` and `/tmp/xyz999.txt`
collapse to the same fingerprint, but `ModuleNotFoundError` and
`FileNotFoundError` stay distinct.

Gated by `looks_like_failure()` — only fires on real error output
(Python exception classes, unix error tokens, HTTP error codes). State is
capped at 200 entries and evicted after 6 hours.

### `deferred_context` — relevance-gated session start [**OFF by default**]

Captures the full static SessionStart dump (project map, recent work,
infrastructure notes, commits) and defers emission. When the user sends
their first message, a cheap TF-style scorer picks the top 3 most
relevant blocks and emits ONLY those. Irrelevant blocks are dropped.

**Flip with `TOKEN_SAVER_V5_DEFERRED=1`.**

This is the single biggest unused lever in the system — the static dump
costs ~5-10K tokens per session × 500+ sessions. Deferred emission on
relevant queries can cut ~3-8K tokens from each session start.

**Why it's off:** the scorer can silently mis-rank. A deliberate rollout
should measure baseline session-start overhead from `debug_log` first,
then flip the flag and compare.

### `billed_tracker` — ground-truth token accounting

The `savings` table measures tool-output volume reduction (48% as of
2026-04-07) but only covers ~5-10% of what Claude actually bills. A typical
turn looks like:

| Component | Est tokens/turn | `savings` sees it? |
|---|---|---|
| System prompt + tool defs | ~6-10K | No |
| Cached conversation history | 20-100K+ | No |
| User message | 50-1K | No |
| **Tool results this turn** | **5-20K** | **Yes (THIS is what's tracked)** |
| Claude's output | 2-8K | No |
| Hook chatter itself | .5-2K | No (and token saver ADDS these) |

`billed_tracker` reads Claude Code's transcript `.jsonl` files and
records the EXACT token usage each assistant turn billed, via the
`usage` field the API returns on every response. Populates a new
`billed_tokens` table in the existing `~/.openkeel/token_ledger.db`
sibling to `savings` (never touches existing data).

**Backfilled 116,704 historical turns** (1,522 transcript files, 1,486
sessions, 935 elapsed hours) at ship time.

Live reporting:

```bash
python3 -m openkeel.token_saver_v5.billed_tracker report      # lifetime
python3 -m openkeel.token_saver_v5.billed_tracker report 15   # last 15h
python3 -m openkeel.token_saver_v5.billed_tracker report 1    # last hour
python3 -m openkeel.token_saver_v5.billed_tracker backfill    # re-scan idempotent
```

Live updates via `hooks/stop.py` registered in `~/.claude/settings.json`
as a Stop hook — fires every time Claude finishes a response.

---

## Configuration

All flags in `config.py`, overridable via env vars:

| Env var | Default | Effect |
|---|---|---|
| `TOKEN_SAVER_V5` | `1` | Master switch for v5 modules |
| `TOKEN_SAVER_V5_ERRORLOOP` | `1` | Enable error-loop nudges |
| `TOKEN_SAVER_V5_TERSE` | `1` | Use hook_chatter formatters (not yet wired) |
| `TOKEN_SAVER_V5_DEFERRED` | `0` | Gate SessionStart dump through scorer |
| `TOKEN_SAVER_DAEMON` | `http://127.0.0.1:11450` | Daemon URL |
| `TOKEN_SAVER_OLLAMA` | `http://127.0.0.1:11434` | Local Ollama |
| `TOKEN_SAVER_JAGG_OLLAMA` | `http://192.168.0.224:11434` | Remote GPU Ollama |
| `TOKEN_SAVER_FAST_MODEL` | `qwen2.5:3b` | Hot-path model |
| `TOKEN_SAVER_ESCALATION_MODEL` | `gemma2:27b` | Escalation model |
| `TOKEN_SAVER_BASH_MIN` | `2000` | Min chars for bash compression |
| `TOKEN_SAVER_SKELETON_MIN` | `4000` | Min chars for semantic skeleton |
| `TOKEN_SAVER_GOAL_MIN` | `4000` | Min chars for goal-reader |
| `TOKEN_SAVER_LEDGER` | `~/.openkeel/token_ledger.db` | Ledger DB path |
| `TOKEN_SAVER_DEBUG_LOG` | `~/.openkeel/logs/token_saver_debug.log` | Debug log path |
| `TOKEN_SAVER_DEFERRED_CACHE` | `~/.openkeel/cache/deferred_context.json` | Deferred dump cache |
| `TOKEN_SAVER_ERROR_STATE` | `~/.openkeel/cache/error_loop_state.json` | Error loop state |

After mutating env vars in tests, call `from openkeel.token_saver_v5.config
import reload; reload()` to re-read.

---

## Running the tests

```bash
# 27 unit tests
python3 -m unittest openkeel.token_saver_v5.tests.test_v5_smoke -v

# 36-check stress test (400 JSON samples, concurrent debug_log writes, etc)
python3 /tmp/stress_v5.py
```

Neither touches your real state — tests isolate via `TOKEN_SAVER_DEBUG_LOG`,
`TOKEN_SAVER_DEFERRED_CACHE`, `TOKEN_SAVER_ERROR_STATE` env overrides to
a temp directory.

---

## What is wired vs dormant

### Wired and live

| Feature | Status |
|---|---|
| `json_guard` in `pre_tool._run_and_compress` | Live |
| `localedit_verify` in `engines/local_edit.py:apply_edit` | Live |
| `error_loop.observe` in `post_tool.handle_bash` | Live |
| `goal_reader` code-file bypass in `v4/engines/goal_reader.py` | Live |
| `debug_log` across v5 | Live |
| `billed_tracker` schema + backfill + Stop hook | Live (fires next session) |
| `audit.db` orphan deleted + .gitignored | Done |
| Monitor cron token_saver check repointed | Done |

### Dormant (built, tested, not yet wired)

| Feature | Why dormant | How to wire |
|---|---|---|
| `hook_chatter` terse formatters | Verbose v3/v4 messages still emitted by ~6 call sites. Cosmetic change, requires careful replacement across `pre_tool.py`, `local_edit.py` to avoid breaking downstream parsers. | Replace f-string message construction with `hook_chatter.edit_applied(...)` / `bash_compressed(...)` etc. Check all callers. |
| `deferred_context.capture` / `score_and_emit` | Scorer can silently mis-rank. Needs baseline measurement from `debug_log` + deliberate rollout. | (1) Modify `session_start.py` to call `capture()` in addition to emitting, (2) add a `UserPromptSubmit` hook that calls `score_and_emit()`, (3) set `TOKEN_SAVER_V5_DEFERRED=1`. |

### Deliberately not yet built

| Feature | Why |
|---|---|
| `inferred_goal` (pre-Read intent inference from recent context) | Needs conversation-history access; complex; defer to v5.1 |
| Grep/Glob result clustering | Low leverage, can add as a small engine later |
| Multi-file API-surface extraction | Just expand `semantic_skeleton` triggers |
| Image/PDF captioning | Needs local vision model |
| v3 config migration to `config.py` | Risky; v5 owns its config, v3 still has hardcoded strings |
| v4 dead-engine deletion | Leaves the audit findable for reference |

---

## Known limitations

1. **Tool outputs that are mostly prose with embedded JSON** (e.g., "Here is
   the data: {...}") are NOT bypassed — `json_guard` is conservative and
   only matches stdin that starts with `{` or `[`. Some structured content
   inside prose will still get compressed. Acceptable trade-off.

2. **`error_loop` nudges are injected via a daemon queue** (`/nudge/queue`
   POST) — the daemon endpoint is NOT yet implemented. Observations are
   recorded to the state file correctly and return nudge strings, but
   there's no automatic surface-up path yet. The state file itself is
   grepable if the agent gets stuck.

3. **`localedit_verify` is called from `engines/local_edit.py:apply_edit`,
   but the parallel "LocalEdit edit shortcut" path in `pre_tool.py` that
   emits the `[TOKEN SAVER ✓ EDIT APPLIED]` messages is a SEPARATE code
   path.** That shortcut still uses an LLM to apply edits without real
   verification. Future work: route the shortcut through `apply_edit` too.

4. **`billed_tracker` Stop hook fires asynchronously** (`async: true`) so it
   doesn't slow down session end. This means `report 0` immediately after
   a session may miss the most recent turn — wait a few seconds.

5. **`deferred_context`'s scorer is TF-overlap, not semantic.** A question
   about "the red button" won't match a block mentioning "that crimson
   trigger". Semantic embedding would help; kept dumb for speed.

---

## Measuring real impact

After a few days of live use, compare:

```bash
# Tool-output reduction (what the savings table measures)
sqlite3 ~/.openkeel/token_ledger.db \
  "SELECT ROUND(100.0*SUM(saved_chars)/SUM(original_chars),1)||'%'
   FROM savings WHERE timestamp > strftime('%s','now','-7 days')"

# Ground-truth billed tokens (what Anthropic actually charges)
python3 -m openkeel.token_saver_v5.billed_tracker report 168  # last week

# Rate comparison: tokens/hour per day to see trend
sqlite3 ~/.openkeel/token_ledger.db \
  "SELECT date(timestamp,'unixepoch','localtime') as day,
          COUNT(*) as turns,
          SUM(total_billed) as billed,
          ROUND(SUM(total_billed)/(MAX(timestamp)-MIN(timestamp)+1)*3600, 0) as tok_per_hr
   FROM billed_tokens
   WHERE timestamp > strftime('%s','now','-14 days')
   GROUP BY day ORDER BY day DESC"
```

The `savings` % will remain near 48% — that's the interception layer's
per-event rate and is honest for its layer. The `billed_tracker` number
is what actually corresponds to your weekly quota bar.

**If you flip `TOKEN_SAVER_V5_DEFERRED=1`**, expect a ~5-10% reduction in
the `billed_tracker` per-session rate on session-start-dominated sessions
(short conversations, many new sessions). On long sustained sessions the
delta is smaller because the deferred dump is a one-time cost per session.

---

## History

| Commit | Summary |
|---|---|
| `3497155` | Token Saver v5: surgical fixes + new visibility/safety layer (pre_tool json_guard, local_edit verify, post_tool error_loop, goal_reader code bypass, delete audit.db, 24 tests) |
| `0b51f71` | [parallel agent] Token Saver: close file_read leak + wire v4.5 engines live (read_log.py, binary guard, persistent re-read cache) |
| *(this commit)* | v5 round 2: billed_tracker + Stop hook + 116K-turn backfill; error_loop failure-gate + 200-entry cap + 3 new tests; .gitignore for audit.db orphan; this documentation |

Test totals: 27 unit + 36 stress = **63 checks passing**.

---

## Moving on

v5 is the closing chapter on the token saver for now. The honest picture
is:

- **Tool-output interception** (phase 1: v1–v4) is mature at ~48% real
  per-event savings. Diminishing returns on adding more engines.
- **Correctness fixes** (v5 phase 1) close the gaps that were silently
  degrading effective savings via retries and corruption.
- **Visibility** (`debug_log`, `billed_tracker`) turns the token saver
  from a black box into a measurable system. This is prerequisite for
  any future optimization — you can't beat what you can't measure.
- **`deferred_context`** is the last big lever and it's ready, just off.

Flip deferred_context when you're ready to measure its impact. Don't
build v6 until `billed_tracker` shows a trend you want to change.
