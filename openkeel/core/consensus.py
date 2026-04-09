"""Consensus Gate — aggregates Cartographer alerts and Pilgrim findings.

Decides when to inject observations into the executor agent.
Three injection modes:
  - QUEUE: findings are queued, shown when executor asks for help
  - NUDGE: findings appear in status bar / side panel
  - INTERRUPT: findings are injected directly into the agent's context
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openkeel.core.pilgrim import PilgrimReport, BlindSpot, report_to_dict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ConsensusConfig:
    """Thresholds for the consensus gate."""
    nudge_threshold: int = 6       # severity >= this → show in UI
    interrupt_threshold: int = 9   # severity >= this → inject into agent
    min_blind_spots_for_interrupt: int = 2  # need N+ spots to interrupt
    cooldown_seconds: int = 120    # min time between interrupts
    require_both_observers: bool = False  # if True, need agreement from both models


# ---------------------------------------------------------------------------
# Consensus state
# ---------------------------------------------------------------------------

@dataclass
class ConsensusState:
    """Tracks observer findings and injection decisions."""
    cartographer_alerts: list[str] = field(default_factory=list)
    pilgrim_reports: list[PilgrimReport] = field(default_factory=list)
    queued_findings: list[BlindSpot] = field(default_factory=list)
    injected_findings: list[dict] = field(default_factory=list)
    last_interrupt_time: float = 0.0
    total_nudges: int = 0
    total_interrupts: int = 0


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def process_cartographer_alerts(
    state: ConsensusState,
    alerts: list[str],
    config: ConsensusConfig,
) -> list[dict]:
    """Process alerts from the Cartographer. Returns actions to take."""
    state.cartographer_alerts.extend(alerts)
    actions = []

    for alert in alerts:
        if "CONTRADICTION" in alert.upper():
            actions.append({
                "type": "nudge",
                "source": "cartographer",
                "message": alert,
                "severity": 7,
            })
            state.total_nudges += 1

    return actions


def process_pilgrim_report(
    state: ConsensusState,
    report: PilgrimReport,
    config: ConsensusConfig,
) -> list[dict]:
    """Process a Pilgrim report. Returns actions to take."""
    state.pilgrim_reports.append(report)
    actions = []
    now = time.time()

    for spot in report.blind_spots:
        if spot.severity >= config.interrupt_threshold:
            # Check cooldown
            if now - state.last_interrupt_time >= config.cooldown_seconds:
                actions.append({
                    "type": "interrupt",
                    "source": "pilgrim",
                    "message": spot.description,
                    "suggested_action": spot.suggested_action,
                    "severity": spot.severity,
                    "category": spot.category,
                })
                state.last_interrupt_time = now
                state.total_interrupts += 1
                state.injected_findings.append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "description": spot.description,
                    "severity": spot.severity,
                })
            else:
                # Cooldown active — queue it
                state.queued_findings.append(spot)
                actions.append({
                    "type": "queue",
                    "source": "pilgrim",
                    "message": spot.description,
                    "severity": spot.severity,
                })

        elif spot.severity >= config.nudge_threshold:
            actions.append({
                "type": "nudge",
                "source": "pilgrim",
                "message": spot.description,
                "suggested_action": spot.suggested_action,
                "severity": spot.severity,
            })
            state.total_nudges += 1

        else:
            state.queued_findings.append(spot)

    # Check for screaming (multiple high-severity findings)
    if report.is_screaming and len(report.blind_spots) >= config.min_blind_spots_for_interrupt:
        if now - state.last_interrupt_time >= config.cooldown_seconds:
            actions.append({
                "type": "interrupt",
                "source": "consensus",
                "message": (
                    f"OBSERVERS ARE SCREAMING: {len(report.blind_spots)} blind spots found, "
                    f"max severity {report.highest_severity}/10. "
                    f"{report.walk_summary}"
                ),
                "severity": report.highest_severity,
                "findings": [s.description for s in report.blind_spots[:5]],
            })
            state.last_interrupt_time = now
            state.total_interrupts += 1

    return actions


def get_queued_findings(state: ConsensusState) -> list[BlindSpot]:
    """Get findings that haven't been injected yet (for when executor asks)."""
    findings = list(state.queued_findings)
    state.queued_findings.clear()
    return findings


def format_injection(actions: list[dict]) -> str:
    """Format actions into text suitable for injection into agent context."""
    interrupts = [a for a in actions if a["type"] == "interrupt"]
    if not interrupts:
        return ""

    lines = [
        "=" * 60,
        "OBSERVER ALERT (Weary Cartographer + Vigilant Pilgrim)",
        "=" * 60,
    ]

    for action in interrupts:
        lines.append(f"[SEVERITY {action['severity']}/10] {action['message']}")
        if action.get("suggested_action"):
            lines.append(f"  SUGGESTED: {action['suggested_action']}")
        if action.get("findings"):
            for f in action["findings"]:
                lines.append(f"  - {f}")
        lines.append("")

    lines.append("The observers see something you might be missing. Consider pausing")
    lines.append("your current approach and investigating the above.")
    lines.append("=" * 60)

    return "\n".join(lines)


def format_nudge(actions: list[dict]) -> str:
    """Format actions into a compact nudge string for the status bar."""
    nudges = [a for a in actions if a["type"] == "nudge"]
    if not nudges:
        return ""

    parts = []
    for n in nudges[:3]:
        parts.append(f"[{n['severity']}] {n['message'][:50]}")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------

def consensus_status_line(state: ConsensusState) -> str:
    """Status line for the UI."""
    parts = []

    total_alerts = len(state.cartographer_alerts)
    total_reports = len(state.pilgrim_reports)
    queued = len(state.queued_findings)

    if total_alerts:
        parts.append(f"Cart:{total_alerts}")
    if total_reports:
        latest = state.pilgrim_reports[-1]
        parts.append(f"Pilgrim:{latest.highest_severity}/10")
    if queued:
        parts.append(f"Queued:{queued}")
    if state.total_interrupts:
        parts.append(f"Injected:{state.total_interrupts}")

    return " | ".join(parts) if parts else "Observers idle"
