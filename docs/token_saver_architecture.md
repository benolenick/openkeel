# Token Saver — Architecture & Implementation Guide

## Overview

The Token Saver is a hook-based system that reduces Claude Code token consumption by intercepting, compressing, caching, and delegating tool calls. It runs as Claude Code hooks (pre-tool, post-tool, session-start) plus a background daemon.

**Current performance:** ~38-60% token savings depending on workload. Saves ~$30+ on Opus pricing across 270+ sessions.

## Architecture

```
Claude Code
    │
    ├── SessionStart hook ──→ Prefill engine (project map, Hyphae context, git)
    │
    ├── PreToolUse hook ────→ Interception layer (compress/block/rewrite)
    │   ├── Read  → re-read cache, large file compression
    │   ├── Bash  → output compression, LocalEdit delegation
    │   ├── Grep  → run rg + filter/rank results
    │   ├── Glob  → run glob + noise filter
    │   └── Agent → prompt tracking
    │
    ├── PostToolUse hook ───→ Observation layer (cache, measure, predict)
    │   ├── Read  → cache for re-read, trigger summarization, predict next reads
    │   ├── Bash  → measure compressibility, track conversation
    │   ├── Grep/Glob → measure filter potential
    │   └── Edit/Write/Agent → track for conversation compression
    │
    └── Token Saver Daemon (localhost:11450)
        ├── File summary cache (LRU, persisted)
        ├── Session read tracker (dedup)
        ├── Predictive pre-cache
        └── Ledger (SQLite at ~/.openkeel/token_ledger.db)
```

## Components

### 1. Hooks (openkeel/token_saver/hooks/)

#### session_start.py
Runs at conversation start. Outputs context to stdout for Claude to see:
- Queries Hyphae for recent work context + infrastructure facts
- Builds ranked project file map (top files by relevance)
- Pre-warms cache for recently modified files
- Resets session state

**Token savings:** ~960K tokens from prefill_index + prefill_ranked_map (prevents Claude from exploring the codebase from scratch each session).

#### pre_tool.py
Intercepts tool calls BEFORE execution. Can block and replace with compressed output.

| Engine | What it does | Avg savings |
|---|---|---|
| Read re-read cache | Serves LLM summary instead of full file on re-reads | 97% per hit |
| Read large file | First reads of >20KB files → head + structure + tail | 90% |
| Bash compress | Runs command, compresses output (SSH, git, tests, builds, packages) | 60-95% |
| Bash LocalEdit | `#LOCALEDIT:` convention → delegates edits to local LLM | 81-99% |
| Grep compress | Runs rg, filters/ranks results by relevance | 99% on large results |
| Glob compress | Runs glob, noise-filters (removes __pycache__, .bak, etc.) | 80% |
| Curl compress | JSON-aware compression for API responses (Hyphae, Kanban, etc.) | 60-80% |

**Protocol:** Reads JSON from stdin `{"tool_name": "...", "tool_input": {...}}`, outputs `{"decision": "block", "reason": "..."}` to block, or nothing to allow.

#### post_tool.py
Runs AFTER tool execution. Cannot modify output (Claude already saw it). Used for:
- Caching file reads for future re-read detection
- Measuring compressibility (logged for analysis)
- Tracking conversation turns for compression
- Triggering predictive pre-cache
- Saving file skeletons to Hyphae for cross-session memory

### 2. Engines (openkeel/token_saver/engines/)

#### context_prefill.py
Builds the session-start context injection:
- Scans project files, ranks by relevance (recently modified, imported, large)
- Generates a compact "project map" with file descriptions, classes, functions
- Includes recent git history (last 5 commits, uncommitted changes)

#### conversation_compressor.py
Tracks tool call history, periodically summarizes via local LLM:
- Records each tool call as a one-line summary (file, command, output size)
- Every 10 calls, sends to Ollama for compression into 5-8 line narrative
- Stored for potential context injection in long sessions

#### output_compressor.py (post-tool measurement only)
Rule-based output compression patterns:
- Package manager output → errors + summary
- Git push/pull → strip progress noise
- Test output → failures + summary, skip individual passes
- Build output → errors + final status
- Search results → truncate to top N

