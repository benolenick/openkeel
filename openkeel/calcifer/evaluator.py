#!/usr/bin/env python3
"""Evaluator: checks whether a StatusPacket meets the step's acceptance contract."""

from __future__ import annotations

import re
from openkeel.calcifer.contracts import StepSpec, StatusPacket, Check
from openkeel.calcifer.evidence_store import EvidenceStore


class Evaluator:
    """Runs acceptance checks on a StatusPacket."""

    def __init__(self, evidence: EvidenceStore):
        self.evidence = evidence

    def apply(self, status: StatusPacket, step: StepSpec) -> None:
        """Run all checks in step.acceptance_contract, mutate status.acceptance_checks."""
        status.acceptance_checks = []

        for check in step.acceptance_contract:
            passed, note = self._run_check(check, status, step)
            kind = "hard" if check.kind.startswith("hard") else "soft"
            status.acceptance_checks.append((f"{kind}:{check.kind}", passed, note))

        # If any hard check failed, flag escalation
        hard_failed = any(
            not passed
            for name, passed, _ in status.acceptance_checks
            if name.startswith("hard:")
        )
        if hard_failed:
            status.needs_escalation = True

    def _run_check(self, check: Check, status: StatusPacket, step: StepSpec) -> tuple[bool, str]:
        """Run a single check. Returns (passed, note)."""
        if check.kind == "file_exists":
            import os
            exists = os.path.exists(check.target)
            return exists, f"file {'exists' if exists else 'missing'}"

        if check.kind == "regex_match":
            # Look in result summary for the pattern
            match = re.search(check.expect, status.result_summary, re.IGNORECASE)
            return bool(match), f"pattern {'found' if match else 'not found'}"

        if check.kind == "min_artifacts":
            count = len(status.artifacts_touched)
            passed = count >= check.expect
            return passed, f"{count} artifacts (need {check.expect})"

        if check.kind == "self_report":
            # Trust the runner's needs_escalation flag
            return not status.needs_escalation, "worker reports ready" if not status.needs_escalation else "worker flagged escalation"

        if check.kind == "exit_code":
            # Expect 0 by default
            code = int(check.target) if check.target else 0
            for line in status.result_summary.splitlines():
                if "exit code" in line.lower() or "rc=" in line.lower():
                    try:
                        actual = int(re.search(r'\d+', line).group())
                        return actual == code, f"rc={actual} (expect {code})"
                    except (ValueError, AttributeError):
                        pass
            return False, "exit code not found in summary"

        if check.kind == "llm_rubric":
            # Stub: would call a semantic runner to score
            # For now, default to True
            return True, "rubric check (stubbed)"

        return False, f"unknown check kind: {check.kind}"
