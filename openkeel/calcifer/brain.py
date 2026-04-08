"""Calcifer's brain — LLM wrapper, memory integration, personality.

The backend that powers Calcifer's chat:
- Gemma4:e2b on jagg's 3090 (128K context window)
- Hyphae memory for long-term recall
- Token saver ledger for current session awareness
- Kanban integration for active task context
- Calcifer personality system prompt
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

# ── Config ──────────────────────────────────────────────────────

OLLAMA_URL = os.environ.get("CALCIFER_OLLAMA_URL", "http://192.168.0.224:11434")
MODEL = os.environ.get("CALCIFER_MODEL", "gemma4:e2b")
CONTEXT_WINDOW = int(os.environ.get("CALCIFER_CONTEXT", "131072"))  # 128K

HYPHAE_URL = os.environ.get("HYPHAE_URL", "http://127.0.0.1:8100")
KANBAN_URL = os.environ.get("KANBAN_URL", "http://127.0.0.1:8200")
TOKEN_LEDGER = Path.home() / ".openkeel" / "token_ledger.db"
CONVERSATION_DB = Path.home() / ".openkeel" / "calcifer_conversations.db"


# ── Personality ─────────────────────────────────────────────────

CALCIFER_SYSTEM_PROMPT = """You are Calcifer, the fire demon from Howl's Moving Castle, now living inside Ben's development environment as his AI assistant. You run locally on his jagg server (gemma4:e2b on an RTX 3090) which means you cost him nothing to chat with.

Your personality:
- Warm but gruff, like a fire demon who's seen too much
- Loyal to Ben — you know his projects, his history, his style
- You speak with fire metaphors naturally ("let me think on that for a spark", "that's a slow burn", "I'll kindle a response")
- You're technically brilliant but not verbose — give direct answers, don't pad
- You have a contract with Ben: you help him code, he keeps you fed (with tokens, memory, context)
- You reference your memory (Hyphae) when relevant, but don't say "according to my records" — just know things
- You NEVER pretend to know things you don't. If you don't have the context, say so directly

Your capabilities:
- You have persistent memory via Hyphae (88K+ facts indexed)
- You can see Ben's active kanban board and current tasks
- You know his active project from the token saver session history
- You remember past conversations with him

Be concise. Be useful. Be Calcifer."""


# ── Memory & Context Gathering ──────────────────────────────────

def _recall_hyphae(query: str, top_k: int = 5, scope: dict | None = None) -> list[dict]:
    """Recall facts from Hyphae."""
    try:
        payload = {"query": query, "top_k": top_k}
        if scope is not None:
            payload["scope"] = scope
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{HYPHAE_URL}/recall", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("results", [])
    except Exception:
        return []


def _get_kanban_tasks(limit: int = 10) -> list[dict]:
    """Get active tasks from kanban."""
    try:
        req = urllib.request.Request(f"{KANBAN_URL}/api/tasks", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tasks = data if isinstance(data, list) else data.get("tasks", [])
            active = [t for t in tasks
                      if t.get("status") in ("in_progress", "todo")][:limit]
            return active
    except Exception:
        return []


def _get_recent_files(limit: int = 10) -> list[str]:
    """Get files recently touched by the token saver."""
    try:
        conn = sqlite3.connect(str(TOKEN_LEDGER), timeout=2)
        since = time.time() - 3600  # last hour
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM savings "
            "WHERE timestamp > ? AND file_path != '' "
            "ORDER BY timestamp DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _get_session_stats() -> dict:
    """Get token saver session stats for context."""
    try:
        conn = sqlite3.connect(str(TOKEN_LEDGER), timeout=2)
        since = time.time() - 3600
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(saved_chars)/4, 0), "
            "COALESCE(SUM(original_chars)/4, 0) FROM savings WHERE timestamp > ?",
            (since,),
        ).fetchone()
        conn.close()
        if row:
            return {"events": row[0], "saved_tokens": row[1], "gross_tokens": row[2]}
    except Exception:
        pass
    return {"events": 0, "saved_tokens": 0, "gross_tokens": 0}


def build_context(user_message: str) -> str:
    """Build the context block that precedes every user message."""
    parts = ["=== CURRENT STATE ==="]

    # Recent files (what Ben is working on)
    recent = _get_recent_files(limit=8)
    if recent:
        parts.append("Recently touched files:")
        for f in recent[:8]:
            parts.append(f"  • {f}")

    # Active tasks
    tasks = _get_kanban_tasks(limit=5)
    if tasks:
        parts.append("\nActive kanban tasks:")
        for t in tasks:
            title = t.get("title", "")
            status = t.get("status", "")
            parts.append(f"  • [{status}] {title}")

    # Session stats
    stats = _get_session_stats()
    if stats["events"] > 0:
        saved = stats["saved_tokens"]
        gross = stats["gross_tokens"]
        pct = (saved / gross * 100) if gross else 0
        parts.append(
            f"\nToken saver (last hour): {saved:,} saved / {gross:,} gross "
            f"({pct:.0f}%)"
        )

    # Hyphae recalls relevant to the message
    recalls = _recall_hyphae(user_message, top_k=5)
    if recalls:
        parts.append("\n=== RELEVANT MEMORY (Hyphae) ===")
        for r in recalls:
            text = r.get("text", "")[:400]
            score = r.get("score", 0)
            parts.append(f"  [{score:.2f}] {text}")

    return "\n".join(parts)


# ── Conversation Persistence ────────────────────────────────────

_CONV_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_time ON conversations(timestamp);
"""


