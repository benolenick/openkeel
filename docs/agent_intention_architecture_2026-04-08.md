# Agent Intention Architecture: Hyphae + IntentionPacket + Session Shards

**Date:** 2026-04-08
**Status:** Detailed design spec
**Purpose:** Enable agents to develop true intention—solving root causes, not symptoms

---

## Executive Summary

Current agents are **task-solvers**: they fix the immediate problem and forget. We need **goal-driven agents** that understand *why* problems recur and prevent them.

The system combines:
- **Hyphae**: Long-term fact database (sparse, strategic)
- **IntentionPacket**: Goal + invariant constraints (lives in Hyphae metadata)
- **Session Shards**: Ephemeral per-project memory (hypothesis evolution, attempts, tests)
- **Opus**: Memory architect (decides what matters, when to escalate, what to keep)

---

## 1. Memory Architecture

### 1.1 Long-Term Hyphae Node Structure

A problem node in Hyphae looks like:

```json
{
  "node_id": "desktop-monitor-off-stability",
  "node_type": "recurring_problem",
  "created": "2026-04-06T14:30:00Z",
  "last_updated": "2026-04-08T10:15:00Z",

  "intention_packet": {
    "goal_id": "desktop-monitor-off-stability",
    "user_request": "Desktop breaks every time I turn monitors off",
    "intended_outcome": "Desktop remains fully stable when monitors power down and back up. No manual intervention needed.",
    "why_it_matters": "Sleep cycle is predictable; corrupted desktop state is a blocker after 24hrs",

    "must_preserve": [
      "Session state persists across monitor power events",
      "No display corruption or graphical glitches",
      "Fix must hold across multiple sleep/wake cycles",
      "No data loss or file system corruption"
    ],

    "allowed_tradeoffs": [
      "Slower wake time (up to 2sec additional)",
      "One-time reconfiguration of display settings",
      "Restart of specific services (not full reboot)"
    ],

    "forbidden_tradeoffs": [
      "Destructive commands without explicit testing",
      "Treating symptoms while ignoring root cause",
      "Fix that works once then fails next cycle"
    ],

    "success_shape": [
      "Works for 7+ days without regression",
      "Cause is understood (not just 'mystery fixed')",
      "Prevention mechanism in place",
      "Future occurrences caught before user notices"
    ],

    "failure_shape": [
      "Fix works one cycle then regresses",
      "Agent never asks why previous fix didn't persist",
      "Root cause left undiagnosed",
      "Same fix gets applied 3+ times"
    ]
  },

  "problem_domain": "system_stability",
  "problem_class": "power_management_desktop",
  "tags": ["display", "wayland", "x11", "systemd-logind", "compositor"],

  "hypothesis_history": [
    {
      "hypothesis_v": 1,
      "text": "Display manager crashes on monitor-off event",
      "confidence": 0.2,
      "proposed_at": "2026-04-06T14:30:00Z",
      "reason": "Desktop corrupts when monitors off",
      "status": "rejected"
    },
    {
      "hypothesis_v": 2,
      "text": "GNOME Shell (compositor) loses state during pm-suspend",
      "confidence": 0.4,
      "proposed_at": "2026-04-07T09:00:00Z",
      "tests_run": [
        "Checked xrandr state before/after monitor-off → confirms loss",
        "Checked systemd-logind config → incomplete"
      ],
      "status": "investigating"
    },
    {
      "hypothesis_v": 3,
      "text": "xrandr display state not persisted through systemd-logind pm-suspend",
      "confidence": 0.7,
      "proposed_at": "2026-04-08T10:00:00Z",
      "root_cause_candidate": true,
      "status": "testing"
    }
  ],

  "attempts_made": [
    {
      "attempt_id": 1,
      "session_ref": "session-2026-04-06-001",
      "what": "systemctl restart graphical-session",
      "result": "works_this_turn",
      "next_cycle": "failed",
      "why_failed": "symptom fix, root cause still present",
      "confidence_impact": "rejected_hypothesis_1"
    },
    {
      "attempt_id": 2,
      "session_ref": "session-2026-04-07-001",
      "what": "Instrument pm-suspend hook to preserve xrandr state",
      "result": "in_testing",
      "confidence_impact": "upgraded_hypothesis_2_to_3"
    }
  ],

  "prevention_plan": {
    "status": "not_started",
    "plan": [
      "Once root cause confirmed: add systemd-logind hook to preserve xrandr state",
      "Set up monitor for pm-suspend events",
      "Automated test: turn monitors off/on 5x, verify display state"
    ]
  },

  "closure_status": "open",
  "closure_criteria": {
    "all_must_pass": [
      "Root cause identified and verified",
      "Fix passes 7-day regression test",
      "Prevention mechanism in place",
      "Test harness automated"
    ]
  }
}
```

