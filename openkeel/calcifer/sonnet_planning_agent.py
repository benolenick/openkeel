#!/usr/bin/env python3
"""SonnetPlanningAgent: Sonnet derives task plan for Band C/standard tasks.

Cheaper alternative to Opus for moderate-complexity tasks that don't need
full architectural thinking. Uses same plan schema as OpusPlanningAgent.
"""

import subprocess
import json
from openkeel.calcifer.contracts import IntentionPacket, Task, StepSpec, Mode
import uuid


class SonnetPlanningAgent:
    """Call Claude Sonnet to derive plan from intention (Band C only)."""

    PLANNING_SYSTEM = """You are a planning agent for standard tasks. Your job is to read the user's
intent and break it into executable steps.

Return ONLY valid JSON with this exact structure:
{
  "task_title": "short title",
  "task_objective": "what we're trying to accomplish",
  "acceptance_criteria": ["criterion 1", "criterion 2"],
  "steps": [
    {
      "kind": "read" | "grep" | "edit" | "diagnose" | "reason",
      "target": "file or subject",
      "quality_floor": 0.7,
      "checks": ["criterion1", "criterion2"]
    }
  ]
}

Step kinds:
- "read": read and understand a file
- "grep": search for patterns
- "edit": make a code change
- "diagnose": reason about a problem
- "reason": plan next phase or answer question

Be specific. Break complex tasks into 3-5 steps."""

    def plan(self, intention: IntentionPacket) -> tuple[Task, list[StepSpec]]:
        """Call Sonnet to derive plan from intention."""
        prompt = f"""User intent: {intention.goal_id}

Intended outcome: {intention.intended_outcome}

Must preserve: {', '.join(intention.must_preserve) if intention.must_preserve else 'nothing special'}

Return JSON plan."""

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", "sonnet"],
                capture_output=True,
                text=True,
                timeout=60,  # Shorter timeout than Opus
            )

            if result.returncode != 0:
                return self._fallback_plan(intention)

            response = result.stdout.strip()
            plan_json = json.loads(response)

            # Build Task
            task = Task(
                id=str(uuid.uuid4())[:8],
                title=plan_json.get("task_title", "Task"),
                objective=plan_json.get("task_objective", intention.intended_outcome),
                acceptance_criteria=plan_json.get("acceptance_criteria", []),
            )

            # Build StepSpecs
            steps = []
            for i, step_def in enumerate(plan_json.get("steps", [])):
                kind = step_def.get("kind", "reason")

                # Route based on step kind
                if kind in ("read", "grep"):
                    mode = Mode.DIRECT
                elif kind == "edit":
                    mode = Mode.SONNET
                else:  # diagnose, reason
                    mode = Mode.SONNET

                step = StepSpec(
                    step_id=f"{task.id}_s{i}",
                    step_kind=kind,
                    task_class="standard",
                    quality_floor=step_def.get("quality_floor", 0.7),
                    replacement_mode=mode,
                    inputs={
                        "target": step_def.get("target", ""),
                        "prompt": step_def.get("target", ""),
                    },
                )
                steps.append(step)

            return task, steps

        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as e:
            print(f"Sonnet planning failed: {e}. Using fallback.")
            return self._fallback_plan(intention)

    def _fallback_plan(self, intention: IntentionPacket) -> tuple[Task, list[StepSpec]]:
        """Fallback plan if Sonnet fails."""
        task = Task(
            id=str(uuid.uuid4())[:8],
            title="Task",
            objective=intention.intended_outcome,
            acceptance_criteria=["completed"],
        )

        step = StepSpec(
            step_id=f"{task.id}_s0",
            step_kind="reason",
            task_class="reasoning",
            replacement_mode=Mode.SONNET,  # Use Sonnet for fallback reasoning
            inputs={"prompt": intention.user_request},
        )

        return task, [step]