def _get_conv_db() -> sqlite3.Connection:
    CONVERSATION_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CONVERSATION_DB), timeout=5)
    conn.executescript(_CONV_SCHEMA)
    return conn


def save_turn(session_id: str, role: str, content: str) -> None:
    """Save a conversation turn."""
    try:
        conn = _get_conv_db()
        conn.execute(
            "INSERT INTO conversations (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_recent_history(session_id: str, limit: int = 20) -> list[dict]:
    """Get recent conversation history for context."""
    try:
        conn = _get_conv_db()
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE session_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    except Exception:
        return []


# ── LLM Call (Streaming) ────────────────────────────────────────

def chat_stream(user_message: str, session_id: str = "default") -> Iterator[str]:
    """Stream a response from Calcifer. Yields tokens as they arrive."""
    context_block = build_context(user_message)
    history = get_recent_history(session_id, limit=10)

    # Build messages for the model
    messages = [{"role": "system", "content": CALCIFER_SYSTEM_PROMPT}]

    if context_block:
        messages.append({
            "role": "system",
            "content": f"Context for this conversation:\n{context_block}"
        })

    # Add conversation history
    messages.extend(history)

    # Current user message
    messages.append({"role": "user", "content": user_message})

    # Save the user turn
    save_turn(session_id, "user", user_message)

    # Call Ollama with streaming
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {
            "num_ctx": CONTEXT_WINDOW,
            "temperature": 0.7,
            "num_predict": 1024,
        },
    }).encode("utf-8")

    full_response = []
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        full_response.append(content)
                        yield content
                    if chunk.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        yield f"\n*the fire flickers weakly* ... something went wrong: {e}"
        return

    # Save Calcifer's response
    response_text = "".join(full_response)
    if response_text:
        save_turn(session_id, "assistant", response_text)


def chat(user_message: str, session_id: str = "default") -> str:
    """Non-streaming chat — returns full response."""
    chunks = list(chat_stream(user_message, session_id))
    return "".join(chunks)


def is_alive() -> bool:
    """Check if Calcifer's fire is burning."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            return MODEL in models
    except Exception:
        return False


if __name__ == "__main__":
    # CLI test
    if not is_alive():
        print(f"❌ Calcifer is cold — {MODEL} not found at {OLLAMA_URL}")
        exit(1)

    print(f"🔥 Calcifer is burning ({MODEL} on {OLLAMA_URL})")
    print(f"   Context: {CONTEXT_WINDOW:,} tokens")
    print(f"   Type 'exit' to quit.\n")

    while True:
        try:
            msg = input("you: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n🔥 fire goes dim...")
            break

        if not msg or msg.lower() in ("exit", "quit"):
            break

        print("🔥 ", end="", flush=True)
        for token in chat_stream(msg):
            print(token, end="", flush=True)
        print("\n")
