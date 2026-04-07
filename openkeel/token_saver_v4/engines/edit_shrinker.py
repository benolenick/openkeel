"""Edit old_string anchor shrinker — deterministic, no LLM.

Shrinks an Edit tool's `old_string` to the minimum contiguous substring
that is still unique in the target file. Pure Python, no hallucination
risk, ~1ms per call.

DESIGN NOTE: First draft used an LLM ("find the shortest unique substring").
qwen2.5:3b hallucinated substrings that weren't in the file at all. This
version is pure Python, deterministic, cannot hallucinate, faster.

Approach:
  1. Skip if old_string < MIN_SHRINK_CHARS.
  2. Split into lines. For every contiguous line-window from shortest to
     longest, check if it's unique in the file AND a real substring of
     old_string.
  3. Reject windows whose anchor lines are blank or brace-only (bad anchors).
  4. Return the shortest qualifying window.

Fails open on any exception.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MIN_SHRINK_CHARS = 400
MAX_FILE_BYTES = 500_000
MIN_ANCHOR_CHARS = 15


@dataclass
class ShrinkResult:
    original: str
    shrunk: str
    mode: str
    original_chars: int
    shrunk_chars: int

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.shrunk_chars)

    @property
    def changed(self) -> bool:
        return self.shrunk != self.original


def _is_meaningful(line: str) -> bool:
    s = line.strip()
    if len(s) < 3:
        return False
    if s in ("{", "}", "()", "[]", "{}", "*/", "/*", "---", "'''", '"""', "#", "//"):
        return False
    return True


def _find_shortest_unique_window(old_string: str, file_content: str) -> str | None:
    lines = old_string.split("\n")
    n = len(lines)

    if n < 2:
        s = old_string
        for end in range(MIN_ANCHOR_CHARS, len(s) + 1):
            cand = s[:end]
            if file_content.count(cand) == 1 and cand in s:
                return cand
        return None

    for window_size in range(1, n):
        for start in range(0, n - window_size + 1):
            window_lines = lines[start:start + window_size]
            if not _is_meaningful(window_lines[0]):
                continue
            if not _is_meaningful(window_lines[-1]):
                continue
            candidate = "\n".join(window_lines)
            if len(candidate) < MIN_ANCHOR_CHARS:
                continue
            if file_content.count(candidate) == 1 and candidate in old_string:
                return candidate
    return None


def shrink(old_string: str, file_path: str) -> ShrinkResult:
    orig_len = len(old_string)

    if orig_len < MIN_SHRINK_CHARS:
        return ShrinkResult(old_string, old_string, "skip_small", orig_len, orig_len)

    if not file_path or not os.path.exists(file_path):
        return ShrinkResult(old_string, old_string, "no_file", orig_len, orig_len)

    try:
        with open(file_path, "r", errors="replace") as f:
            content = f.read()
    except Exception:
        return ShrinkResult(old_string, old_string, "read_fail", orig_len, orig_len)

    if len(content) > MAX_FILE_BYTES:
        return ShrinkResult(old_string, old_string, "file_too_large", orig_len, orig_len)

    if content.count(old_string) != 1:
        return ShrinkResult(old_string, old_string, "not_unique", orig_len, orig_len)

    try:
        candidate = _find_shortest_unique_window(old_string, content)
    except Exception:
        return ShrinkResult(old_string, old_string, "search_fail", orig_len, orig_len)

    if candidate is None or len(candidate) >= orig_len:
        return ShrinkResult(old_string, old_string, "no_shrink_possible", orig_len, orig_len)

    return ShrinkResult(old_string, candidate, "shrunk", orig_len, len(candidate))


def shrink_edit(old_string: str, new_string: str, file_path: str) -> tuple[str, str, ShrinkResult]:
    result = shrink(old_string, file_path)
    return result.shrunk, new_string, result
