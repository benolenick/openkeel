# Token Saver — 2026-04-07 session notes

Session focused on diagnosing runaway token use, shipping three targeted
fixes, and scoping the next round of work on history compaction.

---

## Diagnosis

The visible "heavy token use" wasn't coming from any one tool call. It was
driven by three compounding problems:

1. **SessionStart hook had amnesia.** The hook generated a fresh random UUID
   on every invocation, so it had no way to know it had already briefed the
   current Claude session. Every re-trigger re-dumped the full Hyphae
   briefing + 166-file ranked project map into the context window
   (~4–8K tokens per fire). Fired 4× in 10 minutes in the observed window.
2. **`prefill_ranked_map` was logged as pure spend.** The ranked map is a
   replacement for a much larger implicit baseline, but the ledger recorded
   `saved_chars=0`, making it look like unmitigated cost on the dashboard.
3. **`edit_trim` success messages looked like failures.** When the hook
   trimmed a large Edit and wrote the file itself, it returned
   `{"decision": "block", "reason": "...updated successfully..."}`. Claude
   Code's harness wraps any `block` reason in a red
   "PreToolUse:Edit hook blocking error" banner, so a successful trim was
   indistinguishable from a genuine failure. Risk: unnecessary retries,
   double-edits, or panic rollbacks.

Also observed but not the main cost driver:

- **Two concurrent Claude sessions** running on this machine (pts/0 and
  pts/1). Each re-ships its full transcript.jsonl to the API on every turn.
  Both sessions had been running 6+ hours and had 350–450KB of history each.
  That's the dominant cost once sessions get long, and token saver cannot
  reach it with its current architecture (see Phase 1/2 scope below).
- **File writes are logged with `saved_chars=0` by design** — there's no way
  to trim a file write without corrupting the file.
- **WebFetch / WebSearch / Task results have no interceptors at all.**

---

## Shipped fixes

All three merged into `main` as fast-forward of branch
`fix/session-start-dedup`.

### 1. `37aebfe` — SessionStart dedup via Claude session_id
`openkeel/token_saver/hooks/session_start.py`

- Read Claude Code's real `session_id` from stdin JSON payload.
- First time we see a session id → drop a marker at
  `~/.openkeel/token_saver_sessions/<id>` and run the full briefing.
- Subsequent invocations with the same id → print one line
  (`[TOKEN SAVER] Warm reattach (session xxxxxxxx) — briefing already
  injected.`) and exit.
- Same id is exported as `TOKEN_SAVER_SESSION` so the ledger's
  `COUNT(DISTINCT session_id)` stops inflating.
- Markers auto-expire after 6h. Fallback to `hash(cwd + date)` if stdin is
  empty (manual tests, older harness).
- **Tested live** by piping the same fake session payload twice: first run
  emitted the full briefing, second run emitted only the warm reattach line.

### 2. `3d79307` — Credit `prefill_ranked_map` with actual savings
`openkeel/token_saver/engines/context_prefill.py`

- Estimate a full-dump baseline as `file_count * 120 chars/file`.
- Log `original_chars = full_dump_estimate` and
  `saved_chars = max(0, full_dump_estimate - len(ranked_map))`.
- Purely an accounting fix — no behavior change to the map builder.

### 3. `016a4dc` — Clarify `edit_trim` success message
`openkeel/token_saver/hooks/pre_tool.py`

- Rewrote the `reason` string returned by `_try_edit_trim`. The block/allow
  contract is harness-level and can't be changed from inside a hook, but the
  text inside the "error" banner can be unambiguous.
- New message leads with `[TOKEN SAVER ✓ EDIT APPLIED]`, explicitly tells
  Claude not to retry, and says "Treat this as SUCCESS, not an error."
- Zero behavior change — file-writing logic untouched.

### What I deliberately did NOT change
- **Historical lifetime session count.** Self-heals going forward now that
  all new records share the real Claude session id. Not worth mutating
  history.
- **The ~15 other `decision:block` call sites** in `pre_tool.py`
  (bash_compress, large_file_compress, etc.). Those are obviously
  compressed-content returns and rarely misread. Candidate for a follow-up
  tone sweep if desired.

---

## Phase 1/2 scope — ConversationCompressor v2

### Hard constraint
Token saver **cannot rewrite past `tool_result` blocks**. That data lives in
Claude Code's transcript jsonl and gets re-sent to the API on every turn.
Hooks run *around* tool calls, not inside the API client. The only thing
that actually rewrites history is Claude Code's built-in `/compact` command.

Also: the current `ConversationCompressor` engine is a dead-end sidecar. It
builds a rolling summary of each turn, writes it to
`~/.openkeel/token_saver_conversation_summary.json`, and nothing ever reads
it. It saves tokens of its own log, not of the real conversation.

### Phase 1 — high leverage, achievable (~1 day)

**P1. Auto-compact suggester** ⭐ biggest lever
- PostToolUse hook watches live transcript size at
  `~/.claude/projects/<proj>/<session>.jsonl`.
- At 150KB → soft nudge via `additionalContext`: "Context is heavy.
  Consider `/compact` when you finish the current subtask."
- At 300KB → hard nudge with a specific `/compact` suggestion focused on
  the current work area.
- **Open question** to verify before building: can a hook *invoke* a slash
  command, or only suggest via message injection?

**P2. Resurrect the existing ConversationCompressor output**
- Inject the rolling summary into both the fresh-session and warm-reattach
  paths in `session_start.py`.
- Project-scoped + TTL to avoid stale cross-topic leakage.

