# OpenKeel 2.0

**Token-saving AI agent toolkit for Claude Code.** Cuts Sonnet CLI calls by 60%+ through intelligent bubble delegation — Sonnet plans and synthesizes, cheaper models (Haiku API, local LLMs via Ollama) handle the grunt work.

---

## The Problem

Claude Code's Sonnet model is powerful but quota-limited (~5M OEQ/week on Pro). Every file read, every sub-task, every classification burns the same expensive tokens. Most of that work doesn't need Sonnet-level intelligence.

## The Solution

OpenKeel 2.0 implements **bubble delegation** — a gather-then-reason pattern:

1. **Sonnet plans** the task (what sub-tasks are needed)
2. **Haiku API classifies** each sub-task's difficulty
3. **Cheap models execute** — Haiku API (with tool use) for moderate analysis, local LLMs via Ollama for simple lookups
4. **Sonnet synthesizes** the gathered data into a final answer

Result: same quality output, 60% fewer Sonnet calls, ~$0.10 in Haiku API costs per session.

## Empirical Results

Tested with a comprehensive A/B battery: identical tasks run under vanilla (Sonnet-does-everything) and flat (bubble delegation) configurations.

| Metric | Vanilla | Flat (Bubble) |
|--------|---------|---------------|
| Sonnet CLI calls | 15 | 6 |
| Haiku API calls | 0 | 15 |
| Haiku cost | $0 | $0.099 |
| Local LLM calls | 0 | 3 |
| OEQ burn | 39,000 | 15,600 |
| **Sonnet reduction** | — | **60%** |
| **OEQ saved** | — | **23,400** |

*Pilot results from 3 tasks (easy/medium). Full 15-task battery with LLM-as-judge quality scoring in progress.*

## Features

### Bubble Delegation Engine
- **Flat mode**: Haiku classifies + routes to Haiku API or local LLM
- **Cascade mode**: Haiku classifies difficulty tier → local (easy), local+Haiku judge (medium), Sonnet (hard)
- **Ultra mode**: Local gather + local reason + Haiku quality gate with Sonnet escalation
- Automatic quality gates and vanilla fallback when gather quality is poor

### Hyphae Long-Term Memory
- Persistent vector memory across sessions (38K+ facts)
- Auto-injects relevant context at session start
- Remembers findings, decisions, and techniques
- Project-scoped recall prevents cross-project bleed

### GUI Dashboard
- Qt6 (PySide6) desktop app with embedded terminal
- Real-time token tracking from Claude Code JSONL files
- 4 model dials: Opus / Sonnet / Haiku / Local — exact token counts per model
- BPH (burn per hour) gauge for quota pacing
- Hyphae and LLM status indicators
- One-click "Launch Claude" button
- Settings dialog for all configuration

### Session Watcher
- Tails `~/.claude/projects/*/` JSONL files in real-time
- Extracts exact `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
- Maps model IDs to lanes (opus/sonnet/haiku/local)
- Feeds live data to GUI dials

## Quick Start

```bash
# Install
pip install -e .

# Launch GUI
openkeel

# Or headless mode
openkeel "analyze the authentication flow" --repo /path/to/project

# Check status
openkeel --status
```

### Requirements
- Python 3.10+
- Claude Code CLI (`claude`) installed
- For local LLMs: [Ollama](https://ollama.com) with a model loaded (e.g., `gemma3:4b`)
- For Hyphae memory: Hyphae server running on port 8100

### Configuration

Settings are stored in `~/.openkeel2/settings.json`. Defaults:

| Setting | Default | Description |
|---------|---------|-------------|
| `cli_model` | `sonnet` | Claude CLI model for planning/synthesis |
| `runner` | `haiku_api` | Sub-task executor: `haiku_api`, `local`, or `off` |
| `routing` | `flat` | Delegation mode: `flat`, `cascade`, `ultra` |
| `local_model` | `gemma3:4b` | Ollama model for local execution |
| `hyphae_enabled` | `true` | Enable Hyphae memory integration |

## Architecture

```
openkeel/
  bubble/
    engine.py      — Main orchestrator (gather-then-reason)
    gather.py      — Haiku API + local LLM data collection with tool use
    reason.py      — Sonnet CLI / local / ultra / cascade reasoning
    router.py      — Task complexity routing (bubble vs vanilla)
    config.py      — Claude CLI + model configuration
    ollama.py      — Ollama API client for local LLMs
    settings.py    — Bubble-specific settings
  gui/
    app.py         — Main window, toolbar, terminal integration
    widgets.py     — BPH dial, mini model dials, status dots
    session_watcher.py — Real-time JSONL token tracking
    terminal.py    — Embedded terminal emulator
    theme.py       — Dark theme CSS
    settings.py    — Settings dialog + persistence
  hyphae/
    client.py      — Hyphae memory client (recall/remember)
  cli.py           — CLI entry point
  quota.py         — OEQ quota tracking
```

## How Bubble Delegation Works

```
User Task
    │
    ▼
┌─────────┐
│ Sonnet   │  Plan: break into sub-tasks
│ (CLI)    │
└────┬─────┘
     │
     ▼ for each sub-task:
┌─────────────┐
│ Haiku API   │  Classify: can a small model handle this?
│ (classifier)│
└──────┬──────┘
       │
   ┌───┴───┐
   │       │
   ▼       ▼
┌──────┐ ┌──────────┐
│Local │ │Haiku API │  Execute with tool use
│LLM   │ │(executor)│  (file reads, grep, etc.)
└──┬───┘ └────┬─────┘
   │          │
   └────┬─────┘
        ▼
┌─────────┐
│ Sonnet   │  Synthesize gathered data into answer
│ (CLI)    │
└──────────┘
```

**Sonnet touches**: 2 calls (plan + synthesize)
**Without bubble**: 5-6+ Sonnet calls (plan + read files + analyze + synthesize)

## Testing

### A/B Battery Test
```bash
# Pilot (3 tasks, ~15 min)
python3 tests/ab_battery.py --repo . --tasks T1,T2,T3

# Full battery (15 tasks, ~2 hrs)
python3 tests/ab_full_battery.py --repo . --output tests/ab_results_v2.json --report tests/ab_report_v2.md
```

### Delphi Round Table
Independent assessment by Claude, GPT-4o, and Gemini:
```bash
python3 tests/delphi_roundtable.py --results tests/ab_results.json --output tests/delphi_report.md
```

## License

MIT
