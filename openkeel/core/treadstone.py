"""Treadstone v2 — structured attack methodology with hypothesis tracking.

Tree-based attack model: Mission → Stones → Hypotheses → Attempts
Each node carries Bayesian confidence, KT analysis, and circuit breaker state.
Persistence via YAML alongside existing mission data.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_ABANDON_THRESHOLD = 0.20  # 20% confidence → abandon
DEFAULT_INITIAL_CONFIDENCE = 0.50
CONFIDENCE_DECAY_ON_FAIL = 0.40  # multiply by (1 - this) on failure
CONFIDENCE_BOOST_ON_PARTIAL = 0.15  # add this on partial success
CONFIDENCE_BOOST_ON_SUCCESS = 1.0  # set to 1.0 on success


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class KTAnalysis:
    """Kepner-Tregoe problem analysis for a hypothesis."""
    is_observed: list[str] = field(default_factory=list)       # what IS happening
    is_not_observed: list[str] = field(default_factory=list)   # what ISN'T happening
    distinctions: list[str] = field(default_factory=list)      # what's unique about IS
    changes: list[str] = field(default_factory=list)           # what changed recently
    probable_cause: str = ""
    tested: bool = False
    contradictions: list[str] = field(default_factory=list)    # evidence that contradicts


@dataclass
class Attempt:
    """A single execution attempt against a hypothesis."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    timestamp: str = ""
    command: str = ""
    expected_outcome: str = ""
    actual_outcome: str = ""
    result: str = "pending"  # pending, success, fail, partial
    notes: str = ""
    duration_s: float = 0.0


@dataclass
class Hypothesis:
    """A testable theory about how to achieve the stone's goal."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label: str = ""
    rationale: str = ""  # WHY this should work (evidence-based)
    confidence: float = DEFAULT_INITIAL_CONFIDENCE
    confidence_history: list[dict] = field(default_factory=list)  # [{ts, value, reason}]
    status: str = "active"  # active, succeeded, failed, abandoned
    attempts: list[Attempt] = field(default_factory=list)
    kt: KTAnalysis = field(default_factory=KTAnalysis)
    parent_stone_id: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        return len([a for a in self.attempts if a.result != "pending"])

    @property
    def failed_count(self) -> int:
        return len([a for a in self.attempts if a.result == "fail"])


@dataclass
class StoneNode:
    """A stepping stone in the attack tree."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label: str = ""
    objective: str = ""
    status: str = "pending"  # pending, active, done, failed, pivoted
    phase: str = "recon"  # current phase: recon, research, run, review
    hypotheses: list[Hypothesis] = field(default_factory=list)
    parent_id: Optional[str] = None  # parent stone (for branching)
    children_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""
    pivot_reason: str = ""  # why we branched from parent

    # Sheets content (inline for small data, file refs for large)
    recon_summary: str = ""
    research_summary: str = ""
    run_summary: str = ""
    review_summary: str = ""

    # Environment snapshot (to catch namespace-type blindness)
    environment: dict = field(default_factory=dict)  # {key: value} captured during recon


@dataclass
class CircuitBreaker:
    """Prevents infinite loops on dead-end hypotheses."""
    max_attempts_per_hypothesis: int = DEFAULT_MAX_ATTEMPTS
    abandon_threshold: float = DEFAULT_ABANDON_THRESHOLD
    require_preflight: bool = True
    contradiction_alert: bool = True
    total_stone_timeout_min: int = 60  # max minutes on one stone before forced review


@dataclass
class TreadstoneTree:
    """The full attack tree for a mission."""
    mission_name: str = ""
    stones: list[StoneNode] = field(default_factory=list)
    active_stone_id: Optional[str] = None
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Bayesian confidence updates
# ---------------------------------------------------------------------------

def update_confidence(
    hypothesis: Hypothesis,
    result: str,
    reason: str = "",
) -> float:
    """Update hypothesis confidence based on attempt result.

    Returns new confidence value.
    """
    old = hypothesis.confidence
    if result == "success":
        new = 1.0
    elif result == "partial":
        new = min(1.0, old + CONFIDENCE_BOOST_ON_PARTIAL)
    elif result == "fail":
        new = old * (1.0 - CONFIDENCE_DECAY_ON_FAIL)
    else:
        new = old

    hypothesis.confidence = round(new, 3)
    hypothesis.confidence_history.append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "old": old,
        "new": hypothesis.confidence,
        "result": result,
        "reason": reason,
    })
    return hypothesis.confidence


def redistribute_confidence(hypotheses: list[Hypothesis]) -> None:
    """After updating one hypothesis, normalize active ones to sum ~1.0."""
    active = [h for h in hypotheses if h.status == "active"]
    if not active:
        return
    total = sum(h.confidence for h in active)
    if total <= 0:
        # All at zero — equal distribution
        for h in active:
            h.confidence = round(1.0 / len(active), 3)
        return
    for h in active:
        h.confidence = round(h.confidence / total, 3)


