"""Director Agent — continuous improvement loop with adaptive replanning.

The Director runs an infinite build→test→analyze→improve cycle:
1. Plan a small batch of improvements (3-5 steps)
2. Dispatch each step to the Operator
3. After the batch, run the test harness (game, tests, etc.)
4. Analyze the results — what worked, what broke, what's next
5. Re-plan the next batch based on what was learned
6. Repeat forever until stopped

The Operator gets cumulative context so it never loses track of previous work.
The Critic reviews each step and the test results.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("openkeel.director")

BOARD_URL = "http://127.0.0.1:8200"
HYPHAE_URL = "http://127.0.0.1:8100"
DIRECTOR_NAME = "director"
OPERATOR_NAME = "operator"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class StepStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Step:
    description: str
    status: StepStatus = StepStatus.PENDING
    directive_sent: Optional[str] = None
    result: Optional[str] = None
    operator_output: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3


@dataclass
class CycleResult:
    cycle_num: int
    steps_done: int
    steps_failed: int
    test_output: str = ""
    test_passed: bool = False
    analysis: str = ""
    changes_made: list[str] = field(default_factory=list)


@dataclass
class DirectorState:
    goal: str
    cycle: int = 0
    total_steps_done: int = 0
    total_steps_failed: int = 0
    history: list[str] = field(default_factory=list)  # cumulative context
    cycle_results: list[CycleResult] = field(default_factory=list)
    started_at: Optional[float] = None
    stall_warnings: int = 0
    drift_warnings: int = 0


# ---------------------------------------------------------------------------
# Board API helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, data: dict | None = None) -> dict | None:
    url = f"{BOARD_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("Board API %s %s failed: %s", method, path, e)
        return None


def _hyphae(action: str, data: dict) -> dict | None:
    url = f"{HYPHAE_URL}/{action}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError):
        return None


def register_agent(name: str, capabilities: str = "", model: str = "claude-opus") -> bool:
    result = _api("POST", "/api/agent/register", {
        "name": name, "capabilities": capabilities, "model": model,
    })
    return bool(result and result.get("ok"))


def send_directive(agent: str, message: str, priority: str = "normal",
                   from_who: str = DIRECTOR_NAME) -> bool:
    result = _api("POST", f"/api/agent/{agent}/directive", {
        "message": message, "from": from_who, "priority": priority,
    })
    return bool(result and result.get("ok"))


def heartbeat(name: str, status: str = "busy", commentary: str = "") -> dict | None:
    data: dict = {"status": status}
    if commentary:
        data["commentary"] = commentary
    result = _api("POST", f"/api/agent/{name}/heartbeat", data)
    if result is None:
        # Re-register on 404
        register_agent(name, capabilities="planning, monitoring, dispatching", model="claude-sonnet")
        result = _api("POST", f"/api/agent/{name}/heartbeat", data)
    return result


def get_agents() -> list[dict]:
    result = _api("GET", "/api/agents")
    return result if isinstance(result, list) else []


def get_commentary(agent: str = "", limit: int = 10) -> list[dict]:
    qs = f"?limit={limit}"
    if agent:
        qs += f"&agent={agent}"
    result = _api("GET", f"/api/agent/commentary{qs}")
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Claude reasoning (pipe mode)
# ---------------------------------------------------------------------------

def _ask_claude(system: str, prompt: str, model: str = "sonnet") -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text"],
            input=f"{system}\n\n---\n\n{prompt}",
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning("Claude CLI failed: %s", result.stderr[:200])
        return ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Claude CLI error: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Planning (cycle-aware)
# ---------------------------------------------------------------------------

PLAN_SYSTEM = """You are a Director agent planning the NEXT BATCH of code changes for an Operator agent.
The Operator is a Claude Code session that can read files, write code, and run commands in a project directory.

You are running a continuous improvement loop: code changes → automated test → analyze results → more changes → ...

