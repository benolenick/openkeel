# Claude CLI & Calcifer Ladder — Changes Roadmap

**Date:** 2026-04-09
**Purpose:** Central reference for recent architectural changes to token routing, Calcifer's Ladder, and rate limit protection

---

## Part 1: Rate Limit Protection (Token Saver v6 Refinement)

### What Changed
Fixed 429 rate limits from bloated session contexts being routed to premium models.

### File Modified
- `tools/token_saver_proxy.py`

### Key Changes

**1. Hard Body-Size Ceilings (NEW)**
```python
SONNET_MAX_BODY_CHARS = 150000      # ~50k tokens equivalent
OPUS_MAX_BODY_CHARS = 300000        # ~100k tokens equivalent
HAIKU_MAX_BODY_CHARS = 50000
```
- Prevents fat contexts (235k+ observed) from hitting premium models
- If exceeded, automatic downgrade to Haiku

**2. Smart Premium Downgrade (UPDATED)**
```python
# In _classify_model():
for kw in FORCE_OPUS_KEYWORDS:
    if kw in low:
        if body_chars > OPUS_MAX_BODY_CHARS * 0.8:  # 240k chars
            return SONNET_MODEL  # Downgrade
        return None  # Route to Opus
```
- "architect" / "think hard" keywords still get premium routing
- But downgrade to Sonnet if body approaching Opus limit
- Preserves user intent while protecting against 429s

**3. Aggressive History Eviction (UPDATED)**
Updated `_evict_history(data, body_chars)`:
- `body_chars > 150k`: evict everything except last 3 messages
- `body_chars > 105k`: evict older than 10 messages, keep only 300 chars per result
- `body_chars < 105k`: normal eviction (keep 800 chars)

Previously: only triggered on messages 20+ turns old with weak truncation

**4. Pipeline Wiring (NEW)**
Early computation in `_rewrite_body()`:
```python
orig_body_chars = len(body)  # Computed early
_classify_model(data, body_chars=orig_body_chars)
_evict_history(data, body_chars=orig_body_chars)
```
- Body char count now flows through entire routing pipeline
- Eviction and routing decisions based on same metric

### Why This Matters
- **Apr 8 burn**: 167k Opus tokens from 255 Opus calls in one day
- **Root cause**: calls like 12:10:20 had 105 messages in one conversation
- **Fat bodies**: requests hitting 235k-750k chars while routed to Sonnet/Opus
- **Fresh cache**: 243k cache_create writes on premium turns amplified the problem
- **Result**: repeated 429s and rate limit exhaustion

### Expected Impact
- No more 750k+ char bodies hitting premium models
- Aggressive eviction at 150k chars triggers earlier
- Premium keywords still work, but with safety downgrade
- Should eliminate repeated 429s from bloated contexts

### Testing Done
✓ Syntax verified
✓ Proxy restarted and responding at 127.0.0.1:8787
✓ Changes wired through full pipeline

---

## Part 2: Calcifer's Ladder Architecture

### What This Is
Calcifer's Ladder is the **routing system** that supervises Claude Code sessions and decides where to send work (Haiku for quick, Sonnet for normal, Opus for think-hard, local models, external APIs).

The "ladder" metaphor:
- **Rung 0** (bottom): Haiku — fast/cheap, for quick decision tasks
- **Rung 1**: Sonnet — default route, balanced
- **Rung 2**: Opus — "think hard", "architect", "security" keywords
- **Rung 3** (top): Local LLM (Ollama) or external specialists (GitHub Scout, etc.)

### Related Documents
- `../calcifer_ladder_design_2026-04-08.md` — Full design (routing rules, escalation logic)
- `architecture_scaffold_2026-04-08.md` — How supervisor loop + inner agent loops work
- `conversation_shapes_and_escalation_2026-04-08.md` — Patterns for when/how to escalate
- `intention_packet_2026-04-08.md` — Runtime object holding user intent across turns
- `agent_build_scaffold_2026-04-09.md` — Fractal agent swarm design (for complex tasks)

### Key Components

**1. Conductor (openkeel/calcifer/conductor.py)**
- Opus meta-agent that watches the Ladder
- Reads intent once (first turn)
- Intervenes when routes get stuck or solve wrong problem
- Routes escalation decisions back to Ladder

**2. Ladder Chat Window (openkeel/calcifer/ladder_chat.py)**
- GUI showing message routing in real-time
- 6 runner dials (Haiku, Sonnet, Opus, Local, Specialist, System)
- Token streaming visualization

**3. Brain (openkeel/calcifer/brain.py)**
- LLM wrapper + memory integration
- Pulls context from Hyphae, Kanban, research shards
- Builds context for each rung decision

