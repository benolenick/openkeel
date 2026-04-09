"""Generate the PostToolUse scribe hook for automatic session journaling.

The scribe accumulates tool calls during a session and periodically writes
journal entries summarizing what happened — like an automatic story.txt.
"""
from __future__ import annotations

import textwrap
from pathlib import Path


def generate_scribe_hook(
    output_path: str | Path,
    journal_every: int = 15,
    hyphae_endpoint: str = "http://127.0.0.1:8100",
) -> Path:
    """Generate the PostToolUse scribe hook script.

    The generated script:
    - Accumulates tool calls in ~/.openkeel/scribe_state.json
    - Every `journal_every` tool calls, writes a journal entry via CLI
    - Tracks files read/edited, bash commands, decisions

    Args:
        output_path: Where to write the generated hook script
        journal_every: Write a journal entry every N tool calls

    Returns:
        Path to the generated script
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    script = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """OpenKeel scribe hook (auto-generated).

        PostToolUse hook for Claude Code. Accumulates tool call summaries
        and periodically writes journal entries for session continuity.

        DO NOT EDIT — regenerate with: openkeel install
        """
        import json
        import os
        import re
        import subprocess
        import sys
        from datetime import datetime, timezone
        from pathlib import Path

        OPENKEEL_DIR = os.path.expanduser("~/.openkeel")
        STATE_PATH = os.path.join(OPENKEEL_DIR, "scribe_state.json")
        ACTIVE_MISSION_FILE = os.path.join(OPENKEEL_DIR, "active_mission.txt")
        JOURNAL_EVERY = {journal_every}
        HYPHAE_ENDPOINT = {repr(hyphae_endpoint)}


        def _load_state():
            """Load or initialize scribe state."""
            if os.path.exists(STATE_PATH):
                try:
                    with open(STATE_PATH, "r", encoding="utf-8") as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass
            return {{
                "call_count": 0,
                "since_last_journal": 0,
                "session_start": datetime.now(timezone.utc).isoformat(),
                "files_read": [],
                "files_edited": [],
                "files_created": [],
                "bash_commands": [],
                "journal_count": 0,
            }}


        def _save_state(state):
            """Persist scribe state."""
            try:
                Path(OPENKEEL_DIR).mkdir(parents=True, exist_ok=True)
                with open(STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
            except OSError:
                pass


        def _get_project():
            """Get active mission/project name."""
            try:
                if os.path.exists(ACTIVE_MISSION_FILE):
                    with open(ACTIVE_MISSION_FILE, "r", encoding="utf-8") as f:
                        name = f.read().strip()
                    if name:
                        return name
            except OSError:
                pass
            return "default"


        def _truncate(s, maxlen=120):
            """Truncate a string for summary display."""
            if len(s) <= maxlen:
                return s
            return s[:maxlen - 3] + "..."


        # Tools that count as "research" for the research gate
        RESEARCH_TOOLS = {{"WebSearch", "WebFetch"}}
        RESEARCH_BASH_PATTERNS = [
            r"curl\s+.*localhost[:\s].*8100",   # Hyphae query
            r"curl\s+.*127\.0\.0\.1[:\s].*8100",  # Hyphae query
            r"curl\s+.*localhost[:\s].*8000",   # Legacy Memoria query
            r"curl\s+.*127\.0\.0\.1[:\s].*8000",  # Legacy Memoria query
            r"openkeel\s+recall\b",  # openkeel recall
        ]

        def _is_research_action(tool_name, tool_input):
            """Check if this tool call counts as a research action."""
            if tool_name in RESEARCH_TOOLS:
                return True
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                for pattern in RESEARCH_BASH_PATTERNS:
                    if re.search(pattern, cmd):
                        return True
            return False

        def _record_tool_call(state, tool_name, tool_input):
            """Record a tool call into scribe state."""
            state["call_count"] += 1
            state["since_last_journal"] += 1

            # Track research actions for the research gate
            if _is_research_action(tool_name, tool_input):
                state["commands_since_research"] = 0
                state["total_research"] = state.get("total_research", 0) + 1
            else:
                state["commands_since_research"] = state.get("commands_since_research", 0) + 1

            if tool_name == "Read":
                fp = tool_input.get("file_path", "")
                if fp and fp not in state["files_read"]:
                    state["files_read"].append(fp)
                    # Keep list bounded
                    if len(state["files_read"]) > 50:
                        state["files_read"] = state["files_read"][-50:]

            elif tool_name == "Edit":
                fp = tool_input.get("file_path", "")
                if fp and fp not in state["files_edited"]:
                    state["files_edited"].append(fp)

            elif tool_name == "Write":
                fp = tool_input.get("file_path", "")
                if fp and fp not in state["files_created"]:
                    state["files_created"].append(fp)

            elif tool_name == "Bash":
                cmd = tool_input.get("command", "")
                if cmd:
                    state["bash_commands"].append(_truncate(cmd))
                    # Keep last 30 commands
                    if len(state["bash_commands"]) > 30:
                        state["bash_commands"] = state["bash_commands"][-30:]


        def _build_journal_body(state):
            """Build a structured journal entry from accumulated state."""
            parts = []
            parts.append(f"Auto-journal after {{state['call_count']}} tool calls")

            if state["files_edited"]:
                parts.append("Edited: " + ", ".join(
                    os.path.basename(f) for f in state["files_edited"][-10:]
                ))

            if state["files_created"]:
                parts.append("Created: " + ", ".join(
                    os.path.basename(f) for f in state["files_created"][-10:]
                ))

            if state["files_read"]:
                count = len(state["files_read"])
                recent = ", ".join(
                    os.path.basename(f) for f in state["files_read"][-5:]
                )
                parts.append(f"Read {{count}} files (recent: {{recent}})")

            if state["bash_commands"]:
                # Show last few meaningful commands
                meaningful = [
                    c for c in state["bash_commands"]
                    if not c.strip().startswith(("ls", "pwd", "cd", "echo"))
                ][-5:]
                if meaningful:
                    parts.append("Commands: " + " | ".join(meaningful))

            return "; ".join(parts)


        def _write_journal(state):
            """Write a journal entry via openkeel CLI."""
            body = _build_journal_body(state)
            project = _get_project()
            title = f"scribe-auto-{{state['journal_count'] + 1}}"

            try:
                subprocess.run(
                    [
                        sys.executable, "-m", "openkeel",
                        "journal", "add", body,
                        "-T", title,
                        "-p", project,
                        "--entry-type", "scribe",
                        "-t", "auto,scribe",
                    ],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

            # Remember journal summary + files touched in Hyphae
            _send_hyphae_remember(
                body,
                tags={{"type": "journal", "project": project}},
                source=f"scribe:{{project}}",
            )
            if state["files_edited"] or state["files_created"]:
                all_files = state["files_edited"] + state["files_created"]
                fact = f"Session touched: {{', '.join(os.path.basename(f) for f in all_files[-10:])}}"
                _send_hyphae_remember(
                    fact,
                    tags={{"type": "files_touched", "project": project}},
                    source=f"scribe:{{project}}",
                )

            # Reset counters but keep cumulative data
            state["since_last_journal"] = 0
            state["journal_count"] += 1
            # Clear command buffer after journaling
            state["bash_commands"] = []

            # Trigger memory compaction every 3 journal writes
            if state["journal_count"] % 3 == 0:
                try:
                    from openkeel.core.compactor import compact_and_prune, CompactorConfig
                    compact_and_prune(CompactorConfig(project=project))
                except Exception:
                    pass


        # --- Hyphae auto-remember ---
        ATTACK_TOOLS = {{
            "nmap", "masscan", "rustscan", "gobuster", "ffuf", "feroxbuster",
            "nikto", "nuclei", "sqlmap", "hydra", "medusa", "john", "hashcat",
            "crackmapexec", "netexec", "smbclient", "enum4linux", "ldapsearch",
            "rpcclient", "bloodhound", "kerbrute", "impacket", "evil-winrm",
            "linpeas", "winpeas", "chisel", "ligolo", "msfconsole", "wpscan",
            "secretsdump", "GetNPUsers", "GetUserSPNs", "GetTGT",
        }}

        def _send_hyphae_remember(text, tags=None, source="scribe"):
            """Store a fact in Hyphae via /remember. Fire and forget."""
            try:
                import urllib.request
                payload = json.dumps({{
                    "text": text[:500],
                    "tags": tags or {{}},
                    "source": source,
                }}).encode("utf-8")
                req = urllib.request.Request(
                    f"{{HYPHAE_ENDPOINT}}/remember",
                    data=payload,
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass  # Fire and forget

        def _ensure_hyphae_session(project):
            """Set Hyphae session scope to the active project. Called once per session."""
            try:
                import urllib.request
                payload = json.dumps({{
                    "scope": {{"project": project}},
                }}).encode("utf-8")
                req = urllib.request.Request(
                    f"{{HYPHAE_ENDPOINT}}/session/set",
                    data=payload,
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass

        def _send_hyphae_converse(role, message):
            """Send a conversation turn to Hyphae for fact extraction. Fire and forget."""
            try:
                import urllib.request
                payload = json.dumps({{
                    "role": role,
                    "message": message[:2000],
                    "source": f"conversation:{{_get_project()}}",
                }}).encode("utf-8")
                req = urllib.request.Request(
                    f"{{HYPHAE_ENDPOINT}}/converse",
                    data=payload,
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass

        # --- Distilled log for Observer system ---
        def _write_distilled_log(category, message, confidence=-1.0, **meta):
            """Write an entry to the distilled log for the Cartographer to read.

            The observer daemon watches this file and builds the problem-space graph.
            """
            try:
                project = _get_project()
                goals_dir = os.path.join(OPENKEEL_DIR, "goals", project)
                os.makedirs(goals_dir, exist_ok=True)
                log_path = os.path.join(goals_dir, "distilled_log.jsonl")

                from datetime import datetime, timezone
                entry = json.dumps({{
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    "cat": category,
                    "msg": message[:500],
                    "conf": confidence if confidence >= 0 else None,
                    "stone": None,
                    "hyp": None,
                    "meta": meta or None,
                }}, separators=(",", ":"))

                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(entry + "\\n")
            except Exception:
                pass  # Never block the agent

        def _distill_tool_call(tool_name, tool_input, tool_output):
            """Map a tool call to a distilled log entry for the observers."""
            if tool_name == "Bash":
                cmd = tool_input.get("command", "").strip()
                if not cmd:
                    return
                tool_word = cmd.split()[0].rsplit("/", 1)[-1].rsplit("\\\\", 1)[-1] if cmd else ""

                # Attack tool → ATTEMPT
                if tool_word in ATTACK_TOOLS:
                    _write_distilled_log("ATTEMPT", f"{{tool_word}}: {{cmd[:200]}}")
                    # Check output for success/failure indicators
                    out = (tool_output or "")[:1000].lower()
                    if any(w in out for w in ["error", "failed", "denied", "refused", "timeout"]):
                        _write_distilled_log("RESULT", f"FAIL: {{tool_word}} — {{out[:150]}}")
                    elif any(w in out for w in ["success", "found", "flag", "root", "admin", "shell"]):
                        _write_distilled_log("RESULT", f"SUCCESS: {{tool_word}} — {{out[:150]}}")

                # curl to retrieval stack → research
                elif "curl" in cmd and any(p in cmd for p in ["8000", "8002", "8003", "8004", "8100"]):
                    _write_distilled_log("DISCOVERY", f"Retrieved knowledge: {{cmd[:150]}}")

                # SSH/connection commands → ENV
                elif tool_word in ("ssh", "sshpass", "evil-winrm"):
                    _write_distilled_log("ENV", f"Connection: {{cmd[:200]}}")

            elif tool_name == "WebSearch":
                query = tool_input.get("query", "")
                _write_distilled_log("DISCOVERY", f"Web search: {{query[:200]}}")

            elif tool_name == "WebFetch":
                url = tool_input.get("url", "")
                _write_distilled_log("DISCOVERY", f"Fetched: {{url[:200]}}")

        def _record_treadstone_attempt(cmd, output):
            """Record attack attempt, update confidence, auto-advance if needed."""
            try:
                project = _get_project()
                goals_dir = os.path.join(OPENKEEL_DIR, "goals", project)
                tree_file = os.path.join(goals_dir, "treadstone_tree.yaml")
                if not os.path.exists(tree_file):
                    return
                try:
                    import yaml
                    with open(tree_file, "r", encoding="utf-8") as tf:
                        tree = yaml.safe_load(tf)
                except Exception:
                    return
                if not isinstance(tree, dict):
                    return

                active_id = tree.get("active_stone_id")
                if not active_id:
                    return

                cb = tree.get("circuit_breaker", {{}})
                max_att = cb.get("max_attempts_per_hypothesis", 3)
                abandon_thresh = cb.get("abandon_threshold", 0.2)

                # Find active stone
                active_stone = None
                for stone in tree.get("stones", []):
                    if stone.get("id") == active_id:
                        active_stone = stone
                        break
                if not active_stone:
                    return

                # Detect result from output
                out_lower = (output or "")[:2000].lower()
                if any(w in out_lower for w in ["flag{{", "root:", "nt authority", "pwned", "shell obtained"]):
                    result = "success"
                elif any(w in out_lower for w in ["error", "failed", "denied", "refused", "timeout", "not found", "connection reset"]):
                    result = "fail"
                elif any(w in out_lower for w in ["partial", "redirect", "interesting", "open"]):
                    result = "partial"
                else:
                    result = "pending"

                # Find first active hypothesis and record attempt
                recorded = False
                for hyp in active_stone.get("hypotheses", []):
                    if hyp.get("status") != "active":
                        continue
                    import uuid as _uuid
                    attempt = {{
                        "id": _uuid.uuid4().hex[:8],
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                        "command": cmd[:200],
                        "expected_outcome": "pending",
                        "actual_outcome": (output or "")[:200],
                        "result": result,
                        "notes": "",
                        "duration_s": 0.0,
                    }}
                    hyp.setdefault("attempts", []).append(attempt)

                    # Update confidence (Bayesian)
                    old_conf = hyp.get("confidence", 0.5)
                    if result == "success":
                        hyp["confidence"] = 1.0
                        hyp["status"] = "succeeded"
                    elif result == "fail":
                        hyp["confidence"] = round(old_conf * 0.6, 3)
                    elif result == "partial":
                        hyp["confidence"] = round(min(1.0, old_conf + 0.15), 3)

                    hyp.setdefault("confidence_history", []).append({{
                        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                        "old": old_conf,
                        "new": hyp.get("confidence", old_conf),
                        "result": result,
                        "reason": cmd[:100],
                    }})
                    recorded = True
                    break

                if not recorded:
                    return

                # Check auto-advance
                _maybe_auto_advance(tree, active_stone, max_att, abandon_thresh, tree_file)

            except Exception:
                pass


        def _maybe_auto_advance(tree, active_stone, max_att, abandon_thresh, tree_file):
            """Check if all hypotheses are exhausted and advance to next stone."""
            import yaml
            all_exhausted = True
            any_succeeded = False

            for hyp in active_stone.get("hypotheses", []):
                status = hyp.get("status", "active")
                if status == "succeeded":
                    any_succeeded = True
                elif status == "active":
                    attempts = len([a for a in hyp.get("attempts", []) if a.get("result") != "pending"])
                    conf = hyp.get("confidence", 0.5)
                    if attempts >= max_att or conf <= abandon_thresh:
                        hyp["status"] = "abandoned"
                        hyp["confidence"] = 0.0
                        _write_distilled_log("CIRCUIT", "Auto-abandoned: " + hyp.get("label", "?"))
                        print("[TREADSTONE] Circuit breaker: abandoned '" + hyp.get("label", "?") + "'")
                    else:
                        all_exhausted = False

            if not (any_succeeded or all_exhausted):
                tree["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                with open(tree_file, "w", encoding="utf-8") as tf:
                    yaml.dump(tree, tf, default_flow_style=False, sort_keys=False)
                return

            # Find next pending stone
            next_stone = None
            for stone in tree.get("stones", []):
                if stone.get("status") == "pending":
                    next_stone = stone
                    break

            reason = "hypothesis succeeded" if any_succeeded else "all hypotheses exhausted"
            active_stone["status"] = "done" if any_succeeded else "failed"
            active_stone["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

            if next_stone:
                next_stone["status"] = "active"
                tree["active_stone_id"] = next_stone.get("id")
                _write_distilled_log("PHASE", "Auto-advanced to '" + next_stone.get("label", "?") + "' (" + reason + ")")
                print()
                print("[TREADSTONE] ========== PHASE CHANGE ==========")
                print("[TREADSTONE] Advanced to: " + next_stone.get("label", "?"))
                print("[TREADSTONE] Reason: " + reason)
                print("[TREADSTONE] Objective: " + next_stone.get("objective", ""))
                print("[TREADSTONE] ====================================")
            else:
                _write_distilled_log("PHASE", "All stones complete (" + reason + ")")
                print()
                print("[TREADSTONE] All stones complete or exhausted.")
                print("[TREADSTONE] Add new stones/hypotheses to continue.")

            tree["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            with open(tree_file, "w", encoding="utf-8") as tf:
                yaml.dump(tree, tf, default_flow_style=False, sort_keys=False)


        def _check_live_nudges():
            """Read observer nudges and print them live for the agent to see."""
            project = _get_project()
            nudge_path = os.path.join(OPENKEEL_DIR, "goals", project, "observer_nudges.jsonl")
            if not os.path.exists(nudge_path):
                return
            try:
                with open(nudge_path, "r", encoding="utf-8") as fh:
                    nudge_lines = fh.readlines()
            except OSError:
                return
            if not nudge_lines:
                return
            recent = nudge_lines[-5:]
            findings = []
            for nline in recent:
                try:
                    entry = json.loads(nline.strip())
                    lvl = entry.get("level", "nudge").upper()
                    txt = entry.get("text", "")
                    if txt:
                        findings.append("[OBSERVER " + lvl + "] " + txt)
                except json.JSONDecodeError:
                    continue
            if findings:
                print()
                print("=" * 60)
                print("LIVE OBSERVER FINDINGS")
                print("=" * 60)
                for finding in findings:
                    print(finding)
                print("=" * 60)
                # Consume nudges after reading
                try:
                    with open(nudge_path, "w", encoding="utf-8") as fh:
                        pass
                except OSError:
                    pass

        def main():
            try:
                input_data = json.loads(sys.stdin.read())
            except (json.JSONDecodeError, ValueError):
                sys.exit(0)

            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {{}})

            # Skip recording our own journal/remember calls
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                if "openkeel journal" in cmd or "openkeel remember" in cmd:
                    sys.exit(0)

            state = _load_state()

            # On first tool call, set Hyphae session scope to active project
            if state["call_count"] == 0:
                project = _get_project()
                _ensure_hyphae_session(project)

            _record_tool_call(state, tool_name, tool_input)

            # Send tool output to Hyphae for conversation fact extraction
            # Only for tools that produce meaningful text (not file reads etc.)
            tool_output = input_data.get("tool_output", "")
            if tool_name in ("Bash",) and tool_output and len(tool_output) > 50:
                _send_hyphae_converse("assistant", tool_output[:2000])

            # Auto-remember attack tool usage in Hyphae + record treadstone attempt
            if tool_name == "Bash":
                cmd = tool_input.get("command", "").strip()
                tool_word = cmd.split()[0].rsplit("/", 1)[-1].rsplit("\\\\", 1)[-1] if cmd else ""
                if tool_word in ATTACK_TOOLS:
                    history = state.setdefault("attack_tool_history", [])
                    history.append(cmd[:200])
                    if len(history) > 20:
                        state["attack_tool_history"] = history[-20:]
                    # Remember attack tool usage in Hyphae
                    project = _get_project()
                    _send_hyphae_remember(
                        f"[{{project}}] Ran: {{cmd[:200]}}",
                        tags={{"type": "action", "tool": tool_word, "project": project}},
                        source=f"scribe:{{project}}",
                    )
                    # Auto-record as treadstone attempt (pending — agent marks result)
                    _record_treadstone_attempt(cmd, tool_output)

            # Write to distilled log for Observer system (Cartographer/Pilgrim/Oracle)
            _distill_tool_call(tool_name, tool_input, tool_output)

            # Check for live observer findings and print for the agent
            _check_live_nudges()

            # Check if it's time to journal
            if state["since_last_journal"] >= JOURNAL_EVERY:
                _write_journal(state)

            _save_state(state)
            sys.exit(0)


        if __name__ == "__main__":
            main()
    ''')

    output_path.write_text(script, encoding="utf-8")
    try:
        output_path.chmod(0o755)
    except OSError:
        pass

    return output_path


def flush_scribe(project: str = "") -> str | None:
    """Flush the scribe state — write final journal entry and reset.

    Called by the Stop hook or manually. Returns the journal body or None.
    """
    import json
    import subprocess
    import sys
    from datetime import datetime, timezone

    state_path = Path.home() / ".openkeel" / "scribe_state.json"
    if not state_path.exists():
        return None

    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if state.get("call_count", 0) == 0:
        return None

    # Build final summary
    parts = [f"Session end — {state['call_count']} total tool calls"]

    edited = state.get("files_edited", [])
    created = state.get("files_created", [])
    if edited:
        parts.append(f"Edited {len(edited)} files: " + ", ".join(
            str(Path(f).name) for f in edited
        ))
    if created:
        parts.append(f"Created {len(created)} files: " + ", ".join(
            str(Path(f).name) for f in created
        ))

    read_count = len(state.get("files_read", []))
    if read_count:
        parts.append(f"Read {read_count} files")

    journals = state.get("journal_count", 0)
    if journals:
        parts.append(f"{journals} auto-journal entries written during session")

    body = "; ".join(parts)

    if not project:
        active_file = Path.home() / ".openkeel" / "active_mission.txt"
        try:
            project = active_file.read_text(encoding="utf-8").strip()
        except OSError:
            project = "default"

    try:
        subprocess.run(
            [
                sys.executable, "-m", "openkeel",
                "journal", "add", body,
                "-T", "scribe-session-end",
                "-p", project,
                "--entry-type", "scribe",
                "-t", "auto,scribe,session-end",
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass

    # Reset state
    try:
        state_path.unlink()
    except OSError:
        pass

    return body
