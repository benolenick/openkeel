"""Memory compactor — age-based tiered compression for facts.

Reads facts from LocalMemory, groups by similarity, and produces a
compact digest that fits within a token budget. Older memories get
progressively more compressed so context never blows up but meaning
is never lost.

Tiers:
  Hot   (< hot_hours old):  full text, no changes
  Warm  (hot..warm hours):  deduplicated, similar facts merged
  Cold  (> warm_hours old): clusters summarized into single lines

No heavy dependencies — uses difflib for similarity.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


@dataclass
class CompactorConfig:
    """Configuration for memory compaction."""
    budget_chars: int = 800          # max chars in the injected digest
    hot_hours: float = 1.0           # full detail threshold
    warm_hours: float = 24.0         # merge threshold (beyond = cold)
    similarity_threshold: float = 0.55  # merge facts above this ratio
    max_cluster_size: int = 8        # max facts merged into one line
    project: str = ""                # filter to this project (empty = all)


def _similarity(a: str, b: str) -> float:
    """Fast similarity ratio between two strings."""
    # Short-circuit for very different lengths
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.6:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _cluster_facts(
    facts: list[dict[str, Any]],
    threshold: float,
    max_size: int,
) -> list[list[dict[str, Any]]]:
    """Group similar facts into clusters using greedy single-linkage."""
    if not facts:
        return []

    clusters: list[list[dict[str, Any]]] = []
    used = set()

    for i, fact_i in enumerate(facts):
        if i in used:
            continue
        cluster = [fact_i]
        used.add(i)

        for j, fact_j in enumerate(facts):
            if j in used or len(cluster) >= max_size:
                continue
            # Check similarity against cluster centroid (first item)
            if _similarity(fact_i["text"], fact_j["text"]) >= threshold:
                cluster.append(fact_j)
                used.add(j)

        clusters.append(cluster)

    return clusters


def _merge_cluster(cluster: list[dict[str, Any]]) -> str:
    """Merge a cluster of similar facts into one line.

    Keeps the most recent fact's text and appends a count if merged.
    """
    if len(cluster) == 1:
        return cluster[0]["text"]

    # Sort by created_at descending, keep newest
    sorted_c = sorted(cluster, key=lambda f: f.get("created_at", 0), reverse=True)
    newest = sorted_c[0]["text"]

    # If all texts are very similar, just use the newest
    if all(_similarity(newest, f["text"]) > 0.75 for f in sorted_c[1:]):
        return newest

    # Otherwise, take newest + unique fragments from others
    parts = [newest]
    for f in sorted_c[1:3]:  # max 2 extra fragments
        text = f["text"]
        # Only add if it brings new info
        if _similarity(newest, text) < 0.7:
            # Extract the shorter of the two as a note
            short = text if len(text) < len(newest) else text[:80]
            parts.append(short.rstrip(".") + ".")
    return " | ".join(parts)


def _summarize_cluster(cluster: list[dict[str, Any]]) -> str:
    """Heavily compress a cold cluster into a single short line."""
    if len(cluster) == 1:
        text = cluster[0]["text"]
        # Truncate to ~100 chars
        if len(text) > 100:
            return text[:97] + "..."
        return text

    # Pick the most informative fact (longest, or newest)
    sorted_c = sorted(cluster, key=lambda f: f.get("created_at", 0), reverse=True)
    best = sorted_c[0]["text"]
    if len(best) > 80:
        best = best[:77] + "..."
    return f"{best} (+{len(cluster)-1} related)"


def compact(
    config: CompactorConfig | None = None,
) -> str:
    """Run compaction and return a digest string within budget.

    Reads from LocalMemory, applies tiered compression, returns
    a formatted string ready for injection.
    """
    from openkeel.integrations.local_memory import LocalMemory

    config = config or CompactorConfig()
    mem = LocalMemory()

    # Get all facts, sorted by age (newest first)
    conn = mem._get_conn()
    if config.project:
        rows = conn.execute(
            "SELECT id, text, project, tag, source, created_at "
            "FROM facts WHERE project = ? ORDER BY created_at DESC",
            (config.project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, project, tag, source, created_at "
            "FROM facts ORDER BY created_at DESC",
        ).fetchall()

    if not rows:
        mem.close()
        return ""

    facts = [dict(r) for r in rows]
    now = time.time()

    # Split into tiers
    hot_cutoff = now - (config.hot_hours * 3600)
    warm_cutoff = now - (config.warm_hours * 3600)

    hot = [f for f in facts if f["created_at"] >= hot_cutoff]
    warm = [f for f in facts if warm_cutoff <= f["created_at"] < hot_cutoff]
    cold = [f for f in facts if f["created_at"] < warm_cutoff]

    # Build digest parts
    parts: list[str] = []

    # Hot tier: full text
    for f in hot:
        parts.append(f["text"])

    # Warm tier: cluster and merge
    if warm:
        clusters = _cluster_facts(warm, config.similarity_threshold, config.max_cluster_size)
        for cluster in clusters:
            parts.append(_merge_cluster(cluster))

    # Cold tier: cluster and summarize
    if cold:
        clusters = _cluster_facts(cold, config.similarity_threshold, config.max_cluster_size)
        for cluster in clusters:
            parts.append(_summarize_cluster(cluster))

    mem.close()

    # Apply budget — prioritize hot > warm > cold
    digest_lines: list[str] = []
    used_chars = 0

    for part in parts:
        if used_chars + len(part) + 2 > config.budget_chars:
            # Try to fit a truncated version
            remaining = config.budget_chars - used_chars - 5
            if remaining > 30:
                digest_lines.append(part[:remaining] + "...")
            break
        digest_lines.append(part)
        used_chars += len(part) + 2  # +2 for newline separator

    if not digest_lines:
        return ""

    return "\n".join(digest_lines)


def compact_and_prune(
    config: CompactorConfig | None = None,
    prune_cold_duplicates: bool = True,
) -> dict[str, int]:
    """Run compaction AND prune duplicate/redundant cold facts from the DB.

    This actually reduces the database size by merging cold duplicates
    into single facts. Returns stats about what was pruned.

    Only touches cold-tier facts (older than warm_hours).
    """
    from openkeel.integrations.local_memory import LocalMemory

    config = config or CompactorConfig()
    mem = LocalMemory()
    conn = mem._get_conn()

    now = time.time()
    warm_cutoff = now - (config.warm_hours * 3600)

    # Get cold facts
    if config.project:
        rows = conn.execute(
            "SELECT id, text, project, tag, source, created_at "
            "FROM facts WHERE project = ? AND created_at < ? "
            "ORDER BY created_at DESC",
            (config.project, warm_cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, project, tag, source, created_at "
            "FROM facts WHERE created_at < ? ORDER BY created_at DESC",
            (warm_cutoff,),
        ).fetchall()

    if not rows:
        mem.close()
        return {"pruned": 0, "kept": 0, "clusters": 0}

    cold = [dict(r) for r in rows]
    clusters = _cluster_facts(cold, config.similarity_threshold, config.max_cluster_size)

    pruned = 0
    kept = 0

    for cluster in clusters:
        if len(cluster) <= 1:
            kept += 1
            continue

        # Keep the newest fact, merge text, delete the rest
        sorted_c = sorted(cluster, key=lambda f: f.get("created_at", 0), reverse=True)
        survivor = sorted_c[0]
        merged_text = _merge_cluster(cluster)

        # Update survivor with merged text
        conn.execute(
            "UPDATE facts SET text = ? WHERE id = ?",
            (merged_text, survivor["id"]),
        )

        # Delete the rest
        for f in sorted_c[1:]:
            conn.execute("DELETE FROM facts WHERE id = ?", (f["id"],))
            pruned += 1

        kept += 1

    conn.commit()
    mem.close()

    return {"pruned": pruned, "kept": kept, "clusters": len(clusters)}