**Key insight:** The IntentionPacket is *embedded* in the node. It stays constant while hypothesis and confidence evolve. This prevents drift.

### 1.2 Session Shard Structure

Each problem-solving session creates an ephemeral shard (markdown or JSON):

```markdown
# Session Shard: desktop-monitor-off-2026-04-08

## Metadata
- session_id: session-2026-04-08-001
- problem_node: desktop-monitor-off-stability
- opus_architect: claude-opus-4-6
- started: 2026-04-08T10:00:00Z
- ttl: 7 days (auto-merge to long-term after project close)

## IntentionPacket (loaded from long-term)
[Linked reference to intention_packet above]
must_preserve: [Session state, no corruption, ...]
failure_shape: [Works once then fails, ...]

## Current Hypothesis
**Text:** xrandr display state not persisted through pm-suspend
**Confidence:** 0.7 (upgraded from 0.4 based on xrandr tests)
**Why:** xrandr shows state loss on monitor-off event
**Next test:** Instrument systemd-logind hook to capture exact moment of loss

## Evidence This Session
- [ ] Ran: `xrandr` before monitor-off → output: `HDMI-1 connected primary 3840x2160+0+0`
- [ ] Ran: `xrandr` after monitor-off → output: `HDMI-1 disconnected`
- [ ] Found: systemd-logind config does not persist display state
- [ ] Hypothesis: Loss happens during pm-suspend event

## Attempts This Session
### Attempt 1: Instrument pm-suspend hook
```bash
# Add to /etc/systemd/system-sleep/preserve-xrandr.sh
# Save xrandr state before suspend, restore after
```
- Status: In testing
- Next: Run 5x monitor-off/on cycle, check if state persists
- Expected: HDMI resolution maintained across suspend

## Test Protocol
1. Record initial xrandr state
2. Turn monitors off (triggers pm-suspend)
3. Turn monitors back on
4. Check xrandr state
5. Repeat 5x
6. Check if state stable after 7 days

## Pending
- [ ] Test result from preserve-xrandr.sh fix
- [ ] If passes: add to /etc/systemd/system-sleep/
- [ ] If fails: escalate to Opus for deeper investigation

## Decision Points
- If test passes after 7 days → Move to "prevention_plan" in long-term node
- If test fails → Root cause hypothesis was wrong, escalate
```

**TTL Model:**
- Session shard lives in `.tmp/` or `/var/openkeel/sessions/`
- Auto-purged after project close OR 7 days
- On project close: Opus reviews and merges strategic learnings back to long-term node

### 1.3 Linking & Retrieval

```
Long-Term Hyphae (persistent)
├── desktop-monitor-off-stability
│   ├── intention_packet
│   ├── hypothesis_history
│   ├── attempts_made [with session_refs]
│   └── prevention_plan
│
└── [linked to session shards by goal_id]

Session Shards (ephemeral, .tmp/)
├── session-2026-04-08-001
│   ├── hypothesis (current)
│   ├── tests (incremental)
│   ├── evidence (this session only)
│   └── decision_points (for Opus)
```

**Retrieval flow:**
1. User: "Fix my desktop"
2. Broker: Recalls `desktop-monitor-off-stability` from Hyphae
3. Opus: Reads intention_packet + hypothesis_history
4. Opus: Checks if active session shard exists; if not, creates new one
5. Opus: Populates session shard with "here's what we know, here's next test"
6. Worker: Executes in session context
7. On completion: Opus merges back to long-term node

---

## 2. Intention Flow

### 2.1 Initial Request

```
User: "My desktop keeps breaking when monitors turn off"

↓ Broker extracts:
- domain: "system_stability"
- urgency: 3 (broken, but not emergency)
- frustration: 5 (happens repeatedly)

↓ Broker checks Hyphae:
- Found: desktop-monitor-off-stability node
- Has intention_packet? Yes
- Hypothesis history? 3 versions

↓ Opus reads intention_packet:
- "This is NOT a random request—it's a recurring problem"
- "Last fix didn't hold—need root cause, not symptom patch"
- "Failure shape: 'works once then fails'"
- Confidence in current hypothesis: 0.7

↓ Opus creates/resumes session shard:
- Check for active session shard
- If none: create new with hypothesis v3
- If exists: continue where last session left off

↓ Opus populates directive:
"Run the 5x monitor-off/on test with preserve-xrandr.sh.
If test passes, set up permanent systemd hook.
If test fails, we were wrong—escalate to root cause analysis."
```

