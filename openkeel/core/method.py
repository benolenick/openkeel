"""Method system — structured cognitive workflows for AI agents.

A method defines HOW an agent should approach work: ordered rounds with
evidence-based gating, transition conditions, and brute-force caps.

Methods are YAML files in ~/.openkeel/methods/ (user) or bundled in
openkeel/methods/ (defaults). A profile references a method by name.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


OPENKEEL_DIR = Path.home() / ".openkeel"
METHOD_STATE_PATH = OPENKEEL_DIR / "method_state.json"
USER_METHODS_DIR = OPENKEEL_DIR / "methods"
BUNDLED_METHODS_DIR = Path(__file__).parent.parent / "methods"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EvidenceGate:
    """Confidence threshold required before proceeding."""
    min_confidence: str = "PARTIAL"  # GOOD, PARTIAL, LOW, NONE
    auto_query: bool = True          # auto-query retrieval stack

    # Thresholds (used by enforce hook to classify results)
    good_min_hits: int = 2       # N results >= high_score = GOOD
    high_score: float = 0.6      # individual result score threshold
    partial_avg: float = 0.4     # avg score >= this = PARTIAL


@dataclass
class TransitionDef:
    """How and when a round transitions to the next."""
    type: str = "manual"          # tool_signal, manual, heuristic
    signal_patterns: list[str] = field(default_factory=list)  # regex for tool_signal
    min_commands: int = 2         # minimum commands before transition eligible
    idle_commands: int = 5        # heuristic: N commands without block → advance
    auto_advance: bool = False


@dataclass
class RoundDef:
    """A single round/phase in a method."""
    name: str = ""
    description: str = ""
    goal: str = ""
    allowed_activities: list[str] = field(default_factory=list)  # activity names from profile
    evidence_gate: EvidenceGate | None = None
    transition: TransitionDef = field(default_factory=TransitionDef)
    max_blind_attempts: int = 3       # after N fails without research → force search
    max_attempts_per_hypothesis: int = 2  # same approach N times → pivot
    research_required: bool = True


@dataclass
class Method:
    """A complete methodology — ordered rounds with evidence gating."""
    name: str = ""
    description: str = ""
    version: str = "1"
    rounds: list[RoundDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def _parse_evidence_gate(raw: dict | None) -> EvidenceGate | None:
    if not raw or not isinstance(raw, dict):
        return None
    return EvidenceGate(
        min_confidence=raw.get("min_confidence", "PARTIAL"),
        auto_query=raw.get("auto_query", True),
        good_min_hits=raw.get("good_min_hits", 2),
        high_score=raw.get("high_score", 0.6),
        partial_avg=raw.get("partial_avg", 0.4),
    )


def _parse_transition(raw: dict | None) -> TransitionDef:
    if not raw or not isinstance(raw, dict):
        return TransitionDef()
    return TransitionDef(
        type=raw.get("type", "manual"),
        signal_patterns=raw.get("signal_patterns", []),
        min_commands=raw.get("min_commands", 2),
        idle_commands=raw.get("idle_commands", 5),
        auto_advance=raw.get("auto_advance", False),
    )


def _parse_round(raw: dict) -> RoundDef:
    return RoundDef(
        name=raw.get("name", ""),
        description=raw.get("description", ""),
        goal=raw.get("goal", ""),
        allowed_activities=raw.get("allowed_activities", []),
        evidence_gate=_parse_evidence_gate(raw.get("evidence_gate")),
        transition=_parse_transition(raw.get("transition")),
        max_blind_attempts=raw.get("max_blind_attempts", 3),
        max_attempts_per_hypothesis=raw.get("max_attempts_per_hypothesis", 2),
        research_required=raw.get("research_required", True),
    )


def _parse_method(data: dict) -> Method:
    rounds = [_parse_round(r) for r in data.get("rounds", [])]
    return Method(
        name=data.get("name", ""),
        description=data.get("description", ""),
        version=str(data.get("version", "1")),
        rounds=rounds,
    )


def load_method(name_or_path: str) -> Method:
    """Load a method by name (searches user dir then bundled) or by path."""
    path = Path(name_or_path)
    if path.exists() and path.is_file():
        pass  # use as-is
    else:
        # Search user methods dir, then bundled
        for d in [USER_METHODS_DIR, BUNDLED_METHODS_DIR]:
            for ext in ("", ".yaml", ".yml"):
                candidate = d / f"{name_or_path}{ext}"
                if candidate.exists():
                    path = candidate
                    break
            else:
                continue
            break
        else:
            raise FileNotFoundError(f"Method '{name_or_path}' not found")

    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError(f"Method file '{path}' is not a valid YAML mapping")

    return _parse_method(data)


def list_methods() -> list[str]:
    """List available method names."""
    names: set[str] = set()
    for d in [USER_METHODS_DIR, BUNDLED_METHODS_DIR]:
        if d.exists():
            for f in d.iterdir():
                if f.suffix in (".yaml", ".yml"):
                    names.add(f.stem)
    return sorted(names)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_method_state() -> dict:
    """Read current method state."""
    if METHOD_STATE_PATH.exists():
        try:
            return json.loads(METHOD_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_method_state(state: dict) -> None:
    METHOD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    METHOD_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def init_method_state(method: Method) -> dict:
    """Create fresh state for a method."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "method_name": method.name,
        "current_round": 0,
        "round_name": method.rounds[0].name if method.rounds else "",
        "started_at": now,
        "round_started_at": now,
        "round_command_count": 0,
        "round_blocks": 0,
        "commands_without_block": 0,
        "blind_attempts": 0,
        "hypothesis_attempts": {},
        "confidence_history": [],
        "transitions": [],
    }
    save_method_state(state)
    return state


