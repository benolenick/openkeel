#!/usr/bin/env python3
"""SessionStart hook for token saver.

Runs at session start to:
  1. Query Hyphae for project briefing + known facts (FAST context)
  2. Build/update codebase index
  3. Generate context prefill (project map, git context)
  4. Pre-warm cache for recently modified files
  5. Reset session state (conversation log, predictions)
  6. Start daemon if not running

Outputs context to stdout for Claude to see.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SESSION_MARKER_DIR = Path.home() / ".openkeel" / "token_saver_sessions"
SESSION_MARKER_TTL = 6 * 3600  # 6h — long enough for a real session, short enough to self-heal


def _read_claude_session_id() -> str:
    """Read Claude Code's session_id from stdin JSON payload.

    Claude Code passes hook input as JSON on stdin. Falls back to a hash of
    cwd+date so multiple hook firings within the same day still dedup if
    stdin is unavailable (manual test, older Claude, etc.).
    """
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw:
                data = json.loads(raw)
                sid = data.get("session_id") or data.get("sessionId")
                if sid:
                    return str(sid)[:32]
    except Exception:
        pass
    # Fallback: cwd + date bucket — dedups repeated hook fires within same day
    seed = f"{os.getcwd()}|{time.strftime('%Y%m%d')}"
    return "fallback-" + hashlib.sha1(seed.encode()).hexdigest()[:12]


def _already_briefed(session_id: str) -> bool:
    """Check if this Claude session already got the full briefing."""
    try:
        SESSION_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker = SESSION_MARKER_DIR / session_id
        if marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < SESSION_MARKER_TTL:
                return True
        marker.touch()
        # Opportunistic cleanup of stale markers
        now = time.time()
        for m in SESSION_MARKER_DIR.iterdir():
            try:
                if now - m.stat().st_mtime > SESSION_MARKER_TTL:
                    m.unlink()
            except Exception:
                pass
        return False
    except Exception:
        return False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")
HYPHAE_URL = os.environ.get("HYPHAE_URL", "http://127.0.0.1:8100")


def _daemon_running() -> bool:
    try:
        req = urllib.request.Request(f"{DAEMON_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_daemon() -> None:
    """Try to start the daemon if it's not running."""
    try:
        project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
        subprocess.Popen(
            [sys.executable, "-m", "openkeel.token_saver.daemon"],
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
    except Exception:
        pass


def _hyphae_recall(query: str, top_k: int = 5, timeout: int = 3) -> list[dict]:
    """Query Hyphae for relevant facts. Returns list of results or empty."""
    try:
        payload = json.dumps({"query": query, "top_k": top_k}).encode()
        req = urllib.request.Request(
            f"{HYPHAE_URL}/recall",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("results", [])
    except Exception:
        return []


def _build_hyphae_context(project_name: str) -> str:
    """Query Hyphae for recent work context and known facts.

    This replaces multiple curl calls Claude would otherwise make during
    the session. By front-loading the context, we prevent ~2-3 Hyphae
    curls averaging 13.7k chars each.
    """
    parts = []

    # 1. Recent work context — what was the user doing?
    recent = _hyphae_recall(f"{project_name} recent work status decisions", top_k=5)
    if recent:
        work_lines = []
        seen = set()
        for r in recent:
            if r.get("score", 0) < 0.4:
                continue
            text = r.get("text", "")[:300]
            # Simple dedup — skip if 60%+ of words overlap with something we already have
            words = set(text.lower().split()[:20])
            if any(len(words & s) > len(words) * 0.6 for s in seen):
                continue
            seen.add(frozenset(words))
            work_lines.append(f"  - {text}")
        if work_lines:
            parts.append("[TOKEN SAVER] Recent work context (from Hyphae):")
            parts.extend(work_lines[:5])

    # 2. Infrastructure facts — IPs, ports, paths
    infra = _hyphae_recall("server IPs infrastructure ports paths jagg kaloth", top_k=5)
    if infra:
        fact_lines = []
        for r in infra:
            if r.get("score", 0) < 0.45:
                continue
            text = r.get("text", "")[:200]
            fact_lines.append(f"  - {text}")
        if fact_lines:
            parts.append("[TOKEN SAVER] Known infrastructure:")
            parts.extend(fact_lines[:4])

    # 3. Lifetime savings summary (compact)
    try:
        from openkeel.token_saver import ledger
        summary = ledger.all_time_summary()
        if summary and summary.get("saved_chars", 0) > 0:
            saved_tok = summary["saved_chars"] // 4
            sessions = summary.get("session_count", 0)
            pct = round(summary["saved_chars"] / max(summary.get("original_chars", 1), 1) * 100, 1)
            parts.append(f"[TOKEN SAVER] Lifetime savings: ~{saved_tok:,} tokens saved across {sessions} sessions ({pct}%)")
    except Exception:
        pass

    return "\n".join(parts)


def main():
    project_root = os.getcwd()
    project_name = os.path.basename(project_root)

    # Dedup: if this Claude session already got the full briefing, emit a
    # tiny warm-reattach line and exit. Prevents the hook from re-dumping
    # ~4-8K tokens of briefing on every re-trigger within the same session.
    claude_session_id = _read_claude_session_id()
    os.environ["TOKEN_SAVER_SESSION"] = claude_session_id
    if _already_briefed(claude_session_id):
        print(f"[TOKEN SAVER] Warm reattach (session {claude_session_id[:8]}) — briefing already injected.")
        try:
            from openkeel.token_saver import ledger
            ledger.record(
                event_type="session_reattach",
                tool_name="SessionStart",
                session_id=claude_session_id,
                notes=f"project: {project_name}",
            )
        except Exception:
            pass
        return

    # Ensure daemon is running
    if not _daemon_running():
        _start_daemon()

    # Reset session state
    try:
        from openkeel.token_saver.engines.conversation_compressor import reset as cc_reset
        cc_reset()
    except Exception:
        pass

    try:
        from openkeel.token_saver.engines.predictive_cache import reset as pc_reset
        pc_reset()
    except Exception:
        pass

    # Reset session reads in daemon
    try:
        req = urllib.request.Request(f"{DAEMON_URL}/session/reset", method="GET")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass

    # Phase 1: Hyphae context (fast, prevents future curl calls)
    # NOTE: Hyphae may still be starting up at session start (takes 10-30s after
    # boot/login). If it's unavailable here, it will likely be ready by the time
    # the agent needs it. Do NOT report it as broken — just skip silently.
    try:
        hyphae_ctx = _build_hyphae_context(project_name)
        if hyphae_ctx:
            print(hyphae_ctx)
    except Exception:
        pass  # Fail-open

    # Build prefill context (project map, git context)
    try:
        from openkeel.token_saver.engines.context_prefill import build_prefill, get_recently_modified_files
        prefill = build_prefill(project_root)
        if prefill:
            print(prefill)

        # Pre-warm cache for recently modified files
        recent_files = get_recently_modified_files(project_root, limit=8)
        if recent_files and _daemon_running():
            payload = json.dumps({"files": recent_files}).encode()
            req = urllib.request.Request(
                f"{DAEMON_URL}/cache/warm",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=30)
            except Exception:
                pass
    except Exception as e:
        print(f"[TOKEN SAVER] Prefill failed: {e}")

    # Log session start
    try:
        from openkeel.token_saver import ledger
        ledger.record(
            event_type="session_start",
            tool_name="SessionStart",
            session_id=claude_session_id,
            notes=f"project: {project_name}",
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