### 2.2 How IntentionPacket Guides Routing

Before: Agent routes by urgency → Haiku (cheap, fast)
After: Agent checks intention_packet

```python
def suggest_routing(intention_packet, default_runner):
    runner = default_runner

    # Check: Is this a recurring pattern?
    if intention_packet.failure_shape:
        # High risk: previous attempts failed
        # Escalate to stronger reasoning
        runner = escalate(runner, 1)

    # Check: Are must_preserve items risky?
    if "no data loss" in intention_packet.must_preserve:
        runner = escalate(runner, 1)

    # Check: Is this root cause or symptom?
    if "forbidden_tradeoffs" includes "fixing symptoms":
        runner = escalate(runner, 1)

    return runner
```

**Result:** Desktop monitor-off problem routes to **Sonnet** (not Haiku) because:
- Previous fix failed (recurring pattern)
- Data integrity at risk
- Symptom-patching is forbidden

### 2.3 Completion Criteria

Old completion: "systemctl succeeded"
New completion: Check against intention_packet

```python
def evaluate_completion(session_shard, intention_packet):
    passed = []
    failed = []

    for criterion in intention_packet.success_shape:
        if criterion == "Works for 7+ days without regression":
            if session_shard.test_duration >= 7 and not session_shard.regression:
                passed.append(criterion)
            else:
                failed.append(criterion)

        if criterion == "Cause is understood":
            if session_shard.current_hypothesis.confidence > 0.8:
                passed.append(criterion)
            else:
                failed.append(criterion)

        if criterion == "Prevention mechanism in place":
            if session_shard.prevention_mechanism:
                passed.append(criterion)
            else:
                failed.append(criterion)

    if failed:
        return CompletionStatus.BLOCKED
    return CompletionStatus.COMPLETE
```

---

## 3. Opus as Memory Architect

### 3.1 At Session Start

Opus:
1. **Reads intention_packet** → Understand what can't be sacrificed
2. **Reviews hypothesis_history** → See what was tried, why it failed
3. **Assesses current confidence** → 0.7? Still investigating. 0.2? Completely wrong approach.
4. **Populates session shard structure** → Create template for this session's work
5. **Emits first directive** → "Here's the experiment to run next"

```json
{
  "opus_session_init": {
    "goal_id": "desktop-monitor-off-stability",
    "current_hypothesis": "xrandr state loss on pm-suspend (v3, confidence 0.7)",
    "last_failed_attempt": "systemctl restart (symptom fix, v1)",
    "why_last_failed": "Didn't address root cause, regressed in 24h",

    "this_session_plan": [
      "Run 5x monitor-off/on test with preserve-xrandr.sh",
      "If passes: add hook to /etc/systemd/system-sleep/",
      "If fails: hypothesis is wrong—escalate"
    ],

    "escalation_trigger": "If test fails, must investigate pm-suspend kernel logs",

    "success_criteria": [
      "Test passes 5 consecutive times",
      "State persists after full reboot",
      "Passes 7-day regression test"
    ]
  }
}
```

### 3.2 During Session (Loop Detection)

Opus watches for loops:

```python
def detect_symptom_loop(session_shard, intention_packet):
    """Check if we're just patching symptoms."""

    if len(session_shard.attempts) > 2:
        last_3_attempts = session_shard.attempts[-3:]

        # Are we applying the same or similar fixes?
        if all_similar(last_3_attempts):
            reason = "Applied same fix type 3 times—we're in a symptom loop"
            return (True, reason)

    # Did the previous session's fix fail?
    if session_shard.previous_session_fix_regressed:
        reason = "Previous fix didn't hold—we're not addressing root cause"
        return (True, reason)

    # Are we avoiding investigation?
    if not session_shard.root_cause_investigation_attempted:
        if session_shard.hypothesis_confidence < 0.6:
            reason = "Low confidence in hypothesis—need investigation, not patching"
            return (True, reason)

    return (False, None)
```

