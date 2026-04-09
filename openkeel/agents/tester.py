"""Tester Agent — watches what the bot actually does in-game and reports behavior.

The Tester is the eyes of the system. After each test game it:
1. Reads the game log
2. Analyzes what the bot actually DID (not what the code says it should do)
3. Produces a structured behavior report: timeline, mistakes, metrics
4. Feeds this back to the Director so the next cycle fixes real problems

The Critic reviews code quality. The Tester reviews game behavior.
These are different jobs — code can be clean and still lose badly.

The Tester runs AFTER test_game.sh, reads the log + replay path, and
produces a report that the Director uses for cycle analysis.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("openkeel.tester")

BOARD_URL = "http://127.0.0.1:8200"
HYPHAE_URL = "http://127.0.0.1:8100"
TESTER_NAME = "tester"


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
        body = json.dumps({"text": fact, "source": "tester"}).encode()
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


def _ask_claude(system: str, prompt: str, model: str = "sonnet") -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text"],
            input=f"{system}\n\n---\n\n{prompt}",
            capture_output=True, text=True, timeout=120,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _broadcast(msg: str):
    try:
        from openkeel.agents.dashboard import broadcast_log
        broadcast_log("tester", msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Log parsing — extract structured data from raw game log
# ---------------------------------------------------------------------------

def parse_game_log(log_path: str) -> dict:
    """Parse an SC2 game log into structured data."""
    if not os.path.exists(log_path):
        return {"error": f"Log not found: {log_path}"}

    with open(log_path) as f:
        lines = f.readlines()

    data = {
        "total_lines": len(lines),
        "game_started": False,
        "game_result": "unknown",
        "replay_path": "",
        "operations": [],        # {name, status, phase_at_fail}
        "strategies": [],        # strategy log messages with timestamps
        "expansions": 0,         # expand attempts
        "errors": [],            # errors/exceptions
        "warnings": [],          # warnings
        "max_supply": 0,
        "final_minerals": 0,
        "final_bases": 0,
        "timeline": [],          # key events in order
    }

    for line in lines:
        line = line.strip()

        # Game start/end
        if "Starting game:" in line:
            data["game_started"] = True
            data["timeline"].append(("START", line.split("INFO:")[-1].strip() if "INFO:" in line else line[-80:]))
        if "Result for player 1" in line:
            if "Victory" in line:
                data["game_result"] = "victory"
            elif "Defeat" in line:
                data["game_result"] = "defeat"
            elif "Tie" in line:
                data["game_result"] = "tie"

        # Replay
        if "Saved replay to" in line:
            data["replay_path"] = line.split("Saved replay to ")[-1].strip()

        # Operations
        if "STARTED" in line and "phase:" in line:
            op_name = ""
            for part in line.split("["):
                if part.startswith("op_"):
                    op_name = part.split("]")[0]
                    break
            if op_name:
                data["operations"].append({"name": op_name, "status": "started"})
                data["timeline"].append(("OP_START", op_name))

        if "FAILED" in line and ("phase" in line or "timed out" in line):
            op_name = ""
            for part in line.split("["):
                if part.startswith("op_"):
                    op_name = part.split("]")[0]
                    break
            if op_name:
                for op in reversed(data["operations"]):
                    if op["name"] == op_name:
                        op["status"] = "failed"
                        reason = line.split("FAILED:")[-1].strip() if "FAILED:" in line else "unknown"
                        op["reason"] = reason[:100]
                        break
                data["timeline"].append(("OP_FAIL", f"{op_name}: {reason[:60]}"))

        if "finished: COMPLETED" in line or "finished: SUCCESS" in line:
            op_name = ""
            for part in line.split("["):
                if part.startswith("op_"):
                    op_name = part.split("]")[0]
                    break
            if op_name:
                for op in reversed(data["operations"]):
                    if op["name"] == op_name:
                        op["status"] = "completed"
                        break
                data["timeline"].append(("OP_DONE", op_name))

        # Strategy
        if "Strategy:" in line:
            msg = line.split("Strategy:")[-1].strip()
            if not data["strategies"] or data["strategies"][-1] != msg:
                data["strategies"].append(msg)

        # Supply tracking
        if "supply:" in line.lower():
            import re
            m = re.search(r'supply[:\s]+(\d+)', line.lower())
            if m:
                s = int(m.group(1))
                if s > data["max_supply"]:
                    data["max_supply"] = s

        # Expansion spam
        if "Expanding" in line:
            data["expansions"] += 1

        # Minerals/bases
        if "minerals:" in line:
            import re
            m = re.search(r'minerals:\s*(\d+)', line)
            if m:
                data["final_minerals"] = int(m.group(1))
        if "bases:" in line:
            import re
            m = re.search(r'bases:\s*(\d+)', line)
            if m:
                data["final_bases"] = int(m.group(1))

        # Errors
        if "ERROR" in line or "Exception" in line or "Traceback" in line:
            data["errors"].append(line[-150:])

        # Warnings (but not the noisy ones)
        if "WARNING" in line and "timed out" in line:
            data["warnings"].append(line[-150:])

    # Summary stats
    op_stats = {"started": 0, "failed": 0, "completed": 0}
    for op in data["operations"]:
        op_stats[op.get("status", "started")] = op_stats.get(op.get("status", "started"), 0) + 1
    data["op_stats"] = op_stats

    return data


# ---------------------------------------------------------------------------
# Behavior analysis — the Tester's main job
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """\
You are a Tester agent analyzing a StarCraft II bot's game behavior.
You are NOT reviewing code. You are reporting what the bot ACTUALLY DID in the game.

