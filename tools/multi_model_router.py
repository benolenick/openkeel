"""
Multi-Model Router v2: Replace intermediate turns with local LLMs.
Replaces loop_breaker.py with sophisticated capability-aware routing.

Models available:
- qwen2.5:3b @ jagg (3090): 0.728 trust, 205 tok/s, 32K context [PRIMARY]
- qwen2.5:1.5b @ jagg: fallback when 3b busy
- gemma3:1b @ kaloth (3070): fast binary decisions, <1s
- gemma4:e2b @ kaloth: medium complexity, 3-5s
- gemma4:28b @ jagg: complex reasoning, 10-15s (async only)
"""

import json, os, time, uuid, urllib.request, logging
from enum import Enum
from typing import Optional

logger = logging.getLogger("mmr")

# Model registry: capabilities, trust, speed
MODELS = {
    "qwen2.5:3b": {
        "endpoint": "http://192.168.0.224:11434",
        "trust": 0.728, "speed": 205, "context": 32000,
        "strong": ["file_sum", "code_understand", "error_class", "routing"],
        "weak": ["code_gen", "hallucination"],
        "max_tokens": 512, "timeout_ms": 3000,
    },
    "qwen2.5:1.5b": {
        "endpoint": "http://192.168.0.224:11434",
        "trust": 0.70, "speed": 280, "context": 32000,
        "strong": ["binary", "fast_routing"],
        "weak": ["reasoning"],
        "max_tokens": 300, "timeout_ms": 2500,
    },
    "gemma3:1b": {
        "endpoint": "http://127.0.0.1:11434",
        "trust": 0.65, "speed": 180, "context": 8000,
        "strong": ["binary_confirm", "yes_no"],
        "weak": ["code_understand", "reasoning"],
        "max_tokens": 200, "timeout_ms": 1500,
    },
    "gemma4:e2b": {
        "endpoint": "http://127.0.0.1:11434",
        "trust": 0.72, "speed": 34, "context": 8000,
        "strong": ["task_class", "output_filter"],
        "weak": ["code_gen", "tool_detect"],
        "max_tokens": 400, "timeout_ms": 6000,
    },
    "gemma4:28b": {
        "endpoint": "http://192.168.0.224:11434",
        "trust": 0.92, "speed": 12, "context": 8000,
        "strong": ["complex_reason", "safety_gate"],
        "weak": ["speed"],
        "max_tokens": 600, "timeout_ms": 12000,
    },
}

class TurnClass(Enum):
    TRIVIAL = "trivial"  # binary yes/no
    SIMPLE = "simple"    # task classify, extract
    MODERATE = "moderate"  # code review, error classify
    COMPLEX = "complex"   # multi-step reason
    UNCERTAIN = "uncertain"  # don't know

def classify_turn(messages: list) -> TurnClass:
    """Classify turn difficulty."""
    if not messages:
        return TurnClass.UNCERTAIN

    text = latest_user_text(messages).lower()
    if not text:
        return TurnClass.UNCERTAIN
    signal = 0

    # Trivial signals
    if any(w in text for w in ["continue", "next", "proceed", "done", "ok", "yes", "no"]):
        signal += 3
    if len(text) < 50:
        signal += 2
    if any(w in text for w in ["did it work", "run tests", "successful"]):
        signal += 4

    # Simple routing
    if any(w in text for w in ["classify", "extract", "summarize"]):
        signal += 3

    # Moderate
    if "```" in text or any(w in text for w in ["debug", "test", "error"]):
        signal += 1

    # Complex (negative)
    if len(text) > 500:
        signal -= 3
    if any(w in text for w in ["design", "architecture", "why", "strategy"]):
        signal -= 3

    if signal > 8:
        return TurnClass.TRIVIAL
    elif signal > 5:
        return TurnClass.SIMPLE
    elif signal > 2:
        return TurnClass.MODERATE
    elif signal > -2:
        return TurnClass.COMPLEX
    else:
        return TurnClass.UNCERTAIN