IMPORTANT ROLE SPLIT:
- The Operator WRITES CODE and READS FILES. It makes the actual changes.
- YOU (the Director) run the test/game AUTOMATICALLY after each batch. Do NOT ask the Operator to run tests or games.
- The Operator should focus on: reading code, understanding it, writing fixes, and verifying imports compile.

Given the goal, what's been done so far, and the latest test results, plan the NEXT 2-4 concrete CODE CHANGES.

Rules:
- Each step must be a specific code change (not "investigate" or "run the game")
- Do NOT include steps like "run the bot" or "test the game" — that happens automatically after your batch
- If previous tests failed, analyze the test output and plan fixes
- If previous tests passed, move to the next improvement
- End each batch with a quick compilation/import check step
- Be adaptive — don't repeat what already worked

Output ONLY a JSON array of step descriptions. No markdown, no explanation.
Example: ["Read ops_attack.py to understand the rally timeout logic", "Fix the rally timeout by overriding _on_phase_timeout to advance instead of fail", "Verify the fix compiles: python -c 'from sc2commander.bot import SC2CommanderBot'"]"""

ANALYZE_SYSTEM = """You are a Director agent analyzing test results from an improvement cycle.
Given what was changed and what happened when tested, determine:
1. What worked
2. What's still broken
3. What should be the focus of the next cycle

Respond with ONLY a JSON object:
{
  "worked": ["list of things that improved"],
  "broken": ["list of things still wrong"],
  "next_focus": "the single most important thing to fix next",
  "assessment": "one paragraph summary of current state",
  "ready_for_production": false
}"""

EVALUATE_SYSTEM = """You are a Director agent evaluating an Operator's progress.
You sent the Operator a directive and are now reviewing their recent activity.

