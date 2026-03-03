"""Pre-phase gate evaluation and phase state tracking.

Gate types:
  - file_exists: check that a file/directory exists
  - command_output: run a command and match output against a pattern
  - exit_code: run a command and check its exit code
  - external: call an HTTP endpoint and check response

Phase transitions are explicit — the orchestrator (or auto-advance on
timeout) controls them, never the agent.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profile import GateDef, PhaseDef, Profile

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Result of evaluating a single gate."""
    gate: GateDef
    passed: bool
    message: str = ""


def evaluate_gate(gate: GateDef) -> GateResult:
    """Evaluate a single gate definition.

    Returns a GateResult indicating pass/fail.
    """
    if gate.type == "file_exists":
        path = Path(gate.target).expanduser()
        passed = path.exists()
        msg = f"Path '{gate.target}' {'exists' if passed else 'does not exist'}"
        return GateResult(gate=gate, passed=passed, message=msg)

    if gate.type == "command_output":
        try:
            result = subprocess.run(
                gate.target,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout.strip()
            import re
            passed = bool(re.search(gate.expect, output))
            msg = f"Command output {'matched' if passed else 'did not match'} pattern '{gate.expect}'"
            return GateResult(gate=gate, passed=passed, message=msg)
        except (subprocess.SubprocessError, OSError) as exc:
            return GateResult(gate=gate, passed=False, message=f"Command failed: {exc}")

    if gate.type == "exit_code":
        try:
            result = subprocess.run(
                gate.target,
                shell=True,
                capture_output=True,
                timeout=30,
            )
            expected = int(gate.expect) if gate.expect else 0
            passed = result.returncode == expected
            msg = f"Exit code {result.returncode} {'==' if passed else '!='} expected {expected}"
            return GateResult(gate=gate, passed=passed, message=msg)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            return GateResult(gate=gate, passed=False, message=f"Command failed: {exc}")

    if gate.type == "external":
        try:
            import urllib.request
            with urllib.request.urlopen(gate.target, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                import re
                passed = bool(re.search(gate.expect, body)) if gate.expect else resp.status == 200
                msg = f"External check {'passed' if passed else 'failed'}"
                return GateResult(gate=gate, passed=passed, message=msg)
        except Exception as exc:
            return GateResult(gate=gate, passed=False, message=f"External check failed: {exc}")

    if gate.type == "memory_search":
        return _evaluate_memory_gate(gate)

    return GateResult(gate=gate, passed=False, message=f"Unknown gate type: {gate.type}")


def _evaluate_memory_gate(gate: GateDef) -> GateResult:
    """Query a memory backend and check for relevant results.

    Gate fields:
      - target: memory backend endpoint URL (e.g. "http://localhost:8000")
      - expect: search query string (supports {project}, {phase} placeholders)
      - message: description of what this gate checks for

    The gate passes if the search returns at least one result.
    Results are printed to stderr so the agent can see them.
    """
    try:
        from openkeel.integrations.memory import MemoryClient

        client = MemoryClient(endpoint=gate.target, timeout=15)
        if not client.is_available():
            return GateResult(
                gate=gate,
                passed=True,  # degrade gracefully — don't block on unavailable backend
                message=f"Memory backend at {gate.target} unavailable (skipped)",
            )

        results = client.search(gate.expect, top_k=5)
        if results:
            # Print results so the agent can use them
            import sys
            print(f"\n[openkeel] Memory recall ({len(results)} results for: {gate.expect}):", file=sys.stderr)
            for i, hit in enumerate(results[:5], 1):
                score = hit.get("score", 0)
                text = hit.get("text", "")[:200]
                print(f"  {i}. [{score:.2f}] {text}", file=sys.stderr)
            print("", file=sys.stderr)

            return GateResult(
                gate=gate,
                passed=True,
                message=f"Memory search returned {len(results)} results for '{gate.expect}'",
            )
        else:
            return GateResult(
                gate=gate,
                passed=True,  # no results is not a failure — just no prior knowledge
                message=f"Memory search returned no results for '{gate.expect}' (no prior knowledge)",
            )

    except Exception as exc:
        return GateResult(
            gate=gate,
            passed=True,  # degrade gracefully
            message=f"Memory gate error (skipped): {exc}",
        )


def can_enter_phase(phase: PhaseDef) -> tuple[bool, list[GateResult]]:
    """Check if all gates for a phase pass.

    Returns (can_enter, list_of_gate_results).
    """
    if not phase.gates:
        return True, []

    results = [evaluate_gate(gate) for gate in phase.gates]
    all_pass = all(r.passed for r in results)
    return all_pass, results


# ---------------------------------------------------------------------------
# Phase state tracking
# ---------------------------------------------------------------------------


def _load_phase_state(state_path: Path) -> dict[str, Any]:
    """Load phase state from JSON file."""
    if not state_path.exists():
        return {"current_index": -1, "entered_at": None, "history": []}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"current_index": -1, "entered_at": None, "history": []}


def _save_phase_state(state_path: Path, state: dict[str, Any]) -> None:
    """Save phase state to JSON file."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_current_phase(profile: Profile, state_path: str | Path) -> PhaseDef | None:
    """Get the current phase, or None if no phases or not started."""
    state_path = Path(state_path)
    state = _load_phase_state(state_path)
    idx = state.get("current_index", -1)
    if idx < 0 or idx >= len(profile.phases):
        return None
    return profile.phases[idx]


def advance_phase(
    profile: Profile,
    state_path: str | Path,
    log_path: str | Path = "",
    session_id: str = "",
    force: bool = False,
) -> tuple[bool, str]:
    """Advance to the next phase.

    Args:
        profile: Active profile.
        state_path: Path to phase state JSON.
        log_path: Path to JSONL log (for recording phase transition).
        session_id: Session ID for logging.
        force: Skip gate checks.

    Returns:
        (success, message)
    """
    if not profile.phases:
        return False, "No phases defined in profile"

    state_path = Path(state_path)
    state = _load_phase_state(state_path)
    current_idx = state.get("current_index", -1)
    next_idx = current_idx + 1

    if next_idx >= len(profile.phases):
        return False, "Already at the last phase"

    next_phase = profile.phases[next_idx]

    # Check gates unless forced
    if not force:
        can_enter, results = can_enter_phase(next_phase)
        if not can_enter:
            failed = [r for r in results if not r.passed]
            msgs = "; ".join(r.message for r in failed)
            return False, f"Gate check failed for phase '{next_phase.name}': {msgs}"

    # Record transition
    now = time.time()
    history = state.get("history", [])
    if current_idx >= 0:
        history.append({
            "phase": profile.phases[current_idx].name,
            "entered_at": state.get("entered_at"),
            "exited_at": now,
        })

    state["current_index"] = next_idx
    state["entered_at"] = now
    state["history"] = history
    _save_phase_state(state_path, state)

    # Log to JSONL if log_path provided
    if log_path:
        from .audit import log_event
        log_event(
            log_path=log_path,
            event_type="phase_advance",
            data={"from_phase": profile.phases[current_idx].name if current_idx >= 0 else "",
                  "to_phase": next_phase.name},
            session_id=session_id,
        )

    return True, f"Advanced to phase '{next_phase.name}'"


def check_phase_timeout(
    profile: Profile,
    state_path: str | Path,
) -> tuple[bool, float]:
    """Check if the current phase has timed out.

    Returns (timed_out, remaining_minutes).
    Remaining is negative if timed out.
    """
    state_path = Path(state_path)
    state = _load_phase_state(state_path)
    current_idx = state.get("current_index", -1)

    if current_idx < 0 or current_idx >= len(profile.phases):
        return False, 0.0

    phase = profile.phases[current_idx]
    if phase.timeout_minutes <= 0:
        return False, 0.0  # no timeout

    entered_at = state.get("entered_at")
    if entered_at is None:
        return False, float(phase.timeout_minutes)

    elapsed = (time.time() - entered_at) / 60.0
    remaining = phase.timeout_minutes - elapsed

    return remaining < 0, remaining