def advance_round(method: Method, state: dict, force: bool = False) -> tuple[bool, str]:
    """Advance to the next round. Returns (success, message)."""
    from datetime import datetime, timezone
    idx = state.get("current_round", 0)
    if idx + 1 >= len(method.rounds):
        return False, "Already at the final round."

    current = method.rounds[idx]
    if not force:
        cmd_count = state.get("round_command_count", 0)
        if cmd_count < current.transition.min_commands:
            return False, (
                f"Round '{current.name}' requires at least "
                f"{current.transition.min_commands} commands (have {cmd_count}). "
                f"Use --force to override."
            )

    now = datetime.now(timezone.utc).isoformat()
    next_round = method.rounds[idx + 1]

    state["transitions"].append({
        "from": current.name,
        "to": next_round.name,
        "trigger": "manual" if force else "advance",
        "ts": now,
    })
    state["current_round"] = idx + 1
    state["round_name"] = next_round.name
    state["round_started_at"] = now
    state["round_command_count"] = 0
    state["round_blocks"] = 0
    state["commands_without_block"] = 0
    state["blind_attempts"] = 0
    state["hypothesis_attempts"] = {}

    save_method_state(state)
    return True, f"Advanced to round '{next_round.name}': {next_round.description}"


# ---------------------------------------------------------------------------
# Defaults for GUI — build a Method from sensible defaults
# ---------------------------------------------------------------------------

def default_method() -> Method:
    """Return a sensible default method for offensive security."""
    return Method(
        name="default",
        description="Default structured methodology",
        version="1",
        rounds=[
            RoundDef(
                name="recon",
                description="Network discovery and port scanning",
                goal="Map the attack surface",
                allowed_activities=["recon"],
                evidence_gate=None,
                transition=TransitionDef(
                    type="heuristic", min_commands=2, idle_commands=4, auto_advance=True,
                ),
                max_blind_attempts=5,
                research_required=False,
            ),
            RoundDef(
                name="enumerate",
                description="Service-specific enumeration",
                goal="Identify vulnerabilities and misconfigurations",
                allowed_activities=["enumeration", "dir_bruteforce"],
                evidence_gate=EvidenceGate(min_confidence="PARTIAL"),
                transition=TransitionDef(
                    type="heuristic", min_commands=3, idle_commands=6, auto_advance=True,
                ),
                max_blind_attempts=3,
                research_required=True,
            ),
            RoundDef(
                name="exploit",
                description="Active exploitation",
                goal="Get initial access",
                allowed_activities=["exploitation", "password_attack"],
                evidence_gate=EvidenceGate(min_confidence="GOOD"),
                transition=TransitionDef(type="manual"),
                max_blind_attempts=4,
                max_attempts_per_hypothesis=2,
                research_required=True,
            ),
            RoundDef(
                name="post-exploit",
                description="Privilege escalation and lateral movement",
                goal="Escalate privileges or move laterally",
                allowed_activities=["privesc"],
                evidence_gate=EvidenceGate(min_confidence="PARTIAL"),
                transition=TransitionDef(type="manual"),
                max_blind_attempts=3,
                research_required=True,
            ),
        ],
    )


def method_to_dict(m: Method) -> dict:
    """Serialize a Method to a plain dict (for JSON/YAML export)."""
    def _gate(g: EvidenceGate | None) -> dict | None:
        if g is None:
            return None
        return {
            "min_confidence": g.min_confidence,
            "auto_query": g.auto_query,
            "good_min_hits": g.good_min_hits,
            "high_score": g.high_score,
            "partial_avg": g.partial_avg,
        }

    def _trans(t: TransitionDef) -> dict:
        return {
            "type": t.type,
            "signal_patterns": t.signal_patterns,
            "min_commands": t.min_commands,
            "idle_commands": t.idle_commands,
            "auto_advance": t.auto_advance,
        }

    def _round(r: RoundDef) -> dict:
        return {
            "name": r.name,
            "description": r.description,
            "goal": r.goal,
            "allowed_activities": r.allowed_activities,
            "evidence_gate": _gate(r.evidence_gate),
            "transition": _trans(r.transition),
            "max_blind_attempts": r.max_blind_attempts,
            "max_attempts_per_hypothesis": r.max_attempts_per_hypothesis,
            "research_required": r.research_required,
        }

    return {
        "name": m.name,
        "description": m.description,
        "version": m.version,
        "rounds": [_round(r) for r in m.rounds],
    }
