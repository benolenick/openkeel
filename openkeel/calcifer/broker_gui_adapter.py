#!/usr/bin/env python3
"""BrokerGUIAdapter: thin layer between ladder_chat and the broker.

Flow: classify → plan (or skip) → execute → judge → respond
"""

import uuid
from openkeel.calcifer.broker import Broker
from openkeel.calcifer.contracts import IntentionPacket, TaskSession, Task, StepSpec, Mode
from openkeel.calcifer.band_classifier import BandClassifier, Band, BandRouter
from openkeel.calcifer.opus_planning_agent import OpusPlanningAgent
from openkeel.calcifer.sonnet_planning_agent import SonnetPlanningAgent
from openkeel.calcifer.opus_judgment_agent import OpusJudgmentAgent


class BrokerGUIAdapter:
    """Orchestrate: classify → plan (maybe) → execute → judge → respond."""

    def __init__(self):
        self.broker = Broker()
        self.classifier = BandClassifier()
        self.opus_planner = OpusPlanningAgent()
        self.sonnet_planner = SonnetPlanningAgent()
        self.judge = OpusJudgmentAgent()
        self._current_session: dict[str, TaskSession] = {}
        self._token_log = []

    def handle_user_message(self, user_message: str, session_id: str) -> str:
        """Process a user message end-to-end with band classification."""
        self._token_log = []

        # Step 0: Classify message into band
        classification = self.classifier.classify(user_message)
        band = classification.band
        self._token_log.append(f"[0] Band classified: {band.value} ({classification.reasoning})")
        self._token_log.append(f"    Confidence: {classification.confidence}")

        # Step 1: Create intention from user message
        intention = IntentionPacket(
            goal_id=str(uuid.uuid4())[:8],
            user_request=user_message,
            intended_outcome=user_message[:100],
        )
        self._token_log.append(f"[1] Intention created: goal_id={intention.goal_id}")

        # Step 2: Plan (or skip for trivial bands)
        if classification.skip_planner:
            # Band A/B: skip planner, create direct step
            self._token_log.append(f"[2] Skipping planner (Band {band.name})")
            task, steps = self._create_direct_step(band, intention)
        else:
            # Band C/D/E: use appropriate planner
            if band == Band.C:
                self._token_log.append("[2] → SonnetPlanningAgent.plan() [SONNET subprocess]")
                task, steps = self.sonnet_planner.plan(intention)
            else:  # Band D or E
                self._token_log.append("[2] → OpusPlanningAgent.plan() [OPUS subprocess]")
                task, steps = self.opus_planner.plan(intention)

        self._token_log.append(f"[2] ← Plan: {len(steps)} steps")
        for i, step in enumerate(steps):
            self._token_log.append(f"    {i}. {step.step_kind} (mode={step.replacement_mode.name})")

        # Step 3: Broker executes
        self._token_log.append("[3] Broker.run_task() executes steps:")
        session = self.broker.run_task(intention, task, steps)
        self._current_session[session_id] = session
        for i, status in enumerate(session.history):
            self._token_log.append(f"    {i}. {status.runner_id} (cost={status.cost_units})")

        # Step 4: If blocked, loop back to Opus judgment
        escalation_count = 0
        while session.last_status and session.last_status.needs_escalation:
            escalation_count += 1
            self._token_log.append(f"[4.{escalation_count}] → OpusJudgmentAgent.judge() [OPUS subprocess]")
            decision = self.judge.judge(session, session.last_status)
            self._token_log.append(f"[4.{escalation_count}] ← Decision: {decision.kind}")

            if decision.kind == "blocked":
                self._log_token_summary()
                return self._format_response(session, "BLOCKED: " + decision.note)
            elif decision.kind == "continue":
                step = session.current_plan[session.plan_cursor]
                status = self.broker._execute_step(step)
                session.history.append(status)
                self.broker.evaluator.apply(status, step)
            else:
                self._log_token_summary()
                return self._format_response(session, decision.note)

        # Step 5: Return final result
        self._log_token_summary()
        return self._format_response(session, "COMPLETE")

    def _create_direct_step(self, band: Band, intention: IntentionPacket) -> tuple[Task, list[StepSpec]]:
        """Create a direct step for Band A/B (no planning)."""
        task = Task(
            id=str(uuid.uuid4())[:8],
            title="Direct Task",
            objective=intention.intended_outcome,
            acceptance_criteria=["completed"],
        )

        # Band A: chat (use Haiku)
        # Band B: read (use Direct)
        if band == Band.A:
            step_kind = "reason"
            mode = Mode.SONNET  # Haiku not in mode hierarchy yet, use Sonnet
        else:  # Band B
            step_kind = "read"
            mode = Mode.DIRECT

        step = StepSpec(
            step_id=f"{task.id}_s0",
            step_kind=step_kind,
            task_class=band.value,
            replacement_mode=mode,
            inputs={"prompt": intention.user_request},
        )

        return task, [step]

    def _log_token_summary(self) -> None:
        """Write token log to file and print summary."""
        log_file = "/tmp/calcifer_token_trace.log"
        with open(log_file, "w") as f:
            f.write("\n".join(self._token_log))
        print(f"\n[TOKEN TRACE] Written to {log_file}")
        for line in self._token_log:
            print(line)

    def _format_response(self, session: TaskSession, status: str) -> str:
        """Format session result for user."""
        if session.last_status:
            return session.last_status.result_summary
        return ""
