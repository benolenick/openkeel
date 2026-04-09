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
from openkeel.calcifer.intention_broker import IntentionBroker, get_broker, IntentionPacket, SessionShard


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
        self._broker: IntentionBroker = get_broker()
        self._packet: Optional[IntentionPacket] = None
        self._shard: Optional[SessionShard] = None

    # ── Init ──────────────────────────────────────────────────────────────────

    def initialize_from_message(self, user_message: str) -> ConductorState:
        """Extract intent from first message. Called once per conversation."""
        intent = extract_intention(user_message)
        self.state = ConductorState(
            user_intent=intent,
            conversation_id=self.conversation_id,
        )
        self._packet = self._broker.get_or_create(intent.goal)
        self._shard = self._broker.start_session(self.conversation_id, self._packet.id)
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
            if self._shard:
                self._broker.record_obstacle(self.conversation_id, agent_response[:200])
        else:
            if self._shard:
                self._broker.record_action(self.conversation_id, agent_response[:200])

        # Stuck loop?
        if intent.is_stuck_pattern(self.state.failed_attempts):
            reason = "Pattern detected: responses are looping. Try rephrasing or @opus to escalate."
            self.state.interventions_made.append(f"turn {self.state.turn_count}: stuck_pattern")
            if self._packet:
                self._packet.record_attempt(self.conversation_id, "multiple attempts", "regression/loop")
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

    def close_conversation(self, summary: str = "", decision: str = "CONTINUE", reason: str = "") -> None:
        """Flush session shard and persist IntentionPacket to Hyphae."""
        if not summary and self.state:
            intent = self.state.user_intent
            summary = (
                f"Session ended. Goal: {intent.goal}. "
                f"Turns: {self.state.turn_count}. "
                f"Failures: {len(self.state.failed_attempts)}."
            )
        self._broker.close_session(self.conversation_id, summary, decision, reason)

    def record_discovery(self, text: str) -> None:
        """Record a discovery mid-session (wired to Hyphae immediately)."""
        self._broker.record_discovery(self.conversation_id, text)

    # ── Opus briefing ─────────────────────────────────────────────────────────

    def build_intention_briefing(self) -> str:
        """Return a compact markdown block to inject into Opus prompts.

        This is how Opus actually *sees* the landscape: the goal, current
        hypothesis chain, stuck pattern, and recent attempts. Without this,
        the landscape carves nothing — the water flows blind.
        """
        if not self._packet:
            return ""
        p = self._packet
        lines = [
            "── INTENTION LANDSCAPE ──",
            f"goal: {p.intended_outcome}",
        ]
        if p.hypothesis_chain:
            lines.append("hypothesis chain:")
            for h in p.hypothesis_chain[-3:]:
                flag = " [FAILED]" if h.failed else ""
                lines.append(f"  v{h.version} ({h.confidence:.0%}){flag}: {h.text}")
        if p.attempts:
            lines.append(f"recent attempts ({len(p.attempts)} total):")
            for a in p.attempts[-3:]:
                lines.append(f"  • {a['tried']} → {a['result']}")
        if p.stuck_pattern:
            lines.append(f"⚠ STUCK PATTERN: {p.stuck_pattern}")
            lines.append("  → Don't repeat previous attempts. Step back. Consider root cause.")
        if self._shard and self._shard.discoveries:
            lines.append("discoveries this session:")
            for d in self._shard.discoveries[-3:]:
                lines.append(f"  • {d}")
        if p.blocker:
            lines.append(f"known blocker: {p.blocker}")
        lines.append("── END LANDSCAPE ──")
        return "\n".join(lines)

    def should_inject_briefing(self, runner_id: str) -> bool:
        """Only inject for capable models where the briefing pays for itself."""
        if runner_id not in ("opus", "sonnet"):
            return False
        if not self._packet:
            return False
        return bool(
            self._packet.hypothesis_chain
            or self._packet.attempts
            or self._packet.stuck_pattern
            or (self._shard and self._shard.discoveries)
        )

    def extract_from_response(self, response: str) -> None:
        """Parse an agent response for hypotheses, discoveries, and root causes.

        Looks for explicit markers and natural language cues. Opus doesn't
        need to learn a new syntax — if it says 'I think the real issue is X',
        that becomes a new hypothesis.
        """
        if not self._packet or not response:
            return

        import re

        text = response.strip()
        low = text.lower()

        # Discovery markers
        discovery_patterns = [
            r"(?:i found|found that|discovered that|turns out|it turns out)[:\s]+(.{20,200}?)(?:\.|$)",
            r"(?:^|\n)[-*]\s*discovery[:\s]+(.{10,200}?)(?:\n|$)",
        ]
        for pat in discovery_patterns:
            for match in re.finditer(pat, low, re.IGNORECASE | re.MULTILINE):
                snippet = match.group(1).strip()
                if snippet:
                    self._broker.record_discovery(self.conversation_id, snippet[:200])

        # Hypothesis markers
        hypothesis_patterns = [
            r"(?:the real issue is|root cause is|i think (?:the problem|it) is|my hypothesis is|hypothesis[:\s]+)(.{20,200}?)(?:\.|$)",
            r"(?:^|\n)[-*]\s*hypothesis[:\s]+(.{10,200}?)(?:\n|$)",
        ]
        for pat in hypothesis_patterns:
            m = re.search(pat, low, re.IGNORECASE | re.MULTILINE)
            if m:
                snippet = m.group(1).strip()
                if snippet and len(snippet) > 10:
                    # Avoid duplicate hypothesis on same exact text
                    existing_texts = [h.text.lower() for h in self._packet.hypothesis_chain]
                    if snippet not in existing_texts:
                        self._packet.add_hypothesis(snippet[:200], confidence=0.5)
                        break

    def force_opus_on_stuck(self, runner_id: str) -> str:
        """If stuck pattern is active, force escalation to Opus regardless of default."""
        if self._packet and self._packet.stuck_pattern and runner_id != "opus":
            if self.state:
                self.state.interventions_made.append(
                    f"turn {self.state.turn_count}: forced opus (stuck pattern)"
                )
            return "opus"
        return runner_id

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
        if self._packet and self._packet.stuck_pattern:
            parts.append(f"STUCK: {self._packet.stuck_pattern}")
        if self._packet and self._packet.current_hypothesis:
            h = self._packet.current_hypothesis
            parts.append(f"hypothesis v{h.version} ({h.confidence:.0%})")
        return "  ·  ".join(parts)
