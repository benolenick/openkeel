"""
Token Saver v5 — LocalEdit post-write verification.

v3's engines/local_edit.py reads a file, asks a small LLM for
{old_string, new_string}, does str.replace(), writes, and returns a
"X lines changed" summary. Directly observed in session 2026-04-07:

  - Edit of ~25 lines reported as "6 lines changed"
  - Edit of ~65 lines reported as "9 lines changed"
  - Edit of ~1 line correctly reported as "1 lines changed"

The "lines changed" count comes from the LLM's self-report, not a real
diff. And the file is never re-parsed to verify the change is still
syntactically valid. This module provides:

  1. real_diff()     — an actual unified diff + accurate line count
  2. py_parseable()  — AST-parse check for .py files
  3. verify_edit()   — the wrapper that should replace v3's write step

`verify_edit()` writes the new content, re-checks, and rolls back to the
.localedit.bak if the new content is a syntax disaster. Backups are
already created by v3; we just start trusting them.
"""

from __future__ import annotations

import ast
import difflib
import os
from pathlib import Path
from typing import NamedTuple


class EditResult(NamedTuple):
    ok: bool
    lines_changed: int
    added: int
    removed: int
    diff: str
    rolled_back: bool
    reason: str


def real_diff(old: str, new: str, path: str = "") -> tuple[str, int, int]:
    """
    Compute a real unified diff. Returns (diff_text, added_lines, removed_lines).
    """
    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}" if path else "a",
        tofile=f"b/{path}" if path else "b",
        lineterm="",
    ))
    added = sum(
        1 for line in diff_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff_lines
        if line.startswith("-") and not line.startswith("---")
    )
    return "\n".join(diff_lines), added, removed


def py_parseable(text: str) -> tuple[bool, str]:
    """AST-parse check. Returns (ok, error_msg)."""
    try:
        ast.parse(text)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} at line {e.lineno}"


def verify_edit(
    path: str,
    old_content: str,
    new_content: str,
    *,
    backup_path: str | None = None,
    require_py_valid: bool = True,
) -> EditResult:
    """
    Verify an edit BEFORE claiming success. If the new content:
      - Looks identical to old (no-op): return ok=False with reason
      - Is a .py file that no longer parses: roll back to backup, return ok=False
      - Otherwise: return ok=True with real diff stats

    This does NOT write the file — v3's local_edit.py has already done that
    by the time we're called. Our job is verify + roll back if necessary.
    """
    if old_content == new_content:
        return EditResult(
            ok=False, lines_changed=0, added=0, removed=0, diff="",
            rolled_back=False, reason="no-op: new content matches old",
        )

    diff_text, added, removed = real_diff(old_content, new_content, path)
    lines_changed = added + removed  # symmetric count

    # Syntax check for .py files
    if require_py_valid and path.endswith(".py"):
        ok, err = py_parseable(new_content)
        if not ok:
            rolled_back = _rollback(path, backup_path)
            return EditResult(
                ok=False, lines_changed=lines_changed, added=added, removed=removed,
                diff=diff_text, rolled_back=rolled_back,
                reason=f"py syntax broken after edit: {err}",
            )

    return EditResult(
        ok=True, lines_changed=lines_changed, added=added, removed=removed,
        diff=diff_text, rolled_back=False, reason="",
    )


def _rollback(path: str, backup_path: str | None) -> bool:
    """Restore from backup. Returns True if rollback succeeded."""
    if not backup_path:
        # Try the conventional v3 path
        backup_path = path + ".localedit.bak"
    try:
        if not os.path.exists(backup_path):
            return False
        backup_content = Path(backup_path).read_text(encoding="utf-8")
        Path(path).write_text(backup_content, encoding="utf-8")
        return True
    except Exception:
        return False
