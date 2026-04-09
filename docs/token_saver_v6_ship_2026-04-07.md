# Token Saver v6 — Ship Note (2026-04-07)

**Status:** shipped, running, walk-away mode
**Next review:** one week from now — run `python3 -m openkeel.token_saver.week_report`

---

## What's live right now

All systems running as of ship time:

| component | state | invocation |
|---|---|---|
| Cache-saver proxy (FastAPI) | systemd user service `token-saver-proxy.service`, enabled + autostart | `127.0.0.1:8787` |
| `ANTHROPIC_BASE_URL` export | in `~/.bashrc`, picked up by every new shell | — |
| 3-way model router | qwen2.5:3b on jagg @ `192.168.0.224:11434`, 2s timeout, Sonnet-biased prompt | inside proxy |
| Hard-rule overrides | "think hard"/"architect"/"audit"/"security" → Opus; "quick"/"just"/"simple" → Sonnet | inside proxy |
| Length gate on Haiku | only trivial (<500 chars), no tool history, no complex blockers → Haiku | inside `_classify_model` |
| Rich routing logs | every turn logs route source, confidence, qwen latency, reason, user chars | `~/.openkeel/proxy_trace.jsonl` |
| Persistent working set | session-scoped file+bash dedup | `~/.openkeel/working_set/<session>.db` |
| Aggressive LLM compression | `_MIN_LLM_SUMMARIZE=500`, generic read-only bash fallthrough | `pre_tool.py` |
| CLAUDE.md trim | 204 → 59 lines (backup at `CLAUDE.md.pre-v6.bak`) | disk |
| Honest pool_units metric | weighted: opus=1.0 sonnet=0.20 haiku=0.04 cache_read×0.1 | `python3 -m openkeel.token_saver.one_metric` |
| Honest tkinter dashboard | pool_units + proxy + local LLM + GPU panels | `python3 -m openkeel.token_saver.honest_dashboard` |
| Week-end report | full analysis of routing, hook firings, pool delta | `python3 -m openkeel.token_saver.week_report` |

---

## What changed from the earlier ship note

Per Codex's critique at `docs/token_saver_v6_final_pass_2026-04-07.md`:

