"""Configuration and auth for claude-bubble."""

import json
import os
import shutil
from pathlib import Path


def get_api_key():
    """Get Anthropic API key from env var or Claude CLI credentials.

    Checks in order:
    1. ANTHROPIC_API_KEY environment variable
    2. Claude CLI OAuth credentials (~/.claude/.credentials.json)
    """
    # 1. Standard env var
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    # 2. Claude CLI OAuth token
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text())
            token = creds.get("claudeAiOauth", {}).get("accessToken")
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    raise RuntimeError(
        "No API key found. Set ANTHROPIC_API_KEY or log in with Claude CLI (`claude login`)."
    )


def find_claude_binary():
    """Find the claude CLI binary."""
    # Check common locations
    candidates = [
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),
        str(Path.home() / ".claude" / "local" / "claude"),
        "/usr/local/bin/claude",
    ]

    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise RuntimeError(
        "Claude CLI not found. Install it: npm install -g @anthropic-ai/claude-code"
    )


def get_config():
    """Get configuration with sensible defaults."""
    return {
        "gather_model": os.environ.get(
            "BUBBLE_GATHER_MODEL", "claude-haiku-4-5-20251001"
        ),
        "reason_model": os.environ.get("BUBBLE_REASON_MODEL", "sonnet"),
        "api_url": os.environ.get(
            "BUBBLE_API_URL", "https://api.anthropic.com/v1/messages"
        ),
        "api_version": "2023-06-01",
        "claude_bin": os.environ.get("BUBBLE_CLAUDE_BIN") or find_claude_binary(),
        "max_gather_chars": int(os.environ.get("BUBBLE_MAX_GATHER_CHARS", "25000")),
    }
