"""Post-session learning — extract lessons from session logs and seed to memory.

After a session ends, this module reads the JSONL audit trail, identifies
notable events (timebox blocks, drift events, successful phases, tool gaps),
and stores distilled lessons in the memory backend so future sessions
benefit from past experience.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .profile import LearningConfig

logger = logging.getLogger(__name__)


def extract_lessons(
    log_path: str | Path,
    config: LearningConfig,
    project: str = "",
    profile_name: str = "",
) -> list[str]:
    """Read a session JSONL log and extract lesson strings.

    Returns a list of human-readable fact strings suitable for memorization.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return []

    events = _read_events(log_path)
    lessons: list[str] = []

    if "timebox_blocks" in config.extract_on:
        lessons.extend(_lessons_from_timeboxes(events, project))

    if "successful_phases" in config.extract_on:
        lessons.extend(_lessons_from_phases(events, project))

    if "drift_events" in config.extract_on:
        lessons.extend(_lessons_from_drift(events, project))

    if "blocked_commands" in config.extract_on:
        lessons.extend(_lessons_from_blocked(events, project))

    if "tool_gaps" in config.extract_on:
        lessons.extend(_lessons_from_tool_gaps(events, project))

    return lessons


def seed_lessons(
    lessons: list[str],
    config: LearningConfig,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Seed extracted lessons to the memory backend.

    Returns the number of successfully stored lessons.
    """
    if not lessons:
        return 0

    from openkeel.integrations.memory import MemoryClient

    client = MemoryClient(endpoint=config.endpoint, timeout=config.timeout)
    if not client.is_available():
        logger.warning("Learning: memory backend at %s not available, skipping seed", config.endpoint)
        return 0

    stored = 0
    base_meta = {"source": "openkeel_learning"}
    if metadata:
        base_meta.update(metadata)

    for lesson in lessons:
        if client.memorize(lesson, metadata=base_meta):
            stored += 1

    logger.info("Learning: seeded %d/%d lessons to %s", stored, len(lessons), config.endpoint)
    return stored


def run_post_session_learning(
    log_path: str | Path,
    config: LearningConfig,
    project: str = "",
    profile_name: str = "",
    session_id: str = "",
) -> int:
    """Full pipeline: extract lessons from log, seed to memory.

    Returns count of stored lessons.
    """
    if not config.enabled:
        return 0

    lessons = extract_lessons(log_path, config, project, profile_name)
    if not lessons:
        logger.info("Learning: no lessons extracted from %s", log_path)
        return 0

    logger.info("Learning: extracted %d lessons from %s", len(lessons), log_path)

    if not config.auto_seed:
        # Just log the lessons, don't seed
        for lesson in lessons:
            logger.info("Learning (not seeded): %s", lesson)
        return 0

    metadata = {
        "project": project,
        "profile": profile_name,
        "session_id": session_id,
    }
    return seed_lessons(lessons, config, metadata)


# ---------------------------------------------------------------------------
# Lesson extractors
# ---------------------------------------------------------------------------


def _read_events(log_path: Path) -> list[dict[str, Any]]:
    """Read all events from a JSONL file."""
    events: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return events


def _lessons_from_timeboxes(events: list[dict[str, Any]], project: str) -> list[str]:
    """Extract lessons from timebox block events."""
    lessons = []
    for ev in events:
        if ev.get("event_type") in ("timebox_block", "command_blocked"):
            activity = ev.get("activity", "unknown")
            message = ev.get("message", "")
            if "timebox" in message.lower() or ev.get("event_type") == "timebox_block":
                ctx = f" on project '{project}'" if project else ""
                lessons.append(
                    f"[OPENKEEL:TIMEBOX] Activity '{activity}' was timeboxed with no progress{ctx}. "
                    f"Consider a different approach next time."
                )
    return lessons


def _lessons_from_phases(events: list[dict[str, Any]], project: str) -> list[str]:
    """Extract lessons from successful phase completions."""
    lessons = []
    for ev in events:
        if ev.get("event_type") == "phase_advance":
            data = ev.get("data", ev)
            from_phase = data.get("from_phase", "")
            to_phase = data.get("to_phase", "")
            if from_phase:
                ctx = f" on project '{project}'" if project else ""
                lessons.append(
                    f"[OPENKEEL:PHASE] Phase '{from_phase}' completed successfully{ctx}, "
                    f"advanced to '{to_phase}'."
                )
    return lessons


def _lessons_from_drift(events: list[dict[str, Any]], project: str) -> list[str]:
    """Extract lessons from drift events."""
    lessons = []
    drift_count = 0
    for ev in events:
        if ev.get("event_type") == "drift_event":
            drift_count += 1
            trigger = ev.get("trigger", ev.get("data", {}).get("trigger", "unknown"))
            ctx = f" on project '{project}'" if project else ""
            lessons.append(
                f"[OPENKEEL:DRIFT] Drift detected (trigger: {trigger}){ctx}. "
                f"Agent deviated from intended approach."
            )
    return lessons


def _lessons_from_blocked(events: list[dict[str, Any]], project: str) -> list[str]:
    """Extract lessons from blocked commands (patterns of bad behavior)."""
    blocked_cmds: dict[str, int] = {}
    for ev in events:
        if ev.get("event_type") == "command_blocked":
            cmd = ev.get("command", ev.get("data", {}).get("command", ""))
            # Group by first word (tool name)
            tool = cmd.split()[0] if cmd.split() else "unknown"
            blocked_cmds[tool] = blocked_cmds.get(tool, 0) + 1

    lessons = []
    for tool, count in blocked_cmds.items():
        if count >= 3:
            ctx = f" on project '{project}'" if project else ""
            lessons.append(
                f"[OPENKEEL:BLOCKED] Command '{tool}' was blocked {count} times{ctx}. "
                f"Agent repeatedly attempted a prohibited operation."
            )
    return lessons


def _lessons_from_tool_gaps(events: list[dict[str, Any]], project: str) -> list[str]:
    """Detect commands that failed because a tool wasn't installed."""
    lessons = []
    not_found: set[str] = set()
    for ev in events:
        if ev.get("event_type") in ("command_allowed",):
            data = ev.get("data", ev)
            # Check for "command not found" in the event (if exit code is captured)
            cmd = data.get("command", "")
            exit_code = data.get("exit_code")
            if exit_code == 127:  # command not found
                tool = cmd.split()[0] if cmd.split() else ""
                if tool and tool not in not_found:
                    not_found.add(tool)
                    ctx = f" on project '{project}'" if project else ""
                    lessons.append(
                        f"[OPENKEEL:TOOL_GAP] Tool '{tool}' was not found{ctx}. "
                        f"Install it before the next session."
                    )
    return lessons