**4. IntentionBroker (openkeel/calcifer/intention_broker.py)**
- Manages `IntentionPacket` (user's true goal, decoded once)
- Manages `SessionShard` (accumulated session state per turn)
- Stores in Hyphae for later recall

### Recent Wiring (Apr 8-9)
- **Conductor**: Now wired to watch routing decisions and intervene
- **Signal pipeline**: Fixed to emit directly (no QTimer wrapping)
- **Token streaming**: Wired for live response visibility
- **Session shards**: Connected to IntentionBroker for stateful recalls

### Current Status
- Ladder GUI: fully functional
- Routing matrix: 6 rungs + terminal/workshop endpoints
- E2E tests: 9/9 passing
- Next: proactive loop automation (nightly escalation timer, smartness scoring)

---

## Part 3: Claude Code Changes (via Hooks + Token Saver)

### What's Integrated Into Claude Code Sessions

**1. Token Saver Proxy (127.0.0.1:8787)**
- Intercepts all Claude API calls
- Routes to Sonnet/Opus/Haiku based on context
- Logs metrics to `~/.openkeel/proxy_trace.jsonl`
- Runs as systemd user service

**2. Hooks in `~/.claude/settings.json`**
These run **within** Claude Code at specific events:
- `SessionStart` — Initializes Hyphae recall, loads project context
- `UserPromptSubmit` — Pre-submits your prompt for analysis
- `PreToolUse` — Intercepts before tool execution (filters/logs)
- `PostToolUse` — Records what tools did
- `Stop` — Cleanup on session end

**3. Hyphae Integration (127.0.0.1:8100)**
- Stores facts about your work (Calcifer sessions, bugs found, decisions)
- Recalls on `SessionStart` to prime context
- Used by Calcifer brain to build rich decision context

### The Flow
```
Claude Code session starts
  ↓ SessionStart hook
  ↓ Hyphae recall (get project facts)
  ↓ You submit a prompt
  ↓ UserPromptSubmit hook (analysis)
  ↓ Claude routes to proxy
  ↓ Proxy classifies (Haiku/Sonnet/Opus)
  ↓ Proxy evicts history if needed (NEW)
  ↓ API call to Anthropic
  ↓ Response logged to proxy_trace.jsonl
  ↓ PostToolUse hook (record what happened)
```

### Recent Changes to This Flow
1. **History eviction now body-aware** — uses `orig_body_chars` for smarter truncation
2. **Premium downgrade on large bodies** — protects against 429s
3. **Conductor integration** — future: will intervene in stuck Ladder sessions

---

## Part 4: What Still Needs Documentation

### In Progress
- [ ] Fractal agent swarm design (agents in parallel on complex tasks)
- [ ] Smartness scoring CLI (for monitoring Ladder quality)
- [ ] Nightly escalation timer (automatic rung promotion)
- [ ] GUI consolidation (merge Ladder + Monitor into one view)

### Already Documented
- [x] Rate limit fix (THIS DOCUMENT)
- [x] Conductor loop (calcifer_ladder_design_2026-04-08.md)
- [x] Intention packet (intention_packet_2026-04-08.md)
- [x] Architecture scaffold (architecture_scaffold_2026-04-08.md)

---

## Part 5: Quick Reference — Files to Know

### Token Saver / Rate Limit
- `tools/token_saver_proxy.py` — The proxy router (MODIFIED Apr 9)
- `~/.openkeel/proxy_trace.jsonl` — Per-turn metrics log
- `CLAUDE.md` — Project instructions (mentions Token Saver v6)

### Calcifer & Ladder
- `openkeel/calcifer/conductor.py` — Meta-agent supervisor
- `openkeel/calcifer/ladder_chat.py` — GUI + routing
- `openkeel/calcifer/brain.py` — Context builder
- `openkeel/calcifer/intention_broker.py` — Session state management
- `openkeel/calcifer/ladder_window.py` — Resource monitor dials

### Hooks & Integration
- `~/.claude/settings.json` — Hook definitions
- `openkeel/token_saver/hooks/session_start.py` — Session init hook
- `openkeel/token_saver/hooks/pre_tool.py` — Tool interception hook
- `openkeel/token_saver/hooks/post_tool.py` — Tool logging hook

### Docs
- `docs/calcifers_ladder/` — Full architecture notes (this directory)
- `docs/token_saver_v6_final_pass_2026-04-07.md` — Token Saver v6 overview
- `CLAUDE.md` — Project-level instructions

---

## Part 6: Commands to Monitor / Debug

### Check Proxy Health
```bash
curl http://127.0.0.1:8787/  # Should return Anthropic API banner
```

### See Recent API Calls
```bash
tail -20 ~/.openkeel/proxy_trace.jsonl | python3 -m json.tool
```

### Check Hyphae Memory
```bash
curl -s http://127.0.0.1:8100/health
```

### Run Calcifer Ladder
```bash
python3 -m openkeel.calcifer.ladder_chat
```

### Monitor Token Saver Metrics
```bash
python3 -m openkeel.token_saver.one_metric  # Weekly pool_units (honest metric)
```

---

## Summary

**What Changed:**
1. Rate limit protection added to proxy (body-size ceilings + aggressive eviction)
2. Calcifer's Ladder supervisor loop + routing matrix fully wired
3. Integration between Claude Code hooks and Hyphae memory

**Why:**
- Stop 429s from bloated session contexts
- Supervise routing decisions and catch stuck patterns
- Build richer decision context via memory

**Status:**
- ✓ Rate limit fix deployed and tested
- ✓ Ladder architecture documented
- ✓ Integration wired and 9/9 tests passing
- ⏳ Next: proactive automation (escalation timer, smartness scoring)

