"""Critic Agent — reviews Operator output for quality, correctness, and regressions.

The Critic is the quality gate of the three-agent system. It:
1. Gets triggered by the Director after the Operator completes a step
2. Reviews the actual artifacts produced (code, test output, logs)
3. Runs validation (tests, linting, game replays, etc.)
4. Reports a verdict: approve, reject with reasons, or suggest improvements
5. Director uses the verdict to advance or send Operator back

The Critic never executes the main work — it only reviews.
It's adversarial by design: its job is to find flaws.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("openkeel.critic")

BOARD_URL = "http://127.0.0.1:8200"
HYPHAE_URL = "http://127.0.0.1:8100"
CRITIC_NAME = "critic"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Verdict(Enum):
    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_WORK = "needs_work"


@dataclass
class Review:
    verdict: Verdict
    summary: str
    flaws: list[str]
    suggestions: list[str]
    tests_passed: Optional[bool] = None
    confidence: float = 0.0


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


def register_agent(name: str = CRITIC_NAME) -> bool:
    result = _api("POST", "/api/agent/register", {
        "name": name,
        "capabilities": "code review, test execution, quality analysis",
        "model": "claude-sonnet",
    })
    return bool(result and result.get("ok"))


def heartbeat(name: str = CRITIC_NAME, status: str = "idle",
              commentary: str = "") -> dict | None:
    data: dict = {"status": status}
    if commentary:
        data["commentary"] = commentary
    result = _api("POST", f"/api/agent/{name}/heartbeat", data)
    if result is None:
        # Re-register on 404
        register_agent(name)
        result = _api("POST", f"/api/agent/{name}/heartbeat", data)
    return result


def post_commentary(name: str, text: str) -> bool:
    result = _api("POST", f"/api/agent/{name}/commentary", {"text": text})
    return bool(result and result.get("ok"))


def _hyphae_recall(query: str, top_k: int = 3) -> list[str]:
    try:
        body = json.dumps({"query": query, "top_k": top_k}).encode()
        req = urllib.request.Request(
            f"{BOARD_URL.replace('8200','8100')}/recall", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return [r["text"][:200] for r in data.get("results", [])[:top_k]]
    except Exception:
        return []


def _hyphae_remember(fact: str) -> bool:
    try:
        body = json.dumps({"text": fact, "source": "critic"}).encode()
        req = urllib.request.Request(
            f"{BOARD_URL.replace('8200','8100')}/remember", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Claude review (pipe mode)
# ---------------------------------------------------------------------------

def _ask_claude(system: str, prompt: str, model: str = "haiku") -> str:
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
# Review logic
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = """You are a Critic agent reviewing work produced by an Operator agent.
Your job is to catch real problems — bugs, regressions, wrong logic. Not to nitpick.

You will receive:
- The original directive (what was asked)
- The Operator's report (what they say they did)
- The actual code diff or output (what actually changed)

Respond with ONLY a JSON object:
{
  "verdict": "approve" | "reject" | "needs_work",
  "confidence": 0.0-1.0,
  "summary": "one sentence overall assessment",
  "flaws": ["list of specific problems found"],
  "suggestions": ["list of concrete improvements"],
  "tests_should_run": ["list of test commands to verify, if any"]
}

IMPORTANT GUIDELINES:
- APPROVE read/analysis steps if the Operator found useful information, even with no code diff. Not every step changes code.
- APPROVE code changes if they address the directive and compile, even if imperfect. Progress > perfection.
- "needs_work" = has a specific bug or missed the directive. Give ONE concrete fix.
- "reject" = ONLY for completely wrong work (changed the wrong file, introduced a crash, did the opposite of what was asked)
- Do NOT reject because the test is an import check — that's the Critic test, not the game test. The Director runs the real game test separately.
- Do NOT reject because there's no git diff on a read/analysis step.
- Keep flaws list SHORT (max 3). Focus on what matters most.
- The goal is forward progress. A flawed fix that can be improved next cycle is better than no fix at all.
"""

VALIDATION_SYSTEM = """You are a Critic agent analyzing test/validation output.
Given test results or command output, determine if the work passes quality checks.

