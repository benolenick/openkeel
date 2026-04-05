"""Predictive Pre-Cache — anticipate which files Claude will read next.

Watches Claude's access patterns and pre-warms the summary cache for
files it's likely to need:
  1. Import graph: if Claude read A, pre-cache A's imports
  2. Directory locality: if Claude read dir/foo.py, pre-cache dir/bar.py
  3. Edit graph: if Claude edited A, pre-cache files that import A
  4. Recency: pre-cache recently modified files

Pre-warming is async and non-blocking. Cache hits from predictions
are logged as "predictive_hit" in the ledger.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from openkeel.token_saver import ledger
from openkeel.token_saver.engines import codebase_index

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")

# Track what's been predicted to avoid duplicate work
_predicted: set[str] = set()


def predict_next_reads(
    just_read: str,
    project_root: str = "",
    session_reads: list[str] | None = None,
) -> list[str]:
    """Predict which files Claude will likely read next.

    Returns a list of file paths to pre-warm, ordered by likelihood.
    """
    if not project_root:
        project_root = os.getcwd()

    predictions: list[tuple[float, str]] = []
    root = Path(project_root).resolve()
    just_read_path = Path(just_read).resolve()

    # Strategy 1: Import graph — files imported by the just-read file
    imports = _get_file_imports(str(just_read_path), project_root)
    for imp_path in imports:
        if imp_path not in _predicted:
            predictions.append((0.8, imp_path))

    # Strategy 2: Directory locality — sibling files
    parent = just_read_path.parent
    if parent.is_dir():
        for sibling in parent.iterdir():
            if sibling.is_file() and sibling.suffix in codebase_index.INDEXABLE:
                sib_str = str(sibling)
                if sib_str != str(just_read_path) and sib_str not in _predicted:
                    predictions.append((0.5, sib_str))

    # Strategy 3: Reverse imports — files that import the just-read file
    reverse = _get_reverse_imports(str(just_read_path), project_root)
    for rev_path in reverse:
        if rev_path not in _predicted:
            predictions.append((0.6, rev_path))

    # Strategy 4: Recently modified files not yet read
    if session_reads is not None:
        read_set = set(session_reads)
        try:
            recent = _get_recently_modified(project_root, limit=5)
            for rpath in recent:
                if rpath not in read_set and rpath not in _predicted:
                    predictions.append((0.3, rpath))
        except Exception:
            pass

    # Sort by likelihood, deduplicate
    predictions.sort(key=lambda x: -x[0])
    seen = set()
    unique = []
    for score, path in predictions:
        if path not in seen and os.path.isfile(path):
            seen.add(path)
            unique.append(path)
            if len(unique) >= 5:
                break

    return unique


def pre_warm(files: list[str]) -> dict[str, Any]:
    """Send files to the daemon for pre-warming. Non-blocking best-effort."""
    if not files:
        return {"warmed": 0}

    for f in files:
        _predicted.add(f)

    try:
        payload = json.dumps({"files": files}).encode()
        req = urllib.request.Request(
            f"{DAEMON_URL}/cache/warm",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        warmed = result.get("warmed", 0)
        if warmed > 0:
            ledger.record(
                event_type="predictive_warm",
                tool_name="PredictiveCache",
                original_chars=0,
                saved_chars=0,
                notes=f"pre-warmed {warmed}/{len(files)} files",
            )
        return result
    except Exception:
        return {"warmed": 0, "error": "daemon unreachable"}


def on_cache_hit(file_path: str, was_predicted: bool = False) -> None:
    """Record when a predicted file gets a cache hit."""
    if was_predicted or file_path in _predicted:
        ledger.record(
            event_type="predictive_hit",
            tool_name="PredictiveCache",
            file_path=file_path,
            original_chars=0,
            saved_chars=0,
            notes=f"prediction hit: {os.path.basename(file_path)}",
        )


def _get_file_imports(file_path: str, project_root: str) -> list[str]:
    """Get resolved import paths for a file from the codebase index."""
    index = codebase_index.load_index(project_root)
    if not index:
        return []

    root = Path(project_root).resolve()
    try:
        rel = str(Path(file_path).resolve().relative_to(root))
    except ValueError:
        return []

    file_info = index.get("files", {}).get(rel, {})
    imports = file_info.get("imports", [])

    resolved = []
    for imp in imports:
        # Try to resolve import to a file path
        candidates = _resolve_import(imp, root, index)
        resolved.extend(candidates)

    return resolved[:10]


def _get_reverse_imports(file_path: str, project_root: str) -> list[str]:
    """Find files that import the given file."""
    index = codebase_index.load_index(project_root)
    if not index:
        return []

    root = Path(project_root).resolve()
    try:
        rel = str(Path(file_path).resolve().relative_to(root))
    except ValueError:
        return []

    # Extract module name from file path
    module_name = rel.replace("/", ".").replace("\\", ".")
    if module_name.endswith(".py"):
        module_name = module_name[:-3]

    # Also match the short name
    short_name = Path(rel).stem

    reverse = []
    for other_rel, other_info in index.get("files", {}).items():
        if other_rel == rel:
            continue
        for imp in other_info.get("imports", []):
            if module_name in imp or short_name in imp:
                reverse.append(str(root / other_rel))
                break

    return reverse[:5]


def _resolve_import(import_name: str, root: Path, index: dict) -> list[str]:
    """Try to resolve an import name to actual file paths."""
    # Convert dot notation to path
    parts = import_name.replace(".", "/")
    candidates = [
        f"{parts}.py",
        f"{parts}/__init__.py",
        f"{parts}.ts",
        f"{parts}.js",
        f"{parts}/index.ts",
        f"{parts}/index.js",
    ]

    resolved = []
    files = index.get("files", {})
    for candidate in candidates:
        if candidate in files:
            full_path = str(root / candidate)
            if os.path.isfile(full_path):
                resolved.append(full_path)

    return resolved


def _get_recently_modified(project_root: str, limit: int = 5) -> list[str]:
    """Get recently modified source files."""
    root = Path(project_root)
    files_with_mtime = []
    for ext in (".py", ".js", ".ts"):
        for path in root.rglob(f"*{ext}"):
            skip = any(part in codebase_index.SKIP_DIRS for part in path.parts)
            if skip:
                continue
            try:
                files_with_mtime.append((path.stat().st_mtime, str(path)))
            except OSError:
                continue
    files_with_mtime.sort(reverse=True)
    return [f for _, f in files_with_mtime[:limit]]


def reset() -> None:
    """Reset prediction state (call at session start)."""
    _predicted.clear()