# ---------------------------------------------------------------------------
# Circuit breaker checks
# ---------------------------------------------------------------------------

def check_circuit_breaker(
    stone: StoneNode,
    hypothesis: Hypothesis,
    cb: CircuitBreaker,
) -> list[str]:
    """Check circuit breaker conditions. Returns list of alert messages."""
    alerts = []

    # Max attempts exceeded
    if hypothesis.attempt_count >= cb.max_attempts_per_hypothesis:
        alerts.append(
            f"CIRCUIT BREAKER: Hypothesis '{hypothesis.label}' has "
            f"{hypothesis.attempt_count}/{cb.max_attempts_per_hypothesis} attempts. "
            f"Must abandon or provide new evidence."
        )

    # Confidence below threshold
    if hypothesis.confidence <= cb.abandon_threshold:
        alerts.append(
            f"ABANDON THRESHOLD: '{hypothesis.label}' confidence at "
            f"{hypothesis.confidence:.0%} (threshold: {cb.abandon_threshold:.0%}). "
            f"Recommend abandoning this hypothesis."
        )

    # KT contradictions
    if cb.contradiction_alert and hypothesis.kt.contradictions:
        alerts.append(
            f"KT CONTRADICTION: '{hypothesis.label}' has unresolved contradictions: "
            + "; ".join(hypothesis.kt.contradictions)
        )

    return alerts


def should_force_review(stone: StoneNode, cb: CircuitBreaker) -> bool:
    """Check if stone has been active too long without review."""
    if not stone.created_at:
        return False
    try:
        created = time.mktime(time.strptime(stone.created_at, "%Y-%m-%dT%H:%M:%S"))
        elapsed_min = (time.time() - created) / 60
        return elapsed_min >= cb.total_stone_timeout_min
    except (ValueError, OverflowError):
        return False


# ---------------------------------------------------------------------------
# KT Analysis helpers
# ---------------------------------------------------------------------------

def kt_check_consistency(kt: KTAnalysis, hypothesis_label: str) -> list[str]:
    """Check if probable cause is consistent with all IS/ISN'T observations.

    Returns list of contradiction strings. Empty = consistent.
    """
    contradictions = []
    if not kt.probable_cause:
        return contradictions

    cause_lower = kt.probable_cause.lower()

    # Simple heuristic: if IS_NOT items are mentioned in the cause, flag it
    for item in kt.is_not_observed:
        item_lower = item.lower()
        # Check for obvious contradictions (environment words that appear in both)
        for word in item_lower.split():
            if len(word) > 4 and word in cause_lower:
                contradictions.append(
                    f"Cause mentions '{word}' but '{item}' is in ISN'T column"
                )

    return contradictions


def kt_reconcile(hypothesis: Hypothesis) -> None:
    """Force KT reconciliation — check cause against all evidence and update contradictions."""
    hypothesis.kt.contradictions = kt_check_consistency(
        hypothesis.kt, hypothesis.label
    )


# ---------------------------------------------------------------------------
# Tree operations
# ---------------------------------------------------------------------------

def create_tree(mission_name: str, cb: CircuitBreaker | None = None) -> TreadstoneTree:
    """Create a new empty attack tree."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    return TreadstoneTree(
        mission_name=mission_name,
        stones=[],
        circuit_breaker=cb or CircuitBreaker(),
        created_at=now,
        updated_at=now,
    )


def add_stone(
    tree: TreadstoneTree,
    label: str,
    objective: str = "",
    parent_id: str | None = None,
    pivot_reason: str = "",
) -> StoneNode:
    """Add a new stone to the tree."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    stone = StoneNode(
        label=label,
        objective=objective,
        status="active" if not tree.stones else "pending",
        parent_id=parent_id,
        created_at=now,
        pivot_reason=pivot_reason,
    )

    # Link parent → child
    if parent_id:
        for s in tree.stones:
            if s.id == parent_id:
                s.children_ids.append(stone.id)
                break

    tree.stones.append(stone)
    if tree.active_stone_id is None:
        tree.active_stone_id = stone.id

    tree.updated_at = now
    return stone


def add_hypothesis(
    stone: StoneNode,
    label: str,
    rationale: str = "",
    initial_confidence: float = DEFAULT_INITIAL_CONFIDENCE,
    tags: list[str] | None = None,
) -> Hypothesis:
    """Add a hypothesis to a stone."""
    h = Hypothesis(
        label=label,
        rationale=rationale,
        confidence=initial_confidence,
        parent_stone_id=stone.id,
        tags=tags or [],
    )
    stone.hypotheses.append(h)
    redistribute_confidence(stone.hypotheses)
    return h


