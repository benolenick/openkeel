"""OpenKeel Overwatch — a watcher agent that monitors terminal activity.

Captures text output from the PTY, filters noise, and writes a clean feed
file that a second Claude Code instance watches. No API keys needed — uses
your existing Claude subscription.

The watcher Claude sees only distilled text: commands run and their output.
No source code, no tool calls, no internal reasoning — just decisions and results.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

OVERWATCH_DIR = Path.home() / ".openkeel" / "overwatch"
FEED_FILE = OVERWATCH_DIR / "feed.txt"
ALERTS_FILE = OVERWATCH_DIR / "alerts.txt"
HEARTBEAT_FILE = OVERWATCH_DIR / "heartbeat.txt"
OVERWATCH_LOG = OVERWATCH_DIR / "engine.log"
CLAUDE_MD = OVERWATCH_DIR / "CLAUDE.md"

# Max feed file size before rotation (keep it readable for Claude)
MAX_FEED_LINES = 500

# ---------------------------------------------------------------------------
# Noise filters
# ---------------------------------------------------------------------------

_NOISE_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^PS [A-Z]:\\"),                    # PowerShell prompt
    re.compile(r"^\$\s*$"),                         # bash prompt alone
    re.compile(r"^>>>"),                            # python repl
    re.compile(r"^\[[\d;]*m"),                      # bare ANSI codes
    re.compile(r"^(npm|yarn|pip)\s+(WARN|warn)"),
    re.compile(r"^Collecting\s+"),
    re.compile(r"^Downloading\s+"),
    re.compile(r"^Installing\s+"),
    re.compile(r"^(added|removed)\s+\d+\s+packages"),
    re.compile(r"^\s*\d+\s+packages?\s+"),          # npm summary
    re.compile(r"^Already satisfied"),              # pip
    re.compile(r"^Requirement already"),            # pip
]

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b=|\x1b>|\r"
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# CLAUDE.md for the Overwatch agent
# ---------------------------------------------------------------------------

_OVERWATCH_INSTRUCTIONS = """\
# Overwatch Agent

You are Overwatch — a silent watcher monitoring another AI agent's terminal session.

## Your feed file
Read `{feed_file}` — it contains the live terminal output from the agent you're watching.
Re-read it periodically (every 20-30 seconds) to see new activity.

{goal_section}

## What to watch for
1. **LOOPS** — The agent repeating the same commands or hitting the same errors. This is the #1 thing to catch.
2. **DESTRUCTIVE** — Wrong deletions, force pushes, dropping databases, killing wrong processes.
3. **STALL** — No new output for a long time, or output that shows no progress.
4. **SCOPE** — Actions that seem unrelated to the goal or outside allowed boundaries.
5. **DRIFT** — The agent wandering off-task. Compare what it's doing against the goal above.

## How to alert
Write your alerts to `{alerts_file}` — append, don't overwrite. Format:
```
[TIMESTAMP] [SEVERITY] [CATEGORY] message
```
Severity: INFO, WARNING, CRITICAL
Category: loop, destructive, stall, scope, drift, observation

## First thing to do
When you start, write your process ID to `{heartbeat_file}` so OpenKeel knows you're running:
```
import os; open("{heartbeat_file}", "w").write(str(os.getpid()))
```
Do this ONCE at the start, not repeatedly.

