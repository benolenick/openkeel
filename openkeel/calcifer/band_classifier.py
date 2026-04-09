#!/usr/bin/env python3
"""Band Classifier: fast triage layer that routes messages by intent band.

Bands:
- A: Chat/trivial (no tools) → Haiku
- B: Simple reads (single file/grep/list) → Direct
- C: Standard task (read+analyze+edit) → Sonnet plans, executes
- D: Hard (design/complex reasoning) → Opus plans, executes
- E: Escalated (3+ failures, high-priority bug) → Opus full control
"""

import re
from enum import Enum
from dataclasses import dataclass


class Band(Enum):
    """Message intent bands."""
    A = "chat"       # Trivial, conversational
    B = "simple"     # Single deterministic read
    C = "standard"   # Multi-step task, moderate complexity
    D = "hard"       # Design/complex reasoning
    E = "escalated"  # Critical, multi-failure recovery


@dataclass
class ClassificationResult:
    """Result of band classification."""
    band: Band
    confidence: float  # 0.5-1.0
    reasoning: str     # Why this band
    skip_planner: bool  # If True, go straight to executor
    suggested_runner: str  # "haiku", "direct", "sonnet", "opus"


class BandClassifier:
    """Fast regex-based message triage."""

    # Band A patterns: pure chat, no intent to change code/files
    BAND_A_PATTERNS = [
        r"^(hi|hey|hello|thanks|thx|ok|okay|yep|nope|sure|yes|no)(\s|\.)?",  # Allow trailing space/period
        r"^what (time|date|day) is it",
        r"^(tell me a joke|weather|current time|how are you)",
        r"^(good morning|good afternoon|good evening)",
        r"^(goodbye|bye|see you|later|cya)",
        r"(^| )(love|hate|great|awesome|terrible|sucks?|amazing)($| )",  # Can be mid-sentence
        r"^(lol|haha|lmao|rofl|wtf)",
        r"^(hi|hey|hello|thanks) (there|!|$)",  # Casual greetings with extra words
    ]

    # Band B patterns: single deterministic read/list
    BAND_B_PATTERNS = [
        r"^(read|show|cat|display|print) (\S+)$",
        r"^(list|ls|dir) (\S+)?$",
        r"^(grep|search|find) (for |in )?(.+) in (.+)$",
        r"^(what'?s in|what'?s the content of) (\S+)",
        r"^file (.+)\?$",
        r"^(open|view) (.+)\.?$",
    ]

    # Band D patterns: design/complex reasoning/architecture
    BAND_D_PATTERNS = [
        r"(design|architect|plan|structure|refactor) (this|the|a) (.{20,})",
        r"^(how would you|how should|what'?s the best way to) (.{30,})",
        r"(explain|describe|write an essay about) (\w+)",
        r"^why (is|does|should)",
        r"(audit|review|security|performance|optimization)",
    ]

    # Band E patterns: explicit escalation signals
    BAND_E_PATTERNS = [
        r"(urgent|critical|emergency|asap)",
        r"(broken|crashing|failing|on fire)",
        r"^@escalate",
        r"(please use opus|call opus|use your best)",
    ]

    def __init__(self):
        """Compile regex patterns."""
        self.band_a = [re.compile(p, re.IGNORECASE) for p in self.BAND_A_PATTERNS]
        self.band_b = [re.compile(p, re.IGNORECASE) for p in self.BAND_B_PATTERNS]
        self.band_d = [re.compile(p, re.IGNORECASE) for p in self.BAND_D_PATTERNS]
        self.band_e = [re.compile(p, re.IGNORECASE) for p in self.BAND_E_PATTERNS]

    def classify(self, message: str) -> ClassificationResult:
        """Classify a user message into a band.

        Returns ClassificationResult with band, confidence, reasoning.
        """
        msg = message.strip()

        # Check Band E first (explicit escalation)
        for pattern in self.band_e:
            if pattern.search(msg):
                return ClassificationResult(
                    band=Band.E,
                    confidence=0.95,
                    reasoning="Explicit escalation signal",
                    skip_planner=False,
                    suggested_runner="opus",
                )

        # Check Band A: trivial chat
        if len(msg) < 50:  # Short messages likely chat
            for pattern in self.band_a:
                if pattern.match(msg):
                    return ClassificationResult(
                        band=Band.A,
                        confidence=0.9,
                        reasoning=f"Trivial chat pattern",
                        skip_planner=True,
                        suggested_runner="haiku",
                    )

        # Check Band B: single read
        for pattern in self.band_b:
            if pattern.match(msg):
                return ClassificationResult(
                    band=Band.B,
                    confidence=0.85,
                    reasoning="Single deterministic read",
                    skip_planner=True,
                    suggested_runner="direct",
                )

        # Check Band D: hard reasoning
        for pattern in self.band_d:
            if pattern.search(msg):
                return ClassificationResult(
                    band=Band.D,
                    confidence=0.8,
                    reasoning="Complex design/reasoning task",
                    skip_planner=False,
                    suggested_runner="opus",
                )

        # Heuristic: very long messages with code blocks → Band D
        if "```" in msg and len(msg) > 300:
            return ClassificationResult(
                band=Band.D,
                confidence=0.7,
                reasoning="Long message with code blocks",
                skip_planner=False,
                suggested_runner="opus",
            )

        # Heuristic: multiple questions → Band D
        if msg.count("?") >= 3:
            return ClassificationResult(
                band=Band.D,
                confidence=0.65,
                reasoning="Complex multi-part question",
                skip_planner=False,
                suggested_runner="opus",
            )

        # Default: Band C (standard task, moderate complexity)
        # Use Sonnet for planning (cheaper than Opus), still get good plans
        return ClassificationResult(
            band=Band.C,
            confidence=0.6,
            reasoning="Multi-step task (default)",
            skip_planner=False,
            suggested_runner="sonnet",  # Sonnet plans C, not Opus
        )


class BandRouter:
    """Routes classified messages to appropriate handler."""

    ROUTING_MATRIX = {
        Band.A: {
            "skip_planner": True,
            "target_step_kind": "reason",
            "replacement_mode": "HAIKU",  # Local or cloud Haiku
            "budget_units": 50.0,
        },
        Band.B: {
            "skip_planner": True,
            "target_step_kind": "read",
            "replacement_mode": "DIRECT",  # No model needed
            "budget_units": 10.0,
        },
        Band.C: {
            "skip_planner": False,
            "planner_model": "sonnet",  # Cheaper planning
            "replacement_mode": "SONNET",
            "budget_units": 500.0,
        },
        Band.D: {
            "skip_planner": False,
            "planner_model": "opus",  # Full Opus planning
            "replacement_mode": "OPUS",
            "budget_units": 2000.0,
        },
        Band.E: {
            "skip_planner": False,
            "planner_model": "opus",  # Opus takes full control
            "replacement_mode": "OPUS",
            "budget_units": 5000.0,
            "escalation_mode": "full_control",
        },
    }

    @staticmethod
    def get_routing(band: Band) -> dict:
        """Get routing config for a band."""
        return BandRouter.ROUTING_MATRIX.get(band, BandRouter.ROUTING_MATRIX[Band.C])
