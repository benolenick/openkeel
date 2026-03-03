"""Local memory backend — SQLite + FTS5, zero dependencies.

Stores facts in a local SQLite database with full-text search.
No server, no embeddings, no GPU — just fast keyword search with
BM25 ranking.  Drop-in replacement for the FV HTTP client when
you don't need (or can't run) a full embedding service.

CLI usage:
    openkeel remember "we decided to use asyncio.Queue for the buffer"
    openkeel remember "buffer drops events at >1000/sec" --project shallots --tag bug
    openkeel recall "buffer overflow"
    openkeel recall "what did we decide about the queue" --top 5
    openkeel memory stats
    openkeel memory export --format jsonl
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".openkeel" / "memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    project TEXT DEFAULT '',
    tag TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created_at REAL NOT NULL,
    session_id TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text,
    project,
    tag,
    content=facts,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text, project, tag)
    VALUES (new.id, new.text, new.project, new.tag);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, project, tag)
    VALUES ('delete', old.id, old.text, old.project, old.tag);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, project, tag)
    VALUES ('delete', old.id, old.text, old.project, old.tag);
    INSERT INTO facts_fts(rowid, text, project, tag)
    VALUES (new.id, new.text, new.project, new.tag);
END;
"""


class LocalMemory:
    """Local SQLite + FTS5 memory backend."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    def remember(
        self,
        text: str,
        project: str = "",
        tag: str = "",
        source: str = "",
        session_id: str = "",
    ) -> int:
        """Store a fact. Returns the row ID."""
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO facts (text, project, tag, source, created_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text, project, tag, source, time.time(), session_id),
        )
        conn.commit()
        logger.info("Memory: stored fact #%d (%d chars)", cur.lastrowid, len(text))
        return cur.lastrowid

    def remember_batch(
        self,
        facts: list[str],
        project: str = "",
        tag: str = "",
        source: str = "",
    ) -> int:
        """Store multiple facts. Returns count stored."""
        conn = self._get_conn()
        now = time.time()
        conn.executemany(
            "INSERT INTO facts (text, project, tag, source, created_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(f, project, tag, source, now, "") for f in facts],
        )
        conn.commit()
        return len(facts)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        top_k: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Full-text search with BM25 ranking. Returns list of hits."""
        conn = self._get_conn()

        # Build FTS5 query — quote terms for safety
        fts_query = " OR ".join(
            f'"{w}"' for w in query.split() if w.strip()
        )
        if not fts_query:
            return []

        if project:
            sql = """
                SELECT f.id, f.text, f.project, f.tag, f.source,
                       f.created_at, f.session_id,
                       rank AS score
                FROM facts_fts fts
                JOIN facts f ON f.id = fts.rowid
                WHERE facts_fts MATCH ?
                  AND f.project = ?
                ORDER BY rank
                LIMIT ?
            """
            rows = conn.execute(sql, (fts_query, project, top_k)).fetchall()
        else:
            sql = """
                SELECT f.id, f.text, f.project, f.tag, f.source,
                       f.created_at, f.session_id,
                       rank AS score
                FROM facts_fts fts
                JOIN facts f ON f.id = fts.rowid
                WHERE facts_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = conn.execute(sql, (fts_query, top_k)).fetchall()

        return [
            {
                "id": r["id"],
                "text": r["text"],
                "project": r["project"],
                "tag": r["tag"],
                "source": r["source"],
                "score": abs(r["score"]),  # FTS5 rank is negative (lower=better)
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def recent(self, limit: int = 10, project: str = "") -> list[dict[str, Any]]:
        """Get most recent facts."""
        conn = self._get_conn()
        if project:
            rows = conn.execute(
                "SELECT * FROM facts WHERE project = ? ORDER BY created_at DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM facts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return memory statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        projects = conn.execute(
            "SELECT project, COUNT(*) as cnt FROM facts "
            "WHERE project != '' GROUP BY project ORDER BY cnt DESC"
        ).fetchall()
        tags = conn.execute(
            "SELECT tag, COUNT(*) as cnt FROM facts "
            "WHERE tag != '' GROUP BY tag ORDER BY cnt DESC"
        ).fetchall()
        return {
            "total_facts": total,
            "db_path": str(self.db_path),
            "db_size_kb": round(self.db_path.stat().st_size / 1024, 1) if self.db_path.exists() else 0,
            "projects": {r["project"]: r["cnt"] for r in projects},
            "tags": {r["tag"]: r["cnt"] for r in tags},
        }

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def delete(self, fact_id: int) -> bool:
        """Delete a fact by ID."""
        conn = self._get_conn()
        conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        conn.commit()
        return True

    def export_jsonl(self) -> str:
        """Export all facts as JSONL string."""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM facts ORDER BY created_at").fetchall()
        lines = [json.dumps(dict(r)) for r in rows]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # FV-compatible interface (so existing code can use this as drop-in)
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return True

    def memorize(self, fact: str, metadata: dict[str, Any] | None = None) -> bool:
        project = (metadata or {}).get("project", "")
        tag = (metadata or {}).get("tag", "")
        self.remember(fact, project=project, tag=tag)
        return True

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return self.recall(query, top_k=top_k)