**When loop detected:**
- Opus injects intervention: "We're symptom-patching. Let's escalate to root cause analysis."
- Escalation path: Deeper investigation (kernel logs, driver states, etc.)
- Session shard gets flagged: "Root cause analysis required"

### 3.3 On Project End (Merge Decision)

Opus decides what gets bubbled to long-term Hyphae:

```python
def merge_session_to_longterm(session_shard, longterm_node):
    """Decide what learnings are worth keeping."""

    # If hypothesis confidence grew: upgrade in long-term
    if session_shard.hypothesis.confidence > longterm_node.current_hypothesis.confidence:
        longterm_node.hypothesis_history.append({
            "hypothesis": session_shard.hypothesis.text,
            "confidence": session_shard.hypothesis.confidence,
            "tests_run": session_shard.tests_run,
            "evidence": session_shard.evidence,
            "status": "upgraded"
        })

    # If root cause found: move prevention plan to active
    if session_shard.root_cause_confirmed:
        longterm_node.prevention_plan.status = "implementation"

    # Compress session details: keep why it mattered, discard trial-and-error
    summary = {
        "session_ref": session_shard.id,
        "key_finding": session_shard.breakthrough,
        "tests_that_mattered": [t for t in session_shard.tests if t.was_decisive],
        "next_step": session_shard.next_step
    }
    longterm_node.session_summaries.append(summary)

    # Archive full session shard for audit trail
    longterm_node.session_archive.append(session_shard.id)
```

---

## 4. Hypothesis Evolution

### 4.1 Confidence Upgrading

Confidence is NOT subjective. It upgrades when evidence accumulates:

```
Hypothesis v1 (confidence 0.2):
"Display manager crashes on monitor-off"
Evidence: Desktop corrupts when monitors off
Confidence low because: Correlation ≠ causation

Hypothesis v2 (confidence 0.4):
"GNOME Shell (compositor) loses state during pm-suspend"
New evidence: Checked xrandr state before/after
Result: xrandr DOES lose state on monitor-off
Confidence: 0.4 (partial confirmation, but mechanism unknown)

Hypothesis v3 (confidence 0.7):
"xrandr state not persisted through systemd-logind pm-suspend"
New evidence:
  - xrandr shows state loss (confirmed)
  - systemd-logind config incomplete (checked)
  - Instrumented hook preserves state (testing)
Confidence: 0.7 (strong evidence, pending full test pass)

After 7-day test: Confidence → 0.95
"This is the root cause. Fix is proven."
```

### 4.2 Preventing Hypothesis Loss

Each hypothesis lives in `hypothesis_history`. Never discarded, always linked:

```json
{
  "hypothesis_v": 1,
  "text": "Display manager crashes",
  "confidence": 0.2,
  "status": "rejected",
  "why_rejected": "xrandr state loss confirmed—root cause is deeper",
  "evidence_that_rejected_it": ["xrandr before/after test showed state persists when DM restarted"]
}
```

**When agent recalls:** It sees the full evolution. No amnesia.

---

## 5. Root Cause Detection

### 5.1 Symptom vs. Root Cause

System asks: **Does this fix address the origin of the failure, or just silence the alarm?**

```
Symptom Fix: systemctl restart graphical-session
├─ Result: Works immediately
├─ Why works: Restarts the corrupted service
├─ Why not root: Original corruption still happens
└─ Predicted: Fails same way next monitor-off event ✓ (confirmed)

Root Cause Fix: Persist xrandr state through pm-suspend
├─ Result: Prevents corruption from happening
├─ Why works: Addresses the pm-suspend state loss at source
├─ Prediction: Should not fail on next cycle
└─ Status: Testing
```

### 5.2 Automatic Loop Detection Signals

**CRITICAL: Detection is predictive, not reactive.** Catch loops BEFORE wasting time.

