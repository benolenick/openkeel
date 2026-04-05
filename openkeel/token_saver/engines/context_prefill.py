"""Session Context Prefill — pre-builds compressed context at session start.

Instead of Claude exploring the codebase blind, inject a briefing:
  - Recent git changes (diff stat, recent commits)
  - Codebase map from the index
  - Files most likely to be relevant (recently modified)
  - Active task context from scribe state

This runs as part of SessionStart and outputs to stdout for Claude to see.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from openkeel.token_saver.engines import codebase_index
from openkeel.token_saver import ledger


def build_prefill(project_root: str = "") -> str:
    """Build the session prefill context. Returns formatted string."""
    if not project_root:
        project_root = os.getcwd()

    parts = []

    # 1. Build/update codebase index
    try:
        idx_result = codebase_index.build_index(project_root)
        project_map = codebase_index.format_index_summary(project_root)
        if project_map:
            # Truncate if too large
            if len(project_map) > 3000:
                project_map = project_map[:3000] + "\n  ... (truncated)"
            parts.append(f"[TOKEN SAVER] Project map ({idx_result['file_count']} files indexed):")
            parts.append(project_map)

            # Log the savings — this replaces Claude's exploration phase
            ledger.record(
                event_type="prefill_index",
                tool_name="SessionStart",
                original_chars=idx_result["file_count"] * 2000,  # est. avg file size
                saved_chars=idx_result["file_count"] * 2000 - len(project_map),
                notes=f"indexed {idx_result['file_count']} files, served {len(project_map)} char map",
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
            # Truncate large diffs
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

        # Files modified but not committed
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


def get_recently_modified_files(project_root: str, limit: int = 8) -> list[str]:
    """Get the most recently modified source files for pre-warming."""
    root = Path(project_root)
    files_with_mtime = []

    for ext in (".py", ".js", ".ts", ".go", ".rs"):
        for path in root.rglob(f"*{ext}"):
            skip = False
            for part in path.parts:
                if part in codebase_index.SKIP_DIRS:
                    skip = True
                    break
            if skip:
                continue
            try:
                files_with_mtime.append((path.stat().st_mtime, str(path)))
            except OSError:
                continue

    files_with_mtime.sort(reverse=True)
    return [f for _, f in files_with_mtime[:limit]]
