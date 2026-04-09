"""Operator Agent — executes directives from the Director.

The Operator is the hands of the two-agent system. It:
1. Registers with the Command Board
2. Polls for directives from the Director via heartbeat
3. Executes each directive using Claude Code in pipe mode
4. Reports progress via commentary
5. Signals completion so the Director can advance

Runs as a loop: heartbeat → check directives → execute → report → repeat.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("openkeel.operator")

BOARD_URL = "http://127.0.0.1:8200"
OPERATOR_NAME = "operator"


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


def register_agent(name: str, capabilities: str = "", model: str = "claude-opus") -> bool:
    result = _api("POST", "/api/agent/register", {
        "name": name, "capabilities": capabilities, "model": model,
    })
    return bool(result and result.get("ok"))


def heartbeat(name: str, status: str = "idle", commentary: str = "") -> dict | None:
    data: dict = {"status": status}
    if commentary:
        data["commentary"] = commentary
    result = _api("POST", f"/api/agent/{name}/heartbeat", data)
    if result is None:
        # Likely 404 — agent not registered. Re-register and retry.
        register_agent(name, capabilities="code read/write, bash, git", model="claude-sonnet")
        result = _api("POST", f"/api/agent/{name}/heartbeat", data)
    return result


def post_commentary(name: str, text: str) -> bool:
    result = _api("POST", f"/api/agent/{name}/commentary", {"text": text})
    return bool(result and result.get("ok"))


# ---------------------------------------------------------------------------
# Hyphae memory integration
# ---------------------------------------------------------------------------

HYPHAE_URL = "http://127.0.0.1:8100"


def _hyphae_recall(query: str, top_k: int = 5) -> list[str]:
    """Recall relevant facts from Hyphae, scoped to current project."""
    try:
        body = json.dumps({"query": query, "top_k": top_k}).encode()
        req = urllib.request.Request(
            f"{HYPHAE_URL}/recall", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return [r["text"][:200] for r in data.get("results", [])[:top_k]]
    except Exception:
        return []


def _hyphae_remember(fact: str, source: str = "operator") -> bool:
    """Save a fact to Hyphae for future recall."""
    try:
        body = json.dumps({"text": fact, "source": source}).encode()
        req = urllib.request.Request(
            f"{HYPHAE_URL}/remember", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True
    except Exception:
        return False


def _build_memory_context(directive: str) -> str:
    """Recall relevant memories for this directive."""
    memories = _hyphae_recall(directive, top_k=5)
    if not memories:
        return ""
    return (
        "\n\nRELEVANT MEMORIES (from previous sessions/cycles — use if helpful):\n"
        + "\n".join(f"  • {m}" for m in memories)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Claude execution
# ---------------------------------------------------------------------------

def _execute_directive(directive: str, working_dir: str,
                       model: str = "sonnet", allowlist: str = "") -> str:
    """Execute a directive using Claude Code CLI.

    Runs `claude -p` with the directive as input. The Claude session has full
    tool access (read, write, bash, etc.) scoped to the working directory.
    Injects relevant Hyphae memories for continuity across sessions.
    """
    # Recall relevant context from Hyphae
    memory_context = _build_memory_context(directive)

    system = (
        "You are an Operator agent executing a directive from a Director agent.\n"
        "Focus ONLY on the directive below. Do not wander or add extras.\n\n"
        "MANDATORY PROTOCOL — follow in order:\n"
        "1. READ the target file(s) first. Identify the exact line numbers before touching anything.\n"
        "2. MAKE the change exactly as specified — same values, same lines, nothing extra.\n"
        "3. RUN `git diff` after the change and include the full diff output in your response.\n"
        "4. End your response with: STEP DONE: <brief summary including file:line changed>\n"
        "   Or if stuck: STEP BLOCKED: <what's blocking you>\n\n"
        "CRITICAL: Never skip step 1. Never add unrelated changes. Never guess a line number.\n"
        "If the directive says 'Line 167: change X to Y', you must read the file, confirm\n"
        "line 167 currently says X, then change it to Y. If it doesn't match, report STEP BLOCKED."
    )

    cmd = ["claude", "-p", "--model", model, "--output-format", "text",
           "--dangerously-skip-permissions"]

    if allowlist:
        cmd.extend(["--allowedTools", allowlist])

    full_prompt = f"{system}{memory_context}\n\n---\n\nDIRECTIVE:\n{directive}"

    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max per step
            cwd=working_dir,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n\nSTDERR: {result.stderr[:500]}"
        return output
    except subprocess.TimeoutExpired:
        return "STEP BLOCKED: Execution timed out after 10 minutes"
    except FileNotFoundError:
        return "STEP BLOCKED: claude CLI not found"


# ---------------------------------------------------------------------------
# Operator main loop
# ---------------------------------------------------------------------------

class Operator:
    """The Operator agent loop.

    Usage:
        op = Operator(working_dir="/home/om/openkeel")
        op.run()  # blocks, polling for directives
    """

    def __init__(self, working_dir: str = "", name: str = OPERATOR_NAME,
                 poll_interval: int = 10, model: str = "sonnet",
                 allowlist: str = ""):
        self.name = name
        self.working_dir = working_dir or os.getcwd()
        self.poll_interval = poll_interval
        self.model = model
        self.allowlist = allowlist
        self._running = False
        self._directives_executed = 0

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("[Operator %s] %s", ts, msg)
        print(f"\033[1;33m[Operator {ts}]\033[0m {msg}")
        try:
            from openkeel.agents.dashboard import broadcast_log
            broadcast_log("operator", msg)
        except Exception:
            pass

    def _commentary(self, msg: str):
        """Post Operator commentary to the board."""
        heartbeat(self.name, status="busy", commentary=msg)

    def _process_directive(self, directive: dict):
        """Process a single directive from the Director."""
        message = directive.get("message", "")
        priority = directive.get("priority", "normal")
        from_who = directive.get("from", "unknown")

        self._log(f"Received directive from {from_who} (priority={priority})")
        self._log(f"  → {message[:120]}...")

        # Signal we're working
        self._commentary(f"Starting work on directive from {from_who}")
        heartbeat(self.name, status="busy")

        # Execute via Claude
        self._log("Executing...")
        result = _execute_directive(
            message, self.working_dir,
            model=self.model, allowlist=self.allowlist,
        )

        self._directives_executed += 1

        # Parse outcome and post rich context
        result_upper = result.upper()
        if "STEP DONE" in result_upper:
            idx = result_upper.index("STEP DONE")
            summary = result[idx:].split("\n")[0].strip()
            self._log(f"✓ {summary}")
            # Post summary + reasoning tail so Critic/Director can see what happened
            reasoning_tail = result[max(0, idx-500):idx].strip()
            if reasoning_tail:
                self._commentary(f"{summary}\n\nContext: {reasoning_tail[-300:]}")
            else:
                self._commentary(summary)
        elif "STEP BLOCKED" in result_upper:
            idx = result_upper.index("STEP BLOCKED")
            blocker = result[idx:].split("\n")[0].strip()
            self._log(f"✗ {blocker}")
            self._commentary(blocker)
        else:
            # No explicit signal — post the tail so Director can see what happened
            tail = result[-500:] if len(result) > 500 else result
            self._log(f"Completed (no explicit signal)")
            self._commentary(f"STEP DONE: {tail[-200:]}")

        # Remember what we learned (for future cycles/sessions)
        result_upper = result.upper()
        if "STEP DONE" in result_upper:
            idx = result_upper.index("STEP DONE")
            summary = result[idx:].split("\n")[0].strip()
            _hyphae_remember(
                f"Operator completed: {summary[:200]} (directive: {message[:100]})",
                source="operator",
            )

        # Back to idle
        heartbeat(self.name, status="idle")

    def run(self):
        """Main Operator loop. Blocks, polling for directives."""
        self._running = True

        # Register
        register_agent(self.name,
                       capabilities="code read/write, bash, git, file search",
                       model=self.model)
        self._log(f"Operator '{self.name}' online. Working dir: {self.working_dir}")
        self._log(f"Polling every {self.poll_interval}s for directives...")

        idle_beats = 0

        while self._running:
            # Heartbeat — also pulls pending directives
            result = heartbeat(self.name, status="idle")

            directives = []
            if result and isinstance(result, dict):
                directives = result.get("directives", [])

            if directives:
                idle_beats = 0
                for d in directives:
                    if not self._running:
                        break
                    self._process_directive(d)
            else:
                idle_beats += 1
                if idle_beats % 12 == 0:  # every ~2 min
                    self._log(f"Idle... ({self._directives_executed} directives executed so far)")

            time.sleep(self.poll_interval)

        self._log(f"Operator shutting down. Executed {self._directives_executed} directives total.")
        heartbeat(self.name, status="idle", commentary="Operator shutting down")

    def stop(self):
        """Signal the Operator to stop after the current directive."""
        self._running = False
        self._log("Stop requested — will finish current directive then exit.")
