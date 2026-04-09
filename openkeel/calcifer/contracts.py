#!/usr/bin/env python3
"""Data contracts for the ground-up broker architecture."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional
import time
import uuid


class Mode(Enum):
    """Execution modes, cheapest to most expensive."""
    DIRECT = 1
    SEMANTIC = 2
    BOUNDED = 3
    LOCAL_LOOP = 4
    SONNET = 5
    OPUS = 6


@dataclass
class IntentionPacket:
    """What the user actually wants."""
    goal_id: str
    user_request: str
    intended_outcome: str
    must_preserve: list[str] = field(default_factory=list)
    forbidden_tradeoffs: list[str] = field(default_factory=list)
    success_shape: str = ""
    confidence: float = 0.5


@dataclass
class Task:
    """Persistent bounded job record."""
    id: str
    title: str
    objective: str
    acceptance_criteria: list[str]
    status: Literal["open", "done", "blocked"] = "open"
    summary: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class Check:
    """Single acceptance criterion."""
    kind: str  # "file_exists", "regex_match", "exit_code", "min_artifacts", "self_report", "llm_rubric"
    target: str
    expect: Any = None
    weight: float = 1.0


@dataclass
class EscalationPolicy:
    """When to escalate to a stronger runner."""
    retries_remaining: int = 1
    max_mode: Mode = Mode.SONNET


@dataclass
class StepSpec:
    """Single executable unit of work."""
    step_id: str
    step_kind: str  # "read", "search", "edit", "diagnose", "plan_slice"
    task_class: str  # "io", "reasoning", "coding", "judgment"
    quality_floor: float = 0.7
    latency_ceiling_s: float = 30.0
    replacement_mode: Mode = Mode.DIRECT
    allowed_tools: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    acceptance_contract: list[Check] = field(default_factory=list)
    escalation_policy: EscalationPolicy = field(default_factory=EscalationPolicy)


@dataclass
class StatusPacket:
    """Default output from every worker step."""
    step_id: str
    objective: str
    actions_taken: list[str]
    artifacts_touched: list[str]
    result_summary: str  # <= 40 lines
    acceptance_checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (name, passed, note)
    uncertainties: list[str] = field(default_factory=list)
    needs_escalation: bool = False
    recommended_next_step: Optional[str] = None
    raw_evidence_refs: list[str] = field(default_factory=list)
    runner_id: str = ""
    cost_units: float = 0.0


@dataclass
class CompletionDecision:
    """Broker or Opus-level decision about next state."""
    kind: Literal[
        "done",
        "continue",
        "retry",
        "escalate_runner",
        "escalate_mode",
        "request_raw_evidence",
        "blocked",
        "needs_plan",
        "needs_judge",
    ]
    step: Optional[StepSpec] = None
    note: str = ""


@dataclass
class TaskSession:
    """Live state for the active bounded job."""
    task: Task
    intention: IntentionPacket
    current_plan: list[StepSpec] = field(default_factory=list)
    plan_cursor: int = 0
    history: list[StatusPacket] = field(default_factory=list)
    budget_units: float = 0.0
    opus_calls: int = 0

    @property
    def last_status(self) -> Optional[StatusPacket]:
        """Return the most recent status packet."""
        return self.history[-1] if self.history else None
