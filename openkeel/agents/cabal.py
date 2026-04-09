"""Cabal — Human-in-the-loop improvement system.

The loop:
1. Trio makes code changes (Director→Operator→Critic)
2. Game runs headless (fast)
3. Briefing generated — plain language, metrics, trend
4. Replay saved — you watch at your own pace
5. PAUSE — wait for your direction
6. Your input becomes the next goal
7. Repeat

Usage:
    python -m openkeel.agents.cabal \\
      "Beat Hard Protoss AI" \\
      -d /home/om/sc2-commander \\
      --test-command "bash test_game.sh --headless --difficulty hard"

    # Or autonomous mode (no human pause):
    python -m openkeel.agents.cabal --auto ...
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from openkeel.agents.trio import Trio, _claude, _hyphae, _hyphae_recall, _agent_terminal

logger = logging.getLogger("openkeel.cabal")

HYPHAE_URL = "http://127.0.0.1:8100"
BOARD_URL = "http://127.0.0.1:8200"

WINE = "/home/om/.local/share/lutris/runners/wine/wine-ge-8-26-x86_64/bin/wine"
WINEPREFIX = "/home/om/Games/battlenet"
SC2_EXE = "/home/om/Games/battlenet/drive_c/Program Files (x86)/StarCraft II/Versions/Base95841/SC2_x64.exe"
REPLAY_DIR = "/home/om/sc2-commander/data/replays/bot_games"


def _board_post(path: str, data: dict) -> bool:
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(f"{BOARD_URL}{path}", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SC2 management
# ---------------------------------------------------------------------------

def _kill_sc2():
    try:
        subprocess.run(["pkill", "-f", "SC2_x64"], timeout=5, capture_output=True)
        subprocess.run(["pkill", "-f", "StarCraft"], timeout=5, capture_output=True)
    except Exception:
        pass


def _get_latest_replay() -> str | None:
    replay_dir = Path(REPLAY_DIR)
    if not replay_dir.exists():
        return None
    replays = sorted(replay_dir.glob("*.SC2Replay"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    return str(replays[0]) if replays else None


def _open_replay(replay_path: str):
    """Launch SC2 replay viewer."""
    try:
        env = os.environ.copy()
        env["WINE"] = WINE
        env["WINEPREFIX"] = WINEPREFIX
        subprocess.Popen(
            [WINE, SC2_EXE, "-replay", replay_path],
            env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"  (couldn't open replay: {e})")


# ---------------------------------------------------------------------------
# Game runner + Tester
# ---------------------------------------------------------------------------

def run_test(test_command: str, working_dir: str) -> str:
    _kill_sc2()
    try:
        result = subprocess.run(
            ["bash", "-c", test_command],
            capture_output=True, text=True, timeout=600,
            cwd=working_dir,
        )
        output = result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr[-1000:]}"
        output += f"\nExit code: {result.returncode}"
        return output
    except subprocess.TimeoutExpired:
        return "GAME TIMEOUT (10 min)"
    except Exception as e:
        return f"GAME ERROR: {e}"


TESTER_SYSTEM = """\
You are a Tester agent. You just watched an SC2 bot play a game. Analyze what happened.

Report as JSON:
{
  "result": "victory" | "defeat" | "crash" | "timeout",
  "what_happened": "2-3 sentence narrative of the game",
  "critical_bugs": ["game-breaking issues, max 3"],
  "top_3_fixes": ["most impactful fix first", "second", "third"],
  "max_supply": N,
  "game_duration": "short/medium/long"
}