Think like a coach watching a replay: what went right, what went wrong, what should change.

Given the parsed game data, produce a behavior report as a JSON object:

{
  "result": "victory" | "defeat" | "crash" | "timeout",
  "game_duration_estimate": "short/medium/long based on log size",
  "what_happened": "2-3 sentence narrative of the game",
  "good": ["things the bot did well"],
  "bad": ["things the bot did poorly — be specific"],
  "critical_bugs": ["game-breaking issues that must be fixed first"],
  "metrics": {
    "max_supply": N,
    "operations_attempted": N,
    "operations_succeeded": N,
    "operations_failed": N,
    "minerals_banked_at_end": N,
    "bases_at_end": N
  },
  "top_3_fixes": [
    "most impactful fix, in plain language",
    "second most impactful",
    "third most impactful"
  ],
  "comparison_to_last_game": "better/worse/same and why (if previous game data available)"
}

Be brutally honest. The point is to identify what to fix next, not to be nice."""


class Tester:
    """Analyzes game behavior after each test run.

    Usage (called by Director after test_game.sh):
        tester = Tester(working_dir="/home/om/sc2-commander")
        report = tester.analyze_latest_game()
        # report is a structured dict the Director uses for replanning

    Can also be used standalone:
        tester = Tester(working_dir="/home/om/sc2-commander")
        tester.analyze_and_report("/tmp/sc2_test_12345.log")
    """

    def __init__(self, working_dir: str = "", model: str = "sonnet"):
        self.working_dir = working_dir
        self.model = model
        self._games_analyzed = 0

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("[Tester %s] %s", ts, msg)
        print(f"\033[1;35m[Tester {ts}]\033[0m {msg}")
        _broadcast(msg)

    def _find_latest_log(self) -> str | None:
        """Find the most recent SC2 test game log."""
        logs = sorted(Path("/tmp").glob("sc2_test_*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return str(logs[0]) if logs else None

    def _find_latest_replay(self) -> str | None:
        """Find the most recent replay file."""
        replay_dir = Path(self.working_dir) / "data" / "replays" / "bot_games"
        if not replay_dir.exists():
            return None
        replays = sorted(replay_dir.glob("*.SC2Replay"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        return str(replays[0]) if replays else None

    def analyze_game(self, log_path: str) -> dict:
        """Analyze a specific game log and return a behavior report."""
        self._log(f"Analyzing game: {log_path}")

        # Parse the raw log
        parsed = parse_game_log(log_path)

        if parsed.get("error"):
            self._log(f"Parse error: {parsed['error']}")
            return {"error": parsed["error"]}

        # Recall previous game analyses for comparison
        prev_games = _hyphae_recall("tester game analysis SC2 bot behavior", top_k=3)

        # Build prompt for Claude
        prompt_parts = [
            f"GAME RESULT: {parsed['game_result']}",
            f"MAX SUPPLY REACHED: {parsed['max_supply']}",
            f"FINAL MINERALS: {parsed['final_minerals']} | FINAL BASES: {parsed['final_bases']}",
            f"OPERATIONS: {parsed['op_stats']}",
            f"EXPANSION ATTEMPTS: {parsed['expansions']}",
            f"ERRORS: {len(parsed['errors'])}",
        ]

        if parsed["timeline"]:
            prompt_parts.append("\nTIMELINE OF KEY EVENTS:")
            for event_type, detail in parsed["timeline"][:30]:
                prompt_parts.append(f"  [{event_type}] {detail}")

        if parsed["strategies"]:
            prompt_parts.append(f"\nSTRATEGY PHASES: {' → '.join(parsed['strategies'][:10])}")

        if parsed["errors"]:
            prompt_parts.append("\nERRORS:")
            for e in parsed["errors"][:5]:
                prompt_parts.append(f"  {e}")

        # Failed operations detail
        failed_ops = [op for op in parsed["operations"] if op.get("status") == "failed"]
        if failed_ops:
            prompt_parts.append(f"\nFAILED OPERATIONS ({len(failed_ops)}):")
            for op in failed_ops[:10]:
                prompt_parts.append(f"  {op['name']}: {op.get('reason', '?')}")

        if prev_games:
            prompt_parts.append("\nPREVIOUS GAME ANALYSES (for comparison):")
            for pg in prev_games:
                prompt_parts.append(f"  • {pg}")

        prompt = "\n".join(prompt_parts)

        # Ask Claude to analyze
        response = _ask_claude(ANALYSIS_SYSTEM, prompt, model=self.model)

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            report = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            report = {
                "result": parsed["game_result"],
                "what_happened": response[:300],
                "critical_bugs": ["analysis parsing failed"],
                "top_3_fixes": [],
                "metrics": parsed["op_stats"],
            }

        # Enrich with raw data
        report["_raw"] = {
            "log_path": log_path,
            "replay_path": parsed.get("replay_path", ""),
            "total_lines": parsed["total_lines"],
            "operations": parsed["operations"][:20],
        }

        self._games_analyzed += 1

        # Log summary
        result = report.get("result", "?")
        narrative = report.get("what_happened", "")
        self._log(f"Game: {result} — {narrative[:100]}")
        if report.get("critical_bugs"):
            for bug in report["critical_bugs"]:
                self._log(f"  BUG: {bug}")
        if report.get("top_3_fixes"):
            for i, fix in enumerate(report["top_3_fixes"], 1):
                self._log(f"  FIX {i}: {fix}")

        # Save to Hyphae
        _hyphae_remember(
            f"Tester game #{self._games_analyzed}: {result}. "
            f"{narrative[:150]}. "
            f"Fixes: {'; '.join(report.get('top_3_fixes', [])[:2])}"
        )

        # Post to board
        _api("POST", f"/api/agent/{TESTER_NAME}/heartbeat", {
            "status": "idle",
            "commentary": f"Game #{self._games_analyzed}: {result} — {narrative[:80]}",
        })

        return report

    def analyze_latest_game(self) -> dict:
        """Find and analyze the most recent game log."""
        log_path = self._find_latest_log()
        if not log_path:
            self._log("No game logs found in /tmp/sc2_test_*.log")
            return {"error": "no game logs found"}
        return self.analyze_game(log_path)

    def format_for_director(self, report: dict) -> str:
        """Format the Tester's report as context for the Director's replanning."""
        parts = [f"TESTER REPORT — Game result: {report.get('result', '?')}"]

        narrative = report.get("what_happened", "")
        if narrative:
            parts.append(f"What happened: {narrative}")

        if report.get("critical_bugs"):
            parts.append("CRITICAL BUGS (fix these first):")
            for bug in report["critical_bugs"]:
                parts.append(f"  ⚠ {bug}")

        if report.get("top_3_fixes"):
            parts.append("TOP FIXES (in priority order):")
            for i, fix in enumerate(report["top_3_fixes"], 1):
                parts.append(f"  {i}. {fix}")

        metrics = report.get("metrics", {})
        if metrics:
            parts.append(f"Metrics: supply={metrics.get('max_supply',0)}, "
                         f"ops={metrics.get('operations_attempted',0)} "
                         f"({metrics.get('operations_succeeded',0)} ok, "
                         f"{metrics.get('operations_failed',0)} failed), "
                         f"minerals={metrics.get('minerals_banked_at_end',0)}, "
                         f"bases={metrics.get('bases_at_end',0)}")

        if report.get("good"):
            parts.append("Good: " + "; ".join(report["good"][:3]))

        if report.get("bad"):
            parts.append("Bad: " + "; ".join(report["bad"][:3]))

        comp = report.get("comparison_to_last_game", "")
        if comp:
            parts.append(f"vs. last game: {comp}")

        return "\n".join(parts)
