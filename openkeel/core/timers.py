"""Periodic timer system for OpenKeel sessions.

Timers run health checks, status polls, or other periodic commands during
a session.  They can be defined in the profile YAML or created dynamically
by the agent at runtime via the ``OPENKEEL-TIMER:`` output pattern.

The ``TimerManager`` runs as a daemon thread inside ``openkeel run`` and
logs all timer events to the session JSONL log.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openkeel.core.profile import TimerDef

from openkeel.core.audit import log_event

logger = logging.getLogger(__name__)

# Pattern the agent can output to register a dynamic timer
# Example: OPENKEEL-TIMER: name=check_fv interval=60m command="curl -s http://localhost:8000/health" expect="ok" on_fail=warn
_TIMER_PATTERN = re.compile(
    r"OPENKEEL-TIMER:\s+"
    r"name=(\S+)\s+"
    r"interval=(\d+)m\s+"
    r'command="([^"]+)"\s+'
    r'expect="([^"]*)"\s*'
    r"(?:on_fail=(\S+))?\s*"
    r'(?:on_fail_command="([^"]*)")?'
)


@dataclass
class TimerState:
    """Runtime state for a single timer."""
    name: str
    interval_seconds: int
    command: str
    expect: str  # regex
    on_fail: str  # "warn", "block_phase", "run_command"
    on_fail_command: str
    last_run: float = 0.0
    last_ok: bool = True
    fail_count: int = 0


def _timer_from_def(td: TimerDef) -> TimerState:
    """Convert a profile TimerDef to a runtime TimerState."""
    return TimerState(
        name=td.name,
        interval_seconds=td.interval_minutes * 60,
        command=td.command,
        expect=td.expect,
        on_fail=td.on_fail,
        on_fail_command=td.on_fail_command,
    )


def parse_dynamic_timer(line: str) -> TimerState | None:
    """Parse an OPENKEEL-TIMER: line into a TimerState, or None."""
    m = _TIMER_PATTERN.search(line)
    if not m:
        return None
    return TimerState(
        name=m.group(1),
        interval_seconds=int(m.group(2)) * 60,
        command=m.group(3),
        expect=m.group(4),
        on_fail=m.group(5) or "warn",
        on_fail_command=m.group(6) or "",
    )


class TimerManager:
    """Background thread that runs periodic timer checks."""

    def __init__(
        self,
        timers: list[TimerDef],
        log_path: str,
        session_id: str,
        state_dir: str,
    ):
        self._timers: list[TimerState] = [_timer_from_def(t) for t in timers]
        self._log_path = log_path
        self._session_id = session_id
        self._state_dir = state_dir
        self._dynamic_path = os.path.join(state_dir, "timers.json")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the timer thread."""
        if not self._timers and not os.path.exists(self._dynamic_path):
            # No timers defined and no dynamic timers file — skip
            logger.debug("TimerManager: no timers to manage")
            return

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="openkeel-timers")
        self._thread.start()
        logger.info("TimerManager: started with %d profile timers", len(self._timers))

    def stop(self) -> None:
        """Stop the timer thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("TimerManager: stopped")

    def register_timer(self, timer: TimerState) -> None:
        """Register a dynamic timer (thread-safe)."""
        with self._lock:
            # Replace if same name exists
            self._timers = [t for t in self._timers if t.name != timer.name]
            self._timers.append(timer)
            self._save_dynamic_timers()
        logger.info("TimerManager: registered dynamic timer '%s'", timer.name)

    def _save_dynamic_timers(self) -> None:
        """Persist dynamic timers to state/timers.json."""
        dynamic = [
            {
                "name": t.name,
                "interval_seconds": t.interval_seconds,
                "command": t.command,
                "expect": t.expect,
                "on_fail": t.on_fail,
                "on_fail_command": t.on_fail_command,
            }
            for t in self._timers
        ]
        try:
            Path(self._dynamic_path).write_text(
                json.dumps(dynamic, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("TimerManager: failed to save dynamic timers: %s", exc)

    def _load_dynamic_timers(self) -> None:
        """Load dynamic timers from state/timers.json if present."""
        if not os.path.exists(self._dynamic_path):
            return
        try:
            data = json.loads(Path(self._dynamic_path).read_text(encoding="utf-8"))
            for entry in data:
                name = entry.get("name", "")
                # Skip if already registered
                if any(t.name == name for t in self._timers):
                    continue
                self._timers.append(TimerState(
                    name=name,
                    interval_seconds=entry.get("interval_seconds", 3600),
                    command=entry.get("command", ""),
                    expect=entry.get("expect", ""),
                    on_fail=entry.get("on_fail", "warn"),
                    on_fail_command=entry.get("on_fail_command", ""),
                ))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("TimerManager: failed to load dynamic timers: %s", exc)

    def _run_loop(self) -> None:
        """Main loop — check timers every 10 seconds."""
        self._load_dynamic_timers()

        while not self._stop_event.is_set():
            now = time.monotonic()

            with self._lock:
                timers_snapshot = list(self._timers)

            for timer in timers_snapshot:
                if timer.interval_seconds <= 0:
                    continue
                if (now - timer.last_run) < timer.interval_seconds:
                    continue

                timer.last_run = now
                self._check_timer(timer)

            # Reload dynamic timers periodically
            if int(now) % 60 == 0:
                with self._lock:
                    self._load_dynamic_timers()

            self._stop_event.wait(10)

    def _check_timer(self, timer: TimerState) -> None:
        """Run a timer's command and evaluate the result."""
        try:
            proc = subprocess.run(
                timer.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            stdout = proc.stdout.strip()
            ok = True

            if timer.expect:
                ok = bool(re.search(timer.expect, stdout))

            # Log the check
            log_event(
                log_path=self._log_path,
                event_type="timer_check",
                data={
                    "timer": timer.name,
                    "command": timer.command,
                    "ok": ok,
                    "stdout": stdout[:500],
                    "exit_code": proc.returncode,
                },
                session_id=self._session_id,
            )

            if ok:
                timer.last_ok = True
                timer.fail_count = 0
                return

            # Failure
            timer.last_ok = False
            timer.fail_count += 1

            print(
                f"[openkeel] TIMER FAIL: '{timer.name}' — "
                f"expected /{timer.expect}/ in output, got: {stdout[:200]}",
                file=__import__("sys").stderr,
            )

            log_event(
                log_path=self._log_path,
                event_type="timer_fail",
                data={
                    "timer": timer.name,
                    "fail_count": timer.fail_count,
                    "on_fail": timer.on_fail,
                },
                session_id=self._session_id,
            )

            # Handle failure action
            if timer.on_fail == "run_command" and timer.on_fail_command:
                try:
                    result = subprocess.run(
                        timer.on_fail_command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    log_event(
                        log_path=self._log_path,
                        event_type="timer_action",
                        data={
                            "timer": timer.name,
                            "action_command": timer.on_fail_command,
                            "exit_code": result.returncode,
                            "stdout": result.stdout.strip()[:500],
                        },
                        session_id=self._session_id,
                    )
                except Exception as exc:
                    logger.warning("Timer %s: on_fail_command failed: %s", timer.name, exc)

        except subprocess.TimeoutExpired:
            logger.warning("Timer %s: command timed out", timer.name)
            log_event(
                log_path=self._log_path,
                event_type="timer_fail",
                data={"timer": timer.name, "error": "timeout"},
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.warning("Timer %s: unexpected error: %s", timer.name, exc)