Be brutally honest. The goal is to identify what to fix next."""


def analyze_test(test_output: str) -> dict:
    prev = _hyphae_recall("tester game analysis behavior", top_k=3)
    prev_text = "\n".join(f"- {p}" for p in prev) if prev else "(first test)"

    prompt = f"GAME OUTPUT:\n{test_output[-3000:]}\n\nPREVIOUS GAMES:\n{prev_text}"
    response = _claude(TESTER_SYSTEM, prompt, model="sonnet", timeout=120)
    _agent_terminal("tester", response[-1500:] if len(response) > 1500 else response)

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        report = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        report = {"result": "unknown", "what_happened": response[:200],
                  "critical_bugs": [], "top_3_fixes": []}

    _hyphae("remember", {
        "text": f"Tester: {report.get('result', '?')} — {report.get('what_happened', '')[:150]}",
        "source": "tester",
    })
    return report


# ---------------------------------------------------------------------------
# Metrics ledger
# ---------------------------------------------------------------------------

def record_metrics(working_dir: str, cycle: int, trio_result, test_report: dict,
                   test_output: str) -> dict:
    metrics_path = os.path.join(working_dir, "metrics.json")

    max_supply = test_report.get("max_supply", 0)
    ops_completed = test_output.count("finished: COMPLETED")
    ops_failed = test_output.count("FAILED")
    ops_total = ops_completed + ops_failed

    for m in re.finditer(r'supply:\s*(\d+)', test_output):
        s = int(m.group(1))
        if s > max_supply:
            max_supply = s

    try:
        with open(metrics_path) as f:
            ledger = json.loads(f.read())
    except (FileNotFoundError, json.JSONDecodeError):
        ledger = []

    delta = {}
    if ledger:
        prev = ledger[-1]
        pm = prev.get("metrics", {})
        delta["supply"] = f"{pm.get('max_supply', 0)} → {max_supply}"
        delta["result"] = f"{prev.get('game_result', '?')} → {test_report.get('result', '?')}"

    entry = {
        "cycle": cycle,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "game_result": test_report.get("result", "unknown"),
        "changes": trio_result.changes[:5],
        "what_happened": test_report.get("what_happened", ""),
        "metrics": {
            "max_supply": max_supply,
            "ops_completed": ops_completed,
            "ops_failed": ops_failed,
            "ops_success_rate": f"{ops_completed}/{ops_total}" if ops_total else "0/0",
            "critical_bugs": test_report.get("critical_bugs", []),
        },
        "delta": delta,
        "top_fixes": test_report.get("top_3_fixes", []),
        "replay": _get_latest_replay() or "",
    }

    ledger.append(entry)
    with open(metrics_path, "w") as f:
        json.dump(ledger, f, indent=2)

    return entry


# ---------------------------------------------------------------------------
# Briefing — the human-readable summary
# ---------------------------------------------------------------------------

BRIEFING_SYSTEM = """\
You are writing a briefing for a human who is directing an AI bot improvement project.
They just ran a StarCraft II game. Write a SHORT, clear briefing they can read in 30 seconds.

Use this exact format (fill in the blanks):

RESULT: [victory/defeat/crash/timeout]
DURATION: [short/medium/long]
PEAK SUPPLY: [number]

WHAT HAPPENED:
[2-3 sentences, plain English, what the bot actually did in the game]

WHAT IMPROVED:
[1-2 things that got better vs last game, or "First game" if no history]

WHAT'S BROKEN:
[1-3 specific problems, ranked by impact]

SUGGESTED NEXT MOVES:
1. [most impactful change]
2. [second]
3. [third]

No code, no file paths, no technical jargon unless necessary. Write like you're briefing a coach, not a programmer."""


def generate_briefing(cycle: int, test_report: dict, metrics_entry: dict,
                      trio_result) -> str:
    """Generate a human-readable briefing."""
    prompt = (
        f"CYCLE: {cycle}\n"
        f"GAME RESULT: {test_report.get('result', '?')}\n"
        f"WHAT HAPPENED: {test_report.get('what_happened', '?')}\n"
        f"MAX SUPPLY: {metrics_entry['metrics']['max_supply']}\n"
        f"OPS: {metrics_entry['metrics']['ops_success_rate']}\n"
        f"BUGS: {test_report.get('critical_bugs', [])}\n"
        f"FIXES SUGGESTED: {test_report.get('top_3_fixes', [])}\n"
        f"CHANGES MADE THIS CYCLE: {trio_result.changes}\n"
        f"TREND: {metrics_entry.get('delta', {})}\n"
    )
    response = _claude(BRIEFING_SYSTEM, prompt, model="sonnet", timeout=60)
    return response.strip() if response else f"Cycle {cycle}: {test_report.get('result', '?')}"


# ---------------------------------------------------------------------------
# Briefing file — written to disk for dashboard/persistence
# ---------------------------------------------------------------------------

