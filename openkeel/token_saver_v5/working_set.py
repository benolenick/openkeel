"""Persistent working set — session-scoped cache of file reads and deterministic bash outputs.

The goal: prevent bytes from entering Claude's context in the first place.

On every Read, Grep, or read-only Bash call, we check the working set first.
If the file is unchanged since we last read it (by hash + mtime), we return
a short pointer block instead of the full content. Claude trusts the pointer.

Invalidation: any Edit/Write/append to a file removes its entry.

Storage: ~/.openkeel/working_set/<session_id>.db
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

WS_DIR = Path.home() / ".openkeel" / "working_set"
WS_DIR.mkdir(parents=True, exist_ok=True)

MAX_ENTRIES_PER_SESSION = 200
ENTRY_TTL_SEC = 3600  # 1 hour
MIN_CHARS_TO_DEDUP = 500  # don't bother for tiny files


def _db_path(session_id: str) -> Path:
    # Sanitize session_id to avoid path traversal
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "default"
    return WS_DIR / f"{safe}.db"


def _connect(session_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(session_id)), timeout=3)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            key TEXT PRIMARY KEY,
            kind TEXT,
            path TEXT,
            content_hash TEXT,
            file_mtime REAL,
            size INTEGER,
            last_seen REAL,
            turn INTEGER,
            tool TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON entries(last_seen)")
    conn.commit()
    return conn


def _file_hash(path: str) -> tuple[str, float, int] | None:
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            h = hashlib.blake2b(f.read(), digest_size=16).hexdigest()
        return h, st.st_mtime, st.st_size
    except Exception:
        return None


def _current_session() -> str:
    return os.environ.get("TOKEN_SAVER_SESSION") or os.environ.get("CLAUDE_SESSION_ID") or "default"


def should_block_read(path: str, session_id: str | None = None) -> dict | None:
    """Called from pre_tool.py for Read tool. Returns a dict if the read should be blocked
    (with a pointer message), or None if it should proceed normally.
    """
    session_id = session_id or _current_session()
    if not os.path.isfile(path):
        return None
    st = _file_hash(path)
    if not st:
        return None
    h, mtime, size = st
    if size < MIN_CHARS_TO_DEDUP:
        return None
    key = f"read:{os.path.abspath(path)}"
    try:
        conn = _connect(session_id)
        row = conn.execute(
            "SELECT content_hash, file_mtime, size, last_seen, turn FROM entries WHERE key = ?",
            (key,),
        ).fetchone()
        if row:
            prev_hash, prev_mtime, prev_size, prev_ts, prev_turn = row
            if prev_hash == h and abs(prev_mtime - mtime) < 0.01:
                # Unchanged — block the re-read
                conn.execute(
                    "UPDATE entries SET last_seen = ?, turn = turn + 1 WHERE key = ?",
                    (time.time(), key),
                )
                conn.commit()
                conn.close()
                return {
                    "action": "block_read",
                    "path": path,
                    "reason": "already_in_working_set",
                    "pointer": f"[TOKEN SAVER] {path} already read this session (hash {h[:10]}, {size} bytes, unchanged). Skipping re-read. If you need to verify, run: stat {path}",
                    "saved_chars": size,
                }
        # Either no entry or changed — record it and allow the read
        conn.execute(
            "INSERT OR REPLACE INTO entries (key, kind, path, content_hash, file_mtime, size, last_seen, turn, tool) "
            "VALUES (?, 'read', ?, ?, ?, ?, ?, 1, 'Read')",
            (key, os.path.abspath(path), h, mtime, size, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        return None
    return None


def invalidate(path: str, session_id: str | None = None):
    """Called when a file is edited/written. Removes working set entries for it."""
    session_id = session_id or _current_session()
    try:
        conn = _connect(session_id)
        conn.execute("DELETE FROM entries WHERE path = ?", (os.path.abspath(path),))
        conn.commit()
        conn.close()
    except Exception:
        pass


# --- Bash predict (read-only deterministic commands) ---

READ_ONLY_BASH_PATTERNS = (
    "ls ", "ls\n", "pwd", "whoami", "hostname",
    "cat ", "head ", "tail ", "wc ", "file ",
    "stat ", "find ", "which ", "type ",
    "git status", "git log", "git diff", "git show",
    "git branch", "git remote", "git config",
    "ps ", "top -bn1", "df ", "du ", "free",
    "uname", "date", "uptime",
    "echo ", "printf ",
)


def _cmd_hash(cmd: str, cwd: str = "") -> str:
    return hashlib.blake2b(f"{cwd}||{cmd}".encode(), digest_size=16).hexdigest()


def should_block_bash(command: str, cwd: str = "", session_id: str | None = None) -> dict | None:
    """Block re-execution of deterministic read-only bash commands within the session."""
    session_id = session_id or _current_session()
    stripped = command.strip()
    if not any(stripped.startswith(p) for p in READ_ONLY_BASH_PATTERNS):
        return None
    # Reject if there's shell metacharacters that could mutate
    if any(ch in stripped for ch in [">", ">>", "| sudo", "| xargs rm", "&&", ";"]):
        return None
    key = f"bash:{_cmd_hash(stripped, cwd)}"
    try:
        conn = _connect(session_id)
        row = conn.execute(
            "SELECT last_seen, turn FROM entries WHERE key = ?",
            (key,),
        ).fetchone()
        if row:
            last_seen, turn = row
            age = time.time() - last_seen
            # Re-block within 10 minutes
            if age < 600:
                conn.execute(
                    "UPDATE entries SET last_seen = ?, turn = turn + 1 WHERE key = ?",
                    (time.time(), key),
                )
                conn.commit()
                conn.close()
                return {
                    "action": "block_bash",
                    "command": stripped,
                    "reason": "already_ran_recently",
                    "pointer": f"[TOKEN SAVER] Command already ran {int(age)}s ago this session. Skipping re-run. If you need fresh output, add a comment like '# force' or a unique arg.",
                    "saved_chars": 5000,  # conservative estimate
                }
        conn.execute(
            "INSERT OR REPLACE INTO entries (key, kind, path, content_hash, file_mtime, size, last_seen, turn, tool) "
            "VALUES (?, 'bash', ?, NULL, NULL, NULL, ?, 1, 'Bash')",
            (key, stripped[:500], time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        return None
    return None


def cleanup_stale(session_id: str | None = None):
    """Remove entries older than TTL."""
    session_id = session_id or _current_session()
    try:
        conn = _connect(session_id)
        conn.execute("DELETE FROM entries WHERE last_seen < ?", (time.time() - ENTRY_TTL_SEC,))
        # Cap per-session entries
        conn.execute(
            "DELETE FROM entries WHERE key NOT IN "
            "(SELECT key FROM entries ORDER BY last_seen DESC LIMIT ?)",
            (MAX_ENTRIES_PER_SESSION,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def stats(session_id: str | None = None) -> dict:
    session_id = session_id or _current_session()
    try:
        conn = _connect(session_id)
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(turn - 1), 0) FROM entries"
        ).fetchone()
        conn.close()
        return {"entries": r[0], "bytes_cached": r[1], "dedup_hits": r[2]}
    except Exception:
        return {"entries": 0, "bytes_cached": 0, "dedup_hits": 0}
