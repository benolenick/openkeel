"""Interactive launcher — the main way to start an OpenKeel-governed agent session.

Usage:
    openkeel                     # interactive: pick agent + profile
    openkeel launch              # same
    openkeel launch claude       # explicit agent, pick profile interactively

Flow:
    1. Detect installed agents (claude, gemini, codex)
    2. Pick a profile (sorted by most recently used, shows fact counts)
    3. Load recent facts from memory.db (scoped to profile name)
    4. Inject context block into CLAUDE.md (managed section)
    5. Launch agent in the profile's work_dir
    6. On exit: update memory, clean up context block
    7. On crash: leave context block (agent restart picks it up)
"""
from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Platform-specific imports for raw key reading
_WINDOWS = sys.platform == "win32"
if _WINDOWS:
    import ctypes
    import msvcrt
else:
    import tty
    import termios

# Marker for the managed section in CLAUDE.md
_CONTEXT_START = "<!-- OPENKEEL:START -->"
_CONTEXT_END = "<!-- OPENKEEL:END -->"


# ---------------------------------------------------------------------------
# Agent detection
# ---------------------------------------------------------------------------

_AGENT_NAMES = {
    "claude": ["claude", "claudereal"],
    "gemini": ["gemini", "geminireal"],
    "codex": ["codex", "codexreal"],
}


def detect_agents() -> dict[str, str]:
    """Return {name: path} for each installed agent CLI."""
    found = {}
    for name, binaries in _AGENT_NAMES.items():
        for binary in binaries:
            path = shutil.which(binary)
            if path:
                found[name] = path
                break
    return found


# ---------------------------------------------------------------------------
# Usage tracking — sort profiles by most recently used
# ---------------------------------------------------------------------------

_USAGE_FILE = Path.home() / ".openkeel" / "profile_usage.json"


