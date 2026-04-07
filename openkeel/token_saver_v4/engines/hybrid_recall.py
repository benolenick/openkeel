"""Hybrid Recall — manifold first, graph traversal on multi-hop queries.

Keeps Hyphae's manifold recall as the default. When a query looks multi-hop,
extracts entities from the top-N recall results, walks a sibling SQLite edge
store one or two hops, and returns a ranked subgraph of facts.

The edge store lives at ~/.openkeel/hyphae_edges_v4.sqlite and is built
incrementally: every time we recall, we co-reference the returned facts and
record co-occurrence edges between the entities they mention. Over time the
graph self-assembles from the manifold. No Hyphae schema changes.

Multi-hop detection is regex-first (cheap), LLM-classifier fallback (only if
the regex is unsure and a local LLM is reachable). Single-hop queries skip
the whole graph path and return pure manifold results.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EDGE_DB = Path(os.path.expanduser("~/.openkeel/hyphae_edges_v4.sqlite"))
HYPHAE_URL = os.environ.get("HYPHAE_URL", "http://127.0.0.1:8100")

# Multi-hop cues: "depends on the X that", "which Y uses the Z", etc.
_MULTIHOP_PATTERNS = (
    re.compile(r"\bwhich\s+\w+\s+.*\bthat\b", re.I),
    re.compile(r"\bdepends?\s+on\s+.*\bthat\b", re.I),
    re.compile(r"\bconnected\s+to\b.*\bwhich\b", re.I),
    re.compile(r"\buses?\s+the\s+\w+\s+.*\bthat\b", re.I),
    re.compile(r"\bvia\b.*\bvia\b", re.I),
    re.compile(r"\brelated\s+to\b.*\band\b.*\balso\b", re.I),
)

# Simple entity extractor: CamelCase, snake_case identifiers, IPs, ports,
# quoted strings, and known-proper-noun patterns. Deliberately crude — this
# runs on every recall and must be fast.
_ENTITY_RE = re.compile(
    r"""
    \b[A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]+)+\b     # CamelCase
    | \b[a-z][a-z0-9_]{3,}\b                         # snake_case
    | \b\d{1,3}(?:\.\d{1,3}){3}\b                    # IP
    | \bport\s*\d+\b                                  # "port 8100"
    | "[^"]{3,40}"                                    # short quoted
    """,
    re.VERBOSE,
)

_STOP_ENTITIES = {
    "the", "this", "that", "with", "from", "into", "over", "under", "when",
    "what", "which", "where", "about", "above", "after", "again", "have",
    "been", "were", "will", "would", "should", "could", "must", "might",
    "note", "todo", "fixme", "example", "true", "false", "null", "none",
}


@dataclass
class HybridResult:
    mode: str                 # "manifold" | "graph_expanded"
    manifold_hits: list[dict]
    graph_hits: list[dict]
    entities: list[str]
    query_classification: str


def _init_db() -> sqlite3.Connection:
    EDGE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(EDGE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edges (
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            last_seen REAL NOT NULL,
            PRIMARY KEY (src, dst)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_facts (
            entity TEXT NOT NULL,
            fact TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 1.0,
            last_seen REAL NOT NULL,
            PRIMARY KEY (entity, fact)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ef_entity ON entity_facts(entity)")
    conn.commit()
    return conn


def extract_entities(text: str) -> list[str]:
    ents: list[str] = []
    seen: set[str] = set()
    for match in _ENTITY_RE.finditer(text):
        e = match.group(0).strip('"').lower()
        if len(e) < 3 or e in _STOP_ENTITIES:
            continue
        if e not in seen:
            seen.add(e)
            ents.append(e)
    return ents[:20]  # cap


def classify_query(query: str) -> str:
    """Return 'single' or 'multi'. Regex-first, no LLM call."""
    for pat in _MULTIHOP_PATTERNS:
        if pat.search(query):
            return "multi"
    # Heuristic: two or more "which/that/where" words = likely multi-hop
    hop_words = sum(1 for w in ("which", "that", "where", "whose") if w in query.lower())
    if hop_words >= 2:
        return "multi"
    return "single"


def _ingest(conn: sqlite3.Connection, facts: list[dict]) -> None:
    """Record co-occurrence edges and entity→fact links from a recall batch."""
    now = time.time()
    for f in facts:
        text = f.get("text", "")
        if not text:
            continue
        ents = extract_entities(text)
        for e in ents:
            conn.execute(
                "INSERT INTO entity_facts(entity, fact, score, last_seen) "
                "VALUES (?, ?, 1.0, ?) "
                "ON CONFLICT(entity, fact) DO UPDATE SET "
                "  score = score + 0.5, last_seen = excluded.last_seen",
                (e, text[:2000], now),
            )
        # co-occurrence edges (undirected, stored both ways)
        for i, a in enumerate(ents):
            for b in ents[i + 1:]:
                for src, dst in ((a, b), (b, a)):
                    conn.execute(
                        "INSERT INTO edges(src, dst, weight, last_seen) "
                        "VALUES (?, ?, 1.0, ?) "
                        "ON CONFLICT(src, dst) DO UPDATE SET "
                        "  weight = weight + 0.3, last_seen = excluded.last_seen",
                        (src, dst, now),
                    )
    conn.commit()


def _expand(conn: sqlite3.Connection, seed_entities: list[str], hops: int = 2) -> list[dict]:
    """Walk the edge graph from seed entities and return ranked facts."""
    frontier = set(seed_entities)
    visited = set(seed_entities)
    scores: dict[str, float] = {}

    for hop in range(hops):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for ent in frontier:
            for (dst, w) in conn.execute(
                "SELECT dst, weight FROM edges WHERE src = ? ORDER BY weight DESC LIMIT 8",
                (ent,),
            ):
                if dst not in visited:
                    next_frontier.add(dst)
                    visited.add(dst)
                    scores[dst] = scores.get(dst, 0.0) + w / (hop + 1)
        frontier = next_frontier

    # Pull facts for all visited entities, ranked by entity score
    hits: dict[str, dict] = {}
    for ent in visited:
        ent_score = scores.get(ent, 1.0)
        for (fact, fscore) in conn.execute(
            "SELECT fact, score FROM entity_facts WHERE entity = ? ORDER BY score DESC LIMIT 3",
            (ent,),
        ):
            total = ent_score * fscore
            if fact not in hits or hits[fact]["score"] < total:
                hits[fact] = {"text": fact, "score": total, "via": ent}
    ranked = sorted(hits.values(), key=lambda h: -h["score"])
    return ranked[:15]


def _hyphae_recall(query: str, top_k: int = 10) -> list[dict]:
    body = json.dumps({"query": query, "top_k": top_k}).encode()
    req = urllib.request.Request(
        f"{HYPHAE_URL}/recall",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("results", [])
    except Exception:
        return []


def recall(query: str, top_k: int = 10) -> HybridResult:
    """Public entry. Always runs manifold recall, optionally expands via graph."""
    conn = _init_db()
    try:
        manifold = _hyphae_recall(query, top_k=top_k)
        _ingest(conn, manifold)

        classification = classify_query(query)
        if classification == "single" or not manifold:
            return HybridResult(
                mode="manifold",
                manifold_hits=manifold,
                graph_hits=[],
                entities=[],
                query_classification=classification,
            )

        # Multi-hop: seed from entities in top 5 manifold hits
        seed: list[str] = []
        for h in manifold[:5]:
            seed.extend(extract_entities(h.get("text", "")))
        seed = list(dict.fromkeys(seed))[:10]

        graph = _expand(conn, seed, hops=2)
        return HybridResult(
            mode="graph_expanded",
            manifold_hits=manifold,
            graph_hits=graph,
            entities=seed,
            query_classification=classification,
        )
    finally:
        conn.close()


def merge_for_injection(result: HybridResult, budget_chars: int = 1800) -> str:
    """Format a hybrid result as a compact injection block."""
    lines: list[str] = [f"[LLMOS memory · mode={result.mode}]"]
    used = len(lines[0])
    seen: set[str] = set()

    def _add(text: str) -> bool:
        nonlocal used
        t = text.strip()
        if not t or t in seen:
            return False
        line = f"- {t}"
        if used + len(line) + 1 > budget_chars:
            return False
        lines.append(line)
        used += len(line) + 1
        seen.add(t)
        return True

    for h in result.manifold_hits:
        if not _add(h.get("text", "")):
            break
    for h in result.graph_hits:
        if not _add(h.get("text", "")):
            break

    return "\n".join(lines)