def record_attempt(
    hypothesis: Hypothesis,
    command: str,
    expected: str,
    actual: str,
    result: str,
    notes: str = "",
    duration_s: float = 0.0,
) -> Attempt:
    """Record an attempt and update confidence."""
    attempt = Attempt(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        command=command,
        expected_outcome=expected,
        actual_outcome=actual,
        result=result,
        notes=notes,
        duration_s=duration_s,
    )
    hypothesis.attempts.append(attempt)
    update_confidence(hypothesis, result, reason=notes or actual[:100])
    return attempt


def abandon_hypothesis(hypothesis: Hypothesis, reason: str = "") -> None:
    """Mark a hypothesis as abandoned."""
    hypothesis.status = "abandoned"
    hypothesis.confidence = 0.0
    hypothesis.confidence_history.append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "old": hypothesis.confidence,
        "new": 0.0,
        "result": "abandoned",
        "reason": reason,
    })


def succeed_hypothesis(hypothesis: Hypothesis) -> None:
    """Mark a hypothesis as succeeded."""
    hypothesis.status = "succeeded"
    hypothesis.confidence = 1.0


def get_active_stone(tree: TreadstoneTree) -> StoneNode | None:
    """Get the currently active stone."""
    if not tree.active_stone_id:
        return None
    for s in tree.stones:
        if s.id == tree.active_stone_id:
            return s
    return None


def advance_to_stone(tree: TreadstoneTree, stone_id: str) -> StoneNode | None:
    """Set a specific stone as active."""
    for s in tree.stones:
        if s.id == stone_id:
            # Mark previous active as done if it was active
            if tree.active_stone_id:
                for prev in tree.stones:
                    if prev.id == tree.active_stone_id and prev.status == "active":
                        prev.status = "done"
                        prev.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            s.status = "active"
            tree.active_stone_id = stone_id
            tree.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            return s
    return None


# ---------------------------------------------------------------------------
# Status line generation
# ---------------------------------------------------------------------------

def tree_status_line(tree: TreadstoneTree) -> str:
    """Generate a compact status line for the toolbar/status bar."""
    stone = get_active_stone(tree)
    if not stone:
        return "TREADSTONE: no active stone"

    parts = [f"STONE: {stone.label}", f"PHASE: {stone.phase.title()}"]

    active_hyps = [h for h in stone.hypotheses if h.status == "active"]
    if active_hyps:
        hyp_strs = []
        for i, h in enumerate(active_hyps[:4]):
            hyp_strs.append(f"H{i+1}: {h.confidence:.0%}")
        parts.append(" ".join(hyp_strs))

        # Show attempt count for top hypothesis
        if active_hyps:
            top = max(active_hyps, key=lambda h: h.confidence)
            max_att = tree.circuit_breaker.max_attempts_per_hypothesis
            parts.append(f"Attempts: {top.attempt_count}/{max_att}")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _kt_to_dict(kt: KTAnalysis) -> dict:
    return {
        "is_observed": kt.is_observed,
        "is_not_observed": kt.is_not_observed,
        "distinctions": kt.distinctions,
        "changes": kt.changes,
        "probable_cause": kt.probable_cause,
        "tested": kt.tested,
        "contradictions": kt.contradictions,
    }


def _attempt_to_dict(a: Attempt) -> dict:
    return {
        "id": a.id,
        "timestamp": a.timestamp,
        "command": a.command,
        "expected_outcome": a.expected_outcome,
        "actual_outcome": a.actual_outcome,
        "result": a.result,
        "notes": a.notes,
        "duration_s": a.duration_s,
    }


def _hypothesis_to_dict(h: Hypothesis) -> dict:
    return {
        "id": h.id,
        "label": h.label,
        "rationale": h.rationale,
        "confidence": h.confidence,
        "confidence_history": h.confidence_history,
        "status": h.status,
        "attempts": [_attempt_to_dict(a) for a in h.attempts],
        "kt": _kt_to_dict(h.kt),
        "parent_stone_id": h.parent_stone_id,
        "tags": h.tags,
    }


def _stone_to_dict(s: StoneNode) -> dict:
    return {
        "id": s.id,
        "label": s.label,
        "objective": s.objective,
        "status": s.status,
        "phase": s.phase,
        "hypotheses": [_hypothesis_to_dict(h) for h in s.hypotheses],
        "parent_id": s.parent_id,
        "children_ids": s.children_ids,
        "created_at": s.created_at,
        "completed_at": s.completed_at,
        "pivot_reason": s.pivot_reason,
        "recon_summary": s.recon_summary,
        "research_summary": s.research_summary,
        "run_summary": s.run_summary,
        "review_summary": s.review_summary,
        "environment": s.environment,
    }


