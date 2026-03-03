"""Activity timeboxing for full-mode sessions.

Tracks per-activity command counts and elapsed time. When an activity
exceeds its timebox, the proxy shell warns and eventually blocks.

State is stored as a JSON file per session (fast read/write from the
proxy shell subprocess, which is forked per command).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .profile import ActivityDef, Profile

logger = logging.getLogger(__name__)


def _load_state(state_path: Path) -> dict[str, Any]:
    """Load timebox state from JSON file."""
    if not state_path.exists():
        return {"activities": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"activities": {}}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Save timebox state to JSON file."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _get_activity_def(profile: Profile, activity_name: str) -> ActivityDef | None:
    """Look up an activity definition by name."""
    for a in profile.activities:
        if a.name == activity_name:
            return a
    return None


def record_activity(
    state_path: str | Path,
    activity: str,
    command: str,
    profile: Profile,
) -> tuple[str, str]:
    """Record a command under an activity and check timebox.

    Args:
        state_path: Path to the session's timebox state JSON file.
        activity: The activity name (from classifier).
        command: The command being executed (for logging).
        profile: The active profile.

    Returns:
        (action, message) where action is "allow", "warn", or "block".
    """
    if not activity:
        return "allow", ""

    activity_def = _get_activity_def(profile, activity)
    if not activity_def or activity_def.timebox_minutes <= 0:
        return "allow", ""  # no timebox for this activity

    state_path = Path(state_path)
    state = _load_state(state_path)
    now = time.time()

    activities = state.setdefault("activities", {})
    entry = activities.get(activity)

    if entry is None:
        # First command in this activity
        entry = {
            "first_seen": now,
            "last_seen": now,
            "command_count": 1,
            "warned": False,
        }
        activities[activity] = entry
        _save_state(state_path, state)
        return "allow", ""

    entry["last_seen"] = now
    entry["command_count"] = entry.get("command_count", 0) + 1

    elapsed_minutes = (now - entry["first_seen"]) / 60.0
    timebox = activity_def.timebox_minutes
    grace = activity_def.grace_minutes

    if elapsed_minutes > timebox + grace:
        # Past grace period → block
        _save_state(state_path, state)
        return "block", (
            f"Activity '{activity}' exceeded timebox: "
            f"{elapsed_minutes:.0f}min elapsed (limit: {timebox}min + {grace}min grace). "
            f"Use OPENKEEL-EXTEND to add time."
        )

    if elapsed_minutes > timebox:
        # In grace period → warn
        remaining_grace = (timebox + grace) - elapsed_minutes
        entry["warned"] = True
        _save_state(state_path, state)
        return "warn", (
            f"Activity '{activity}' timebox exceeded: "
            f"{elapsed_minutes:.0f}min elapsed (limit: {timebox}min). "
            f"Grace period: {remaining_grace:.0f}min remaining."
        )

    _save_state(state_path, state)
    return "allow", ""


def extend_activity(
    state_path: str | Path,
    activity: str,
    extra_minutes: int,
) -> bool:
    """Extend an activity's timebox by resetting its start time.

    Returns True if the activity was found and extended.
    """
    state_path = Path(state_path)
    state = _load_state(state_path)
    activities = state.get("activities", {})

    if activity not in activities:
        return False

    # Push the first_seen forward to effectively grant more time
    entry = activities[activity]
    entry["first_seen"] = entry["first_seen"] + (extra_minutes * 60)
    entry["warned"] = False
    _save_state(state_path, state)
    return True


def get_activity_status(
    state_path: str | Path,
    profile: Profile,
) -> list[dict[str, Any]]:
    """Get status of all tracked activities.

    Returns a list of dicts with: name, elapsed_min, timebox_min,
    command_count, status (active/warning/blocked/ok).
    """
    state_path = Path(state_path)
    state = _load_state(state_path)
    activities = state.get("activities", {})
    now = time.time()

    results = []
    for name, entry in activities.items():
        activity_def = _get_activity_def(profile, name)
        timebox = activity_def.timebox_minutes if activity_def else 0
        grace = activity_def.grace_minutes if activity_def else 5
        elapsed = (now - entry["first_seen"]) / 60.0

        if timebox <= 0:
            status = "ok"
        elif elapsed > timebox + grace:
            status = "blocked"
        elif elapsed > timebox:
            status = "warning"
        else:
            status = "active"

        results.append({
            "name": name,
            "elapsed_min": round(elapsed, 1),
            "timebox_min": timebox,
            "command_count": entry.get("command_count", 0),
            "status": status,
        })

    return results
