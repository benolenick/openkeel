#!/usr/bin/env python3
"""Research Shards — isolated knowledge bases for AI research.

Each shard is a separate SQLite DB that can be:
- Toggled on/off during Calcifer chats
- Searched independently without polluting main Hyphae
- Deleted without affecting project memory
- Populated from arXiv, papers, or manual ingestion

Usage:
  shard = ResearchShard.get_or_create("routing-papers")
  shard.add_paper(arxiv_id, title, abstract, url, insights)
  results = shard.search("mixture of experts routing", top_k=5)
  shard.toggle(False)  # disable
  shard.delete()  # nuke it
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

SHARDS_DIR = Path.home() / ".openkeel" / "research_shards"


def _ensure_shards_dir() -> None:
    """Create shards directory if needed."""
    SHARDS_DIR.mkdir(parents=True, exist_ok=True)


# ── ResearchPaper ──────────────────────────────────────────────────────────────

@dataclass
class ResearchPaper:
    """A single paper in a shard."""
    paper_id: str          # arxiv ID, doi, or unique key
    title: str
    abstract: str
    url: str
    authors: list[str] = field(default_factory=list)
    published: str = ""    # ISO date
    insights: str = ""     # extracted key findings
    tags: list[str] = field(default_factory=list)
    added_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "abstract": self.abstract,
            "url": self.url,
            "authors": self.authors,
            "published": self.published,
            "insights": self.insights,
            "tags": self.tags,
            "added_at": self.added_at,
        }


# ── ResearchShard ─────────────────────────────────────────────────────────────

class ResearchShard:
    """Isolated research knowledge base (separate SQLite DB)."""

    def __init__(self, name: str) -> None:
        """Load or create a shard by name."""
        _ensure_shards_dir()
        self.name = name
        self.db_path = SHARDS_DIR / f"{name}.db"
        self.enabled = True
        self._init_db()

    def _init_db(self) -> None:
        """Create schema if needed."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                abstract TEXT,
                url TEXT,
                authors TEXT,
                published TEXT,
                insights TEXT,
                tags TEXT,
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        conn.close()

    def add_paper(
        self,
        paper_id: str,
        title: str,
        abstract: str,
        url: str,
        authors: list[str] | None = None,
        published: str = "",
        insights: str = "",
        tags: list[str] | None = None,
    ) -> ResearchPaper:
        """Add or update a paper in this shard."""
        paper = ResearchPaper(
            paper_id=paper_id,
            title=title,
            abstract=abstract,
            url=url,
            authors=authors or [],
            published=published,
            insights=insights,
            tags=tags or [],
        )

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT OR REPLACE INTO papers
            (paper_id, title, abstract, url, authors, published, insights, tags, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            paper_id, title, abstract, url,
            json.dumps(paper.authors),
            published,
            insights,
            json.dumps(paper.tags),
            paper.added_at,
            datetime.utcnow().isoformat(),
        ))
        conn.commit()
        conn.close()
        return paper

    def search(self, query: str, top_k: int = 5) -> list[ResearchPaper]:
        """Full-text search across title, abstract, insights.

        Returns top_k papers ranked by relevance.
        """
        conn = sqlite3.connect(str(self.db_path))
        # Simple rank: count query word hits in searchable fields
        query_words = set(query.lower().split())

        rows = conn.execute("""
            SELECT paper_id, title, abstract, url, authors, published, insights, tags, added_at
            FROM papers
        """).fetchall()
        conn.close()

        results = []
        for row in rows:
            paper_id, title, abstract, url, authors_json, published, insights, tags_json, added_at = row

            # Calculate relevance score
            searchable = f"{title} {abstract} {insights}".lower()
            hits = sum(1 for w in query_words if w in searchable)

            if hits > 0:
                paper = ResearchPaper(
                    paper_id=paper_id,
                    title=title,
                    abstract=abstract,
                    url=url,
                    authors=json.loads(authors_json or "[]"),
                    published=published,
                    insights=insights,
                    tags=json.loads(tags_json or "[]"),
                    added_at=added_at,
                )
                results.append((hits, paper))

        # Sort by relevance (hits desc), return top_k
        results.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in results[:top_k]]

    def list_papers(self, limit: int = 100, offset: int = 0) -> list[ResearchPaper]:
        """List all papers in this shard."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute("""
            SELECT paper_id, title, abstract, url, authors, published, insights, tags, added_at
            FROM papers
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        conn.close()

        papers = []
        for row in rows:
            paper_id, title, abstract, url, authors_json, published, insights, tags_json, added_at = row
            paper = ResearchPaper(
                paper_id=paper_id,
                title=title,
                abstract=abstract,
                url=url,
                authors=json.loads(authors_json or "[]"),
                published=published,
                insights=insights,
                tags=json.loads(tags_json or "[]"),
                added_at=added_at,
            )
            papers.append(paper)
        return papers

    def get_paper(self, paper_id: str) -> Optional[ResearchPaper]:
        """Fetch a single paper by ID."""
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute("""
            SELECT paper_id, title, abstract, url, authors, published, insights, tags, added_at
            FROM papers WHERE paper_id = ?
        """, (paper_id,)).fetchone()
        conn.close()

        if not row:
            return None

        paper_id, title, abstract, url, authors_json, published, insights, tags_json, added_at = row
        return ResearchPaper(
            paper_id=paper_id,
            title=title,
            abstract=abstract,
            url=url,
            authors=json.loads(authors_json or "[]"),
            published=published,
            insights=insights,
            tags=json.loads(tags_json or "[]"),
            added_at=added_at,
        )

    def delete_paper(self, paper_id: str) -> bool:
        """Remove a paper from this shard."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
        conn.commit()
        conn.close()
        return True

    def clear(self) -> None:
        """Delete all papers from this shard (keep DB structure)."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DELETE FROM papers")
        conn.commit()
        conn.close()

    def count(self) -> int:
        """Number of papers in this shard."""
        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        conn.close()
        return count

    def toggle(self, enabled: bool) -> None:
        """Enable or disable this shard."""
        self.enabled = enabled

    def is_enabled(self) -> bool:
        """Check if shard is active."""
        return self.enabled

    def delete(self) -> None:
        """Permanently delete this shard and its DB."""
        if self.db_path.exists():
            self.db_path.unlink()

    def summary(self) -> str:
        """Human-readable shard status."""
        count = self.count()
        status = "enabled" if self.enabled else "disabled"
        return f"ResearchShard '{self.name}' [{status}] — {count} papers"

    @classmethod
    def list_all(cls) -> list[str]:
        """List all available shards."""
        _ensure_shards_dir()
        return [f.stem for f in SHARDS_DIR.glob("*.db")]

    @classmethod
    def get_or_create(cls, name: str) -> ResearchShard:
        """Load existing shard or create new one."""
        return cls(name)
