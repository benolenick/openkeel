#!/usr/bin/env python3
"""OpusPlanningAgent: Opus derives the task plan from user intent."""

import subprocess
import json
from openkeel.calcifer.contracts import IntentionPacket, Task, StepSpec, Mode, Check
import uuid


class OpusPlanningAgent:
    """Call Opus to derive initial task + step plan from user intent."""

    PLANNING_SYSTEM = """You are Calcifer's high-judgment planning agent for Band D/E tasks: complex design,
architecture, deep reasoning, and multi-system problems. Your job is to think carefully about
the problem — including hidden dependencies, risks, and trade-offs — then produce an execution
plan that a cheaper model can follow.

You MUST return JSON (and ONLY JSON, no other text) with this structure:
{
  "task_title": "short title",
  "task_objective": "what we're trying to accomplish and why it matters",
  "acceptance_criteria": ["concrete measurable criterion 1", "criterion 2"],
  "risks": ["potential problem 1", "gotcha 2"],
  "steps": [
    {
      "kind": "read" | "grep" | "edit" | "diagnose" | "reason",
      "target": "file or subject",
      "quality_floor": 0.8,
      "checks": ["criterion1", "criterion2"],
      "rationale": "why this step is necessary"
    }
  ]
}

Step kinds:
- "read": read a file and understand it deeply
- "grep": search for patterns or dependencies
- "edit": make a precise, considered code change
- "diagnose": reason about root cause, not just symptoms
- "reason": strategic thinking, trade-off analysis, or planning the next phase

Think before you plan. Surface risks and dependencies. 4-8 steps for complex tasks.
Each step should have a clear rationale. Do not skip steps that guard against known failure modes."""

    def plan(self, intention: IntentionPacket, prior_context: str = "") -> tuple[Task, list[StepSpec]]:
        """Call Opus to derive plan from intention."""
        context_section = f"\nPrior session context (for continuity):\n{prior_context}\n" if prior_context.strip() else ""
        prompt = f"""User intent: {intention.user_request}{context_section}

Intended outcome: {intention.intended_outcome}

Must preserve: {', '.join(intention.must_preserve) if intention.must_preserve else 'nothing special'}

Return JSON plan."""

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", "opus",
                 "--system-prompt", self.PLANNING_SYSTEM],
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
