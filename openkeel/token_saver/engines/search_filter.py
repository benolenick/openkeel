"""Search Filter — ranks and filters grep/glob results before Claude sees them.

When Claude runs a grep that returns 80 matches, most are noise.
This engine:
  1. Scores results by relevance (file importance, match context)
  2. Deduplicates near-identical matches
  3. Returns top N results with a "X more omitted" footer

All filtering is logged to the ledger.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any

from openkeel.token_saver import ledger
from openkeel.token_saver.engines import codebase_index

# Files that are usually more important
_IMPORTANT_PATTERNS = [
    re.compile(r"(^|/)__init__\.py$"),
    re.compile(r"(^|/)main\.py$"),
    re.compile(r"(^|/)app\.py$"),
    re.compile(r"(^|/)cli\.py$"),
    re.compile(r"(^|/)index\.(ts|js)$"),
    re.compile(r"(^|/)server\.(py|ts|js)$"),
    re.compile(r"(^|/)config\.(py|ts|js|yaml|yml)$"),
]

# Files that are usually less important
_NOISE_PATTERNS = [
    re.compile(r"(^|/)(test_|_test\.|\.test\.|spec\.)"),
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.(min|bundle)\.(js|css)$"),
    re.compile(r"(^|/)node_modules/"),
    re.compile(r"(^|/)\."),
    re.compile(r"(^|/)(dist|build)/"),
    re.compile(r"\.lock$"),
    re.compile(r"\.bak\d?$"),
]


def filter_grep_results(
    output: str,
    pattern: str = "",
    max_results: int = 25,
    project_root: str = "",
) -> tuple[str, dict[str, Any]]:
    """Filter and rank grep results.

    Returns (filtered_output, metadata).
    """
    original_chars = len(output)
    lines = output.strip().split("\n")
    original_count = len(lines)

    if original_count <= max_results:
        return output, {
            "original_count": original_count,
            "filtered_count": original_count,
            "saved_chars": 0,
            "method": "skip_small",
        }

    # Score each result
    scored: list[tuple[float, str]] = []
    for line in lines:
        score = _score_result(line, pattern)
        scored.append((score, line))

    # Sort by score descending
    scored.sort(key=lambda x: -x[0])

    # Deduplicate similar lines (same file, similar content)
    seen_files: dict[str, int] = defaultdict(int)
    kept = []
    for score, line in scored:
        # Extract file path from grep output
        file_part = line.split(":")[0] if ":" in line else line
        seen_files[file_part] += 1

        # Don't show more than 3 results from the same file
        if seen_files[file_part] > 3:
            continue

        kept.append(line)
        if len(kept) >= max_results:
            break

    omitted = original_count - len(kept)
    filtered = "\n".join(kept)
    if omitted > 0:
        filtered += f"\n\n... ({omitted} more results omitted, showing top {len(kept)} by relevance)"

    saved = max(0, original_chars - len(filtered))

    if saved > 200:
        ledger.record(
            event_type="search_filter",
            tool_name="Grep",
            original_chars=original_chars,
            saved_chars=saved,
            notes=f"filtered {original_count} → {len(kept)} results for '{pattern[:40]}'",
        )

    return filtered, {
        "original_count": original_count,
        "filtered_count": len(kept),
        "saved_chars": saved,
        "method": "relevance_ranked",
    }


def filter_glob_results(
    output: str,
    pattern: str = "",
    max_results: int = 40,
) -> tuple[str, dict[str, Any]]:
    """Filter glob results — remove noise files, rank by importance."""
    original_chars = len(output)
    lines = output.strip().split("\n")
    original_count = len(lines)

    if original_count <= max_results:
        return output, {
            "original_count": original_count,
            "filtered_count": original_count,
            "saved_chars": 0,
            "method": "skip_small",
        }

    # Remove noise files
    filtered_lines = []
    for line in lines:
        is_noise = any(p.search(line) for p in _NOISE_PATTERNS)
        if not is_noise:
            filtered_lines.append(line)

    # Score and sort remaining
    scored = [(1.0 + sum(1 for p in _IMPORTANT_PATTERNS if p.search(l)), l) for l in filtered_lines]
    scored.sort(key=lambda x: -x[0])

    kept = [line for _, line in scored[:max_results]]
    omitted = original_count - len(kept)

    filtered = "\n".join(kept)
    if omitted > 0:
        filtered += f"\n\n... ({omitted} more files omitted, showing {len(kept)} most relevant)"

    saved = max(0, original_chars - len(filtered))

    if saved > 200:
        ledger.record(
            event_type="search_filter",
            tool_name="Glob",
            original_chars=original_chars,
            saved_chars=saved,
            notes=f"filtered {original_count} → {len(kept)} files for '{pattern[:40]}'",
        )

    return filtered, {
        "original_count": original_count,
        "filtered_count": len(kept),
        "saved_chars": saved,
        "method": "noise_filtered",
    }


def _score_result(line: str, pattern: str) -> float:
    """Score a grep result by relevance."""
    score = 1.0

    # Important file patterns
    if any(p.search(line) for p in _IMPORTANT_PATTERNS):
        score += 3.0

    # Noise file patterns
    if any(p.search(line) for p in _NOISE_PATTERNS):
        score -= 5.0

    # Exact match scores higher than partial
    if pattern and pattern.lower() in line.lower():
        score += 2.0

    # Definition lines score higher (def, class, function, export)
    if re.search(r"(def |class |function |export |const |let |var )", line):
        score += 2.0

    # Comment lines score lower
    if re.search(r"^\s*(#|//|/\*|\*)", line.split(":", 2)[-1] if ":" in line else line):
        score -= 1.0

    return score
