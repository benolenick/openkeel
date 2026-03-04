"""Wiki knowledge pages for OpenKeel.

Stores structured knowledge pages in SQLite with FTS5 full-text search
and optional semantic search via the embeddings service.

Usage::

    from openkeel.integrations.wiki import Wiki

    wiki = Wiki()
    page_id = wiki.add_page("SSH Hardening", "Disable root login...", category="ops")
    result  = wiki.get_page("ssh-hardening")
    pages   = wiki.list_pages(category="ops")
    context = wiki.get_relevant_pages("how to harden ssh")
    wiki.close()
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import threading
import urllib.error
import urllib.request
from typing import Any

from openkeel.integrations.knowledge_db import init_db

logger = logging.getLogger(__name__)

_EMBEDDINGS_URL = "http://localhost:7437/index"
_EMBEDDINGS_TIMEOUT = 10  # seconds


class Wiki:
    """Persistent wiki-style knowledge pages backed by SQLite + FTS5."""

    def __init__(self, db_path: str | None = None) -> None:
        self._conn: sqlite3.Connection = init_db(db_path)
        logger.debug("Wiki: opened knowledge DB")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
            logger.debug("Wiki: closed connection")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _slugify(self, title: str) -> str:
        """Convert *title* to a URL-safe slug (max 80 chars)."""
        slug = title.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")
        return slug[:80]

    def _index_async(self, page_id: int, text: str) -> None:
        """Fire-and-forget POST to the embeddings service (stdlib only).

        Mirrors the pattern used in journal.py.  Failures are logged as
        warnings and never propagate to the caller.
        """
        def _post() -> None:
            payload = json.dumps(
                {"source_type": "wiki", "source_id": page_id, "text": text}
            ).encode("utf-8")
            req = urllib.request.Request(
                _EMBEDDINGS_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=_EMBEDDINGS_TIMEOUT):
                    logger.debug("Wiki: indexed page %d in embeddings service", page_id)
            except urllib.error.URLError as exc:
                logger.debug("Wiki: embeddings service unavailable (%s) — skipping index", exc.reason)
            except Exception as exc:
                logger.warning("Wiki: _index_async error for page %d: %s", page_id, exc)

        thread = threading.Thread(target=_post, daemon=True, name=f"wiki-index-{page_id}")
        thread.start()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add_page(
        self,
        title: str,
        body: str,
        category: str = "",
        project: str = "",
        tags: str = "",
        source_type: str = "manual",
    ) -> int:
        """Create a wiki page or append to it if the slug already exists.

        If a page with the same slug exists its body is extended with a
        ``\\n\\n---\\n\\n`` separator, ``updated_at`` is refreshed, and the
        merged page is re-indexed.

        Parameters
        ----------
        title:
            Human-readable page title.  Used to derive the slug.
        body:
            Markdown body text.
        category:
            Free-form category label (e.g. "ops", "dev", "security").
        project:
            Project tag for filtering.
        tags:
            Comma-separated keyword tags.
        source_type:
            Origin of the content: ``"manual"``, ``"journal"``, ``"mission"``.

        Returns
        -------
        int
            Database ``id`` of the created or updated page.
        """
        slug = self._slugify(title)
        now = time.time()

        existing = self._conn.execute(
            "SELECT id, body FROM wiki_pages WHERE slug = ?", (slug,)
        ).fetchone()

        if existing:
            page_id: int = existing["id"]
            merged_body = existing["body"] + "\n\n---\n\n" + body
            self._conn.execute(
                "UPDATE wiki_pages SET body = ?, updated_at = ? WHERE id = ?",
                (merged_body, now, page_id),
            )
            self._conn.commit()
            logger.info("Wiki: appended to page %r (id=%d)", slug, page_id)
            self._index_async(page_id, title + " " + merged_body)
            return page_id

        cur = self._conn.execute(
            """
            INSERT INTO wiki_pages
                (slug, title, body, category, project, tags, created_at, updated_at, source_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (slug, title, body, category, project, tags, now, now, source_type),
        )
        self._conn.commit()
        page_id = cur.lastrowid
        logger.info("Wiki: created page %r (id=%d)", slug, page_id)
        self._index_async(page_id, title + " " + body)
        return page_id

    def link_pages(self, from_slug: str, to_slug: str) -> bool:
        """Create a directed cross-reference from *from_slug* to *to_slug*.

        Returns True if the link was created (or already existed), False if
        either slug does not exist.
        """
        from_row = self._conn.execute(
            "SELECT id FROM wiki_pages WHERE slug = ?", (from_slug,)
        ).fetchone()
        to_row = self._conn.execute(
            "SELECT id FROM wiki_pages WHERE slug = ?", (to_slug,)
        ).fetchone()

        if not from_row or not to_row:
            logger.warning(
                "Wiki: link_pages failed — slug not found (%r → %r)", from_slug, to_slug
            )
            return False

        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO wiki_links (from_page_id, to_page_id) VALUES (?, ?)",
                (from_row["id"], to_row["id"]),
            )
            self._conn.commit()
            logger.info("Wiki: linked %r → %r", from_slug, to_slug)
            return True
        except sqlite3.Error as exc:
            logger.warning("Wiki: link_pages error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_page(self, slug: str) -> dict[str, Any] | None:
        """Return full page content by slug, or None if not found.

        The returned dict includes a ``links`` key: a list of slugs that
        this page links **to** or that link **to** this page (union of
        both directions).
        """
        row = self._conn.execute(
            "SELECT * FROM wiki_pages WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            return None

        page: dict[str, Any] = dict(row)
        page_id: int = page["id"]

        # Outbound links (pages this page points to)
        outbound = self._conn.execute(
            """
            SELECT wp.slug FROM wiki_links wl
            JOIN wiki_pages wp ON wp.id = wl.to_page_id
            WHERE wl.from_page_id = ?
            """,
            (page_id,),
        ).fetchall()

        # Inbound links (pages that point to this page)
        inbound = self._conn.execute(
            """
            SELECT wp.slug FROM wiki_links wl
            JOIN wiki_pages wp ON wp.id = wl.from_page_id
            WHERE wl.to_page_id = ?
            """,
            (page_id,),
        ).fetchall()

        linked: list[str] = list(
            {r["slug"] for r in outbound} | {r["slug"] for r in inbound}
        )
        linked.sort()
        page["links"] = linked

        return page

    def list_pages(
        self, category: str = "", project: str = ""
    ) -> list[dict[str, Any]]:
        """Return a filtered list of pages ordered by ``updated_at`` DESC.

        Each item contains: ``id``, ``slug``, ``title``, ``category``,
        ``project``, ``updated_at``.
        """
        base_sql = (
            "SELECT id, slug, title, category, project, updated_at "
            "FROM wiki_pages"
        )
        conditions: list[str] = []
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category)
        if project:
            conditions.append("project = ?")
            params.append(project)

        if conditions:
            base_sql += " WHERE " + " AND ".join(conditions)
        base_sql += " ORDER BY updated_at DESC"

        rows = self._conn.execute(base_sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_categories(self) -> list[dict[str, Any]]:
        """Return ``[{category, count}]`` ordered by count DESC."""
        rows = self._conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM wiki_pages
            WHERE category != ''
            GROUP BY category
            ORDER BY count DESC
            """
        ).fetchall()
        return [{"category": r["category"], "count": r["count"]} for r in rows]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_keyword(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """FTS5 BM25 keyword search over ``wiki_fts``.

        Returns a list of page dicts with an added ``score`` field.
        """
        fts_query = " OR ".join(
            f'"{w}"' for w in query.split() if w.strip()
        )
        if not fts_query:
            return []

        sql = """
            SELECT wp.id, wp.slug, wp.title, wp.body, wp.category,
                   wp.project, wp.tags, wp.updated_at,
                   rank AS score
            FROM wiki_fts fts
            JOIN wiki_pages wp ON wp.id = fts.rowid
            WHERE wiki_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = self._conn.execute(sql, (fts_query, top_k)).fetchall()
        return [
            {**dict(r), "score": abs(r["score"])}
            for r in rows
        ]

    def search_semantic(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Semantic search via EmbeddingsClient, falling back to keyword search.

        The import of EmbeddingsClient is deferred so the module loads even
        when the embeddings service is not installed.
        """
        try:
            from openkeel.integrations.embeddings_client import EmbeddingsClient  # noqa: PLC0415

            client = EmbeddingsClient()
            results = client.search(query, top_k=top_k, source_types=["wiki"])
            if results:
                # Resolve source_ids back to full page dicts (deduplicate)
                pages: list[dict[str, Any]] = []
                seen: set[int] = set()
                for hit in results:
                    source_id = hit.get("source_id")
                    if source_id is None or source_id in seen:
                        continue
                    seen.add(source_id)
                    row = self._conn.execute(
                        "SELECT * FROM wiki_pages WHERE id = ?", (source_id,)
                    ).fetchone()
                    if row:
                        page = dict(row)
                        page["score"] = hit.get("score", 0.0)
                        pages.append(page)
                if pages:
                    logger.debug(
                        "Wiki: semantic search for %r returned %d hits", query[:40], len(pages)
                    )
                    return pages
        except ImportError:
            logger.debug("Wiki: EmbeddingsClient not available — falling back to keyword search")
        except Exception as exc:
            logger.warning("Wiki: semantic search failed (%s) — falling back to keyword search", exc)

        return self.search_keyword(query, top_k=top_k)

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def get_relevant_pages(self, query: str, top_k: int = 3) -> str:
        """Return a formatted string of relevant pages for context injection.

        Format::

            ## Relevant Wiki Pages

            ### Page Title (category)
            Body text truncated to 500 chars...

            ### Another Page (category)
            ...

        Returns an empty string if no pages are found.
        """
        pages = self.search_semantic(query, top_k=top_k)
        if not pages:
            return ""

        sections: list[str] = ["## Relevant Wiki Pages"]
        for page in pages:
            title = page.get("title", "Untitled")
            category = page.get("category", "")
            body = page.get("body", "")
            truncated = body[:500] + ("..." if len(body) > 500 else "")
            header = f"### {title} ({category})" if category else f"### {title}"
            sections.append(f"\n{header}\n{truncated}")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Journal integration
    # ------------------------------------------------------------------

    def from_journal(
        self, journal_id: int, title: str = "", category: str = ""
    ) -> int:
        """Create a wiki page from a journal entry.

        Reads the journal entry with *journal_id* from the same database
        connection and creates (or appends to) a wiki page from it.

        Parameters
        ----------
        journal_id:
            Row ``id`` in the ``journal`` table.
        title:
            Override title.  If empty, the journal entry's ``title`` is used.
        category:
            Category for the new wiki page.

        Returns
        -------
        int
            Wiki page ``id``.

        Raises
        ------
        ValueError
            If no journal entry with the given id exists.
        """
        row = self._conn.execute(
            "SELECT * FROM journal WHERE id = ?", (journal_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Journal entry {journal_id!r} not found")

        page_title = title or row["title"] or f"Journal Entry {journal_id}"
        body = row["body"]
        project = row["project"] or ""
        tags = row["tags"] or ""

        page_id = self.add_page(
            title=page_title,
            body=body,
            category=category,
            project=project,
            tags=tags,
            source_type="journal",
        )
        logger.info(
            "Wiki: created page %d from journal entry %d", page_id, journal_id
        )
        return page_id
