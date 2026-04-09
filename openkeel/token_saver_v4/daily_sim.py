"""Daily-usage stress simulator for token saver v4.5.

Runs three realistic session profiles end-to-end through the live
hook handlers and measures:
  - wall-clock time per engine call
  - chars saved per engine call
  - aggregate per-session: time added, tokens saved, time-per-saved-token

This is the closest thing to a real-traffic measurement we can do
without instrumenting Claude itself. Each tool call is dispatched to
the actual handler in pre_tool.py with TOKEN_SAVER_V4=1.

Usage:
    TOKEN_SAVER_V4=1 python -m openkeel.token_saver_v4.daily_sim
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ["TOKEN_SAVER_V4"] = "1"

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from openkeel.token_saver.hooks import pre_tool


# ---------------------------------------------------------------------------
# Session definitions
# ---------------------------------------------------------------------------

LIGHT_CODING = {
    "name": "Light coding session",
    "description": "Editing 1-2 files, occasional reference lookups",
    "calls": [
        # 1 startup recall
        {"kind": "recall", "query": "openkeel project status recent work"},
        # 1 ad-hoc recall mid-session
        {"kind": "recall", "query": "kanban board task progress"},
        # 6 file reads — 3 large, 3 small
        {"kind": "read", "path": "/home/om/openkeel/openkeel/core/cartographer.py"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/core/pilgrim.py"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/token_saver_v4/engines/recall_rerank.py"},
        # 1 git diff before commit
        {"kind": "diff", "command": "git log -p -n 2 -- openkeel/token_saver_v4/"},
        # 0 agents, 0 webfetch
    ],
}

HEAVY_RESEARCH = {
    "name": "Heavy research session",
    "description": "Investigating an unfamiliar problem, lots of recall + web + delegation",
    "calls": [
        {"kind": "recall", "query": "openkeel project status recent work"},
        {"kind": "recall", "query": "amyloidosis treatment ATTR cardiac"},
        {"kind": "recall", "query": "pilgrim cartographer observer stack"},
        {"kind": "recall", "query": "kaloth jagg infrastructure ip"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/token_saver/hooks/post_tool.py"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/token_saver/summarizer.py"},
        {"kind": "webfetch",
         "url": "https://docs.python.org/3/library/sqlite3.html",
         "prompt": "How do I enable WAL mode safely?"},
        {"kind": "agent",
         "description": "Audit token saver hooks",
         "prompt_inflation_path": "/home/om/openkeel/openkeel/token_saver/hooks/post_tool.py"},
        {"kind": "agent",
         "description": "Survey LLMOS codebase state",
         "prompt_inflation_path": "/home/om/openkeel/openkeel/core/cartographer.py"},
    ],
}

DEBUGGING = {
    "name": "Debugging session",
    "description": "Hunting a bug, lots of file reads + git history",
    "calls": [
        {"kind": "recall", "query": "openkeel project status recent work"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/core/cartographer.py"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/core/pilgrim.py"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/core/oracle.py"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/core/consensus.py"},
        {"kind": "diff", "command": "git log -p -n 5 -- openkeel/core/"},
        {"kind": "diff", "command": "git show HEAD"},
        {"kind": "read", "path": "/home/om/openkeel/openkeel/token_saver/hooks/pre_tool.py"},
    ],
}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _dispatch(call: dict) -> tuple[float, int, int, str]:
    """Run one simulated tool call. Returns (wall_ms, orig_chars, saved_chars, engine_label)."""
    t0 = time.time()
    kind = call["kind"]

    if kind == "recall":
        cmd = (
            f"curl -s -X POST http://127.0.0.1:8100/recall "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"query\": \"{call['query']}\", \"top_k\": 10}}'"
        )
        try:
            result = pre_tool.handle_bash({"command": cmd})
        except Exception:
            result = None
        wall = (time.time() - t0) * 1000
        out = result.get("reason", "") if result else ""
        return wall, _orig_for_recall(call), max(0, _orig_for_recall(call) - len(out)), "recall_rerank"

    if kind == "read":
        # Force a "first read" by clearing session state for this path
        try:
            state = pre_tool._load_session_state()
            state.setdefault("read_files", [])
            if call["path"] in state["read_files"]:
                state["read_files"].remove(call["path"])
            pre_tool._save_session_state(state)
        except Exception:
            pass
        try:
            with open(call["path"], "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            orig = len(content)
        except Exception:
            return 0.0, 0, 0, "read"
        try:
            result = pre_tool.handle_read({"file_path": call["path"]})
        except Exception:
            result = None
        wall = (time.time() - t0) * 1000
        out = result.get("reason", "") if result else ""
        saved = max(0, orig - len(out)) if out else 0
        return wall, orig, saved, "goal_filter" if out and "TOKEN SAVER v4.3" in out else "read_other"

    if kind == "diff":
        try:
            result = pre_tool.handle_bash({"command": call["command"]})
        except Exception:
            result = None
        wall = (time.time() - t0) * 1000
        out = result.get("reason", "") if result else ""
        # Estimate original by running the raw command separately (but cheaply, with cap)
        try:
            import subprocess
            raw = subprocess.check_output(
                call["command"], shell=True, cwd="/home/om/openkeel",
                stderr=subprocess.DEVNULL, timeout=15,
            ).decode("utf-8", errors="replace")
            orig = len(raw)
        except Exception:
            orig = len(out) * 10  # rough estimate
        return wall, orig, max(0, orig - len(out)), "diff_compress"

    if kind == "webfetch":
        try:
            result = pre_tool.handle_webfetch({"url": call["url"], "prompt": call["prompt"]})
        except Exception:
            result = None
        wall = (time.time() - t0) * 1000
        out = result.get("reason", "") if result else ""
        # Original would be the full page; our hook reports it in the reason
        orig = 0
        if out and "chars)" in out:
            try:
                import re
                m = re.search(r"\((\d+) chars\)", out)
                if m:
                    orig = int(m.group(1))
            except Exception:
                pass
        return wall, orig, max(0, orig - len(out)), "webfetch_compress"

    if kind == "agent":
        # Build a realistically-inflated agent prompt
        try:
            with open(call["prompt_inflation_path"], "r", encoding="utf-8", errors="replace") as f:
                pasted = f.read()[:9000]
        except Exception:
            pasted = ""
        prompt = (
            f"Background: I am working on token saver v4.5 in openkeel. "
            f"Need help with: {call['description']}.\n\n"
            f"Please analyze this carefully and return a structured report.\n\n"
            f"--- PASTED CONTEXT: {call['prompt_inflation_path']} ---\n{pasted}\n\n"
            f"Return findings as a numbered list with severity ratings."
        )
        orig = len(prompt)
        try:
            result = pre_tool.handle_agent({"prompt": prompt, "description": call["description"]})
        except Exception:
            result = None
        wall = (time.time() - t0) * 1000
        out = result.get("reason", "") if result else ""
        return wall, orig, max(0, orig - len(out)) if out else 0, "subagent_compress"

    return 0.0, 0, 0, "unknown"


def _orig_for_recall(call: dict) -> int:
    """Approximate original recall payload size from a live fetch."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8100/recall",
            data=json.dumps({"query": call["query"], "top_k": 10}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return len(resp.read())
    except Exception:
        return 5000  # conservative fallback


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_session(profile: dict) -> dict:
    print(f"\n{'=' * 70}")
    print(f"SESSION: {profile['name']}")
    print(f"  {profile['description']}")
    print(f"  {len(profile['calls'])} tool calls")
    print("=" * 70)

    per_call = []
    total_wall = 0.0
    total_orig = 0
    total_saved = 0

    for i, call in enumerate(profile["calls"], 1):
        wall, orig, saved, label = _dispatch(call)
        per_call.append((label, wall, orig, saved))
        total_wall += wall
        total_orig += orig
        total_saved += saved
        ratio = saved / max(1, orig) * 100
        print(f"  [{i:2d}] {label:<22} wall={wall:>7.0f}ms  "
              f"orig={orig:>7}  saved={saved:>7}  ({ratio:>5.1f}%)")

    print(f"  {'─' * 60}")
    print(f"  TOTAL                  wall={total_wall:>7.0f}ms  "
          f"orig={total_orig:>7}  saved={total_saved:>7}  "
          f"({total_saved / max(1, total_orig) * 100:>5.1f}%)")
    return {
        "name": profile["name"],
        "calls": len(profile["calls"]),
        "total_wall_ms": total_wall,
        "total_orig": total_orig,
        "total_saved": total_saved,
        "per_call": per_call,
    }


def main() -> int:
    print("DAILY USAGE STRESS SIMULATOR — token saver v4.5")
    print("Running three realistic session profiles end-to-end through live hooks.\n")

    sessions = [
        run_session(LIGHT_CODING),
        run_session(HEAVY_RESEARCH),
        run_session(DEBUGGING),
    ]

    print(f"\n{'=' * 70}")
    print("AGGREGATE")
    print("=" * 70)
    grand_wall = sum(s["total_wall_ms"] for s in sessions)
    grand_orig = sum(s["total_orig"] for s in sessions)
    grand_saved = sum(s["total_saved"] for s in sessions)
    grand_calls = sum(s["calls"] for s in sessions)

    print(f"  3 sessions, {grand_calls} total tool calls")
    print(f"  total wall time on engines: {grand_wall / 1000:.1f}s")
    print(f"  total chars saved:          {grand_saved:,}")
    print(f"  est Claude tokens saved:    {grand_saved // 4:,}")
    if grand_orig:
        print(f"  overall compression:        "
              f"{grand_saved / grand_orig * 100:.1f}%")
    if grand_saved > 0:
        ms_per_ktok = grand_wall / (grand_saved / 4 / 1000)
        print(f"  ms of LLM time per 1K Claude tokens saved: {ms_per_ktok:.0f}ms")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
