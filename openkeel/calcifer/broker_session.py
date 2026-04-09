#!/usr/bin/env python3
"""BrokerSession: Pure Python broker orchestration (no Qt dependency).

Extracted core logic from BrokerGUIAdapter for use in CLI/tests.
Handles: classify → plan → execute → judge → respond
"""

import uuid
import json
import subprocess
import logging
from typing import Optional
from openkeel.calcifer.broker import Broker
from openkeel.calcifer.contracts import IntentionPacket, TaskSession
from openkeel.calcifer.band_classifier import BandClassifier, Band
from openkeel.calcifer.opus_planning_agent import OpusPlanningAgent
from openkeel.calcifer.sonnet_planning_agent import SonnetPlanningAgent
from openkeel.calcifer.opus_judgment_agent import OpusJudgmentAgent
from openkeel.calcifer.routing_policy import RoutingPolicy

logger = logging.getLogger("calcifer.broker_session")


class BrokerSession:
    """Pure Python broker session — no GUI, testable, CLI-friendly."""

    def __init__(self, session_id: Optional[str] = None, verbose: bool = False, policy: Optional[RoutingPolicy] = None):
        """Initialize session.

        Args:
            session_id: optional session identifier (defaults to random UUID)
            verbose: if True, log all decisions to stdout
            policy: routing policy (loads ~/.calcifer/config.json if None)
        """
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.verbose = verbose
        self.policy = policy or RoutingPolicy.load()
        self.broker = Broker()
        self.classifier = BandClassifier()
        self.opus_planner = OpusPlanningAgent()
        self.sonnet_planner = SonnetPlanningAgent()
        self.judge = OpusJudgmentAgent()
        self._current_session: Optional[TaskSession] = None
        self._message_history = []  # (user_msg, assistant_response) tuples

    def send_message(self, user_message: str) -> tuple[str, dict]:
        """Send a message and get response.

        Returns:
            (response_text, metadata_dict)
            metadata includes: band, routing_correct, latency, success, errors
        """
        metadata = {
            "session_id": self.session_id,
            "band": None,
            "routing_correct": None,
            "latency": 0.0,
            "success": False,
            "planner_failed": False,
            "errors": [],
        }

        import time
        start = time.time()

        try:
            # Step 0: Classify
            classification = self.classifier.classify(user_message)
            band = classification.band
            metadata["band"] = band.name
            self._log(f"[0] Classified as Band {band.name} (confidence={classification.confidence:.1f})")

            # Step 1: Create intention
            intention = IntentionPacket(
                goal_id=str(uuid.uuid4())[:8],
                user_request=user_message,
                intended_outcome=user_message[:100],
            )
            self._log(f"[1] Intention created: {intention.goal_id}")

            # Step 2: Plan (or skip)
            if classification.skip_planner:
                self._log(f"[2] Skipping planner (Band {band.name})")
                task, steps = self._create_direct_step(band, intention)
            else:
                _pm = self.policy.planner_model_for(band.name)
                planner = self.sonnet_planner if _pm == "sonnet" else self.opus_planner
                planner_name = _pm.capitalize()
                self._log(f"[2] Calling {planner_name}PlanningAgent...")
                try:
                    task, steps = planner.plan(intention)
                    self._log(f"[2] Plan: {len(steps)} steps")
                except Exception as e:
                    # Log error LOUDLY
                    error_msg = f"PLANNER ERROR ({planner_name}): {e}"
                    self._log(f"[2] ✗ {error_msg}")
                    metadata["planner_failed"] = True
                    metadata["errors"].append(error_msg)
                    # Fall back to simple plan
                    task, steps = self._create_fallback_step(intention)
                    self._log(f"[2] Using fallback plan")

            # Step 3: Broker executes
            self._log(f"[3] Executing {len(steps)} step(s)...")
            session = self.broker.run_task(intention, task, steps)
            self._current_session = session

            # Step 4: Judge if escalation needed
            escalation_count = 0
            while session.last_status and session.last_status.needs_escalation:
                escalation_count += 1
                self._log(f"[4.{escalation_count}] Escalating to Opus judgment...")
                decision = self.judge.judge(session, session.last_status)

                if decision.kind == "blocked":
                    metadata["errors"].append(f"Blocked: {decision.note}")
                    return f"BLOCKED: {decision.note}", metadata
                elif decision.kind == "continue":
                    step = session.current_plan[session.plan_cursor]
                    status = self.broker._execute_step(step)
                    session.history.append(status)
                    self.broker.evaluator.apply(status, step)
                else:
                    return decision.note, metadata

            # Step 5: Format response
            response = session.last_status.result_summary if session.last_status else ""
            metadata["success"] = True
            metadata["latency"] = time.time() - start
            metadata["routing_correct"] = classification.band.name == self._expected_band(user_message)

            # Store in history for context
            self._message_history.append((user_message, response))

            return response, metadata

        except Exception as e:
            metadata["errors"].append(f"Session error: {e}")
            metadata["latency"] = time.time() - start
            self._log(f"[!] FATAL: {e}")
            return f"ERROR: {e}", metadata

    def _create_direct_step(self, band: Band, intention: IntentionPacket):
        """Create a direct step for Band A/B (skip planning)."""
        from openkeel.calcifer.contracts import Task, StepSpec, Mode

        task = Task(
            id=str(uuid.uuid4())[:8],
            title="Direct Task",
            objective=intention.intended_outcome,
            acceptance_criteria=["completed"],
        )

        if band == Band.A:
            step_kind = "reason"
            mode = self.policy.mode_for_band_a()
        else:  # Band B
            step_kind = "read"
            mode = self.policy.mode_for_band_b()

        step = StepSpec(
            step_id=f"{task.id}_s0",
            step_kind=step_kind,
            task_class=band.value,
            replacement_mode=mode,
            inputs={"prompt": intention.user_request},
        )

        return task, [step]

    def _create_fallback_step(self, intention: IntentionPacket):
        """Create fallback step when planner fails."""
        from openkeel.calcifer.contracts import Task, StepSpec, Mode

        task = Task(
            id=str(uuid.uuid4())[:8],
            title="Fallback Task",
            objective=intention.intended_outcome,
            acceptance_criteria=["completed"],
        )

        step = StepSpec(
            step_id=f"{task.id}_s0",
            step_kind="reason",
            task_class="fallback",
            replacement_mode=Mode.SONNET,
            inputs={"prompt": intention.user_request},
        )

        return task, [step]

    def _expected_band(self, user_message: str) -> str:
        """Rough heuristic for expected band (for validation)."""
        if len(user_message) < 50 and any(x in user_message.lower() for x in ["hi", "thanks", "what time"]):
            return "A"
        if any(x in user_message.lower() for x in ["read", "list", "grep", "show"]):
            return "B"
        if any(x in user_message.lower() for x in ["design", "architect", "why"]):
            return "D"
        return "C"

    def _log(self, msg: str):
        """Log message to both logger and stdout if verbose."""
        logger.info(msg)
        if self.verbose:
            print(msg)

    def get_context(self, max_turns: int = 5) -> str:
        """Get recent message history for context in next turn."""
        recent = self._message_history[-max_turns:]
        lines = []
        for user, assistant in recent:
            lines.append(f"User: {user}")
            lines.append(f"Assistant: {assistant}\n")
        return "\n".join(lines)

    def clear_history(self):
        """Clear session history (start fresh)."""
        self._message_history = []
        self._current_session = None
