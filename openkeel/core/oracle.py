"""Overwatch Oracle — 122B model on CPU, sees everything, speaks rarely.

The Oracle runs on a dedicated CPU-only ollama instance. Every cycle it receives
the full distilled mission state and answers one strategic question in one sentence.

Architecture:
  - Input: ~2-5K tokens (distilled from all sources)
  - Output: ~20-50 tokens (one sentence)
  - Cycle: every 5 minutes (configurable)
  - Model: qwen3.5:122b-a10b (122B params, 10B active per forward pass)
  - Hardware: CPU/RAM only (no GPU contention)

The Oracle doesn't explain, doesn't show its work, doesn't hedge. It sees
what the fast small models can't and says one devastating thing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from openkeel.integrations.local_llm import (
    LLMEndpoint,
    complete,
    check_health,
    OVERWATCH_ENDPOINT,
)


# ---------------------------------------------------------------------------
# Strategic questions — rotated each cycle
# ---------------------------------------------------------------------------

STRATEGIC_QUESTIONS = [
    "What assumption has everyone accepted without evidence?",
    "What information do we have that we haven't used?",
    "What would a defender see right now, and what would they do about it?",
    "Are we in a loop? What pattern keeps repeating?",
    "What is the single most likely reason we are stuck?",
]

# The Oracle's system prompt — terse, authoritative, no hedging
ORACLE_SYSTEM_PROMPT = """\
You are the Overwatch Oracle. You observe an AI agent performing a security \
engagement. You see the full mission state: objectives, attack tree, problem \
map, recent actions, blind spots found by two smaller observer models, and \
all credentials discovered.

Your job: answer the strategic question in ONE SENTENCE. No preamble, no \
hedging, no "I think" or "it seems". Just the answer. If you don't see \
anything wrong, say "Nothing to report." and stop.

Be specific. Name the exact credential, service, technique, or assumption. \
Vague advice is worthless."""


# ---------------------------------------------------------------------------
# Context builder — distills everything into ~2-5K tokens
# ---------------------------------------------------------------------------

def build_oracle_context(
    mission_objective: str = "",
    mission_plan: str = "",
    credentials: list[dict] | None = None,
    tree_summary: str = "",
    map_summary: str = "",
    log_window: str = "",
    pilgrim_findings: str = "",
    cartographer_alerts: str = "",
    executor_action: str = "",
) -> str:
    """Pack the full mission state into a compact context for the Oracle."""
    sections = []

    # Mission (required)
    if mission_objective:
        sections.append(f"OBJECTIVE: {mission_objective}")
    if mission_plan:
        sections.append(f"PLAN:\n{mission_plan}")

    # Credentials
    if credentials:
        cred_lines = []
        for c in credentials:
            parts = []
            if c.get("username"):
                parts.append(c["username"])
            if c.get("password"):
                parts.append(c["password"])
            if c.get("hash"):
                parts.append(f"hash:{c['hash']}")
            if c.get("note"):
                parts.append(f"({c['note']})")
            cred_lines.append(" / ".join(parts))
        sections.append("CREDENTIALS:\n" + "\n".join(f"  - {l}" for l in cred_lines))

    # Attack tree state
    if tree_summary:
        sections.append(f"ATTACK TREE:\n{tree_summary}")

    # Problem map summary (from Cartographer)
    if map_summary:
        sections.append(f"PROBLEM MAP:\n{map_summary}")

    # Recent distilled log entries
    if log_window:
        sections.append(f"RECENT ACTIONS:\n{log_window}")

    # Pilgrim's findings
    if pilgrim_findings:
        sections.append(f"OBSERVER FINDINGS:\n{pilgrim_findings}")

    # Cartographer alerts
    if cartographer_alerts:
        sections.append(f"CARTOGRAPHER ALERTS:\n{cartographer_alerts}")

    # What the executor is doing right now
    if executor_action:
        sections.append(f"EXECUTOR IS CURRENTLY: {executor_action}")

    return "\n\n".join(sections)


def build_oracle_prompt(context: str, question: str) -> str:
    """Build the full prompt: context + strategic question."""
    return f"""{context}

