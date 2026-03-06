"""OpenKeel operational modes.

Modes are behavioral presets that overlay on top of profiles. They modify
how the governance engine behaves at runtime without changing the underlying
profile rules.

Modes
-----
normal      Standard governance — profile rules apply as written.
babysit     Watch a process/log. Poll every N minutes. Alert on errors.
              Saves tokens by sleeping between checks.
stakeout    Passive surveillance. Watch for regex/semantic patterns in
              logs or process output. Alert when triggered. No action.
lockdown    Block ALL commands. Hard stop. Nothing executes until released.
audit       Read-only. All write/mutating operations blocked. Safe for
              code review and investigation.
pair        Every command requires explicit user approval before execution.
              Like pair programming with a senior dev.
training    Log everything, block nothing. Review the audit log afterward
              to tune constitution rules. Good for building new profiles.
essential   Filter output. Only show decisions, status updates, and results.
              Suppress verbose reasoning and filler text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------

@dataclass
class ModeConfig:
    """Runtime configuration for an operational mode."""
    name: str
    description: str
    default_action: str  # allow / deny / gate
    override_blocked: list[str] = field(default_factory=list)
    override_safe: list[str] = field(default_factory=list)
    allow_writes: bool = True
    require_approval: bool = False
    log_only: bool = False


MODES: dict[str, ModeConfig] = {
    "normal": ModeConfig(
        name="normal",
        description="Standard governance — profile rules apply as written",
        default_action="profile",  # use profile's default
        allow_writes=True,
    ),
    "babysit": ModeConfig(
        name="babysit",
        description="Watch a process/log, poll periodically, alert on errors",
        default_action="profile",
        allow_writes=True,
    ),
    "stakeout": ModeConfig(
        name="stakeout",
        description="Passive surveillance — watch for patterns, alert when triggered",
        default_action="profile",
        allow_writes=False,
    ),
    "lockdown": ModeConfig(
        name="lockdown",
        description="Block ALL commands — hard stop until released",
        default_action="deny",
        allow_writes=False,
    ),
    "audit": ModeConfig(
        name="audit",
        description="Read-only — all write/mutating operations blocked",
        default_action="deny",
        allow_writes=False,
        override_safe=[
            r'^\s*(cat|less|head|tail|more)\s',
            r'^\s*(grep|rg|ag|ack)\s',
            r'^\s*(find|fd|ls|ll|la|dir|pwd|tree)\b',
            r'^\s*(wc|sort|uniq|diff|comm)\s',
            r'^\s*(file|stat|du|df)\s',
            r'^\s*(git\s+(log|diff|status|show|blame|branch|tag|remote))\b',
            r'^\s*(python[23]?\s+-c\s)',
            r'^\s*(sqlite3\s+\S+\s+"SELECT)',
            r'^\s*(type|Get-Content|Select-String)\s',
            r'^\s*(echo|printf)\s',
            r'^\s*(id|whoami|hostname|uname|env|set)\b',
        ],
    ),
    "pair": ModeConfig(
        name="pair",
        description="Every command requires explicit approval before execution",
        default_action="gate",
        allow_writes=True,
        require_approval=True,
    ),
    "training": ModeConfig(
        name="training",
        description="Log everything, block nothing — review logs to tune rules",
        default_action="allow",
        allow_writes=True,
        log_only=True,
    ),
    "essential": ModeConfig(
        name="essential",
        description="Filter output — only show decisions, status, and results",
        default_action="profile",
        allow_writes=True,
    ),
}


def get_mode(name: str) -> ModeConfig:
    """Return mode config by name, or 'normal' if not found."""
    return MODES.get(name, MODES["normal"])


def list_modes() -> list[str]:
    """Return list of all mode names."""
    return list(MODES.keys())


# ---------------------------------------------------------------------------
# Mode state — persisted in ~/.openkeel/mode_state.json
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path.home() / ".openkeel" / "mode_state.json"


def get_active_mode() -> str:
    """Return the currently active mode name."""
    path = _state_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("mode", "normal")
        except (json.JSONDecodeError, OSError):
            pass
    return "normal"


def set_active_mode(mode: str) -> None:
    """Set the active mode."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    data["mode"] = mode
    data["changed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Babysit mode — process/log watcher
# ---------------------------------------------------------------------------

@dataclass
class BabysitConfig:
    """Configuration for babysit mode."""
    target: str  # process name, PID, or log file path
    check_interval_seconds: int = 300  # 5 minutes default
    error_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)\b(error|exception|fatal|panic|crash|segfault|killed)\b",
        r"(?i)\b(failed|failure|denied|refused|timeout)\b",
        r"(?i)exit\s+code\s+[1-9]",
        r"(?i)traceback\s+\(most\s+recent",
    ])
    on_error: str = "alert"  # alert / log / command
    on_error_command: str = ""  # command to run on error detection
    max_checks: int = 0  # 0 = unlimited


def _babysit_state_path() -> Path:
    return Path.home() / ".openkeel" / "babysit_state.json"


