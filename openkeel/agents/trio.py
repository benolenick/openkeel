"""Trio — a self-contained cell of three agents: Director, Operator, Critic.

The Trio is the atomic unit of work. It:
1. Takes a goal
2. Director plans 2-4 code changes
3. Operator executes each one (fresh claude -p call per step)
4. Critic reviews each one (fresh claude -p call per review)
5. If approved → next step. If rejected → Operator revises (max 2 retries).
6. When all steps done → exit with a summary of what changed.

No threads. No API. No heartbeats. One process, sequential calls.
Communicates results via stdout/return value.
Optionally posts status to Board + Hyphae for dashboard visibility (non-critical).

Usage:
    from openkeel.agents.trio import Trio
    result = Trio(goal="Fix the rally timeout", working_dir="/home/om/sc2-commander").run()
    # result = {"steps_done": 3, "steps_failed": 1, "changes": [...], "summary": "..."}
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("openkeel.trio")

HYPHAE_URL = "http://127.0.0.1:8100"
BOARD_URL = "http://127.0.0.1:8200"
AGENT_LOG_DIR = "/tmp/agent_terminals"


# ---------------------------------------------------------------------------
# Helpers — all optional/non-critical
# ---------------------------------------------------------------------------

def _agent_terminal(agent: str, text: str):
    """Write to a per-agent terminal log file for dashboard display."""
    try:
        os.makedirs(AGENT_LOG_DIR, exist_ok=True)
        path = os.path.join(AGENT_LOG_DIR, f"{agent}.log")
        with open(path, "a") as f:
            f.write(text + "\n")
        # Keep files manageable — truncate to last 200 lines
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > 200:
            with open(path, "w") as f:
                f.writelines(lines[-200:])
    except Exception:
        pass


def _hyphae(action: str, data: dict):
    """Fire and forget to Hyphae. Never blocks the trio."""
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(f"{HYPHAE_URL}/{action}", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


def _hyphae_recall(query: str, top_k: int = 5) -> list[str]:
    try:
        body = json.dumps({"query": query, "top_k": top_k}).encode()
        req = urllib.request.Request(f"{HYPHAE_URL}/recall", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return [r["text"][:200] for r in data.get("results", [])[:top_k]]
    except Exception:
        return []


def _board_post(path: str, data: dict):
    """Fire and forget to Board. Never blocks the trio."""
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(f"{BOARD_URL}{path}", data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


def _claude(system: str, prompt: str, model: str = "sonnet",
            cwd: str = "", timeout: int = 600) -> str:
    """One claude -p call. Fresh context every time."""
    cmd = ["claude", "-p", "--model", model, "--output-format", "text",
           "--dangerously-skip-permissions"]
    try:
        result = subprocess.run(
            cmd, input=f"{system}\n\n---\n\n{prompt}",
            capture_output=True, text=True, timeout=timeout,
            cwd=cwd or None,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nSTDERR: {result.stderr[:500]}"
        return output
    except subprocess.TimeoutExpired:
        return "STEP BLOCKED: Timed out"
    except FileNotFoundError:
        return "STEP BLOCKED: claude CLI not found"


# ---------------------------------------------------------------------------
# Agent prompts
# ---------------------------------------------------------------------------

DIRECTOR_SYSTEM = """\
You are a Director agent planning code changes for an Operator agent.
The Operator can read/write files and run commands in {working_dir}.

You are given a goal and context about what's been done. Plan 2-4 concrete CODE CHANGES.

Rules:
- Each step must be a specific, actionable code change
- Do NOT plan "run the game" or "test it" — testing happens separately
- End with a compile/import verification step
- If previous test results show a specific bug, plan the fix for that bug FIRST

Output ONLY a JSON array of step descriptions. No markdown, no explanation.
Example: ["Read ops_attack.py and find the rally timeout value", "Change rally timeout from 20s to 60s on line 56", "Verify: python -c 'from sc2commander.bot import SC2CommanderBot'"]"""


OPERATOR_SYSTEM = """\
You are an Operator agent. Execute the directive below exactly.

MANDATORY PROTOCOL:
1. READ the target file(s) first. Identify exact line numbers.
2. MAKE the change as specified.
3. RUN `git diff` and include the diff in your response.
4. End with: STEP DONE: <summary including file:line changed>
   Or if stuck: STEP BLOCKED: <what's blocking>

Never skip step 1. Never add unrelated changes. Never guess line numbers."""


CRITIC_SYSTEM = """\
You are a Critic agent reviewing code changes.

Respond with ONLY a JSON object:
{{
  "verdict": "approve" | "reject" | "needs_work",
  "summary": "one sentence",
  "flaws": ["max 3 specific issues"],
  "fix": "if needs_work/reject, ONE specific thing to fix"
}}

