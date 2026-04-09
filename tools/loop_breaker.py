"""
Loop Breaker: intercept mid-task Claude turns and answer locally.

The problem: Claude Code sends dozens of API calls per task. Most are
"intermediate" turns where Claude just received tool results and decides
what to do next. A local LLM can handle many of these.

Strategy:
  - Only fire on intermediate turns (last message is tool_result)
  - Only fire when user intent is short/clear (< 400 chars)
  - NEVER generate tool calls locally (too risky)
  - Return text-only responses, faking SSE if needed
  - Aggressive fallthrough: any doubt → skip, forward to Claude

Log: ~/.openkeel/loop_breaker.jsonl
Disable: TSPROXY_NO_LOOP_BREAKER=1
"""

import json
import os
import time
import uuid
import urllib.request
from typing import Optional

OLLAMA_URL = os.environ.get("TSPROXY_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("TSPROXY_LB_MODEL", "gemma3:1b")
BYPASS_MODEL = "claude-haiku-4-5-20251001"

# Only try bypass for short user prompts (long = complex task = don't risk it)
MAX_INTENT_CHARS = 400
# Skip if Ollama takes longer than this
OLLAMA_TIMEOUT = 8  # Increased from 4 — Ollama can be slow
# Min tool results in conversation before we try (need some context)
MIN_TOOL_TURNS = 1

_LOG = os.path.expanduser("~/.openkeel/loop_breaker.jsonl")
_stats = {"attempted": 0, "used": 0, "skipped": 0, "errors": 0}


def _log(entry: dict) -> None:
    try:
        with open(_LOG, "a") as f:
            f.write(json.dumps({**entry, "ts": time.time()}) + "\n")
    except Exception:
        pass


def _ollama_call(prompt: str) -> Optional[str]:
    try:
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 150, "stop": ["NEEDS_TOOLS", "\n\n\n"]},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            result = json.loads(r.read())
            resp = result.get("response", "").strip()
            return resp if resp else None
    except Exception as e:
        _log({"event": "ollama_error", "error": str(e)[:100]})
        return None


def _extract_context(msgs: list) -> dict:
    """Pull user intent, last tool results, and conversation shape."""
    user_intent = ""
    tool_result_count = 0
    last_tool_results = []
    last_assistant_text = ""

    # Scan forward for user intent (first real user text)
    for m in msgs:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            t = content.strip()
            if t and not t.startswith("<system"):
                user_intent = t[:MAX_INTENT_CHARS]
                break
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    t = b.get("text", "")
                    if t and not t.startswith("<system"):
                        user_intent = t[:MAX_INTENT_CHARS]
                        break
            if user_intent:
                break

    # Scan for tool results and last assistant text
    for m in msgs:
        role = m.get("role", "")
        content = m.get("content", [])
        if role == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_result_count += 1
                    rc = b.get("content", "")
                    if isinstance(rc, str):
                        last_tool_results.append(rc[:400])
                    elif isinstance(rc, list):
                        for rb in rc:
                            if isinstance(rb, dict) and rb.get("type") == "text":
                                last_tool_results.append(rb.get("text", "")[:400])
        elif role == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    last_assistant_text = b.get("text", "")[:300]

    return {
        "user_intent": user_intent,
        "tool_result_count": tool_result_count,
        "last_tool_results": last_tool_results[-3:],  # Last 3 results
        "last_assistant_text": last_assistant_text,
    }


def _is_intermediate_turn(msgs: list) -> bool:
    """True if the last user message is all tool_results (no fresh text)."""
    if not msgs:
        return False
    last = msgs[-1]
    if last.get("role") != "user":
        return False
    content = last.get("content", [])
    if not isinstance(content, list):
        return False
    has_tool_result = False
    has_real_text = False
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            has_tool_result = True
        elif b.get("type") == "text":
            t = b.get("text", "")
            if t and not t.startswith("<system"):
                has_real_text = True
    return has_tool_result and not has_real_text


def _fake_sse(text: str) -> bytes:
    """Build a fake Claude SSE stream for a text-only response."""
    msg_id = f"msg_lb_{uuid.uuid4().hex[:12]}"
    usage = {"input_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "output_tokens": max(1, len(text.split()))}

    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": BYPASS_MODEL,
            "stop_reason": None, "stop_sequence": None, "usage": usage,
        }}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}),
        ("ping", {"type": "ping"}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": usage["output_tokens"]}}),
        ("message_stop", {"type": "message_stop"}),
    ]

    buf = ""
    for event_type, data in events:
        buf += f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    return buf.encode()


