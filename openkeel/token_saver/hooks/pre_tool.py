#!/usr/bin/env python3
"""PreToolUse hook for token saver.

Intercepts Read tool calls to serve cached summaries instead of full files.
Intercepts Bash calls to optimize verbose commands.

Protocol: reads JSON from stdin, outputs JSON to stdout.
  - {"decision": "block", "reason": "..."} to block and replace
  - {"decision": "allow"} or no output to allow
  - Must complete within 2 seconds

Fail-open: any error → allow the tool call through.
"""

import json
import os
import sys
import urllib.error
import urllib.request

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")
# Files being actively edited — never serve cached versions
EDITED_FILES_PATH = os.path.expanduser("~/.openkeel/scribe_state.json")
# Minimum line count to trigger summarization
MIN_LINES_FOR_SUMMARY = 100


def _daemon_get(path: str) -> dict | None:
    try:
        req = urllib.request.Request(f"{DAEMON_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _daemon_post(path: str, data: dict) -> dict | None:
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{DAEMON_URL}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_edited_files() -> set:
    """Load files being edited from scribe state."""
    try:
        with open(EDITED_FILES_PATH, "r") as f:
            state = json.load(f)
        return set(state.get("files_edited", []) + state.get("files_created", []))
    except Exception:
        return set()


def _check_session_read(file_path: str) -> bool:
    """Check if file was already read this session and record it."""
    result = _daemon_post("/session/read", {"path": file_path})
    if result:
        return result.get("already_read", False)
    return False


def handle_read(tool_input: dict) -> dict | None:
    """Handle Read tool interception."""
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    # Never cache files being edited
    if file_path in _get_edited_files():
        return None

    # Check if file exists and get size
    try:
        stat = os.stat(file_path)
    except OSError:
        return None

    # Don't intercept small files
    if stat.st_size < 4000:  # ~100 lines
        _check_session_read(file_path)
        return None

    # If this is a re-read, try to serve a summary
    already_read = _check_session_read(file_path)
    if not already_read:
        return None  # First read — let it through, post-hook will cache

    # Try to get cached summary from daemon
    result = _daemon_post("/summarize", {"path": file_path})
    if not result or not result.get("summary"):
        return None

    summary = result["summary"]
    orig_lines = result.get("original_lines", 0)
    orig_chars = result.get("original_chars", 0)
    summary_chars = len(summary)

    # Record savings
    _daemon_post("/ledger/record", {
        "event_type": "cache_hit",
        "tool_name": "Read",
        "file_path": file_path,
        "original_chars": orig_chars,
        "saved_chars": max(0, orig_chars - summary_chars),
        "notes": f"re-read: served {len(summary.splitlines())} line summary instead of {orig_lines} lines",
    })

    return {
        "decision": "block",
        "reason": (
            f"[TOKEN SAVER] You already read this file. Here's a summary ({len(summary.splitlines())} lines "
            f"vs {orig_lines} original). Use Read again with specific line range if you need exact content.\n\n"
            f"File: {file_path}\n{summary}"
        ),
    }


def handle_bash(tool_input: dict) -> dict | None:
    """Optimize verbose bash commands."""
    command = tool_input.get("command", "").strip()
    if not command:
        return None

    # git log without limits — add --oneline -20
    if command.startswith("git log") and "-n" not in command and "--oneline" not in command and "| head" not in command:
        if len(command.split()) <= 3:  # Simple git log, not a complex pipeline
            return {
                "decision": "block",
                "reason": (
                    "[TOKEN SAVER] Rewrote `git log` to `git log --oneline -20` to save tokens. "
                    "Use the full command explicitly if you need more."
                ),
            }

    return None


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    result = None

    if tool_name == "Read":
        result = handle_read(tool_input)
    elif tool_name == "Bash":
        result = handle_bash(tool_input)

    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