def _load_usage() -> dict[str, str]:
    """Return {profile_name: ISO timestamp} from the usage file."""
    if _USAGE_FILE.exists():
        try:
            return _json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _record_usage(profile_name: str) -> None:
    """Record that a profile was just used (now)."""
    usage = _load_usage()
    usage[profile_name] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USAGE_FILE.write_text(_json.dumps(usage, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Context injection into CLAUDE.md
# ---------------------------------------------------------------------------

def _build_context_block(project: str, facts: list[dict]) -> str:
    """Build the managed context block for CLAUDE.md."""
    lines = [
        _CONTEXT_START,
        "",
        "## OpenKeel Session Context",
        "",
        f"**Project:** {project}",
        f"**Session started:** {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if facts:
        lines.append("**Recent memory:**")
        for f in facts[:10]:
            tag = f" #{f['tag']}" if f.get('tag') else ""
            lines.append(f"- {f['text'][:150]}{tag}")
        lines.append("")

    lines.append(
        "**Instructions:** If you lose track of what you're doing, "
        "run `openkeel recall \"<topic>\"` to search project memory. "
        "When you make a decision or discover something important, "
        "run `openkeel remember \"<fact>\" -p " + project + "` to save it."
    )
    lines.append("")
    lines.append(_CONTEXT_END)
    return "\n".join(lines)


def inject_context(project_dir: str, project: str, facts: list[dict]) -> Path:
    """Inject/replace the managed context block in CLAUDE.md.

    Creates CLAUDE.md if it doesn't exist.
    Returns the path to the CLAUDE.md file.
    """
    claude_md = Path(project_dir) / "CLAUDE.md"
    block = _build_context_block(project, facts)

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        # Replace existing block
        pattern = re.compile(
            re.escape(_CONTEXT_START) + r".*?" + re.escape(_CONTEXT_END),
            re.DOTALL,
        )
        if pattern.search(content):
            new_content = pattern.sub(block, content)
        else:
            # Append block at the end
            new_content = content.rstrip() + "\n\n" + block + "\n"
        claude_md.write_text(new_content, encoding="utf-8")
    else:
        claude_md.write_text(block + "\n", encoding="utf-8")

    return claude_md


def remove_context(project_dir: str) -> None:
    """Remove the managed context block from CLAUDE.md."""
    claude_md = Path(project_dir) / "CLAUDE.md"
    if not claude_md.exists():
        return

    content = claude_md.read_text(encoding="utf-8")
    pattern = re.compile(
        r"\n*" + re.escape(_CONTEXT_START) + r".*?" + re.escape(_CONTEXT_END) + r"\n*",
        re.DOTALL,
    )
    new_content = pattern.sub("", content).strip()

    if new_content:
        claude_md.write_text(new_content + "\n", encoding="utf-8")
    else:
        # CLAUDE.md was only our block — remove the file
        claude_md.unlink()


# ---------------------------------------------------------------------------
# Raw key input (cross-platform)
# ---------------------------------------------------------------------------

def _supports_raw_input() -> bool:
    """Check if the terminal supports raw keypress reading."""
    if _WINDOWS:
        try:
            handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            return bool(ok)
        except Exception:
            return False
    else:
        try:
            termios.tcgetattr(sys.stdin.fileno())
            return sys.stdin.isatty()
        except Exception:
            return False


def _read_key() -> str:
    """Read a single keypress. Returns 'up', 'down', 'enter', or the char."""
    if _WINDOWS:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\xe0":  # arrow key prefix
            ch2 = msvcrt.getwch()
            if ch2 == "H":
                return "up"
            if ch2 == "P":
                return "down"
            return ""
        return ch
    else:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                return "enter"
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    if ch3 == "B":
                        return "down"
                return ""
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------

def _pick_interactive(prompt: str, options: list[str], default: int = 0) -> int:
    """Interactive picker with arrow keys or numbered fallback.

    Returns index of chosen option.
    """
    if _supports_raw_input():
        return _pick_arrow(prompt, options, default)
    return _pick_numbered(prompt, options, default)


def _pick_arrow(prompt: str, options: list[str], default: int = 0) -> int:
    """Arrow-key picker with in-place redraw."""
    sel = default
    n = len(options)
    # hint line adds 1 extra line
    total_lines = n + 1

    def render(first: bool = False):
        # Move cursor up to overwrite previous render (skip on first draw)
        if not first:
            sys.stdout.write(f"\033[{total_lines}A")
        for i, opt in enumerate(options):
            marker = ">" if i == sel else " "
            # Clear line before writing
            sys.stdout.write(f"\033[2K  {marker} {opt}\n")
        sys.stdout.write("\033[2K  \033[90m↑↓ navigate · enter select\033[0m\n")
        sys.stdout.flush()

    print(f"  {prompt}")
    render(first=True)

    while True:
        key = _read_key()
        if key == "up":
            sel = (sel - 1) % n
            render()
        elif key == "down":
            sel = (sel + 1) % n
            render()
        elif key == "enter":
            # Overwrite hint line with blank
            sys.stdout.write(f"\033[1A\033[2K")
            sys.stdout.flush()
            return sel


def _pick_numbered(prompt: str, options: list[str], default: int = 0) -> int:
    """Numbered fallback picker (works in any terminal)."""
    print(f"  {prompt}")
    for i, opt in enumerate(options):
        marker = ">" if i == default else " "
        print(f"  {marker} [{i + 1}] {opt}")
    print()

    while True:
        try:
            raw = input(f"  Choice [{default + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if not raw:
            return default
        try:
            choice = int(raw) - 1
            if 0 <= choice < len(options):
                return choice
        except ValueError:
            pass
        print(f"  Enter 1-{len(options)}")


# ---------------------------------------------------------------------------
# Profile picker (sorted by most recently used)
# ---------------------------------------------------------------------------

def _pick_profile(
    available: list[str],
    known_facts: dict[str, int],
) -> str:
    """Interactive profile picker sorted by most recently used.

    Args:
        available: profile names from list_profiles().
        known_facts: {profile_name: fact_count} from memory.db stats.

    Returns the chosen profile name.
    """
    usage = _load_usage()

    # Sort: recently used first, then alphabetical for never-used
    def sort_key(name: str) -> tuple[int, str]:
        ts = usage.get(name, "")
        # used profiles sort before unused; within used, reverse chrono
        return (0 if ts else 1, "" if not ts else chr(0) + ts[::-1], name)

    ordered = sorted(available, key=lambda n: (0 if usage.get(n) else 1, usage.get(n, ""), n))
    # Reverse the "used" portion so most recent is first
    used = [n for n in ordered if usage.get(n)]
    unused = [n for n in ordered if not usage.get(n)]
    used.sort(key=lambda n: usage[n], reverse=True)
    ordered = used + unused

    options = []
    profile_keys: list[str | None] = []

    for name in ordered:
        count = known_facts.get(name, 0)
        extends_info = ""
        try:
            from openkeel.core.profile import _resolve_path, _load_raw_yaml
            raw = _load_raw_yaml(_resolve_path(name))
            if raw.get("extends"):
                extends_info = raw["extends"]
        except Exception:
            pass

        suffix_parts = []
        if count:
            suffix_parts.append(f"{count} facts")
        if extends_info:
            suffix_parts.append(extends_info)

        suffix = f" \u2014 {', '.join(suffix_parts)}" if suffix_parts else ""
        options.append(f"{name}{suffix}")
        profile_keys.append(name)

    # New profile option at bottom
    options.append("+ New profile\u2026")
    profile_keys.append(None)

    if len(options) == 1:
        return _prompt_new_profile()

    idx = _pick_interactive("Profile:", options, default=0)

    if profile_keys[idx] is None:
        return _prompt_new_profile()
    return profile_keys[idx]


def _prompt_new_profile() -> str:
    """Prompt user to create a minimal new profile YAML."""
    from openkeel.core.profile import list_profiles

    while True:
        try:
            name = input("  Profile name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if not name:
            print("  Name cannot be empty.")
            continue

        # Ask for optional base profile
        bases = list_profiles()
        try:
            if bases:
                print(f"  Available bases: {', '.join(bases)}")
            extends = input("  Extends (base profile, or blank): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        # Ask for working directory
        try:
            work_dir = input("  Work directory (or blank for current dir): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        # Write minimal YAML
        profile_dir = Path.home() / ".openkeel" / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / f"{name}.yaml"

        lines = [f"name: {name}"]
        if extends:
            lines.append(f"extends: {extends}")
        if work_dir:
            lines.append(f"work_dir: \"{work_dir}\"")
        lines.append(f'description: ""')
        lines.append("")
        profile_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  Created {profile_path}")
        return name


# ---------------------------------------------------------------------------
# Main launcher
# ---------------------------------------------------------------------------

def launch(agent_name: str = "") -> None:
    """Interactive launcher entry point.

    Run from anywhere — the profile's work_dir determines where the agent runs.
    """
    from openkeel.integrations.local_memory import LocalMemory
    from openkeel.core.profile import list_profiles, load_profile

    # 1. Detect agents
    agents = detect_agents()
    if not agents:
        print("No agents found. Install claude, gemini, or codex.")
        sys.exit(1)

    # 2. Pick agent (skip picker if only one)
    agent_names = list(agents.keys())
    if agent_name and agent_name in agents:
        chosen_agent = agent_name
    elif agent_name:
        print(f"Agent '{agent_name}' not found. Available: {', '.join(agent_names)}")
        sys.exit(1)
    elif len(agent_names) == 1:
        chosen_agent = agent_names[0]
    else:
        idx = _pick_interactive("Agent:", agent_names)
        chosen_agent = agent_names[idx]

    agent_path = agents[chosen_agent]

    # 3. Pick profile (sorted by most recently used)
    mem = LocalMemory()
    stats = mem.stats()
    known_facts = stats.get("projects", {})

    available = list_profiles()

    print()
    profile_name = _pick_profile(available, dict(known_facts))

    # Load full profile to get work_dir
    profile = load_profile(profile_name)
    project_dir = profile.work_dir or os.getcwd()

    # Record usage so this profile sorts to top next time
    _record_usage(profile_name)

    # Memory scoped to profile name
    project_name = profile_name

    # 4. Load context from memory
    recent_facts = mem.recent(limit=10, project=project_name)
    all_recent = mem.recent(limit=5) if not recent_facts else []

    # 5. Show summary
    print()
    print(f"  Agent:    {chosen_agent} ({agent_path})")
    print(f"  Profile:  {profile_name}")
    print(f"  Dir:      {project_dir}")
    print(f"  Memory:   {stats['total_facts']} facts ({len(recent_facts)} for this profile)")
    print()

    # 6. Inject context into CLAUDE.md
    facts_for_context = recent_facts or all_recent
    claude_md = inject_context(project_dir, project_name, facts_for_context)
    print(f"  Context injected into {claude_md}")
    print()

    # 7. Launch agent in the profile's work_dir
    exit_code = 0
    clean_exit = False
    try:
        proc = subprocess.Popen(
            [agent_path],
            cwd=project_dir,
        )
        exit_code = proc.wait()
        clean_exit = exit_code == 0
    except KeyboardInterrupt:
        print("\n[openkeel] Session interrupted.")
        exit_code = 130
        clean_exit = True  # User intentionally stopped
    except FileNotFoundError:
        print(f"ERROR: Agent not found: {agent_path}")
        exit_code = 127

    # 8. Post-session
    if clean_exit:
        remove_context(project_dir)
        print(f"[openkeel] Context cleaned from CLAUDE.md")
    else:
        print(f"[openkeel] Agent exited with code {exit_code}.")
        print(f"[openkeel] Context left in CLAUDE.md for restart.")

        # Offer to relaunch
        try:
            raw = input("\n  Relaunch? [Y/n]: ").strip().lower()
            if raw in ("", "y", "yes"):
                recent_facts = mem.recent(limit=10, project=project_name)
                inject_context(project_dir, project_name, recent_facts)
                print(f"  Context refreshed. Relaunching {chosen_agent}...\n")
                mem.close()
                launch(chosen_agent)
                return
        except (EOFError, KeyboardInterrupt):
            pass

    mem.close()
    sys.exit(exit_code)
