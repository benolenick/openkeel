#!/usr/bin/env python3
"""Token Saver daemon — local HTTP cache server for file summaries and token tracking.

Runs on localhost:11450. Provides:
  POST /summarize   — summarize a file (cached by path+mtime)
  POST /filter      — filter command output
  POST /classify    — classify task difficulty
  GET  /ledger      — token savings summary
  POST /ledger/record — record a savings event
  GET  /cache/status — cache stats
  POST /cache/warm  — pre-warm cache for a list of files
  GET  /health      — health check

Start:  python -m openkeel.token_saver.daemon
Stop:   kill $(cat ~/.openkeel/token_saver_daemon.pid)
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Any

# Import our modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from openkeel.token_saver import summarizer, ledger

PORT = int(os.environ.get("TOKEN_SAVER_PORT", "11450"))
CACHE_DIR = Path.home() / ".openkeel" / "token_saver_cache"
PID_FILE = Path.home() / ".openkeel" / "token_saver_daemon.pid"
SESSION_FILE = Path.home() / ".openkeel" / "token_saver_session.json"

# In-memory caches (persisted to disk)
_file_cache: dict[str, dict[str, Any]] = {}
_session_reads: set[str] = set()


def _cache_key(file_path: str) -> str:
    return hashlib.sha256(file_path.encode()).hexdigest()[:16]


def _load_cache_entry(file_path: str) -> dict[str, Any] | None:
    """Load cached summary for a file. Returns None if stale or missing."""
    key = _cache_key(file_path)

    # Check memory first
    if key in _file_cache:
        entry = _file_cache[key]
        try:
            current_mtime = os.path.getmtime(file_path)
            if entry.get("mtime") == current_mtime:
                return entry
        except OSError:
            pass
        del _file_cache[key]

    # Check disk
    cache_path = CACHE_DIR / f"{key}.json"
    if not cache_path.exists():
        return None

    try:
        entry = json.loads(cache_path.read_text())
        current_mtime = os.path.getmtime(file_path)
        if entry.get("mtime") == current_mtime:
            _file_cache[key] = entry
            return entry
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_cache_entry(file_path: str, entry: dict[str, Any]) -> None:
    key = _cache_key(file_path)
    _file_cache[key] = entry
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{key}.json"
    try:
        cache_path.write_text(json.dumps(entry, indent=2))
    except OSError:
        pass


def _load_session() -> None:
    global _session_reads
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text())
            _session_reads = set(data.get("reads", []))
        except (OSError, json.JSONDecodeError):
            _session_reads = set()


def _save_session() -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps({"reads": list(_session_reads)[-200:]}))
    except OSError:
        pass


class TokenSaverHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the token saver daemon."""

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {}

    def do_GET(self) -> None:
        if self.path == "/health":
            ollama_ok = summarizer.is_available()
            self._send_json({
                "status": "ok",
                "ollama": "connected" if ollama_ok else "disconnected",
                "cache_entries": len(_file_cache),
                "session_reads": len(_session_reads),
            })

        elif self.path == "/ledger":
            session = ledger.session_summary()
            alltime = ledger.all_time_summary()
            self._send_json({"session": session, "all_time": alltime})

        elif self.path == "/cache/status":
            disk_entries = len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.exists() else 0
            self._send_json({
                "memory_entries": len(_file_cache),
                "disk_entries": disk_entries,
                "session_reads": len(_session_reads),
            })

        elif self.path == "/session/reset":
            _session_reads.clear()
            _save_session()
            self._send_json({"status": "reset"})

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = self._read_body()

        if self.path == "/summarize":
            self._handle_summarize(body)
        elif self.path == "/filter":
            self._handle_filter(body)
        elif self.path == "/classify":
            self._handle_classify(body)
        elif self.path == "/ledger/record":
            self._handle_ledger_record(body)
        elif self.path == "/cache/warm":
            self._handle_cache_warm(body)
        elif self.path == "/session/read":
            self._handle_session_read(body)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_summarize(self, body: dict) -> None:
        file_path = body.get("path", "")
        if not file_path or not os.path.isfile(file_path):
            self._send_json({"error": "file not found", "path": file_path}, 404)
            return

        # Check cache
        cached = _load_cache_entry(file_path)
        if cached and cached.get("summary"):
            self._send_json({
                "summary": cached["summary"],
                "cached": True,
                "original_lines": cached.get("line_count", 0),
                "original_chars": cached.get("size_bytes", 0),
            })
            return

        # Read file and summarize
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._send_json({"error": str(e)}, 500)
            return

        line_count = content.count("\n") + 1
        size_bytes = len(content)

        # Don't summarize small files
        if line_count <= 80:
            self._send_json({
                "summary": "",
                "cached": False,
                "skip_reason": "file_too_small",
                "original_lines": line_count,
                "original_chars": size_bytes,
            })
            return

        summary = summarizer.summarize_file(content, file_path)
        if summary:
            entry = {
                "path": file_path,
                "mtime": os.path.getmtime(file_path),
                "size_bytes": size_bytes,
                "line_count": line_count,
                "summary": summary,
                "created_at": time.time(),
            }
            _save_cache_entry(file_path, entry)

        self._send_json({
            "summary": summary,
            "cached": False,
            "original_lines": line_count,
            "original_chars": size_bytes,
        })

    def _handle_filter(self, body: dict) -> None:
        command = body.get("command", "")
        output = body.get("output", "")
        if not output:
            self._send_json({"filtered": "", "saved_chars": 0})
            return

        filtered = summarizer.filter_output(command, output)
        saved = max(0, len(output) - len(filtered))
        self._send_json({
            "filtered": filtered,
            "original_chars": len(output),
            "filtered_chars": len(filtered),
            "saved_chars": saved,
        })

    def _handle_classify(self, body: dict) -> None:
        description = body.get("description", "")
        result = summarizer.classify_task(description)
        self._send_json(result)

    def _handle_ledger_record(self, body: dict) -> None:
        ledger.record(
            event_type=body.get("event_type", "unknown"),
            tool_name=body.get("tool_name", ""),
            file_path=body.get("file_path", ""),
            original_chars=body.get("original_chars", 0),
            saved_chars=body.get("saved_chars", 0),
            notes=body.get("notes", ""),
        )
        self._send_json({"status": "recorded"})

    def _handle_cache_warm(self, body: dict) -> None:
        """Pre-warm cache for a list of files (async)."""
        files = body.get("files", [])
        warmed = 0
        for fp in files[:10]:  # Limit to 10 files
            if not os.path.isfile(fp):
                continue
            cached = _load_cache_entry(fp)
            if cached and cached.get("summary"):
                continue
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                if content.count("\n") > 80:
                    summary = summarizer.summarize_file(content, fp)
                    if summary:
                        entry = {
                            "path": fp,
                            "mtime": os.path.getmtime(fp),
                            "size_bytes": len(content),
                            "line_count": content.count("\n") + 1,
                            "summary": summary,
                            "created_at": time.time(),
                        }
                        _save_cache_entry(fp, entry)
                        warmed += 1
            except OSError:
                continue
        self._send_json({"warmed": warmed, "total_requested": len(files)})

    def _handle_session_read(self, body: dict) -> None:
        """Record that a file was read in this session."""
        file_path = body.get("path", "")
        already_read = file_path in _session_reads
        _session_reads.add(file_path)
        _save_session()
        self._send_json({"path": file_path, "already_read": already_read})


def run_daemon() -> None:
    """Start the token saver daemon."""
    # Write PID file
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Load session state
    _load_session()

    # Setup signal handlers
    def shutdown(signum, frame):
        _save_session()
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server = HTTPServer(("127.0.0.1", PORT), TokenSaverHandler)
    print(f"Token Saver daemon listening on http://127.0.0.1:{PORT}")

    ollama_ok = summarizer.is_available()
    if ollama_ok:
        print(f"  Ollama connected: {summarizer.OLLAMA_URL} ({summarizer.MODEL})")
    else:
        print(f"  Ollama NOT reachable at {summarizer.OLLAMA_URL} — running in cache-only mode")

    try:
        server.serve_forever()
    finally:
        _save_session()
        try:
            PID_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    run_daemon()