```python
def detect_symptom_loop(session_shard, longterm_node):
    """Detect if we're in a loop BEFORE the 3rd failed attempt."""

    # Signal 1: Previous session's fix regressed (most reliable signal)
    if (longterm_node.last_session_fix and
        longterm_node.last_session_fix.regressed):
        return (True, "previous_fix_regressed",
                "Last session's fix didn't hold. Root cause still active.")

    # Signal 2: Same hypothesis, same low confidence as last session
    if (session_shard.current_hypothesis.text ==
        longterm_node.previous_session_hypothesis.text):
        if longterm_node.previous_session_hypothesis.confidence < 0.6:
            return (True, "stalled_hypothesis",
                    f"Same hypothesis ({longterm_node.previous_session_hypothesis.confidence} confidence) "
                    f"hasn't grown. Approach isn't working.")

    # Signal 3: About to apply a fix that's in history
    if session_shard.proposed_fix in [a["what"] for a in longterm_node.attempts_made]:
        return (True, "fix_already_tried",
                f"'{session_shard.proposed_fix}' was already attempted. Why retry?")

    # Signal 4: Hypothesis confidence > 0.5 but no test protocol defined
    if (session_shard.current_hypothesis.confidence > 0.5 and
        not session_shard.test_protocol_defined):
        return (True, "untested_hypothesis",
                "High confidence but no test plan. Can't close without evidence.")

    # Signal 5: Hypothesis confidence stalled (< 0.05 improvement in 2 sessions)
    if len(longterm_node.hypothesis_history) > 2:
        recent_growth = (longterm_node.hypothesis_history[-1].confidence -
                        longterm_node.hypothesis_history[-2].confidence)
        if recent_growth < 0.05 and longterm_node.hypothesis_history[-1].confidence < 0.8:
            return (True, "confidence_stalled",
                    f"Confidence barely growing ({recent_growth:.2f} in last session). "
                    f"Need to pivot approach.")

    return (False, None, None)
```

**When loop detected:**
- Opus injects intervention: "🔥 conductor: [reason]. Escalating to root cause analysis."
- Routing escalates one tier (Haiku→Sonnet, Sonnet→Opus)
- Session flags: `escalation_reason`, `escalation_timestamp`
- New directive: "Stop patching. Investigate root cause. Build test protocol."

---

## 6. Prevention

### 6.1 Prevention Mechanism Template

Once root cause found, Opus generates prevention:

```json
{
  "root_cause": "xrandr state not persisted through pm-suspend",
  "prevention": {
    "mechanism": "systemd-logind hook script",
    "implementation": "/etc/systemd/system-sleep/preserve-xrandr.sh",
    "test_harness": "Monitor pm-suspend events, verify xrandr state persists",
    "persistence": "survives reboot, runs on every suspend event"
  },
  "instrumentation": {
    "monitor_for": "xrandr state mismatch on resume",
    "alert_if": "xrandr state != saved state after resume",
    "action_if_detected": "Log to /var/log/xrandr-monitor.log, alert user before corruption"
  }
}
```

### 6.2 Persistence Across Reboots

### 6.2 Deployment Workflow (5 Phases)

**Phase 1: Draft & Review**
- Opus generates mechanism design + implementation code + test harness + rollback plan
- User reviews before deployment

