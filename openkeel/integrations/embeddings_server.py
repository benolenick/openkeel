"""Embeddings server for OpenKeel — semantic search over the knowledge base.

Loads ``sentence-transformers/all-MiniLM-L6-v2`` once, embeds text chunks,
stores vectors in ``knowledge.db``, and exposes a small HTTP API for search
and indexing.

HTTP endpoints
--------------
GET  /health   — liveness + stats
POST /search   — semantic search over indexed content
POST /index    — embed and store a single document
POST /reindex  — wipe and rebuild the entire embeddings table

Run directly::

    python -m openkeel.integrations.embeddings_server [--port 7437] [--db-path PATH]

Or programmatically::

    from openkeel.integrations.embeddings_server import run_server
    run_server(port=7437)
"""
from __future__ import annotations

import argparse
import array
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy dependencies — give a helpful message if missing
# ---------------------------------------------------------------------------

try:
    import numpy as np
except ImportError as _err:  # pragma: no cover
    raise SystemExit(
        "OpenKeel embeddings server requires numpy.\n"
        "Install it with:  pip install numpy"
    ) from _err

try:
    from sentence_transformers import SentenceTransformer
except ImportError as _err:  # pragma: no cover
    raise SystemExit(
        "OpenKeel embeddings server requires sentence-transformers.\n"
        "Install it with:  pip install sentence-transformers"
    ) from _err

from openkeel.integrations.knowledge_db import init_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL_NAME = "all-MiniLM-L6-v2"
_VECTOR_DIM = 384
_CHUNK_MAX = 500  # characters


# ---------------------------------------------------------------------------
# EmbeddingsIndex
# ---------------------------------------------------------------------------


