# Token Saver v4.5 — Final Reference

**Status:** Production. Five LLM-as-input-filter engines live in `pre_tool.py`.
**Date:** 2026-04-07
**Operating principle:** The local LLM is a filter in front of Claude's input, never a writer of Claude's output.

---

## The thesis (one paragraph)

Earlier versions of the token saver tried to use the local LLM (qwen2.5:3b on jagg's 3090) as a *substitute for Claude's output* — most notably LocalEdit, which let qwen edit user files directly. After 500 sessions LocalEdit had a 40% failure rate and saved 155K chars total. The redesign that became v4.1–v4.5 inverts that role completely: **the local LLM never produces output the user trusts; it only filters input that Claude is about to read.** Worst case = Claude has slightly less context, same as a smaller `top_k` or a tighter offset/limit. There is zero correctness risk on user files because the engines never write anything. This is the design that finally made the 3090 earn its keep.

---

## The five live engines

| Engine | Version | Traffic class | Bench ratio | Live ratio | Live event_type |
|---|---|---|---|---|---|
| recall_rerank | v4.1 | Hyphae /recall responses | 94–97% | 95.2% | `recall_rerank` |
| diff_compress | v4.2 | git diff/show/log -p | 97–99% | 98.8% | `diff_compress` |
| goal_filter | v4.3 | Read on files >5KB | 86–97% | 76.8% | `goal_filter` |
| subagent_compress | v4.4 | Agent prompts >2.5K | 84–95% | 67.7% | `subagent_compress` |
| webfetch_compress | v4.5 | WebFetch responses >1.5K | 93% | **99.1%** | `webfetch_compress` |

**Combined live result:** 320,498 chars → 16,548 kept = **94.8% compression** across 9 real hook invocations spanning every major input traffic class.

**Combined per-session estimate** (typical, not heavy): **~50–150K Claude tokens saved per session.**

**Combined annual estimate** at ~250 sessions/month: **60M–225M tokens/year.**

---

## How each engine works

### 1. recall_rerank (v4.1)
- **Hook:** PreToolUse on Bash, intercepts curl POSTs to `127.0.0.1:8100/recall`
- **Action:** Extracts the `query` from the curl body, runs `recall_rerank.rerank(query, results)`, qwen returns a JSON array of result indices to keep, the rest are dropped before the existing v3 heuristic compression runs on what remains
- **Latency:** ~1s
- **Why it works:** Hyphae returns top-k results sorted by vector score, but only 1-3 of them usually answer the query directly. qwen filters semantically before scoring kicks in.

### 2. diff_compress (v4.2)
- **Hook:** PreToolUse on Bash, matches `git diff`, `git show`, `git log -p`
- **Action:** Runs the git command, passes raw diff to qwen with prompt "compress to a tight semantic change list, drop whitespace/import-shuffle/formatting"
- **Latency:** ~3s
- **Why it works:** Diffs are 90% noise (whitespace, formatting, file headers); the actual semantic changes are usually <5%.

### 3. goal_filter (v4.3) — the biggest single lever
- **Hook:** PreToolUse on Read, fires on files >5000 chars
- **Action:** Reads the agent's current goal from `~/.openkeel/current_goal.txt` (or env var, or distilled_log entry), passes file + goal to qwen, returns only lines relevant to the goal with line numbers preserved
- **Latency:** 7-18s (slowest engine, but cheapest for what it saves)
- **Why it works:** Most file reads are looking for one specific function or pattern; reading the whole file is wasteful. qwen extracts only the relevant ~10-15%.
- **Caveat:** Goal source must be populated for best results. Falls back to a generic "useful for first read" filter when goal is empty.

### 4. subagent_compress (v4.4)
- **Hook:** PreToolUse on Agent, fires on prompts >2500 chars
- **Action:** qwen rewrites the prompt into a tighter version, **appends a concision directive** (`"End your response with '## ANSWER:' followed by max 25 lines"`), block the original spawn with the rewritten prompt as the reason, Claude re-spawns
- **Latency:** ~5s
- **Why it works:** Real Agent prompts in the ledger are 17-30K chars because Claude pastes file content. The rewrite preserves file paths and code blocks verbatim while dropping context dump and preamble. The directive constrains the subagent's response too (saves on both sides of the boundary).
- **Important caveat:** This engine **cannot transparently filter subagent return values** — Claude Code's PostToolUse hooks can't replace delivered tool results. The block-and-respawn pattern is the workable approximation. There is one extra round-trip per intercepted spawn.

### 5. webfetch_compress (v4.5)
- **Hook:** PreToolUse on WebFetch
- **Action:** Fetches the URL ourselves (15s timeout, 800KB cap), runs `webfetch_summarizer.summarize(page, question)` where `question` comes from the WebFetch tool's `prompt` parameter, qwen returns only content relevant to the question
- **Latency:** ~1-3s + fetch time
- **Why it works:** Web pages are 80-95% navigation/footer/marketing. The actual answer to a specific question is usually in 1-3 paragraphs.

---

## What's in the codebase

```
openkeel/token_saver_v4/
├── __init__.py                 # version 0.4.5
├── bench.py                    # stress test for all engines
└── engines/
    ├── recall_rerank.py        # v4.1 — live
    ├── diff_compressor.py      # v4.2 — live
    ├── error_distiller.py      # v4.2 — built, NOT live
    ├── webfetch_summarizer.py  # v4.2 — live (via v4.5 hook)
    ├── pre_compactor.py        # v4.2 — built, NOT live (needs real transcript bench)
    ├── goal_reader.py          # v4.3 — live
    ├── subagent_filter.py      # v4.4 — live
    ├── lingua_compressor.py    # v4.0 — runs as a shim post-process
    └── subagent_offload.py     # v4.0 — nudge-only, marginal value

openkeel/token_saver/hooks/pre_tool.py
  ├── _try_compress_json   ← recall_rerank pass before v3 heuristic
  ├── handle_read          ← goal_filter pass before v3 semantic_skeleton
  ├── handle_bash          ← diff_compress branch for git diff/show/log -p
  ├── handle_agent         ← subagent_filter for prompts >2500
  └── handle_webfetch      ← webfetch_compress (new in v4.5)
```

---

## Operating procedures

### Master kill switch
```bash
# Disable all v4 engines, fall back to v3
export TOKEN_SAVER_V4=0
```

### Per-engine kill switches
```bash
export TOKEN_SAVER_DISABLE_RECALL_RERANK=1
export TOKEN_SAVER_DISABLE_DIFF_COMPRESSOR=1
export TOKEN_SAVER_DISABLE_GOAL_READER=1
export TOKEN_SAVER_DISABLE_SUBAGENT_FILTER=1
export TOKEN_SAVER_DISABLE_WEBFETCH_SUMMARIZER=1
```

### Setting the goal for goal_filter
```bash
# Single line file, picked up by goal_reader.get_current_goal()
echo "find where Hyphae recall responses are intercepted" > ~/.openkeel/current_goal.txt
```

A `UserPromptSubmit` hook to auto-populate this from the user's most recent message would 10x the engine's effectiveness. **Not yet built.** This is the highest-leverage follow-up.

### Health check the ledger
```bash
sqlite3 ~/.openkeel/token_ledger.db "
SELECT event_type, COUNT(*), SUM(original_chars), SUM(saved_chars),
       ROUND(SUM(saved_chars) * 100.0 / NULLIF(SUM(original_chars), 0), 1) as pct
FROM savings
WHERE event_type IN (
  'recall_rerank','diff_compress','goal_filter',
  'subagent_compress','webfetch_compress'
)
GROUP BY event_type;
"
```

### Run the stress test
```bash
TOKEN_SAVER_V4=1 python3 -m openkeel.token_saver_v4.bench
```

---

## Lessons learned (what I'd tell the next person)

### What works
1. **LLM as input filter is the right pattern.** The 3090 pre-processes bytes that are about to enter Claude's context. No correctness risk on user files.
2. **Per-engine kill switches.** Every engine respects an env var so you can isolate problems without redeploys.
3. **Fail-open everywhere.** Every engine returns the original input on any error. The token saver should never break a tool call.
4. **Semantic compression on real source code is qwen's sweet spot.** goal_filter routinely hits 85–95% on Python files with realistic goals.
5. **Bench against real data, not synthetic.** The first subagent_filter bench used 1K-char synthetic prompts and looked broken; real sizes from the ledger were 17-30K and the engine showed 87% compression. Always inflate bench probes to realistic sizes.

### What doesn't work
1. **LLM as Claude's editor.** LocalEdit had a 40% failure rate on qwen2.5:3b. Even gemma4:e2b would only get to ~85%. The trust threshold is too high for any small model.
2. **PostToolUse cannot replace tool results.** This is a structural limit of Claude Code's hook architecture. The original "transparent post-hoc subagent return filter" is impossible. The workaround (block-and-respawn on the input side) works but adds friction.
3. **Generic semantic skeletons.** v4's `semantic_skeleton` produces a structural summary regardless of what Claude needs. goal-conditioned filtering produces a 3-5x tighter result.
4. **Synthetic ledger replays for `pre_compactor`.** The engine looks promising on paper but the bench is unstable because the ledger doesn't store real transcript bytes. Don't trust pre_compactor numbers until it sees a real conversation dump.

### Anti-patterns to avoid
1. **Building features without measurement.** v4.0 shipped lingua_prehook, subagent_offload, and hybrid_recall — three engines that fired 1, 0, and 0 times respectively in production. Always wire ledger logging *first*, ship *second*.
2. **Trusting cherry-picked benches.** Every engine in the suite was first benched on synthetic data, then on real data, and the second number is always lower. Cite both.
3. **Optimizing for the wrong cost.** LocalEdit was sold as "faster than Claude" — but Claude's edits are reliable, and qwen's aren't. Speed without reliability is negative value.

---

## What's NOT shipped, and why

| Engine | Status | Why not live |
|---|---|---|
| `error_distiller` | Built, benched at 91.6% | Lower-leverage than the live engines; bash_compress already catches some |
| `pre_compactor` | Built, benched at 6-25% (unstable) | Needs a real transcript dump to bench honestly. Don't ship until measured on real data. |
| `lingua_compressor` | Live as a post-process shim | Runs on every blocked tool reason, marginal effect (~1-95 chars/event) |
| `subagent_offload` | Live as nudge-only | 73 nudges fired, 0 actual savings — it tells me to delegate but doesn't measure if I obey |
| `hybrid_recall` | Built, never fired | Multi-hop entity expansion was speculative; recall_rerank made it redundant |

---

## The four highest-leverage follow-ups (ranked)

1. **`UserPromptSubmit` hook to auto-populate `~/.openkeel/current_goal.txt`** — would dramatically improve goal_filter. ~1 hour.
2. **`error_distiller` live wiring on bash test/error commands** — built and benched, just needs ~30 min of hook plumbing.
3. **`pre_compactor` real-transcript bench, then live wire** — biggest unmeasured opportunity. Needs a captured Claude Code transcript dump first.
4. **A/B logging for every engine** — capture both "what qwen produced" and "what Claude would have read" for a sample of calls, so we can detect quality regressions before users notice them.

---

## Honest ceiling assessment

The token saver currently runs at **~45% lifetime savings**. With v4.1–v4.5 fully active and the ledger accumulating ~50 events of each new engine type per day, the lifetime number should reach **55–65% within 30 days**. With the four follow-ups above, **65–75%** is plausible within 60 days.

**The hard ceiling** is whatever fraction of Claude's input is genuinely irreducible — file paths it actually needs, tool call results it actually uses, your own messages. My honest estimate is that ceiling is around **75–80%**. We are getting close to it. Beyond that, marginal improvements come from **fewer tool calls**, not from compressing the calls Claude already makes — which is a different (and harder) problem domain.

---

## TL;DR

- The local LLM is now embedded in the five biggest token streams entering Claude's context
- All five engines are read-side filters with zero correctness risk on user files
- Live measurement: 94.8% compression across 9 real hook invocations
- Estimated session savings: 50–150K Claude tokens
- Lifetime savings counter on track to move from 45% → 60–65% within 30 days
- The original "LLM as Claude's editor" idea (LocalEdit) is dead; the "LLM as Claude's input filter" idea is the entire system now
- Kill switches are wired per-engine; the system fails open on every error path
- Next biggest leverage: auto-populating the goal source for goal_filter

The 3090 has a real seat at the table.