## Rules
- Be concise. One line per alert.
- Only flag things that actually matter. Normal workflow is not an alert.
- If the agent runs the same command 3+ times with the same result, that's a loop — flag it.
- If the agent is doing something unrelated to the stated goal, that's drift — flag it.
- If nothing notable happened, don't write anything. Silence means all clear.
- Re-read the feed file every 20-30 seconds. Don't read it constantly.
- You can also read the feed file from the bottom up to see the most recent activity first.
"""

# Agent launch commands — keyed by agent name
WATCHER_AGENTS = {
    "claude": (
        'claude --dangerously-skip-permissions '
        '-p "You are Overwatch. Read CLAUDE.md for instructions, then start watching the feed file. '
        'Re-read it every 20 seconds and write alerts to alerts.txt when you spot issues."'
    ),
    "codex": (
        'codex '
        '-p "You are Overwatch. Read CLAUDE.md for instructions, then start watching the feed file. '
        'Re-read it every 20 seconds and write alerts to alerts.txt when you spot issues."'
    ),
    "gemini": (
        'gemini '
        '-p "You are Overwatch. Read CLAUDE.md for instructions, then start watching the feed file. '
        'Re-read it every 20 seconds and write alerts to alerts.txt when you spot issues."'
    ),
}

WATCHER_AGENT_NAMES = list(WATCHER_AGENTS.keys())


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class OverwatchAlert:
    severity: str       # "info", "warning", "critical"
    category: str       # "loop", "scope", "destructive", "stall", "observation"
    message: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass
class OverwatchConfig:
    enabled: bool = False
    watcher_agent: str = "claude"  # "claude", "codex", "gemini"
    stall_timeout_sec: float = 120.0
    on_alert: object = None  # callable(OverwatchAlert) -> None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class OverwatchEngine:
    """Writes filtered terminal output to a feed file for a watcher Claude."""

    def __init__(self, config: OverwatchConfig | None = None) -> None:
        self._config = config or OverwatchConfig()
        self._buffer: deque[str] = deque(maxlen=MAX_FEED_LINES)
        self._last_meaningful_time = time.time()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._dirty = False
        self._last_alert_check = 0.0
        self._seen_alert_lines = 0

        # Ensure dirs
        OVERWATCH_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @enabled.setter
    def enabled(self, val: bool) -> None:
        self._config.enabled = val
        if not val and self._running:
            self.stop()
        # Note: when enabling, caller should use start() with context args
        # The setter alone enables feed capture but won't auto-start the thread
        # unless start() has been called

    @property
    def feed_file(self) -> Path:
        return FEED_FILE

    @property
    def alerts_file(self) -> Path:
        return ALERTS_FILE

    def setup_instructions(
        self,
        mission_objective: str = "",
        mission_plan: str = "",
        profile_name: str = "",
        profile_description: str = "",
    ) -> Path:
        """Write the CLAUDE.md that tells the watcher agent what to do.

        Includes the active mission goal and profile scope so the watcher
        knows what the agent is supposed to be doing.
        """
        # Build goal section
        goal_parts = []
        if mission_objective:
            goal_parts.append(f"## Current Goal\n**Objective:** {mission_objective}")
            if mission_plan:
                goal_parts.append(f"\n**Plan:**\n{mission_plan}")
        if profile_name:
            goal_parts.append(f"\n## Active Profile: {profile_name}")
            if profile_description:
                goal_parts.append(profile_description)

        goal_section = "\n".join(goal_parts) if goal_parts else (
            "## Current Goal\nNo mission or profile is set. "
            "Watch for general issues (loops, destructive actions, stalls)."
        )

        content = _OVERWATCH_INSTRUCTIONS.format(
            feed_file=str(FEED_FILE).replace("\\", "/"),
            alerts_file=str(ALERTS_FILE).replace("\\", "/"),
            heartbeat_file=str(HEARTBEAT_FILE).replace("\\", "/"),
            goal_section=goal_section,
        )
        CLAUDE_MD.write_text(content, encoding="utf-8")
        return CLAUDE_MD

    def feed(self, raw_text: str) -> None:
        """Feed raw terminal output into the watcher."""
        if not self._config.enabled:
            return
        clean = strip_ansi(raw_text)
        lines = clean.split("\n")
        with self._lock:
            for line in lines:
                line = line.rstrip()
                if not line:
                    continue
                if any(p.match(line) for p in _NOISE_PATTERNS):
                    continue
                ts = time.strftime("%H:%M:%S")
                self._buffer.append(f"[{ts}] {line}")
                self._last_meaningful_time = time.time()
                self._dirty = True

    def start(
        self,
        mission_objective: str = "",
        mission_plan: str = "",
        profile_name: str = "",
        profile_description: str = "",
    ) -> None:
        if self._running:
            return

        # Clear old feed/alerts/heartbeat
        FEED_FILE.write_text("", encoding="utf-8")
        ALERTS_FILE.write_text("", encoding="utf-8")
        if HEARTBEAT_FILE.exists():
            HEARTBEAT_FILE.unlink()
        self._seen_alert_lines = 0

        # Write instructions for watcher with goal context
        self.setup_instructions(
            mission_objective=mission_objective,
            mission_plan=mission_plan,
            profile_name=profile_name,
            profile_description=profile_description,
        )

        self._running = True
        self._last_meaningful_time = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log(
            f"Overwatch started — mission: {mission_objective[:80] or '(none)'}, "
            f"profile: {profile_name or '(none)'}"
        )

    def stop(self) -> None:
        self._running = False
        self._log("Overwatch stopped")

    def _loop(self) -> None:
        while self._running:
            time.sleep(3)

            # Flush buffer to feed file
            if self._dirty:
                self._flush_feed()

            # Check stall
            now = time.time()
            with self._lock:
                stall = now - self._last_meaningful_time

            if stall > self._config.stall_timeout_sec:
                alert = OverwatchAlert(
                    severity="warning",
                    category="stall",
                    message=f"No terminal output for {int(stall)}s",
                )
                self._emit_alert(alert)
                with self._lock:
                    self._last_meaningful_time = now  # don't spam

            # Poll alerts file for new lines from watcher
            self._poll_alerts()

            # Check watcher heartbeat
            self._check_heartbeat()

    def _flush_feed(self) -> None:
        """Write buffered lines to the feed file."""
        with self._lock:
            lines = list(self._buffer)
            self._dirty = False

        try:
            FEED_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            self._log(f"Feed write error: {exc}")

    def _poll_alerts(self) -> None:
        """Check if the watcher Claude wrote new alerts."""
        if not ALERTS_FILE.exists():
            return
        try:
            all_lines = ALERTS_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return

        new_lines = all_lines[self._seen_alert_lines:]
        self._seen_alert_lines = len(all_lines)

        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            alert = self._parse_alert_line(line)
            if alert:
                self._emit_alert(alert)

    @staticmethod
    def _parse_alert_line(line: str) -> OverwatchAlert | None:
        """Parse an alert written by the watcher Claude."""
        # Expected: [TIMESTAMP] [SEVERITY] [CATEGORY] message
        # But be flexible — any text is an alert
        severity = "info"
        category = "observation"
        message = line

        # Try to extract structured parts
        upper = line.upper()
        if "[CRITICAL]" in upper:
            severity = "critical"
        elif "[WARNING]" in upper:
            severity = "warning"
        elif "[INFO]" in upper:
            severity = "info"

        for cat in ("loop", "destructive", "stall", "scope", "observation"):
            if f"[{cat.upper()}]" in upper or f"[{cat}]" in line.lower():
                category = cat
                break

        # Strip the bracketed prefixes from message
        import re as _re
        message = _re.sub(r"\[[^\]]*\]\s*", "", line).strip()
        if not message:
            message = line

        return OverwatchAlert(
            severity=severity,
            category=category,
            message=message,
        )

    def _check_heartbeat(self) -> None:
        """Log heartbeat status — UI polls watcher_status directly."""
        status = self.watcher_status
        # Just log transitions, don't spam alerts
        prev = getattr(self, "_prev_heartbeat_status", None)
        if status != prev:
            self._prev_heartbeat_status = status
            self._log(f"Watcher heartbeat: {status}")

    @property
    def watcher_status(self) -> str:
        """Return 'alive', 'waiting', or 'dead' based on PID file."""
        if not HEARTBEAT_FILE.exists():
            return "waiting"
        try:
            pid_str = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
            pid = int(pid_str)
            # Check if process is running
            import os
            import signal
            if os.name == "nt":
                # Windows: use tasklist
                import subprocess
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=5,
                )
                if str(pid) in result.stdout:
                    return "alive"
                return "dead"
            else:
                # Unix: signal 0 checks existence
                os.kill(pid, 0)
                return "alive"
        except (ValueError, ProcessLookupError, PermissionError):
            return "dead"
        except Exception:
            return "waiting"

    def _emit_alert(self, alert: OverwatchAlert) -> None:
        self._log(f"[{alert.severity.upper()}] [{alert.category}] {alert.message}")
        if callable(self._config.on_alert):
            try:
                self._config.on_alert(alert)
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(OVERWATCH_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def get_launch_command(self, agent: str | None = None) -> str:
        """Return the command to launch the watcher agent instance."""
        agent = agent or self._config.watcher_agent
        cwd = str(OVERWATCH_DIR).replace("\\", "/")
        agent_cmd = WATCHER_AGENTS.get(agent, WATCHER_AGENTS["claude"])
        return f'cd "{cwd}" && {agent_cmd}'
