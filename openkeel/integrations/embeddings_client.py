"""Thin HTTP client for the OpenKeel embeddings server (localhost:7437).

Mirrors the pattern of MemoryClient.  Endpoints: GET /health, POST /search,
POST /index, POST /reindex.  All methods degrade gracefully on error.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_HEALTH_CACHE_TTL = 30.0  # seconds


class EmbeddingsClient:
    """Thin HTTP client for the OpenKeel embeddings server."""

    def __init__(self, base_url: str = "http://localhost:7437") -> None:
        self.base_url = base_url.rstrip("/")
        self._available: bool | None = None
        self._available_checked_at: float = 0

    def is_available(self) -> bool:
        """GET /health, cache result for 30 s. Returns False on any error."""
        now = time.monotonic()
        if self._available is not None and (now - self._available_checked_at) < _HEALTH_CACHE_TTL:
            return self._available
        try:
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self._available = resp.status == 200
        except Exception as exc:
            logger.warning("EmbeddingsClient: health check failed: %s", exc)
            self._available = False
        self._available_checked_at = now
        return self._available

    def search(
        self,
        query: str,
        top_k: int = 5,
        source_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """POST /search. Returns list of {source_type, source_id, chunk_index, text_preview, score}."""
        if not self.is_available():
            return []
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "source_types": source_types or [],
        }
        try:
            data = self._post("/search", payload)
            # Server returns {"results": [...]}
            if isinstance(data, dict):
                results = data.get("results", [])
            elif isinstance(data, list):
                results = data
            else:
                logger.warning("EmbeddingsClient: unexpected search response type: %s", type(data))
                return []
            logger.info("EmbeddingsClient: search '%s' returned %d results", query[:40], len(results))
            return results
        except Exception as exc:
            logger.warning("EmbeddingsClient: search failed: %s", exc)
            return []

    def index(self, source_type: str, source_id: str, text: str) -> bool:
        """POST /index. Fire-and-forget — returns True on success, False on any error."""
        if not self.is_available():
            return False
        payload: dict[str, Any] = {"source_type": source_type, "source_id": source_id, "text": text}
        try:
            self._post("/index", payload)
            logger.info("EmbeddingsClient: indexed %s/%s (%d chars)", source_type, source_id, len(text))
            return True
        except Exception as exc:
            logger.warning("EmbeddingsClient: index failed: %s", exc)
            return False

    def reindex(self) -> bool:
        """POST /reindex. Returns True on success. May take a while."""
        try:
            self._post("/reindex", {}, timeout=300)
            logger.info("EmbeddingsClient: reindex completed")
            return True
        except Exception as exc:
            logger.warning("EmbeddingsClient: reindex failed: %s", exc)
            return False

    def _post(self, path: str, payload: dict[str, Any], timeout: int = 15) -> Any:
        """POST JSON to *path* and return the parsed response body."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
