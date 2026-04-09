#!/usr/bin/env python3
"""BrokerGUIAdapter: thin layer between ladder_chat and the broker."""

from openkeel.calcifer.broker import Broker
from openkeel.calcifer.contracts import IntentionPacket, TaskSession
from openkeel.calcifer.opus_planning_agent import OpusPlanningAgent
from openkeel.calcifer.opus_judgment_agent import OpusJudgmentAgent
from openkeel.calcifer.intention_broker import get_broker as get_intention_broker


class BrokerGUIAdapter:
    """Orchestrate: plan → execute → judge → respond."""

    def __init__(self):
        self.broker = Broker()
        self.planner = OpusPlanningAgent()
        self.judge = OpusJudgmentAgent()
        self._current_session: dict[str, TaskSession] = {}
        self._token_log = []

    def handle_user_message(self, user_message: str, session_id: str) -> str:
        """Process a user message end-to-end."""
        import uuid
        self._token_log = []

        # Step 1: Create intention from user message
        intention = IntentionPacket(
            goal_id=str(uuid.uuid4())[:8],
            user_request=user_message,
            intended_outcome=user_message[:100],
        )
        self._token_log.append(f"[1] Intention created: goal_id={intention.goal_id}")

        # Step 2: Opus derives plan (OPUS CALL #1)
        self._token_log.append("[2] → OpusPlanningAgent.plan() [OPUS subprocess]")
        task, steps = self.planner.plan(intention)
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