def tree_to_dict(tree: TreadstoneTree) -> dict:
    return {
        "mission_name": tree.mission_name,
        "stones": [_stone_to_dict(s) for s in tree.stones],
        "active_stone_id": tree.active_stone_id,
        "circuit_breaker": {
            "max_attempts_per_hypothesis": tree.circuit_breaker.max_attempts_per_hypothesis,
            "abandon_threshold": tree.circuit_breaker.abandon_threshold,
            "require_preflight": tree.circuit_breaker.require_preflight,
            "contradiction_alert": tree.circuit_breaker.contradiction_alert,
            "total_stone_timeout_min": tree.circuit_breaker.total_stone_timeout_min,
        },
        "created_at": tree.created_at,
        "updated_at": tree.updated_at,
    }


def _kt_from_dict(d: dict) -> KTAnalysis:
    return KTAnalysis(
        is_observed=d.get("is_observed", []),
        is_not_observed=d.get("is_not_observed", []),
        distinctions=d.get("distinctions", []),
        changes=d.get("changes", []),
        probable_cause=d.get("probable_cause", ""),
        tested=d.get("tested", False),
        contradictions=d.get("contradictions", []),
    )


def _attempt_from_dict(d: dict) -> Attempt:
    return Attempt(
        id=d.get("id", uuid.uuid4().hex[:8]),
        timestamp=d.get("timestamp", ""),
        command=d.get("command", ""),
        expected_outcome=d.get("expected_outcome", ""),
        actual_outcome=d.get("actual_outcome", ""),
        result=d.get("result", "pending"),
        notes=d.get("notes", ""),
        duration_s=d.get("duration_s", 0.0),
    )


def _hypothesis_from_dict(d: dict) -> Hypothesis:
    return Hypothesis(
        id=d.get("id", uuid.uuid4().hex[:8]),
        label=d.get("label", ""),
        rationale=d.get("rationale", ""),
        confidence=d.get("confidence", DEFAULT_INITIAL_CONFIDENCE),
        confidence_history=d.get("confidence_history", []),
        status=d.get("status", "active"),
        attempts=[_attempt_from_dict(a) for a in d.get("attempts", [])],
        kt=_kt_from_dict(d.get("kt", {})),
        parent_stone_id=d.get("parent_stone_id", ""),
        tags=d.get("tags", []),
    )


def _stone_from_dict(d: dict) -> StoneNode:
    return StoneNode(
        id=d.get("id", uuid.uuid4().hex[:8]),
        label=d.get("label", ""),
        objective=d.get("objective", ""),
        status=d.get("status", "pending"),
        phase=d.get("phase", "recon"),
        hypotheses=[_hypothesis_from_dict(h) for h in d.get("hypotheses", [])],
        parent_id=d.get("parent_id"),
        children_ids=d.get("children_ids", []),
        created_at=d.get("created_at", ""),
        completed_at=d.get("completed_at", ""),
        pivot_reason=d.get("pivot_reason", ""),
        recon_summary=d.get("recon_summary", ""),
        research_summary=d.get("research_summary", ""),
        run_summary=d.get("run_summary", ""),
        review_summary=d.get("review_summary", ""),
        environment=d.get("environment", {}),
    )


def tree_from_dict(d: dict) -> TreadstoneTree:
    cb_raw = d.get("circuit_breaker", {})
    cb = CircuitBreaker(
        max_attempts_per_hypothesis=cb_raw.get("max_attempts_per_hypothesis", DEFAULT_MAX_ATTEMPTS),
        abandon_threshold=cb_raw.get("abandon_threshold", DEFAULT_ABANDON_THRESHOLD),
        require_preflight=cb_raw.get("require_preflight", True),
        contradiction_alert=cb_raw.get("contradiction_alert", True),
        total_stone_timeout_min=cb_raw.get("total_stone_timeout_min", 60),
    )
    return TreadstoneTree(
        mission_name=d.get("mission_name", ""),
        stones=[_stone_from_dict(s) for s in d.get("stones", [])],
        active_stone_id=d.get("active_stone_id"),
        circuit_breaker=cb,
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def tree_path(mission_dir: Path) -> Path:
    """Path to the treadstone tree file for a mission."""
    return mission_dir / "treadstone_tree.yaml"


def save_tree(mission_dir: Path, tree: TreadstoneTree) -> None:
    """Save tree to YAML."""
    tree.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    mission_dir.mkdir(parents=True, exist_ok=True)
    tree_path(mission_dir).write_text(
        yaml.dump(tree_to_dict(tree), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def load_tree(mission_dir: Path) -> TreadstoneTree | None:
    """Load tree from YAML. Returns None if not found."""
    p = tree_path(mission_dir)
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    return tree_from_dict(data)
