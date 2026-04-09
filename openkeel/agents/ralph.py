"""Ralph — Overwatch for the Duo agent system.

Ralph is simple: every few minutes, read the logs, think about what's happening,
and act if something's wrong. Same thing a human does when they check in.

No heartbeat protocol, no boolean health checks. Just:
  1. Read the recent Duo output
  2. Ask Claude: "What's going on? Is this working? Is it on track?"
  3. If there's a problem, fix it (restart, send directive, patch code)
  4. Save what you found to Hyphae so future checks have context
  5. Sleep. Repeat.

Usage:
    python -m openkeel.agents.ralph \\
      --goal "Improve the SC2 bot until it beats Hard AI" \\
      --working-dir /home/om/sc2-commander \\
      --test-command "bash test_game.sh --headless" \\
      --test "bash -c 'cd /home/om/sc2-commander && ...'"
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ralph] %(message)s")
log = logging.getLogger("ralph")

BOARD_URL = "http://127.0.0.1:8200"
HYPHAE_URL = "http://127.0.0.1:8100"
CHECK_INTERVAL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, data: dict | None = None) -> dict | None:
    url = f"{BOARD_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _hyphae_remember(fact: str):
    try:
        body = json.dumps({"text": fact, "source": "ralph"}).encode()
        req = urllib.request.Request(f"{HYPHAE_URL}/remember", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _hyphae_recall(query: str, top_k: int = 5) -> list[str]:
    try:
        body = json.dumps({"query": query, "top_k": top_k}).encode()
        req = urllib.request.Request(f"{HYPHAE_URL}/recall", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return [r["text"][:200] for r in data.get("results", [])[:top_k]]
    except Exception:
        return []


def _get_duo_log() -> str | None:
    """Find the most recent Duo log file."""
    logs = sorted(Path("/tmp").glob("duo_ralph_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(logs[0]) if logs else None


def _tail_log(path: str, lines: int = 80) -> str:
    """Read the last N lines of a log file."""
    try:
        result = subprocess.run(["tail", "-n", str(lines), path],
                                capture_output=True, text=True, timeout=5)
        return result.stdout
    except Exception:
        return "(could not read log)"


def _get_board_status() -> str:
    """Get a quick status summary from the Command Board."""
    agents = _api("GET", "/api/agents")
    if not agents or not isinstance(agents, list):
        return "BOARD DOWN or unreachable"

    duo_agents = [a for a in agents if a.get("name") in ("director", "operator", "critic")]
    if not duo_agents:
        return "BOARD UP but no Duo agents registered"

    parts = []
    for a in duo_agents:
        age = time.time() - a.get("last_heartbeat", 0)
        parts.append(f"{a['name']}={a.get('effective_status','?')} ({age:.0f}s ago)")
    return "BOARD UP. " + ", ".join(parts)


def _get_duo_pid() -> int | None:
    try:
        result = subprocess.run(["pgrep", "-f", "openkeel.agents.duo"],
                                capture_output=True, text=True, timeout=5)
        pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


def _kill_duo():
    try:
        subprocess.run(["pkill", "-f", "openkeel.agents.duo"], timeout=10)
        time.sleep(3)
    except Exception:
        pass


def _launch_duo(goal: str, working_dir: str, test_command: str,
                test_commands: list[str], model: str = "sonnet") -> int:
    """Launch Duo and return PID."""
    cmd = [sys.executable, "-m", "openkeel.agents.duo", goal,
           "-d", working_dir, "-m", model, "--poll", "30"]
    if test_command:
        cmd.extend(["--test-command", test_command])
    for tc in test_commands:
        cmd.extend(["--test", tc])

    log_path = f"/tmp/duo_ralph_{int(time.time())}.log"
    log_file = open(log_path, "w")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    return proc.pid, log_path


# ---------------------------------------------------------------------------
# The brain — just ask Claude what's going on
# ---------------------------------------------------------------------------

OVERWATCH_PROMPT = """\
You are Ralph, an overwatch agent monitoring a multi-agent AI system called "Duo."

THE GOAL: {goal}

The Duo system has:
- Director: plans improvement cycles (3-5 code changes per batch), runs tests between cycles
- Operator: executes code changes via Claude CLI in {working_dir}
- Critic: reviews each change before Director advances

