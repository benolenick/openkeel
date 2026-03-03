"""Rule capsule re-injection for full-mode sessions.

Periodically injects constitution rules into the agent's context to
counteract context window decay. Two modes:

- Capsule: first N lines of rules file + template vars (every capsule_every commands)
- Full: entire rules file (every full_every commands)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .profile import Profile, ReinjectionConfig

logger = logging.getLogger(__name__)


def _load_counter(counter_path: Path) -> int:
    """Load the command counter from JSON file."""
    if not counter_path.exists():
        return 0
    try:
        data = json.loads(counter_path.read_text(encoding="utf-8"))
        return data.get("count", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def _save_counter(counter_path: Path, count: int) -> None:
    """Save the command counter to JSON file."""
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(json.dumps({"count": count}), encoding="utf-8")


def _read_rules_file(path: str) -> str:
    """Read the rules file. Returns empty string if unreadable."""
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _build_capsule(rules_text: str, config: ReinjectionConfig, context: dict[str, Any]) -> str:
    """Build a capsule from the first N lines of rules + template vars."""
    lines = rules_text.splitlines()[:config.capsule_lines]
    capsule = "\n".join(lines)

    # Apply template variables
    template_vars = {
        "{PHASE}": str(context.get("phase", "")),
        "{REMAINING}": str(context.get("remaining_minutes", "")),
        "{ELAPSED}": str(context.get("elapsed_minutes", "")),
        "{COMMANDS}": str(context.get("command_count", "")),
        "{DRIFT_COUNT}": str(context.get("drift_count", 0)),
        "{ACTIVE_ACTIVITY}": str(context.get("active_activity", "")),
    }

    for var, value in template_vars.items():
        capsule = capsule.replace(var, value)

    header = (
        "=" * 60 + "\n"
        "OPENKEEL RULE CAPSULE (re-injected, do not ignore)\n"
        + "=" * 60
    )
    return f"{header}\n{capsule}\n{'=' * 60}"


def _build_full(rules_text: str, context: dict[str, Any]) -> str:
    """Build a full rules injection."""
    header = (
        "=" * 60 + "\n"
        "OPENKEEL FULL RULES RE-INJECTION (do not ignore)\n"
        + "=" * 60
    )
    return f"{header}\n{rules_text}\n{'=' * 60}"


def maybe_inject(
    profile: Profile,
    counter_path: str | Path,
    **context: Any,
) -> str | None:
    """Check if it's time to inject a rule capsule or full rules.

    Increments the command counter and returns:
      - Full rules string if at full_every interval
      - Capsule string if at capsule_every interval
      - None otherwise

    Args:
        profile: Active profile with reinjection config.
        counter_path: Path to the counter JSON file.
        **context: Template variables (phase, remaining_minutes, etc.)

    Returns:
        Injection string or None.
    """
    counter_path = Path(counter_path)
    config = profile.reinjection
    count = _load_counter(counter_path) + 1
    _save_counter(counter_path, count)

    rules_path = config.rules_path
    if not rules_path:
        # Fall back to constitution path from default config location
        const_path = Path.home() / ".openkeel" / "constitution.yaml"
        if const_path.exists():
            rules_path = str(const_path)

    rules_text = _read_rules_file(rules_path)
    if not rules_text:
        return None

    # Full injection takes priority
    if config.full_every > 0 and count % config.full_every == 0:
        return _build_full(rules_text, context)

    # Capsule injection
    if config.capsule_every > 0 and count % config.capsule_every == 0:
        return _build_capsule(rules_text, config, context)

    return None


def should_inject_capsule(count: int, profile: Profile) -> bool:
    """Check if a capsule should be injected at this command count (for testing)."""
    config = profile.reinjection
    if config.capsule_every <= 0:
        return False
    return count % config.capsule_every == 0
