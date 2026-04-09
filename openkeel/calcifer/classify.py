#!/usr/bin/env python3
"""Conversation shape classification and escalation profile scoring.

Transforms a turn into a scored band (A-E) based on 6 dimensions:
  1. structural_complexity — how much hidden structure?
  2. operational_depth — how many dependent steps?
  3. evidence_need — how much proof required?
  4. verifiability_risk — can it be verified deterministically?
  5. consequence_of_error — how bad is wrong?
  6. loop_difficulty — does it need iterative tool use?

Band mapping:
  A: Minimal — local or Haiku
  B: Lightweight — local or Haiku, maybe Sonnet
  C: Operational — Sonnet or local bounded loop
  D: High-judgment — Sonnet + Opus planning/arbitration
  E: Strategic/Long-horizon — Opus governor + execution
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ConversationShape(Enum):
    """The four conversation shapes from the design doc."""
    INSTANT_ANSWER = "instant_answer"
    GUIDED_TROUBLESHOOTING = "guided_troubleshooting"
    BOUNDED_WORK = "bounded_work"
    STRATEGIC_ENGINEERING = "strategic_engineering"


class Band(Enum):
    """Escalation bands A-E."""
    A = "A"  # Minimal
    B = "B"  # Lightweight
    C = "C"  # Operational
    D = "D"  # High-judgment
    E = "E"  # Strategic


@dataclass
class EscalationProfile:
    """Scored escalation profile (0.0-1.0 on each dimension)."""

    structural_complexity: float       # low: 0.0 (one simple thing), high: 1.0 (hidden structure)
    operational_depth: float           # low: 0.0 (one step), high: 1.0 (many dependent steps)
    evidence_need: float               # low: 0.0 (casual), high: 1.0 (heavy proof needed)
    verifiability_risk: float          # low: 0.0 (easy deterministic check), high: 1.0 (ambiguous)
    consequence_of_error: float        # low: 0.0 (convenience), high: 1.0 (major impact)
    loop_difficulty: float             # low: 0.0 (direct answer), high: 1.0 (iterative tool use)

    conversation_shape: ConversationShape
    keywords_detected: list[str]

    def overall_score(self) -> float:
        """Weighted average of all 6 dimensions."""
        dims = [
            self.structural_complexity,
            self.operational_depth,
            self.evidence_need,
            self.verifiability_risk,
            self.consequence_of_error,
            self.loop_difficulty,
        ]
        return sum(dims) / len(dims)

    def band(self) -> Band:
        """Map overall score to band A-E."""
        score = self.overall_score()
        if score < 0.2:
            return Band.A
        elif score < 0.4:
            return Band.B
        elif score < 0.6:
            return Band.C
        elif score < 0.8:
            return Band.D
        else:
            return Band.E

    def summary(self) -> str:
        """One-liner summary for logging."""
        parts = [
            f"band {self.band().value}",
            f"shape {self.conversation_shape.value.split('_')[0]}",
            f"score {self.overall_score():.2f}",
        ]
        if self.keywords_detected:
            parts.append(f"keywords {','.join(self.keywords_detected)}")
        return " | ".join(parts)


# ── Conversation shape detection ──────────────────────────────────────────────

def _detect_conversation_shape(text: str) -> ConversationShape:
    """Classify the user's turn into one of four shapes.

    Based on content patterns, not just keywords.
    """
    low = text.lower()

    # Instant answer patterns
    instant_patterns = [
        r"what does",
        r"how do i",
        r"explain",
        r"show me",
        r"^[^?]*\?$",  # just a question
        r"summarize",
        r"list the",
    ]
    if any(p in low for p in instant_patterns if p[0] != "^"):
        if low.count("?") == 1 and len(text.split()) < 20:
            return ConversationShape.INSTANT_ANSWER

    # Guided troubleshooting patterns
    troubleshooting_patterns = [
        r"why (isn't|isn't|doesn't|won't|do)",
        r"help me debug",
        r"something is (broken|failing|wrong)",
        r"how do i fix",
    ]
    if any(p in low for p in troubleshooting_patterns):
        return ConversationShape.GUIDED_TROUBLESHOOTING

    # Strategic/engineering patterns
    strategic_patterns = [
        r"design",
        r"architect",
        r"refactor",
        r"plan",
        r"proposal",
        r"should we",
        r"which approach",
        r"how should we",
    ]
    if any(p in low for p in strategic_patterns):
        return ConversationShape.STRATEGIC_ENGINEERING

    # Default: bounded work
    return ConversationShape.BOUNDED_WORK


# ── Dimension scorers ──────────────────────────────────────────────────────────

def _score_structural_complexity(text: str, shape: ConversationShape) -> float:
    """How much hidden structure? (0.0 = obvious, 1.0 = intricate)."""
    low = text.lower()

    if shape == ConversationShape.INSTANT_ANSWER:
        return 0.1

    complexity_signals = [
        ("subsystem", 0.3),
        ("architecture", 0.4),
        ("design", 0.3),
        ("interaction", 0.25),
        ("multiple", 0.2),
        ("dependency", 0.3),
    ]

    score = 0.15
    for signal, boost in complexity_signals:
        if signal in low:
            score = max(score, boost)

    if shape == ConversationShape.STRATEGIC_ENGINEERING:
        score = max(score, 0.7)
    elif shape == ConversationShape.GUIDED_TROUBLESHOOTING:
        score = min(score, 0.5)

    return min(score, 0.95)


def _score_operational_depth(text: str, shape: ConversationShape) -> float:
    """How many dependent steps? (0.0 = one, 1.0 = many)."""
    low = text.lower()

    if shape == ConversationShape.INSTANT_ANSWER:
        return 0.05

    depth_signals = [
        ("then", 0.3),
        ("next", 0.3),
        ("after", 0.25),
        ("also", 0.2),
        ("and fix", 0.4),
        ("step by step", 0.5),
    ]

    score = 0.2 if shape == ConversationShape.BOUNDED_WORK else 0.15
    for signal, boost in depth_signals:
        if signal in low:
            score = max(score, boost)

    if shape == ConversationShape.STRATEGIC_ENGINEERING:
        score = max(score, 0.6)

    return min(score, 0.9)


def _score_evidence_need(text: str, shape: ConversationShape) -> float:
    """How much proof is required? (0.0 = casual, 1.0 = rigorous)."""
    low = text.lower()

    if shape == ConversationShape.INSTANT_ANSWER:
        return 0.1

    evidence_signals = [
        ("why", 0.3),
        ("prove", 0.6),
        ("verify", 0.5),
        ("check", 0.3),
        ("test", 0.4),
        ("audit", 0.5),
    ]

    score = 0.15
    for signal, boost in evidence_signals:
        if signal in low:
            score = max(score, boost)

    if shape == ConversationShape.STRATEGIC_ENGINEERING:
        score = max(score, 0.6)

    return min(score, 0.95)


def _score_verifiability_risk(text: str, shape: ConversationShape) -> float:
    """Can it be verified deterministically? (0.0 = yes, 1.0 = ambiguous)."""
    low = text.lower()

    testable_signals = [
        ("test", -0.2),
        ("assert", -0.2),
        ("check", -0.15),
        ("run ", -0.15),
    ]

    ambiguous_signals = [
        ("should", 0.4),
        ("design", 0.5),
        ("architecture", 0.6),
        ("opinion", 0.7),
        ("think", 0.2),
        ("better", 0.3),
    ]

    score = 0.3 if shape == ConversationShape.INSTANT_ANSWER else 0.5

    for signal, delta in testable_signals:
        if signal in low:
            score = max(score + delta, 0.1)

    for signal, boost in ambiguous_signals:
        if signal in low:
            score = max(score, boost)

    if shape == ConversationShape.STRATEGIC_ENGINEERING:
        score = max(score, 0.75)

    return min(score, 0.95)


def _score_consequence_of_error(text: str, shape: ConversationShape) -> float:
    """How bad is a wrong answer? (0.0 = low, 1.0 = high)."""
    low = text.lower()

    high_consequence_signals = [
        ("production", 0.8),
        ("critical", 0.7),
        ("system", 0.6),
        ("database", 0.7),
        ("delete", 0.8),
        ("security", 0.8),
        ("data loss", 0.9),
    ]

    score = {
        ConversationShape.INSTANT_ANSWER: 0.2,
        ConversationShape.GUIDED_TROUBLESHOOTING: 0.4,
        ConversationShape.BOUNDED_WORK: 0.5,
        ConversationShape.STRATEGIC_ENGINEERING: 0.7,
    }[shape]

    for signal, boost in high_consequence_signals:
        if signal in low:
            score = max(score, boost)

    return min(score, 0.95)


def _score_loop_difficulty(text: str, shape: ConversationShape) -> float:
    """Does it need iterative tool use? (0.0 = no, 1.0 = yes)."""
    low = text.lower()

    loop_signals = [
        ("debug", 0.6),
        ("investigate", 0.5),
        ("inspect", 0.4),
        ("fix", 0.4),
        ("patch", 0.5),
        ("trace", 0.5),
        ("step through", 0.6),
    ]

    score = {
        ConversationShape.INSTANT_ANSWER: 0.05,
        ConversationShape.GUIDED_TROUBLESHOOTING: 0.4,
        ConversationShape.BOUNDED_WORK: 0.5,
        ConversationShape.STRATEGIC_ENGINEERING: 0.3,
    }[shape]

    for signal, boost in loop_signals:
        if signal in low:
            score = max(score, boost)

    return min(score, 0.95)


# ── Main classifier ───────────────────────────────────────────────────────────

def classify(text: str, context: Optional[dict] = None) -> EscalationProfile:
    """Classify a user message into an EscalationProfile.

    Args:
        text: The user's message
        context: Optional context dict with prior task state, failures, etc.

    Returns:
        EscalationProfile with all 6 dimensions scored and band assigned
    """
    context = context or {}

    shape = _detect_conversation_shape(text)

    # Detect keywords that override shape classification
    keywords = []
    keyword_overrides = {
        "think hard": ConversationShape.STRATEGIC_ENGINEERING,
        "architect": ConversationShape.STRATEGIC_ENGINEERING,
        "design": ConversationShape.STRATEGIC_ENGINEERING,
        "audit": ConversationShape.BOUNDED_WORK,
        "ultrathink": ConversationShape.STRATEGIC_ENGINEERING,
    }

    low = text.lower()
    for keyword, override_shape in keyword_overrides.items():
        if keyword in low:
            keywords.append(keyword)
            shape = override_shape

    profile = EscalationProfile(
        structural_complexity=_score_structural_complexity(text, shape),
        operational_depth=_score_operational_depth(text, shape),
        evidence_need=_score_evidence_need(text, shape),
        verifiability_risk=_score_verifiability_risk(text, shape),
        consequence_of_error=_score_consequence_of_error(text, shape),
        loop_difficulty=_score_loop_difficulty(text, shape),
        conversation_shape=shape,
        keywords_detected=keywords,
    )

    return profile
