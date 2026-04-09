#!/usr/bin/env python3
"""StepDeriver: decide the next step or escalation action."""

from __future__ import annotations

from openkeel.calcifer.contracts import (
    CompletionDecision,
    EscalationPolicy,
    Mode,
    StepSpec,
    StatusPacket,
    Task,
    TaskSession,
)


class StepDeriver:
    """Pure logic: given (task, session, last_status), decide what comes next."""

    def derive(self, session: TaskSession, last_status: StatusPacket) -> CompletionDecision:
        """Return the next decision: continue, retry, escalate, done, etc."""

        # Check acceptance
        checks = last_status.acceptance_checks
        hard_checks = [c for c in checks if c[0].startswith("hard:")]
        hard_passed = all(c[1] for c in hard_checks)

        if hard_passed and not last_status.needs_escalation:
            # This step is done
            return self._decide_next_step(session)

        # Step failed. Decide retry vs escalate vs block
        if last_status.needs_escalation:
            return self._decide_escalation(session, last_status)

        # Soft checks failed but no hard escalation flag
        # Try to retry at same mode
        step = session.current_plan[session.plan_cursor]
        policy = step.escalation_policy
        if policy.retries_remaining > 0:
            policy.retries_remaining -= 1
            return CompletionDecision(
                kind="retry",
                step=step,
                note=f"soft check failed, retrying ({policy.retries_remaining} left)",
            )

        # Out of retries, escalate
        return self._decide_escalation(session, last_status)

    def _decide_next_step(
        self, session: TaskSession
    ) -> CompletionDecision:
        """Current step passed. Move to next or declare done."""
        session.plan_cursor += 1

        if session.plan_cursor >= len(session.current_plan):
            # Plan is exhausted
            return CompletionDecision(
                kind="done",
                note="all planned steps completed",
            )

        # Derive next step
        next_step = session.current_plan[session.plan_cursor]
        return CompletionDecision(kind="continue", step=next_step)

    def _decide_escalation(
        self, session: TaskSession, status: StatusPacket
    ) -> CompletionDecision:
        """Decide how to escalate: retry at higher mode, request raw evidence, or block."""
        step = session.current_plan[session.plan_cursor]
        policy = step.escalation_policy

        # Check if we can escalate the runner
        current_mode = getattr(step, "replacement_mode", Mode.DIRECT)
        if current_mode.value < policy.max_mode.value:
            # Escalate to next mode (by numeric value, not direct comparison)
            next_mode_value = min(current_mode.value + 1, policy.max_mode.value)
            next_mode = Mode(next_mode_value)
            step.replacement_mode = next_mode
            step.escalation_policy.retries_remaining = 1
            return CompletionDecision(
                kind="escalate_runner",
                step=step,
                note=f"escalate {current_mode.name} → {next_mode.name}",
            )

        # Can't escalate further. Need judgment.
        if status.raw_evidence_refs:
            return CompletionDecision(
                kind="request_raw_evidence",
                note="step failed, evidence available for Opus review",
            )

        # Blocked: can't proceed without human or Opus judgment
        return CompletionDecision(
            kind="blocked",
            note="escalation exhausted, needs Opus judgment",
        )
