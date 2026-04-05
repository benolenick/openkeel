"""Token savings ledger — SQLite-backed tracking of tokens saved vs spent.

Records every cache hit, output filter, and command rewrite. Provides
session and all-time summaries.

Rough token estimation: 1 token ~= 4 characters.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".openkeel" / "token_ledger.db"
CHARS_PER_TOKEN = 4

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS savings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    file_path TEXT,
    original_chars INTEGER DEFAULT 0,
    saved_chars INTEGER DEFAULT 0,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_savings_session ON savings(session_id);
CREATE INDEX IF NOT EXISTS idx_savings_time ON savings(timestamp);
"""

# Module-level session ID — one per process lifetime
_SESSION_ID = os.environ.get("TOKEN_SAVER_SESSION", uuid.uuid4().hex[:12])


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.executescript(_CREATE_SQL)
    return conn


def record(
    event_type: str,
    tool_name: str = "",
    file_path: str = "",
    original_chars: int = 0,
    saved_chars: int = 0,
    notes: str = "",
    session_id: str = "",
) -> None:
    """Record a token savings event."""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO savings (id, session_id, timestamp, event_type, tool_name, file_path, original_chars, saved_chars, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex[:16],
                session_id or _SESSION_ID,
                time.time(),
                event_type,
                tool_name,
                file_path,
                original_chars,
                saved_chars,
                notes,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def session_summary(session_id: str = "") -> dict[str, Any]:
    """Get savings summary for current or specified session."""
    sid = session_id or _SESSION_ID
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(original_chars),0), COALESCE(SUM(saved_chars),0) "
            "FROM savings WHERE session_id = ?",
            (sid,),
        ).fetchone()
        conn.close()
        events, orig, saved = row
        return {
            "session_id": sid,
            "events": events,
            "original_chars": orig,
            "saved_chars": saved,
            "original_tokens_est": orig // CHARS_PER_TOKEN,
            "saved_tokens_est": saved // CHARS_PER_TOKEN,
            "savings_pct": round(saved / orig * 100, 1) if orig else 0.0,
        }
    except Exception:
        return {"session_id": sid, "events": 0, "saved_chars": 0, "saved_tokens_est": 0}


def all_time_summary() -> dict[str, Any]:
    """Get all-time savings summary."""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(original_chars),0), COALESCE(SUM(saved_chars),0), "
            "COUNT(DISTINCT session_id) FROM savings",
        ).fetchone()
        conn.close()
        events, orig, saved, sessions = row
        return {
            "sessions": sessions,
            "events": events,
            "original_chars": orig,
            "saved_chars": saved,
            "original_tokens_est": orig // CHARS_PER_TOKEN,
            "saved_tokens_est": saved // CHARS_PER_TOKEN,
            "savings_pct": round(saved / orig * 100, 1) if orig else 0.0,
        }
    except Exception:
        return {"sessions": 0, "events": 0, "saved_chars": 0, "saved_tokens_est": 0}


def recent_events(limit: int = 20, session_id: str = "") -> list[dict]:
    """Get recent savings events."""
    try:
        conn = _get_db()
        if session_id:
            rows = conn.execute(
                "SELECT event_type, tool_name, file_path, original_chars, saved_chars, notes, timestamp "
                "FROM savings WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT event_type, tool_name, file_path, original_chars, saved_chars, notes, timestamp "
                "FROM savings ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [
            {
                "event_type": r[0],
                "tool_name": r[1],
                "file_path": r[2],
                "original_chars": r[3],
                "saved_chars": r[4],
                "notes": r[5],
                "timestamp": r[6],
            }
            for r in rows
        ]
    except Exception:
        return []


def format_summary(summary: dict[str, Any]) -> str:
    """Format a summary dict for terminal display."""
    lines = []
    if "sessions" in summary:
        lines.append(f"  All-time ({summary['sessions']} sessions, {summary['events']} events)")
    else:
        lines.append(f"  Session {summary.get('session_id', '?')} ({summary['events']} events)")
    lines.append(f"  Original: ~{summary['original_tokens_est']:,} tokens")
    lines.append(f"  Saved:    ~{summary['saved_tokens_est']:,} tokens")
    lines.append(f"  Savings:  {summary['savings_pct']}%")
    return "\n".join(lines)