Respond with ONLY a JSON object:
{
  "passed": true | false,
  "issues": ["list of failures or concerns"],
  "summary": "one sentence"
}"""


class Critic:
    """The Critic agent — reviews Operator output for quality.

    Can be used in two modes:
    1. Standalone loop: runs continuously, picks up review requests via directives
    2. On-demand: Director calls review() directly

    Usage (standalone):
        c = Critic(working_dir="/home/om/sc2-commander")
        c.run()

    Usage (on-demand from Director):
        c = Critic(working_dir="/home/om/sc2-commander")
        review = c.review(directive="...", operator_report="...", diff="...")
    """

    def __init__(self, working_dir: str = "", name: str = CRITIC_NAME,
                 model: str = "sonnet", poll_interval: int = 15,
                 test_commands: list[str] | None = None):
        self.name = name
        self.working_dir = working_dir
        self.model = model
        self.poll_interval = poll_interval
        self.test_commands = test_commands or []
        self._running = False
        self._reviews_done = 0

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("[Critic %s] %s", ts, msg)
        print(f"\033[1;31m[Critic {ts}]\033[0m {msg}")
        try:
            from openkeel.agents.dashboard import broadcast_log
            broadcast_log("critic", msg)
        except Exception:
            pass

    def _commentary(self, msg: str):
        heartbeat(self.name, status="busy", commentary=msg)

    def _get_recent_diff(self) -> str:
        """Get the most recent git changes in the working directory."""
        try:
            # Staged + unstaged changes
            result = subprocess.run(
                ["git", "diff", "HEAD~1", "--stat"],
                capture_output=True, text=True, timeout=10,
                cwd=self.working_dir,
            )
            stat = result.stdout.strip()

            # Full diff (limited)
            result = subprocess.run(
                ["git", "diff", "HEAD~1"],
                capture_output=True, text=True, timeout=10,
                cwd=self.working_dir,
            )
            diff = result.stdout.strip()
            if len(diff) > 5000:
                diff = diff[:5000] + "\n... (truncated)"

            return f"Files changed:\n{stat}\n\nDiff:\n{diff}" if stat else ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    def _get_working_diff(self) -> str:
        """Get uncommitted changes."""
        try:
            result = subprocess.run(
                ["git", "diff"],
                capture_output=True, text=True, timeout=10,
                cwd=self.working_dir,
            )
            diff = result.stdout.strip()
            if not diff:
                result = subprocess.run(
                    ["git", "diff", "--cached"],
                    capture_output=True, text=True, timeout=10,
                    cwd=self.working_dir,
                )
                diff = result.stdout.strip()
            if len(diff) > 5000:
                diff = diff[:5000] + "\n... (truncated)"
            return diff
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    def _run_tests(self) -> tuple[bool, str]:
        """Run configured test commands. Returns (all_passed, output)."""
        if not self.test_commands:
            return True, "(no test commands configured)"

        outputs = []
        all_passed = True

        for cmd in self.test_commands:
            self._log(f"Running test: {cmd}")
            try:
                result = subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, timeout=300,
                    cwd=self.working_dir,
                )
                passed = result.returncode == 0
                if not passed:
                    all_passed = False
                output = result.stdout[-1000:] if len(result.stdout) > 1000 else result.stdout
                if result.stderr:
                    output += f"\nSTDERR: {result.stderr[-500:]}"
                outputs.append(f"$ {cmd}\nExit: {result.returncode}\n{output}")
            except subprocess.TimeoutExpired:
                all_passed = False
                outputs.append(f"$ {cmd}\nTIMEOUT after 5 min")

        return all_passed, "\n---\n".join(outputs)

    def review(self, directive: str, operator_report: str = "",
               diff: str = "") -> Review:
        """Review the Operator's work on a specific directive.

        Args:
            directive: What the Operator was asked to do
            operator_report: What the Operator says it did
            diff: Code diff (if not provided, pulls from git)

        Returns:
            Review with verdict, flaws, and suggestions
        """
        self._log(f"Reviewing: {directive[:80]}...")
        self._commentary(f"Reviewing step: {directive[:60]}...")

        # Get diff if not provided
        if not diff:
            diff = self._get_working_diff() or self._get_recent_diff()

        # Run tests first
        tests_passed, test_output = self._run_tests()

        # Recall past reviews for similar work
        past_reviews = _hyphae_recall(f"critic review {directive[:80]}", top_k=3)

        # Build review prompt
        prompt_parts = [f"DIRECTIVE (what was asked):\n{directive}"]

        if past_reviews:
            prompt_parts.append("PAST REVIEWS FOR SIMILAR WORK (learn from these):\n"
                                + "\n".join(f"  • {r}" for r in past_reviews))

        if operator_report:
            prompt_parts.append(f"OPERATOR REPORT (what they say they did):\n{operator_report}")

        if diff:
            prompt_parts.append(f"ACTUAL CHANGES:\n{diff}")
        else:
            prompt_parts.append("ACTUAL CHANGES: No code diff available.")

        if test_output and test_output != "(no test commands configured)":
            prompt_parts.append(f"TEST RESULTS (passed={tests_passed}):\n{test_output}")

        prompt = "\n\n".join(prompt_parts)

        # Ask Claude for review
        response = _ask_claude(REVIEW_SYSTEM, prompt, model=self.model)

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(text)

            verdict_str = data.get("verdict", "needs_work")
            try:
                verdict = Verdict(verdict_str)
            except ValueError:
                verdict = Verdict.NEEDS_WORK

            review = Review(
                verdict=verdict,
                summary=data.get("summary", ""),
                flaws=data.get("flaws", []),
                suggestions=data.get("suggestions", []),
                tests_passed=tests_passed,
                confidence=data.get("confidence", 0.5),
            )
        except (json.JSONDecodeError, KeyError):
            review = Review(
                verdict=Verdict.NEEDS_WORK,
                summary=f"Could not parse review: {response[:100]}",
                flaws=["Review parsing failed"],
                suggestions=[],
                tests_passed=tests_passed,
                confidence=0.0,
            )

        self._reviews_done += 1

        # Log and report
        icon = {"approve": "✓", "reject": "✗", "needs_work": "~"}[review.verdict.value]
        self._log(f"{icon} Verdict: {review.verdict.value} — {review.summary}")
        if review.flaws:
            for flaw in review.flaws:
                self._log(f"  FLAW: {flaw}")
        if review.suggestions:
            for sug in review.suggestions:
                self._log(f"  SUGGEST: {sug}")

        # Post to board
        flaw_text = "; ".join(review.flaws[:3]) if review.flaws else "none"
        self._commentary(
            f"REVIEW {review.verdict.value.upper()}: {review.summary} "
            f"| Flaws: {flaw_text}"
        )

        # Save review to Hyphae for future reference
        _hyphae_remember(
            f"Critic review ({review.verdict.value}): {directive[:80]} — "
            f"{review.summary} | Flaws: {flaw_text}"
        )

        return review

    def review_to_directive(self, review: Review, original_directive: str) -> str | None:
        """Convert a rejection/needs_work review into a directive for the Operator.

        Returns None if the review is approved.
        """
        if review.verdict == Verdict.APPROVE:
            return None

        parts = [f"⚠️ REVISION REQUIRED (Critic verdict: {review.verdict.value})\n"]
        parts.append(f"Original task: {original_directive}\n")
        parts.append(f"Critic's assessment: {review.summary}\n")

        if review.flaws:
            parts.append("FLAWS TO FIX:")
            for i, flaw in enumerate(review.flaws, 1):
                parts.append(f"  {i}. {flaw}")

        if review.suggestions:
            parts.append("\nSUGGESTIONS:")
            for sug in review.suggestions:
                parts.append(f"  - {sug}")

        if review.tests_passed is False:
            parts.append("\nTESTS ARE FAILING — fix them before reporting done.")

        parts.append("\nFix these issues, then report STEP DONE again.")
        return "\n".join(parts)

    def run(self):
        """Standalone loop — polls for review requests via directives."""
        self._running = True
        register_agent(self.name)
        self._log(f"Critic '{self.name}' online. Working dir: {self.working_dir}")
        self._log(f"Test commands: {self.test_commands or '(none)'}")
        self._log("Waiting for review requests via directives...")

        while self._running:
            result = heartbeat(self.name, status="idle")
            directives = []
            if result and isinstance(result, dict):
                directives = result.get("directives", [])

            for d in directives:
                if not self._running:
                    break
                message = d.get("message", "")
                # Parse review request
                if "REVIEW:" in message.upper() or "CRITIC:" in message.upper():
                    review = self.review(directive=message)
                    # Post verdict back as commentary for Director to see
                    verdict_msg = self.review_to_directive(review, message)
                    if verdict_msg:
                        self._commentary(f"CRITIC VERDICT: {review.verdict.value} — {verdict_msg[:200]}")
                    else:
                        self._commentary(f"CRITIC VERDICT: APPROVED — {review.summary}")

            time.sleep(self.poll_interval)

        self._log(f"Critic shutting down. {self._reviews_done} reviews completed.")

    def stop(self):
        self._running = False
        self._log("Stop requested.")
