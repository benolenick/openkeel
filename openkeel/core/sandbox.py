"""Host sandbox via systemd-run (Linux only).

Wraps the agent subprocess in a transient systemd scope unit with
resource limits, filesystem restrictions, and network isolation.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from .profile import SandboxConfig

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """Check if systemd-run is available on this system."""
    return shutil.which("systemd-run") is not None


def build_systemd_run_args(
    config: SandboxConfig,
    unit_name: str = "openkeel-session",
) -> list[str]:
    """Build systemd-run command-line arguments from sandbox config.

    Returns a list of args to prepend to the agent command.
    The caller should do: subprocess.Popen(args + agent_command, ...)
    """
    if not config.enabled:
        return []

    args = [
        "systemd-run",
        "--scope",
        f"--unit={unit_name}",
        "--user",
    ]

    if config.memory_max:
        args.append(f"--property=MemoryMax={config.memory_max}")

    if config.cpu_quota:
        args.append(f"--property=CPUQuota={config.cpu_quota}")

    if config.readonly_paths:
        for path in config.readonly_paths:
            args.append(f"--property=ReadOnlyPaths={path}")

    if config.inaccessible_paths:
        for path in config.inaccessible_paths:
            args.append(f"--property=InaccessiblePaths={path}")

    # Separator
    args.append("--")

    return args


def setup_network_restrictions(
    config: SandboxConfig,
    session_id: str,
) -> list[str]:
    """Set up iptables rules to restrict network access.

    Returns a list of teardown commands to run at session end.
    Only applies network_deny rules.
    """
    if not config.enabled or not config.network_deny:
        return []

    teardown_commands: list[str] = []
    chain_name = f"OPENKEEL_{session_id[:8].upper()}"

    # Create a custom iptables chain
    setup_cmds = [
        f"iptables -N {chain_name}",
        f"iptables -A OUTPUT -j {chain_name}",
    ]

    for cidr in config.network_deny:
        setup_cmds.append(f"iptables -A {chain_name} -d {cidr} -j DROP")
        teardown_commands.append(f"iptables -D {chain_name} -d {cidr} -j DROP")

    teardown_commands.extend([
        f"iptables -D OUTPUT -j {chain_name}",
        f"iptables -X {chain_name}",
    ])

    # Execute setup (requires root)
    for cmd in setup_cmds:
        try:
            subprocess.run(
                cmd.split(),
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Failed to set up network restriction '%s': %s", cmd, exc)
            # Try to tear down what we've set up so far
            teardown_network_restrictions(teardown_commands)
            return []

    return teardown_commands


def teardown_network_restrictions(teardown_commands: list[str]) -> None:
    """Remove iptables rules set up by setup_network_restrictions."""
    for cmd in teardown_commands:
        try:
            subprocess.run(
                cmd.split(),
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Failed to tear down network restriction '%s': %s", cmd, exc)
