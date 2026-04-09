#!/usr/bin/env python3
"""OpusPlanningAgent: Opus derives the task plan from user intent."""

import subprocess
import json
from openkeel.calcifer.contracts import IntentionPacket, Task, StepSpec, Mode, Check
import uuid


class OpusPlanningAgent:
    """Call Opus to derive initial task + step plan from user intent."""

    PLANNING_SYSTEM = """You are Calcifer's planning agent. Your ONLY job is to read the user's intent
and break it into executable steps.

You MUST return JSON (and ONLY JSON, no other text) with this structure:
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
- "read": read a file and understand it
- "grep": search for patterns
- "edit": make a code change
- "diagnose": reason about a problem
- "reason": plan the next phase

Be specific. Don't be vague."""

    def plan(self, intention: IntentionPacket) -> tuple[Task, list[StepSpec]]:
        """Call Opus to derive plan from intention."""
        prompt = f"""User intent: {intention.goal_id}

Intended outcome: {intention.intended_outcome}

Must preserve: {', '.join(intention.must_preserve) if intention.must_preserve else 'nothing special'}

Return JSON plan."""

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", "opus"],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                # Fallback: create a simple plan
                return self._fallback_plan(intention)

            # Parse Opus's JSON response
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
                    task_class="unknown",
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
            print(f"Planning failed: {e}. Using fallback.")
            return self._fallback_plan(intention)

    def _fallback_plan(self, intention: IntentionPacket) -> tuple[Task, list[StepSpec]]:
        """Fallback plan if Opus fails."""
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
            replacement_mode=Mode.SONNET,
            inputs={"prompt": intention.user_request},
        )

        return task, [step]
