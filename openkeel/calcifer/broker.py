#!/usr/bin/env python3
"""Broker: owns task state, orchestrates routing, keeps Opus out of the loop."""

from __future__ import annotations

import json
from openkeel.calcifer.contracts import (
    CompletionDecision,
    IntentionPacket,
    Mode,
    StatusPacket,
    StepSpec,
    Task,
    TaskSession,
)
from openkeel.calcifer.evaluator import Evaluator
from openkeel.calcifer.evidence_store import EvidenceStore
from openkeel.calcifer.executors import DirectRunner, SemanticRunner, SonnetRunner, OpusRunner
from openkeel.calcifer.status_summarizer import StatusSummarizer
from openkeel.calcifer.step_deriver import StepDeriver


class Broker:
    """Owns task session, routes execution, keeps raw output out of Opus's context."""

    def __init__(self):
        self.evaluator = Evaluator(EvidenceStore())
        self.summarizer = StatusSummarizer()
        self.deriver = StepDeriver()
        self.direct = DirectRunner()
        self.semantic = SemanticRunner()
        self.sonnet = SonnetRunner()
        self.opus = OpusRunner()

    def run_task(
        self,
        intention: IntentionPacket,
        task: Task,
        initial_plan: list[StepSpec],
    ) -> TaskSession:
        """Execute a task: plan steps, route execution, manage state."""
        session = TaskSession(
            task=task,
            intention=intention,
            current_plan=initial_plan,
            plan_cursor=0,
        )

        # Main loop: execute steps until done, blocked, or needs Opus
        while True:
            if session.plan_cursor >= len(session.current_plan):
                # Plan exhausted
                break

            step = session.current_plan[session.plan_cursor]

            # Execute the step
            status = self._execute_step(step)
            session.history.append(status)

            # Evaluate against acceptance contract
            self.evaluator.apply(status, step)

            # Decide what's next
            decision = self.deriver.derive(session, status)

            if decision.kind == "done":
                break
            elif decision.kind == "continue":
                session.plan_cursor += 1
            elif decision.kind == "retry":
                # Retry same step, don't advance cursor
                pass
            elif decision.kind == "escalate_runner":
                # Step will be retried with higher mode
                pass
            elif decision.kind == "needs_judge":
                # Needs Opus judgment; return session for broker to ask Opus
                break
            elif decision.kind == "blocked":
                # Can't proceed
                break

        return session

    def _execute_step(self, step: StepSpec) -> StatusPacket:
        """Route a step to the appropriate executor."""
        mode = step.replacement_mode

        if mode == Mode.DIRECT:
            return self.direct.execute(step)
        elif mode == Mode.SEMANTIC:
            return self.semantic.execute(step)
        elif mode == Mode.SONNET:
            return self.sonnet.execute(step)
        elif mode == Mode.OPUS:
            return self.opus.execute(step)
        else:
            # Fallback
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=[],
                artifacts_touched=[],
                result_summary=f"Unknown mode: {mode}",
                acceptance_checks=[("unknown_mode", False, str(mode))],
                needs_escalation=True,
                runner_id="broker",
            )

    def summarize_for_opus(self, session: TaskSession) -> str:
        """Generate a compact summary of session state for Opus to see."""
        lines = [
            "=== TASK STATE ===",
            f"Task: {session.task.title}",
            f"Steps: {session.plan_cursor + 1}/{len(session.current_plan)}",
            f"Opus calls so far: {session.opus_calls}",
        ]

        if session.last_status:
            s = session.last_status
            lines.extend([
                "",
                "=== LAST STEP ===",
                f"Step: {s.step_id}",
                f"Summary: {s.result_summary[:200]}",
                f"Checks: {len([c for c in s.acceptance_checks if c[1]])} passed, {len([c for c in s.acceptance_checks if not c[1]])} failed",
            ])

        lines.append("=== END STATE ===")
        return "\n".join(lines)
