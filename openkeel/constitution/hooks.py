"""Generate self-contained hook scripts for constitution enforcement."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any


def generate_enforce_hook(
    constitution_path: str | Path,
    mission_dir: str | Path,
    active_mission: str,
    log_path: str | Path,
    output_path: str | Path,
    *,
    memoria_enabled: bool = False,
    memoria_endpoint: str = "http://127.0.0.1:8000",
    memoria_timeout: int = 10,
    memoria_top_k: int = 5,
    fv_mandatory_patterns: list[str] | None = None,
    fv_advisory_patterns: list[str] | None = None,
    fv_tool_queries: dict[str, str] | None = None,
) -> Path:
    """Generate the PreToolUse enforcement hook script.

    The generated script is completely self-contained — no openkeel imports.
    It reads the constitution YAML, evaluates rules, and outputs a decision.
    Also fires self-timer reminders, injects mission capsule, and queries
    Memoria memory before attack commands.

    Args:
        constitution_path: Path to constitution.yaml
        mission_dir: Path to missions directory (for reading active mission tags)
        active_mission: Name of active mission file (e.g. "my-mission.yaml")
        log_path: Path to enforcement log file
        output_path: Where to write the generated hook script
        memoria_enabled: Whether Memoria memory enforcement is active
        memoria_endpoint: Memoria server URL (default http://127.0.0.1:8000)
        memoria_timeout: Seconds per Memoria query
        memoria_top_k: Number of facts to retrieve
        fv_mandatory_patterns: Regex patterns for mandatory Memoria queries (exploitation, post-exploitation)
        fv_advisory_patterns: Regex patterns for advisory Memoria queries (enumeration)
        fv_tool_queries: Map of tool name -> semantic search query template

    Returns:
        Path to the generated script
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert paths to strings for embedding
    const_path_str = str(Path(constitution_path).expanduser().resolve())
    mission_dir_str = str(Path(mission_dir).expanduser().resolve())
    log_path_str = str(Path(log_path).expanduser().resolve())
    openkeel_dir_str = str(Path("~/.openkeel").expanduser().resolve())

    # Self-protection: write/delete patterns for Bash commands
    # These only match destructive operations targeting openkeel files
    self_protect_write_patterns = [
        r"(rm|del|move|mv|cp)\s+.*openkeel",
        r"(rm|del|move|mv|cp)\s+.*\.openkeel",
        r"(echo|cat|tee|sed|awk)\s+.*>.*openkeel",
        r"(echo|cat|tee|sed|awk)\s+.*>.*\.openkeel",
        r"(echo|cat|tee|sed|awk)\s+.*>.*constitution\.yaml",
    ]
    self_protect_write_json = repr(self_protect_write_patterns)
    # File path patterns for Write/Edit tools (always writes, so any match is blocked)
    self_protect_path_patterns = [
        r"openkeel_enforce",
        r"openkeel_inject",
        r"openkeel_drift",
        r"[/\\]\.openkeel[/\\]",
        r"constitution\.yaml",
    ]
    self_protect_path_json = repr(self_protect_path_patterns)

    # Memoria config for embedding
    fv_mandatory_json = json.dumps(fv_mandatory_patterns or [])
    fv_advisory_json = json.dumps(fv_advisory_patterns or [])
    fv_tool_queries_json = json.dumps(fv_tool_queries or {})

    script = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """OpenKeel constitution enforcement hook (auto-generated).

        PreToolUse hook for Claude Code. Reads rules from constitution.yaml,
        evaluates them against the tool call, and outputs allow/block decision.
        Also fires self-timer reminders, injects mission capsule on allow,
        and queries Memoria memory before attack commands.

        DO NOT EDIT — regenerate with: openkeel install
        """
        import json
        import os
        import re
        import sys
        from datetime import datetime, timedelta, timezone
        from pathlib import Path

        CONSTITUTION_PATH = r"{const_path_str}"
        MISSION_DIR = r"{mission_dir_str}"
        ACTIVE_MISSION = r"{active_mission}"
        LOG_PATH = r"{log_path_str}"
        OPENKEEL_DIR = r"{openkeel_dir_str}"
        TIMERS_PATH = os.path.join(OPENKEEL_DIR, "self_timers.json")
        ACTIVE_MISSION_FILE = os.path.join(OPENKEEL_DIR, "active_mission.txt")
        SELF_PROTECT_WRITE_PATTERNS = {self_protect_write_json}
        SELF_PROTECT_PATH_PATTERNS = {self_protect_path_json}

        # --- Memoria memory enforcement config ---
        MEMORIA_ENABLED = {memoria_enabled!r}
        MEMORIA_ENDPOINT = {json.dumps(memoria_endpoint)}
        MEMORIA_TIMEOUT = {memoria_timeout}
        MEMORIA_TOP_K = {memoria_top_k}
        MEMORIA_MANDATORY_PATTERNS = {fv_mandatory_json}
        MEMORIA_ADVISORY_PATTERNS = {fv_advisory_json}
        TOOL_QUERY_MAP = {fv_tool_queries_json}

        def load_yaml_simple(path):
            """Minimal YAML parser for constitution rules.

            Handles the subset of YAML used by constitution files:
            - Top-level mapping
            - Lists of mappings
            - Nested mappings (match field)
            - String values (quoted and unquoted)
            - Lists of strings

            Falls back gracefully if the file can\\'t be parsed.
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
            if tool_name == "Bash":
                command = tool_input.get("command", "")
                for pattern in SELF_PROTECT_WRITE_PATTERNS:
                    if re.search(pattern, command, re.IGNORECASE):
                        return f"Self-protection: blocked write/delete targeting openkeel (matched: {{pattern}})"
            elif tool_name in ("Write", "Edit", "NotebookEdit"):
                file_path = tool_input.get("file_path", "")
                for pattern in SELF_PROTECT_PATH_PATTERNS:
                    if re.search(pattern, file_path, re.IGNORECASE):
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

        def check_timers():
            """Fire any elapsed self-timers. Prints reminders to stdout."""
            try:
                if not os.path.exists(TIMERS_PATH):
                    return
                with open(TIMERS_PATH, "r", encoding="utf-8") as f:
                    timers = json.load(f)
                if not timers:
                    return

                now = datetime.now(timezone.utc)
                changed = False
                for t in timers:
                    if t.get("fired"):
                        continue
                    fire_at = datetime.fromisoformat(t["fire_at"])
                    # Normalize to UTC if naive
                    if fire_at.tzinfo is None:
                        fire_at = fire_at.replace(tzinfo=timezone.utc)
                    if fire_at <= now:
                        print(f"[OPENKEEL TIMER] {{t.get('message', 'Timer fired')}}")
                        repeat = t.get("repeat_minutes")
                        if repeat and isinstance(repeat, (int, float)) and repeat > 0:
                            t["fire_at"] = (fire_at + timedelta(minutes=repeat)).isoformat()
                        else:
                            t["fired"] = True
                        changed = True

                if changed:
                    with open(TIMERS_PATH, "w", encoding="utf-8") as f:
                        json.dump(timers, f, indent=2)
            except Exception:
                pass

        def get_mission_capsule():
            """Build a compact mission capsule string for context injection."""
            try:
                if not os.path.exists(ACTIVE_MISSION_FILE):
                    return ""
                with open(ACTIVE_MISSION_FILE, "r", encoding="utf-8") as f:
                    mission_name = f.read().strip()
                if not mission_name:
                    return ""

                mission_path = os.path.join(MISSION_DIR, f"{{mission_name}}.yaml")
                if not os.path.exists(mission_path):
                    return ""

                data = load_yaml_simple(mission_path)
                if not isinstance(data, dict):
                    return ""

                objective = data.get("objective", "")
                if not objective:
                    return ""

                lines = [f"[OPENKEEL] Mission: {{mission_name}}"]
                lines.append(f"Objective: {{objective}}")

                # Plan steps — show up to 3 next steps
                plan = data.get("plan", [])
                if plan:
                    shown = 0
                    for step in plan:
                        if not isinstance(step, dict):
                            continue
                        status = step.get("status", "pending")
                        if status == "done":
                            continue
                        if status == "in_progress":
                            marker = "[>]"
                        elif status == "skipped":
                            marker = "[-]"
                        else:
                            marker = "[ ]"
                        label = "Next: " if shown == 0 else "      "
                        lines.append(f"{{label}}{{marker}} {{step.get('step', '?')}}")
                        shown += 1
                        if shown >= 3:
                            break

                # Latest finding (max 1)
                findings = data.get("findings", [])
                if findings:
                    lines.append(f"Finding: {{findings[-1]}}")

                return "\\n".join(lines)
            except Exception:
                return ""

        def get_knowledge_capsule():
            """Read capsule_line from session_context.json if available."""
            try:
                ctx_path = os.path.join(OPENKEEL_DIR, "session_context.json")
                if not os.path.exists(ctx_path):
                    return ""
                with open(ctx_path, "r", encoding="utf-8") as f:
                    ctx = json.load(f)
                parts = []
                capsule = ctx.get("capsule_line", "")
                if capsule:
                    parts.append(capsule)
                task_summary = ctx.get("task_summary", "")
                if task_summary:
                    # Extract just the count line for brevity
                    for line in task_summary.split("\\n"):
                        if "tasks" in line and "todo" in line:
                            parts.append(line.strip())
                            break
                return " | ".join(parts)
            except Exception:
                return ""

        # --- Memoria memory enforcement ---

        def unwrap_ssh_command(command):
            """Extract the remote command from an SSH invocation.

            Handles:
              ssh user@host "remote command"
              ssh user@host 'remote command'
              ssh -i key -p 22 user@host remote command args
              ssh user@host bash -c "remote command"

            Returns the inner command if SSH wrapper detected, else original command.
            """
            cmd = command.strip()
            if not cmd.startswith("ssh "):
                return command
            # Tokenize: skip 'ssh', consume flags (some take args), find host, rest is remote cmd
            # Flags that take a separate argument:
            ARG_FLAGS = {{"-i", "-p", "-l", "-o", "-J", "-F", "-L", "-R", "-D", "-W", "-b", "-c", "-E", "-e", "-I", "-m", "-O", "-Q", "-S", "-w"}}
            parts = cmd.split()
            i = 1  # skip "ssh"
            host = None
            while i < len(parts):
                tok = parts[i]
                if tok.startswith("-"):
                    if tok in ARG_FLAGS and i + 1 < len(parts):
                        i += 2  # skip flag + its argument
                    else:
                        i += 1  # boolean flag like -v, -N, -T
                else:
                    # First non-flag token = host
                    host = tok
                    i += 1
                    break
            if host is None or i >= len(parts):
                return command  # no remote command (interactive ssh)
            # Everything after the host is the remote command
            remote = " ".join(parts[i:]).strip()
            # Strip surrounding quotes
            if (remote.startswith('"') and remote.endswith('"')) or \\
               (remote.startswith("'") and remote.endswith("'")):
                remote = remote[1:-1].strip()
            # Handle: bash -c "actual command"
            m2 = re.match(r'^bash\s+-c\s+["\\'](.*)["\\']\s*$', remote)
            if m2:
                remote = m2.group(1).strip()
            return remote if remote else command

        def classify_for_memoria(command):
            """Classify a command as mandatory/advisory/None for Memoria lookup.

            Unwraps SSH commands first so `ssh host "sqlmap ..."` matches sqlmap patterns.

            Returns:
                "mandatory" — always query Memoria, warn if no results
                "advisory"  — query Memoria, silently skip if no results
                None        — skip Memoria entirely (recon, utils, etc.)
            """
            if not MEMORIA_ENABLED:
                return None
            inner = unwrap_ssh_command(command)
            for pattern in MEMORIA_MANDATORY_PATTERNS:
                try:
                    if re.search(pattern, inner):
                        return "mandatory"
                except re.error:
                    continue
            for pattern in MEMORIA_ADVISORY_PATTERNS:
                try:
                    if re.search(pattern, inner):
                        return "advisory"
                except re.error:
                    continue
            return None

        def extract_memoria_query(command):
            """Extract a semantic search query from a command.

            Unwraps SSH commands first. Uses TOOL_QUERY_MAP for known tools,
            falls back to extracting the tool name + target from the command.

            Returns:
                Search query string, or empty string if no query can be built.
            """
            # Unwrap SSH wrapper to get inner command
            cmd_stripped = unwrap_ssh_command(command).strip()
            for tool_name, query_template in TOOL_QUERY_MAP.items():
                # Match tool name at start, with optional path prefix
                if re.match(rf"^(?:\S*/)?{{re.escape(tool_name)}}\\b", cmd_stripped):
                    # Try to extract target from the command for context
                    # Look for IP addresses, hostnames, or URLs
                    targets = re.findall(
                        r"(?:https?://)?\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}(?::\\d+)?(?:/\\S*)?|"
                        r"(?:https?://)?[a-zA-Z][a-zA-Z0-9.-]+\\.(?:htb|local|com|net|org)(?::\\d+)?(?:/\\S*)?",
                        cmd_stripped
                    )
                    if targets:
                        return f"{{query_template}} target {{targets[0]}}"
                    return query_template
            # Also check for impacket-* style tools
            m = re.match(r"^(?:\\S*/)?impacket-(\\S+)", cmd_stripped)
            if m and "impacket" in TOOL_QUERY_MAP:
                tool = m.group(1)
                return f"{{TOOL_QUERY_MAP['impacket']}} {{tool}}"
            return ""

        def query_memoria(search_query):
            """Query Memoria /search endpoint. Returns list of (score, fact) tuples.

            Uses urllib only (no external deps). Graceful degradation on failure.
            """
            try:
                import urllib.request
                import urllib.error
                url = f"{{MEMORIA_ENDPOINT}}/search"
                payload = json.dumps({{"query": search_query, "top_k": MEMORIA_TOP_K}}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=payload,
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=MEMORIA_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                results = data.get("results", data.get("facts", []))
                out = []
                for item in results:
                    if isinstance(item, dict):
                        score = item.get("relevance", item.get("score", item.get("similarity", 0.0)))
                        fact = item.get("fact", item.get("text", ""))
                        if fact:
                            out.append((float(score), fact))
                    elif isinstance(item, str):
                        out.append((0.0, item))
                return out
            except Exception:
                return []

        def inject_memoria_results(command, tier):
            """Query Memoria and print results to stdout for Claude Code context injection.

            Args:
                command: The Bash command being run
                tier: "mandatory" or "advisory"
            """
            search_query = extract_memoria_query(command)
            if not search_query:
                if tier == "mandatory":
                    print("[OPENKEEL MEMORIA] No query could be built for this command. Consider consulting Memoria memory manually.")
                return

            results = query_memoria(search_query)

            if results:
                print(f"[OPENKEEL MEMORIA] Knowledge recall for: {{search_query}}")
                for i, (score, fact) in enumerate(results, 1):
                    # Truncate long facts to keep output manageable
                    display = fact if len(fact) <= 200 else fact[:197] + "..."
                    print(f"  {{i}}. [{{score:.2f}}] {{display}}")
                print("[OPENKEEL MEMORIA] Review these facts before proceeding.")
            elif tier == "mandatory":
                print(f"[OPENKEEL MEMORIA] No facts found for: {{search_query}}")
                print("[OPENKEEL MEMORIA] WARNING: No Memoria knowledge for this attack. Consider seeding Memoria or researching first.")
            # advisory tier with no results: stay silent

        def check_memoria_health():
            """Check if Memoria is reachable. Used by session start hook."""
            if not MEMORIA_ENABLED:
                return
            try:
                import urllib.request
                url = f"{{MEMORIA_ENDPOINT}}/health"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                count = data.get("memory_facts", data.get("fact_count", data.get("facts", "?")))
                print(f"[OPENKEEL MEMORIA] Connected — {{count}} facts available")
            except Exception:
                print("[OPENKEEL MEMORIA] OFFLINE — Memoria not reachable at {{MEMORIA_ENDPOINT}}")
                print("[OPENKEEL MEMORIA] Run: ssh -L 8000:localhost:8000 om@192.168.0.224")

        def emit_allow_extras():
            """Fire timers and print mission capsule + knowledge capsule on allow path."""
            check_timers()
            capsule = get_mission_capsule()
            if capsule:
                print(capsule)
            knowledge = get_knowledge_capsule()
            if knowledge:
                print(f"[OPENKEEL KNOWLEDGE] {{knowledge}}")

        def main():
            try:
                input_data = json.loads(sys.stdin.read())
            except (json.JSONDecodeError, ValueError):
                # Can\\'t parse input — fail open
                sys.exit(0)

            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {{}})

            # Self-protection check (always active, not in YAML)
            sp_msg = check_self_protection(tool_name, tool_input)
            if sp_msg:
                log_event("self-protect", "deny", tool_name, sp_msg)
                print(sp_msg, file=sys.stderr)
                sys.exit(2)

            # Load active mission tags
            active_tags = get_active_tags()

            # Evaluate constitution rules
            action, rule_id, message = evaluate_rules(tool_name, tool_input, active_tags)

            if action == "deny":
                log_event(rule_id, "deny", tool_name, message)
                print(message, file=sys.stderr)
                sys.exit(2)
            elif action == "alert":
                log_event(rule_id, "alert", tool_name, message)
                # Memoria query for Bash commands (advisory — after alert)
                if MEMORIA_ENABLED and tool_name == "Bash":
                    command = tool_input.get("command", "")
                    tier = classify_for_memoria(command)
                    if tier:
                        inject_memoria_results(command, tier)
                emit_allow_extras()
                sys.exit(0)
            else:
                # Memoria query for Bash commands (before allow extras)
                if MEMORIA_ENABLED and tool_name == "Bash":
                    command = tool_input.get("command", "")
                    tier = classify_for_memoria(command)
                    if tier:
                        inject_memoria_results(command, tier)
                emit_allow_extras()
                sys.exit(0)

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
