"""Journal — session narrative store for the OpenKeel knowledge system.

Stores time-stamped entries in the ``journal`` table created by
``knowledge_db.init_db``.  Full-text search is provided by the
``journal_fts`` FTS5 virtual table; optional semantic search delegates
to the embeddings service on localhost:7437.

Typical usage::

    from openkeel.integrations.journal import Journal

    j = Journal()
    eid = j.add_entry("Figured out the asyncio queue sizing.", project="shallots")
    j.add_session_summary(
        session_id="abc123",
        project="shallots",
        accomplishments=["Implemented ring buffer", "Fixed FTS triggers"],
        decisions=["Use asyncio.Queue with maxsize=1000"],
        blockers=["rockyou.txt not yet on jagg"],
    )
    hits = j.search_keyword("queue buffer")
    print(j.get_recent_narrative(project="shallots"))
    j.close()
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from openkeel.integrations.knowledge_db import init_db

logger = logging.getLogger(__name__)

_EMBEDDINGS_BASE = "http://localhost:7437"


class Journal:
    """SQLite + FTS5 journal with optional semantic search via embeddings service."""

    def __init__(self, db_path: str | None = None) -> None:
        self._conn: sqlite3.Connection = init_db(db_path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_entry(
        self,
        body: str,
        title: str = "",
        project: str = "",
        entry_type: str = "manual",
        tags: str = "",
        session_id: str = "",
        mission_name: str = "",
    ) -> int:
        """Insert a journal entry and return its row ID.

        Also fires a background embeddings-index request (fire-and-forget;
        silently no-ops if the embeddings server is not running).
        """
        cur = self._conn.execute(
            """
            INSERT INTO journal
                (session_id, project, timestamp, entry_type, title, body, tags, mission_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, project, time.time(), entry_type, title, body, tags, mission_name),
        )
        self._conn.commit()
        entry_id: int = cur.lastrowid  # type: ignore[assignment]
        logger.info(
            "Journal: added entry #%d type=%s project=%r (%d chars)",
            entry_id,
            entry_type,
            project,
            len(body),
        )
        self._index_async(entry_id, body)
        return entry_id

    def add_session_summary(
        self,
        session_id: str,
        project: str,
        accomplishments: list[str],
        decisions: list[str],
        blockers: list[str],
    ) -> int:
        """Format and store a structured end-of-session summary.

        Each decision is also auto-promoted to the wiki (category='decisions').

        Returns the journal entry row ID.
        """
        lines: list[str] = []

        lines.append("## Accomplishments")
        if accomplishments:
            for item in accomplishments:
                lines.append(f"- {item}")
        else:
            lines.append("- (none)")

        lines.append("")
        lines.append("## Decisions")
        if decisions:
            for item in decisions:
                lines.append(f"- {item}")
        else:
            lines.append("- (none)")

        lines.append("")
        lines.append("## Blockers")
        if blockers:
            for item in blockers:
                lines.append(f"- {item}")
        else:
            lines.append("- (none)")

        body = "\n".join(lines)
        ts_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title = f"Session summary — {ts_str}"

        entry_id = self.add_entry(
            body=body,
            title=title,
            project=project,
            entry_type="session_end",
            session_id=session_id,
        )

        self._promote_decisions(decisions, project)
        return entry_id

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_entries(
        self,
        project: str = "",
        limit: int = 20,
        entry_type: str = "",
    ) -> list[dict[str, Any]]:
        """Return journal entries ordered by timestamp DESC.

        Optionally filtered by *project* and/or *entry_type*.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if project:
            clauses.append("project = ?")
            params.append(project)
        if entry_type:
            clauses.append("entry_type = ?")
            params.append(entry_type)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        sql = f"""
            SELECT id, session_id, project, timestamp, entry_type,
                   title, body, tags, mission_name
            FROM journal
            {where}
            ORDER BY timestamp DESC
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_keyword(
        self,
        query: str,
        top_k: int = 10,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """FTS5 BM25 full-text search over journal entries.

        Returns results ordered by relevance (best match first).
        """
        fts_query = " OR ".join(
            f'"{w}"' for w in query.split() if w.strip()
        )
        if not fts_query:
            return []

        if project:
            sql = """
                SELECT j.id, j.session_id, j.project, j.timestamp,
                       j.entry_type, j.title, j.body, j.tags, j.mission_name,
                       rank AS score
                FROM journal_fts fts
                JOIN journal j ON j.id = fts.rowid
                WHERE journal_fts MATCH ?
                  AND j.project = ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self._conn.execute(sql, (fts_query, project, top_k)).fetchall()
        else:
            sql = """
                SELECT j.id, j.session_id, j.project, j.timestamp,
                       j.entry_type, j.title, j.body, j.tags, j.mission_name,
                       rank AS score
                FROM journal_fts fts
                JOIN journal j ON j.id = fts.rowid
                WHERE journal_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self._conn.execute(sql, (fts_query, top_k)).fetchall()

        return [
            {
                **dict(r),
                "score": abs(r["score"]),  # FTS5 rank is negative (lower = better)
            }
            for r in rows
        ]

    def search_semantic(
        self,
        query: str,
        top_k: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Semantic search via the embeddings service.

        Falls back to :meth:`search_keyword` if the service is unavailable.
        """
        try:
            from openkeel.integrations.embeddings_client import EmbeddingsClient  # lazy

            client = EmbeddingsClient(base_url=_EMBEDDINGS_BASE)
            hits = client.search(query, top_k=top_k, source_types=["journal"])
            if hits:
                # Resolve source_ids back to full journal entries
                entries: list[dict[str, Any]] = []
                seen: set[int] = set()
                for hit in hits:
                    src_id = hit.get("source_id")
                    if src_id is None or src_id in seen:
                        continue
                    seen.add(src_id)
                    row = self._conn.execute(
                        "SELECT * FROM journal WHERE id = ?", (src_id,)
                    ).fetchone()
                    if row:
                        entry = dict(row)
                        entry["score"] = hit.get("score", 0)
                        entries.append(entry)
                if entries:
                    logger.debug("Journal: semantic search returned %d hits", len(entries))
                    return entries
        except Exception as exc:
            logger.debug("Journal: embeddings search unavailable (%s), falling back", exc)

        return self.search_keyword(query, top_k=top_k, project=project)

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def get_recent_narrative(self, project: str = "", limit: int = 5) -> str:
        """Return a formatted string of recent entries for context injection.

        Format::

            ## Recent Journal

            ### Entry title (YYYY-MM-DD)
            Entry body text...

            ### Another entry (YYYY-MM-DD)
            ...
        """
        entries = self.get_entries(project=project, limit=limit)
        if not entries:
            return "## Recent Journal\n\n(no entries)"

        parts: list[str] = ["## Recent Journal"]
        for entry in entries:
            ts = entry.get("timestamp", 0.0)
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            heading_title = entry.get("title") or f"Entry #{entry['id']}"
            parts.append(f"\n### {heading_title} ({date_str})")
            parts.append(entry.get("body", "").strip())

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _index_async(self, entry_id: int, text: str) -> None:
        """Fire-and-forget POST to the embeddings service to index an entry.

        Uses only stdlib (``urllib.request``).  All exceptions are swallowed
        silently so callers are never interrupted by a missing service.
        """
        try:
            payload = json.dumps(
                {"source_type": "journal", "source_id": entry_id, "text": text}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{_EMBEDDINGS_BASE}/index",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                logger.debug(
                    "Journal: indexed entry #%d via embeddings service (status %s)",
                    entry_id,
                    resp.status,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Journal: embeddings indexing skipped for #%d: %s", entry_id, exc)

    def _promote_decisions(self, decisions: list[str], project: str) -> None:
        """Upsert each decision string as a wiki page (category='decisions').

        Wiki is imported lazily to avoid circular imports between journal and wiki.
        """
        if not decisions:
            return

        try:
            from openkeel.integrations.wiki import Wiki  # lazy import

            wiki = Wiki()
            for decision in decisions:
                decision = decision.strip()
                if not decision:
                    continue
                wiki.add_page(
                    title=f"Decision: {decision[:60]}",
                    body=decision,
                    category="decisions",
                    project=project,
                    source_type="journal",
                )
                logger.debug("Journal: promoted decision to wiki")
            wiki.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Journal: decision promotion failed: %s", exc)
