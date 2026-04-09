#!/usr/bin/env python3
"""Governor loop: Opus as the top-level decision maker.

Minimal working version:
- every user turn goes through Opus first
- Opus never gets direct execution tools
- Opus asks for delegation using a plain-text protocol
- delegated work runs via `claude -p` on cheaper models
- delegated results are compressed before they go back up

This is intentionally crude. The goal is to keep Opus as the front desk and
make the auth path work reliably on this machine.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

from openkeel.calcifer.intention_broker import IntentionPacket, get_broker
from openkeel.calcifer.logging_config import get_logger, log_intention_state

logger = get_logger("governor")

# Route through token saver proxy for claude -p calls.
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"

GOVERNOR_SYSTEM = """You are Calcifer's Governor — Ben's reasoning supervisor.

You are always the front desk.
You handle:
- chatting with Ben
- planning
- deciding whether to delegate
- translating delegated results back to Ben

You never directly execute tools yourself.

If you need execution, emit exactly:
DELEGATE_RUNNER: haiku|sonnet
DELEGATE_TASK: <specific task>

If you are done and are replying to Ben directly, do not emit DELEGATE_* lines.

Rules:
- Be concise and direct
- Read the intention landscape before acting
- Record discoveries by writing "discovery: <text>"
- Prefer delegation when execution is needed
"""

SUBAGENT_SUMMARY_SYSTEM = """You summarize execution results for Opus.

Return exactly this shape:

ATTEMPTED: <one short line>
FOUND: <one short line>
STATUS: <done|partial|blocked>
RISK: <one short line>
NEXT: <one short line>