class EmbeddingsIndex:
    """In-process embedding index backed by ``knowledge.db``.

    Vectors are stored as raw BLOB (float32 little-endian bytes) in the
    ``embeddings`` table.  A numpy matrix cache is maintained in memory for
    fast batch cosine-similarity search; the cache is lazily rebuilt whenever
    the underlying table changes.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialise the index.

        Parameters
        ----------
        db_path:
            Path to the SQLite knowledge database.  ``None`` uses the default
            ``~/.openkeel/knowledge.db``.
        """
        self._conn = init_db(db_path)
        self._model: SentenceTransformer | None = None

        # In-memory cache rebuilt from the DB on demand.
        self._vectors: np.ndarray | None = None   # shape (N, 384) float32
        self._ids: list[dict[str, Any]] | None = None  # parallel metadata list
        self._dirty: bool = True

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Lazy-load the sentence-transformers model.

        Only called once; subsequent calls are a no-op.
        """
        if self._model is not None:
            return
        logger.info("Loading sentence-transformers model '%s' …", _MODEL_NAME)
        t0 = time.monotonic()
        self._model = SentenceTransformer(_MODEL_NAME)
        elapsed = time.monotonic() - t0
        logger.info("Model loaded in %.2f s", elapsed)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> np.ndarray:
        """Encode *texts* and return an L2-normalised (N, 384) float32 array.

        Parameters
        ----------
        texts:
            One or more strings to encode.

        Returns
        -------
        np.ndarray
            Shape ``(len(texts), 384)``, dtype ``float32``.  Each row is a
            unit-length vector so dot-product == cosine similarity.
        """
        self._load_model()
        vecs = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        vecs = vecs.astype(np.float32)

        # L2 normalise each row in-place.
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        # Avoid division by zero for zero vectors.
        norms = np.where(norms == 0.0, 1.0, norms)
        vecs /= norms

        return vecs

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        """Split *text* into chunks of at most ``_CHUNK_MAX`` characters.

        Strategy:

        1. Split on double newlines (paragraph boundaries).
        2. If any paragraph is still longer than ``_CHUNK_MAX``, split it
           further on sentence boundaries (``'. '``).
        3. Any remaining oversized fragment is kept as-is (single sentence
           longer than the limit).

        Empty chunks are dropped.
        """
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

        chunks: list[str] = []
        for para in paragraphs:
            if len(para) <= _CHUNK_MAX:
                chunks.append(para)
            else:
                # Split further on sentence boundaries.
                sentences = para.split(". ")
                current = ""
                for i, sent in enumerate(sentences):
                    # Re-add the ". " that was consumed by split (except last).
                    piece = sent if i == len(sentences) - 1 else sent + ". "
                    if current and len(current) + len(piece) > _CHUNK_MAX:
                        chunks.append(current.rstrip())
                        current = piece
                    else:
                        current += piece
                if current.strip():
                    chunks.append(current.strip())

        return chunks if chunks else [text[:_CHUNK_MAX]]

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_entry(self, source_type: str, source_id: int, text: str) -> None:
        """Chunk *text*, embed each chunk, and persist to the embeddings table.

        Parameters
        ----------
        source_type:
            E.g. ``'journal'`` or ``'wiki'``.
        source_id:
            Primary key of the source row.
        text:
            Raw text content to index.
        """
        chunks = self._chunk_text(text)
        vectors = self.embed(chunks)  # (C, 384)
        ts = time.time()

        cur = self._conn.cursor()
        for chunk_idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            blob = vec.astype(np.float32).tobytes()
            preview = chunk[:200]
            cur.execute(
                """
                INSERT INTO embeddings
                    (source_type, source_id, chunk_index, text_preview, vector, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_type, source_id, chunk_idx, preview, blob, ts),
            )
        self._conn.commit()
        self._dirty = True
        logger.debug(
            "index_entry: %s/%s → %d chunk(s) stored", source_type, source_id, len(chunks)
        )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _reload_cache(self) -> None:
        """Rebuild the in-memory vector matrix from the embeddings table.

        Called automatically by :meth:`search` when :attr:`_dirty` is True.
        Sets ``_dirty = False`` on completion.
        """
        logger.debug("Reloading embeddings cache …")
        t0 = time.monotonic()

        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, source_type, source_id, chunk_index, text_preview, vector
            FROM embeddings
            ORDER BY id
            """
        )
        rows = cur.fetchall()

        if not rows:
            self._vectors = np.empty((0, _VECTOR_DIM), dtype=np.float32)
            self._ids = []
            self._dirty = False
            logger.debug("Cache empty (no embeddings stored).")
            return

        matrix_rows: list[np.ndarray] = []
        metadata: list[dict[str, Any]] = []

        for row in rows:
            blob = row["vector"]
            vec = np.frombuffer(blob, dtype=np.float32).copy()
            if vec.shape[0] != _VECTOR_DIM:
                logger.warning(
                    "Skipping malformed vector (row id=%s, dim=%d)", row["id"], vec.shape[0]
                )
                continue
            matrix_rows.append(vec)
            metadata.append(
                {
                    "row_id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "chunk_index": row["chunk_index"],
                    "text_preview": row["text_preview"],
                }
            )

        self._vectors = np.stack(matrix_rows, axis=0).astype(np.float32)  # (N, 384)
        self._ids = metadata
        self._dirty = False

        elapsed = time.monotonic() - t0
        logger.info(
            "Cache reloaded: %d vectors in %.3f s", len(self._ids), elapsed
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        source_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the *top_k* most semantically similar chunks to *query*.

        Parameters
        ----------
        query:
            Natural-language query string.
        top_k:
            Maximum number of results to return.
        source_types:
            If provided, only return results whose ``source_type`` is in this
            list.  Filtering is done after similarity scoring so the full
            cache matrix is always used.

        Returns
        -------
        list[dict]
            Each dict has keys: ``source_type``, ``source_id``,
            ``chunk_index``, ``text_preview``, ``score``.
        """
        if self._dirty:
            self._reload_cache()

        if self._vectors is None or self._vectors.shape[0] == 0:
            return []

        # Embed the query — result shape is (1, 384).
        query_vec = self.embed([query])[0]  # (384,)

        # Dot product of L2-normalised vectors == cosine similarity.
        scores: np.ndarray = self._vectors @ query_vec  # (N,)

        # Build (score, metadata) pairs.
        indexed_scores = list(enumerate(scores.tolist()))

        # Filter by source_type if requested.
        if source_types:
            source_set = set(source_types)
            indexed_scores = [
                (i, s) for i, s in indexed_scores
                if self._ids[i]["source_type"] in source_set  # type: ignore[index]
            ]

        # Sort descending by score.
        indexed_scores.sort(key=lambda t: t[1], reverse=True)
        top = indexed_scores[:top_k]

        results: list[dict[str, Any]] = []
        for idx, score in top:
            meta = self._ids[idx]  # type: ignore[index]
            results.append(
                {
                    "source_type": meta["source_type"],
                    "source_id": meta["source_id"],
                    "chunk_index": meta["chunk_index"],
                    "text_preview": meta["text_preview"],
                    "score": round(float(score), 6),
                }
            )

        return results

    # ------------------------------------------------------------------
    # Reindex
    # ------------------------------------------------------------------

    def reindex_all(self) -> int:
        """Wipe all embeddings and re-index every journal entry and wiki page.

        Returns
        -------
        int
            Total number of chunks indexed.
        """
        logger.info("reindex_all: deleting existing embeddings …")
        self._conn.execute("DELETE FROM embeddings")
        self._conn.commit()
        self._dirty = True

        total_chunks = 0

        # --- Journal entries ---
        cur = self._conn.cursor()
        cur.execute("SELECT id, title, body FROM journal")
        journal_rows = cur.fetchall()
        logger.info("reindex_all: indexing %d journal entries …", len(journal_rows))
        for i, row in enumerate(journal_rows):
            combined = f"{row['title']}\n\n{row['body']}" if row["title"] else row["body"]
            before = _count_chunks(combined)
            self.index_entry("journal", row["id"], combined)
            total_chunks += before
            if (i + 1) % 50 == 0:
                logger.info("  journal: %d / %d done", i + 1, len(journal_rows))

        # --- Wiki pages ---
        cur.execute("SELECT id, title, body FROM wiki_pages")
        wiki_rows = cur.fetchall()
        logger.info("reindex_all: indexing %d wiki pages …", len(wiki_rows))
        for i, row in enumerate(wiki_rows):
            combined = f"{row['title']}\n\n{row['body']}"
            before = _count_chunks(combined)
            self.index_entry("wiki", row["id"], combined)
            total_chunks += before
            if (i + 1) % 50 == 0:
                logger.info("  wiki: %d / %d done", i + 1, len(wiki_rows))

        logger.info(
            "reindex_all: complete — %d source docs, ~%d chunks",
            len(journal_rows) + len(wiki_rows),
            total_chunks,
        )
        return total_chunks

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return a summary of the index state.

        Returns
        -------
        dict
            Keys: ``indexed`` (int), ``model`` (str), ``cache_loaded`` (bool).
        """
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM embeddings")
        count = cur.fetchone()["n"]
        return {
            "indexed": count,
            "model": _MODEL_NAME,
            "cache_loaded": not self._dirty,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_chunks(text: str) -> int:
    """Return the number of chunks that *text* would be split into."""
    return len(EmbeddingsIndex._chunk_text(text))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _make_handler(index: EmbeddingsIndex) -> type[BaseHTTPRequestHandler]:
    """Return a ``BaseHTTPRequestHandler`` subclass bound to *index*."""

    class _Handler(BaseHTTPRequestHandler):
        """Minimal JSON-over-HTTP handler for the embeddings service."""

        # Suppress the default per-request log lines; use our own logger.
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
            logger.debug("HTTP %s", fmt % args)

        # --------------------------------------------------------------
        # Routing
        # --------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._handle_health()
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            path_map = {
                "/search": self._handle_search,
                "/index": self._handle_index,
                "/reindex": self._handle_reindex,
            }
            handler_fn = path_map.get(self.path)
            if handler_fn is None:
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                body = self._read_json_body()
                handler_fn(body)
            except (ValueError, KeyError) as exc:
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:  # pragma: no cover
                logger.exception("Unhandled error in %s", self.path)
                self._send_json({"error": str(exc)}, status=500)

        # --------------------------------------------------------------
        # Endpoint implementations
        # --------------------------------------------------------------

        def _handle_health(self) -> None:
            s = index.stats()
            self._send_json(
                {
                    "status": "ok",
                    "model": s["model"],
                    "indexed": s["indexed"],
                }
            )

        def _handle_search(self, body: dict[str, Any]) -> None:
            query: str = body.get("query", "")
            if not query:
                raise ValueError("'query' is required")
            top_k: int = int(body.get("top_k", 5))
            source_types: list[str] | None = body.get("source_types") or None
            results = index.search(query, top_k=top_k, source_types=source_types)
            self._send_json({"results": results})

        def _handle_index(self, body: dict[str, Any]) -> None:
            source_type: str = body["source_type"]
            source_id: int = int(body["source_id"])
            text: str = body["text"]
            if not text.strip():
                raise ValueError("'text' must not be empty")
            index.index_entry(source_type, source_id, text)
            self._send_json({"ok": True})

        def _handle_reindex(self, _body: dict[str, Any]) -> None:
            n = index.reindex_all()
            self._send_json({"ok": True, "indexed": n})

        # --------------------------------------------------------------
        # Helpers
        # --------------------------------------------------------------

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, data: Any, status: int = 200) -> None:
            payload = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


# ---------------------------------------------------------------------------
# Server entry-points
# ---------------------------------------------------------------------------


def run_server(port: int = 7437, db_path: str | None = None) -> None:
    """Start the embeddings HTTP server and block forever.

    Parameters
    ----------
    port:
        TCP port to listen on.  Defaults to ``7437``.
    db_path:
        Path to the knowledge SQLite database.  ``None`` uses the default.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    idx = EmbeddingsIndex(db_path=db_path)
    handler_cls = _make_handler(idx)

    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    print(
        f"OpenKeel embeddings server listening on http://0.0.0.0:{port}  "
        f"(model: {_MODEL_NAME})"
    )
    logger.info("Embeddings server started on port %d", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down embeddings server.")
    finally:
        server.server_close()


def main() -> None:
    """Parse command-line arguments and start the server."""
    parser = argparse.ArgumentParser(
        description="OpenKeel embeddings server — semantic search via HTTP",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7437,
        help="TCP port to listen on (default: 7437)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        metavar="PATH",
        help="Path to knowledge.db (default: ~/.openkeel/knowledge.db)",
    )
    args = parser.parse_args()
    run_server(port=args.port, db_path=args.db_path)


if __name__ == "__main__":
    main()