---
STRATEGIC QUESTION: {question}
Answer in one sentence."""


# ---------------------------------------------------------------------------
# Oracle verdict
# ---------------------------------------------------------------------------

@dataclass
class OracleVerdict:
    """One pronouncement from the Oracle."""
    answer: str
    question: str
    cycle: int
    timestamp: str = ""
    inference_seconds: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    @property
    def is_actionable(self) -> bool:
        """True if the Oracle has something to say (not 'nothing to report')."""
        lower = self.answer.lower().strip()
        return bool(self.answer) and "nothing to report" not in lower

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "question": self.question,
            "cycle": self.cycle,
            "timestamp": self.timestamp,
            "inference_seconds": self.inference_seconds,
            "actionable": self.is_actionable,
        }


# ---------------------------------------------------------------------------
# Oracle configuration
# ---------------------------------------------------------------------------

@dataclass
class OracleConfig:
    """Configuration for the Overwatch Oracle."""
    endpoint: LLMEndpoint = field(default_factory=lambda: OVERWATCH_ENDPOINT)
    cycle_seconds: float = 300.0      # 5 minutes between queries
    questions: list[str] = field(default_factory=lambda: list(STRATEGIC_QUESTIONS))
    inject_threshold: int = 3          # consecutive actionable verdicts → inject
    enabled: bool = True


# ---------------------------------------------------------------------------
# Oracle engine
# ---------------------------------------------------------------------------

class OverwatchOracle:
    """The slow, deep thinker. Runs on CPU, sees everything, speaks rarely."""

    def __init__(
        self,
        config: OracleConfig | None = None,
        on_verdict: Callable[[OracleVerdict], None] | None = None,
        on_inject: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        context_builder: Callable[[], str] | None = None,
    ):
        self._config = config or OracleConfig()
        self._on_verdict = on_verdict or (lambda v: None)
        self._on_inject = on_inject or (lambda s: None)
        self._on_status = on_status or (lambda s: None)
        self._context_builder = context_builder

        self._cycle = 0
        self._question_index = 0
        self._verdicts: list[OracleVerdict] = []
        self._consecutive_actionable = 0

        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def verdicts(self) -> list[OracleVerdict]:
        return list(self._verdicts)

    @property
    def last_verdict(self) -> OracleVerdict | None:
        return self._verdicts[-1] if self._verdicts else None

    @property
    def cycle_count(self) -> int:
        return self._cycle

    def set_context_builder(self, fn: Callable[[], str]) -> None:
        """Set the function that builds the Oracle's input context."""
        self._context_builder = fn

    def start(self) -> None:
        """Start the Oracle background thread."""
        if self._running or not self._config.enabled:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._oracle_loop,
            name="overwatch-oracle",
            daemon=True,
        )
        self._thread.start()
        self._on_status("Oracle awakened")

    def stop(self) -> None:
        """Stop the Oracle."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        self._on_status("Oracle sleeping")

    def health_check(self) -> dict:
        """Check if the Oracle's LLM endpoint is reachable."""
        result = check_health(self._config.endpoint)
        result["cycle"] = self._cycle
        result["verdicts"] = len(self._verdicts)
        result["last_verdict"] = self._verdicts[-1].answer[:80] if self._verdicts else None
        return result

    def query_now(self, context: str | None = None, question: str | None = None) -> OracleVerdict:
        """Perform an immediate Oracle query (blocking, for manual use)."""
        if context is None and self._context_builder:
            context = self._context_builder()
        if context is None:
            context = "(no context available)"

        if question is None:
            question = self._next_question()

        return self._do_query(context, question)

    # ---- Internal ----

    def _next_question(self) -> str:
        """Get the next strategic question in rotation."""
        questions = self._config.questions
        q = questions[self._question_index % len(questions)]
        self._question_index += 1
        return q

    def _oracle_loop(self) -> None:
        """Background loop: query every cycle_seconds."""
        # Initial delay — let the system warm up before first query
        warmup = min(60.0, self._config.cycle_seconds / 2)
        waited = 0.0
        while self._running and waited < warmup:
            time.sleep(1)
            waited += 1

        while self._running:
            try:
                # Build context
                if self._context_builder:
                    context = self._context_builder()
                else:
                    context = "(no context builder configured)"

                # Pick question
                question = self._next_question()

                # Query
                verdict = self._do_query(context, question)

                # Track consecutive actionable verdicts
                if verdict.is_actionable:
                    self._consecutive_actionable += 1
                else:
                    self._consecutive_actionable = 0

                # Emit verdict
                self._on_verdict(verdict)

                # Check injection threshold
                if self._consecutive_actionable >= self._config.inject_threshold:
                    recent = self._verdicts[-self._config.inject_threshold:]
                    injection = self._format_injection(recent)
                    self._on_inject(injection)
                    self._consecutive_actionable = 0  # reset after injection

                # Status update
                self._on_status(
                    f"Oracle cycle {self._cycle} | "
                    f"{verdict.inference_seconds:.0f}s | "
                    f"{verdict.answer[:50]}"
                )

            except Exception as e:
                self._on_status(f"Oracle error: {e}")

            # Sleep until next cycle
            slept = 0.0
            while self._running and slept < self._config.cycle_seconds:
                time.sleep(1)
                slept += 1

    def _do_query(self, context: str, question: str) -> OracleVerdict:
        """Execute a single Oracle query."""
        self._cycle += 1
        prompt = build_oracle_prompt(context, question)

        start = time.time()
        raw = complete(
            self._config.endpoint,
            ORACLE_SYSTEM_PROMPT,
            prompt,
        )
        elapsed = time.time() - start

        # Clean the response — strip thinking tags, markdown, etc.
        answer = self._clean_response(raw)

        verdict = OracleVerdict(
            answer=answer,
            question=question,
            cycle=self._cycle,
            inference_seconds=elapsed,
        )
        self._verdicts.append(verdict)

        return verdict

    @staticmethod
    def _clean_response(raw: str) -> str:
        """Extract the actual answer from model output."""
        text = raw.strip()

        # Strip <think>...</think> tags (common in reasoning models)
        import re
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        # Strip markdown formatting
        text = re.sub(r"^\*\*|\*\*$", "", text).strip()

        # Take only the first sentence/line if model rambled
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if lines:
            text = lines[0]

        # Cap length
        if len(text) > 300:
            text = text[:297] + "..."

        return text

    @staticmethod
    def _format_injection(verdicts: list[OracleVerdict]) -> str:
        """Format multiple verdicts into an injection message."""
        lines = [
            "=" * 60,
            "OVERWATCH ORACLE — STRATEGIC ALERT",
            "=" * 60,
            f"The Oracle has flagged issues for {len(verdicts)} consecutive cycles:",
            "",
        ]
        for v in verdicts:
            lines.append(f"  Q: {v.question}")
            lines.append(f"  A: {v.answer}")
            lines.append("")

        lines.append("The Oracle sees something fundamental. Pause and consider.")
        lines.append("=" * 60)
        return "\n".join(lines)
