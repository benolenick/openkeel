"""Claude Code adapter — wire OpenKeel hooks into .claude/settings.json."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def get_settings_path() -> Path:
    """Get the path to Claude Code's settings.json."""
    return Path.home() / ".claude" / "settings.json"


def load_settings() -> dict[str, Any]:
    """Load Claude Code settings. Returns empty dict if not found."""
    path = get_settings_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    """Save Claude Code settings atomically."""
    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")

    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        try:
            os.replace(tmp_path, path)
        except OSError:
            tmp_path.rename(path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _python_cmd() -> str:
    """Get the Python command to use in hooks."""
    return sys.executable


def _make_hook_entry(command: str) -> dict[str, Any]:
    """Create a hook entry dict for Claude Code settings."""
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": command}],
    }


def _is_openkeel_hook(entry: dict) -> bool:
    """Check if a hook entry belongs to openkeel."""
    hooks = entry.get("hooks", [])
    for h in hooks:
        if isinstance(h, str) and "openkeel" in h:
            return True
        if isinstance(h, dict) and "openkeel" in h.get("command", ""):
            return True
    return False


def install_hooks(
    enforce_script: str | Path,
    inject_script: str | Path,
    drift_script: str | Path,
) -> Path:
    """Install OpenKeel hooks into Claude Code settings.

    Adds PreToolUse, SessionStart, and Stop hooks. Preserves
    existing non-openkeel hooks.

    Args:
        enforce_script: Path to the constitution enforcement hook script
        inject_script: Path to the mission injection hook script
        drift_script: Path to the drift detection hook script

    Returns:
        Path to the settings.json file
    """
    python = _python_cmd()
    settings = load_settings()

    if "hooks" not in settings:
        settings["hooks"] = {}
    hooks = settings["hooks"]

    # Define our hook commands
    hook_map = {
        "PreToolUse": f'{python} "{enforce_script}"',
        "SessionStart": f'{python} "{inject_script}"',
        "Stop": f'{python} "{drift_script}"',
    }

    for event, command in hook_map.items():
        existing = hooks.get(event, [])
        # Remove any existing openkeel hooks
        filtered = [e for e in existing if isinstance(e, dict) and not _is_openkeel_hook(e)]
        # Add our hook
        filtered.append(_make_hook_entry(command))
        hooks[event] = filtered

    save_settings(settings)
    return get_settings_path()


def uninstall_hooks() -> bool:
    """Remove OpenKeel hooks from Claude Code settings.

    Returns True if hooks were found and removed.
    """
    settings = load_settings()
    hooks = settings.get("hooks", {})
    changed = False

    for event in ("PreToolUse", "SessionStart", "Stop"):
        entries = hooks.get(event, [])
        filtered = [e for e in entries if isinstance(e, dict) and not _is_openkeel_hook(e)]
        if len(filtered) != len(entries):
            changed = True
            if filtered:
                hooks[event] = filtered
            else:
                hooks.pop(event, None)

    if changed:
        # Clean up empty hooks dict
        if not hooks:
            settings.pop("hooks", None)
        save_settings(settings)

    return changed
