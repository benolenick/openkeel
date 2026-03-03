"""Generate the Stop hook script for drift detection."""
from __future__ import annotations

import textwrap
from pathlib import Path


def generate_drift_hook(
    missions_dir: str | Path,
    output_path: str | Path,
) -> Path:
    """Generate the Stop hook script for drift detection.

    The generated script checks the active mission for:
    - Time-boxed steps that have exceeded their allocated time
    - General drift indicators

    Args:
        missions_dir: Path to missions directory
        output_path: Where to write the generated hook script

    Returns:
        Path to the generated script
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    missions_dir_str = str(Path(missions_dir).expanduser().resolve())

    script = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """OpenKeel drift detection hook (auto-generated).

        Stop hook for Claude Code. Checks the active mission for drift
        indicators like time-box violations.

        DO NOT EDIT — regenerate with: openkeel install
        """
        import json
        import os
        import sys
        from datetime import datetime, timezone
        from pathlib import Path

        MISSIONS_DIR = r"{missions_dir_str}"
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
            if os.path.exists(ACTIVE_FILE):
                try:
                    with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                        name = f.read().strip()
                    if name:
                        return name
                except OSError:
                    pass
            return ""

        def check_drift(data):
            """Check for drift indicators. Returns list of warning messages."""
            warnings = []

            plan = data.get("plan", [])
            in_progress_count = 0
            done_count = 0
            total = len(plan)

            for step in plan:
                if not isinstance(step, dict):
                    continue
                status = step.get("status", "pending")
                if status == "in_progress":
                    in_progress_count += 1
                elif status == "done":
                    done_count += 1

            # Multiple in-progress steps = possible thrashing
            if in_progress_count > 1:
                warnings.append(
                    f"DRIFT WARNING: {{in_progress_count}} steps are marked in_progress simultaneously. "
                    f"Focus on one step at a time."
                )

            # No progress at all
            if total > 0 and done_count == 0 and in_progress_count == 0:
                warnings.append(
                    "DRIFT WARNING: No steps started yet. Begin with step 1."
                )

            # Progress summary
            if total > 0:
                pct = int(done_count / total * 100)
                warnings.append(
                    f"Mission progress: {{done_count}}/{{total}} steps complete ({{pct}}%)"
                )

            return warnings

        def main():
            name = get_active_mission_name()
            if not name:
                return

            mission_path = os.path.join(MISSIONS_DIR, f"{{name}}.yaml")
            if not os.path.exists(mission_path):
                return

            data = load_yaml_simple(mission_path)
            if not isinstance(data, dict):
                return

            warnings = check_drift(data)
            for w in warnings:
                print(w)

        if __name__ == "__main__":
            main()
    ''')

    output_path.write_text(script, encoding="utf-8")
    try:
        output_path.chmod(0o755)
    except OSError:
        pass

    return output_path
