"""JSONL audit logging for proxy shell and full-mode sessions."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def log_event(
    log_path: str | Path,
    event_type: str,
    data: dict[str, Any],
    session_id: str = "",
) -> None:
    """Append a single JSON-line event to *log_path*.

    Each line is a self-contained JSON object with:
        timestamp, session_id, event_type, + all keys from *data*.

    The file is opened in append mode — safe for sequential writes from a
    single process (the proxy shell forks per command, but writes are
    short and atomic on most OS/FS combos for lines under 4KB).
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "event_type": event_type,
    }
    record.update(data)

    line = json.dumps(record, default=str) + "\n"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        logger.warning("Failed to write audit event to %s: %s", log_path, exc)


def read_events(
    log_path: str | Path,
    event_type: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read events from a JSONL log file, with optional filters.

    Returns an empty list if the file doesn't exist or is unreadable.
    Malformed lines are silently skipped.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return []

    events: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and record.get("event_type") != event_type:
                    continue
                if session_id and record.get("session_id") != session_id:
                    continue
                events.append(record)
    except OSError as exc:
        logger.warning("Failed to read audit log %s: %s", log_path, exc)

    return events
