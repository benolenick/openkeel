"""
Token Saver v5 — error-loop detection and nudges.

When an agent hits the same class of error repeatedly (ModuleNotFoundError on
the same module, same "file not found" path, same HTTP 404, same test failure)
it burns tokens re-ingesting nearly-identical error output. Worse, the
ITERATIONS themselves compound: each retry costs another tool call + another
read of a nearly-identical error.

Most other token-saver optimizations save per-call. This one saves per-LOOP:
if we can notice "you've hit this 3 times" and inject a terse nudge after
the third occurrence, we short-circuit the 4th, 5th, and 6th iteration
entirely.

Approach:
  1. Fingerprint each failing tool result with a cheap signature
     (error class + normalized stack head + normalized first error line).
  2. Track count per fingerprint in a per-session JSON file.
  3. On the Nth repeat (default N=3), return a nudge message that the
     pre_tool hook can surface as a system reminder on the NEXT tool call.
  4. Age out state after SESSION_TTL so cross-session noise is bounded.

This is deliberately conservative — we never block a tool call, we only
suggest. The LLM stays in control.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .config import CFG, ensure_dirs
from .debug_log import note, swallow


REPEAT_THRESHOLD = 3  # Nudge on the 3rd identical failure
SESSION_TTL = 6 * 3600  # 6 hours — wider than a typical session
MAX_ENTRIES = 200     # hard cap on state file size — evict oldest if exceeded

# Strong error markers — an output must contain at least one of these to be
# considered a failure worth fingerprinting. This prevents "every successful
# bash output that happens to mention 'error_loop_state.json' from getting
# tracked as a repeating error". Prefixes are case-sensitive for Python
# exceptions; lowercase tokens are case-insensitive.
_EXCEPTION_NAMES = (
    "Error:", "Exception:", "Traceback (most recent call last)",
    "SyntaxError", "ValueError", "KeyError", "TypeError", "AttributeError",
    "NameError", "IndexError", "ZeroDivisionError", "ImportError",
    "ModuleNotFoundError", "FileNotFoundError", "PermissionError",
    "ConnectionError", "TimeoutError", "RuntimeError", "AssertionError",
    "OSError", "IOError",
)
_FAIL_TOKENS = (
    " error:", "error: ", "errno ", "fatal:", "failed:", "failure:",
    "command not found", "no such file", "permission denied",
    "connection refused", "cannot access", "is not a", "cannot open",
    "segfault", "core dumped", "broken pipe", "address already in use",
    "exit code", "exit status", "killed", "aborted",
    "404 not found", "500 internal server", "503 service", "bad gateway",
)


# Regexes that normalize common noise out of error signatures so
# "file /tmp/foo123.txt" and "file /tmp/foo456.txt" fingerprint the same.
_NOISE_PATTERNS = [
    (re.compile(r"\b\d{5,}\b"), "N"),                 # long numbers (pids, ts, sizes)
    (re.compile(r"0x[0-9a-fA-F]+"), "0xH"),           # hex addresses
    (re.compile(r"/tmp/[^\s'\"]+"), "/tmp/X"),        # tempfile paths
    (re.compile(r"line \d+"), "line N"),              # line numbers
    (re.compile(r"\s+"), " "),                        # collapse whitespace
]


@dataclass
class LoopEntry:
    fingerprint: str
    first_seen: float
    last_seen: float
    count: int
    tool: str
    summary: str  # short human-readable preview
    nudged_at_count: int = 0  # highest count we've already nudged about

    def is_stale(self, now: float) -> bool:
        return (now - self.last_seen) > SESSION_TTL


@dataclass
class LoopState:
    entries: dict[str, LoopEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: asdict(v) for k, v in self.entries.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopState":
        entries = {
            k: LoopEntry(**v) for k, v in data.items()
            if isinstance(v, dict) and "fingerprint" in v
        }
        return cls(entries=entries)


def looks_like_failure(text: str) -> bool:
    """
    Return True if `text` shows strong evidence of a failure.

    This is the gate that prevents observe() from recording every successful
    bash output as a potential "error loop". We require at least one strong
    marker: a Python exception class name, a unix error token, or an HTTP
    error code. Plain words like "error" in a file path do NOT qualify.

    Tuned to be conservative: we'd rather miss some failures than nudge on
    noise. If a real failure slips past, the agent will retry and hit it
    again — the 3rd occurrence still catches a genuine loop.
    """
    if not text:
        return False
    # Fast path: check the last 2KB — errors usually at the tail
    tail = text[-2048:] if len(text) > 2048 else text
    low = tail.lower()
    # Strong Python / JS / Rust exception signals (case-sensitive)
    for marker in _EXCEPTION_NAMES:
        if marker in tail:
            return True
    for token in _FAIL_TOKENS:
        if token in low:
            return True
    return False


def fingerprint_error(text: str) -> tuple[str, str]:
    """
    Return (fingerprint_hash, short_preview).

    The fingerprint is stable across cosmetic noise (pids, tempfiles, line
    numbers) but sensitive to the real error class + message.
    """
    # Extract the most signal-dense lines — error class name + first "error" line
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return "", ""

    # Prefer lines that look like Python error class or "Error:" / "error:" text
    candidates: list[str] = []
    for line in lines:
        low = line.lower()
        if "error" in low or "traceback" in low or "exception" in low or "failed" in low:
            candidates.append(line.strip())
        if len(candidates) >= 3:
            break
    if not candidates:
        candidates = [lines[-1].strip()]

    raw = " | ".join(candidates)[:500]
    normalized = raw
    for pattern, replacement in _NOISE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)

    h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    preview = raw[:120]
    return h, preview


def _load_state() -> LoopState:
    try:
        ensure_dirs()
        if not CFG.error_loop_state.exists():
            return LoopState()
        data = json.loads(CFG.error_loop_state.read_text(encoding="utf-8"))
        return LoopState.from_dict(data)
    except Exception as e:
        swallow("error_loop.load", error=e)
        return LoopState()


def _save_state(state: LoopState) -> None:
    try:
        ensure_dirs()
        CFG.error_loop_state.write_text(
            json.dumps(state.to_dict(), default=str),
            encoding="utf-8",
        )
    except Exception as e:
        swallow("error_loop.save", error=e)


def _gc(state: LoopState) -> None:
    """
    Evict stale entries so the state file stays bounded. Two passes:
      1. Drop anything older than SESSION_TTL.
      2. If still over MAX_ENTRIES, drop the oldest last_seen until under cap.
    """
    now = time.time()
    state.entries = {
        k: v for k, v in state.entries.items() if not v.is_stale(now)
    }
    if len(state.entries) > MAX_ENTRIES:
        # Keep the most-recently-seen MAX_ENTRIES, drop the rest
        sorted_entries = sorted(
            state.entries.items(), key=lambda kv: kv[1].last_seen, reverse=True,
        )
        state.entries = dict(sorted_entries[:MAX_ENTRIES])


def observe(tool: str, output: str) -> str | None:
    """
    Record an observation. If the caller has hit the same error 3+ times,
    return a nudge string. Otherwise return None.

    Call this from PostToolUse (after a tool call completes) with the
    output text. The returned nudge should be injected on the NEXT
    PreToolUse as a system reminder.
    """
    if not CFG.error_loop_nudges:
        return None
    if not output or len(output) < 20:
        return None

    # Gate: only fingerprint real failures, not every successful bash output.
    # Fixes the noise-pollution bug observed 2026-04-07 where the state file
    # accumulated 80+ entries per session from successful commands.
    if not looks_like_failure(output):
        return None

    fp, preview = fingerprint_error(output)
    if not fp:
        return None

    state = _load_state()
    _gc(state)

    now = time.time()
    entry = state.entries.get(fp)
    if entry is None:
        entry = LoopEntry(
            fingerprint=fp, first_seen=now, last_seen=now, count=1,
            tool=tool, summary=preview,
        )
        state.entries[fp] = entry
    else:
        entry.last_seen = now
        entry.count += 1
        entry.tool = tool  # most recent tool that hit it

    nudge: str | None = None
    if entry.count >= REPEAT_THRESHOLD and entry.count > entry.nudged_at_count:
        nudge = _format_nudge(entry)
        entry.nudged_at_count = entry.count
        note("error_loop", f"nudged on count={entry.count} fp={fp}", tool=tool)

    # Post-insert gc: ensure final persisted state is <= MAX_ENTRIES.
    # (_gc ran pre-insert to bound pre-insert size, but the insert itself
    # can push us one over; this second pass keeps the steady state exact.)
    _gc(state)
    _save_state(state)
    return nudge


def _format_nudge(entry: LoopEntry) -> str:
    return (
        f"[TS! loop] You've hit this error {entry.count} times via {entry.tool}:\n"
        f"  {entry.summary}\n"
        f"Consider: a different approach rather than another retry."
    )


def clear() -> None:
    """Wipe all loop state. Used by tests and manual resets."""
    try:
        if CFG.error_loop_state.exists():
            CFG.error_loop_state.unlink()
    except Exception as e:
        swallow("error_loop.clear", error=e)
