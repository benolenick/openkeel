"""Profile dataclasses and YAML loading for full-mode sessions.

A *profile* defines the complete policy for a full-mode run:
which commands are safe/gated/blocked, scope constraints (IPs, paths),
activity definitions (for timeboxing), phases + gates, re-injection
cadence, and optional host sandbox settings.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CommandTier:
    """A tier of command patterns with a shared action."""
    patterns: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ScopeConfig:
    """Network and filesystem scope constraints."""
    allowed_ips: list[str] = field(default_factory=list)  # CIDRs or IPs
    allowed_hostnames: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)  # glob patterns
    denied_paths: list[str] = field(default_factory=list)


@dataclass
class ActivityDef:
    """An activity category for timeboxing."""
    name: str = ""
    patterns: list[str] = field(default_factory=list)  # regex patterns matching commands
    timebox_minutes: int = 0  # 0 = unlimited
    grace_minutes: int = 5  # extra time after warn before block


@dataclass
class GateDef:
    """A prerequisite gate that must pass before entering a phase."""
    type: str = ""  # "file_exists", "command_output", "exit_code", "external"
    target: str = ""  # path, command, or URL depending on type
    expect: str = ""  # expected value/pattern (for command_output/exit_code)
    message: str = ""  # human-readable description


@dataclass
class PhaseDef:
    """A phase in a multi-phase workflow."""
    name: str = ""
    description: str = ""
    timeout_minutes: int = 0  # 0 = no timeout
    auto_advance: bool = False  # advance to next phase on timeout
    gates: list[GateDef] = field(default_factory=list)


@dataclass
class ReinjectionConfig:
    """Configuration for periodic rule re-injection."""
    capsule_every: int = 20  # inject short capsule every N commands
    full_every: int = 100  # inject full rules every M commands
    rules_path: str = ""  # path to rules.txt (defaults to constitution path)
    capsule_lines: int = 20  # first N lines of rules file for capsule


@dataclass
class SandboxConfig:
    """Host sandbox configuration (Linux systemd-run)."""
    enabled: bool = False
    memory_max: str = "4G"
    cpu_quota: str = ""  # e.g. "200%" for 2 cores
    network_deny: list[str] = field(default_factory=list)  # CIDRs to block
    readonly_paths: list[str] = field(default_factory=list)
    inaccessible_paths: list[str] = field(default_factory=list)


@dataclass
class LearningConfig:
    """Cross-session learning via an external memory backend."""
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8000"  # memory backend URL
    timeout: int = 15  # seconds per request
    extract_on: list[str] = field(default_factory=lambda: [
        "timebox_blocks",
        "successful_phases",
        "drift_events",
    ])
    auto_seed: bool = True  # automatically seed lessons after session ends
    search_top_k: int = 5  # default top_k for memory_search gates


@dataclass
class Profile:
    """Complete policy profile for a full-mode session."""
    name: str = ""
    description: str = ""
    version: str = "1"

    # Command classification tiers
    blocked: CommandTier = field(default_factory=CommandTier)
    gated: CommandTier = field(default_factory=CommandTier)
    safe: CommandTier = field(default_factory=CommandTier)
    default_action: str = "allow"  # "allow" or "deny" for unmatched commands

    # Scope
    scope: ScopeConfig = field(default_factory=ScopeConfig)

    # Activities (for timeboxing)
    activities: list[ActivityDef] = field(default_factory=list)

    # Phases
    phases: list[PhaseDef] = field(default_factory=list)

    # Re-injection
    reinjection: ReinjectionConfig = field(default_factory=ReinjectionConfig)

    # Sandbox
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    # Learning (cross-session memory)
    learning: LearningConfig = field(default_factory=LearningConfig)

    # Metadata
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_command_tier(raw: dict[str, Any] | None) -> CommandTier:
    if not raw:
        return CommandTier()
    return CommandTier(
        patterns=raw.get("patterns", []),
        message=raw.get("message", ""),
    )


def _parse_scope(raw: dict[str, Any] | None) -> ScopeConfig:
    if not raw:
        return ScopeConfig()
    return ScopeConfig(
        allowed_ips=raw.get("allowed_ips", []),
        allowed_hostnames=raw.get("allowed_hostnames", []),
        allowed_paths=raw.get("allowed_paths", []),
        denied_paths=raw.get("denied_paths", []),
    )


def _parse_gate(raw: dict[str, Any]) -> GateDef:
    return GateDef(
        type=raw.get("type", ""),
        target=raw.get("target", ""),
        expect=raw.get("expect", ""),
        message=raw.get("message", ""),
    )


def _parse_phase(raw: dict[str, Any]) -> PhaseDef:
    gates = [_parse_gate(g) for g in raw.get("gates", [])]
    return PhaseDef(
        name=raw.get("name", ""),
        description=raw.get("description", ""),
        timeout_minutes=raw.get("timeout_minutes", 0),
        auto_advance=raw.get("auto_advance", False),
        gates=gates,
    )


def _parse_activity(raw: dict[str, Any]) -> ActivityDef:
    return ActivityDef(
        name=raw.get("name", ""),
        patterns=raw.get("patterns", []),
        timebox_minutes=raw.get("timebox_minutes", 0),
        grace_minutes=raw.get("grace_minutes", 5),
    )


def _parse_reinjection(raw: dict[str, Any] | None) -> ReinjectionConfig:
    if not raw:
        return ReinjectionConfig()
    return ReinjectionConfig(
        capsule_every=raw.get("capsule_every", 20),
        full_every=raw.get("full_every", 100),
        rules_path=raw.get("rules_path", ""),
        capsule_lines=raw.get("capsule_lines", 20),
    )


def _parse_sandbox(raw: dict[str, Any] | None) -> SandboxConfig:
    if not raw:
        return SandboxConfig()
    return SandboxConfig(
        enabled=raw.get("enabled", False),
        memory_max=raw.get("memory_max", "4G"),
        cpu_quota=raw.get("cpu_quota", ""),
        network_deny=raw.get("network_deny", []),
        readonly_paths=raw.get("readonly_paths", []),
        inaccessible_paths=raw.get("inaccessible_paths", []),
    )


def _parse_learning(raw: dict[str, Any] | None) -> LearningConfig:
    if not raw:
        return LearningConfig()
    return LearningConfig(
        enabled=raw.get("enabled", False),
        endpoint=raw.get("endpoint", "http://127.0.0.1:8000"),
        timeout=raw.get("timeout", 15),
        extract_on=raw.get("extract_on", ["timebox_blocks", "successful_phases", "drift_events"]),
        auto_seed=raw.get("auto_seed", True),
        search_top_k=raw.get("search_top_k", 5),
    )


def _parse_profile(data: dict[str, Any]) -> Profile:
    """Convert a raw YAML dict into a typed Profile dataclass."""
    activities = [_parse_activity(a) for a in data.get("activities", [])]
    phases = [_parse_phase(p) for p in data.get("phases", [])]

    return Profile(
        name=data.get("name", ""),
        description=data.get("description", ""),
        version=str(data.get("version", "1")),
        blocked=_parse_command_tier(data.get("blocked")),
        gated=_parse_command_tier(data.get("gated")),
        safe=_parse_command_tier(data.get("safe")),
        default_action=data.get("default_action", "allow"),
        scope=_parse_scope(data.get("scope")),
        activities=activities,
        phases=phases,
        reinjection=_parse_reinjection(data.get("reinjection")),
        sandbox=_parse_sandbox(data.get("sandbox")),
        learning=_parse_learning(data.get("learning")),
        tags=data.get("tags", []),
    )


# ---------------------------------------------------------------------------
# Profile search paths
# ---------------------------------------------------------------------------

_BUNDLED_DIR = Path(__file__).resolve().parent.parent.parent / "profiles"
_USER_DIR = Path.home() / ".openkeel" / "profiles"


def _profile_search_paths() -> list[Path]:
    """Return directories to search for profile YAML files, in order."""
    paths = [_USER_DIR, _BUNDLED_DIR]
    env_dir = os.environ.get("OPENKEEL_PROFILES_DIR")
    if env_dir:
        paths.insert(0, Path(env_dir))
    return paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_profile(name_or_path: str) -> Profile:
    """Load a profile by name or file path.

    Search order:
      1. Exact file path (if it exists)
      2. OPENKEEL_PROFILES_DIR env var
      3. ~/.openkeel/profiles/
      4. Bundled profiles/ directory

    Raises FileNotFoundError if no profile is found.
    """
    # Try as exact path first
    candidate = Path(name_or_path).expanduser()
    if candidate.exists() and candidate.is_file():
        return _load_profile_file(candidate)

    # Search by name
    for search_dir in _profile_search_paths():
        for ext in (".yaml", ".yml"):
            candidate = search_dir / f"{name_or_path}{ext}"
            if candidate.exists():
                return _load_profile_file(candidate)

    raise FileNotFoundError(
        f"Profile '{name_or_path}' not found. "
        f"Searched: {', '.join(str(p) for p in _profile_search_paths())}"
    )


def _load_profile_file(path: Path) -> Profile:
    """Load and parse a single profile YAML file."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"Profile file {path} does not contain a YAML mapping.")

    profile = _parse_profile(data)
    if not profile.name:
        profile.name = path.stem
    return profile


