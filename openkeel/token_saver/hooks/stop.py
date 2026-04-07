#!/usr/bin/env python3
"""
Token Saver — Stop hook.

Fires when Claude Code finishes a response. Reads the transcript path from
stdin and records the turn's actual billed token usage to the v5
billed_tokens table. This is the ground-truth number that tracks Claude's
real API bill, complementary to the existing `savings` table which only
tracks tool-output volume reduction.

Wire into ~/.claude/settings.json under hooks.Stop.

Hook payload (stdin JSON):
    {
      "session_id": "...",
      "transcript_path": "/home/.../xxx.jsonl",
      "hook_event_name": "Stop",
      ...
    }

MUST NEVER BLOCK. Always exits 0. All errors are swallowed + logged to
the v5 debug_log.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        from openkeel.token_saver_v5 import billed_tracker
        billed_tracker.process_stop_hook(payload)
    except Exception as e:
        try:
            from openkeel.token_saver_v5 import debug_log
            debug_log.swallow("stop_hook.main", error=e,
                              extra={"session_id": payload.get("session_id", "")})
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
