#!/usr/bin/env python3
"""PostToolUse hook for token saver.

Runs after every tool call to:
  - Cache file content after Read calls (for future summarization)
  - Track session file reads
  - Record output sizes in the ledger

Protocol: reads JSON from stdin, no output needed.
Runs async (does not block Claude).
"""

import json
import os
import sys
import urllib.request

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")


def _daemon_post(path: str, data: dict) -> dict | None:
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{DAEMON_URL}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def handle_read(tool_input: dict, tool_output: str) -> None:
    """After a Read, trigger async summarization for the cache."""
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    # Record the read
    _daemon_post("/session/read", {"path": file_path})

    # Only trigger summarization for large files
    if len(tool_output) > 4000:
        _daemon_post("/summarize", {"path": file_path})

    # Record the read size in ledger
    _daemon_post("/ledger/record", {
        "event_type": "file_read",
        "tool_name": "Read",
        "file_path": file_path,
        "original_chars": len(tool_output),
        "saved_chars": 0,
        "notes": f"read {len(tool_output)} chars",
    })


def handle_bash(tool_input: dict, tool_output: str) -> None:
    """Track bash output sizes."""
    command = tool_input.get("command", "")
    output_len = len(tool_output) if tool_output else 0

    if output_len > 2000:
        _daemon_post("/ledger/record", {
            "event_type": "large_output",
            "tool_name": "Bash",
            "original_chars": output_len,
            "saved_chars": 0,
            "notes": f"cmd: {command[:100]}",
        })


def handle_grep(tool_input: dict, tool_output: str) -> None:
    """Track grep output sizes."""
    output_len = len(tool_output) if tool_output else 0
    if output_len > 3000:
        _daemon_post("/ledger/record", {
            "event_type": "large_output",
            "tool_name": "Grep",
            "original_chars": output_len,
            "saved_chars": 0,
            "notes": f"pattern: {tool_input.get('pattern', '')[:80]}",
        })


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    tool_output = input_data.get("tool_output", "")

    if tool_name == "Read":
        handle_read(tool_input, tool_output)
    elif tool_name == "Bash":
        handle_bash(tool_input, tool_output)
    elif tool_name == "Grep":
        handle_grep(tool_input, tool_output)


if __name__ == "__main__":
    main()