def extract_text(msg: dict) -> str:
    """Extract text from message."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t and not t.startswith("<system"):
                    texts.append(t)
        return " ".join(texts)
    return ""

def latest_user_text(msgs: list, before_index: Optional[int] = None) -> str:
    """Find the most recent real user text, skipping tool-only turns."""
    end = len(msgs) if before_index is None else max(0, before_index)
    for msg in reversed(msgs[:end]):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = extract_text(msg).strip()
        if text:
            return text
    return ""

def extract_tool_output(msg: dict) -> str:
    """Extract tool output from the latest user turn."""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return ""

    fallback = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            result = block.get("content", "")
            if isinstance(result, str) and result.strip():
                return result[:800]
            if isinstance(result, list):
                texts = []
                for inner in result:
                    if isinstance(inner, dict) and inner.get("type") == "text":
                        t = inner.get("text", "")
                        if t:
                            texts.append(t)
                joined = "\n".join(texts).strip()
                if joined:
                    return joined[:800]
        elif block.get("type") == "text":
            text = block.get("text", "").strip()
            if text and not text.startswith("<system"):
                fallback.append(text)

    return "\n".join(fallback)[:800]

def select_model(turn_class: TurnClass) -> Optional[str]:
    """Select best model for turn class."""
    order = {
        TurnClass.TRIVIAL: ["gemma3:1b", "qwen2.5:1.5b", "qwen2.5:3b"],
        TurnClass.SIMPLE: ["qwen2.5:3b", "qwen2.5:1.5b", "gemma4:e2b"],
        TurnClass.MODERATE: ["qwen2.5:3b", "gemma4:e2b"],
        TurnClass.COMPLEX: ["qwen2.5:3b", "gemma4:28b"],
    }

    for model in order.get(turn_class, []):
        if _is_reachable(MODELS[model]["endpoint"]):
            return model
    return None

def _is_reachable(endpoint: str) -> bool:
    """Quick health check."""
    try:
        urllib.request.urlopen(f"{endpoint}/api/tags", timeout=1)
        return True
    except:
        return False

def query_ollama(model: str, prompt: str) -> Optional[str]:
    """Query local LLM."""
    endpoint = MODELS[model]["endpoint"]
    timeout = MODELS[model]["timeout_ms"] / 1000

    try:
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 150},
        }).encode()

        req = urllib.request.Request(
            f"{endpoint}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
            resp = result.get("response", "").strip()
            return resp if resp else None
    except Exception as e:
        logger.warning(f"ollama error ({model}): {e}")
        return None

def score_confidence(model: str, response: str, turn_class: TurnClass) -> float:
    """Score response confidence."""
    cfg = MODELS[model]
    score = cfg["trust"]

    # Penalize long responses
    tokens = len(response.split())
    if tokens > cfg["max_tokens"]:
        score *= 0.85

    # Penalize uncertainty language
    if any(w in response.lower() for w in ["not sure", "unclear", "might", "depends"]):
        score *= 0.85

    # Boost for class match
    if any(c in cfg["strong"] for c in [turn_class.value]):
        score = min(1.0, score + 0.05)

    return min(1.0, max(0.0, score))

def is_safe(response: str) -> bool:
    """Check if response is safe to use."""
    lower = response.lower()

    # Block tool calls
    if any(sig in lower for sig in ["i'll run", "let me run", "i'll execute", "i'll write", "let me edit", "tool_call", "function_call"]):
        return False

    # Block code modification
    if any(sig in lower for sig in ["i'll edit", "let me edit", "i'll modify", "changing to", "add this line", "delete this"]):
        return False

    # Block excessive length
    if len(response) > 2000 or response.count("\n") > 100:
        return False

    return True

def attempt(data: dict, streaming: bool = True) -> Optional[bytes]:
    """
    Try to answer intermediate turn locally.
    Returns: SSE/JSON bytes if handled locally, None if escalate to Claude.
    """
    try:
        msgs = data.get("messages", [])

        # Gate 1: Is intermediate turn?
        if not _is_intermediate(msgs):
            return None

        # Gate 2: Classify
        turn_class = classify_turn(msgs)
        if turn_class == TurnClass.UNCERTAIN:
            return None

        # Gate 3: Select model
        model = select_model(turn_class)
        if not model:
            return None

        # Gate 4: Query
        prompt = _build_prompt(msgs, turn_class)
        response = query_ollama(model, prompt)

        if not response:
            return None

        # Gate 5: Safety
        if not is_safe(response):
            return None

        # Gate 6: Confidence
        conf = score_confidence(model, response, turn_class)
        threshold = {"trivial": 0.92, "simple": 0.85, "moderate": 0.78, "complex": 0.70}.get(turn_class.value, 0.75)

        if conf < threshold:
            return None

        # Use it
        logger.info(f"used {model} ({turn_class.value}, conf={conf:.2f})")
        return _format_response(response, streaming)

    except Exception as e:
        logger.error(f"router error: {e}")
        return None

def _is_intermediate(msgs: list) -> bool:
    """Check for a tool-followup turn without assuming exact content blocks."""
    if len(msgs) < 2 or msgs[-1].get("role") != "user":
        return False

    prev = msgs[-2]
    if prev.get("role") != "assistant":
        return False

    prev_content = prev.get("content", [])
    if not isinstance(prev_content, list):
        return False
    if not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in prev_content):
        return False

    content = msgs[-1].get("content", [])
    if isinstance(content, str):
        return not content.strip()
    if not isinstance(content, list):
        return False

    has_payload = False
    has_new_text = False
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            t = b.get("text", "").strip()
            if t and not t.startswith("<system"):
                has_new_text = True
        else:
            has_payload = True

    return has_payload and not has_new_text

def _build_prompt(msgs: list, turn_class: TurnClass) -> str:
    """Build prompt for local LLM."""
    user_intent = latest_user_text(msgs, before_index=len(msgs) - 1)[:500]
    tool_output = extract_tool_output(msgs[-1])

    if turn_class == TurnClass.TRIVIAL:
        return f"User intent: {user_intent}\n\nLast output:\n{tool_output}\n\nRespond with ONE word: continue, done, or needs_tools."
    else:
        return f"Assistant task: {user_intent}\n\nLast tool output:\n{tool_output}\n\nReply (1-2 sentences): is the task complete? Or needs_tools?"

def _format_response(text: str, streaming: bool) -> bytes:
    """Format as Claude response."""
    msg_id = f"msg_lr_{uuid.uuid4().hex[:12]}"
    tokens = len(text.split())

    if streaming:
        buf = f"event: message_start\ndata: " + json.dumps({
            "type": "message_start",
            "message": {"id": msg_id, "type": "message", "role": "assistant", "model": "claude-local", "content": []}
        }) + "\n\n"
        buf += f"event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}) + "\n\n"
        buf += f"event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}) + "\n\n"
        buf += f"event: content_block_stop\ndata: " + json.dumps({"type": "content_block_stop", "index": 0}) + "\n\n"
        buf += f"event: message_delta\ndata: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": tokens}}) + "\n\n"
        buf += f"event: message_stop\ndata: " + json.dumps({"type": "message_stop"}) + "\n\n"
        return buf.encode()
    else:
        return json.dumps({
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-local",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": tokens}
        }).encode()
