#!/usr/bin/env python3
"""SonnetRunner: Claude Sonnet for bounded work (code fixes, plans, analysis)."""

import subprocess
from openkeel.calcifer.contracts import StepSpec, StatusPacket


class SonnetRunner:
    """Execute via Claude Sonnet (claude -p)."""

    def execute(self, step: StepSpec) -> StatusPacket:
        """Run a step on Sonnet."""
        prompt = step.inputs.get("prompt", "")
        context = step.inputs.get("context", "")

        if context:
            full_prompt = f"{context}\n\n{prompt}"
        else:
            full_prompt = prompt

        try:
            result = subprocess.run(
                ["claude", "-p", full_prompt, "--model", "sonnet"],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                return StatusPacket(
                    step_id=step.step_id,
                    objective=step.task_class,
                    actions_taken=[f"invoked sonnet"],
                    artifacts_touched=[],
                    result_summary=f"Error: {result.stderr[:500]}",
                    acceptance_checks=[("sonnet_responded", False, result.stderr[:100])],
                    needs_escalation=True,
                    runner_id="sonnet",
                    cost_units=0.0,
                )

            response = result.stdout.strip()
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=[f"sonnet completed {len(response)} chars"],
                artifacts_touched=[],
                result_summary=response,
                acceptance_checks=[("sonnet_responded", True, "ok")],
                runner_id="sonnet",
                cost_units=1.5,  # rough estimate
            )

        except subprocess.TimeoutExpired:
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=["sonnet timeout"],
                artifacts_touched=[],
                result_summary="Sonnet timed out (>120s)",
                acceptance_checks=[("sonnet_responded", False, "timeout")],
                needs_escalation=True,
                runner_id="sonnet",
                cost_units=0.0,
            )
        except Exception as e:
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=["sonnet failed"],
                artifacts_touched=[],
                result_summary=f"Error: {e}",
                acceptance_checks=[("sonnet_responded", False, str(e))],
                needs_escalation=True,
                runner_id="sonnet",
                cost_units=0.0,
            )
