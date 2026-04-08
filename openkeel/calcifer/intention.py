#!/usr/bin/env python3
"""Contextual intention extraction for Calcifer Conductor.

Extracts the user's *actual need* from their first message using
fast rule-based parsing — no LLM call, no latency cost.

The IntentionModel is then used by the Conductor to:
  - bias routing decisions
  - detect stuck/wrong-problem patterns
  - trigger interventions
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IntentionModel:
    """User's actual contextual need, extracted from first message."""

    goal: str          # short description of what they want
    domain: str        # "coding" | "debugging" | "architecture" | "explanation" | "general"

    # Constraint scales (0–10)
    time_critical: int = 0
    quality_needed: int = 5
    cost_sensitive: int = 5
    risk_tolerance: int = 5

    # Meta
    frustration_level: int = 0         # 0=calm, 10=furious
    urgency_signal: str = "normal"     # "routine" | "urgent" | "emergency"

    # Accumulated across conversation
    prior_attempts: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)

    def matches_solution(self, response: str) -> bool:
        """Does this response seem to address the intent?"""
        r = response.lower()
        # If goal keywords appear in response it's probably on track
        goal_words = [w for w in self.goal.lower().split() if len(w) > 4]
        hits = sum(1 for w in goal_words if w in r)
        return hits >= max(1, len(goal_words) // 3)

    def is_stuck_pattern(self, attempts: list[str]) -> bool:
        """Are we looping on the same failed approach?"""
        if len(attempts) < 2:
            return False
        last = attempts[-1].lower().split()
        prev = attempts[-2].lower().split()
        if not last or not prev:
            return False
        overlap = sum(1 for w in last if w in prev) / max(len(last), 1)
        return overlap > 0.55


# ── Keyword banks ──────────────────────────────────────────────────────────────

_CODING_WORDS = {
    "code", "function", "bug", "error", "fix", "debug", "test", "import",
    "class", "method", "script", "module", "variable", "loop", "syntax",
    "exception", "traceback", "compile", "lint", "type", "annotation",
}
_ARCH_WORDS = {
    "design", "architect", "plan", "system", "refactor", "structure",
    "blueprint", "pattern", "interface", "abstraction", "pipeline", "schema",
}
_DEBUG_WORDS = {
    "crash", "broken", "not working", "fails", "slow", "hang", "freeze",
    "timeout", "memory", "leak", "error", "exception", "unexpected",
}
_EXPLAIN_WORDS = {
    "explain", "what is", "how does", "understand", "tell me", "describe",
    "clarify", "what does", "meaning", "why does",
}
_URGENT_WORDS = {
    "asap", "urgent", "emergency", "critical", "broken prod", "down", "outage",
}
_FRUSTRATION_WORDS = {
    "frick", "fuck", "shit", "ugh", "wtf", "why", "still", "again", "lmao",
    "goddamn", "dammit", "seriously", "ugh", "argh",
}


def extract_intention(message: str) -> IntentionModel:
    """Fast rule-based intent extraction. No LLM, no latency."""
    msg = message.lower()
    words = set(msg.split())

    # Domain
    domain = "general"
    if _ARCH_WORDS & words or any(p in msg for p in _ARCH_WORDS if " " in p):
        domain = "architecture"
    elif _DEBUG_WORDS & words or any(p in msg for p in _DEBUG_WORDS if " " in p):
        domain = "debugging"
    elif _CODING_WORDS & words:
        domain = "coding"
    elif any(p in msg for p in _EXPLAIN_WORDS if " " in p) or _EXPLAIN_WORDS & words:
        domain = "explanation"

    # Urgency
    urgency = "normal"
    if _URGENT_WORDS & words or any(p in msg for p in _URGENT_WORDS if " " in p):
        urgency = "urgent"

    # Time criticality
    time_critical = 0
    if urgency == "urgent":
        time_critical = 8
    elif any(w in msg for w in ("today", "eod", "tonight", "by morning")):
        time_critical = 5

    # Frustration (context-aware — this convo started with "frick, agents died")
    frustration = 0
    frust_hits = sum(1 for w in _FRUSTRATION_WORDS if w in msg)
    frustration = min(10, frust_hits * 3)

    # Goal: first meaningful sentence, capped at 150 chars
    goal = message.strip().splitlines()[0][:150]

    return IntentionModel(
        goal=goal,
        domain=domain,
        time_critical=time_critical,
        urgency_signal=urgency,
        frustration_level=frustration,
        quality_needed=7 if domain == "architecture" else 5,
    )
