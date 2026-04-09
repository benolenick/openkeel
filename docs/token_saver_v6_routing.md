# Token Saver v6 — Model Routing & Honest Accounting

**Status:** spec, not built
**Author:** Ben (via Claude audit, 2026-04-07)
**Goal:** turn Ben's $200 Max plan into ~2.5× more usable headroom by routing turns to the cheapest model that can do the job, and stop lying about savings.

---

## Why this exists

The current token saver (v1–v5) optimizes the wrong number. It compresses tool *output* after the fact and reports inflated "savings" against fake counterfactuals (e.g. `edit_trim` claims credit for not sending whole files Claude was never going to send). Real measurable savings are <1% of billed tokens. See `docs/token_saver_audit_2026-04-07.md` (write this if it doesn't exist — pull numbers from `~/.openkeel/token_ledger.db`).

The actual rate-limit lever on a Max plan is **which model runs the turn**:
- Opus = 1× pool weight
- Sonnet 4.6 ≈ 1/5× pool weight
- Haiku 4.5 ≈ 1/25× pool weight

If 50% of turns route to Sonnet and 20% to Haiku, the effective pool grows ~2–3×. Nothing else on the table moves the cap this much.

---

## What to build

A **pre-turn classifier + router** that picks `opus | sonnet | haiku` for each user turn, runs locally, and is fully reversible.

### Components

1. **Local classifier** (`openkeel/token_saver_v6/router/classifier.py`)
   - Calls qwen2.5:3b on jagg via Ollama (same path LocalEdit uses)
   - Input: user's latest message + last 2 turns of context (truncated to 1KB)
   - Output: `{"model": "opus|sonnet|haiku", "confidence": 0.0–1.0, "reason": "..."}`
   - Target latency: <200ms. If it takes longer, fall back to Opus and log it.

2. **Router hook** (`openkeel/token_saver_v6/router/pre_turn.py`)
   - Fires as a UserPromptSubmit hook
   - Calls classifier, writes result to `~/.openkeel/router_decision.json`
   - Sets the model for the upcoming turn (mechanism TBD — see "Open questions")
   - On classifier error: default Opus, never block the turn

3. **Override commands**
   - `/opus`, `/sonnet`, `/haiku` — force the next turn (already exist as built-in model switches; just document the workflow)
   - `/route-off` — disable routing for the rest of the session
   - `/route-on` — re-enable
   - Sticky overrides: if Ben says "use opus for the rest of this," respect it until session end

4. **Status line integration**
   - Show current model + classifier confidence in the status line
   - Show today's pool consumption split by model (pull from `billed_tokens` table joined with model column)

5. **Honest dashboard** (`openkeel/token_saver_v6/dashboard.py`)
   - Replaces the existing inflated-savings dashboard
   - **One number prominent:** "full-rate tokens this week vs last week" (cache_creation + non_cache_input + output, weighted by model)
   - Secondary: turns routed to each model, classifier accuracy estimate
   - **Do not display** the old `savings` table totals. Hide them or move them to a "legacy/counterfactual" footer.

---

## Classification rules

The classifier prompt should bucket turns roughly like this. Tune from real usage.

### → Haiku (cheapest, ~25× cheaper than Opus)
- Single file read with no follow-up reasoning
- Bash output interpretation ("did this pass?", "what's the error?")
- Grep/glob result filtering
- Status checks, monitor pings, kanban updates
- Hyphae recall summarization
- Simple yes/no questions about code
- Mechanical edits (but those should already go through LocalEdit, not Claude at all)
- "What does this file do" for files <200 lines

### → Sonnet (5× cheaper than Opus)
- Most coding tasks
- Multi-file edits within one module
- Normal debugging
- Writing tests
- Refactors with clear scope
- Most back-and-forth conversation
- Anything that would benefit from Claude's reasoning but doesn't need Opus-level judgment

### → Opus (full price, use sparingly)
- Architecture decisions ("should we use X or Y")
- Gnarly multi-file debugging where the bug isn't localized
- Anything where Ben has explicitly said "think hard"
- First turn of a brand-new complex task (let Opus plan, route follow-ups down)
- Anything involving security, money, or irreversible actions
- When the classifier confidence is <0.6 — default up, not down

### Hard rules (override the classifier)
- If user message contains "think hard", "carefully", "ultrathink" → Opus
- If user message contains "quick", "just", "simple" → at most Sonnet
- If session has had 3+ Opus turns in a row → consider Sonnet for the next one unless confidence is high
- Never route to Haiku for turns that will write code to disk — Haiku's edits are not reliable enough

---

## Measurement (the only thing that matters)

Build this **first**, before the router. You need a baseline to know if v6 is working.

1. **Add a model column** to `billed_tokens` if it isn't already populated. Backfill from transcript paths if possible.

2. **Daily report query:**
   ```sql
   SELECT
     date(timestamp,'unixepoch','localtime') d,
     model,
     COUNT(*) turns,
     SUM(cache_creation + input_tokens + output_tokens) full_rate,
     SUM(cache_read) cache_read
   FROM billed_tokens
   WHERE timestamp > strftime('%s','now') - 14*86400
   GROUP BY d, model
   ORDER BY d, model;
   ```

3. **Weighted pool consumption** (the real number):
   ```
   pool_units = opus_full_rate * 1.0
              + sonnet_full_rate * 0.2
              + haiku_full_rate * 0.04
              + cache_read * 0.1  (across all models)
   ```
   This is the number that has to go down for Ben's plan to feel bigger. Plot it daily. If it's not trending down after v6 ships, v6 failed.

4. **Success criterion:** weekly `pool_units` drops by ≥40% within 2 weeks of shipping, with no quality complaints from Ben. Stretch: 60% (= 2.5× effective plan).

---

## What to delete / stop doing

This is as important as what to build. The current system is noisy and self-deceiving.

1. **Delete or quarantine `edit_trim` from savings totals.** Its baseline (full file size) is wrong by ~50×. Either fix the baseline to `len(old_string) + len(new_string)` or remove it from the headline number entirely.

2. **Same for `prefill_ranked_map`, `bash_predict`, `bash_llm_summarize`, `recall_rerank`** — all use counterfactual baselines. Tag them `counterfactual` in the ledger and exclude from the headline. Keep them running if they have other utility, but don't credit them with token savings.

3. **Scrub Hyphae of inflated savings facts.** Search for "5.73M tokens", "79.5%", "40.2%", "tokens saved" and either delete or annotate as "pre-v6 inflated metric." These get recalled into every new session and poison Claude's framing. Run:
   ```bash
   curl -s -X POST http://127.0.0.1:8100/recall \
     -H "Content-Type: application/json" \
     -d '{"query": "token saver savings percent", "top_k": 30, "scope": {}}'
   ```
   Review and delete the inflated ones.

4. **Trim CLAUDE.md.** It's ~400 lines and gets cached on every turn. Move the LocalEdit instructions and long examples to a skill that loads on demand. Target: CLAUDE.md under 150 lines.

5. **Reduce default Hyphae recall from `top_k: 10` to `top_k: 3`** in the session-startup instructions. Less noise, smaller cache, cleaner answers.

6. **Stop the docs sprawl.** There are 12 `docs/token_saver_*.md` files. Most contradict each other. Consolidate into one `docs/token_saver.md` that reflects v6 reality. Move the rest to `docs/archive/`.

---

## Other ideas (lower priority, do after routing works)

These are worth ~5–25% each. Routing is worth ~150%. Do routing first.

### A. Sub-agent offloading to local models
Use jagg's 3090 (qwen2.5-coder:32b) for things that currently burn Opus/Sonnet tokens:
- Bash output interpretation → local model writes the summary, Claude reads the summary
- File-read summarization for large files (>500 lines) before they enter Claude's context
- Hyphae recall reranking — local model picks top 3 of 20, only those go to Claude
- Monitor cron tasks — most don't need Claude at all

### B. Session hygiene
- Hard session resets between unrelated tasks (don't drag wholesale-copilot context into token-saver work)
- Detect topic shifts and prompt Ben: "this looks unrelated to the current session, want a fresh one?"

### C. Skills instead of preamble
Anything in CLAUDE.md that's "guidance for situation X" should be a skill that loads when X happens, not preamble that loads always.

### D. Pre-tool interception (the v5 dream, mostly unrealized)
The original v4/v5 vision was to block tool calls before they fire and return summaries instead. It barely works in practice. Worth revisiting *only* if routing alone doesn't hit the 2.5× target.

---

## Open questions (resolve before building)

1. **How does the router actually switch models?** Claude Code has a model setting per session, not per turn. Options:
   - (a) Use a hook to write to settings.json mid-session (jank, may need restart)
   - (b) Spawn a sub-agent on the cheaper model and let it handle the turn (clean but adds latency and breaks conversation flow)
   - (c) Wait for / request a Claude Code feature: per-turn model override via hook return value
   - **Recommend:** start with (b) for clear cases (single-shot Haiku tasks), document (a) as a manual `/sonnet` workflow, and file a feature request for (c).

2. **How to evaluate classifier accuracy?** Suggest: log every routing decision, periodically have Opus re-grade a sample, compute agreement rate. Target >80% before trusting it.

3. **Cold-start without history.** First two weeks the classifier has no feedback loop. Use conservative defaults (lean toward Sonnet, not Haiku) until accuracy is measured.

4. **Quality regression detection.** If Ben starts saying "that's wrong" or "redo this" more often after v6 ships, the router is being too aggressive. Add a simple counter for re-asks per session and alarm if it spikes.

---

## Build order

1. **Honest measurement first** (1 day). Ledger query, weighted pool_units calculation, baseline chart. Without this, you can't tell if anything else worked.
2. **Scrub Hyphae + trim CLAUDE.md + consolidate docs** (half day). Free wins, cleans up the framing problem.
3. **Manual routing workflow** (half day). Document `/opus`, `/sonnet`, `/haiku` discipline. Have Ben use it manually for a week. Measure pool_units delta. This alone might hit half the target.
4. **Local classifier** (1–2 days). Build, calibrate against the manual-routing week's data.
5. **Auto-router hook** (1–2 days). Wire classifier into pre-turn hook with overrides.
6. **Honest dashboard** (1 day). Replace the lying one.
7. **Sub-agent offloading** (open-ended). Only if pool_units hasn't hit target after step 5.

Total: ~1 week of focused work for steps 1–6. Step 7 is ongoing.

---

## Definition of done

- `pool_units` (weighted full-rate token consumption) drops ≥40% week-over-week
- Ben doesn't notice quality regressions
- The dashboard shows one honest number, not twelve flattering ones
- Hyphae no longer recalls inflated savings claims into new sessions
- Old savings engines are either fixed or labeled `counterfactual`
- CLAUDE.md is under 150 lines

If `pool_units` doesn't drop, **v6 failed** regardless of how clever the router is. Don't ship a victory lap on a fake number again.

---

## Notes for the implementing agent

- Ben is sick of inflated metrics and Claude flip-flopping on what the bottleneck is. Be brutally honest. If something doesn't work, say so and stop.
- Measure against `~/.openkeel/token_ledger.db` `billed_tokens` table. That's ground truth. Everything else is a self-report.
- Don't add new "engines" with new counterfactual baselines. The baseline is always the real billed token count from before the change.
- If you find yourself writing a doc that celebrates a percentage, check whether that percentage is measured against something Anthropic actually charged for. If not, delete the doc.
- Read `docs/token_saver_proxy_audit_2026-04-07.md` and the 2026-04-07 audit playbook before starting — they already flagged some of this and got partially ignored.
- When in doubt about quality vs cost, ask Ben. Don't silently downgrade his coding turns to Haiku and hope he doesn't notice.