#### search_filter.py
Relevance ranking for grep/glob results:
- Scores files by importance (main.py > test_foo.py > .bak)
- Deduplicates (max 3 results per file)
- Noise filtering (__pycache__, node_modules, .min.js, etc.)

#### predictive_cache.py
Predicts what files Claude will read next based on current read:
- Import graph following (if reading A which imports B, pre-cache B)
- Co-read patterns (files frequently read together)
- Pre-warms the daemon's summary cache

#### local_edit.py
Delegates file edits to local LLM instead of Claude's Edit tool:
- Parses `#LOCALEDIT: /path | instruction` format
- Sends file content + instruction to best available Ollama model
- Parses JSON response (old_string/new_string)
- Safety: creates .localedit.bak backup, verifies unique match
- Returns compact diff for Claude to verify

**GPU Tier-aware:** Automatically routes to the best available model.

#### gpu_tier.py
Auto-detects GPU capabilities across local and network machines:

| Tier | Model Size | Features Unlocked |
|---|---|---|
| 0 | No GPU | Rule-based compression only |
| 1 | ≤8B (gemma4:e2b) | Simple LocalEdit, basic summarization, conversation compress |
| 2 | 12-27B (gemma4:26b) | Complex LocalEdit, multi-line edits, smart summaries, code review |
| 3 | >30B | Full code delegation, architectural reasoning |

**How it works:**
1. Probes Ollama endpoints (local + network, e.g., jagg at 192.168.0.224)
2. Enumerates available models, estimates parameter count
3. Picks the best model by: tier > loaded status > latency
4. Caches result for 60 seconds
5. All engines call `get_best_endpoint()` to auto-route

**Adding a new machine:** Just add its IP to `_ENDPOINTS` list in gpu_tier.py. The tier system auto-discovers its models.

### 3. Daemon (openkeel/token_saver/daemon.py)

Background HTTP server on localhost:11450:
- `POST /summarize` — summarize a file (LLM-powered, cached by path+mtime)
- `POST /session/read` — track which files have been read this session
- `POST /ledger/record` — record a savings event
- `POST /cache/warm` — pre-warm cache for a list of files
- `GET /health` — health check (ollama status, cache stats)
- `GET /session/reset` — reset session state

### 4. Ledger (openkeel/token_saver/ledger.py)

SQLite database at `~/.openkeel/token_ledger.db`:
```sql
CREATE TABLE savings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,  -- bash_compress, local_edit, cache_hit, etc.
    tool_name TEXT,
    file_path TEXT,
    original_chars INTEGER DEFAULT 0,
    saved_chars INTEGER DEFAULT 0,
    notes TEXT
);
```

All engines write to this. The dashboard reads from it in real-time.

### 5. Dashboard (openkeel/token_saver/dashboard.py)

Tkinter real-time monitor with system tray support:
- Scrolling bar graph: dark red (irreducible), bright red (compressible), green (saved)
- Engine breakdown strip showing per-engine savings
- GPU status: utilization, VRAM, loaded model, tier
- Daemon health + cache stats
- Last LocalEdit activity
- View modes: REALTIME (10m), HISTORY (1h), SESSION (4h)
- Minimizes to system tray (pystray)

Launch: `python -m openkeel.token_saver.dashboard`

### 6. Summarizer (openkeel/token_saver/summarizer.py)

Ollama client for the daemon and conversation compressor:
- `ollama_generate()` — text generation with think:false
- `summarize_file()` — compress a source file to ~15 bullet points
- `filter_output()` — LLM-powered output filtering
- `classify_task()` — task difficulty classification for routing

**Critical:** Uses `think: false` because gemma4 is a thinking model. Without this, it burns all tokens on internal reasoning and returns empty.

## Data Flow Examples

### Example 1: File Re-read
```
Claude calls Read(file_path="foo.py")
  → pre_tool checks: already read this session? YES
  → daemon /summarize returns cached LLM summary (14 lines vs 800)
  → pre_tool blocks with summary
  → Claude sees 14-line summary instead of 800-line file
  → Savings: ~97%
```

