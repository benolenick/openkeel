"""
Token Saver v5 — structured debug log for swallowed exceptions.

v3's pre_tool.py has 28 `except Exception: pass` blocks. Fail-open is the
right safety default, but it means daemon-down / ollama-down / JSON-parse-fail
all become invisible passthroughs. This module exists so every swallow gets
written to ~/.openkeel/logs/token_saver_debug.log with enough context to
diagnose later.

USAGE:
    from openkeel.token_saver_v5.debug_log import note, swallow

    try:
        ...
    except Exception as e:
        swallow("bash_compress", tool="Bash", error=e, extra={"size": len(out)})

    # or for a non-exception observation:
    note("json_guard", "bypassed JSON output", tool="Bash")

Each line is one JSON object so the log is grep-friendly and machine-parseable.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

from .config import CFG, ensure_dirs

_LOG_INITIALIZED = False


def _init() -> None:
    global _LOG_INITIALIZED
    if _LOG_INITIALIZED:
        return
    try:
        ensure_dirs()
        _LOG_INITIALIZED = True
    except Exception:
        # If we can't even make the dir, give up silently so we never block
        # a real tool call.
        _LOG_INITIALIZED = True


def _write(entry: dict[str, Any]) -> None:
    _init()
    try:
        with open(CFG.debug_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        # Deliberately silent — debug log must never break the tool call path.
        pass


def note(site: str, msg: str, *, tool: str | None = None, **extra: Any) -> None:
    """Log a non-exception observation (e.g. "bypassed", "short-circuited")."""
    _write({
        "ts": time.time(),
        "level": "note",
        "site": site,
        "tool": tool,
        "msg": msg,
        "pid": os.getpid(),
        **extra,
    })


def swallow(
    site: str,
    *,
    tool: str | None = None,
    error: BaseException | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a swallowed exception at `site` with traceback."""
    tb = None
    err_type = None
    err_msg = None
    if error is not None:
        err_type = type(error).__name__
        err_msg = str(error)[:500]
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-1500:]
    _write({
        "ts": time.time(),
        "level": "swallow",
        "site": site,
        "tool": tool,
        "error_type": err_type,
        "error_msg": err_msg,
        "traceback": tb,
        "pid": os.getpid(),
        **(extra or {}),
    })


def tail(n: int = 50) -> list[dict[str, Any]]:
    """Read the last n entries as dicts. Used by tests and the dashboard."""
    try:
        with open(CFG.debug_log, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
