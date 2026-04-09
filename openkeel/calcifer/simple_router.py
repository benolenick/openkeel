#!/usr/bin/env python3
"""Simple router: one-shot routing for ordinary turns.

This is the cheap path for Calcifer:
- explicit runner tags always win
- ordinary turns route to one runner
- local turns use Ollama directly
- cloud turns use one Claude CLI call

Strategic / conductor-worthy turns are handled outside this module by the
Governor loop.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from dataclasses import dataclass

from openkeel.calcifer.brain import (
    CALCIFER_SYSTEM_PROMPT,
    OLLAMA_URL,
    build_context,
    get_recent_history,
)
from openkeel.calcifer.classify import classify, Band
from openkeel.calcifer.intention_broker import get_broker


@dataclass
class RouteDecision:
    runner: str
    band: str
    reason: str


_TAG_RUNNERS = [
    ("@local", "gemma4_small", "explicit @local"),
    ("@qwen", "qwen25", "explicit @qwen"),
    ("@big", "gemma4_large", "explicit @big"),
    ("@haiku", "haiku", "explicit @haiku"),
    ("@sonnet", "sonnet", "explicit @sonnet"),
]


def choose_runner(user_message: str) -> RouteDecision:
    """Choose a single ordinary-turn runner.

    Opus is intentionally excluded here; the caller decides when a turn should
    leave the simple path and enter the governor path.
    """
    low = user_message.lower()
    for tag, runner, reason in _TAG_RUNNERS:
        if tag in low:
            return RouteDecision(runner=runner, band="override", reason=reason)

    profile = classify(user_message)
    band = profile.band()

    if band == Band.A:
        return RouteDecision("gemma4_small", "A", "minimal turn")
    if band == Band.B:
        if profile.conversation_shape.value == "instant_answer":
            return RouteDecision("gemma4_small", "B", "lightweight instant answer")
        return RouteDecision("haiku", "B", "lightweight cloud turn")
    if band == Band.C:
        if profile.loop_difficulty >= 0.55 or profile.evidence_need >= 0.45:
            return RouteDecision("sonnet", "C", "bounded operational work")
        return RouteDecision("gemma4_large", "C", "bounded local reasoning")
    if band == Band.D:
        return RouteDecision("sonnet", "D", "high-judgment turn")

    # Band E should have been intercepted by the governor path.
    return RouteDecision("sonnet", "E", "fallback while governor unavailable")


def _build_messages(session_id: str, user_message: str) -> list[dict]:
    """Build compact message history shared by local/cloud one-shot paths."""
    context_block = build_context(user_message)
    history = get_recent_history(session_id, limit=8)

    messages = [{"role": "system", "content": CALCIFER_SYSTEM_PROMPT}]
    if context_block:
        messages.append({
            "role": "system",
            "content": f"Context for this conversation:\n{context_block}",
        })
    messages.extend(history)
    # The caller may already have persisted the current user turn.
    if not history or history[-1].get("role") != "user" or history[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})
    return messages


def _run_ollama(model: str, messages: list[dict], host: str = OLLAMA_URL) -> str:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 1024,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message") or {}).get("content", "").strip()


def _run_claude(model: str, messages: list[dict]) -> str:
    prompt_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            prompt_parts.append(content)
        elif role == "assistant":
            prompt_parts.append(f"Assistant: {content}")
        else:
            prompt_parts.append(f"User: {content}")
    prompt = "\n\n".join(prompt_parts)
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "text"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:300] or "claude CLI failed")
    return result.stdout.strip()


def route_and_execute(user_message: str, conversation_id: str = "default") -> tuple[RouteDecision, str]:
    """Classify, route, run once, and return `(decision, response)`."""
    decision = choose_runner(user_message)
    messages = _build_messages(conversation_id, user_message)

    broker = get_broker()
    packet = broker.get_or_create(user_message[:80])
    if packet.hypothesis_chain or packet.attempts or packet.stuck_pattern:
        brief = [
            "=== INTENTION LANDSCAPE ===",
            f"Goal: {packet.intended_outcome}",
            f"Attempts: {len(packet.attempts)}",
            f"Stuck: {packet.stuck_pattern or 'no'}",
            "=== END LANDSCAPE ===",
        ]
        messages.insert(1, {"role": "system", "content": "\n".join(brief)})

    if decision.runner == "gemma4_small":
        response = _run_ollama("gemma4:e2b", messages, host="http://127.0.0.1:11434")
    elif decision.runner == "qwen25":
        response = _run_ollama("qwen2.5:3b", messages, host="http://192.168.0.224:11434")
    elif decision.runner == "gemma4_large":
        response = _run_ollama("gemma4:26b", messages, host="http://192.168.0.224:11434")
    elif decision.runner == "haiku":
        response = _run_claude("haiku", messages)
    elif decision.runner == "sonnet":
        response = _run_claude("sonnet", messages)
    else:
        raise RuntimeError(f"unsupported runner: {decision.runner}")

    return decision, response
