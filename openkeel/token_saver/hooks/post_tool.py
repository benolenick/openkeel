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
    """Track all bash output sizes."""
    command = tool_input.get("command", "")
    output_len = len(tool_output) if tool_output else 0

    # Log everything — even small outputs — for accurate total tracking
    _daemon_post("/ledger/record", {
        "event_type": "bash_output",
        "tool_name": "Bash",
        "original_chars": output_len,
        "saved_chars": 0,
        "notes": f"cmd: {command[:120]}",
    })


def handle_grep(tool_input: dict, tool_output: str) -> None:
    """Track all grep output sizes."""
    output_len = len(tool_output) if tool_output else 0
    _daemon_post("/ledger/record", {
        "event_type": "grep_output",
        "tool_name": "Grep",
        "original_chars": output_len,
        "saved_chars": 0,
        "notes": f"pattern: {tool_input.get('pattern', '')[:80]}",
    })


def handle_glob(tool_input: dict, tool_output: str) -> None:
    """Track glob output sizes."""
    output_len = len(tool_output) if tool_output else 0
    _daemon_post("/ledger/record", {
        "event_type": "glob_output",
        "tool_name": "Glob",
        "original_chars": output_len,
        "saved_chars": 0,
        "notes": f"pattern: {tool_input.get('pattern', '')[:80]}",
    })


def handle_edit(tool_input: dict, tool_output: str) -> None:
    """Track edits — record file path so we know not to cache it."""
    file_path = tool_input.get("file_path", "")
    _daemon_post("/ledger/record", {
        "event_type": "file_edit",
        "tool_name": "Edit",
        "file_path": file_path,
        "original_chars": len(tool_output) if tool_output else 0,
        "saved_chars": 0,
        "notes": f"edited {os.path.basename(file_path)}",
    })


def handle_write(tool_input: dict, tool_output: str) -> None:
    """Track writes."""
    file_path = tool_input.get("file_path", "")
    content_len = len(tool_input.get("content", ""))
    _daemon_post("/ledger/record", {
        "event_type": "file_write",
        "tool_name": "Write",
        "file_path": file_path,
        "original_chars": content_len,
        "saved_chars": 0,
        "notes": f"wrote {content_len} chars to {os.path.basename(file_path)}",
    })


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    tool_output = input_data.get("tool_output", "")

    handlers = {
        "Read": handle_read,
        "Bash": handle_bash,
        "Grep": handle_grep,
        "Glob": handle_glob,
        "Edit": handle_edit,
        "Write": handle_write,
    }
    handler = handlers.get(tool_name)
    if handler:
        handler(tool_input, tool_output)


if __name__ == "__main__":
    main()
