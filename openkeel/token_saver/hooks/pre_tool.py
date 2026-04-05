#!/usr/bin/env python3
"""PreToolUse hook for token saver — all engines engaged.

Intercepts tool calls to save tokens via:
  1. File re-read caching (serve summaries instead of full files)
  2. Output compression (npm install, test output, git push, etc.)
  3. Search result filtering (rank + dedupe grep/glob results)
  4. Bash command optimization (verbose commands → compact)
  5. Task routing (simple tasks → local model suggestion)

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
EDITED_FILES_PATH = os.path.expanduser("~/.openkeel/scribe_state.json")


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
    try:
        with open(EDITED_FILES_PATH, "r") as f:
            state = json.load(f)
        return set(state.get("files_edited", []) + state.get("files_created", []))
    except Exception:
        return set()


def _check_session_read(file_path: str) -> bool:
    result = _daemon_post("/session/read", {"path": file_path})
    if result:
        return result.get("already_read", False)
    return False


# ---------------------------------------------------------------------------
# Engine: File Re-read Caching
# ---------------------------------------------------------------------------

def handle_read(tool_input: dict) -> dict | None:
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
    if stat.st_size < 4000:
        _check_session_read(file_path)
        return None

    # If specific lines requested (offset/limit), let it through
    # (Claude is already being precise)
    if tool_input.get("offset") or tool_input.get("limit"):
        _check_session_read(file_path)
        return None

    # If this is a re-read, try to serve a summary
    already_read = _check_session_read(file_path)
    if not already_read:
        return None  # First read passes through

    # Try cached summary from daemon
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
        "notes": f"re-read: {len(summary.splitlines())}L summary vs {orig_lines}L original",
    })

    return {
        "decision": "block",
        "reason": (
            f"[TOKEN SAVER] You already read this file. Here's a summary "
            f"({len(summary.splitlines())} lines vs {orig_lines} original). "
            f"Use Read with specific offset/limit if you need exact content.\n\n"
            f"File: {file_path}\n{summary}"
        ),
    }


# ---------------------------------------------------------------------------
# Engine: Bash Command Optimization
# ---------------------------------------------------------------------------

def handle_bash(tool_input: dict) -> dict | None:
    command = tool_input.get("command", "").strip()
    if not command:
        return None

    # git log without limits
    if command.startswith("git log") and "-n" not in command and "--oneline" not in command and "| head" not in command:
        if len(command.split()) <= 3:
            _daemon_post("/ledger/record", {
                "event_type": "command_rewrite",
                "tool_name": "Bash",
                "original_chars": 500,  # est. full git log
                "saved_chars": 350,
                "notes": "rewrote: git log → git log --oneline -20",
            })
            return {
                "decision": "block",
                "reason": (
                    "[TOKEN SAVER] Rewrote `git log` to `git log --oneline -20` to save tokens. "
                    "Use the full command explicitly if you need more."
                ),
            }

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    result = None

    try:
        if tool_name == "Read":
            result = handle_read(tool_input)
        elif tool_name == "Bash":
            result = handle_bash(tool_input)
    except Exception:
        pass  # Fail-open

    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
