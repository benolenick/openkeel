"""Generate the SessionStart hook script for mission context injection."""
from __future__ import annotations

import textwrap
from pathlib import Path


def generate_inject_hook(
    missions_dir: str | Path,
    active_mission: str,
    output_path: str | Path,
) -> Path:
    """Generate the SessionStart injection hook script.

    The generated script reads the active mission file and outputs
    formatted mission state to stdout, which Claude Code injects
    into the agent's context.

    Args:
        missions_dir: Path to missions directory
        active_mission: Name of active mission (without .yaml extension)
        output_path: Where to write the generated hook script

    Returns:
        Path to the generated script
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    missions_dir_str = str(Path(missions_dir).expanduser().resolve())

    script = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """OpenKeel mission context injection hook (auto-generated).

        SessionStart hook for Claude Code. Reads the active mission file
        and outputs formatted mission state to stdout for context injection.

        DO NOT EDIT — regenerate with: openkeel install
        """
        import json
        import os
        import sys
        from pathlib import Path

        MISSIONS_DIR = r"{missions_dir_str}"
        ACTIVE_MISSION = r"{active_mission}"

        # Also check a "pointer" file that stores which mission is active
        # This allows `openkeel mission start` to change the active mission
        # without re-running `openkeel install`
        ACTIVE_FILE = os.path.join(os.path.expanduser("~"), ".openkeel", "active_mission.txt")

        def load_yaml_simple(path):
            """Load YAML, falling back to JSON if PyYAML unavailable."""
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f)
            except ImportError:
                pass
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass
            return None

        def get_active_mission_name():
            """Determine the active mission name."""
            # Check pointer file first (allows dynamic switching)
            if os.path.exists(ACTIVE_FILE):
                try:
                    with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                        name = f.read().strip()
                    if name:
                        return name
                except OSError:
                    pass
            return ACTIVE_MISSION

        def format_mission(data):
            """Format mission data for injection."""
            lines = [
                "=" * 60,
                "OPENKEEL MISSION STATE (auto-injected, do not ignore)",
                "=" * 60,
                f"OBJECTIVE: {{data.get('objective', 'No objective set')}}",
            ]

            plan = data.get("plan", [])
            if plan:
                lines.append("PLAN:")
                for step in plan:
                    if not isinstance(step, dict):
                        continue
                    status = step.get("status", "pending")
                    if status == "done":
                        marker = "[x]"
                    elif status == "in_progress":
                        marker = "[>]"
                    elif status == "skipped":
                        marker = "[-]"
                    else:
                        marker = "[ ]"
                    tb = ""
                    tbm = step.get("time_box_minutes", 0)
                    if tbm:
                        tb = f" (time-box: {{tbm}}min)"
                    lines.append(f"  {{marker}} {{step.get('id', '?')}}. {{step.get('step', '')}}" + tb)

            findings = data.get("findings", [])
            if findings:
                lines.append("KEY FINDINGS:")
                for f in findings:
                    lines.append(f"  - {{f}}")

            credentials = data.get("credentials", [])
            if credentials:
                lines.append("CREDENTIALS:")
                for c in credentials:
                    lines.append(f"  - {{c}}")

            notes = data.get("notes", "")
            if notes:
                lines.append(f"NOTES: {{notes}}")

            tags = data.get("tags", [])
            if tags:
                lines.append(f"TAGS: {{', '.join(tags)}}")

            lines.append("=" * 60)
            return "\\n".join(lines)

        def main():
            name = get_active_mission_name()
            if not name:
                return  # No active mission, output nothing

            # Try with and without .yaml extension
            mission_path = os.path.join(MISSIONS_DIR, f"{{name}}.yaml")
            if not os.path.exists(mission_path):
                mission_path = os.path.join(MISSIONS_DIR, name)
            if not os.path.exists(mission_path):
                return

            data = load_yaml_simple(mission_path)
            if not isinstance(data, dict):
                return

            print(format_mission(data))

        if __name__ == "__main__":
            main()
    ''')

    output_path.write_text(script, encoding="utf-8")
    try:
        output_path.chmod(0o755)
    except OSError:
        pass

    return output_path
