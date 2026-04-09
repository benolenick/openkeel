"""Distilled Log — the essential narrative that observers read.

The executor writes terse, structured entries. The Cartographer reads them
one at a time. The Pilgrim never reads them directly — only the map.

Format: timestamp | category | message
Categories: GOAL, HYPOTHESIS, ATTEMPT, RESULT, DISCOVERY, PIVOT, PHASE, ENV, CRED
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Log entry
# ---------------------------------------------------------------------------

CATEGORIES = {
    "GOAL",        # setting or changing a goal
    "HYPOTHESIS",  # proposing or updating a hypothesis
    "ATTEMPT",     # trying something
    "RESULT",      # outcome of an attempt
    "DISCOVERY",   # found something new
    "PIVOT",       # changing approach
    "PHASE",       # moving to next phase (recon/research/run/review)
    "ENV",         # environment observation
    "CRED",        # credential found
    "CIRCUIT",     # circuit breaker triggered
    "OBSERVER",    # injected by observers
}


@dataclass
class LogEntry:
    """A single distilled log entry."""
    timestamp: str = ""
    category: str = "ATTEMPT"
    message: str = ""
    confidence: float = -1.0  # -1 = not applicable
    stone_label: str = ""
    hypothesis_label: str = ""
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Log file operations
# ---------------------------------------------------------------------------

class DistilledLog:
    """Append-only distilled log for a mission."""

    def __init__(self, mission_dir: Path):
        self._path = mission_dir / "distilled_log.jsonl"
        self._mission_dir = mission_dir
        mission_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(
        self,
        category: str,
        message: str,
        confidence: float = -1.0,
        stone_label: str = "",
        hypothesis_label: str = "",
        **metadata,
    ) -> LogEntry:
        """Write a new entry to the log."""
        entry = LogEntry(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            category=category.upper(),
            message=message,
            confidence=confidence,
            stone_label=stone_label,
            hypothesis_label=hypothesis_label,
            metadata=metadata,
        )

        line = json.dumps({
            "ts": entry.timestamp,
            "cat": entry.category,
            "msg": entry.message,
            "conf": entry.confidence if entry.confidence >= 0 else None,
            "stone": entry.stone_label or None,
            "hyp": entry.hypothesis_label or None,
            "meta": entry.metadata or None,
        }, separators=(",", ":"))

        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        return entry

    def read_all(self) -> list[LogEntry]:
        """Read all entries."""
        if not self._path.exists():
            return []
        entries = []
        for line in self._path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                entries.append(LogEntry(
                    timestamp=d.get("ts", ""),
                    category=d.get("cat", ""),
                    message=d.get("msg", ""),
                    confidence=d.get("conf", -1.0) or -1.0,
                    stone_label=d.get("stone", "") or "",
                    hypothesis_label=d.get("hyp", "") or "",
                    metadata=d.get("meta", {}) or {},
                ))
            except json.JSONDecodeError:
                continue
        return entries

    def read_tail(self, n: int = 20) -> list[LogEntry]:
        """Read the last N entries (the rolling window)."""
        all_entries = self.read_all()
        return all_entries[-n:]

    def read_since(self, last_count: int) -> list[LogEntry]:
        """Read entries added since we last read (by count)."""
        all_entries = self.read_all()
        return all_entries[last_count:]

    def count(self) -> int:
        """Count total entries."""
        if not self._path.exists():
            return 0
        return sum(1 for line in self._path.read_text(encoding="utf-8").strip().split("\n") if line.strip())

    def format_entry(self, entry: LogEntry) -> str:
        """Format a single entry as human-readable text."""
        parts = [f"[{entry.timestamp}]", entry.category]
        if entry.stone_label:
            parts.append(f"({entry.stone_label})")
        if entry.hypothesis_label:
            parts.append(f"[{entry.hypothesis_label}]")
        parts.append(entry.message)
        if entry.confidence >= 0:
            parts.append(f"conf:{entry.confidence:.0%}")
        return " ".join(parts)

    def format_window(self, entries: list[LogEntry] | None = None, n: int = 20) -> str:
        """Format the rolling window as text for the Cartographer."""
        if entries is None:
            entries = self.read_tail(n)
        return "\n".join(self.format_entry(e) for e in entries)


# ---------------------------------------------------------------------------
# Convenience functions for the executor to call
# ---------------------------------------------------------------------------

def log_goal(log: DistilledLog, goal: str, stone: str = "") -> LogEntry:
    return log.append("GOAL", goal, stone_label=stone)


def log_hypothesis(
    log: DistilledLog,
    label: str,
    confidence: float,
    stone: str = "",
) -> LogEntry:
    return log.append("HYPOTHESIS", label, confidence=confidence, stone_label=stone, hypothesis_label=label)


def log_attempt(
    log: DistilledLog,
    description: str,
    hypothesis: str = "",
    stone: str = "",
) -> LogEntry:
    return log.append("ATTEMPT", description, stone_label=stone, hypothesis_label=hypothesis)


def log_result(
    log: DistilledLog,
    outcome: str,
    result: str = "fail",
    hypothesis: str = "",
    stone: str = "",
    confidence: float = -1.0,
) -> LogEntry:
    return log.append("RESULT", f"{result.upper()}: {outcome}", confidence=confidence,
                      stone_label=stone, hypothesis_label=hypothesis, result=result)


def log_discovery(log: DistilledLog, what: str, stone: str = "") -> LogEntry:
    return log.append("DISCOVERY", what, stone_label=stone)


def log_pivot(log: DistilledLog, reason: str, stone: str = "") -> LogEntry:
    return log.append("PIVOT", reason, stone_label=stone)


def log_phase(log: DistilledLog, phase: str, stone: str = "") -> LogEntry:
    return log.append("PHASE", f"Entering {phase}", stone_label=stone)


def log_env(log: DistilledLog, observation: str, stone: str = "") -> LogEntry:
    return log.append("ENV", observation, stone_label=stone)


def log_credential(log: DistilledLog, description: str, stone: str = "") -> LogEntry:
    return log.append("CRED", description, stone_label=stone)


def log_circuit_breaker(log: DistilledLog, alert: str, stone: str = "", hypothesis: str = "") -> LogEntry:
    return log.append("CIRCUIT", alert, stone_label=stone, hypothesis_label=hypothesis)


def log_observer_injection(log: DistilledLog, message: str) -> LogEntry:
    return log.append("OBSERVER", message)
