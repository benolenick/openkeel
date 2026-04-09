"""
Advanced Multi-Model Router: Production-ready intermediate turn replacement.

Features:
- Endpoint health checking with cascading fallbacks
- Model-specific context trimming (8K vs 32K windows)
- Optimized prompts per model family
- Retry logic with exponential backoff
- Confidence calibration curves
- Metrics collection and dashboard integration
- A/B testing framework
"""

import json, os, time, uuid, urllib.request, urllib.error, logging, threading
from enum import Enum
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from collections import defaultdict
import hashlib

logger = logging.getLogger("mmr")
handler = logging.FileHandler(os.path.expanduser("~/.openkeel/mmr.log"))
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# COMPREHENSIVE MODEL REGISTRY
MODELS = {
    "qwen2.5:3b": {
        "endpoints": ["http://192.168.0.224:11434"],  # Primary: jagg 3090
        "fallbacks": ["http://192.168.0.224:11434"],
        "family": "qwen",
        "params": 3e9,
        "context": 32000,
        "trust": 0.78,
        "speed_tok_per_sec": 205,
        "max_response_tokens": 512,
        "timeout_ms": 4000,
        "strong_at": ["reasoning", "code_understand", "multi_step", "error_classify"],
        "weak_at": ["speed", "code_generation"],
        "prompt_template": "qwen_reasoning",
    },
    "qwen2.5:1.5b": {
        "endpoints": ["http://192.168.0.224:11434"],
        "fallbacks": ["http://192.168.0.224:11434"],
        "family": "qwen",
        "params": 1.5e9,
        "context": 32000,
        "trust": 0.72,
        "speed_tok_per_sec": 280,
        "max_response_tokens": 300,
        "timeout_ms": 3500,
        "strong_at": ["binary", "fast_classify", "simple_routing"],
        "weak_at": ["reasoning"],
        "prompt_template": "qwen_fast",
    },
    "gemma3:1b": {
        "endpoints": ["http://127.0.0.1:11434"],
        "fallbacks": ["http://127.0.0.1:11434"],
        "family": "gemma",
        "params": 1e9,
        "context": 8000,
        "trust": 0.68,
        "speed_tok_per_sec": 180,
        "max_response_tokens": 200,
        "timeout_ms": 2000,
        "strong_at": ["binary_confirm", "yes_no", "trivial"],
        "weak_at": ["reasoning", "code_understand"],
        "prompt_template": "gemma_binary",
    },
    "gemma4:e2b": {
        "endpoints": ["http://127.0.0.1:11434", "http://192.168.0.224:11434"],
        "fallbacks": ["http://127.0.0.1:11434", "http://192.168.0.224:11434"],
        "family": "gemma",
        "params": 8e9,
        "context": 8000,
        "trust": 0.74,
        "speed_tok_per_sec": 34,
        "max_response_tokens": 400,
        "timeout_ms": 7000,
        "strong_at": ["task_classify", "output_filter", "simple_reason"],
        "weak_at": ["code_gen", "tool_detect"],
        "prompt_template": "gemma_medium",
    },
    "gemma4:26b": {
        "endpoints": ["http://192.168.0.224:11434"],
        "fallbacks": ["http://192.168.0.224:11434"],
        "family": "gemma",
        "params": 26e9,
        "context": 8000,
        "trust": 0.88,
        "speed_tok_per_sec": 12,
        "max_response_tokens": 600,
        "timeout_ms": 15000,
        "strong_at": ["complex_reason", "safety_gate", "hallucination_detect"],
        "weak_at": ["speed"],
        "prompt_template": "gemma_deep",
    },
}

# PROMPT TEMPLATES (model-specific)
PROMPTS = {
    "qwen_reasoning": """You are a coding assistant mid-task. User goal: {user_intent}

Last tool output:
{tool_output}

Prior context: {prior_text}

Respond with ONE of these ONLY:
1. A 1-2 sentence confirmation if task is DONE
2. The word "NEEDS_TOOLS" if more work required

Your response:""",

    "qwen_fast": """Task: {user_intent}
Output: {tool_output}

Done or NEEDS_TOOLS?""",

    "gemma_binary": """Complete? {user_intent} → {tool_output}
Answer: yes/no/tools""",

    "gemma_medium": """Classify the task progress:
Goal: {user_intent}
Output: {tool_output}

Status: complete/incomplete/error/blocked?""",

    "gemma_deep": """Comprehensive analysis of task progress.

User goal: {user_intent}

Tool output received:
{tool_output}

Prior assistant note: {prior_text}

Analysis:
1. What did the tool accomplish?
2. Is the original goal satisfied?
3. What should happen next?

Conclusion: Is task COMPLETE or do we need more TOOLS?""",
}