Rules:
- keep each line under 160 characters
- no markdown bullets
- no raw logs
- no code blocks
- no long quotes
- if information is missing, say "unknown"
"""


def _normalize_summary_shape(summary: str, task_spec: str) -> str:
    wanted = ["ATTEMPTED", "FOUND", "STATUS", "RISK", "NEXT"]
    values = {k: "unknown" for k in wanted}
    for line in summary.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().upper()
        if key in values:
            clean = " ".join(value.strip().split())
            values[key] = clean[:160] if clean else "unknown"

    if values["ATTEMPTED"] == "unknown":
        values["ATTEMPTED"] = " ".join(task_spec.split())[:160] or "unknown"

    status = values["STATUS"].lower()
    if status not in {"done", "partial", "blocked"}:
        values["STATUS"] = "partial"

    return "\n".join(f"{k}: {values[k]}" for k in wanted)


def _fallback_summary_shape(task_spec: str, text: str) -> str:
    low = text.lower()
    status = "partial"
    if any(w in low for w in ("error", "failed", "exception", "traceback", "timeout", "blocked")):
        status = "blocked"
    elif any(w in low for w in ("done", "completed", "success", "passed", "fixed")):
        status = "done"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found = lines[0][:160] if lines else "unknown"
    risk = "errors or uncertainty present" if status != "done" else "low"
    next_step = "review blocked condition and retry with stronger runner" if status == "blocked" else (
        "verify completion and continue only if needed" if status == "done" else "continue with the next bounded step"
    )
    attempted = " ".join(task_spec.split())[:160] or "unknown"
    return "\n".join([
        f"ATTEMPTED: {attempted}",
        f"FOUND: {found}",
        f"STATUS: {status}",
        f"RISK: {risk}",
        f"NEXT: {next_step}",
    ])


def _compress_delegate_output(task_spec: str, raw_output: str) -> str:
    text = (raw_output or "").strip()
    if not text:
        return "[EMPTY] delegate produced no output"

    summary_prompt = (
        f"Task spec:\n{task_spec[:1500]}\n\n"
        f"Raw output:\n{text[:12000]}\n\n"
        "Summarize this for Opus in the required format."
    )
    cmd = ["claude", "-p", summary_prompt, "--model", "haiku", "--append-system-prompt", SUBAGENT_SUMMARY_SYSTEM]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode == 0:
            summary = _normalize_summary_shape(result.stdout.strip(), task_spec)
            if summary:
                return summary
    except Exception:
        pass

    return _fallback_summary_shape(task_spec, text)


def _delegate_to_subagent(runner: str, task_spec: str) -> str:
    logger.info(f"Delegating to {runner}: {task_spec[:80]}")

    model = {"haiku": "haiku", "sonnet": "sonnet"}.get(runner)
    if not model:
        return f"[ERROR] unsupported delegate runner: {runner}"

    cmd = ["claude", "-p", task_spec, "--model", model]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return f"[ERROR] sub-agent failed: {result.stderr.strip()[:300]}"
        return _compress_delegate_output(task_spec, result.stdout)
    except subprocess.TimeoutExpired:
        return "[TIMEOUT] sub-agent exceeded 120s"
    except Exception as e:
        return f"[ERROR] {e}"


class GovernorLoop:
    """Opus governor with in-memory conversation state."""

    def __init__(self, conversation_id: str = "default", client=None):
        self.conversation_id = conversation_id
        self.messages: list[dict] = []
        self._packet: Optional[IntentionPacket] = None
        self._broker = get_broker()
        self._turn_count = 0

    def initialize(self, goal: str) -> None:
        self._packet = self._broker.get_or_create(goal)

    def build_intention_briefing(self) -> str:
        if not self._packet:
            return ""

        lines = ["── INTENTION LANDSCAPE ──", f"Goal: {self._packet.intended_outcome}"]
        if self._packet.hypothesis_chain:
            lines.append("Hypotheses:")
            for h in self._packet.hypothesis_chain[-3:]:
                lines.append(f"  v{h.version} ({h.confidence:.0%}): {h.text}")
        if self._packet.attempts:
            lines.append(f"Attempts ({len(self._packet.attempts)} total):")
            for a in self._packet.attempts[-3:]:
                lines.append(f"  • {a['tried']} → {a['result']}")
        if self._packet.stuck_pattern:
            lines.append(f"⚠ STUCK: {self._packet.stuck_pattern}")
        lines.append("── END LANDSCAPE ──")
        return "\n".join(lines)

    def _extract_discoveries(self, response: str) -> None:
        if not self._packet:
            return
        for match in re.finditer(r"discovery:\s*(.+?)(?:\n|$)", response, re.IGNORECASE):
            text = match.group(1).strip()
            if text:
                self._broker.record_discovery(self.conversation_id, text[:200])

    def _build_cli_prompt(self) -> str:
        parts = []
        briefing = self.build_intention_briefing()
        if briefing:
            parts.append(briefing)

        for msg in self.messages[-12:]:
            role = msg.get("role", "user")
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            label = "User" if role == "user" else "Assistant"
            parts.append(f"{label}: {content}")
        parts.append("Assistant:")
        return "\n\n".join(parts)

    def _call_opus(self) -> str:
        prompt = self._build_cli_prompt()
        cmd = [
            "claude", "-p", prompt,
            "--model", "opus",
            "--append-system-prompt", GOVERNOR_SYSTEM,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:400] or "Opus CLI failed")
        return result.stdout.strip()

    def _parse_delegate_request(self, text: str) -> tuple[Optional[str], Optional[str]]:
        runner_match = re.search(r"^\s*DELEGATE_RUNNER:\s*(haiku|sonnet)\s*$", text, re.MULTILINE | re.IGNORECASE)
        task_match = re.search(r"^\s*DELEGATE_TASK:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        if not runner_match or not task_match:
            return None, None
        return runner_match.group(1).lower(), task_match.group(1).strip()

    def _strip_delegate_lines(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            if re.match(r"^\s*DELEGATE_(RUNNER|TASK):", line, re.IGNORECASE):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _handle_delegation_loop(self) -> str:
        while True:
            opus_text = self._call_opus()
            runner, task = self._parse_delegate_request(opus_text)
            visible_text = self._strip_delegate_lines(opus_text)
            self.messages.append({"role": "assistant", "content": visible_text or "[delegating]"})

            if not runner or not task:
                self._extract_discoveries(visible_text)
                return visible_text

            summary = _delegate_to_subagent(runner, task)
            self.messages.append({
                "role": "user",
                "content": f"Delegate result from {runner}:\n{summary}",
            })
            logger.info("Delegation complete, looping back to Opus...")

    def query(self, user_message: str) -> str:
        self._turn_count += 1
        logger.info(f"\n{'='*60}\nTURN {self._turn_count}\nUser: {user_message[:100]}")

        if not self.messages:
            self.initialize(user_message)
            log_intention_state(self._packet)
            logger.info("First turn — governor initialized")

        self.messages.append({"role": "user", "content": user_message})

        if not self._broker.get_shard(self.conversation_id):
            self._broker.start_session(self.conversation_id, self._packet.id)

        response = self._handle_delegation_loop()
        logger.info(f"✓ Opus complete: {len(response)} chars")
        return response

    def close(self) -> None:
        if self._packet:
            self._broker.close_session(
                self.conversation_id,
                f"Ended after {self._turn_count} turns",
                "COMPLETE",
                "",
            )
