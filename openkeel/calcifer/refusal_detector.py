#!/usr/bin/env python3
"""Detect when LLM responses are refusals and suggest escalation."""

import re
from dataclasses import dataclass


@dataclass
class RefusalDetection:
    """Result of refusal detection."""
    is_refusal: bool
    reason: str  # Why we think it's a refusal
    confidence: float  # 0.0-1.0
    suggested_escalation: bool  # Should we retry with Opus?


class RefusalDetector:
    """Detect refusal patterns in LLM responses."""

    # Patterns that indicate refusal
    REFUSAL_PATTERNS = [
        r"i\s+can't",
        r"i\s+cannot",
        r"unable to",
        r"not able to",
        r"don't have access",
        r"don't have permission",
        r"cannot access",
        r"no access to",
        r"i'm not able",
        r"not authorized",
        r"not permitted",
        r"cannot execute",
        r"unable to execute",
        r"permission denied",
        r"access denied",
        r"not allowed",
        r"forbidden",
        r"restricted",
        r"cannot read",
        r"cannot write",
        r"cannot modify",
        r"i don't have",
        r"i lack",
        r"outside my capabilities",
        r"beyond my scope",
        r"not in my power",
        r"not within my abilities",
    ]

    # Patterns that should NOT trigger escalation (legitimate refusals)
    LEGITIMATE_REFUSAL_PATTERNS = [
        r"security\s+(risk|concern|reason)",
        r"safety\s+concern",
        r"ethical\s+issue",
        r"against\s+policy",
        r"violates.*policy",
        r"due to security",
        r"for safety",
        r"security\s+reasons?",
        r"not.*for security",
    ]

    def __init__(self):
        """Compile regex patterns."""
        self.refusal_regexes = [re.compile(p, re.IGNORECASE) for p in self.REFUSAL_PATTERNS]
        self.legitimate_regexes = [re.compile(p, re.IGNORECASE) for p in self.LEGITIMATE_REFUSAL_PATTERNS]

    def detect(self, response: str, context: str = "") -> RefusalDetection:
        """Detect if response is a refusal.

        Args:
            response: The LLM's response text
            context: Optional context (original question, model name, etc)

        Returns:
            RefusalDetection with is_refusal and suggested_escalation flags
        """
        if not response or len(response) < 10:
            # Too short to be meaningful refusal
            return RefusalDetection(
                is_refusal=False,
                reason="response too short",
                confidence=0.0,
                suggested_escalation=False,
            )

        # Check if this is a legitimate refusal (security, ethics, etc)
        for regex in self.legitimate_regexes:
            if regex.search(response):
                return RefusalDetection(
                    is_refusal=True,
                    reason="legitimate refusal (security/ethics)",
                    confidence=0.9,
                    suggested_escalation=False,  # Don't escalate on legitimate refusals
                )

        # Check for refusal patterns
        matches = sum(1 for regex in self.refusal_regexes if regex.search(response))

        if matches >= 2:  # Multiple refusal patterns = likely refusal
            return RefusalDetection(
                is_refusal=True,
                reason=f"detected {matches} refusal patterns",
                confidence=0.95,
                suggested_escalation=True,
            )
        elif matches == 1:
            # Single match - check if response is short (likely pure refusal)
            if len(response) < 200:
                return RefusalDetection(
                    is_refusal=True,
                    reason="single refusal pattern, short response",
                    confidence=0.7,
                    suggested_escalation=True,
                )
            else:
                # Longer response with refusal - might be partial refusal
                return RefusalDetection(
                    is_refusal=True,
                    reason="single refusal pattern in longer response",
                    confidence=0.5,
                    suggested_escalation=False,  # Don't escalate on mixed responses
                )

        return RefusalDetection(
            is_refusal=False,
            reason="no refusal patterns detected",
            confidence=0.0,
            suggested_escalation=False,
        )

    @staticmethod
    def is_likely_refusal(response: str) -> bool:
        """Quick check: is this response definitely a refusal?"""
        detector = RefusalDetector()
        detection = detector.detect(response)
        return detection.is_refusal and detection.suggested_escalation