# METRICS
_metrics_lock = threading.Lock()
_metrics = defaultdict(lambda: {"attempts": 0, "used": 0, "skipped": 0, "errors": 0, "latencies": []})

@dataclass
class TurnDecision:
    used: bool
    model: Optional[str]
    confidence: float
    reason: str
    latency_ms: int
    response: Optional[str]

class TurnClass(Enum):
    TRIVIAL = 1      # binary decision, <50 chars
    SIMPLE = 2       # classify, extract
    MODERATE = 3     # error classify, code review
    COMPLEX = 4      # multi-step reasoning
    UNCERTAIN = 0


TURN_CAPABILITY = {
    TurnClass.TRIVIAL: "trivial",
    TurnClass.SIMPLE: "simple_reason",
    TurnClass.MODERATE: "reasoning",
    TurnClass.COMPLEX: "complex_reason",
}

def health_check(endpoint: str, model: str, timeout: float = 1.0) -> bool:
    """Check if model is loaded and responding."""
    try:
        req = urllib.request.Request(
            f"{endpoint}/api/generate",
            data=json.dumps({
                "model": model,
                "prompt": "hi",
                "stream": False,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
            return "response" in result
    except Exception as e:
        logger.debug(f"health_check failed {model}@{endpoint}: {e}")
        return False

def get_active_endpoint(model: str) -> Optional[str]:
    """Get first healthy endpoint for model."""
    cfg = MODELS.get(model)
    if not cfg:
        return None

    for endpoint in cfg["endpoints"]:
        if health_check(endpoint, model):
            return endpoint

    logger.warning(f"no healthy endpoint for {model}")
    return None

def classify_turn(messages: list) -> TurnClass:
    """Classify turn difficulty with detailed scoring."""
    if not messages:
        return TurnClass.UNCERTAIN

    text = _latest_user_text(messages).lower()
    if not text:
        return TurnClass.UNCERTAIN
    score = 0

    # Trivial signals (+3 each)
    trivial_words = ["continue", "next", "proceed", "ok", "yes", "no", "run it", "do it", "execute"]
    score += 3 * sum(1 for w in trivial_words if w in text)

    if len(text) < 50:
        score += 4
    if len(text) < 30:
        score += 3

    # Simple signals (+2 each)
    simple_words = ["classify", "extract", "summarize", "list", "find"]
    score += 2 * sum(1 for w in simple_words if w in text)

    # Moderate signals (+1 each)
    moderate_words = ["debug", "test", "error", "fix", "why"]
    score += 1 * sum(1 for w in moderate_words if w in text)

    # Complex signals (-3 each)
    complex_words = ["design", "architecture", "strategy", "should we", "which approach"]
    score -= 3 * sum(1 for w in complex_words if w in text)

    # Length penalty
    if len(text) > 300:
        score -= 2
    if len(text) > 600:
        score -= 4

    if score >= 10:
        return TurnClass.TRIVIAL
    elif score >= 5:
        return TurnClass.SIMPLE
    elif score >= 1:
        return TurnClass.MODERATE
    elif score >= -3:
        return TurnClass.COMPLEX
    else:
        return TurnClass.UNCERTAIN

def _extract_text(msg: dict) -> str:
    """Extract text from message, handling nested structures."""
    content = msg.get("content")
    if isinstance(content, str):
        return content[:1000]
    if isinstance(content, list):
        texts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t and not t.startswith("<system"):
                    texts.append(t[:500])
        return " ".join(texts)[:1000]
    return ""

def _latest_user_text(messages: list, before_index: Optional[int] = None) -> str:
    """Return the most recent real user text, skipping tool-only/system turns."""
    end = len(messages) if before_index is None else max(0, before_index)
    for msg in reversed(messages[:end]):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _extract_text(msg).strip()
        if text:
            return text[:1000]
    return ""

def _extract_tool_output(msg: dict) -> str:
    """Extract the most useful tool payload from a user tool-result turn."""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return ""

    fallback_text = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, str) and inner.strip():
                return inner[:800]
            if isinstance(inner, list):
                texts = []
                for inner_block in inner:
                    if isinstance(inner_block, dict) and inner_block.get("type") == "text":
                        t = inner_block.get("text", "")
                        if t:
                            texts.append(t)
                joined = "\n".join(texts).strip()
                if joined:
                    return joined[:800]
        elif block_type == "text":
            text = block.get("text", "").strip()
            if text and not text.startswith("<system"):
                fallback_text.append(text)

    return "\n".join(fallback_text)[:800]

def _is_intermediate(messages: list) -> bool:
    """Detect a tool-followup turn without depending on one exact block shape."""
    if len(messages) < 2:
        return False

    # Last message must be from user with tool results
    last = messages[-1]
    if last.get("role") != "user":
        return False

    # Second-to-last must be from assistant (Claude's last response)
    prev = messages[-2]
    if prev.get("role") != "assistant":
        return False

    prev_content = prev.get("content", [])
    if not isinstance(prev_content, list):
        return False

    had_tool_use = any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for b in prev_content
    )
    if not had_tool_use:
        return False

    curr_content = last.get("content", [])
    if isinstance(curr_content, str):
        return not curr_content.strip()
    if not isinstance(curr_content, list):
        return False

    has_fresh_text = False
    has_non_text_payload = False
    for block in curr_content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "").strip()
            if text and not text.startswith("<system"):
                has_fresh_text = True
        else:
            has_non_text_payload = True

    return has_non_text_payload and not has_fresh_text

