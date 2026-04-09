"""Session history — automatic story.txt-style logging per profile.

Each profile gets an append-only history file at:
    ~/.openkeel/history/<profile>.txt

Entries are structured like the agent history story.txt format:
    [timestamp]
    Session: <session_id>
    Profile: <name> | Mode: <mode> | Mission: <mission>
    Duration: <minutes>
    Commands: <total> (blocked: N, gated: N, allowed: N)
    Activity:
    - <command summaries>
    Terminal excerpt:
    - <last ~50 meaningful lines of terminal output>
    ---

On session start, the last entry is read and included in context injection
so the agent immediately knows what happened previously.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

HISTORY_DIR = Path.home() / ".openkeel" / "history"
MAX_EXCERPT_LINES = 50
MAX_ENTRY_COMMANDS = 30


def _sanitize_profile_name(name: str) -> str:
    """Make profile name safe for filenames."""
    return re.sub(r'[^\w\-]', '_', name) if name else "default"


def write_session_entry(
    profile: str,
    *,
    session_id: str = "",
    mode: str = "Normal",
    mission: str = "",
    start_time: float = 0,
    history_entries: list[dict] | None = None,
    terminal_lines: list[str] | None = None,
) -> Path | None:
    """Append a session summary entry to the profile's history file.

    Args:
        profile: Profile name (used as filename).
        session_id: Unique session identifier.
        mode: Active governance mode.
        mission: Active mission name.
        start_time: Session start epoch time.
        history_entries: List of governance decisions [{time, action, tier, command}].
        terminal_lines: Last N lines of terminal output (stripped of ANSI).
    """
    if not profile:
        return None

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_profile_name(profile)
    history_file = HISTORY_DIR / f"{safe_name}.txt"

    entries = history_entries or []
    lines = terminal_lines or []

    # Calculate stats
    blocked = sum(1 for e in entries if e.get("action") == "deny")
    gated = sum(1 for e in entries if e.get("action") == "gate")
    allowed = sum(1 for e in entries if e.get("action") == "allow")
    total = blocked + gated + allowed

    duration_min = 0
    if start_time > 0:
        duration_min = int((time.time() - start_time) / 60)

    # Build the entry
    now = time.strftime("%Y-%m-%d %H:%M")
    parts = [
        f"[{now}]",
        f"Session: {session_id or 'unknown'}",
        f"Profile: {profile} | Mode: {mode} | Mission: {mission or 'none'}",
        f"Duration: {duration_min}min",
        f"Commands: {total} (blocked: {blocked}, gated: {gated}, allowed: {allowed})",
    ]

    # Command activity summary (deduplicated, limited)
    if entries:
        parts.append("Activity:")
        seen_cmds = set()
        cmd_lines = []
        for e in entries:
            cmd = e.get("command", "").strip()
            action = e.get("action", "")
            if not cmd:
                continue
            # Normalize for dedup (first 60 chars)
            key = cmd[:60]
            if key in seen_cmds:
                continue
            seen_cmds.add(key)
            marker = {"deny": "BLOCKED", "gate": "GATED", "allow": ""}.get(action, "")
            prefix = f"[{marker}] " if marker else "- "
            cmd_lines.append(f"  {prefix}{cmd[:100]}")
            if len(cmd_lines) >= MAX_ENTRY_COMMANDS:
                break
        parts.extend(cmd_lines)

    # Terminal excerpt — last meaningful lines
    if lines:
        # Filter out empty lines and pure ANSI noise
        meaningful = []
        for line in lines:
            stripped = _strip_ansi(line).strip()
            if stripped and len(stripped) > 2:
                meaningful.append(stripped)
        if meaningful:
            excerpt = meaningful[-MAX_EXCERPT_LINES:]
            parts.append("Terminal excerpt:")
            for l in excerpt:
                parts.append(f"  {l[:200]}")

    parts.append("---\n")

    entry = "\n".join(parts) + "\n"

    # Append to file
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(entry)

    return history_file


def read_last_session(profile: str, max_lines: int = 40) -> str:
    """Read the last session entry from a profile's history file.

    Returns the formatted text, or empty string if no history.
    """
    if not profile:
        return ""

    safe_name = _sanitize_profile_name(profile)
    history_file = HISTORY_DIR / f"{safe_name}.txt"

    if not history_file.exists():
        return ""

    try:
        content = history_file.read_text(encoding="utf-8")
    except Exception:
        return ""

    # Split into entries by the "---" separator
    entries = content.split("\n---\n")
    entries = [e.strip() for e in entries if e.strip()]
    if not entries:
        return ""

    last = entries[-1]
    # Limit to max_lines
    lines = last.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append("  ... (truncated)")
    return "\n".join(lines)


def get_terminal_scrollback(terminal_widget) -> list[str]:
    """Extract text lines from a TerminalWidget's pyte screen buffer.

    Returns a list of strings (one per line), newest last.
    """
    screen = terminal_widget._screen
    if not screen:
        return []

    lines = []

    # History lines (scrolled off the top)
    history = terminal_widget._get_history_lines()
    for line_dict in history:
        chars = []
        for col in range(screen.columns):
            char = line_dict.get(col)
            chars.append(char.data if char else " ")
        lines.append("".join(chars).rstrip())

    # Current screen buffer
    for row in range(screen.lines):
        line = screen.buffer[row]
        chars = []
        for col in range(screen.columns):
            chars.append(line[col].data)
        lines.append("".join(chars).rstrip())

    return lines


# Simple ANSI escape stripper
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]|\x1b[>=<]')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