def save_briefing(working_dir: str, cycle: int, briefing: str, replay_path: str):
    """Save briefing to a file the dashboard can read."""
    briefing_dir = os.path.join(working_dir, "briefings")
    os.makedirs(briefing_dir, exist_ok=True)

    data = {
        "cycle": cycle,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "briefing": briefing,
        "replay": replay_path,
        "status": "awaiting_input",
    }

    # Current briefing (dashboard reads this)
    with open(os.path.join(briefing_dir, "current.json"), "w") as f:
        json.dump(data, f, indent=2)

    # History
    with open(os.path.join(briefing_dir, f"cycle_{cycle:03d}.json"), "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Cabal — the main loop
# ---------------------------------------------------------------------------

OVERWATCH_SYSTEM = """\
You are the Overwatch agent governing an improvement loop for an SC2 bot.
Given the original goal, what was just changed, and the test results,
decide the goal for the NEXT improvement cycle.

Be specific. Reference exact files, line numbers, and behaviors from the test output.
If the test crashed, the next goal should fix the crash.
If the test ran but lost, focus on the biggest tactical weakness.

Respond with a single paragraph — the goal for the next Trio run. No JSON, no formatting."""


class Cabal:
    def __init__(self, goal: str, working_dir: str, test_command: str,
                 model: str = "sonnet", max_cycles: int = 0,
                 auto: bool = False):
        self.goal = goal
        self.working_dir = working_dir
        self.test_command = test_command
        self.model = model
        self.max_cycles = max_cycles
        self.auto = auto  # If True, no human pause
        self._running = False
        self.cycle = 0
        self.history: list[str] = []

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\033[1;37m[Overwatch {ts}]\033[0m {msg}")
        logger.info("[overwatch] %s", msg)
        result = _board_post("/api/agent/overwatch/heartbeat",
                             {"status": "busy", "commentary": msg[:120]})
        if not result:
            _board_post("/api/agent/register",
                        {"name": "overwatch", "capabilities": "governance", "model": "sonnet"})

    def _decide_next_goal(self, trio_result, test_report: dict) -> str:
        prompt = (
            f"ORIGINAL GOAL: {self.goal}\n\n"
            f"CHANGES JUST MADE:\n{trio_result.summary}\n\n"
            f"TEST RESULT: {test_report.get('result', '?')}\n"
            f"WHAT HAPPENED: {test_report.get('what_happened', '?')}\n"
            f"CRITICAL BUGS: {test_report.get('critical_bugs', [])}\n"
            f"TOP FIXES: {test_report.get('top_3_fixes', [])}\n\n"
            f"CYCLE HISTORY:\n" + "\n".join(f"- {h}" for h in self.history[-5:])
        )
        response = _claude(OVERWATCH_SYSTEM, prompt, model="sonnet", timeout=120)
        _agent_terminal("overwatch", response[-1500:] if len(response) > 1500 else response)

        if response and len(response.strip()) > 20:
            return response.strip()

        bugs = test_report.get("critical_bugs", [])
        fixes = test_report.get("top_3_fixes", [])
        if bugs:
            return f"Fix critical bugs: {'. '.join(bugs[:3])}"
        if fixes:
            return f"Apply fixes: {'. '.join(fixes[:3])}"
        return self.goal

    def _present_briefing(self, briefing: str, replay_path: str) -> str:
        """Show briefing and wait for human input. Returns the user's direction."""
        print()
        print("\033[1;35m" + "=" * 60)
        print("  BRIEFING")
        print("=" * 60 + "\033[0m")
        print()
        print(briefing)
        print()

        if replay_path:
            print(f"\033[1;33m📹 Replay: {replay_path}\033[0m")
            print(f"\033[33m   Open replay? [y/N]\033[0m ", end="", flush=True)
            try:
                answer = input().strip().lower()
                if answer == "y":
                    _open_replay(replay_path)
                    print("   Replay opening in SC2... watch it, then come back here.")
                    print()
            except EOFError:
                pass

        print("\033[1;35m" + "-" * 60 + "\033[0m")
        print("\033[1mWhat should the next cycle focus on?\033[0m")
        print("\033[33m(type your direction, or 'auto' for agent decision, or 'quit')\033[0m")
        print()

        try:
            user_input = input("> ").strip()
        except EOFError:
            user_input = "auto"

        if user_input.lower() == "quit":
            self._running = False
            return ""
        elif user_input.lower() == "auto" or not user_input:
            return ""  # Let Overwatch decide
        else:
            return user_input

    def run(self):
        self._running = True
        self._log("=== CABAL ONLINE ===")
        self._log(f"Goal: {self.goal[:80]}")
        self._log(f"Mode: {'autonomous' if self.auto else 'human-in-the-loop'}")

        _board_post("/api/agent/register",
                    {"name": "overwatch", "capabilities": "governance", "model": "sonnet"})
        _hyphae("remember", {"text": f"Cabal started: {self.goal[:150]}", "source": "overwatch"})

        current_goal = self.goal

        while self._running:
            self.cycle += 1
            self._log(f"")
            self._log(f"{'=' * 50}")
            self._log(f"CYCLE {self.cycle}")
            self._log(f"{'=' * 50}")
            self._log(f"Goal: {current_goal[:100]}")

            # --- TRIO ---
            self._log("Starting Trio...")
            trio = Trio(
                goal=current_goal,
                working_dir=self.working_dir,
                model=self.model,
                history=self.history,
            )
            trio_result = trio.run()
            self._log(f"Trio done: {trio_result.summary[:100]}")
            self.history.append(f"[Cycle {self.cycle}] {trio_result.summary[:150]}")

            if not self._running:
                break

            # --- TEST ---
            self._log("Running test (headless)...")
            _board_post("/api/agent/register",
                        {"name": "tester", "capabilities": "testing", "model": "sonnet"})
            _board_post("/api/agent/tester/heartbeat",
                        {"status": "busy", "commentary": "Running game..."})

            test_output = run_test(self.test_command, self.working_dir)
            test_report = analyze_test(test_output)

            result_str = test_report.get("result", "?")
            narrative = test_report.get("what_happened", "")
            self._log(f"Test: {result_str} — {narrative[:100]}")
            self.history.append(f"[Cycle {self.cycle} test] {result_str}: {narrative[:100]}")

            # --- METRICS ---
            metrics_entry = record_metrics(
                self.working_dir, self.cycle, trio_result, test_report, test_output,
            )
            self._log(f"Metrics: supply={metrics_entry['metrics']['max_supply']}")

            # Dashboard cycle history
            _board_post("/api/agent/overwatch/commentary", {
                "text": f"Cycle {self.cycle} done. Test {result_str.upper()}. "
                        f"Supply: {metrics_entry['metrics']['max_supply']}"
            })

            # --- BRIEFING ---
            replay_path = _get_latest_replay() or ""
            briefing = generate_briefing(self.cycle, test_report, metrics_entry, trio_result)
            save_briefing(self.working_dir, self.cycle, briefing, replay_path)

            # --- HUMAN INPUT or AUTO ---
            if not self.auto:
                user_direction = self._present_briefing(briefing, replay_path)

                if not self._running:
                    break

                if user_direction:
                    # Human gave direction — use it as the goal
                    current_goal = user_direction
                    self._log(f"Human direction: {current_goal[:80]}")
                    _hyphae("remember", {
                        "text": f"Human directed cycle {self.cycle + 1}: {current_goal[:150]}",
                        "source": "human",
                    })
                else:
                    # Auto — Overwatch decides
                    if result_str == "victory":
                        self._log("🎉 VICTORY!")
                        _hyphae("remember", {
                            "text": f"Cabal victory after {self.cycle} cycles",
                            "source": "overwatch",
                        })
                        break
                    current_goal = self._decide_next_goal(trio_result, test_report)
                    self._log(f"Overwatch goal: {current_goal[:80]}")
            else:
                # Autonomous mode
                if result_str == "victory":
                    self._log("🎉 VICTORY!")
                    break
                if self.max_cycles and self.cycle >= self.max_cycles:
                    self._log(f"Max cycles ({self.max_cycles}) reached.")
                    break
                current_goal = self._decide_next_goal(trio_result, test_report)
                self._log(f"Next goal: {current_goal[:80]}")

            _hyphae("remember", {
                "text": f"Cabal cycle {self.cycle}: {result_str}. Next: {current_goal[:100]}",
                "source": "overwatch",
            })

            time.sleep(3)

        self._log(f"=== CABAL DONE — {self.cycle} cycles ===")
        _kill_sc2()

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(prog="cabal",
                                     description="Human-in-the-loop improvement system")
    parser.add_argument("goal", help="The improvement goal")
    parser.add_argument("-d", "--working-dir", default=os.getcwd())
    parser.add_argument("-m", "--model", default="sonnet")
    parser.add_argument("--test-command", required=True)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--auto", action="store_true",
                        help="Autonomous mode — no human pause between cycles")
    args = parser.parse_args()

    cabal = Cabal(
        goal=args.goal, working_dir=args.working_dir,
        test_command=args.test_command, model=args.model,
        max_cycles=args.max_cycles, auto=args.auto,
    )

    def _sigint(sig, frame):
        print("\n[Cabal] Stopping...")
        cabal.stop()

    signal.signal(signal.SIGINT, _sigint)
    cabal.run()


if __name__ == "__main__":
    main()
