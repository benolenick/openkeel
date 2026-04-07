"""Persistent cross-session read log.

Fixes the file_read leak where Claude re-reads the same file across many
sessions and the per-session mtime cache resets each time.

Schema:
    read_log(file_path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
             last_read_ts REAL, read_count INTEGER)

A single row per file. Updated on every intercepted Read. The hook
checks this table before falling back to the per-session check.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".openkeel" / "token_saver_read_log.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS read_log (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            last_read_ts REAL NOT NULL,
            read_count INTEGER DEFAULT 1
        )"""
    )
    return conn


def lookup(file_path: str) -> dict | None:
    """Return the previous read record for this path, or None."""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT mtime, size, last_read_ts, read_count FROM read_log "
            "WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "mtime": row[0],
        "size": row[1],
        "last_read_ts": row[2],
        "read_count": row[3],
    }


def record(file_path: str, mtime: float, size: int) -> None:
    """Insert or update the read record for this file."""
    try:
        conn = _conn()
        now = time.time()
        conn.execute(
            """INSERT INTO read_log (file_path, mtime, size, last_read_ts, read_count)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(file_path) DO UPDATE SET
                   mtime = excluded.mtime,
                   size = excluded.size,
                   last_read_ts = excluded.last_read_ts,
                   read_count = read_log.read_count + 1""",
            (file_path, mtime, size, now),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def unchanged_since_last_read(file_path: str, current_mtime: float,
                              max_age_hours: float = 168) -> dict | None:
    """If this file was previously read AND its mtime is unchanged AND the
    last read was within max_age_hours, return the prior record.
    Returns None otherwise (caller should proceed with a real read)."""
    prev = lookup(file_path)
    if prev is None:
        return None
    if prev["mtime"] != current_mtime:
        return None
    age_sec = time.time() - prev["last_read_ts"]
    if age_sec > max_age_hours * 3600:
        return None
    return prev
