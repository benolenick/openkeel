"""Hybrid memory — queries multiple backends, merges and deduplicates results.

Supports two backends:
  - "local"   → LocalMemory (SQLite + FTS5, always available)
  - "memoria" → MemoryClient (HTTP, semantic search via sentence-transformers)

Profile config controls which backends are active:

    learning:
      enabled: true
      backends: [local]              # dev on Windows — just SQLite
      # backends: [local, memoria]   # pentesting on jagg — both
      memoria_endpoint: http://localhost:8000

Writes always go to LocalMemory (the source of truth for session lessons).
Reads query all active backends, merge results, and deduplicate by text.
"""
from __future__ import annotations

import logging
from typing import Any

from openkeel.integrations.local_memory import LocalMemory

logger = logging.getLogger(__name__)


class HybridMemory:
    """Multi-backend memory with per-profile sharding."""

    def __init__(
        self,
        backends: list[str] | None = None,
        memoria_endpoint: str = "http://127.0.0.1:8000",
        memoria_timeout: int = 15,
    ):
        self._backend_names = backends or ["local"]
        self._local = LocalMemory()
        self._remote = None

        if "memoria" in self._backend_names:
            try:
                from openkeel.integrations.memory import MemoryClient
                self._remote = MemoryClient(
                    endpoint=memoria_endpoint,
                    timeout=memoria_timeout,
                )
                if not self._remote.is_available():
                    logger.warning("HybridMemory: Memoria at %s not reachable", memoria_endpoint)
                    self._remote = None
            except Exception as exc:
                logger.warning("HybridMemory: failed to init Memoria client: %s", exc)
                self._remote = None

    @property
    def backends_active(self) -> list[str]:
        """Return list of actually active backend names."""
        active = ["local"]
        if self._remote is not None:
            active.append("memoria")
        return active

    # ------------------------------------------------------------------
    # Store — always writes to local, optionally mirrors to Memoria
    # ------------------------------------------------------------------

    def remember(
        self,
        text: str,
        project: str = "",
        tag: str = "",
        source: str = "",
        session_id: str = "",
    ) -> int:
        """Store a fact locally. Returns the local row ID."""
        row_id = self._local.remember(
            text, project=project, tag=tag, source=source, session_id=session_id,
        )

        # Mirror to Memoria if active (fire-and-forget, don't block on failure)
        if self._remote is not None:
            try:
                self._remote.memorize(text, metadata={
                    "project": project,
                    "tag": tag,
                    "source": source,
                })
            except Exception:
                pass

        return row_id

    def remember_batch(
        self,
        facts: list[str],
        project: str = "",
        tag: str = "",
        source: str = "",
    ) -> int:
        """Store multiple facts locally. Returns count stored."""
        count = self._local.remember_batch(facts, project=project, tag=tag, source=source)

        if self._remote is not None:
            try:
                self._remote.memorize_batch(facts, metadata={
                    "project": project,
                    "tag": tag,
                    "source": source,
                })
            except Exception:
                pass

        return count

    # ------------------------------------------------------------------
    # Search — query all backends, merge, deduplicate
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        top_k: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Search all backends, merge and deduplicate by text."""
        results: list[dict[str, Any]] = []
        seen_texts: set[str] = set()

        # Local first (fast, always available)
        try:
            local_hits = self._local.recall(query, top_k=top_k, project=project)
            for hit in local_hits:
                text = hit.get("text", "")
                if text and text not in seen_texts:
                    seen_texts.add(text)
                    hit["_source"] = "local"
                    results.append(hit)
        except Exception as exc:
            logger.warning("HybridMemory: local recall failed: %s", exc)

        # Remote (semantic search — may find things FTS5 misses)
        if self._remote is not None:
            try:
                remote_hits = self._remote.search(query, top_k=top_k)
                for hit in remote_hits:
                    text = hit.get("text", "")
                    if text and text not in seen_texts:
                        seen_texts.add(text)
                        hit["_source"] = "memoria"
                        results.append(hit)
            except Exception as exc:
                logger.warning("HybridMemory: memoria recall failed: %s", exc)

        return results[:top_k * 2]  # allow extra results from combined sources

    def recall_multi(
        self,
        queries: list[str],
        top_k: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Run multiple queries, deduplicate across all."""
        seen_texts: set[str] = set()
        results: list[dict[str, Any]] = []

        for q in queries:
            for hit in self.recall(q, top_k=top_k, project=project):
                text = hit.get("text", "")
                if text not in seen_texts:
                    seen_texts.add(text)
                    results.append(hit)

        return results

    # ------------------------------------------------------------------
    # Stats / info
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return combined stats."""
        info = self._local.stats()
        info["backends"] = self.backends_active
        if self._remote is not None:
            try:
                remote_health = self._remote.health_info()
                info["memoria"] = remote_health
            except Exception:
                info["memoria"] = {"status": "unreachable"}
        return info

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._local.close()