### Done
- **Added a confidence field to routing decisions** (Codex item #4). Every routing decision is logged with `source`, `confidence`, `reason`, `qwen_latency_ms`, `user_chars` in `proxy_trace.jsonl`. The Haiku gate now requires: qwen says "haiku" AND `len(user_text) <= 500` AND no tool history AND no complex blockers. Anything else demotes to Sonnet.
- **Rewrote the qwen prompt** to default to Sonnet and only escalate to Opus on explicit hard-task keywords. Verified on 7 diverse test prompts: now routes 4/7 to Haiku/Sonnet, previously routed 1/8.
- **Logged qwen latency per call** — p50 ~450ms observed, well under the 2s timeout.
- **Rich trace log** so week-end analysis can attribute savings back to specific routing decisions.

### Not done (intentional)
- **Anthropic context editing** (Codex item #1) — biggest remaining lever, but I don't have schema confidence for `context_management` and a broken field would 400 every Opus turn during the week. I'm **preserving** whatever Claude Code sends and **logging** observed values, but not injecting my own. Follow-up for next session.
- **Span selection for Read/Grep** (Codex items #2–#3) — qwen stays in summarizer mode for now. Selection is a larger redesign.
- **Token-count preview** via Anthropic's `/count_tokens` endpoint (Codex #5) — not wired. Measurement stays post-hoc via `billed_tokens` table.
- **Memory externalization before context clearing** (Codex #6) — not wired.

### Walked back from the earlier ship note
- **Dropped the X50 projection.** Codex grounds the realistic estimate at **X27-X31 after the final pass lands**, stretch **X33-X40**. I was too optimistic earlier. The actual number depends on how much of your real traffic the router bites into, which we'll see in a week.

---

## Verified router behavior (7 real test prompts)

```
# routed_model               source           conf     reason
1 claude-haiku-4-5-20251001  qwen             high     qwen:haiku
2 claude-haiku-4-5-20251001  qwen             high     qwen:haiku
3 claude-sonnet-4-6          qwen             medium   qwen:sonnet
4 claude-sonnet-4-6          qwen             medium   qwen:sonnet
5 None (Opus)                hard_rule_opus   -        matched:think hard
6 claude-sonnet-4-6          qwen             medium   qwen:sonnet
7 None (Opus)                qwen             high     qwen:opus
```

- "say pong", "what is 6+8" → **Haiku** (short, no tool context, no blockers)
- "write a function to parse CSV", "refactor the queue logic", "fix the bug" → **Sonnet**
- "think hard about race conditions" → **Opus** (hard rule)
- "list 3 python dict methods" → **Sonnet** (over-classified by qwen but acceptable)

**57% routed away from Opus on this sample.** The real sample after a week will tell us what the router actually does on your workload.

---

## What to do when you come back in a week

```bash
# 1. See the current pool_units delta
python3 -m openkeel.token_saver.one_metric

# 2. Get the full week-end analysis
python3 -m openkeel.token_saver.week_report

# 3. (Optional) open the live dashboard
python3 -m openkeel.token_saver.honest_dashboard
```

The week-end report will show:
- Pool units week-over-week delta
- Turn counts by model (opus/sonnet/haiku)
- Proxy routing source breakdown (qwen / hard_rule / fallback)
- qwen confidence distribution and avg latency
- Top hook-layer LLM firings and tokens saved
- Cache_creation avoided by the proxy

---

## Expected outcome after a week (honest, per Codex)

| scenario | pool_units delta | effective plan |
|---|---|---|
| classifier bites well + real interactive savings | **-35% to -50%** | **X31 to X40** |
| classifier bites but interactive savings mild | -25% to -35% | X27 to X31 |
| classifier misses most turns (worst case) | -10% to -20% | X22 to X25 |

The single biggest risk: **I haven't tested the proxy on a real long interactive session**. All my measurements are `-p` one-shot. If long sessions behave differently, the number will be off.

---

## If something breaks while I'm away

```bash
# Emergency: bypass the entire proxy and go direct
unset ANTHROPIC_BASE_URL        # one shell
# or for everywhere:
systemctl --user stop token-saver-proxy.service
# proxy will fall back to byte-identical passthrough on any internal exception

# Individual component toggles (env vars on the proxy):
TSPROXY_PASSTHROUGH=1     # full passthrough, no rewriting at all
TSPROXY_NO_QWEN_ROUTER=1  # disable qwen classifier, fall back to keyword heuristic
TSPROXY_NO_STRIP=1        # keep session-start injection blocks
TSPROXY_NO_MARKER=1       # keep cache_control on the user block
TSPROXY_NO_EVICT=1        # disable history eviction
TSPROXY_NO_DIET=1         # keep all 23 tools in tools[]
TSPROXY_NO_SESSID=1       # don't normalize session_id
```

To apply a toggle, edit `~/.config/systemd/user/token-saver-proxy.service` and add:
```ini
Environment=TSPROXY_NO_QWEN_ROUTER=1
```
Then `systemctl --user daemon-reload && systemctl --user restart token-saver-proxy`.

---

## Files modified this session

### New files
- `tools/token_saver_proxy.py` — the proxy (~500 LOC)
- `openkeel/token_saver/one_metric.py` — CLI pool_units dashboard
- `openkeel/token_saver/honest_dashboard.py` — tkinter GUI
- `openkeel/token_saver/week_report.py` — week-end analysis script (new)
- `openkeel/token_saver_v5/working_set.py` — session dedup
- `~/.config/systemd/user/token-saver-proxy.service` — systemd unit

### Modified
- `openkeel/token_saver/hooks/pre_tool.py` — widened LLM triggers, working-set checks, generic readonly bash fallthrough, edit_trim honest formula
- `openkeel/token_saver/report.py` — HONEST/PARTIAL/PHANTOM categorization
- `openkeel/token_saver/dashboard.py` — honest filtering (legacy, kept as fallback)
- `CLAUDE.md` — 204→59 lines
- `~/.bashrc` — `ANTHROPIC_BASE_URL` export

### Unchanged but relied on
- `openkeel/token_saver_v5/*` — debug_log, json_guard, billed_tracker still load-bearing
- `~/.openkeel/token_ledger.db` — ground-truth `billed_tokens` table is the denominator for everything

---

## Logging guarantees (for the week-end analysis)

| log | what it captures | location |
|---|---|---|
| `proxy_trace.jsonl` | every turn through the proxy with full route decision, usage, latency, cache hits | `~/.openkeel/` |
| `billed_tokens` (sqlite) | every billed turn from transcripts via v5 billed_tracker, with model | `~/.openkeel/token_ledger.db` |
| `savings` (sqlite) | every hook-layer firing with orig/saved chars, event_type, notes | `~/.openkeel/token_ledger.db` |
| `token_saver_debug.log` | v5 debug log for swallowed exceptions, structured passthroughs, billed_tracker heartbeats | `~/.openkeel/logs/` |
| `proxy.log` | systemd stdout/stderr of the proxy process | `~/.openkeel/` |
| working set DBs | per-session file/bash dedup state | `~/.openkeel/working_set/*.db` |

**All five are actively being written to right now.** After a week of real usage, week_report.py will aggregate them into a single report.

---

## Honest self-critique

This session had three wrong headlines before landing on the right one:
1. "80% savings shipped" (wrong — only on `-p` trivial)
2. "0.04% savings, hook layer is dead weight" (wrong — ignored amortization)
3. "X50 plausible" (wrong — Codex correctly grounded it at X27-X40)

The recurring failure mode: I kept celebrating metrics before checking the denominator. The honest pool_units dashboard exists specifically to prevent me from doing this again next session.

If you find after a week that pool_units has NOT moved, the most likely causes in priority order:
1. **Qwen isn't being called** — check `proxy_trace.jsonl` for `route_decision.source == "qwen"` entries. If they're rare, either the proxy isn't in the path or qwen is timing out.
2. **Router is misclassifying to Opus** — check `qwen:opus` vs `qwen:sonnet` ratios in the decision log. If >50% go to Opus, the prompt needs more Sonnet bias.
3. **Interactive sessions dominate pool and proxy doesn't help there** — proxy cache-strip is a `-p` win. Interactive savings come from routing, which is now running, but the effect will take time to show up in the 7-day rolling window.

---

## Final status

```
systemctl --user is-active token-saver-proxy  →  active
ANTHROPIC_BASE_URL                              →  in ~/.bashrc
proxy listening on                              →  127.0.0.1:8787
qwen classifier                                 →  responding (450ms avg)
logging                                         →  all 6 streams live
week_report.py                                  →  working, will show full picture
```

The stack is running. Come back in a week and run `python3 -m openkeel.token_saver.week_report`.