**P3. WebFetch / WebSearch interceptors**
- Neither tool has an interceptor today. Web results routinely dump 5–20K
  tokens into the transcript, which then re-ships every turn forever.
- New PreToolUse interceptor routes output through the local LLM summarizer
  (same pattern as `bash_llm_summarize`).
- Keep URL + title + first paragraph verbatim; summarize the rest.
- **Expected savings: 20–40% on research-heavy sessions.**

**P4. Subagent/Task result interceptor**
- Subagent returns can be 10–30K tokens. Same summarizer pattern as P3.
- Lower risk — subagent outputs are already distilled by definition.

### Phase 2 — medium leverage, tune after measuring (~0.5 day)

**P5. Lower PreToolUse trim thresholds**
- `edit_trim` old_string 150 → 80 chars, file 6KB → 3KB.
- A/B toggle in ledger before/after. ~5–15% additional savings.

**P6. Context hygiene nudges** (duplicate reads, redundant `ls -la`, etc.)
via `additionalContext`. Pure suggestion, no enforcement.

**P7. Per-session token budget daemon**
- Real-time per-session tally (now possible thanks to the dedup fix).
- Surface runaway sessions in the dashboard before they become expensive.

### Explicitly NOT in scope
- **Rewriting `transcript.jsonl` directly.** Would corrupt Claude Code's
  session state.
- **Proxying the Anthropic API.** Claude Code's HTTP client isn't routed
  through a user proxy.
- **Replacing Claude's Edit/Read tools wholesale.** Too invasive, low upside
  over the current interceptors.

### Success metric
- Baseline: **44.6%** lifetime savings.
- Target after Phase 1: **60–65%**, with the biggest gains on sessions >3h
  where history re-send dominates.

### Unknowns to resolve before committing
1. Can a hook *invoke* a slash command, or only suggest it?
2. `additionalContext` size limits and rendering behavior.
3. Real-world availability of the local LLM summarizer (ollama on kaloth's
   RTX 3090) — need a fallback to head/tail truncation if it's ever down.

### Recommended next step
**Spike P1 in ~30 min** — verify whether a hook can force `/compact` and
measure transcript size growth on a live session. That single answer
determines whether Phase 1 is a ~1-day build or a ~2-hour build.

---

## Phase 3 — LLM Leverage Expansion (added 2026-04-07 late-night)

Investigation found that the local LLM was vestigial: 20 LocalEdit calls in
497 sessions, summarizer hardcoded to kaloth's 3070 instead of jagg's 3090.
After pointing summarizer.py at jagg (qwen2.5:3b @ ~200 tok/s, 0.6s/call)
and calibrating, the local LLM can now genuinely carry load. This phase
builds the engines that turn the LLM from vestigial into load-bearing.

Features are ranked by untapped reservoir size (real ledger data).

### A. Edit `old_string` anchor shrinker  ⭐ highest impact
- **Reservoir:** `file_edit` 444 calls, 26M chars, only 11.2M saved by rule-
  based edit_trim. ~14.8M chars remaining = the single biggest uncovered
  reservoir in the whole ledger.
- **Mechanism:** Before an Edit fires, send the proposed old_string + the
  target file to qwen2.5:3b with "return the shortest unique substring that
  will still match." Verify exactness via grep, fall back on failure.
- **Projected:** +5–8pp lifetime savings.
- **Risk:** Low — every shrink is verified before use. If the shrunk
  anchor isn't unique or isn't present, original old_string is kept.

### G. First-read semantic skeleton  ⭐
- **Reservoir:** `file_read` 856 calls, 4.0M chars, 0% current savings.
- **Mechanism:** On first read of any file >400 lines, generate an LLM
  skeleton (classes, functions, imports, key constants with line refs) and
  return that in place of the raw file. Cache the original keyed by mtime
  so subsequent targeted reads can be served from cache.
- **Projected:** +3–5pp lifetime savings.

### E. Grep semantic clustering  ⭐
- **Reservoir:** `grep_output` 235 calls, 186K chars, 0% saved.
- **Mechanism:** When a grep returns 30+ matches, send to LLM with "group
  these into 3–5 semantic categories, pick one representative per group,
  list other files by count."
- **Projected:** +1–2pp lifetime savings, compounds with v4 rule pruner.

### C. Conversation compressor rewrite
- **Reservoir:** current conv_compress at 52% efficiency, 236 calls, 131K
  saved. ~60-70% leakage.
- **Mechanism:** Replace rule-based dedupe with real LLM summarization of
  10-turn blocks older than N minutes. Preserve code blocks, file paths,
  function names verbatim.
- **Projected:** 2–3x current engine output.

### I. Task/Agent result interceptor
- **Reservoir:** `agent_spawn` 59 calls, 413K chars, 0% saved.
- **Mechanism:** Summarize subagent returns through qwen2.5:3b before they
  hit the main context window.
- **Projected:** +~0.5pp.

### H. Preamble shortening (not LLM, trivial)
- Shorten `[TOKEN SAVER]` prefixes across all hook fires, drop
  "run again for raw" hint after first fire per session.
- **Projected:** +~0.3pp, near-zero effort.

### Build order (sequential fractal)
1. A → bench → wire → test
2. G → bench → wire → test
3. E → bench → wire → test
4. C → wire → test
5. I → wire → test
6. H → wire → test
7. Full re-bench against ledger
8. Commit + Hyphae facts + report update

### Success metric
Baseline 44.6% → after Phase 3: target **58–62%**. LocalEdit should fire
~1–2× per session (hundreds per week, vs 20 lifetime today). The LLM path
should contribute ≥15pp of the total savings, not <1pp like today.
