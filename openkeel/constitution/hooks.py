"""Generate self-contained hook scripts for constitution enforcement."""
from __future__ import annotations

import textwrap
from pathlib import Path


def generate_enforce_hook(
    constitution_path: str | Path,
    mission_dir: str | Path,
    active_mission: str,
    log_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Generate the PreToolUse enforcement hook script.

    The generated script is completely self-contained — no openkeel imports.
    It reads the constitution YAML, evaluates rules, and outputs a decision.

    Args:
        constitution_path: Path to constitution.yaml
        mission_dir: Path to missions directory (for reading active mission tags)
        active_mission: Name of active mission file (e.g. "my-mission.yaml")
        log_path: Path to enforcement log file
        output_path: Where to write the generated hook script

    Returns:
        Path to the generated script
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert paths to strings for embedding
    const_path_str = str(Path(constitution_path).expanduser().resolve())
    mission_dir_str = str(Path(mission_dir).expanduser().resolve())
    log_path_str = str(Path(log_path).expanduser().resolve())

    # Self-protection rules that are always active (hardcoded, not in YAML)
    # These prevent the agent from editing openkeel files
    self_protect_patterns = [
        r"openkeel",
        r"\.openkeel",
        r"openkeel_enforce",
        r"openkeel_inject",
        r"openkeel_drift",
        r"constitution\.yaml",
    ]
    self_protect_json = repr(self_protect_patterns)

    script = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """OpenKeel constitution enforcement hook (auto-generated).

        PreToolUse hook for Claude Code. Reads rules from constitution.yaml,
        evaluates them against the tool call, and outputs allow/block decision.

        DO NOT EDIT — regenerate with: openkeel install
        """
        import json
        import os
        import re
        import sys
        from datetime import datetime, timezone
        from pathlib import Path

        CONSTITUTION_PATH = r"{const_path_str}"
        MISSION_DIR = r"{mission_dir_str}"
        ACTIVE_MISSION = r"{active_mission}"
        LOG_PATH = r"{log_path_str}"
        SELF_PROTECT_PATTERNS = {self_protect_json}

        def load_yaml_simple(path):
            """Minimal YAML parser for constitution rules.

            Handles the subset of YAML used by constitution files:
            - Top-level mapping
            - Lists of mappings
            - Nested mappings (match field)
            - String values (quoted and unquoted)
            - Lists of strings

            Falls back gracefully if the file can\'t be parsed.
            """
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f)
            except ImportError:
                pass

            # Fallback: try json (in case someone uses JSON format)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass

            return None

        def get_active_tags():
            """Read tags from the active mission file."""
            if not ACTIVE_MISSION:
                return []
            mission_path = os.path.join(MISSION_DIR, ACTIVE_MISSION)
            if not os.path.exists(mission_path):
                return []
            data = load_yaml_simple(mission_path)
            if isinstance(data, dict):
                tags = data.get("tags", [])
                return tags if isinstance(tags, list) else []
            return []

        def check_self_protection(tool_name, tool_input):
            """Check if the tool call tries to modify openkeel files."""
            # Only check tools that modify files
            if tool_name not in ("Bash", "Write", "Edit", "NotebookEdit"):
                return None

            # Get the relevant string to check
            check_str = ""
            if tool_name == "Bash":
                check_str = tool_input.get("command", "")
            elif tool_name in ("Write", "Edit", "NotebookEdit"):
                check_str = tool_input.get("file_path", "")

            for pattern in SELF_PROTECT_PATTERNS:
                if re.search(pattern, check_str, re.IGNORECASE):
                    return f"Self-protection: cannot modify openkeel files (matched: {{pattern}})"
            return None

        def evaluate_rules(tool_name, tool_input, active_tags):
            """Evaluate constitution rules against a tool call."""
            if not os.path.exists(CONSTITUTION_PATH):
                return "allow", "", ""

            data = load_yaml_simple(CONSTITUTION_PATH)
            if not isinstance(data, dict) or "rules" not in data:
                return "allow", "", ""

            for rule in data["rules"]:
                if not isinstance(rule, dict):
                    continue

                # Check tool match
                rule_tool = rule.get("tool", "*")
                if rule_tool != "*" and rule_tool != tool_name:
                    continue

                # Check when_tags
                when_tags = rule.get("when_tags", [])
                if when_tags and not all(t in active_tags for t in when_tags):
                    continue

                # Get match config
                match = rule.get("match", {{}})
                if not isinstance(match, dict):
                    continue

                field_name = match.get("field", "command")
                pattern = match.get("pattern", "")
                if not pattern:
                    continue

                # Get field value
                field_value = tool_input.get(field_name, "")
                if not isinstance(field_value, str):
                    field_value = json.dumps(field_value)

                # Regex match
                try:
                    if re.search(pattern, field_value):
                        action = rule.get("action", "deny")
                        rule_id = rule.get("id", "unnamed")
                        message = rule.get("message", f"Rule {{rule_id}}: {{action}}")
                        return action, rule_id, message
                except re.error:
                    continue

            return "allow", "", ""

        def log_event(rule_id, action, tool_name, message):
            """Append enforcement event to log file."""
            try:
                Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
                entry = json.dumps({{
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "rule_id": rule_id,
                    "action": action,
                    "tool": tool_name,
                    "message": message,
                }})
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(entry + "\\n")
            except OSError:
                pass

        def main():
            try:
                input_data = json.loads(sys.stdin.read())
            except (json.JSONDecodeError, ValueError):
                # Can\'t parse input — allow by default
                print(json.dumps({{"decision": "allow"}}))
                return

            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {{}})

            # Self-protection check (always active, not in YAML)
            sp_msg = check_self_protection(tool_name, tool_input)
            if sp_msg:
                log_event("self-protect", "deny", tool_name, sp_msg)
                print(json.dumps({{"decision": "block", "reason": sp_msg}}))
                return

            # Load active mission tags
            active_tags = get_active_tags()

            # Evaluate constitution rules
            action, rule_id, message = evaluate_rules(tool_name, tool_input, active_tags)

            if action == "deny":
                log_event(rule_id, "deny", tool_name, message)
                print(json.dumps({{"decision": "block", "reason": message}}))
            elif action == "alert":
                log_event(rule_id, "alert", tool_name, message)
                print(json.dumps({{"decision": "allow"}}))
            else:
                print(json.dumps({{"decision": "allow"}}))

        if __name__ == "__main__":
            main()
    ''')

    output_path.write_text(script, encoding="utf-8")
    # Make executable on Unix
    try:
        output_path.chmod(0o755)
    except OSError:
        pass

    return output_path
