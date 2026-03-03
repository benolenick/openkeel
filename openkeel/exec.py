"""Proxy shell entry point — installed as ``openkeel-exec``.

When ``openkeel run`` launches an agent, it sets ``SHELL=openkeel-exec``.
Every time the agent runs a shell command (``subprocess.run(..., shell=True)``),
the OS invokes this script as::

    openkeel-exec -c "the actual command"

This script:
  1. Loads the profile from ``OPENKEEL_PROFILE`` env var
  2. Classifies the command (BLOCKED / GATED / SAFE / default)
  3. Checks scope (IPs, paths)
  4. Checks timebox (activity tracking)
  5. Maybe injects a rule capsule
  6. Logs to JSONL
  7. Executes or blocks the command
"""
from __future__ import annotations

import os
import subprocess
import sys

from openkeel.core.audit import log_event
from openkeel.core.classifier import classify
from openkeel.core.profile import load_profile
from openkeel.core.reinjector import maybe_inject
from openkeel.core.timebox import record_activity


def _get_real_shell() -> str:
    """Get the real shell to execute commands with."""
    # Check for explicit override
    real_shell = os.environ.get("OPENKEEL_REAL_SHELL", "")
    if real_shell:
        return real_shell

    # Platform defaults
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return "/bin/sh"


def main() -> None:
    """Entry point for the proxy shell."""
    # Parse arguments — we only care about -c "command"
    args = sys.argv[1:]

    if not args:
        # Interactive shell requested — pass through to real shell
        real_shell = _get_real_shell()
        os.execvp(real_shell, [real_shell])
        return

    if args[0] != "-c" or len(args) < 2:
        # Not a -c invocation — pass through to real shell
        real_shell = _get_real_shell()
        os.execvp(real_shell, [real_shell] + args)
        return

    command = args[1]

    # Load profile
    profile_name = os.environ.get("OPENKEEL_PROFILE", "")
    if not profile_name:
        # No profile → pass through (openkeel not active)
        _exec_passthrough(command)
        return

    try:
        profile = load_profile(profile_name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[openkeel] WARNING: Could not load profile '{profile_name}': {exc}", file=sys.stderr)
        _exec_passthrough(command)
        return

    # Get session env vars
    session_id = os.environ.get("OPENKEEL_SESSION_ID", "")
    log_dir = os.environ.get("OPENKEEL_LOG_DIR", "")
    log_path = os.path.join(log_dir, "session.jsonl") if log_dir else ""
    state_dir = os.path.join(log_dir, "state") if log_dir else ""

    # 1. Classify
    result = classify(command, profile)

    # 2. Check timebox (if activity matched)
    timebox_action = "allow"
    timebox_message = ""
    if result.activity and state_dir:
        timebox_state = os.path.join(state_dir, "timebox.json")
        timebox_action, timebox_message = record_activity(
            timebox_state, result.activity, command, profile,
        )

    # 3. Maybe inject rule capsule
    injection = None
    if state_dir:
        counter_path = os.path.join(state_dir, "reinjection_counter.json")
        injection = maybe_inject(
            profile, counter_path,
            active_activity=result.activity,
            command_count=0,  # counter is tracked internally
        )

    # 4. Determine final action
    if result.action == "deny":
        # Blocked by classifier
        _handle_blocked(command, result.message, log_path, session_id, result)
        if injection:
            print(injection, file=sys.stderr)
        sys.exit(126)

    if timebox_action == "block":
        # Blocked by timebox
        _handle_blocked(command, timebox_message, log_path, session_id, result)
        if injection:
            print(injection, file=sys.stderr)
        sys.exit(126)

    if timebox_action == "warn":
        print(f"[openkeel] WARNING: {timebox_message}", file=sys.stderr)

    # 5. Log the allowed command
    if log_path:
        log_event(
            log_path=log_path,
            event_type="command_allowed",
            data={
                "command": command,
                "action": result.action,
                "tier": result.tier,
                "activity": result.activity,
                "rule_id": result.rule_id,
                "message": result.message,
            },
            session_id=session_id,
        )

    # 6. Print injection if due
    if injection:
        print(injection, file=sys.stderr)

    # 7. Execute the command
    _exec_passthrough(command)


def _handle_blocked(
    command: str,
    message: str,
    log_path: str,
    session_id: str,
    result,
) -> None:
    """Handle a blocked command — log and print denial."""
    print(f"[openkeel] BLOCKED: {message}", file=sys.stderr)
    print(f"[openkeel] Command: {command}", file=sys.stderr)

    if log_path:
        log_event(
            log_path=log_path,
            event_type="command_blocked",
            data={
                "command": command,
                "action": "deny",
                "tier": result.tier,
                "activity": result.activity,
                "rule_id": result.rule_id,
                "message": message,
            },
            session_id=session_id,
        )


def _exec_passthrough(command: str) -> None:
    """Execute a command via the real shell and exit with its return code."""
    real_shell = _get_real_shell()
    try:
        proc = subprocess.run(
            [real_shell, "-c", command],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        sys.exit(proc.returncode)
    except FileNotFoundError:
        print(f"[openkeel] ERROR: Real shell not found: {real_shell}", file=sys.stderr)
        sys.exit(127)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