### Example 2: LocalEdit
```
Claude calls Bash("#LOCALEDIT: /path/foo.py | Change TIMEOUT = 30 to 60")
  → pre_tool detects #LOCALEDIT: prefix
  → gpu_tier.get_best_endpoint() → gemma4:e2b on localhost (tier 1)
  → local_edit reads foo.py, sends to Ollama with instruction
  → Ollama returns {"old_string": "TIMEOUT = 30", "new_string": "TIMEOUT = 60"}
  → local_edit creates backup, applies edit, builds diff
  → pre_tool blocks with compact diff (6 lines)
  → Claude sees diff instead of Edit tool's full file echo
  → Savings: ~95%
```

### Example 3: Large Grep
```
Claude calls Grep(pattern="import", path="/project", output_mode="content")
  → pre_tool runs rg itself, output is 300KB
  → search_filter ranks by relevance, dedupes, keeps top 25
  → pre_tool blocks with filtered results (2KB)
  → Savings: ~99%
```

## LLMOS Integration Plan

### Phase 1: Package as standalone
- Extract token_saver/ into its own pip-installable package
- Config file instead of hardcoded IPs/ports
- CLI for setup: `llmos-token-saver install` (creates hooks, starts daemon)

### Phase 2: GPU tier as a service
- gpu_tier.py becomes a network service that all LLMOS nodes report to
- Central registry of available GPUs/models across the fleet
- Load balancing: route LocalEdit to least-loaded GPU

### Phase 3: Model-aware routing
- Different prompts for different model families (gemma vs llama vs qwen)
- Quality scoring: track LocalEdit success/failure rate per model
- Auto-downgrade: if tier 2 model fails, retry on tier 1

### Phase 4: Cross-session learning
- Track which files get re-read most → pre-cache aggressively
- Track which edit patterns succeed → optimize prompts
- Track which commands produce large output → add compression rules

## Key Design Decisions

1. **Fail-open everywhere** — any error in hooks → allow tool call through. Never block Claude's work.
2. **Pre-tool does the real saving** — PostToolUse can't modify output, so all actual savings happen in pre_tool.py.
3. **Rule-based first, LLM second** — LLM is optional (tier 0 still saves ~30%). Rules are instant, LLM adds latency.
4. **think: false is mandatory** — gemma4 is a thinking model. Without this flag, every LLM call returns empty.
5. **Backup before edit** — LocalEdit always creates .localedit.bak. Safety net for a 5B model making code changes.
6. **Fixed time slots in dashboard** — the time window is divided into N equal slots regardless of data density. Prevents sparse data from clustering.

## File Locations

```
openkeel/token_saver/
├── __init__.py
├── daemon.py              # Background cache/ledger server
├── dashboard.py           # Tkinter real-time monitor
├── ledger.py              # SQLite savings tracker
├── pricing.py             # Model cost comparison
├── report.py              # CLI savings report
├── summarizer.py          # Ollama client (gemma4:e2b)
├── test_local_edit.py     # LocalEdit test suite
├── engines/
│   ├── codebase_index.py      # File ranking/indexing
│   ├── context_prefill.py     # Session start context builder
│   ├── conversation_compressor.py  # Turn history compression
│   ├── gpu_tier.py            # GPU detection + tier system
│   ├── local_edit.py          # LLM-delegated file editing
│   ├── output_compressor.py   # Rule-based output compression
│   ├── predictive_cache.py    # Next-read prediction
│   ├── search_filter.py       # Grep/glob result ranking
│   └── task_router.py         # Task complexity classification
└── hooks/
    ├── pre_tool.py        # PreToolUse interception (main savings engine)
    ├── post_tool.py       # PostToolUse observation + caching
    └── session_start.py   # Session context prefill

~/.openkeel/
├── token_ledger.db        # SQLite savings database
├── token_saver_cache/     # Daemon file summary cache
├── token_saver_session.json
├── token_saver_conversation.jsonl
└── token_saver_daemon.pid
```

## Metrics (as of 2026-04-06)

- **272 sessions**, 2,481 events
- **5.9M tokens processed**, 2.3M saved (38.3% overall)
- **Top engines:** bash_compress (800K), prefill (966K), grep (148K), local_edit (26K)
- **$34+ saved** on Opus pricing
- **LocalEdit:** 18 successful edits, 81-99% reduction per edit
- **GPU Tier:** Currently Tier 1 (gemma4:e2b 5.1B on kaloth). Tier 2 (gemma4:26b on jagg) pending.
