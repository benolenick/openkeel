"""Shared regex matching primitives used by both constitution engine and proxy shell."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class EvalResult:
    """Result of evaluating a command or tool call."""
    action: str  # "allow", "deny", "warn"
    rule_id: str = ""  # ID of the matched rule
    message: str = ""  # Human-readable explanation
    tier: str = ""  # "safe", "gated", "blocked"
    activity: str = ""  # Matched activity name (for timeboxing)


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _compile(pattern: str) -> re.Pattern:
    """Compile a regex pattern with caching."""
    if pattern not in _PATTERN_CACHE:
        _PATTERN_CACHE[pattern] = re.compile(pattern)
    return _PATTERN_CACHE[pattern]


def match_pattern(pattern: str, value: str) -> bool:
    """Test whether *pattern* (regex) matches anywhere in *value*."""
    return bool(_compile(pattern).search(value))


def match_any_pattern(patterns: list[str], value: str) -> bool:
    """Return True if any pattern in *patterns* matches *value*."""
    return any(match_pattern(p, value) for p in patterns)
