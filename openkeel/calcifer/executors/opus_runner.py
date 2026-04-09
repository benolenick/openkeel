#!/usr/bin/env python3
"""OpusRunner: Claude Opus for planning and judgment only."""

import subprocess
from openkeel.calcifer.contracts import StepSpec, StatusPacket


class OpusRunner:
    """Execute via Claude Opus (claude -p) — reserved for planning and judgment."""

    def execute(self, step: StepSpec) -> StatusPacket:
        """Run a step on Opus."""
        prompt = step.inputs.get("prompt", "")
        context = step.inputs.get("context", "")

        if context:
            full_prompt = f"{context}\n\n{prompt}"
        else:
            full_prompt = prompt

        try:
            result = subprocess.run(
                ["claude", "-p", full_prompt, "--model", "opus"],
                capture_output=True,
                text=True,
                timeout=180,
            )

            if result.returncode != 0:
                return StatusPacket(
                    step_id=step.step_id,
                    objective=step.task_class,
                    actions_taken=[f"invoked opus"],
                    artifacts_touched=[],
                    result_summary=f"Error: {result.stderr[:500]}",
                    acceptance_checks=[("opus_responded", False, result.stderr[:100])],
                    needs_escalation=True,
                    runner_id="opus",
                    cost_units=0.0,
                )

            response = result.stdout.strip()
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=[f"opus completed {len(response)} chars"],
                artifacts_touched=[],
                result_summary=response,
                acceptance_checks=[("opus_responded", True, "ok")],
                runner_id="opus",
                cost_units=8.0,  # rough estimate, much higher than sonnet
            )

        except subprocess.TimeoutExpired:
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=["opus timeout"],
                artifacts_touched=[],
                result_summary="Opus timed out (>180s)",
                acceptance_checks=[("opus_responded", False, "timeout")],
                needs_escalation=False,  # Opus timeout is blocking, not escalation
                runner_id="opus",
                cost_units=0.0,
            )
        except Exception as e:
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=["opus failed"],
                artifacts_touched=[],
                result_summary=f"Error: {e}",
                acceptance_checks=[("opus_responded", False, str(e))],
                needs_escalation=False,
                runner_id="opus",
                cost_units=0.0,
            )
