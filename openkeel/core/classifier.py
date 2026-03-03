"""Command classification for the proxy shell.

Classifies a shell command into a tier (BLOCKED / GATED / SAFE / default)
based on the active profile. Also extracts scope context (IPs, hostnames)
and matches activities for timeboxing.

Evaluation order:
  1. BLOCKED patterns → immediate deny
  2. GATED patterns  → scope check required
  3. SAFE patterns   → immediate allow
  4. No match        → profile.default_action
"""
from __future__ import annotations

import fnmatch
import ipaddress
import logging
import re
from typing import Any

from .evaluate import EvalResult, match_any_pattern, match_pattern
from .profile import Profile

logger = logging.getLogger(__name__)

# Regex to extract IP addresses from a command string
_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

# Regex to extract hostnames (simple heuristic: word.word patterns)
_HOSTNAME_RE = re.compile(
    r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b"
)


def _extract_ips(command: str) -> list[str]:
    """Extract IP addresses from a command string."""
    return _IP_RE.findall(command)


def _extract_hostnames(command: str) -> list[str]:
    """Extract hostnames from a command string."""
    return _HOSTNAME_RE.findall(command)


def _ip_in_scope(ip_str: str, allowed: list[str]) -> bool:
    """Check if an IP is within any of the allowed CIDRs/IPs."""
    if not allowed:
        return True  # no restrictions
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    for entry in allowed:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if addr == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


def _hostname_in_scope(hostname: str, allowed: list[str]) -> bool:
    """Check if a hostname matches any allowed hostname pattern (fnmatch globs)."""
    if not allowed:
        return True  # no restrictions
    hostname_lower = hostname.lower()
    for pattern in allowed:
        if fnmatch.fnmatch(hostname_lower, pattern.lower()):
            return True
    return False


def _check_scope(command: str, profile: Profile) -> tuple[bool, str]:
    """Check if the command's targets are within scope.

    Returns (in_scope, reason).
    """
    scope = profile.scope

    # Check IPs
    if scope.allowed_ips:
        for ip in _extract_ips(command):
            if not _ip_in_scope(ip, scope.allowed_ips):
                return False, f"IP {ip} is out of scope (allowed: {', '.join(scope.allowed_ips)})"

    # Check hostnames
    if scope.allowed_hostnames:
        for hostname in _extract_hostnames(command):
            if not _hostname_in_scope(hostname, scope.allowed_hostnames):
                return False, f"Hostname {hostname} is out of scope"

    # Check denied paths (glob patterns matched against words in the command)
    if scope.denied_paths:
        # Extract path-like tokens from the command
        tokens = command.split()
        for pattern in scope.denied_paths:
            for token in tokens:
                if fnmatch.fnmatch(token, pattern):
                    return False, f"Command touches denied path (matched: {pattern})"

    return True, ""


def _match_activity(command: str, profile: Profile) -> str:
    """Match command against activity definitions, return activity name or ""."""
    for activity in profile.activities:
        if match_any_pattern(activity.patterns, command):
            return activity.name
    return ""


def classify(
    command: str,
    profile: Profile,
    scope_context: dict[str, Any] | None = None,
) -> EvalResult:
    """Classify a command according to the profile's tiers and scope.

    Args:
        command: The shell command string to classify.
        profile: The active profile defining tiers and scope.
        scope_context: Optional additional scope info (not used in v1).

    Returns:
        EvalResult with action, tier, activity, and message.
    """
    # 1. BLOCKED — safety first
    if profile.blocked.patterns and match_any_pattern(profile.blocked.patterns, command):
        return EvalResult(
            action="deny",
            rule_id="blocked-tier",
            message=profile.blocked.message or "Command matches BLOCKED tier",
            tier="blocked",
            activity=_match_activity(command, profile),
        )

    # 2. GATED — needs scope check
    if profile.gated.patterns and match_any_pattern(profile.gated.patterns, command):
        in_scope, reason = _check_scope(command, profile)
        if not in_scope:
            return EvalResult(
                action="deny",
                rule_id="gated-scope-fail",
                message=f"GATED command out of scope: {reason}",
                tier="gated",
                activity=_match_activity(command, profile),
            )
        # Gated but in scope → allow
        return EvalResult(
            action="allow",
            rule_id="gated-scope-pass",
            message="GATED command passed scope check",
            tier="gated",
            activity=_match_activity(command, profile),
        )

    # 3. SAFE — explicit allow
    if profile.safe.patterns and match_any_pattern(profile.safe.patterns, command):
        return EvalResult(
            action="allow",
            rule_id="safe-tier",
            message="Command matches SAFE tier",
            tier="safe",
            activity=_match_activity(command, profile),
        )

    # 4. Default action
    activity = _match_activity(command, profile)
    if profile.default_action == "deny":
        return EvalResult(
            action="deny",
            rule_id="default-deny",
            message="Command did not match any tier; default action is deny",
            tier="default",
            activity=activity,
        )

    return EvalResult(
        action="allow",
        rule_id="default-allow",
        message="",
        tier="default",
        activity=activity,
    )