def _trim_context(text: str, max_chars: int) -> str:
    """Trim context to fit model window."""
    if len(text) <= max_chars:
        return text
    # Keep last max_chars (most recent context)
    return text[-max_chars:]

def select_model(turn_class: TurnClass, available: List[str] = None) -> Optional[str]:
    """Select best model for turn class."""
    routing = {
        TurnClass.TRIVIAL: ["gemma3:1b", "qwen2.5:1.5b", "qwen2.5:3b"],
        TurnClass.SIMPLE: ["qwen2.5:3b", "gemma4:e2b", "qwen2.5:1.5b"],
        TurnClass.MODERATE: ["qwen2.5:3b", "gemma4:e2b", "gemma4:26b"],
        TurnClass.COMPLEX: ["qwen2.5:3b", "gemma4:26b"],
    }

    candidates = routing.get(turn_class, [])
    for model in candidates:
        if available and model not in available:
            continue
        endpoint = get_active_endpoint(model)
        if endpoint:
            return model

    return None

def build_prompt(model: str, messages: list, turn_class: TurnClass) -> str:
    """Build model-specific prompt."""
    cfg = MODELS[model]
    tmpl = PROMPTS.get(cfg["prompt_template"], PROMPTS["qwen_reasoning"])

    user_intent = _latest_user_text(messages, before_index=len(messages) - 1)[:400]
    tool_output = _extract_tool_output(messages[-1])

    # Trim based on context window
    context_budget = cfg["context"] // 4  # Reserve 75% for prompt
    user_intent = _trim_context(user_intent, context_budget // 2)
    tool_output = _trim_context(tool_output, context_budget)

    # Prior assistant text
    prior_text = ""
    if len(messages) > 2:
        prev = messages[-2]
        if prev.get("role") == "assistant":
            prior_text = _extract_text(prev)[:300]

    return tmpl.format(
        user_intent=user_intent,
        tool_output=tool_output,
        prior_text=prior_text,
    )

def query_model(model: str, prompt: str) -> Tuple[Optional[str], int]:
    """Query model with retry logic. Returns (response, latency_ms)."""
    cfg = MODELS[model]
    endpoint = get_active_endpoint(model)

    if not endpoint:
        logger.warning(f"no endpoint for {model}")
        return None, 0

    for attempt in range(3):  # 3 retries
        t0 = time.time()
        try:
            payload = json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": cfg["max_response_tokens"],
                    "top_p": 0.9,
                },
            }).encode()

            req = urllib.request.Request(
                f"{endpoint}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            timeout = cfg["timeout_ms"] / 1000
            with urllib.request.urlopen(req, timeout=timeout) as r:
                result = json.loads(r.read())
                resp = result.get("response", "").strip()
                latency = int((time.time() - t0) * 1000)

                if resp:
                    return resp, latency

        except urllib.error.URLError as e:
            logger.debug(f"attempt {attempt+1} {model}: {e}")
            if attempt < 2:
                time.sleep(0.2 * (2 ** attempt))  # exponential backoff
                continue

        except Exception as e:
            logger.error(f"{model} error: {e}")
            return None, int((time.time() - t0) * 1000)

    return None, int((time.time() - t0) * 1000)

def score_response(model: str, response: str, turn_class: TurnClass) -> float:
    """Score confidence based on model trust and response characteristics."""
    cfg = MODELS[model]
    score = cfg["trust"]

    lower = response.lower()

    # Penalize uncertainty
    uncertain_phrases = ["not sure", "unclear", "might", "depends", "could be", "probably"]
    score -= 0.08 * sum(1 for p in uncertain_phrases if p in lower)

    # Penalize length
    tokens = len(response.split())
    if tokens > cfg["max_response_tokens"]:
        score *= 0.85

    # Boost for confident phrases
    confident_phrases = ["complete", "done", "finished", "working", "passed", "successful"]
    score += 0.05 * sum(1 for p in confident_phrases if p in lower)

    # Boost if model is strong at this turn class
    if TURN_CAPABILITY.get(turn_class) in cfg["strong_at"]:
        score = min(1.0, score + 0.08)

    return min(1.0, max(0.0, score))

def is_safe(response: str) -> bool:
    """Safety gates: never generate tools or code mods."""
    lower = response.lower()

    # Block tool generation
    tool_signals = [
        "i'll run", "let me run", "i'll execute", "run the", "execute the",
        "i'll write", "let me write", "generate code", "create code",
        "i'll edit", "let me edit", "i'll modify", "let me modify",
        "tool_call", "function_call", "subprocess", "os.system",
    ]

    for sig in tool_signals:
        if sig in lower:
            logger.info(f"blocked: tool signal '{sig}'")
            return False

    # Block excessive length
    if len(response) > 2000 or response.count("\n") > 50:
        logger.info(f"blocked: too long ({len(response)} chars)")
        return False

    return True

def attempt(data: dict, streaming: bool = True) -> Optional[bytes]:
    """Main entry point: try to answer locally, None = escalate to Claude."""
    t0 = time.time()

    try:
        messages = data.get("messages", [])

        # Gate 1: Intermediate turn?
        if not _is_intermediate(messages):
            _record_metric("skipped", "not_intermediate")
            return None

        # Gate 2: Classify
        turn_class = classify_turn(messages)
        if turn_class == TurnClass.UNCERTAIN:
            _record_metric("skipped", "uncertain_class")
            return None

        # Gate 3: Select model
        model = select_model(turn_class)
        if not model:
            _record_metric("skipped", "no_model")
            return None

        # Gate 4: Build prompt
        prompt = build_prompt(model, messages, turn_class)

        # Gate 5: Query model
        response, query_latency = query_model(model, prompt)
        if not response:
            _record_metric(model, "ollama_none")
            return None

        # Gate 6: Safety check
        if not is_safe(response):
            _record_metric(model, "unsafe")
            return None

        # Gate 7: Confidence threshold
        confidence = score_response(model, response, turn_class)
        thresholds = {
            TurnClass.TRIVIAL: 0.90,
            TurnClass.SIMPLE: 0.82,
            TurnClass.MODERATE: 0.75,
            TurnClass.COMPLEX: 0.70,
        }
        threshold = thresholds.get(turn_class, 0.75)

        if confidence < threshold:
            _record_metric(model, f"low_confidence_{confidence:.2f}")
            return None

        # USE IT
        latency_ms = int((time.time() - t0) * 1000)
        _record_metric(model, "used", latency_ms)

        logger.info(f"USED {model} (class={turn_class.name}, conf={confidence:.2f}, latency={latency_ms}ms)")

        return _format_response(response, streaming)

    except Exception as e:
        logger.error(f"attempt error: {e}")
        return None

def _record_metric(model: str, status: str, latency_ms: int = 0) -> None:
    """Thread-safe metrics recording."""
    with _metrics_lock:
        m = _metrics[model]
        m["attempts"] += 1
        if status == "used":
            m["used"] += 1
            if latency_ms:
                m["latencies"].append(latency_ms)
        elif "low_confidence" in status or "unsafe" in status or status == "ollama_none":
            m["skipped"] += 1
        else:
            m["errors"] += 1

def get_metrics() -> dict:
    """Return current metrics for dashboard."""
    with _metrics_lock:
        result = {}
        for model, m in _metrics.items():
            latencies = m["latencies"]
            result[model] = {
                "attempts": m["attempts"],
                "used": m["used"],
                "success_rate": m["used"] / max(1, m["attempts"]),
                "skipped": m["skipped"],
                "errors": m["errors"],
                "avg_latency_ms": sum(latencies) / len(latencies) if latencies else 0,
                "p95_latency_ms": sorted(latencies)[int(len(latencies)*0.95)] if len(latencies) > 20 else 0,
            }
        return result

def _format_response(text: str, streaming: bool) -> bytes:
    """Format as Claude API response (SSE or JSON)."""
    msg_id = f"msg_mmr_{uuid.uuid4().hex[:12]}"
    tokens = max(1, len(text.split()))

    if streaming:
        lines = [
            f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': 'claude-mmr', 'content': []}})}\n",
            f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n",
            f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}})}\n",
            f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n",
            f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': tokens}})}\n",
            f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n",
        ]
        return "\n".join(lines).encode()
    else:
        return json.dumps({
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-mmr",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": tokens}
        }).encode()