def save_babysit_config(config: BabysitConfig) -> None:
    """Save babysit configuration."""
    path = _babysit_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "target": config.target,
        "check_interval_seconds": config.check_interval_seconds,
        "error_patterns": config.error_patterns,
        "on_error": config.on_error,
        "on_error_command": config.on_error_command,
        "max_checks": config.max_checks,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_babysit_config() -> BabysitConfig | None:
    """Load babysit configuration if it exists."""
    path = _babysit_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BabysitConfig(
            target=data["target"],
            check_interval_seconds=data.get("check_interval_seconds", 300),
            error_patterns=data.get("error_patterns", []),
            on_error=data.get("on_error", "alert"),
            on_error_command=data.get("on_error_command", ""),
            max_checks=data.get("max_checks", 0),
        )
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def babysit_check(config: BabysitConfig) -> list[str]:
    """Run one babysit check. Returns list of matched error lines."""
    target = config.target
    matches: list[str] = []
    compiled = [re.compile(p) for p in config.error_patterns]

    # Check if target is a file path
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8", errors="ignore") as f:
                # Read last 100 lines
                lines = f.readlines()[-100:]
            for line in lines:
                for pat in compiled:
                    if pat.search(line):
                        matches.append(line.strip())
                        break
        except OSError:
            matches.append(f"[babysit] Cannot read target file: {target}")
        return matches

    # Check if target is a process name or PID
    try:
        if target.isdigit():
            # PID check
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {target}"],
                    capture_output=True, text=True, timeout=10,
                )
                if target not in result.stdout:
                    matches.append(f"[babysit] Process PID {target} is NOT running")
            else:
                result = subprocess.run(
                    ["ps", "-p", target], capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    matches.append(f"[babysit] Process PID {target} is NOT running")
        else:
            # Process name check
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist"], capture_output=True, text=True, timeout=10,
                )
                if target.lower() not in result.stdout.lower():
                    matches.append(f"[babysit] Process '{target}' is NOT running")
            else:
                result = subprocess.run(
                    ["pgrep", "-f", target], capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    matches.append(f"[babysit] Process '{target}' is NOT running")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        matches.append(f"[babysit] Check failed: {e}")

    return matches


# ---------------------------------------------------------------------------
# Stakeout mode — pattern watcher
# ---------------------------------------------------------------------------

@dataclass
class StakeoutConfig:
    """Configuration for stakeout mode."""
    targets: list[str]  # file paths or process names to watch
    patterns: list[str]  # regex patterns to alert on
    check_interval_seconds: int = 60
    on_match: str = "alert"  # alert / log
    max_alerts: int = 0  # 0 = unlimited


def _stakeout_state_path() -> Path:
    return Path.home() / ".openkeel" / "stakeout_state.json"


def save_stakeout_config(config: StakeoutConfig) -> None:
    """Save stakeout configuration."""
    path = _stakeout_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "targets": config.targets,
        "patterns": config.patterns,
        "check_interval_seconds": config.check_interval_seconds,
        "on_match": config.on_match,
        "max_alerts": config.max_alerts,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_stakeout_config() -> StakeoutConfig | None:
    """Load stakeout configuration if it exists."""
    path = _stakeout_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return StakeoutConfig(
            targets=data["targets"],
            patterns=data["patterns"],
            check_interval_seconds=data.get("check_interval_seconds", 60),
            on_match=data.get("on_match", "alert"),
            max_alerts=data.get("max_alerts", 0),
        )
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def stakeout_check(config: StakeoutConfig) -> list[str]:
    """Run one stakeout check across all targets. Returns matched lines."""
    matches: list[str] = []
    compiled = [re.compile(p) for p in config.patterns]

    for target in config.targets:
        if os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()[-200:]
                for line in lines:
                    for pat in compiled:
                        if pat.search(line):
                            matches.append(f"[{target}] {line.strip()}")
                            break
            except OSError:
                pass

    return matches


# ---------------------------------------------------------------------------
# Mode-aware command classification
# ---------------------------------------------------------------------------

def apply_mode_override(
    mode_name: str,
    command: str,
    profile_action: str,
    profile_tier: str,
) -> tuple[str, str]:
    """Apply mode overrides to a classification result.

    Returns (final_action, reason).
    """
    mode = get_mode(mode_name)

    # Lockdown blocks everything
    if mode.name == "lockdown":
        return "deny", "LOCKDOWN mode — all commands blocked"

    # Training allows everything (but logs)
    if mode.name == "training":
        return "allow", "TRAINING mode — logged for review"

    # Audit mode — only safe read-only commands allowed
    if mode.name == "audit":
        for pattern in mode.override_safe:
            if re.search(pattern, command):
                return "allow", "AUDIT mode — read-only command allowed"
        return "deny", "AUDIT mode — write/mutating operations blocked"

    # Pair mode — everything needs approval
    if mode.name == "pair":
        if profile_action == "deny":
            return "deny", profile_tier  # still respect hard blocks
        return "gate", "PAIR mode — approval required"

    # Normal, babysit, stakeout, essential — use profile's decision
    return profile_action, profile_tier
