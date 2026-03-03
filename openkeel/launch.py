"""Interactive launcher — the main way to start an OpenKeel-governed agent session.

Usage:
    openkeel                     # interactive: pick agent + project
    openkeel launch              # same
    openkeel launch claude       # explicit agent, infer project from cwd
    openkeel launch claude myproj # explicit agent + project

Flow:
    1. Detect installed agents (claude, gemini, codex)
    2. Detect project from cwd (git repo name or dir name)
    3. Load recent facts from memory.db
    4. Inject context block into CLAUDE.md (managed section)
    5. Launch agent subprocess
    6. On exit: update memory, clean up context block
    7. On crash: leave context block (agent restart picks it up)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
# Project detection
# ---------------------------------------------------------------------------

def detect_project(cwd: str | None = None) -> tuple[str, str]:
    """Detect project name and root dir from cwd.

    Returns (project_name, project_dir).
    Tries git repo name first, falls back to directory name.
    """
    cwd = cwd or os.getcwd()

    # Try git repo name
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode == 0:
            repo_dir = result.stdout.strip()
            name = Path(repo_dir).name
            return name, repo_dir
    except Exception:
        pass

    # Fall back to directory name
    return Path(cwd).name, cwd


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
# Interactive prompt
# ---------------------------------------------------------------------------

def _pick_option(prompt: str, options: list[str], default: int = 0) -> int:
    """Simple terminal picker. Returns index of chosen option."""
    print(prompt)
    for i, opt in enumerate(options):
        marker = ">" if i == default else " "
        print(f"  {marker} [{i + 1}] {opt}")
    print()

    while True:
        try:
            raw = input(f"Choice [{default + 1}]: ").strip()
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
# Main launcher
# ---------------------------------------------------------------------------

def launch(agent_name: str = "", project: str = "") -> None:
    """Interactive launcher entry point."""
    from openkeel.integrations.local_memory import LocalMemory

    # 1. Detect agents
    agents = detect_agents()
    if not agents:
        print("No agents found. Install claude, gemini, or codex.")
        sys.exit(1)

    # 2. Pick agent
    agent_names = list(agents.keys())
    if agent_name and agent_name in agents:
        chosen_agent = agent_name
    elif agent_name:
        print(f"Agent '{agent_name}' not found. Available: {', '.join(agent_names)}")
        sys.exit(1)
    elif len(agent_names) == 1:
        chosen_agent = agent_names[0]
    else:
        idx = _pick_option("Agent:", agent_names)
        chosen_agent = agent_names[idx]

    agent_path = agents[chosen_agent]

    # 3. Detect / pick project
    detected_name, detected_dir = detect_project()

    mem = LocalMemory()
    stats = mem.stats()
    existing_projects = list(stats.get("projects", {}).keys())

    if project:
        project_name = project
        project_dir = detected_dir
    elif detected_name and detected_name != os.path.basename(os.path.expanduser("~")):
        # Use detected project, but let user confirm/override
        project_name = detected_name
        project_dir = detected_dir
    else:
        project_name = detected_name or "default"
        project_dir = detected_dir

    # 4. Load context from memory
    recent_facts = mem.recent(limit=10, project=project_name)
    all_recent = mem.recent(limit=5) if not recent_facts else []

    # 5. Show summary
    print()
    print(f"  Agent:    {chosen_agent} ({agent_path})")
    print(f"  Project:  {project_name}")
    print(f"  Dir:      {project_dir}")
    print(f"  Memory:   {stats['total_facts']} facts ({len(recent_facts)} for this project)")
    print()

    # 6. Inject context into CLAUDE.md
    facts_for_context = recent_facts or all_recent
    claude_md = inject_context(project_dir, project_name, facts_for_context)
    print(f"  Context injected into {claude_md}")
    print()

    # 7. Launch agent
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
        # Clean exit — remove context block
        remove_context(project_dir)
        print(f"[openkeel] Context cleaned from CLAUDE.md")
    else:
        # Crash — leave context block so restart picks it up
        print(f"[openkeel] Agent exited with code {exit_code}.")
        print(f"[openkeel] Context left in CLAUDE.md for restart.")

        # Offer to relaunch
        try:
            raw = input("\n  Relaunch? [Y/n]: ").strip().lower()
            if raw in ("", "y", "yes"):
                # Refresh context before relaunch
                recent_facts = mem.recent(limit=10, project=project_name)
                inject_context(project_dir, project_name, recent_facts)
                print(f"  Context refreshed. Relaunching {chosen_agent}...\n")
                mem.close()
                # Recursive relaunch
                launch(chosen_agent, project_name)
                return
        except (EOFError, KeyboardInterrupt):
            pass

    mem.close()
    sys.exit(exit_code)