**Phase 2: Test in Isolation**
- Deploy to test environment (or staging marker on user's machine)
- Run test harness 5x in controlled conditions
- Verify no side effects, measure performance impact
- Example: 5 consecutive suspend/resume cycles, verify xrandr state persists

**Phase 3: Deploy to Production**
- Deploy mechanism permanently (/etc/systemd/system-sleep/preserve-xrandr.sh)
- Set up instrumentation/monitoring
- Document: "Date X: Deployed [mechanism] to prevent [root cause]"
- Start 7-day regression test

**Phase 4: Instrumentation**
- Monitor pm-suspend events in systemd journal
- Alert if xrandr state loss detected (before visible corruption)
- Log every resume: "xrandr state preserved ✓" or "ALERT: state mismatch"

**Phase 5: Regression Testing (7+ days)**
- Automated daily: trigger 5x monitor-off/on cycles
- Verify desktop remains stable
- Alert on any regression
- Project closes only after 7 clean days

**Key insight:** Prevention isn't deployed until root cause confidence > 0.85. And deployment isn't "done" until it survives 7 days. This prevents the "fix that works once then fails" pattern.

---

## 7. Completion & Closure

### 7.1 Completion vs. "Symptom Patched"

Old completion: Task done = exit
New completion: Intention preserved?

```python
def can_close_project(session_shard, intention_packet):
    checks = {
        "root_cause_identified": hypothesis.confidence > 0.8,
        "fix_holds_across_cycles": test_passed_5_consecutive_times,
        "regression_test_passed": test_passed_7_days,
        "prevention_in_place": prevention_script.deployed,
        "must_preserve_honored": all(preserved for preserved in intention_packet.must_preserve),
        "failure_shape_avoided": not any_failure_shape_triggered,
    }

    if all(checks.values()):
        return ProjectStatus.CLOSED
    else:
        failed = [k for k,v in checks.items() if not v]
        return ProjectStatus.OPEN_BLOCKERS(failed)
```

Desktop monitor-off example:

```
Can close when:
✅ Root cause confirmed (xrandr state loss, confidence 0.95)
✅ Fix tested 5 consecutive monitor-off cycles
✅ 7-day regression test passed
✅ Prevention hook installed
✅ Instrumentation in place
✅ No data corruption (must_preserve honored)
✅ Didn't just restart services (failure_shape avoided)
```

### 7.2 Closure Snapshot

When project closes, Opus creates a summary:

```markdown
# Project Closure: desktop-monitor-off-stability

**Status:** CLOSED
**Duration:** 3 sessions over 8 days
**Final confidence:** 0.95

## Root Cause
xrandr display state lost during systemd-logind pm-suspend

## Solution
Added /etc/systemd/system-sleep/preserve-xrandr.sh:
- Saves xrandr state before suspend
- Restores xrandr state after resume
- Prevents display corruption on monitor power events

## Testing
- 5x monitor-off/on cycles: ✅ passed
- 7-day regression test: ✅ passed
- Reboot persistence: ✅ verified

## Prevention
Instrumentation monitors:
- pm-suspend events
- xrandr state consistency
- Alerts if mismatch detected

## Why This Fixes It
Previous sessions applied symptom fixes (systemctl restart).
This session found the root cause (xrandr state loss) and prevented it.
The fix addresses the origin, not the symptom.
Prevention ensures it never regresses.

## Lessons
- Monitor power events are subtle (state loss wasn't obvious)
- xrandr state must be explicitly managed across pm-suspend
- Systemd hooks are the right place for this fix
```

---

## 8. Complete Flow: Desktop Monitor-Off Example

### Session 1: 2026-04-06 (Initial diagnosis)

```
User: "Desktop broken when monitors off again"

1. Broker recalls hyphae node: desktop-monitor-off-stability
2. Opus reads: intention_packet (must preserve state, no corruption)
3. Opus reads: hypothesis_history (v1 rejected, v2 investigating)
4. Opus creates: session-2026-04-06-001

Opus directive:
"Desktop corrupts when monitors off. Last attempt (systemctl) failed.
Run xrandr before/after monitor-off to see what changes."

Session result:
- xrandr before: HDMI-1 connected 3840x2160
- xrandr after: HDMI-1 disconnected
- Finding: xrandr state IS lost on monitor-off
- Upgraded hypothesis from 0.2 → 0.4
```

### Session 2: 2026-04-07 (Hypothesis refinement)

```
User: "It broke again overnight"

1. Broker recalls hyphae node
2. Opus reads: hypothesis v2 (0.4 confidence)
3. Opus checks: session-2026-04-06-001 → xrandr state loss confirmed
4. Opus creates: session-2026-04-07-001

Opus directive:
"xrandr state loss confirmed. Now investigate WHY.
Check: systemd-logind config, pm-suspend hooks, compositor state."

Session result:
- Found: systemd-logind doesn't persist display config
- New hypothesis v3: "State lost in pm-suspend event"
- Confidence: 0.7
- Plan: Instrument pm-suspend hook to preserve state
```

### Session 3: 2026-04-08 (Root cause + prevention)

```
User: "It broke again this morning"

1. Broker recalls hyphae node
2. Opus reads: hypothesis v3 (0.7 confidence)
3. Loop detection: TRIGGERED
   - Reason: Same failure for 3rd session, previous fix didn't hold
4. Escalation: ROOT_CAUSE_INVESTIGATION
5. Opus creates: session-2026-04-08-001

Opus directive:
"We're in a loop: same failure, same band-aid approach.
Hypothesis v3 is probably right (0.7 confidence).
Let's test it: build systemd hook to preserve xrandr before suspend."

Session result:
- Built: /etc/systemd/system-sleep/preserve-xrandr.sh
- Tested: 5x monitor-off/on cycles → ALL PASS
- Confidence: 0.95 (root cause confirmed)
- Next: 7-day regression test, then close

Opus merge decision:
- Hypothesis v3 is winner (0.95 confidence)
- Prevention mechanism ready
- Move to 7-day monitoring phase
```

### Session 4: 2026-04-15 (Closure)

```
User: "Desktop hasn't broken since last week"

1. Opus checks: 7-day regression test → PASSED
2. Closure criteria: ALL MET
   ✅ Root cause identified (xrandr, 0.95 confidence)
   ✅ Fix tested and holds (5 consecutive cycles)
   ✅ 7-day regression passed
   ✅ Prevention in place
   ✅ Instrumentation live

Opus closes project:
- Archives session-2026-04-08-001 to long-term
- Updates intention_packet.closure_status = "closed"
- Summarizes: "Root cause: pm-suspend xrandr loss. Solution: systemd hook."
- Future queries for "desktop monitor" will see this closed project + prevention
```

---

## 9. Key Invariants

**These MUST be true for intention to be preserved:**

1. **IntentionPacket is immutable** — Changes only with Opus approval, never silently reinterpreted by workers

2. **Hypothesis confidence increases with evidence, never jumps** — 0.2 → 0.4 → 0.7 → 0.95, never 0.2 → 0.9

3. **Session shards are ephemeral** — Discarded after TTL or project close (never pollute long-term memory)

4. **Root cause investigation is mandatory** — Can't close if hypothesis confidence < 0.8 and failure_shape includes "treating symptoms"

5. **Prevention is verified** — Fix must pass regression test before project closes

6. **Failure shapes block completion** — If any failure_shape was triggered, project stays open

7. **Opus is the authority** — Only Opus can update intention, merge sessions, close projects

---

## 10. Open Questions & Trade-offs

### Q1: Session Shard Storage
**Where should session shards live?**
- Option A: `.tmp/` (ephemeral, fast access)
- Option B: `~/.openkeel/sessions/` (persistent, archived)
- Option C: Both (current in .tmp/, archive in ~/.openkeel/)

**Recommendation:** Option C. Current session in .tmp/, archived at project close.

### Q2: Hypothesis Confidence Calibration
**How exactly does confidence grow?**

**REFINED ANSWER:** Multi-factor confidence model:

```
confidence = (evidence_weight × 0.4) + (prediction_accuracy × 0.4) + (parsimony × 0.2)

evidence_weight:
  - 0.0: Purely speculative (observed symptom, no mechanism)
  - 0.3: Single test passed (xrandr shows state loss)
  - 0.6: Multiple independent tests confirm mechanism (xrandr + systemd-logind + kernel logs)
  - 0.9: Mechanism fully understood and documented

prediction_accuracy:
  - 0.0: Hypothesis made wrong prediction (thought xrandr was fine, it wasn't)
  - 0.5: Hypothesis made neutral prediction (no clear prediction was made)
  - 0.8: Hypothesis correctly predicted an outcome
  - 1.0: Hypothesis correctly predicted multiple independent outcomes

parsimony:
  - Simple hypothesis (single root cause) → 1.0
  - Complex hypothesis (multiple interacting causes) → 0.5
  - Overly complex (3+ causes) → 0.2
  - Contradicts evidence → 0.0
```

**Practical evolution for desktop example:**

```
v1: "Display manager crashes"
  evidence: 0.1 (just observation of corruption)
  prediction: 0.0 (systemctl restart worked, so prediction was wrong)
  parsimony: 1.0
  confidence = (0.1 × 0.4) + (0.0 × 0.4) + (1.0 × 0.2) = 0.24 → rounds to 0.2

v2: "GNOME Shell loses state during pm-suspend"
  evidence: 0.4 (xrandr confirms state loss)
  prediction: 0.6 (if state loss, then persisting it should help — pending)
  parsimony: 1.0
  confidence = (0.4 × 0.4) + (0.6 × 0.4) + (1.0 × 0.2) = 0.4

v3: "xrandr state not persisted through systemd-logind pm-suspend"
  evidence: 0.8 (xrandr + systemd-logind config + kernel logs all confirm)
  prediction: 0.95 (if we preserve state before suspend, it should restore correctly — test in progress)
  parsimony: 1.0
  confidence = (0.8 × 0.4) + (0.95 × 0.4) + (1.0 × 0.2) = 0.78 → rounds to 0.8

v3-after-7day-test: confidence = 0.95 (prediction nailed it: 7 days, no regression)
```

### Q3: When to Escalate?
**Escalate to Opus/Sonnet when:**
- Hypothesis confidence < 0.6 AND symptoms recurring?
- OR after 2 failed fix attempts?
- OR when failure_shape is triggered?

**Recommendation:** All three. Combined signal = escalate.

### Q4: Hyphae Scaling
**At 50K nodes, how fast is retrieval?**
- Needs indexing by: goal_id, domain, tags, confidence
- Needs semantic search for "similar problems"
- Needs TTL handling for stale hypotheses

**Recommendation:** Build hierarchical index (by domain) + semantic embeddings (for discovery).

### Q5: Prevention Persistence
**How do we know prevention survived a reboot?**
- Check: systemd hook file exists?
- Check: Hook was executed on last suspend?
- Check: xrandr state actually persisted?

**Recommendation:** Multi-level verification: file existence + execution log + state verification.

---

## Implementation Priority

1. **Phase 1:** Intention packet extraction + session shard creation (week 1)
2. **Phase 2:** Hypothesis tracking in long-term Hyphae (week 2)
3. **Phase 3:** Loop detection + escalation signals (week 2)
4. **Phase 4:** Root cause verification + prevention mechanism (week 3)
5. **Phase 5:** Regression testing + closure criteria (week 3)
6. **Phase 6:** Opus memory architect logic (week 4)

---

## 11. Session Merge Protocol (Critical Detail)

When a session ends, NOT everything goes into long-term. Opus decides what generalizes.

### 11.1 Merge Decision Tree

**Question 1: Did confidence grow?**
- YES → Add new hypothesis version to hypothesis_history
- NO → Archive session for audit, don't merge (learning was negative/null)

**Question 2: Was root cause found?**
- YES (confidence > 0.85) → Update prevention_plan.status = "implementation"
- NO → Mark prevention_plan as "still_investigating", link current session

**Question 3: Was prevention deployed and tested?**
- YES (7-day regression passed) → Close project, archive session
- IN PROGRESS (testing) → Keep prevention_plan.status = "testing", link test session
- NO → Keep prevention_plan.status = "blocked"

**Question 4: What evidence matters?**
- Only the tests that were decisive
- Only the measurements that confirmed/rejected hypothesis
- Dead ends stay in session archive for audit, don't pollute long-term

### 11.2 Example: Desktop Monitor-Off Merge

**Session 1 (2026-04-06):**
```
Hypothesis v1 → v2 (0.2 → 0.4)
Evidence: "xrandr shows state loss"
Merge decision: Hypothesis grew, add v2 to history
Archive: session-2026-04-06-001.md (full trial-and-error)
```

**Session 2 (2026-04-07):**
```
Hypothesis v2 → v3 (0.4 → 0.8)
Evidence: "xrandr + systemd-logind config confirm mechanism"
Root cause: Suspected but not fully confirmed
Merge decision: Hypothesis grew significantly, add v3 to history
Prevention plan: Mark as "still_investigating"
Archive: session-2026-04-07-001.md
```

**Session 3 (2026-04-08):**
```
Hypothesis v3 confirmed (0.8 → 0.95)
Root cause: CONFIRMED
Prevention mechanism: Built and tested 5x
Merge decision:
  - Add final hypothesis v3 to history (confidence 0.95)
  - Update prevention_plan: status = "implementation", code = preserve-xrandr.sh
  - Start 7-day regression test
Archive: session-2026-04-08-001.md
```

**Session 4 (2026-04-15, after 7-day test passes):**
```
Regression test: PASSED (7 days, no issues)
Prevention: Still running, state persists
Merge decision:
  - Final confidence: 0.95 (still in hypothesis_history)
  - Prevention status: DEPLOYED
  - Close project: closure_status = "closed", closure_date = 2026-04-15
Archive: Full session shard moves to ~/.openkeel/session_archive/

Long-term node now shows:
  - Root cause: "xrandr state loss during pm-suspend" (confirmed)
  - Prevention: "systemd-logind hook script" (deployed + tested)
  - Closure: "closed 2026-04-15"
  - Future queries: "If user says desktop breaks on monitor-off again, check prevention is still in place"
```

---

## Conclusion

This architecture turns Hyphae from a **fact database** into a **learning system**.

By pairing:
- **IntentionPacket** (what matters)
- **Session shards** (how understanding evolves)
- **Hypothesis history** (what was tried, why it worked/failed)
- **Opus architect** (deciding what to keep, when to escalate)

We enable agents to:
- ✅ Understand why problems recur
- ✅ Propose root causes instead of symptoms
- ✅ Prevent future failures, not just fix current ones
- ✅ Learn across sessions and projects
- ✅ Know when they're in a loop and escalate

The desktop monitor problem becomes: not a one-off fix, but a solved root cause with prevention in place forever.
