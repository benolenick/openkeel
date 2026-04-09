"""Post-mortem session report generator.

Reads enforcement.log + scribe_state.json between two timestamps and
produces a structured analysis of the agent's behavior — research ratio,
brute force score, activity breakdown, knowledge injection effectiveness.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


OPENKEEL_DIR = Path.home() / ".openkeel"
ENFORCEMENT_LOG = OPENKEEL_DIR / "enforcement.log"


def _parse_enforcement_log(
    start_ts: float, end_ts: float
) -> list[dict]:
    """Read enforcement.log entries between two timestamps."""
    entries = []
    if not ENFORCEMENT_LOG.exists():
        return entries
    with ENFORCEMENT_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = entry.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
            except (ValueError, TypeError):
                continue
            if start_ts <= ts <= end_ts:
                entries.append(entry)
    return entries


def generate_report(
    start_time: float,
    end_time: float,
    history_entries: list[dict],
    scribe_state: dict | None = None,
    profile_name: str = "",
    goal_name: str = "",
    mode_name: str = "",
) -> str:
    """Generate a post-mortem session report.

    Args:
        start_time: Session start (epoch seconds)
        end_time: Session end (epoch seconds)
        history_entries: List of governance decisions from GUI history
        scribe_state: Scribe state dict (if available)
        profile_name: Active profile name
        goal_name: Active goal/mission name
        mode_name: Active mode name

    Returns:
        Formatted report string
    """
    # Parse enforcement log for the session window
    log_entries = _parse_enforcement_log(start_time, end_time)

    duration_min = (end_time - start_time) / 60.0
    start_str = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.fromtimestamp(end_time).strftime("%H:%M:%S")

    lines = []
    lines.append("=" * 70)
    lines.append("OPENKEEL POST-MORTEM SESSION REPORT")
    lines.append("=" * 70)
    lines.append(f"Session:  {start_str} — {end_str} ({duration_min:.0f} min)")
    if profile_name:
        lines.append(f"Profile:  {profile_name}")
    if goal_name:
        lines.append(f"Goal:     {goal_name}")
    if mode_name:
        lines.append(f"Mode:     {mode_name}")
    lines.append("")

    # --- Governance summary ---
    total = len(history_entries)
    blocked = sum(1 for e in history_entries if e.get("action") == "blocked")
    gated = sum(1 for e in history_entries if e.get("action") == "gated")
    allowed = sum(1 for e in history_entries if e.get("action") == "allowed")

    lines.append("GOVERNANCE SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  Total decisions:  {total}")
    lines.append(f"  Allowed:          {allowed}")
    lines.append(f"  Gated:            {gated}")
    lines.append(f"  Blocked:          {blocked}")
    if total > 0:
        lines.append(f"  Block rate:       {blocked/total*100:.0f}%")
    lines.append("")

    # --- Research ratio ---
    research_actions = 0
    tool_actions = 0
    research_gate_fires = 0
    for e in log_entries:
        rule_id = e.get("rule_id", "")
        action = e.get("action", "")
        if rule_id == "research-gate":
            research_gate_fires += 1
        # Count research from scribe state
    if scribe_state:
        research_actions = scribe_state.get("total_research", 0)
        tool_actions = scribe_state.get("call_count", 0) - research_actions

    lines.append("RESEARCH vs BRUTE FORCE")
    lines.append("-" * 40)
    if tool_actions + research_actions > 0:
        ratio = research_actions / max(tool_actions, 1)
        lines.append(f"  Research actions:    {research_actions}")
        lines.append(f"  Tool actions:        {tool_actions}")
        lines.append(f"  Research ratio:      {ratio:.2f} (target: >0.5)")
        lines.append(f"  Research gate fires: {research_gate_fires}")
        if ratio < 0.2:
            lines.append("  VERDICT: Heavy brute-forcing — agent rarely searched")
        elif ratio < 0.5:
            lines.append("  VERDICT: Below target — more searching needed")
        else:
            lines.append("  VERDICT: Good research discipline")
    else:
        lines.append("  No tool/research data available")
    lines.append("")

    # --- Repetition analysis ---
    lines.append("REPETITION ANALYSIS")
    lines.append("-" * 40)
    # Find consecutive same-tool runs from history
    tool_runs: dict[str, int] = {}
    max_consecutive = 0
    max_consecutive_tool = ""
    prev_tool = ""
    consecutive = 0
    for e in history_entries:
        cmd = e.get("command", "").strip()
        tool = cmd.split()[0] if cmd else ""
        tool = tool.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if tool:
            tool_runs[tool] = tool_runs.get(tool, 0) + 1
            if tool == prev_tool:
                consecutive += 1
                if consecutive > max_consecutive:
                    max_consecutive = consecutive
                    max_consecutive_tool = tool
            else:
                consecutive = 1
            prev_tool = tool

    if tool_runs:
        # Top 5 most used tools
        sorted_tools = sorted(tool_runs.items(), key=lambda x: x[1], reverse=True)
        lines.append("  Most used tools:")
        for tool, count in sorted_tools[:8]:
            lines.append(f"    {tool}: {count}x")
        lines.append(f"  Longest consecutive same-tool: {max_consecutive}x ({max_consecutive_tool})")
        if max_consecutive >= 5:
            lines.append("  WARNING: Significant brute-forcing detected")
    else:
        lines.append("  No tool usage data")
    lines.append("")

    # --- Activity breakdown ---
    lines.append("ACTIVITY BREAKDOWN")
    lines.append("-" * 40)
    activities: dict[str, list] = {}
    for e in log_entries:
        activity = e.get("activity", "")
        if activity:
            activities.setdefault(activity, []).append(e)
    if activities:
        for act_name, events in sorted(activities.items()):
            act_blocked = sum(1 for e in events if e.get("action") == "deny")
            act_allowed = sum(1 for e in events if e.get("action") == "allow")
            lines.append(f"  {act_name}: {len(events)} events ({act_allowed} allowed, {act_blocked} blocked)")
    else:
        lines.append("  No activity-level data in enforcement log")
    lines.append("")

    # --- Blocks and why ---
    block_events = [e for e in log_entries if e.get("action") == "deny"]
    if block_events:
        lines.append("BLOCKS")
        lines.append("-" * 40)
        # Group by rule_id
        by_rule: dict[str, list] = {}
        for e in block_events:
            rid = e.get("rule_id", "unknown")
            by_rule.setdefault(rid, []).append(e)
        for rid, events in sorted(by_rule.items(), key=lambda x: len(x[1]), reverse=True):
            msg = events[0].get("message", "")[:80]
            lines.append(f"  [{rid}] x{len(events)}: {msg}")
        lines.append("")

    # --- Knowledge injection ---
    gate_events = [e for e in log_entries if e.get("rule_id") == "research-gate"]
    if gate_events:
        lines.append("KNOWLEDGE INJECTION (Research Gate)")
        lines.append("-" * 40)
        lines.append(f"  Gate fired: {len(gate_events)} times")
        for e in gate_events:
            ts = e.get("timestamp", "?")[:19]
            msg = e.get("message", "")[:80]
            lines.append(f"    {ts}: {msg}")
        lines.append("")

    # --- Timeline (last 20 significant events) ---
    lines.append("TIMELINE (significant events)")
    lines.append("-" * 40)
    significant = [
        e for e in log_entries
        if e.get("action") in ("deny", "alert") or e.get("rule_id") == "research-gate"
    ]
    for e in significant[-20:]:
        ts = e.get("timestamp", "?")[11:19]
        action = e.get("action", "?")
        tool = e.get("tool", "?")
        msg = e.get("message", "")[:60]
        lines.append(f"  [{ts}] {action.upper():5s} {tool}: {msg}")
    if not significant:
        lines.append("  No significant events (all allowed)")
    lines.append("")

    lines.append("=" * 70)
    lines.append("END REPORT")
    lines.append("=" * 70)

    return "\n".join(lines)
