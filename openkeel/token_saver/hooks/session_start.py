#!/usr/bin/env python3
"""SessionStart hook for token saver.

Runs at session start to:
  1. Build/update codebase index
  2. Generate context prefill (project map, git context)
  3. Pre-warm cache for recently modified files
  4. Reset session state (conversation log, predictions)
  5. Start daemon if not running

Outputs context to stdout for Claude to see.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")


def _daemon_running() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(f"{DAEMON_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_daemon() -> None:
    """Try to start the daemon if it's not running."""
    try:
        project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
        subprocess.Popen(
            [sys.executable, "-m", "openkeel.token_saver.daemon"],
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
    except Exception:
        pass


def main():
    project_root = os.getcwd()

    # Ensure daemon is running
    if not _daemon_running():
        _start_daemon()

    # Reset session state
    try:
        from openkeel.token_saver.engines.conversation_compressor import reset as cc_reset
        cc_reset()
    except Exception:
        pass

    try:
        from openkeel.token_saver.engines.predictive_cache import reset as pc_reset
        pc_reset()
    except Exception:
        pass

    # Reset session reads in daemon
    try:
        import urllib.request
        req = urllib.request.Request(f"{DAEMON_URL}/session/reset", method="GET")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass

    # Build prefill context
    try:
        from openkeel.token_saver.engines.context_prefill import build_prefill, get_recently_modified_files
        prefill = build_prefill(project_root)
        if prefill:
            print(prefill)

        # Pre-warm cache for recently modified files
        recent_files = get_recently_modified_files(project_root, limit=8)
        if recent_files and _daemon_running():
            import urllib.request
            payload = json.dumps({"files": recent_files}).encode()
            req = urllib.request.Request(
                f"{DAEMON_URL}/cache/warm",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=30)
            except Exception:
                pass
    except Exception as e:
        print(f"[TOKEN SAVER] Prefill failed: {e}")

    # Log session start
    try:
        from openkeel.token_saver import ledger
        ledger.record(
            event_type="session_start",
            tool_name="SessionStart",
            notes=f"project: {os.path.basename(project_root)}",
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
