"""Generic memory backend client for cross-session learning.

Connects to any HTTP service that exposes these endpoints:

    GET  /health              — returns 200 if alive
    POST /memorize            — store a fact: {"fact": "text", "metadata": {...}}
    POST /search              — semantic search: {"query": "text", "top_k": N}
                                returns: {"results": [{"text": "...", "score": 0.8}, ...]}
    POST /reflect (optional)  — LLM-powered extraction: {"text": "text"}
                                returns: {"reflection": "..."}

The default implementation targets FV v3.0 but any service implementing
the same REST contract will work.  All methods degrade gracefully — if
the backend is unreachable, calls return empty/False instead of raising.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Cache health for 30 seconds so the proxy shell doesn't hammer the
# backend on every single command.
_HEALTH_CACHE_TTL = 30.0


class MemoryClient:
    """Thin HTTP client for a memory backend (FV-compatible API)."""

    def __init__(self, endpoint: str = "http://127.0.0.1:8000", timeout: int = 15):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._healthy: bool | None = None
        self._health_checked_at: float = 0.0

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the backend responds to /health within timeout."""
        now = time.monotonic()
        if self._healthy is not None and (now - self._health_checked_at) < _HEALTH_CACHE_TTL:
            return self._healthy

        try:
            req = urllib.request.Request(f"{self.endpoint}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self._healthy = resp.status == 200
        except Exception:
            self._healthy = False

        self._health_checked_at = now
        return self._healthy

    def health_info(self) -> dict[str, Any]:
        """Return full /health payload (model name, fact count, etc.)."""
        try:
            req = urllib.request.Request(f"{self.endpoint}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def memorize(self, fact: str, metadata: dict[str, Any] | None = None) -> bool:
        """Store a fact. Returns True on success."""
        if not self.is_available():
            return False
        payload: dict[str, Any] = {"fact": fact}
        if metadata:
            payload["metadata"] = metadata
        try:
            self._post("/memorize", payload)
            logger.info("Memory: stored fact (%d chars)", len(fact))
            return True
        except Exception as exc:
            logger.warning("Memory: memorize failed: %s", exc)
            return False

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Semantic search. Returns list of {"text": ..., "score": ...}."""
        if not self.is_available():
            return []
        try:
            data = self._post("/search", {"query": query, "top_k": top_k})
            results = data.get("results", [])
            logger.info("Memory: search '%s' returned %d results", query[:40], len(results))
            return results
        except Exception as exc:
            logger.warning("Memory: search failed: %s", exc)
            return []

    def reflect(self, text: str) -> str:
        """Ask the backend to extract learnings from text (LLM-powered).

        Returns the reflection string, or empty string on failure.
        """
        if not self.is_available():
            return ""
        try:
            data = self._post("/reflect", {"text": text}, timeout=60)
            return data.get("reflection", data.get("response", ""))
        except Exception as exc:
            logger.warning("Memory: reflect failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Convenience: batch operations
    # ------------------------------------------------------------------

    def memorize_batch(self, facts: list[str], metadata: dict[str, Any] | None = None) -> int:
        """Store multiple facts. Returns count of successfully stored."""
        stored = 0
        for fact in facts:
            if self.memorize(fact, metadata):
                stored += 1
        return stored

    def search_multi(self, queries: list[str], top_k: int = 3) -> list[dict[str, Any]]:
        """Run multiple searches, deduplicate results by text."""
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for query in queries:
            for hit in self.search(query, top_k):
                text = hit.get("text", "")
                if text not in seen:
                    seen.add(text)
                    results.append(hit)
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        """POST JSON, return parsed response."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
