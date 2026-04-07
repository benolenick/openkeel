"""Conversation Compressor — summarize old conversation context.

Claude Code's context window fills up with old messages. This engine
provides conversation turn summaries that can be injected to prevent
re-reading stale context.

Works by:
  1. Tracking conversation turns via the post-hook
  2. Every N turns, summarize the recent batch via local LLM
  3. Store the summary for the prefill engine to inject

All summaries are logged to the ledger.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from openkeel.token_saver import summarizer, ledger

CONVERSATION_LOG = Path.home() / ".openkeel" / "token_saver_conversation.jsonl"
CONVERSATION_SUMMARY = Path.home() / ".openkeel" / "token_saver_conversation_summary.json"
SUMMARIZE_EVERY = 10  # Summarize every N tool calls


def record_turn(
    tool_name: str,
    tool_input: dict,
    tool_output: str = "",
    direction: str = "tool_call",
) -> None:
    """Record a conversation turn for later summarization."""
    entry = {
        "ts": time.time(),
        "direction": direction,
        "tool_name": tool_name,
        "summary": _summarize_turn(tool_name, tool_input, tool_output),
    }

    try:
        CONVERSATION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CONVERSATION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

    # Check if we should compress
    _maybe_compress()


def _summarize_turn(tool_name: str, tool_input: dict, tool_output: str) -> str:
    """Create a concise but informative summary of a tool call.

    Includes output size and key findings to make compressed summaries
    actually useful for understanding session history.
    """
    out_len = len(tool_output)
    out_preview = ""
    if out_len > 200:
        # Extract first meaningful line from output for context
        for line in tool_output.split("\n")[:10]:
            stripped = line.strip()
            if stripped and len(stripped) > 10 and not stripped.startswith(("[TOKEN SAVER]", "---")):
                out_preview = f" → {stripped[:80]}"
                break

    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        lines = tool_output.count("\n") + 1 if tool_output else 0
        return f"Read {os.path.basename(path)} ({lines}L, {out_len} chars)"
    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")[:50]
        new = tool_input.get("new_string", "")[:50]
        return f"Edited {os.path.basename(path)}: '{old}' → '{new}'"
    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        content_len = len(tool_input.get("content", ""))
        return f"Created {os.path.basename(path)} ({content_len} chars)"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")[:100]
        status = "OK" if out_len < 200 else f"{out_len} chars"
        return f"Ran: {cmd} [{status}]{out_preview}"
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        match_count = tool_output.count("\n") + 1 if tool_output.strip() else 0
        return f"Grep '{pattern}' → {match_count} results ({out_len} chars)"
    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        match_count = tool_output.count("\n") + 1 if tool_output.strip() else 0
        return f"Glob '{pattern}' → {match_count} files"
    elif tool_name == "Agent":
        desc = tool_input.get("description", "agent task")
        return f"Agent: {desc} ({out_len} chars output)"
    else:
        return f"{tool_name} call ({out_len} chars)"


def _maybe_compress() -> None:
    """Check turn count and compress if threshold reached."""
    try:
        if not CONVERSATION_LOG.exists():
            return
        with open(CONVERSATION_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if len(lines) < SUMMARIZE_EVERY:
            return

        # Load existing summary
        existing_summary = ""
        if CONVERSATION_SUMMARY.exists():
            try:
                data = json.loads(CONVERSATION_SUMMARY.read_text())
                existing_summary = data.get("summary", "")
            except (json.JSONDecodeError, OSError):
                pass

        # Build turn summaries
        turns = []
        for line in lines:
            try:
                entry = json.loads(line.strip())
                turns.append(entry.get("summary", ""))
            except json.JSONDecodeError:
                continue

        turn_text = "\n".join(f"- {t}" for t in turns)
        original_chars = sum(len(t) for t in turns)

        # Try LLM compression
        compressed = _llm_compress(existing_summary, turn_text)

        if not compressed:
            # Fallback: just keep the turn summaries as-is
            compressed = turn_text

        # Save compressed summary
        summary_data = {
            "summary": compressed,
            "turn_count": len(turns),
            "updated_at": time.time(),
            "original_chars": original_chars,
            "compressed_chars": len(compressed),
        }
        CONVERSATION_SUMMARY.write_text(json.dumps(summary_data, indent=2))

        # Clear the log
        CONVERSATION_LOG.write_text("")

        # Record savings
        saved = max(0, original_chars - len(compressed))
        if saved > 0:
            ledger.record(
                event_type="conversation_compress",
                tool_name="ConversationCompressor",
                original_chars=original_chars,
                saved_chars=saved,
                notes=f"compressed {len(turns)} turns: {original_chars} → {len(compressed)} chars",
            )

    except Exception:
        pass


def _llm_compress(existing_summary: str, new_turns: str) -> str:
    """Use local LLM to compress conversation history."""
    if not summarizer.is_available():
        return ""

    context = ""
    if existing_summary:
        context = f"Previous context:\n{existing_summary}\n\n"

    system = (
        "You are a conversation summarizer. Compress the session activity into "
        "a brief summary (5-8 lines). Focus on: what was done, what files were "
        "changed, what decisions were made. Output only the summary, no preamble."
    )
    prompt = f"{context}Recent activity:\n{new_turns}"

    result = summarizer.ollama_generate(prompt, system=system, max_tokens=300)
    return result.strip() if result else ""


def get_session_context() -> str:
    """Get the compressed conversation context for prefill injection."""
    if not CONVERSATION_SUMMARY.exists():
        return ""
    try:
        data = json.loads(CONVERSATION_SUMMARY.read_text())
        summary = data.get("summary", "")
        if summary:
            return f"[TOKEN SAVER] Session activity so far:\n{summary}"
    except (json.JSONDecodeError, OSError):
        pass
    return ""


def reset() -> None:
    """Reset conversation state (call at session start)."""
    try:
        if CONVERSATION_LOG.exists():
            CONVERSATION_LOG.write_text("")
        if CONVERSATION_SUMMARY.exists():
            CONVERSATION_SUMMARY.unlink()
    except OSError:
        pass
