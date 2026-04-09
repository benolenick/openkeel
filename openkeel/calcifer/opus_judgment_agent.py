#!/usr/bin/env python3
"""OpusJudgmentAgent: Opus decides what to do when a step is blocked or uncertain."""

import subprocess
import json
from openkeel.calcifer.contracts import CompletionDecision, TaskSession, StatusPacket


class OpusJudgmentAgent:
    """Call Opus to make judgment calls when steps fail or block."""

    JUDGMENT_SYSTEM = """You are Calcifer's judgment agent. A step has failed or blocked.
Review the evidence and decide what to do next.

Return JSON (ONLY JSON):
{
  "decision": "continue" | "retry_higher_mode" | "request_raw_evidence" | "ask_user" | "blocked",
  "reasoning": "why you chose this",
  "next_prompt": "if retrying, what to try next"
}"""

    def judge(self, session: TaskSession, status: StatusPacket) -> CompletionDecision:
        """Call Opus to judge a blocked step."""
        session_summary = self._summarize_session(session, status)

        prompt = f"""Task: {session.task.objective}

Session summary:
{session_summary}

Last step: {status.step_id}
Checks: {status.acceptance_checks}
Uncertainties: {', '.join(status.uncertainties) if status.uncertainties else 'none'}

What should we do next?"""

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", "opus"],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                return CompletionDecision(
                    kind="blocked",
                    note="Opus judgment failed; blocking for manual review",
                )

            response = result.stdout.strip()
            judgment = json.loads(response)

            kind = judgment.get("decision", "blocked")
            reasoning = judgment.get("reasoning", "")

            return CompletionDecision(
                kind=kind,
                note=f"{reasoning}",
            )

        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as e:
            return CompletionDecision(
                kind="blocked",
                note=f"Judgment failed ({e}); needs manual review",
            )

    def _summarize_session(self, session: TaskSession, current_status: StatusPacket) -> str:
        """Summarize the session for Opus to review."""
        lines = [
            f"Steps completed: {session.plan_cursor}/{len(session.current_plan)}",
            f"Current step: {current_status.step_id}",
        ]

        # Show recent statuses (last 3)
        for status in session.history[-3:]:
            lines.append(f"  - {status.step_id}: {status.result_summary[:100]}")

        return "\n".join(lines)