Respond with ONLY a JSON object:
{
  "status": "on_track" | "drifting" | "stalled" | "done" | "failed",
  "confidence": 0.0-1.0,
  "summary": "one sentence",
  "correction": "directive to send if drifting, or null"
}"""


def plan_cycle(goal: str, history: list[str], last_test_output: str = "",
               last_analysis: str = "") -> list[str]:
    """Plan the next batch of steps based on accumulated context."""
    prompt_parts = [f"GOAL: {goal}"]

    if history:
        # Only include last 10 entries to keep context manageable
        recent = history[-10:]
        prompt_parts.append(f"WORK DONE SO FAR:\n" + "\n".join(f"- {h}" for h in recent))

    if last_test_output:
        prompt_parts.append(f"LATEST TEST OUTPUT:\n{last_test_output[-2000:]}")

    if last_analysis:
        prompt_parts.append(f"ANALYSIS OF LAST CYCLE:\n{last_analysis}")

    prompt = "\n\n".join(prompt_parts)
    response = _ask_claude(PLAN_SYSTEM, prompt)

    if not response:
        return [f"Continue working on: {goal}"]

    # Try to extract JSON array from response (may be wrapped in markdown, preamble, etc.)
    import re
    text = response.strip()

    # Strip markdown fences
    text = re.sub(r'```(?:json)?\s*', '', text).strip()

    # Find the JSON array in the response
    bracket_match = re.search(r'\[.*\]', text, re.DOTALL)
    if bracket_match:
        try:
            steps = json.loads(bracket_match.group())
            if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
                return steps
        except json.JSONDecodeError:
            pass

    # Fallback: split by newlines, clean up
    lines = [l.strip().lstrip("0123456789.-) ") for l in response.splitlines()
             if l.strip() and not l.strip().startswith("```") and len(l.strip()) > 10]
    return lines if lines else [goal]


def analyze_cycle(changes: list[str], test_output: str, goal: str) -> dict:
    """Analyze the results of a cycle to inform the next one."""
    prompt = (
        f"GOAL: {goal}\n\n"
        f"CHANGES MADE THIS CYCLE:\n" + "\n".join(f"- {c}" for c in changes) + "\n\n"
        f"TEST RESULTS:\n{test_output[-3000:]}"
    )
    response = _ask_claude(ANALYZE_SYSTEM, prompt, model="sonnet")

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return {
            "worked": [], "broken": ["analysis failed"],
            "next_focus": "retry", "assessment": response[:200],
            "ready_for_production": False,
        }


def evaluate_progress(directive: str, commentary: list[dict]) -> dict:
    if not commentary:
        return {"status": "stalled", "confidence": 0.5,
                "summary": "No activity from Operator", "correction": None}

    recent = "\n".join(
        f"[{c.get('timestamp', '?')}] {c.get('agent', '?')}: {c.get('text', c.get('message', ''))}"
        for c in commentary[-15:]
    )
    prompt = f"Directive sent to Operator:\n{directive}\n\nOperator's recent activity:\n{recent}"
    response = _ask_claude(EVALUATE_SYSTEM, prompt, model="haiku")

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return {"status": "unknown", "confidence": 0.0,
                "summary": response[:100], "correction": None}


# ---------------------------------------------------------------------------
# Director main loop — continuous improvement
# ---------------------------------------------------------------------------

class Director:
    """Continuous improvement Director.

    Runs an infinite loop: plan batch → execute → test → analyze → replan.

    Usage:
        d = Director("Make the SC2 bot win against medium AI",
                     test_command="bash run_visual.sh",
                     working_dir="/home/om/sc2-commander")
        d.run()  # runs forever until stopped
    """

    def __init__(self, goal: str, operator: str = OPERATOR_NAME,
                 poll_interval: int = 30, model: str = "sonnet",
                 working_dir: str = "", use_critic: bool = True,
                 test_commands: list[str] | None = None,
                 test_command: str = "",
                 max_cycles: int = 0):
        self.state = DirectorState(goal=goal)
        self.operator = operator
        self.poll_interval = poll_interval
        self.model = model
        self.working_dir = working_dir
        self.use_critic = use_critic
        self.test_command = test_command
        self.max_cycles = max_cycles  # 0 = infinite
        self._running = False
        self._critic = None
        if use_critic:
            from openkeel.agents.critic import Critic
            self._critic = Critic(
                working_dir=working_dir,
                test_commands=test_commands or [],
            )
        self._tester = None
        if test_command:  # only create Tester if we have a test command (means we run games)
            from openkeel.agents.tester import Tester
            self._tester = Tester(working_dir=working_dir)
            # Register tester on the board
            _api("POST", "/api/agent/register", {
                "name": "tester", "capabilities": "game analysis, behavior review",
                "model": "claude-sonnet",
            })

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("[Director %s] %s", ts, msg)
        print(f"\033[1;36m[Director {ts}]\033[0m {msg}")
        try:
            from openkeel.agents.dashboard import broadcast_log
            broadcast_log("director", msg)
        except Exception:
            pass

    def _commentary(self, msg: str):
        heartbeat(DIRECTOR_NAME, status="busy", commentary=msg)

    def _get_context(self) -> str:
        result = _hyphae("recall", {"query": self.state.goal, "top_k": 5})
        if result and "results" in result:
            return "\n".join(r["text"][:200] for r in result["results"][:5])
        return ""

    def _check_operator_alive(self) -> bool:
        agents = get_agents()
        for a in agents:
            if a.get("name") == self.operator:
                return True
        return False

    def _wait_for_operator(self, timeout: int = 300) -> bool:
        self._log(f"Waiting for Operator '{self.operator}'...")
        start = time.time()
        while time.time() - start < timeout:
            if self._check_operator_alive():
                self._log(f"Operator online.")
                return True
            time.sleep(5)
        self._log(f"Operator never showed up.")
        return False

    def _build_operator_context(self, step_desc: str, step_num: int,
                                total_steps: int) -> str:
        """Build a directive with cumulative context from previous work."""
        parts = [
            f"DIRECTIVE FROM DIRECTOR (cycle {self.state.cycle}, "
            f"step {step_num}/{total_steps}):\n",
            f"{step_desc}\n",
            f"OVERALL GOAL: {self.state.goal}\n",
        ]

        # Inject cumulative history so Operator knows what's been done
        if self.state.history:
            recent = self.state.history[-8:]
            parts.append("PREVIOUS WORK THIS SESSION (for context, don't repeat):")
            for h in recent:
                parts.append(f"  • {h}")
            parts.append("")

        # Inject last test results if available
        if self.state.cycle_results:
            last = self.state.cycle_results[-1]
            if last.analysis:
                parts.append(f"LAST CYCLE ANALYSIS: {last.analysis[:300]}")
                parts.append("")

        parts.append("When done, post commentary saying 'STEP DONE: <brief summary of what you changed>'.")
        parts.append("If blocked, post 'STEP BLOCKED: <what's wrong>'.")
        return "\n".join(parts)

    def _dispatch_step(self, step: Step, step_num: int, total: int) -> bool:
        step.attempts += 1
        directive = self._build_operator_context(step.description, step_num, total)
        step.directive_sent = directive
        step.status = StepStatus.IN_PROGRESS

        ok = send_directive(self.operator, directive, priority="normal")
        if ok:
            self._log(f"→ Step {step_num}/{total}: {step.description[:80]}")
            self._commentary(f"Dispatched step {step_num}/{total}: {step.description[:80]}")
        else:
            self._log(f"Failed to dispatch to {self.operator}")
        return ok

    def _monitor_step(self, step: Step) -> str:
        commentary = get_commentary(agent=self.operator, limit=20)

        for c in reversed(commentary[-5:]):
            text = c.get("text", c.get("message", "")).upper()
            if "STEP DONE" in text:
                # Capture the result summary
                full = c.get("text", c.get("message", ""))
                step.result = full
                return "done"
            if "STEP BLOCKED" in text:
                full = c.get("text", c.get("message", ""))
                step.result = full
                return "failed"

        if step.directive_sent:
            eval_result = evaluate_progress(step.directive_sent, commentary)
            status = eval_result.get("status", "unknown")
            summary = eval_result.get("summary", "")
            correction = eval_result.get("correction")

            self._log(f"Eval: {status} — {summary}")

            if status == "drifting" and correction:
                self.state.drift_warnings += 1
                self._log(f"DRIFT ({self.state.drift_warnings}x) — correcting")
                send_directive(self.operator,
                    f"⚠️ COURSE CORRECTION:\n{correction}\n\n"
                    f"Current step: {step.description}",
                    priority="urgent")
                return "corrected"

            if status == "done":
                return "done"
            if status == "failed":
                return "failed"
            if status == "stalled":
                self.state.stall_warnings += 1
                if self.state.stall_warnings >= 3:
                    self._log("Stalled 3x — nudging")
                    send_directive(self.operator,
                        f"🔔 NUDGE: You seem stuck on:\n{step.description}\n\n"
                        f"Try a different approach or report what's blocking you.",
                        priority="urgent")
                    self.state.stall_warnings = 0
                return "stalled"

            return "working"
        return "unknown"

    def _run_test(self) -> str:
        """Run the test harness and return output."""
        if not self.test_command:
            return "(no test command configured)"

        self._log(f"🧪 Running test: {self.test_command[:60]}...")
        self._commentary(f"Running test harness...")

        try:
            result = subprocess.run(
                ["bash", "-c", self.test_command],
                capture_output=True, text=True, timeout=600,
                cwd=self.working_dir,
            )
            output = result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr[-1000:]}"
            output += f"\nExit code: {result.returncode}"

            passed = result.returncode == 0
            self._log(f"🧪 Test {'PASSED' if passed else 'FAILED'} (exit {result.returncode})")
            return output
        except subprocess.TimeoutExpired:
            self._log("🧪 Test TIMEOUT (10 min)")
            return "TEST TIMEOUT after 10 minutes"
        except Exception as e:
            self._log(f"🧪 Test ERROR: {e}")
            return f"TEST ERROR: {e}"

    def _execute_batch(self, steps: list[Step]) -> list[str]:
        """Execute a batch of steps. Returns list of change summaries."""
        changes = []

        for i, step in enumerate(steps):
            if not self._running:
                break

            step_num = i + 1
            total = len(steps)

            # Dispatch
            if not self._dispatch_step(step, step_num, total):
                if step.attempts >= step.max_attempts:
                    step.status = StepStatus.FAILED
                    self.state.total_steps_failed += 1
                    continue
                time.sleep(5)
                continue

            # Monitor until done
            while self._running:
                time.sleep(self.poll_interval)
                status = self._monitor_step(step)

                if status == "done":
                    # Critic review
                    if self._critic and step.directive_sent:
                        self._log("Critic reviewing...")
                        review = self._critic.review(
                            directive=step.description,
                            operator_report=step.result or "",
                        )
                        if review.verdict.value != "approve":
                            revision = self._critic.review_to_directive(review, step.description)
                            if revision and step.attempts < step.max_attempts:
                                self._log(f"Critic: {review.verdict.value} — revising")
                                step.status = StepStatus.IN_PROGRESS
                                step.attempts += 1
                                send_directive(self.operator, revision, priority="urgent")
                                self._commentary(f"Critic sent revision for step {step_num}")
                                continue
                            else:
                                # Critic rejected and max attempts exhausted — do NOT mark done
                                self._log(f"Critic: {review.verdict.value} — max attempts reached, failing step")
                                step.status = StepStatus.FAILED
                                self.state.total_steps_failed += 1
                                self.state.history.append(
                                    f"[Cycle {self.state.cycle}] CRITIC REJECTED (exhausted): {step.description[:100]}"
                                )
                                break

                    step.status = StepStatus.DONE
                    summary = step.result or step.description
                    # Trim to just the summary part
                    if "STEP DONE:" in summary.upper():
                        idx = summary.upper().index("STEP DONE:")
                        summary = summary[idx:]
                    changes.append(summary[:200])
                    self.state.history.append(f"[Cycle {self.state.cycle}] {summary[:150]}")
                    self.state.total_steps_done += 1
                    self._log(f"✓ Step {step_num}/{total} done")
                    self._commentary(f"Step {step_num}/{total} complete")
                    self.state.stall_warnings = 0
                    break

                elif status == "failed":
                    if step.attempts < step.max_attempts:
                        self._log(f"Step {step_num} failed — retrying")
                        step.status = StepStatus.PENDING
                        break
                    step.status = StepStatus.FAILED
                    self.state.total_steps_failed += 1
                    self.state.history.append(f"[Cycle {self.state.cycle}] FAILED: {step.description[:100]}")
                    self._log(f"✗ Step {step_num}/{total} failed permanently")
                    break

        return changes

    def run(self):
        """Main continuous improvement loop. Runs forever until stopped."""
        self._running = True
        self.state.started_at = time.time()

        register_agent(DIRECTOR_NAME,
                       capabilities="planning, monitoring, dispatching, continuous improvement",
                       model=self.model)
        self._log(f"Director online — CONTINUOUS IMPROVEMENT MODE")
        self._log(f"Goal: {self.state.goal}")
        if self.test_command:
            self._log(f"Test command: {self.test_command[:80]}")
        self._log(f"Will run cycles until stopped (Ctrl+C).")

        if not self._wait_for_operator():
            return False

        last_test_output = ""
        last_analysis = ""

        # ===== THE LOOP =====
        while self._running:
            self.state.cycle += 1
            cycle_num = self.state.cycle

            self._log(f"\n{'='*50}")
            self._log(f"CYCLE {cycle_num} — Planning next improvements...")
            self._log(f"{'='*50}")
            self._commentary(f"=== CYCLE {cycle_num} starting ===")

            # --- PLAN ---
            step_descriptions = plan_cycle(
                self.state.goal,
                self.state.history,
                last_test_output,
                last_analysis,
            )
            steps = [Step(description=desc) for desc in step_descriptions]

            self._log(f"Plan ({len(steps)} steps):")
            for i, s in enumerate(steps, 1):
                self._log(f"  {i}. {s.description[:80]}")

            _hyphae("remember", {
                "text": (f"Director cycle {cycle_num} plan for '{self.state.goal}':\n"
                         + "\n".join(f"{i+1}. {s.description}" for i, s in enumerate(steps))),
                "source": "director",
            })

            # --- EXECUTE ---
            changes = self._execute_batch(steps)

            if not self._running:
                break

            # --- TEST ---
            test_output = self._run_test()
            test_passed = "Exit code: 0" in test_output
            last_test_output = test_output

            # --- TESTER: behavior analysis ---
            tester_report = ""
            if self._tester:
                self._log("Tester analyzing game behavior...")
                report = self._tester.analyze_latest_game()
                if not report.get("error"):
                    tester_report = self._tester.format_for_director(report)
                    self._log(f"Tester: {report.get('result','?')} — {report.get('what_happened','')[:100]}")
                    # Append tester report to test output for the analyzer
                    test_output += f"\n\n{tester_report}"

            # --- ANALYZE ---
            self._log(f"Analyzing cycle {cycle_num} results...")
            analysis = analyze_cycle(changes, test_output, self.state.goal)
            last_analysis = analysis.get("assessment", "")

            cycle_result = CycleResult(
                cycle_num=cycle_num,
                steps_done=sum(1 for s in steps if s.status == StepStatus.DONE),
                steps_failed=sum(1 for s in steps if s.status == StepStatus.FAILED),
                test_output=test_output[-500:],
                test_passed=test_passed,
                analysis=last_analysis,
                changes_made=changes,
            )
            self.state.cycle_results.append(cycle_result)

            # Log analysis
            self._log(f"Analysis: {last_analysis[:200]}")
            if analysis.get("worked"):
                for w in analysis["worked"]:
                    self._log(f"  ✓ {w}")
            if analysis.get("broken"):
                for b in analysis["broken"]:
                    self._log(f"  ✗ {b}")
            self._log(f"Next focus: {analysis.get('next_focus', '?')}")

            self._commentary(
                f"Cycle {cycle_num} done. "
                f"Test {'PASSED' if test_passed else 'FAILED'}. "
                f"Next: {analysis.get('next_focus', '?')[:80]}"
            )

            # Save to Hyphae
            _hyphae("remember", {
                "text": (f"Director cycle {cycle_num} results: "
                         f"test={'PASS' if test_passed else 'FAIL'}. "
                         f"{last_analysis[:200]}"),
                "source": "director",
            })

            # Check if we should stop
            if analysis.get("ready_for_production"):
                self._log("🎉 Analysis says ready for production! Stopping.")
                break

            if self.max_cycles and cycle_num >= self.max_cycles:
                self._log(f"Reached max cycles ({self.max_cycles}). Stopping.")
                break

            # Stuck-in-a-loop detection: if same next_focus 3 cycles in a row, escalate
            next_focus = analysis.get("next_focus", "")
            if len(self.state.cycle_results) >= 3:
                last_3 = self.state.cycle_results[-3:]
                if all(not cr.test_passed for cr in last_3):
                    self._log("⚠️ STUCK: 3 consecutive failing cycles — forcing new approach")
                    self.state.history.append(
                        f"[STUCK] 3 cycles failed on same issue. Previous focus: {next_focus[:80]}. "
                        f"MUST try a completely different approach."
                    )
                    _hyphae("remember", {
                        "text": (f"Director STUCK after 3 failing cycles. Focus was: {next_focus[:100]}. "
                                 f"Need different approach."),
                        "source": "director",
                    })

            # Brief pause between cycles
            self._log(f"Starting next cycle in 10s...")
            time.sleep(10)

        # Final summary
        elapsed = time.time() - self.state.started_at
        summary = (
            f"Director stopped after {self.state.cycle} cycles. "
            f"{self.state.total_steps_done} steps done, "
            f"{self.state.total_steps_failed} failed. "
            f"Elapsed: {elapsed/60:.1f} min."
        )
        self._log(summary)
        self._commentary(summary)
        _hyphae("remember", {"text": f"Director session complete: {summary}", "source": "director"})

        return True

    def stop(self):
        self._running = False
        self._log("Stop requested — finishing current step then exiting.")