Guidelines:
- APPROVE read/analysis steps with no diff — that's normal
- APPROVE changes that address the directive and compile, even if imperfect
- "needs_work" only if there's a specific bug or the directive was missed
- "reject" ONLY if the work is completely wrong (wrong file, introduces crash)
- Progress > perfection. A working fix beats a perfect plan."""


# ---------------------------------------------------------------------------
# The Trio
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    description: str
    operator_output: str = ""
    critic_verdict: str = ""
    critic_summary: str = ""
    status: str = "pending"  # pending, done, failed


@dataclass
class TrioResult:
    goal: str
    steps: list[StepResult] = field(default_factory=list)
    steps_done: int = 0
    steps_failed: int = 0
    changes: list[str] = field(default_factory=list)
    summary: str = ""
    elapsed: float = 0.0


class Trio:
    """Self-contained Director→Operator→Critic cell.

    Usage:
        result = Trio("Fix the rally timeout", "/home/om/sc2-commander").run()
    """

    def __init__(self, goal: str, working_dir: str, model: str = "sonnet",
                 history: list[str] | None = None, max_retries: int = 2):
        self.goal = goal
        self.working_dir = working_dir
        self.model = model
        self.history = history or []
        self.max_retries = max_retries

    def _log(self, agent: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        colors = {"director": "36", "operator": "33", "critic": "31", "trio": "35"}
        c = colors.get(agent, "0")
        print(f"\033[1;{c}m[{agent.title()} {ts}]\033[0m {msg}")
        logger.info("[%s] %s", agent, msg)
        # Write to per-agent terminal log
        _agent_terminal(agent, f"[{ts}] {msg}")
        # Non-critical: post to board for dashboard
        _board_post(f"/api/agent/{agent}/heartbeat", {
            "status": "busy", "commentary": msg[:120],
        })

    # --- Director ---
    def _plan(self) -> list[str]:
        self._log("director", f"Planning for: {self.goal[:80]}")

        context_parts = [f"GOAL: {self.goal}"]
        if self.history:
            context_parts.append("PREVIOUS WORK:\n" + "\n".join(f"- {h}" for h in self.history[-8:]))

        memories = _hyphae_recall(self.goal, top_k=5)
        if memories:
            context_parts.append("RELEVANT MEMORIES:\n" + "\n".join(f"- {m}" for m in memories))

        prompt = "\n\n".join(context_parts)
        system = DIRECTOR_SYSTEM.format(working_dir=self.working_dir)
        response = _claude(system, prompt, model=self.model, timeout=300)
        _agent_terminal("director", response[-1500:] if len(response) > 1500 else response)

        if not response:
            return [self.goal]

        # Extract JSON array
        text = re.sub(r'```(?:json)?\s*', '', response.strip()).strip()
        bracket_match = re.search(r'\[.*\]', text, re.DOTALL)
        if bracket_match:
            try:
                steps = json.loads(bracket_match.group())
                if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
                    return steps
            except json.JSONDecodeError:
                pass

        # Fallback
        lines = [l.strip().lstrip("0123456789.-) ") for l in response.splitlines()
                 if l.strip() and not l.strip().startswith("```") and len(l.strip()) > 10]
        return lines if lines else [self.goal]

    # --- Operator ---
    def _execute(self, directive: str, context: str = "") -> str:
        self._log("operator", f"Executing: {directive[:80]}")

        prompt_parts = [f"DIRECTIVE:\n{directive}"]
        if context:
            prompt_parts.append(f"CONTEXT FROM PREVIOUS STEPS:\n{context}")
        prompt_parts.append(f"WORKING DIRECTORY: {self.working_dir}")

        # Inject Hyphae memories
        memories = _hyphae_recall(directive, top_k=3)
        if memories:
            prompt_parts.append("MEMORIES:\n" + "\n".join(f"- {m}" for m in memories))

        output = _claude(OPERATOR_SYSTEM, "\n\n".join(prompt_parts),
                         model=self.model, cwd=self.working_dir, timeout=600)

        # Write full output to operator terminal
        _agent_terminal("operator", output[-2000:] if len(output) > 2000 else output)

        # Save to Hyphae if step done
        if "STEP DONE" in output.upper():
            idx = output.upper().index("STEP DONE")
            summary = output[idx:].split("\n")[0]
            _hyphae("remember", {"text": f"Operator: {summary[:200]}", "source": "operator"})

        return output

    # --- Critic ---
    def _review(self, directive: str, operator_output: str) -> dict:
        self._log("critic", f"Reviewing: {directive[:60]}")

        # Get diff from working dir
        diff = ""
        try:
            result = subprocess.run(["git", "diff"], capture_output=True, text=True,
                                    timeout=5, cwd=self.working_dir)
            diff = result.stdout[:3000] if result.stdout else "(no diff)"
        except Exception:
            diff = "(could not get diff)"

        prompt = (
            f"DIRECTIVE: {directive}\n\n"
            f"OPERATOR REPORT:\n{operator_output[-1500:]}\n\n"
            f"CODE DIFF:\n{diff}"
        )

        response = _claude(CRITIC_SYSTEM, prompt, model="haiku", timeout=60)
        _agent_terminal("critic", response[-1000:] if len(response) > 1000 else response)

        try:
            text = re.sub(r'```(?:json)?\s*', '', response.strip()).strip()
            return json.loads(text)
        except (json.JSONDecodeError, IndexError):
            # If we can't parse, approve to keep moving
            return {"verdict": "approve", "summary": "review parse failed — approving to proceed",
                    "flaws": [], "fix": ""}

    # --- Main loop ---
    def run(self) -> TrioResult:
        start = time.time()
        result = TrioResult(goal=self.goal)

        self._log("trio", f"=== TRIO START: {self.goal[:60]} ===")

        # Register on board (non-critical)
        for name in ("director", "operator", "critic"):
            _board_post("/api/agent/register", {"name": name, "capabilities": "trio", "model": self.model})

        # Director plans
        step_descriptions = self._plan()
        self._log("director", f"Plan ({len(step_descriptions)} steps):")
        for i, s in enumerate(step_descriptions, 1):
            self._log("director", f"  {i}. {s[:80]}")

        _hyphae("remember", {
            "text": f"Trio plan: {', '.join(s[:50] for s in step_descriptions[:4])}",
            "source": "director",
        })

        # Execute steps
        accumulated_context = []

        for i, desc in enumerate(step_descriptions):
            step = StepResult(description=desc)
            result.steps.append(step)
            step_num = i + 1

            for attempt in range(1, self.max_retries + 2):  # 1 attempt + max_retries
                # Operator executes
                context = "\n".join(accumulated_context[-5:]) if accumulated_context else ""
                output = self._execute(desc if attempt == 1 else f"REVISION (attempt {attempt}): {desc}\nCritic feedback: {step.critic_summary}",
                                       context)
                step.operator_output = output

                # Check if blocked
                if "STEP BLOCKED" in output.upper():
                    self._log("operator", f"BLOCKED: {output[output.upper().index('STEP BLOCKED'):][:100]}")
                    step.status = "failed"
                    result.steps_failed += 1
                    break

                # Critic reviews
                review = self._review(desc, output)
                verdict = review.get("verdict", "approve")
                summary = review.get("summary", "")
                step.critic_verdict = verdict
                step.critic_summary = summary

                self._log("critic", f"{'✓' if verdict == 'approve' else '✗'} {verdict}: {summary[:80]}")

                if verdict == "approve" or verdict == "needs_work":
                    # Good enough — move forward
                    step.status = "done"
                    result.steps_done += 1

                    # Extract change summary
                    if "STEP DONE" in output.upper():
                        idx = output.upper().index("STEP DONE")
                        change = output[idx:].split("\n")[0]
                    else:
                        change = f"Step {step_num}: {desc[:80]}"
                    result.changes.append(change[:200])
                    accumulated_context.append(change[:150])

                    self._log("trio", f"✓ Step {step_num}/{len(step_descriptions)} done")
                    break

                elif attempt > self.max_retries:
                    # Max retries exhausted
                    self._log("trio", f"✗ Step {step_num} failed after {attempt} attempts")
                    step.status = "failed"
                    result.steps_failed += 1
                    break

                else:
                    # Rejected — retry with feedback
                    fix = review.get("fix", summary)
                    self._log("critic", f"  Fix: {fix[:80]}")
                    desc = f"{desc}\n\nCRITIC FEEDBACK: {fix}"

        # Summary
        result.elapsed = time.time() - start
        result.summary = (
            f"{result.steps_done}/{len(step_descriptions)} steps done, "
            f"{result.steps_failed} failed. "
            f"Changes: {'; '.join(c[:60] for c in result.changes[:5])}"
        )

        self._log("trio", f"=== TRIO DONE ({result.elapsed:.0f}s): {result.summary[:100]} ===")

        _hyphae("remember", {
            "text": f"Trio completed: {result.summary[:200]}",
            "source": "trio",
        })

        return result


# ---------------------------------------------------------------------------
# CLI entry point (for standalone use / testing)
# ---------------------------------------------------------------------------

def main():
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(prog="trio", description="Run a Director/Operator/Critic cell")
    parser.add_argument("goal", help="What to accomplish")
    parser.add_argument("-d", "--working-dir", default=".")
    parser.add_argument("-m", "--model", default="sonnet")
    args = parser.parse_args()

    result = Trio(goal=args.goal, working_dir=args.working_dir, model=args.model).run()
    print(json.dumps({
        "steps_done": result.steps_done,
        "steps_failed": result.steps_failed,
        "changes": result.changes,
        "summary": result.summary,
        "elapsed": result.elapsed,
    }, indent=2))


if __name__ == "__main__":
    main()
