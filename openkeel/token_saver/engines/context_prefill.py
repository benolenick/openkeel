"""Session Context Prefill — relevance-ranked project map at session start.

Inspired by aider's repo map: instead of dumping the full project index,
rank files by relevance signals and serve a token-budgeted map that
focuses on what Claude is most likely to need.

Relevance signals (from aider's approach):
  1. Git recency — recently changed files rank higher
  2. Git status — uncommitted changes rank highest
  3. Edit history — files from previous sessions rank higher
  4. Directory proximity — files near recent edits rank higher
  5. Import centrality — files imported by many others rank higher

The map expands/contracts to fit a configurable token budget.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from openkeel.token_saver.engines import codebase_index
from openkeel.token_saver import ledger

# Token budget for the map (in chars, ~4 chars/token)
DEFAULT_MAP_BUDGET = 4000  # ~1000 tokens
MAX_MAP_BUDGET = 12000     # ~3000 tokens


def build_prefill(project_root: str = "", map_budget: int = DEFAULT_MAP_BUDGET) -> str:
    """Build the session prefill context with relevance-ranked map."""
    if not project_root:
        project_root = os.getcwd()

    parts = []

    # 1. Build/update codebase index
    try:
        idx_result = codebase_index.build_index(project_root)
        index = codebase_index.load_index(project_root)

        if index:
            # Build relevance-ranked map instead of full dump
            ranked_map = _build_ranked_map(index, project_root, budget=map_budget)
            if ranked_map:
                parts.append(f"[TOKEN SAVER] Project map ({idx_result['file_count']} files, ranked by relevance):")
                parts.append(ranked_map)

                ledger.record(
                    event_type="prefill_ranked_map",
                    tool_name="SessionStart",
                    original_chars=idx_result["file_count"] * 2000,
                    saved_chars=idx_result["file_count"] * 2000 - len(ranked_map),
                    notes=f"ranked map: {len(ranked_map)} chars from {idx_result['file_count']} files (budget: {map_budget})",
                )
    except Exception as e:
        parts.append(f"[TOKEN SAVER] Index build failed: {e}")

    # 2. Git context
    try:
        git_stat = subprocess.run(
            ["git", "diff", "--stat", "HEAD~5..HEAD"],
            capture_output=True, text=True, timeout=5, cwd=project_root,
        ).stdout.strip()
        if git_stat:
            lines = git_stat.split("\n")
            if len(lines) > 15:
                git_stat = "\n".join(lines[:12]) + f"\n  ... and {len(lines) - 12} more files"
            parts.append(f"\n[TOKEN SAVER] Recent changes (last 5 commits):\n{git_stat}")

        git_log = subprocess.run(
            ["git", "log", "--oneline", "-8"],
            capture_output=True, text=True, timeout=5, cwd=project_root,
        ).stdout.strip()
        if git_log:
            parts.append(f"\n[TOKEN SAVER] Recent commits:\n{git_log}")

        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=project_root,
        ).stdout.strip()
        if git_status:
            status_lines = git_status.split("\n")
            if len(status_lines) > 20:
                parts.append(f"\n[TOKEN SAVER] Uncommitted changes: {len(status_lines)} files (showing first 15)")
                git_status = "\n".join(status_lines[:15])
            else:
                parts.append(f"\n[TOKEN SAVER] Uncommitted changes:")
            parts.append(git_status)
    except Exception:
        pass

    # 3. Previous session savings
    try:
        summary = ledger.all_time_summary()
        if summary.get("events", 0) > 0:
            parts.append(
                f"\n[TOKEN SAVER] Lifetime savings: ~{summary['saved_tokens_est']:,} tokens "
                f"saved across {summary['sessions']} sessions ({summary['savings_pct']}%)"
            )
    except Exception:
        pass

    return "\n".join(parts)


def _build_ranked_map(index: dict, project_root: str, budget: int = DEFAULT_MAP_BUDGET) -> str:
    """Build a relevance-ranked project map within a token budget.

    Scores each file by multiple signals, then includes as many as fit
    within the budget, with more detail for higher-ranked files.
    """
    files = index.get("files", {})
    if not files:
        return ""

    root = Path(project_root).resolve()

    # Score every file
    scored_files: list[tuple[float, str, dict]] = []
    git_recent = _get_git_recent_files(project_root)
    git_dirty = _get_git_dirty_files(project_root)
    import_counts = _count_reverse_imports(files)
    scribe_files = _get_scribe_history()

    for rel, info in files.items():
        score = 0.0

        # Signal 1: Git dirty (uncommitted changes) — strongest signal
        if rel in git_dirty:
            score += 50.0

        # Signal 2: Git recency (recent commits)
        if rel in git_recent:
            rank = git_recent[rel]  # 0 = most recent
            score += max(0, 30.0 - rank * 3.0)

        # Signal 3: Previous session edit history
        full_path = str(root / rel)
        if full_path in scribe_files.get("edited", set()):
            score += 20.0
        elif full_path in scribe_files.get("read", set()):
            score += 5.0

        # Signal 4: Import centrality — files imported by many others
        reverse_count = import_counts.get(rel, 0)
        if reverse_count > 0:
            score += min(15.0, reverse_count * 3.0)

        # Signal 5: File type importance
        basename = Path(rel).name
        if basename in ("__init__.py", "main.py", "app.py", "cli.py", "index.ts", "server.py", "config.py"):
            score += 8.0
        if basename.startswith("test_") or basename.endswith("_test.py"):
            score -= 5.0
        if ".bak" in basename:
            score -= 20.0

        # Signal 6: File size (prefer medium-sized files — not too tiny, not huge)
        lines = info.get("lines", 0)
        if 50 < lines < 500:
            score += 3.0
        elif lines > 1000:
            score -= 2.0

        scored_files.append((score, rel, info))

    # Sort by score descending
    scored_files.sort(key=lambda x: -x[0])

    # Build the map within budget
    lines = []
    current_chars = 0
    current_dir = ""
    files_included = 0
    files_skipped = 0

    for score, rel, info in scored_files:
        dir_name = str(Path(rel).parent) if "/" in rel else "."

        # Format entry based on rank
        if score >= 20:
            # High relevance: full detail
            entry = _format_file_detail(rel, info, score)
        elif score >= 5:
            # Medium relevance: compact
            entry = _format_file_compact(rel, info)
        else:
            # Low relevance: just the filename
            entry = f"  {Path(rel).name} ({info.get('lines', 0)}L)"

        # Add directory header if changed
        dir_line = ""
        if dir_name != current_dir:
            dir_line = f"\n{dir_name}/"
            current_dir = dir_name

        entry_text = (dir_line + "\n" + entry) if dir_line else ("\n" + entry)
        entry_chars = len(entry_text)

        if current_chars + entry_chars > budget:
            files_skipped += 1
            continue

        lines.append(entry_text)
        current_chars += entry_chars
        files_included += 1

    if files_skipped > 0:
        lines.append(f"\n  ... ({files_skipped} low-relevance files omitted)")

    return "".join(lines)


def _format_file_detail(rel: str, info: dict, score: float) -> str:
    """Full detail for high-relevance files."""
    fname = Path(rel).name
    lines_count = info.get("lines", 0)
    classes = [c["name"] for c in info.get("classes", [])]
    funcs = [f["name"] for f in info.get("functions", [])[:6]]
    docstring = info.get("docstring", "")

    parts = [f"  * {fname} ({lines_count}L)"]
    if docstring:
        parts.append(f"    {docstring[:100]}")
    if classes:
        parts.append(f"    classes: {', '.join(classes[:4])}")
    if funcs:
        parts.append(f"    fn: {', '.join(funcs)}")
    return "\n".join(parts)


def _format_file_compact(rel: str, info: dict) -> str:
    """Compact format for medium-relevance files."""
    fname = Path(rel).name
    lines_count = info.get("lines", 0)
    classes = [c["name"] for c in info.get("classes", [])[:2]]
    funcs = [f["name"] for f in info.get("functions", [])[:3]]
    detail_parts = []
    if classes:
        detail_parts.append(f"classes: {', '.join(classes)}")
    if funcs:
        detail_parts.append(f"fn: {', '.join(funcs)}")
    detail = f" — {'; '.join(detail_parts)}" if detail_parts else ""
    return f"  {fname} ({lines_count}L){detail}"


# ---------------------------------------------------------------------------
# Relevance signal extractors
# ---------------------------------------------------------------------------

def _get_git_recent_files(project_root: str, limit: int = 30) -> dict[str, int]:
    """Get files from recent commits, ranked by recency. Returns {rel_path: rank}."""
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", "-20"],
            capture_output=True, text=True, timeout=5, cwd=project_root,
        )
        files: dict[str, int] = {}
        rank = 0
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line not in files:
                files[line] = rank
                rank += 1
            if len(files) >= limit:
                break
        return files
    except Exception:
        return {}


def _get_git_dirty_files(project_root: str) -> set[str]:
    """Get files with uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=project_root,
        )
        dirty = set()
        for line in result.stdout.strip().split("\n"):
            if len(line) > 3:
                # Git status format: XY filename
                dirty.add(line[3:].strip().split(" -> ")[-1])
        return dirty
    except Exception:
        return set()


