"""
Token Saver v5 — JSON / structured-output detection.

v3's bash compressor runs on any stdout over a threshold, including valid
JSON, HTML, and XML. It drops commas/braces and fabricates entries
(directly observed in session 2026-04-07 — a `curl | jq` output came back
with missing braces and ghost duplicate task entries).

This module provides a cheap, reliable `looks_structured()` check that
the bash handler should call BEFORE invoking the LLM summarizer. If the
output looks like JSON/HTML/XML/CSV/TSV, pass it through unmodified.

We deliberately don't try to compress structured output here. The right
way to shrink a large JSON blob is: let the user pipe it through `jq`,
write it to a file and Read it (which engages semantic_skeleton), or
cache it. Compressing it with a text LLM is never the right answer.
"""

from __future__ import annotations

import json
from typing import Literal

StructuredKind = Literal["json", "ndjson", "html", "xml", "csv", "tsv", "none"]


def looks_structured(text: str) -> StructuredKind:
    """
    Cheap heuristic + parse check. Returns the detected kind, or "none".
    Designed to be fast enough to run on every bash output.
    """
    if not text:
        return "none"
    stripped = text.lstrip()
    if not stripped:
        return "none"

    first = stripped[0]

    # JSON object or array — try a real parse (cheap, <100us on typical outputs)
    if first in "{[":
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            # May be truncated or have trailing text; still risky to compress.
            # Treat "probably JSON" as structured too.
            if _looks_probably_json(stripped):
                return "json"

    # NDJSON / JSON-lines — most lines parse as JSON individually
    if first in "{[":
        lines = [l for l in stripped.splitlines() if l.strip()]
        if len(lines) >= 2:
            parsed = 0
            for line in lines[:10]:
                try:
                    json.loads(line)
                    parsed += 1
                except Exception:
                    break
            if parsed == min(len(lines), 10) and parsed >= 2:
                return "ndjson"

    # HTML / XML
    if first == "<":
        low = stripped[:200].lower()
        if low.startswith("<!doctype") or "<html" in low or "<?xml" in low:
            return "html" if "html" in low else "xml"
        # Generic XML-ish tag soup
        if ">" in stripped[:1000] and stripped.count("<") >= 3:
            return "xml"

    # CSV / TSV — check for consistent delimiter in first few lines
    lines = stripped.splitlines()[:8]
    if len(lines) >= 3:
        for delim, kind in ((",", "csv"), ("\t", "tsv")):
            counts = [line.count(delim) for line in lines]
            if counts[0] >= 2 and all(c == counts[0] for c in counts):
                return kind  # type: ignore[return-value]

    return "none"


def _looks_probably_json(text: str) -> bool:
    """
    Secondary heuristic for JSON that didn't fully parse (e.g. truncated
    curl output). Looks for the structural markers without fully validating.
    """
    # High density of ", :, {, [ suggests JSON structure
    sample = text[:500]
    if len(sample) < 20:
        return False
    markers = sample.count('"') + sample.count(":") + sample.count("{") + sample.count("[")
    return markers >= len(sample) / 20  # roughly one marker per 20 chars


def should_bypass_compression(text: str) -> bool:
    """Convenience predicate for the bash handler."""
    return looks_structured(text) != "none"