def list_profiles() -> list[str]:
    """List all available profile names (bundled + user)."""
    seen: set[str] = set()
    names: list[str] = []

    for search_dir in reversed(_profile_search_paths()):
        if not search_dir.exists():
            continue
        for path in sorted(search_dir.glob("*.yaml")):
            stem = path.stem
            if stem not in seen:
                seen.add(stem)
                names.append(stem)
        for path in sorted(search_dir.glob("*.yml")):
            stem = path.stem
            if stem not in seen:
                seen.add(stem)
                names.append(stem)

    return sorted(names)


def validate_profile(profile: Profile) -> list[str]:
    """Validate a profile and return a list of issues (empty = valid).

    Checks:
      - blocked/gated/safe patterns are valid regexes
      - activities have names and valid patterns
      - phases have names
      - gates have valid types
      - reinjection intervals make sense
    """
    import re
    issues: list[str] = []

    # Check command tier patterns
    for tier_name, tier in [("blocked", profile.blocked), ("gated", profile.gated), ("safe", profile.safe)]:
        for i, pattern in enumerate(tier.patterns):
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(f"{tier_name}.patterns[{i}]: invalid regex '{pattern}': {exc}")

    # Check activities
    for i, activity in enumerate(profile.activities):
        if not activity.name:
            issues.append(f"activities[{i}]: missing name")
        for j, pattern in enumerate(activity.patterns):
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(f"activities[{i}].patterns[{j}]: invalid regex '{pattern}': {exc}")
        if activity.timebox_minutes < 0:
            issues.append(f"activities[{i}]: timebox_minutes cannot be negative")

    # Check phases
    for i, phase in enumerate(profile.phases):
        if not phase.name:
            issues.append(f"phases[{i}]: missing name")
        for j, gate in enumerate(phase.gates):
            valid_types = ("file_exists", "command_output", "exit_code", "external", "memory_search")
            if gate.type and gate.type not in valid_types:
                issues.append(
                    f"phases[{i}].gates[{j}]: invalid type '{gate.type}', "
                    f"must be one of: {', '.join(valid_types)}"
                )

    # Check reinjection
    if profile.reinjection.capsule_every < 0:
        issues.append("reinjection.capsule_every cannot be negative")
    if profile.reinjection.full_every < 0:
        issues.append("reinjection.full_every cannot be negative")
    if (profile.reinjection.capsule_every > 0
            and profile.reinjection.full_every > 0
            and profile.reinjection.full_every < profile.reinjection.capsule_every):
        issues.append(
            "reinjection.full_every should be >= capsule_every "
            f"(got {profile.reinjection.full_every} < {profile.reinjection.capsule_every})"
        )

    # Check default_action
    if profile.default_action not in ("allow", "deny"):
        issues.append(f"default_action must be 'allow' or 'deny', got '{profile.default_action}'")

    return issues
