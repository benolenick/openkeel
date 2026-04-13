"""Watch Claude Code session JSONL files for exact token usage."""

import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


CLAUDE_DIR = Path.home() / ".claude"


class SessionWatcher(QObject):
    """Tails the active Claude Code conversation JSONL for token usage.

    Emits token_update(model, input_tok, output_tok, cache_read, cache_create)
    whenever a new assistant message with usage data appears.
    """

    token_update = Signal(str, int, int, int, int)  # model, in, out, cache_read, cache_create
    session_found = Signal(str)  # session file path

    def __init__(self, parent=None, poll_ms=3000):
        super().__init__(parent)
        self._poll_ms = poll_ms
        self._current_file = None
        self._file_pos = 0  # byte offset we've read up to
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._find_session()
        self._timer.start(self._poll_ms)

    def stop(self):
        self._timer.stop()

    def _find_session(self):
        """Find the most recently modified JSONL in ~/.claude/projects/."""
        projects = CLAUDE_DIR / "projects"
        if not projects.exists():
            return

        newest = None
        newest_mtime = 0
        for d in projects.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                mt = f.stat().st_mtime
                if mt > newest_mtime:
                    newest = f
                    newest_mtime = mt

        if newest and newest != self._current_file:
            self._current_file = newest
            # Start from end so we only see new messages
            self._file_pos = newest.stat().st_size
            self.session_found.emit(str(newest))

    def _poll(self):
        # Check if a newer session file appeared
        self._find_session()

        if not self._current_file or not self._current_file.exists():
            return

        try:
            size = self._current_file.stat().st_size
            if size <= self._file_pos:
                return

            with open(self._current_file, "r") as f:
                f.seek(self._file_pos)
                new_data = f.read()
                self._file_pos = f.tell()

            for line in new_data.strip().split("\n"):
                if not line.strip():
                    continue
                self._parse_line(line)

        except Exception:
            pass

    def _parse_line(self, line: str):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return

        msg = d.get("message")
        if not isinstance(msg, dict):
            return

        usage = msg.get("usage")
        if not usage:
            return

        model = msg.get("model", "unknown")

        # Normalize model name to lane
        lane = self._model_to_lane(model)

        input_tok = usage.get("input_tokens", 0)
        output_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)

        self.token_update.emit(lane, input_tok, output_tok, cache_read, cache_create)

    @staticmethod
    def _model_to_lane(model: str) -> str:
        """Map a model ID string to one of our 4 lanes."""
        m = model.lower()
        if "opus" in m:
            return "opus"
        elif "haiku" in m:
            return "haiku"
        elif "sonnet" in m:
            return "sonnet"
        else:
            return "local"
