# LLMOS Routing Ladder — Status

**Date:** 2026-04-06 (night)
**Server:** http://localhost:7800 (v2 prototype)
**Status:** Built, needs end-to-end testing tomorrow

---

## What got built today

### The unified routing ladder
Single conversation surface where requests travel up rungs until handled. Lives in `lllm/os/token_bridge.py` → `TokenBridge.route_and_execute()`.

| Rung | Handler | Location | Status |
|---|---|---|---|
| 1 (bash) | `_detect_bash_kind` + `_execute_bash` | `token_bridge.py` | ✅ Built, needs browser test |
| 1 (cache/learned) | `LearnedAnswers` | `token_bridge.py` | ✅ Built |
| 2 (semantic) | `shell.handle()` | `server.py /api/chat` | ✅ Built |
| 3 (Calcifer) | `LocalLLM` via Ollama | `token_bridge.py` | ✅ Verified via curl |
| 4 (Claude) | `ClaudeCLIAgent` via `claude -p` | `agent_bridge.py` | ✅ Verified via curl |
| 5 (Claude API full-context) | — | — | ⏸ Deferred |

### Safety rails (Rung 1)
- **Safe allowlist** — read-only commands auto-execute: `ls, cat, grep, ps, df, git status, docker ps, systemctl status`, etc.
- **Unsafe** — command detected but modifies state → returns message asking for confirmation
- **Dangerous** — matches pattern (`rm -rf /`, `sudo`, `dd if=`, `mkfs`, `curl | sh`, `shutdown`, etc.) → refused with message

### Apprenticeship loop
- When Claude (Rung 4) answers a query, the Q&A pair is saved to `~/.config/lllm/learned_answers.json`
- Next time that query comes in, it returns from cache as Rung 1 — free, instant
- `self_test_batch()` replays saved queries to the local LLM in background, word-overlap match → feeds calibration
- Stats visible in Settings → Calibration → "Apprenticeship (Learned Answers)"

### Live rung preview
- New `/api/classify` endpoint — takes text, returns `{rung, rung_name, color, reason}` without executing
- Taskbar omnibar in v2 prototype shows a **live pill** next to the search icon
- Updates as you type (200ms debounce) with color-coded label
- Hover for reason

### New Settings pages (v2 prototype)
In the Settings sidebar between **Skills** and **Token Saver**:
- **Models** — GPU VRAM bars, installed Ollama models with pull/delete, recommended model
- **CLI Agents** — Claude CLI + Aider with install/auth/storage/capabilities
- **Calibration** — trust score bars, task routing map, Run Calibration button, Learned Answers stats

### New taskbar indicators
Two green-dot pills in the v2 taskbar right side (between mode pins and token widget):
- **Model indicator** — shows `gemma4:e2b` with green dot when loaded (click → Models settings)
- **Hyphae indicator** — shows memory count (`88,736 mem`) with green dot when connected (click → Hyphae settings)
- Polls `/api/model/loaded` and `/api/hyphae/status` every 10s

### Mode bar
Three pinned buttons in the v2 taskbar (after the omnibar):
- 🔥 **Calcifer** — focuses omnibar (local LLM, free)
- ⌨ **Smart Terminal** — opens Smart Terminal window (shell + `?` for AI)
- ⚒ **Workshop** — opens Workshop window (full Claude CLI agent)

---

## To test tomorrow

1. **Hard refresh** http://localhost:7800 (Ctrl+Shift+R) to bust browser cache
2. **Taskbar omnibar** — type and watch the rung pill update live as you type
3. **Rung 1 bash** — type `ls /tmp` and press Enter, should show real output in chat
4. **Rung 1 dangerous** — type `rm -rf /` and press Enter, should refuse
5. **Rung 3 Calcifer** — type `what is tcp vs udp` — should be handled locally
6. **Rung 4 Claude** — type something complex like `design a distributed rate limiter with redis and token bucket semantics`, should escalate to Claude with narration *"(let me ask Claude — this one's beyond me)"*
7. **Settings > Models** — verify GPU bars, model list, recommended show correctly
8. **Settings > CLI Agents** — verify Claude CLI shows as installed/ready
9. **Settings > Calibration** — verify trust scores load, run calibration button works
10. **Taskbar indicators** — verify both green, tooltips correct, click navigates to settings

---

## Known issues / unfinished

- The `desktop.html` simpler chat UI with three-mode bar exists but isn't served (v2 prototype takes priority in `server.py` index route)
- The **Workshop window** UI in v2 is a stub — doesn't actually dispatch work yet. It has input/output but no backend wiring
- The **Smart Terminal window** UI is also a stub — the `?` prefix routes to `/api/chat` but regular bash input isn't wired
- **Rung 5** (full-context Claude API) not implemented
- **Unsafe bash** (Rung 1 middle tier) doesn't have a real approval dialog yet — just returns a message
- **Pluggable avatars** (non-Calcifer companions) saved to Hyphae as post-launch feature

---

## Files touched today

- `lllm/os/token_bridge.py` — Calibrator wrapper, LearnedAnswers, _detect_bash_kind, _execute_bash, route_and_execute, classify_rung, self_test methods
- `lllm/os/gui/server.py` — new endpoints: /api/classify, /api/models, /api/models/pull, /api/models/delete, /api/agents/installed, /api/agents/install, /api/calibration, /api/calibration/run, /api/calibration/reset, /api/learning/stats, /api/model/loaded, /api/hyphae/status. New chat handler with bash detection → semantic → ladder order.
- `lllm/os/model_manager.py` — fixed `recommend_model()` KeyError on url
- `lllm/os/agent_bridge.py` — fixed `--system` → `--append-system-prompt` flag
- `openkeel/docs/llmos_v2.html` — mode bar, taskbar indicators, rung pill, Settings pages (Models/CLI Agents/Calibration), dynamic page loaders
- `lllm/os/gui/templates/desktop.html` — mode bar, Settings overlay with Models/Agents/Calibration (NOT currently served — only used if v2 prototype missing)

---

## How to start the server tomorrow

```bash
cd ~/lllm
nohup python3 -c "from lllm.os.gui.server import create_app; app=create_app(); app.run(host='0.0.0.0', port=7800)" > /tmp/llmos_server.log 2>&1 &
```

Then hard refresh http://localhost:7800.
