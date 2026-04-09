"""Fractal gate hook — Stop hook that refuses to let Claude end a turn
while an EnforcedSession is open with incomplete leaves.

This is the harness-level enforcement. Without it, EnforcedSession is
just a library the agent CHOOSES to call (per other-Claude's review).
With it, the agent literally cannot return to the user while a
fractal session is incomplete.

Wire into ~/.claude/settings.json:

    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/om/openkeel/openkeel/fractal/hooks/fractal_gate.py",
            "timeout": 5
          }
        ]
      }
    ]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SENTINEL = Path.home() / ".openkeel" / "fractal" / "active.json"
DB_PATH = Path.home() / ".openkeel" / "fractal" / "enforced_sessions.db"


def main() -> int:
    # Stop hook protocol: read JSON from stdin, output JSON to stdout
    try:
        _ = sys.stdin.read()
    except Exception:
        pass

    if not SENTINEL.exists():
        # No active session — let Claude stop
        return 0

    try:
        info = json.loads(SENTINEL.read_text())
    except Exception:
        return 0

    session_id = info.get("session_id")
    if not session_id or not DB_PATH.exists():
        return 0

    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT COUNT(*) FROM leaves WHERE session_id = ? "
            "AND state NOT IN ('COMPLETE', 'SKIPPED', 'BLOCKED_EXTERNAL', 'WAIVED')",
            (session_id,),
        ).fetchone()
        incomplete = row[0] if row else 0

        leaves = conn.execute(
            "SELECT label, state, depth FROM leaves WHERE session_id = ? "
            "AND state NOT IN ('COMPLETE', 'SKIPPED', 'BLOCKED_EXTERNAL', 'WAIVED') "
            "ORDER BY depth, id",
            (session_id,),
        ).fetchall()
        conn.close()
    except Exception:
        return 0

    if incomplete == 0:
        return 0

    summary_lines = [
        f"[FRACTAL GATE] refusing to stop — session {session_id} has "
        f"{incomplete} incomplete leaf(s):",
    ]
    for lbl, state, depth in leaves[:8]:
        summary_lines.append(f"  d{depth} [{state}] {lbl}")
    if incomplete > 8:
        summary_lines.append(f"  ... and {incomplete - 8} more")
    summary_lines.append(
        "Submit evidence, skip-with-reason, or call session.abort() before stopping."
    )

    out = {
        "decision": "block",
        "reason": "\n".join(summary_lines),
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
