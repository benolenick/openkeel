# OpenKeel v3

**A cheaper Claude Code-style chat. Same answers, ~94% less money per turn.**

OpenKeel is a desktop app that gives you a Claude-Code-like chat experience while quietly delegating the expensive parts to Haiku. You ask a question; Haiku does all the file reading, grepping, and bash; Sonnet does just the final synthesis with the gathered data already in hand. You get the same quality answer at a fraction of the cost.

![OpenKeel v3 GUI](assets/openkeelv2.svg)

## Real measured savings

One representative task — *"How does the bubble engine route between cascade, ultra, local-only, and Haiku-API modes? Walk through the decision logic in engine.py, what each mode actually does in reason.py, and what determines the choice. Be specific with line numbers."* — run on the OpenKeel codebase itself:

| | Vanilla `claude -p` | OpenKeel bubble |
|---|---:|---:|
| **Total cost** | **$1.183** | **$0.074** |
| Wall time | 125s | 33s |
| Sonnet input tokens | 251 | 5,416 |
| Sonnet output tokens | 5,226 | 1,742 |
| Sonnet cache_create | 275,523 | 7,464 |
| Sonnet cache_read | 234,256 | 2,603 |
| Sonnet internal turns | 41 | 1 |
| Haiku tokens | 0 | 1,568 in / 254 out |

**~16× cheaper, ~4× faster, same answer quality.** Both runs produced complete, correct, well-cited explanations with line numbers.

## Why it works

Vanilla Claude Code lets Sonnet drive the whole tool loop: read a file → think → grep something → think → read another → think. On a non-trivial task that's 30-50 internal Sonnet turns, each one adding to the cache. The 41-turn vanilla run above generated **275K cache_create tokens** at $3.75 per million — that alone is over $1 of the bill.

OpenKeel routes those tool calls to Haiku instead, in a separate context. Haiku at $0.80/$4 per Mtok is dramatically cheaper than Sonnet at $3/$15. It does the exploration, hands a clean summary back, and Sonnet writes one synthesis on top. **Sonnet sees the gathered data once, writes the answer, exits.** No iteration, no cache explosion.

```
User question
    ↓
[Haiku] Plans + executes 5-8 tool calls (read, grep, bash) in its own context
    ↓
[Sonnet] One synthesis call: gathered data → final answer
    ↓
Response
```

Per-turn cost is essentially fixed at ~$0.07 (the Claude Code system prompt overhead + small Haiku gather + small Sonnet synthesis), regardless of how deep the task goes. Multi-step exploration that would explode vanilla's cache stays cheap because it happens in Haiku.

## Features

- **Bubble chat REPL** — `openkeel chat` drops you into an interactive session that uses the bubble pattern automatically. The GUI's ▶ Bubble button launches it.
- **Per-model dials** — Live token counters for Sonnet, Haiku, Opus, and Local. See exactly where every token went on every turn.
- **Pace gauge** — Visualizes whether you're ahead or behind your weekly token budget, weighted by 8-hour working blocks.
- **Hyphae memory** — Auto-injects long-term project memory so the engine remembers what you were working on last time.
- **Local LLM mode** — Optional Ollama integration for completely free local synthesis on simple tasks (`local_for: gather|reason|both|cascade|ultra`).
- **Cost transparency** — Every chat turn prints exactly what it cost: `[bubble] $0.0023  32825ms  14663 chars gathered`.

## Installation

### Prerequisites

- Python 3.10+
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
- `ANTHROPIC_API_KEY` exported (for the Haiku gather phase — Sonnet uses your existing Claude Code login)
- PySide6 (`pip install PySide6`) for the GUI
- Optional: [Hyphae](https://github.com/benolenick/hyphae) for long-term memory
- Optional: Ollama for local LLM modes

### Setup

```bash
git clone https://github.com/benolenick/openkeel.git
cd openkeel
pip install -e .
```

### Launch

```bash
# GUI (recommended)
python3 -m openkeel.gui.app

# Or jump straight into the bubble chat REPL
openkeel chat

# Or run a one-shot bubble task
openkeel "explain how routing works in this codebase" --repo /path/to/repo
```

## How a bubble turn actually works

1. You type a question into `openkeel chat`
2. Engine queries Hyphae for relevant project memory (if available)
3. **Haiku phase** (`bubble/gather.py`):
   - Haiku plans which files/commands to fetch based on your question
   - Executes 5-8 tool calls (read_file, bash) in its own context, accumulating data
   - Returns a single bundle of gathered text + a token-usage event
4. **Sonnet phase** (`bubble/reason.py`):
   - Spawns `claude -p --output-format json` with a synthesis prompt: the gathered data + your question + "answer based on this, don't go fetch more"
   - Sonnet writes one synthesis pass and exits
   - Token usage parsed from the JSON envelope, logged to the dial
5. Output streams back to the REPL with a cost line

## GUI components

- **▶ Bubble button** — Launches `openkeel chat` in the embedded terminal
- **Pace gauge** (big dial) — Green = under budget for the week, red = over. Center = on pace.
- **Model dials** — Per-model token rates (Opus, Sonnet, Haiku, Local). Hover for totals.
- **Hyphae dot** — Green when the memory server is reachable
- **LLM dot** — Green when Ollama is running with your local model loaded
- **Status bar** — Weekly quota remaining and days until reset

## Architecture

```
openkeel/
├── cli.py                 # `openkeel` entry — chat REPL, headless mode, status
├── gui/
│   ├── app.py             # Main window, toolbar, status bar
│   ├── terminal.py        # Embedded PTY terminal widget
│   ├── widgets.py         # Pace gauge, model dials, status dots
│   ├── session_watcher.py # Tails token_events.jsonl + Claude Code session JSONL
│   └── theme.py           # Dark theme
├── bubble/                # The actual delegation engine
│   ├── engine.py          # Orchestrates: hyphae → gather → reason
│   ├── gather.py          # Haiku API tool-use loop (read_file, bash)
│   ├── reason.py          # Spawns `claude -p` for Sonnet synthesis
│   ├── ollama.py          # Local LLM client (optional)
│   ├── router.py          # Routes simple queries to vanilla
│   └── settings.py        # Model + mode config
├── hooks/
│   └── session_start.py   # Hyphae injection for vanilla Claude Code sessions
├── token_events.py        # Append-only token-usage log consumed by the dial
├── quota.py               # Weekly quota tracking
└── hyphae.py              # Memory integration
```

## Testing

```bash
# Full A/B benchmark across 15 tasks (easy/medium/hard)
python3 tests/ab_full_battery.py

# Blind A/B quality judge
python3 tests/judge_v3.py

# Re-run only the bubble side (re-uses vanilla baselines)
python3 tests/rerun_flat_only.py
```

## Honest limitations

- **Per-turn fixed cost**: every bubble turn pays ~$0.07 for the Claude Code system prompt overhead. For trivial questions ("hi") that's *more* than vanilla. The savings appear on anything that would have caused vanilla Sonnet to do multi-step exploration (which is most real coding questions).
- **Cache reuse across turns**: not currently used. Each turn is one-shot. Session persistence was tested and made things worse for our pattern (each turn's gathered data accumulated in the cache).
- **First Haiku call on a fresh API key may be slow** as Anthropic's edge warms up.
- **The 70% standalone benchmark numbers** that appeared in older versions of this README came from a synthetic test harness, not real chat usage. The numbers shown above are real bubble vs vanilla measurements on the actual `openkeel chat` REPL you're getting.

## Credits

Built by [Ben Olenick](https://github.com/benolenick). Uses [Hyphae](https://github.com/benolenick/hyphae) for long-term memory.

## License

MIT
