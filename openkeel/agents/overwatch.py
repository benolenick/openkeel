"""Overwatch Agent — keeps the council running and on track.

Overwatch is a full council member, not just a watchdog. It:
1. Monitors all agent liveness (heartbeats, Board, Hyphae)
2. Reads logs and diagnoses WHY things are broken
3. Detects loops (same failure 3+ times) and forces new approaches
4. Restarts the Duo system when needed
5. Sends course corrections to the Director
6. Self-repairs the orchestration code when the problem is systemic
7. Persists every diagnosis to Hyphae for cross-session continuity
8. Reports to the Board so the dashboard shows system health

Overwatch runs on a 5-minute heartbeat loop. Each check:
  - Read the last 80 lines of the Duo log
  - Read agent status from the Board
  - Recall recent Overwatch notes from Hyphae
  - Feed it all to Claude (Haiku — fast/cheap)
  - Act on the diagnosis: none, restart, directive, fix

Usage:
    python -m openkeel.agents.overwatch \\
      --goal "..." --working-dir /path/to/project \\
      --test-command "bash test_game.sh" --interval 300
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [overwatch] %(message)s")
log = logging.getLogger("overwatch")

BOARD_URL = "http://127.0.0.1:8200"
HYPHAE_URL = "http://127.0.0.1:8100"
AGENT_NAME = "overwatch"
CHECK_INTERVAL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Infrastructure helpers
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
        body = json.dumps({"text": fact, "source": AGENT_NAME}).encode()
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


def _board_healthy() -> bool:
    """Check if the Command Board is reachable."""
    try:
        with urllib.request.urlopen(f"{BOARD_URL}/api/stats", timeout=5):
            return True
    except Exception:
        return False


def _hyphae_healthy() -> bool:
    """Check if Hyphae is reachable."""
    try:
        body = json.dumps({"query": "test", "top_k": 1}).encode()
        req = urllib.request.Request(f"{HYPHAE_URL}/recall", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _get_agents() -> dict[str, dict]:
    result = _api("GET", "/api/agents")
    if not result or not isinstance(result, list):
        return {}
    return {a["name"]: a for a in result
            if a.get("name") in ("director", "operator", "critic", "tester", AGENT_NAME)}


def _get_commentary(limit: int = 15) -> list[str]:
    result = _api("GET", f"/api/agent/commentary?limit={limit}")
    if not result or not isinstance(result, list):
        return []
    return [f"[{c.get('agent', '?')}] {c.get('text', c.get('message', ''))[:120]}"
            for c in result]


def _get_duo_log() -> str | None:
    logs = sorted(Path("/tmp").glob("duo_overwatch_*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        # Fall back to ralph logs for continuity
        logs = sorted(Path("/tmp").glob("duo_ralph_*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return str(logs[0]) if logs else None


def _tail_log(path: str, lines: int = 80) -> str:
    try:
        result = subprocess.run(["tail", "-n", str(lines), path],
                                capture_output=True, text=True, timeout=5)
        return result.stdout
    except Exception:
        return "(could not read log)"


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
                test_commands: list[str], model: str = "sonnet") -> tuple[int, str]:
    cmd = [sys.executable, "-m", "openkeel.agents.duo", goal,
           "-d", working_dir, "-m", model, "--poll", "30"]
    if test_command:
        cmd.extend(["--test-command", test_command])
    for tc in test_commands:
        cmd.extend(["--test", tc])

    log_path = f"/tmp/duo_overwatch_{int(time.time())}.log"
    log_file = open(log_path, "w")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    return proc.pid, log_path


# ---------------------------------------------------------------------------
# The brain — Claude reads the situation and decides what to do
# ---------------------------------------------------------------------------

OVERWATCH_SYSTEM = """\
You are the Overwatch agent on a multi-agent council building a StarCraft II AI bot.

THE GOAL: {goal}

THE COUNCIL:
- Director: plans improvement cycles (3-5 code changes), runs tests between cycles, analyzes results
- Operator: executes code changes via Claude CLI (has full file/bash permissions)
- Critic: reviews each code change for quality before Director advances
- Tester: analyzes game behavior after each test run (reads game logs, reports what the bot actually did)
- Overwatch (you): keeps everyone running and on track

THE LOOP: plan code changes → Operator writes code → Critic reviews → Director runs test → Tester analyzes game → Director replans → repeat forever

YOUR JOB — answer these questions about the current state:
1. ALIVE? Is the Duo process running? Are agents heartbeating?
2. INFRASTRUCTURE? Is the Command Board up? Is Hyphae up?
3. PROGRESSING? Are cycles completing? Steps advancing? Tests running?
4. ON TRACK? Is the work moving toward the goal, or stuck on meta-problems?
5. LOOPING? Same error repeating? Agents talking past each other?
6. QUALITY? Is the Operator producing real code changes with diffs? Is the Critic catching real issues?

