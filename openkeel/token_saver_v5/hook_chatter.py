"""
Token Saver v5 — terse hook status formatting.

v3/v4 hook messages are chatty. Observed per-message cost in one session:

  "[TOKEN SAVER ✓ EDIT APPLIED] The file has already been written.
   Do NOT retry this Edit call.
   File: ../.openkeel/hooks/monitor_cron.py
   Summary: 6 lines changed at line 150 (old_string was 335 chars;
   token saver performed the write directly to save tokens).
   Treat this as SUCCESS, not an error."
  ≈ 80 tokens × every Edit call

  "[TOKEN SAVER v4.3] Goal-filtered file (9467 -> 3574 chars,
   goal: (none — generic filter)). Use Read with offset/limit for
   raw sections."
  ≈ 40 tokens × every large Read

  "[TOKEN SAVER v4.4] Agent prompt was 4182 chars. Re-spawn the
   Agent with this tighter version (2218 chars, saves 46%)."
  + full rewritten prompt echoed
  ≈ 1800 tokens × every intercepted Agent call

Multiply by hundreds of tool calls and the token saver spends 20-30% of
its own savings on self-chatter. This module gives every hook a single
canonical formatter that produces compact status lines.
"""

from __future__ import annotations


def edit_applied(path: str, lines_changed: int, old_len: int, new_len: int) -> str:
    """
    `[TS✓ edit 25L -112c ../path]`
    """
    delta = new_len - old_len
    sign = "+" if delta >= 0 else ""
    short_path = _shorten_path(path)
    return f"[TS✓ edit {lines_changed}L {sign}{delta}c {short_path}]"


def file_filtered(path: str, orig_chars: int, new_chars: int, reason: str = "skeleton") -> str:
    """
    `[TS✓ filter 9467→3574 (skeleton) foo/bar.py]`
    """
    short_path = _shorten_path(path)
    return f"[TS✓ filter {orig_chars}→{new_chars} ({reason}) {short_path}]"


def bash_compressed(orig_chars: int, new_chars: int) -> str:
    return f"[TS✓ bash {orig_chars}→{new_chars}]"


def bash_passthrough(reason: str) -> str:
    """When we deliberately didn't compress (e.g. JSON detected)."""
    return f"[TS~ bash passthrough ({reason})]"


def cache_hit(path: str) -> str:
    return f"[TS✓ cache {_shorten_path(path)}]"


def recall_reranked(orig_count: int, new_count: int) -> str:
    return f"[TS✓ recall {orig_count}→{new_count}]"


def error_loop_nudge(fingerprint: str, repeat_count: int) -> str:
    return f"[TS! loop×{repeat_count} {fingerprint[:40]}]"


def fail_open(site: str, error_class: str) -> str:
    """When something swallowed an exception — visible but tiny."""
    return f"[TS⚠ {site} {error_class} → fail-open]"


def _shorten_path(path: str) -> str:
    """Trim to last 2 components so messages stay compact."""
    if not path:
        return ""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= 2:
        return path
    return ".../" + "/".join(parts[-2:])