The system should be running continuous improvement cycles:
  plan code changes → Operator executes them → Critic reviews → Director runs test → analyze → replan → repeat

YOUR JOB: Read the recent output below and answer these questions:

1. IS IT RUNNING? Is the Duo process alive and producing output?
2. IS IT WORKING? Are cycles completing? Are steps succeeding? Are tests running?
3. IS IT ON TRACK? Is the work actually moving toward the goal, or is it spinning on meta-problems (permissions, test harness bugs, etc.)?
4. IS IT STUCK? Same error repeating? Agents unable to communicate? Board down?

PREVIOUS CONTEXT FROM HYPHAE:
{hyphae_context}

COMMAND BOARD STATUS:
{board_status}

DUO PROCESS: {process_status}

RECENT DUO OUTPUT (last 80 lines):
{recent_log}

---

Respond with a JSON object:
{{
  "status": "healthy" | "degraded" | "broken" | "dead",
  "diagnosis": "1-2 sentence summary of what's happening",
  "on_track": true | false,
  "action": "none" | "restart" | "directive" | "fix",
  "action_detail": "if action is not 'none', what specifically to do",
  "note_for_hyphae": "one sentence worth remembering for next check"
}}

Be pragmatic. If it's working, say so. If it needs a restart, say restart.
If the Operator is producing real code changes and tests are running, that's healthy even if some steps fail.
The goal is PROGRESS, not perfection."""


def _ask_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=120,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _check_and_act(goal: str, working_dir: str, test_command: str,
                   test_commands: list[str]) -> dict:
    """The core loop: read, think, act."""

    # Gather context
    log_path = _get_duo_log()
    recent_log = _tail_log(log_path, 80) if log_path else "(no log file found)"
    board_status = _get_board_status()
    duo_pid = _get_duo_pid()
    process_status = f"PID {duo_pid} (alive)" if duo_pid else "NOT RUNNING"
    hyphae_facts = _hyphae_recall("ralph duo status cycle progress problems", top_k=5)
    hyphae_context = "\n".join(f"  • {f}" for f in hyphae_facts) if hyphae_facts else "(no prior context)"

    # Ask Claude
    prompt = OVERWATCH_PROMPT.format(
        goal=goal, working_dir=working_dir,
        hyphae_context=hyphae_context, board_status=board_status,
        process_status=process_status, recent_log=recent_log,
    )

    response = _ask_claude(prompt)

    # Parse response
    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        assessment = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        assessment = {
            "status": "unknown", "diagnosis": response[:200],
            "on_track": False, "action": "none",
            "action_detail": "", "note_for_hyphae": "",
        }

    return assessment


# ---------------------------------------------------------------------------
# Ralph main loop
# ---------------------------------------------------------------------------

class Ralph:
    def __init__(self, goal: str, working_dir: str, test_command: str = "",
                 test_commands: list[str] | None = None, model: str = "sonnet"):
        self.goal = goal
        self.working_dir = working_dir
        self.test_command = test_command
        self.test_commands = test_commands or []
        self.model = model
        self._running = False
        self._restarts = 0

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log.info(msg)
        print(f"\033[1;32m[Ralph {ts}]\033[0m {msg}")

    def _ensure_running(self):
        """If Duo isn't running, start it."""
        if not _get_duo_pid():
            self._log("Duo not running — launching")
            _kill_duo()  # clean up orphans
            time.sleep(2)
            pid, log_path = _launch_duo(
                self.goal, self.working_dir,
                self.test_command, self.test_commands, self.model,
            )
            self._restarts += 1
            self._log(f"Launched Duo (PID {pid}), log: {log_path}")
            _hyphae_remember(f"Ralph launched Duo (restart #{self._restarts})")
            time.sleep(30)  # give it time to boot

    def _handle_assessment(self, assessment: dict):
        """Act on Claude's assessment."""
        status = assessment.get("status", "unknown")
        diagnosis = assessment.get("diagnosis", "")
        action = assessment.get("action", "none")
        detail = assessment.get("action_detail", "")
        on_track = assessment.get("on_track", False)
        note = assessment.get("note_for_hyphae", "")

        # Log
        icon = {"healthy": "✓", "degraded": "~", "broken": "✗", "dead": "☠"}.get(status, "?")
        self._log(f"{icon} Status: {status} | On track: {on_track}")
        self._log(f"  Diagnosis: {diagnosis}")

        if note:
            _hyphae_remember(f"Ralph check: {note}")

        # Report to board
        _api("POST", "/api/agent/ralph/heartbeat", {
            "status": "busy",
            "commentary": f"[{status}] {diagnosis[:100]}",
        })

        # Act
        if action == "restart":
            self._log(f"ACTION: Restarting Duo — {detail}")
            _kill_duo()
            time.sleep(5)
            pid, log_path = _launch_duo(
                self.goal, self.working_dir,
                self.test_command, self.test_commands, self.model,
            )
            self._restarts += 1
            self._log(f"Restarted (PID {pid}), log: {log_path}")
            _hyphae_remember(f"Ralph restarted Duo: {detail}")
            time.sleep(30)

        elif action == "directive":
            self._log(f"ACTION: Sending directive — {detail}")
            _api("POST", "/api/agent/director/directive", {
                "message": f"⚠️ RALPH OVERRIDE: {detail}",
                "from": "ralph", "priority": "urgent",
            })
            _hyphae_remember(f"Ralph sent directive: {detail}")

        elif action == "fix":
            self._log(f"ACTION: Fix needed — {detail}")
            # For fixes, use Claude to actually apply them
            fix_prompt = (
                f"You are Ralph, an overwatch agent. You diagnosed this problem with "
                f"the Duo agent system:\n\n{diagnosis}\n\nFix: {detail}\n\n"
                f"Apply the fix. The Duo code is at /home/om/openkeel/openkeel/agents/. "
                f"The SC2 project is at {self.working_dir}. "
                f"Make the minimal change needed, then say DONE."
            )
            try:
                result = subprocess.run(
                    ["claude", "-p", "--model", "sonnet", "--output-format", "text",
                     "--dangerously-skip-permissions"],
                    input=fix_prompt, capture_output=True, text=True,
                    timeout=180, cwd="/home/om/openkeel",
                )
                self._log(f"Fix applied: {result.stdout[-200:]}")
                _hyphae_remember(f"Ralph applied fix: {detail}")
            except Exception as e:
                self._log(f"Fix failed: {e}")

        else:
            self._log("No action needed.")

    def run(self):
        self._running = True
        self._log(f"Ralph online — overwatch mode")
        self._log(f"Goal: {self.goal[:80]}")
        self._log(f"Checking every {CHECK_INTERVAL}s")

        _api("POST", "/api/agent/register", {
            "name": "ralph", "capabilities": "overwatch, restart, diagnosis",
            "model": "haiku",
        })
        _hyphae_remember(f"Ralph started overwatch for: {self.goal[:100]}")

        # Initial launch
        self._ensure_running()

        while self._running:
            try:
                self._log("--- Checking in ---")

                # Make sure it's at least running
                self._ensure_running()

                # Read, think, act
                assessment = _check_and_act(
                    self.goal, self.working_dir,
                    self.test_command, self.test_commands,
                )
                self._handle_assessment(assessment)

            except Exception as e:
                self._log(f"Error in check loop: {e}")

            time.sleep(CHECK_INTERVAL)

        self._log("Ralph shutting down.")

    def stop(self):
        self._running = False


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="ralph", description="Overwatch for Duo")
    parser.add_argument("goal", help="Goal for the Duo system")
    parser.add_argument("-d", "--working-dir", default=os.getcwd())
    parser.add_argument("-m", "--model", default="sonnet")
    parser.add_argument("--test-command", default="")
    parser.add_argument("--test", action="append", dest="test_commands", default=[])
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds")
    args = parser.parse_args()

    global CHECK_INTERVAL
    CHECK_INTERVAL = args.interval

    ralph = Ralph(
        goal=args.goal, working_dir=args.working_dir,
        test_command=args.test_command, test_commands=args.test_commands,
        model=args.model,
    )

    def _sigint(sig, frame):
        print("\n[Ralph] Shutting down...")
        ralph.stop()
        _kill_duo()

    signal.signal(signal.SIGINT, _sigint)
    ralph.run()


if __name__ == "__main__":
    main()