def _count_reverse_imports(files: dict[str, dict]) -> dict[str, int]:
    """Count how many other files import each file (centrality)."""
    counts: dict[str, int] = {}
    # Build a module → file mapping
    module_to_file: dict[str, str] = {}
    for rel in files:
        module = rel.replace("/", ".").replace("\\", ".")
        if module.endswith(".py"):
            module = module[:-3]
        short = Path(rel).stem
        module_to_file[module] = rel
        module_to_file[short] = rel

    # Count imports
    for rel, info in files.items():
        for imp in info.get("imports", []):
            target = module_to_file.get(imp)
            if target and target != rel:
                counts[target] = counts.get(target, 0) + 1
    return counts


def _get_scribe_history() -> dict[str, set[str]]:
    """Get files from the scribe state (previous session's reads/edits)."""
    state_path = os.path.expanduser("~/.openkeel/scribe_state.json")
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
        return {
            "edited": set(state.get("files_edited", [])),
            "read": set(state.get("files_read", [])),
            "created": set(state.get("files_created", [])),
        }
    except Exception:
        return {"edited": set(), "read": set(), "created": set()}


def get_recently_modified_files(project_root: str, limit: int = 8) -> list[str]:
    """Get the most recently modified source files for pre-warming."""
    root = Path(project_root)
    files_with_mtime = []
    for ext in (".py", ".js", ".ts", ".go", ".rs"):
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
