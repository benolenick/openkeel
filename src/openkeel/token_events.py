"""Lightweight token event log for GUI dial integration.

Haiku API and local LLM calls write events here so the session watcher
can pick them up and feed them to the GUI dials alongside Sonnet tokens
from Claude Code's JSONL.

Format: one JSON object per line:
  {"lane": "haiku", "input_tokens": N, "output_tokens": N, "ts": float}
"""

import json
import time
from pathlib import Path

TOKEN_LOG = Path.home() / ".openkeel2" / "token_events.jsonl"


def emit(lane: str, input_tokens: int, output_tokens: int,
         cache_read: int = 0, cache_create: int = 0):
    """Append a token event to the log file."""
    try:
        TOKEN_LOG.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "lane": lane,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
            "ts": time.time(),
        }
        with open(TOKEN_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass
