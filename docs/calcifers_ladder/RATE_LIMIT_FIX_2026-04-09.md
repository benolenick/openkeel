# Rate Limit Fix — Quick Reference

**Date:** 2026-04-09
**Status:** ✓ Deployed and verified
**File Modified:** `tools/token_saver_proxy.py`
**Service Restarted:** token-saver-proxy (127.0.0.1:8787)

---

## The Problem

You were hitting 429 rate limits repeatedly due to **bloated session contexts being routed to premium models**.

### Numbers
- **Apr 8**: 167k Opus tokens from 255 Opus calls in one day
- **Apr 7**: 101k Opus tokens
- **Total 2 days**: 268k tokens (likely hitting Anthropic's daily limit)

### Root Causes
1. **Fat contexts**: requests with 235k-750k body chars routed to Sonnet/Opus
2. **Deep conversations**: single calls with 105+ messages (e.g., 12:10:20 call)
3. **Fresh cache writes**: 243k cache_create on premium turns (expensive)
4. **Weak eviction**: only triggered on messages 20+ turns old
5. **Repeated retries**: after 429, same fat context retried again

---

## The Fix

### Four Changes to `tools/token_saver_proxy.py`

#### 1. Hard Body-Size Ceilings (NEW)
```python
SONNET_MAX_BODY_CHARS = 150000      # ~50k tokens
OPUS_MAX_BODY_CHARS = 300000        # ~100k tokens
HAIKU_MAX_BODY_CHARS = 50000
```
**What it does:**
- If body exceeds these limits → automatic downgrade to cheaper model
- Opus exceeded? → route to Haiku instead

#### 2. Smart Premium Downgrade (UPDATED)
In `_classify_model()`:
```python
for kw in FORCE_OPUS_KEYWORDS:
    if kw in low:
        # If body approaching Opus limit, downgrade to Sonnet
        if body_chars > OPUS_MAX_BODY_CHARS * 0.8:  # >240k chars
            return SONNET_MODEL
        return None  # Route to Opus normally
```
**What it does:**
- Keywords like "architect", "think hard", "security" still get premium routing
- BUT if body is 240k+ chars, downgrade to Sonnet instead of Opus
- Preserves user intent while protecting against 429s

#### 3. Aggressive History Eviction (UPDATED)
Updated `_evict_history(data, body_chars)`:
```python
if body_chars > SONNET_MAX_BODY_CHARS:           # >150k
    cutoff = max(0, n - 3)                       # Keep only last 3 messages
    evict_keep = 300                             # Truncate results to 300 chars
elif body_chars > SONNET_MAX_BODY_CHARS * 0.7:  # >105k
    cutoff = max(0, n - 10)                      # Keep last 10 messages
    evict_keep = 400                             # Truncate to 400 chars
else:                                            # Normal (old behavior)
    cutoff = n - EVICT_MIN_AGE                   # 20+ message cutoff
    evict_keep = 800
```
**What it does:**
- At 150k+ chars: drops everything except last 3 messages
- At 105k+ chars: keeps only last 10 messages, aggressive truncation
- Below 105k: old behavior (slow eviction)

#### 4. Pipeline Wiring (NEW)
In `_rewrite_body()`:
```python
orig_body_chars = len(body)  # Compute early
_classify_model(data, body_chars=orig_body_chars)      # Pass to router
_evict_history(data, body_chars=orig_body_chars)       # Pass to eviction
```
**What it does:**
- Body char count now flows through entire pipeline
- Both router and eviction use same metric for consistent decisions

---

## Expected Behavior After Fix

### Before
```
Session accumulates 750k chars
  ↓
"architect" keyword found
  ↓
Route to Opus (premium)
  ↓
429 Rate Limited ❌
  ↓
Retry with same fat context
  ↓
429 again ❌
```

### After
```
Session accumulates 750k chars
  ↓
Body size gate triggers
  ↓
Downgrade to Haiku (cheap) ✓
  ↓
Also evict old messages (now just 3 remain)
  ↓
No 429, task completes ✓
```

Or with "architect" keyword:
```
Session at 250k chars + "architect" keyword
  ↓
Body near Opus limit (80% threshold)
  ↓
Downgrade to Sonnet instead of Opus ✓
  ↓
Request goes through ✓
```

---

## Testing Done

✓ **Python syntax verified**
```bash
python3 -m py_compile /home/om/openkeel/tools/token_saver_proxy.py
```

✓ **Service restarted and responding**
```bash
systemctl --user restart token-saver-proxy
curl http://127.0.0.1:8787/
```

✓ **Changes wired through pipeline**
- `_evict_history()` now receives body_chars
- `_classify_model()` now receives body_chars
- Both use the same metric

---

## Monitoring & Debugging

### Check Proxy is Running
```bash
curl http://127.0.0.1:8787/
# Should return Anthropic API banner
```

### See Latest API Calls
```bash
tail -20 ~/.openkeel/proxy_trace.jsonl | python3 -m json.tool
```

### Find Calls with Body Size Info
```bash
python3 << 'EOF'
import json
from datetime import datetime, timedelta

with open(f"/home/om/.openkeel/proxy_trace.jsonl") as f:
    lines = f.readlines()

now = datetime.now()
one_hour_ago = now - timedelta(hours=1)

for line in lines[-50:]:
    try:
        entry = json.loads(line)
        ts = datetime.fromtimestamp(entry.get('ts', 0))
        if ts >= one_hour_ago:
            print(f"{ts.strftime('%H:%M:%S')} | "
                  f"{entry['req'].get('routed_model', '?')[:15]:15} | "
                  f"{entry['req'].get('orig_body_chars', 0):8,} chars")
    except:
        pass
EOF
```

### Check if Eviction Fired
Look for "evicted by token_saver_proxy" markers in tool results:
```bash
grep "evicted by token_saver_proxy" ~/.openkeel/proxy_trace.jsonl | wc -l
```

---

## Impact on Development

### What Gets Cheaper
- Long debugging sessions with fat context → downgraded to Haiku
- "Architect" decisions on large codebases → downgraded to Sonnet instead of Opus
- Reduces premium model pressure

### What Stays Premium
- Short "think hard" prompts on normal sessions → still Opus
- Keywords like "security" with small context → still get treated properly
- User intent preserved, just with safety guardrails

### What Disappears
- 429s from bloated contexts (expected)
- Repeated retries of same fat request
- Cache_create bloat on premium turns (eviction truncates old results)

---

## If You See Issues

### If proxy won't start:
```bash
systemctl --user status token-saver-proxy
journalctl --user -u token-saver-proxy -n 20
```

### If downgrades are too aggressive:
Adjust constants in `tools/token_saver_proxy.py`:
```python
SONNET_MAX_BODY_CHARS = 150000  # Increase if too aggressive
OPUS_MAX_BODY_CHARS = 300000    # Increase if good requests being downgraded
```

### If eviction isn't firing:
Check if `TSPROXY_NO_EVICT=1` is set:
```bash
grep TSPROXY ~/.config/systemd/user/token-saver-proxy.service.d/env.conf
```

---

## Related Docs
- `CHANGES_ROADMAP_2026-04-09.md` — Full context on all recent changes
- `claude_rate_limit_investigation_2026-04-09.md` — Original investigation that led to this fix
- `../token_saver_v6_final_pass_2026-04-07.md` — Token Saver v6 overview

