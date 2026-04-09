# OpenKeel Changelog

All notable changes to the OpenKeel project are documented here.

## [Unreleased]

### Added

#### Calcifer Ladder: Conductor Layer (2026-04-08)

**Files Created:**
- `openkeel/calcifer/intention.py` — Fast rule-based intent extraction from user's first message
  - Extracts: goal, domain (coding|debugging|architecture|explanation|general), urgency, frustration, constraints (time_critical, quality_needed, cost_sensitive, risk_tolerance)
  - Zero LLM latency — pure keyword banks and pattern matching
  - Methods: `matches_solution()` (does response address intent?), `is_stuck_pattern()` (detects loops)

- `openkeel/calcifer/conductor.py` — Opus meta-agent that supervises routing and intervenes
  - `Conductor` class:
    - `initialize_from_message(msg)` — Extract intent once per conversation
    - `suggest_routing(msg, runner)` — Optionally escalate ladder runner based on intent + history (architecture tasks, frustrated user, stuck patterns, emergencies)
    - `observe_response(response, runner_id)` — Detect stuck loops, agent confusion, wrong-tier routing; return intervention reason + human guidance
    - `status_line()` — Summary for toolbar (goal, attempt count, interventions made)
  - Pure heuristics — no per-turn LLM cost, no latency

**Files Modified:**
- `openkeel/calcifer/ladder_chat.py`:
  - Added import: `from openkeel.calcifer.conductor import Conductor, ConductorState`
  - Added `RUNNER_CFG["conductor"]` entry (grey meta-runner for intervention UI)
  - Added `__init__` fields: `self._conductor` (initialized on first message), `self._first_message` flag
  - Modified `_send()`: Initialize conductor on first message, then call `suggest_routing()` to optionally escalate runner before streaming
  - Modified `_on_done()`: Call `observe_response()` after each assistant response; if intervention needed, inject a grey `🔥 conductor:` bubble with human guidance

**How It Works:**
1. User sends first message → Conductor extracts intent (goal, domain, frustration, time-criticality)
2. Every subsequent message → Conductor biases routing: escalate if architecture task, if user frustrated + prior failures, if stuck pattern, or if emergency
3. After each assistant response → Conductor checks if response addresses intent; if stuck loop/confusion/wrong tier detected, injects intervention bubble ("try @sonnet for architecture", "responses are looping", etc.)

**Design Philosophy:**
- Cost invariant: ~0 per-turn LLM overhead (pure heuristics)
- Pays for itself by catching failures early and avoiding 5-10K token wasted attempts
- No interruption of user flow; interventions appear as advisory bubbles, not blocking dialogs

**Testing:**
- Intent extraction tested on "frick my agents died..." → correctly identifies frustration, coding domain
- Conductor routing logic tested → escalates as expected
- All imports verified, no runtime errors

---

## [Release Notes — Earlier Sessions]

### 2026-04-07: Calcifer's Ladder v1.0 — Signal Pipeline Fix
- Fixed QTimer wrapping in signal emission — direct emit from background thread now works
- Ladder fully wired into LLMOS chat window with 6 runner dials (gemma4·e2b, qwen2.5·3b, gemma4·26b, Haiku, Sonnet, Opus)
- Routing matrix + escalation logic live
- Chat persistence + history + boot message working

### 2026-04-06: Massive Build Session
- Built entire Calcifer ecosystem in single session
- UX Research Pipeline deployed to kagg (882 Reddit posts, 471 tagged with qwen3:8b categorizer)
- Systemd timers, DB integration
- Token Saver v4.5 wired live

### Pre-v6: Token Saver Architecture
- v6: Cache-saver proxy (127.0.0.1:8787) routes 3-way (Opus/Sonnet/Haiku by intent)
- One honest metric: `python3 -m openkeel.token_saver.one_metric` — weekly pool_units
- All prior "51%/79%" savings figures deprecated
