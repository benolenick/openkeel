"""SQLite session history with FTS5 search.

Design: exec.py writes JSONL only (fast, no lock contention).
The runner batch-imports JSONL into SQLite at session end for
queryability and FTS5 full-text search.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".openkeel" / "history.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (or create) the history database and return a connection."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL DEFAULT '',
            profile TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            command_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            command TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT '',
            activity TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS phases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            phase_name TEXT NOT NULL,
            entered_at TEXT NOT NULL,
            exited_at TEXT,
            exit_reason TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_phases_session
            ON phases(session_id);
    """)

    # FTS5 virtual table (may already exist)
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
            USING fts5(command, message, activity, content=events, content_rowid=id)
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 not available — degrade gracefully

    conn.commit()


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def start_session(
    conn: sqlite3.Connection,
    session_id: str,
    project: str = "",
    profile: str = "",
) -> None:
    """Record a new session."""
    conn.execute(
        "INSERT INTO sessions (id, project, profile, status, started_at) VALUES (?, ?, ?, 'running', ?)",
        (session_id, project, profile, _utc_now()),
    )
    conn.commit()


def end_session(
    conn: sqlite3.Connection,
    session_id: str,
    status: str = "completed",
    notes: str = "",
) -> None:
    """Mark a session as ended."""
    conn.execute(
        "UPDATE sessions SET status=?, ended_at=?, notes=? WHERE id=?",
        (status, _utc_now(), notes, session_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


def record_event(
    conn: sqlite3.Connection,
    session_id: str,
    event_type: str,
    command: str = "",
    action: str = "",
    tier: str = "",
    activity: str = "",
    message: str = "",
    data: dict[str, Any] | None = None,
) -> int:
    """Record a single event and update session counters.

    Returns the event row ID.
    """
    data_json = json.dumps(data or {})
    cursor = conn.execute(
        """INSERT INTO events (session_id, timestamp, event_type, command, action, tier, activity, message, data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, _utc_now(), event_type, command, action, tier, activity, message, data_json),
    )
    row_id = cursor.lastrowid

    # Update session counters
    conn.execute(
        "UPDATE sessions SET command_count = command_count + 1 WHERE id = ?",
        (session_id,),
    )
    if action == "deny":
        conn.execute(
            "UPDATE sessions SET blocked_count = blocked_count + 1 WHERE id = ?",
            (session_id,),
        )

    # Update FTS5
    try:
        conn.execute(
            "INSERT INTO events_fts(rowid, command, message, activity) VALUES (?, ?, ?, ?)",
            (row_id, command, message, activity),
        )
    except sqlite3.OperationalError:
        pass  # FTS5 not available

    conn.commit()
    return row_id


# ---------------------------------------------------------------------------
# Phase tracking
# ---------------------------------------------------------------------------


def record_phase(
    conn: sqlite3.Connection,
    session_id: str,
    phase_name: str,
) -> int:
    """Record entry into a new phase. Closes the previous phase if open."""
    # Close previous open phase
    conn.execute(
        "UPDATE phases SET exited_at=?, exit_reason='advanced' WHERE session_id=? AND exited_at IS NULL",
        (_utc_now(), session_id),
    )

    cursor = conn.execute(
        "INSERT INTO phases (session_id, phase_name, entered_at) VALUES (?, ?, ?)",
        (session_id, phase_name, _utc_now()),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def query_sessions(
    conn: sqlite3.Connection,
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Query sessions with optional filters."""
    conditions = []
    params: list[Any] = []

    if project:
        conditions.append("project = ?")
        params.append(project)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM sessions {where} ORDER BY started_at DESC LIMIT ?",
        params,
    ).fetchall()

    return [dict(row) for row in rows]


def search_events(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Full-text search across events using FTS5."""
    try:
        rows = conn.execute(
            """SELECT e.* FROM events e
               JOIN events_fts f ON e.id = f.rowid
               WHERE events_fts MATCH ?
               ORDER BY e.timestamp DESC
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        # FTS5 not available — fall back to LIKE search
        like_query = f"%{query}%"
        rows = conn.execute(
            """SELECT * FROM events
               WHERE command LIKE ? OR message LIKE ? OR activity LIKE ?
               ORDER BY timestamp DESC LIMIT ?""",
            (like_query, like_query, like_query, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_session_events(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict[str, Any]]:
    """Get all events for a session."""
    rows = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_session_phases(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict[str, Any]]:
    """Get all phases for a session."""
    rows = conn.execute(
        "SELECT * FROM phases WHERE session_id = ? ORDER BY entered_at",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# JSONL import
# ---------------------------------------------------------------------------


def sync_jsonl_to_db(
    conn: sqlite3.Connection,
    session_id: str,
    jsonl_path: str | Path,
) -> int:
    """Batch-import events from a JSONL file into the database.

    Returns the number of events imported.
    """
    jsonl_path = Path(jsonl_path)
    if not jsonl_path.exists():
        return 0

    count = 0
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_event(
                conn=conn,
                session_id=session_id,
                event_type=record.get("event_type", "unknown"),
                command=record.get("command", ""),
                action=record.get("action", ""),
                tier=record.get("tier", ""),
                activity=record.get("activity", ""),
                message=record.get("message", ""),
                data=record.get("data", {}),
            )
            count += 1

    return count


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Get aggregate statistics across all sessions."""
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    blocked_total = conn.execute(
        "SELECT COALESCE(SUM(blocked_count), 0) FROM sessions"
    ).fetchone()[0]

    # Per-status breakdown
    status_rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM sessions GROUP BY status"
    ).fetchall()
    by_status = {row["status"]: row["cnt"] for row in status_rows}

    # Per-project breakdown
    project_rows = conn.execute(
        "SELECT project, COUNT(*) as cnt FROM sessions WHERE project != '' GROUP BY project"
    ).fetchall()
    by_project = {row["project"]: row["cnt"] for row in project_rows}

    # Top blocked commands
    top_blocked = conn.execute(
        """SELECT command, COUNT(*) as cnt FROM events
           WHERE action = 'deny' GROUP BY command
           ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()

    return {
        "total_sessions": session_count,
        "total_events": event_count,
        "total_blocked": blocked_total,
        "by_status": by_status,
        "by_project": by_project,
        "top_blocked": [{"command": r["command"], "count": r["cnt"]} for r in top_blocked],
    }
