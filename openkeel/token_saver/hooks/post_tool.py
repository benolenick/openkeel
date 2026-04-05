#!/usr/bin/env python3
"""PostToolUse hook for token saver — all engines engaged.

Runs after every tool call to:
  1. Cache file reads for future summarization
  2. Compress large outputs (Bash, Grep, Glob)
  3. Track conversation turns for compression
  4. Trigger predictive pre-caching
  5. Log everything to the ledger

Protocol: reads JSON from stdin, no stdout needed.
"""

import json
import os
import sys
import urllib.request

# Add project root to path for engine imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

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


def _daemon_available() -> bool:
    try:
        req = urllib.request.Request(f"{DAEMON_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Engine: File Read Caching + Predictive Pre-cache
# ---------------------------------------------------------------------------

def handle_read(tool_input: dict, tool_output: str) -> None:
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    output_len = len(tool_output) if tool_output else 0

    # Record the read in session
    _daemon_post("/session/read", {"path": file_path})

    # Trigger summarization for large files
    if output_len > 4000:
        _daemon_post("/summarize", {"path": file_path})

    # Log the read
    _daemon_post("/ledger/record", {
        "event_type": "file_read",
        "tool_name": "Read",
        "file_path": file_path,
        "original_chars": output_len,
        "saved_chars": 0,
        "notes": f"read {output_len} chars ({os.path.basename(file_path)})",
    })

    # Predictive pre-cache: predict and warm next likely reads
    try:
        from openkeel.token_saver.engines.predictive_cache import predict_next_reads, pre_warm
        predictions = predict_next_reads(file_path, project_root=os.getcwd())
        if predictions:
            pre_warm(predictions)
    except Exception:
        pass

    # Record conversation turn
    _record_turn("Read", tool_input, tool_output)


# ---------------------------------------------------------------------------
# Engine: Output Compression
# ---------------------------------------------------------------------------

def handle_bash(tool_input: dict, tool_output: str) -> None:
    command = tool_input.get("command", "")
    output_len = len(tool_output) if tool_output else 0

    # Try to compress the output for logging
    saved_chars = 0
    try:
        from openkeel.token_saver.engines.output_compressor import compress_output
        compressed, meta = compress_output(command, tool_output or "", tool_name="Bash")
        saved_chars = meta.get("saved_chars", 0)
    except Exception:
        pass

    # Log
    _daemon_post("/ledger/record", {
        "event_type": "bash_output",
        "tool_name": "Bash",
        "original_chars": output_len,
        "saved_chars": saved_chars,
        "notes": f"cmd: {command[:120]}",
    })

    _record_turn("Bash", tool_input, tool_output)


def handle_grep(tool_input: dict, tool_output: str) -> None:
    output_len = len(tool_output) if tool_output else 0
    pattern = tool_input.get("pattern", "")

    # Try search filtering
    saved_chars = 0
    try:
        from openkeel.token_saver.engines.search_filter import filter_grep_results
        filtered, meta = filter_grep_results(
            tool_output or "",
            pattern=pattern,
            project_root=os.getcwd(),
        )
        saved_chars = meta.get("saved_chars", 0)
    except Exception:
        pass

    _daemon_post("/ledger/record", {
        "event_type": "grep_output",
        "tool_name": "Grep",
        "original_chars": output_len,
        "saved_chars": saved_chars,
        "notes": f"pattern: {pattern[:80]}",
    })

    _record_turn("Grep", tool_input, tool_output)


def handle_glob(tool_input: dict, tool_output: str) -> None:
    output_len = len(tool_output) if tool_output else 0
    pattern = tool_input.get("pattern", "")

    saved_chars = 0
    try:
        from openkeel.token_saver.engines.search_filter import filter_glob_results
        filtered, meta = filter_glob_results(tool_output or "", pattern=pattern)
        saved_chars = meta.get("saved_chars", 0)
    except Exception:
        pass

    _daemon_post("/ledger/record", {
        "event_type": "glob_output",
        "tool_name": "Glob",
        "original_chars": output_len,
        "saved_chars": saved_chars,
        "notes": f"pattern: {pattern[:80]}",
    })

    _record_turn("Glob", tool_input, tool_output)


def handle_edit(tool_input: dict, tool_output: str) -> None:
    file_path = tool_input.get("file_path", "")
    _daemon_post("/ledger/record", {
        "event_type": "file_edit",
        "tool_name": "Edit",
        "file_path": file_path,
        "original_chars": len(tool_output) if tool_output else 0,
        "saved_chars": 0,
        "notes": f"edited {os.path.basename(file_path)}",
    })
    _record_turn("Edit", tool_input, tool_output)


def handle_write(tool_input: dict, tool_output: str) -> None:
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
    _record_turn("Write", tool_input, tool_output)


def handle_agent(tool_input: dict, tool_output: str) -> None:
    """Track agent spawns — these are expensive."""
    prompt_len = len(tool_input.get("prompt", ""))
    output_len = len(tool_output) if tool_output else 0
    _daemon_post("/ledger/record", {
        "event_type": "agent_spawn",
        "tool_name": "Agent",
        "original_chars": prompt_len + output_len,
        "saved_chars": 0,
        "notes": f"agent: {tool_input.get('description', '')[:80]}",
    })


# ---------------------------------------------------------------------------
# Conversation tracking
# ---------------------------------------------------------------------------

def _record_turn(tool_name: str, tool_input: dict, tool_output: str) -> None:
    """Record turn for conversation compression."""
    try:
        from openkeel.token_saver.engines.conversation_compressor import record_turn
        record_turn(tool_name, tool_input, tool_output or "")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Quick daemon check — if it's down, still log what we can
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
        "Agent": handle_agent,
    }

    handler = handlers.get(tool_name)
    if handler:
        try:
            handler(tool_input, tool_output)
        except Exception:
            pass  # Never block Claude


if __name__ == "__main__":
    main()
