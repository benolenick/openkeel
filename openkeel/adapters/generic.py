"""Generic adapter for non-Claude AI agents.

For agents that don't support hooks (Codex CLI, Gemini CLI), this provides
a wrapper script approach: the wrapper intercepts commands and applies
constitution rules before passing them to the real agent.

This is a placeholder for future implementation.
"""
from __future__ import annotations

from pathlib import Path


def generate_wrapper(
    agent_command: str,
    enforce_script: str | Path,
    output_path: str | Path,
) -> Path:
    """Generate a wrapper script for a non-Claude agent.

    The wrapper reads commands from stdin, checks them against the
    constitution, and only passes allowed commands to the real agent.

    NOTE: This is a simplified wrapper. Full interception requires
    agent-specific integration.

    Args:
        agent_command: The real agent command to wrap (e.g. "codexreal.cmd")
        enforce_script: Path to the enforcement script
        output_path: Where to write the wrapper script

    Returns:
        Path to the generated wrapper script
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # For now, generate a simple pass-through wrapper with a comment
    # about where enforcement would be added
    script = f"""#!/usr/bin/env bash
# OpenKeel wrapper for {agent_command} (auto-generated)
# TODO: Add pre-command enforcement via {enforce_script}
# For now, this is a pass-through wrapper.
exec {agent_command} "$@"
"""

    output_path.write_text(script, encoding="utf-8")
    try:
        output_path.chmod(0o755)
    except OSError:
        pass

    return output_path