PREVIOUS OVERWATCH NOTES:
{overwatch_history}

INFRASTRUCTURE:
  Board: {board_status}
  Hyphae: {hyphae_status}
  Duo process: {process_status}

REGISTERED AGENTS:
{agent_status}

RECENT ACTIVITY (Board commentary):
{commentary}

DUO LOG (last 80 lines):
{duo_log}

---

Respond with a JSON object:
{{
  "status": "healthy" | "degraded" | "broken" | "dead",
  "diagnosis": "2-3 sentence summary of what's happening RIGHT NOW",
  "on_track": true | false,
  "progress_note": "what concrete progress was made since last check",
  "action": "none" | "restart" | "directive" | "fix",
  "action_detail": "if action is not 'none', exactly what to do and why",
  "note_for_next_check": "what should I look for next time I check in"
}}

Be concise and pragmatic. Healthy systems need no action — just note progress.
Broken systems need specific fixes, not generic advice.
If the Operator is producing real code changes and tests are running, that's healthy even if some steps fail — failure is part of learning."""


def _ask_claude(prompt: str, model: str = "haiku") -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=120,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Overwatch main class
# ---------------------------------------------------------------------------

class Overwatch:
    def __init__(self, goal: str, working_dir: str, test_command: str = "",
                 test_commands: list[str] | None = None, model: str = "sonnet"):
        self.goal = goal
        self.working_dir = working_dir
        self.test_command = test_command
        self.test_commands = test_commands or []
        self.model = model
        self._running = False
        self._restarts = 0
        self._checks = 0

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log.info(msg)
        print(f"\033[1;37m[Overwatch {ts}]\033[0m {msg}")
        # Also broadcast to dashboard
        try:
            from openkeel.agents.dashboard import broadcast_log
            broadcast_log("overwatch", msg)
        except Exception:
            pass

    def _ensure_running(self):
        if not _get_duo_pid():
            self._log("Duo not running — launching")
            _kill_duo()
            time.sleep(2)
            pid, log_path = _launch_duo(
                self.goal, self.working_dir,
                self.test_command, self.test_commands, self.model,
            )
            self._restarts += 1
            self._log(f"Launched Duo (PID {pid}), log: {log_path}")
            _hyphae_remember(f"Overwatch launched Duo (restart #{self._restarts})")
            time.sleep(30)

    def _gather_context(self) -> dict:
        """Gather all context for the diagnosis."""
        return {
            "board_status": "UP" if _board_healthy() else "DOWN",
            "hyphae_status": "UP" if _hyphae_healthy() else "DOWN",
            "process_status": f"PID {_get_duo_pid()} (alive)" if _get_duo_pid() else "NOT RUNNING",
            "agents": _get_agents(),
            "commentary": _get_commentary(15),
            "duo_log": _tail_log(_get_duo_log() or "", 80),
            "overwatch_history": _hyphae_recall("overwatch diagnosis progress status", top_k=5),
        }

    def _diagnose(self, ctx: dict) -> dict:
        """Ask Claude to assess the situation."""
        agent_lines = []
        for name, a in ctx["agents"].items():
            age = time.time() - a.get("last_heartbeat", 0)
            agent_lines.append(f"  {name}: {a.get('effective_status', '?')} ({age:.0f}s ago)")

        prompt = OVERWATCH_SYSTEM.format(
            goal=self.goal,
            board_status=ctx["board_status"],
            hyphae_status=ctx["hyphae_status"],
            process_status=ctx["process_status"],
            agent_status="\n".join(agent_lines) or "  (none registered)",
            commentary="\n".join(ctx["commentary"]) or "  (no commentary)",
            duo_log=ctx["duo_log"],
            overwatch_history="\n".join(f"  • {h}" for h in ctx["overwatch_history"]) or "  (first check)",
        )

        response = _ask_claude(prompt, model="haiku")

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
        except (json.JSONDecodeError, IndexError):
            return {
                "status": "unknown", "diagnosis": response[:200],
                "on_track": False, "action": "none",
                "action_detail": "", "progress_note": "",
                "note_for_next_check": "",
            }

    def _act(self, assessment: dict):
        """Execute the diagnosed action."""
        status = assessment.get("status", "unknown")
        diagnosis = assessment.get("diagnosis", "")
        action = assessment.get("action", "none")
        detail = assessment.get("action_detail", "")
        on_track = assessment.get("on_track", False)
        progress = assessment.get("progress_note", "")
        next_check = assessment.get("note_for_next_check", "")

        icon = {"healthy": "✓", "degraded": "~", "broken": "✗", "dead": "☠"}.get(status, "?")
        self._log(f"{icon} {status.upper()} | On track: {on_track}")
        self._log(f"  {diagnosis}")
        if progress:
            self._log(f"  Progress: {progress}")

        # Persist to Hyphae
        _hyphae_remember(
            f"Overwatch check #{self._checks}: {status}. {diagnosis[:150]}. "
            f"Progress: {progress[:100]}. Next: {next_check[:80]}"
        )

        # Report to Board (auto-re-register on 404)
        result = _api("POST", f"/api/agent/{AGENT_NAME}/heartbeat", {
            "status": "busy",
            "commentary": f"[{status}] {diagnosis[:100]}",
        })
        if result is None:
            _api("POST", "/api/agent/register", {
                "name": AGENT_NAME,
                "capabilities": "monitoring, diagnosis, restart, course-correction",
                "model": "haiku",
            })
            _api("POST", f"/api/agent/{AGENT_NAME}/heartbeat", {
                "status": "busy",
                "commentary": f"[{status}] {diagnosis[:100]}",
            })

        if action == "restart":
            self._log(f"ACTION: RESTART — {detail}")
            _kill_duo()
            time.sleep(5)
            pid, log_path = _launch_duo(
                self.goal, self.working_dir,
                self.test_command, self.test_commands, self.model,
            )
            self._restarts += 1
            self._log(f"Restarted (PID {pid})")
            _hyphae_remember(f"Overwatch restarted Duo: {detail}")
            time.sleep(30)

        elif action == "directive":
            self._log(f"ACTION: DIRECTIVE — {detail}")
            _api("POST", "/api/agent/director/directive", {
                "message": f"⚠️ OVERWATCH: {detail}",
                "from": AGENT_NAME, "priority": "urgent",
            })
            _hyphae_remember(f"Overwatch sent directive: {detail}")

        elif action == "fix":
            self._log(f"ACTION: FIX — {detail}")
            fix_prompt = (
                f"You are the Overwatch agent. You diagnosed this problem:\n\n"
                f"{diagnosis}\n\nFix needed: {detail}\n\n"
                f"The Duo agent code is at /home/om/openkeel/openkeel/agents/. "
                f"The SC2 project is at {self.working_dir}. "
                f"Make the minimal fix. Say DONE when finished."
            )
            try:
                result = subprocess.run(
                    ["claude", "-p", "--model", "sonnet", "--output-format", "text",
                     "--dangerously-skip-permissions"],
                    input=fix_prompt, capture_output=True, text=True,
                    timeout=180, cwd="/home/om/openkeel",
                )
                self._log(f"Fix applied: {result.stdout[-200:].strip()}")
                _hyphae_remember(f"Overwatch applied fix: {detail}")
            except Exception as e:
                self._log(f"Fix failed: {e}")

        else:
            if next_check:
                self._log(f"  Next check: {next_check}")

    def run(self):
        self._running = True
        self._log(f"Overwatch online")
        self._log(f"Goal: {self.goal[:80]}")
        self._log(f"Check interval: {CHECK_INTERVAL}s")

        _api("POST", "/api/agent/register", {
            "name": AGENT_NAME,
            "capabilities": "monitoring, diagnosis, restart, course-correction, self-repair",
            "model": "haiku",
        })
        _hyphae_remember(f"Overwatch started for: {self.goal[:100]}")

        # Initial launch
        self._ensure_running()

        while self._running:
            self._checks += 1
            try:
                self._log(f"--- Check #{self._checks} ---")
                self._ensure_running()
                ctx = self._gather_context()
                assessment = self._diagnose(ctx)
                self._act(assessment)
            except Exception as e:
                self._log(f"Error: {e}")

            time.sleep(CHECK_INTERVAL)

        self._log("Overwatch shutting down.")

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(prog="overwatch", description="Council Overwatch agent")
    parser.add_argument("goal", help="Goal for the Duo system")
    parser.add_argument("-d", "--working-dir", default=os.getcwd())
    parser.add_argument("-m", "--model", default="sonnet")
    parser.add_argument("--test-command", default="")
    parser.add_argument("--test", action="append", dest="test_commands", default=[])
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    global CHECK_INTERVAL
    CHECK_INTERVAL = args.interval

    ow = Overwatch(
        goal=args.goal, working_dir=args.working_dir,
        test_command=args.test_command, test_commands=args.test_commands,
        model=args.model,
    )

    def _sigint(sig, frame):
        print("\n[Overwatch] Shutting down...")
        ow.stop()
        _kill_duo()

    signal.signal(signal.SIGINT, _sigint)
    ow.run()


if __name__ == "__main__":
    main()