def _fake_json(text: str) -> bytes:
    """Build a fake Claude JSON response for non-streaming."""
    msg_id = f"msg_lb_{uuid.uuid4().hex[:12]}"
    return json.dumps({
        "id": msg_id, "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": BYPASS_MODEL,
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0,
                  "output_tokens": max(1, len(text.split()))},
    }).encode()


# Phrases that indicate Ollama wants to use tools — we skip in that case
_TOOL_SIGNALS = (
    "i'll run", "let me run", "let me read", "i need to", "i should check",
    "i'll check", "let me check", "i'll look", "let me look", "i'll execute",
    "running the", "reading the", "let me", "i need", "```",
)

# Phrases that indicate Ollama is confident it's done
_DONE_SIGNALS = (
    "done", "complete", "successful", "working", "fixed", "applied",
    "restarted", "active", "running", "looks good", "no errors",
)


def attempt(data: dict, streaming: bool = True) -> Optional[bytes]:
    """
    Try to answer this API request locally.

    Returns SSE/JSON bytes if handled locally, None if Claude should handle it.
    """
    if os.environ.get("TSPROXY_NO_LOOP_BREAKER"):
        return None

    _stats["attempted"] += 1
    t0 = time.time()

    try:
        msgs = data.get("messages", [])

        # Gate 1: must be an intermediate turn (last msg = tool results only)
        if not _is_intermediate_turn(msgs):
            _log({"event": "lb_skip", "reason": "not_intermediate", "n_msgs": len(msgs)})
            _stats["skipped"] += 1
            return None

        ctx = _extract_context(msgs)

        # Gate 2: must have a clear short user intent
        if not ctx["user_intent"] or len(ctx["user_intent"]) > MAX_INTENT_CHARS:
            _log({"event": "lb_skip", "reason": "no_intent", "intent_len": len(ctx.get("user_intent", ""))})
            _stats["skipped"] += 1
            return None

        # Gate 3: must have at least some tool results to work with
        if ctx["tool_result_count"] < MIN_TOOL_TURNS:
            _log({"event": "lb_skip", "reason": "no_tool_results", "count": ctx["tool_result_count"]})
            _stats["skipped"] += 1
            return None

        tool_summary = "\n---\n".join(ctx["last_tool_results"])

        prompt = (
            "You are a coding assistant mid-task. Respond with ONE of:\n"
            "- A SHORT confirmation (1-2 sentences) if the task looks complete\n"
            "- 'NEEDS_TOOLS' if more tool calls are required\n\n"
            f"User goal: {ctx['user_intent']}\n"
            f"Last tool output:\n{tool_summary[:600]}\n"
            f"Prior assistant note: {ctx['last_assistant_text'][:150]}\n\n"
            "Reply (NEEDS_TOOLS or short confirmation only):"
        )

        response = _ollama_call(prompt)
        latency_ms = int((time.time() - t0) * 1000)

        if not response:
            _stats["errors"] += 1
            _log({"event": "lb_skip", "reason": "ollama_none", "latency_ms": latency_ms})
            return None

        resp_low = response.lower()

        # Skip if Ollama needs tools
        if "needs_tools" in resp_low:
            _stats["skipped"] += 1
            _log({"event": "lb_skip", "reason": "needs_tools", "latency_ms": latency_ms})
            return None

        # Skip if response sounds like it wants to use tools
        if any(sig in resp_low for sig in _TOOL_SIGNALS):
            _stats["skipped"] += 1
            _log({"event": "lb_skip", "reason": "tool_signal", "latency_ms": latency_ms,
                  "response": response[:100]})
            return None

        # Skip if too long (probably hallucinating)
        if len(response) > 400:
            _stats["skipped"] += 1
            _log({"event": "lb_skip", "reason": "too_long", "len": len(response)})
            return None

        # Use it
        _stats["used"] += 1
        _log({
            "event": "lb_used",
            "intent": ctx["user_intent"][:80],
            "response": response[:120],
            "latency_ms": latency_ms,
            "streaming": streaming,
        })

        return _fake_sse(response) if streaming else _fake_json(response)

    except Exception as e:
        _stats["errors"] += 1
        _log({"event": "lb_error", "error": str(e)})
        return None


def stats() -> dict:
    return dict(_stats)
