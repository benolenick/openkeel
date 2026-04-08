#!/usr/bin/env python3
"""Calcifer Conductor: Opus meta-agent that watches the Ladder.

The Conductor reads intent once (first message), then supervises every
exchange. It biases routing and intervenes when the Ladder is stuck or
solving the wrong problem.

Cost invariant: ~200 tokens overhead per turn. Pays for itself by
catching failures before they burn 5-10K tokens on wrong approaches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from openkeel.calcifer.intention import IntentionModel, extract_intention


# Runner escalation order (cheapest → most expensive)
_LADDER = ["gemma4_small", "qwen3b", "gemma26b", "haiku", "sonnet", "opus"]


def _escalate(runner_id: str, steps: int = 1) -> str:
    """Move up the ladder by `steps`."""
    try:
        idx = _LADDER.index(runner_id)
    except ValueError:
        return runner_id
    return _LADDER[min(idx + steps, len(_LADDER) - 1)]


@dataclass
class ConductorState:
    """Conductor state across a conversation."""

    user_intent: IntentionModel
    conversation_id: str
    last_routing_decision: Optional[str] = None
    failed_attempts: list[str] = field(default_factory=list)
    interventions_made: list[str] = field(default_factory=list)
    turn_count: int = 0


class Conductor:
    """Supervises the Ladder. No LLM call per turn — pure heuristics."""

    def __init__(self, conversation_id: str = "default"):
        self.conversation_id = conversation_id
        self.state: Optional[ConductorState] = None

    # ── Init ──────────────────────────────────────────────────────────────────

    def initialize_from_message(self, user_message: str) -> ConductorState:
        """Extract intent from first message. Called once per conversation."""
        intent = extract_intention(user_message)
        self.state = ConductorState(
            user_intent=intent,
            conversation_id=self.conversation_id,
        )
        return self.state

    # ── Routing advice ────────────────────────────────────────────────────────

    def suggest_routing(self, message: str, default_runner: str) -> str:
        """Return a possibly-escalated runner based on intent + history."""
        if not self.state:
            return default_runner

        intent = self.state.user_intent
        runner = default_runner

        # Architecture/design tasks need stronger reasoning
        if intent.domain == "architecture" and runner in ("gemma4_small", "qwen3b"):
            runner = _escalate(runner, 2)

        # Frustrated user + prior failure → bump one rung
        if intent.frustration_level >= 5 and self.state.failed_attempts:
            runner = _escalate(runner, 1)

        # Stuck pattern → escalate hard
        if intent.is_stuck_pattern(self.state.failed_attempts):
            runner = _escalate(runner, 2)

        # Emergency → never stay local
        if intent.urgency_signal == "urgent" and runner in ("gemma4_small", "qwen3b"):
            runner = "haiku"

        if runner != default_runner:
            self.state.interventions_made.append(
                f"turn {self.state.turn_count}: routed {default_runner} → {runner}"
            )

        self.state.last_routing_decision = runner
        return runner

    # ── Intervention check ────────────────────────────────────────────────────

    def observe_response(self, agent_response: str, runner_id: str) -> tuple[bool, Optional[str]]:
        """Record response and decide if conductor should intervene.

        Returns (should_intervene, human_readable_reason).
        """
        if not self.state:
            return False, None

        self.state.turn_count += 1
        intent = self.state.user_intent

        # Track failed attempts
        if not intent.matches_solution(agent_response):
            self.state.failed_attempts.append(agent_response[:300])

        # Stuck loop?
        if intent.is_stuck_pattern(self.state.failed_attempts):
            reason = "Pattern detected: responses are looping. Try rephrasing or @opus to escalate."
            self.state.interventions_made.append(f"turn {self.state.turn_count}: stuck_pattern")
            return True, reason

        # Agent admitted confusion
        r = agent_response.lower()
        confusion = ["i don't understand", "could you clarify", "what do you mean", "please provide more"]
        if any(p in r for p in confusion):
            reason = "Agent is confused. Add context or force a stronger runner with @sonnet / @opus."
            return True, reason

        # Local model on an arch task
        if intent.domain == "architecture" and runner_id in ("gemma4_small", "qwen3b", "gemma26b"):
            reason = "Architecture task on a local model — quality may be low. Consider @sonnet."
            return True, reason

        return False, None

    # ── Summary ───────────────────────────────────────────────────────────────

    def status_line(self) -> str:
        """One-liner status for the toolbar."""
        if not self.state:
            return ""
        intent = self.state.user_intent
        parts = [f"goal: {intent.goal[:40]}"]
        if self.state.failed_attempts:
            parts.append(f"attempts: {len(self.state.failed_attempts)}")
        if self.state.interventions_made:
            parts.append(f"interventions: {len(self.state.interventions_made)}")
        return "  ·  ".join(parts)
