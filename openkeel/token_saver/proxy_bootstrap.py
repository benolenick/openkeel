#!/usr/bin/env python3
"""Self-healing bootstrap for the Claude token-saver proxy.

Goals:
- keep Claude's local front door on 127.0.0.1:8787 available
- repair the proxy quickly when it is down
- fail safe: prefer restoring service automatically over leaving Claude dead
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HEALTH_URL = os.environ.get("TOKEN_SAVER_PROXY_HEALTH", "http://127.0.0.1:8787/health")
SERVICE_NAME = os.environ.get("TOKEN_SAVER_PROXY_SERVICE", "token-saver-proxy.service")
PROXY_PORT = int(os.environ.get("TOKEN_SAVER_PROXY_PORT", "8787"))
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROXY_SCRIPT = PROJECT_ROOT / "tools" / "token_saver_proxy.py"
LOG_PATH = Path.home() / ".openkeel" / "proxy_bootstrap.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _log(event: str, **fields) -> None:
    record = {
        "ts": time.time(),
        "event": event,
        **fields,
    }
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def proxy_healthy(timeout: float = 1.5) -> bool:
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception:
        return False


def port_is_listening(port: int = PROXY_PORT) -> bool:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


def _kill_port_occupants(port: int = PROXY_PORT) -> None:
    try:
        result = _run(["fuser", "-n", "tcp", str(port)])
        pids = [p for p in result.stdout.split() if p.isdigit()]
    except Exception:
        pids = []

    for pid_str in pids:
        try:
            os.kill(int(pid_str), signal.SIGKILL)
            _log("killed_port_occupant", pid=int(pid_str), port=port)
        except Exception:
            pass


def start_service() -> bool:
    daemon_reload = _systemctl("daemon-reload")
    _log("systemctl_daemon_reload", rc=daemon_reload.returncode)
    start = _systemctl("start", SERVICE_NAME)
    _log("systemctl_start", rc=start.returncode, stderr=start.stderr[-300:])
    return start.returncode == 0


def restart_service() -> bool:
    restart = _systemctl("restart", SERVICE_NAME)
    _log("systemctl_restart", rc=restart.returncode, stderr=restart.stderr[-300:])
    return restart.returncode == 0


def spawn_fallback() -> bool:
    try:
        subprocess.Popen(
            [sys.executable, str(PROXY_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log("spawn_fallback", script=str(PROXY_SCRIPT))
        return True
    except Exception as e:
        _log("spawn_fallback_failed", error=str(e))
        return False


def wait_for_health(deadline_s: float = 10.0, interval_s: float = 0.5) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proxy_healthy():
            return True
        time.sleep(interval_s)
    return proxy_healthy()


def ensure_proxy() -> bool:
    if proxy_healthy():
        return True

    _log("ensure_begin", listening=port_is_listening())

    # First try the systemd-managed path.
    start_service()
    if wait_for_health(6.0):
        _log("ensure_recovered", strategy="systemd_start")
        return True

    restart_service()
    if wait_for_health(6.0):
        _log("ensure_recovered", strategy="systemd_restart")
        return True

    # If the port is wedged by a stale process, clear it and retry.
    if port_is_listening() and not proxy_healthy():
        _kill_port_occupants()
        time.sleep(0.5)
        start_service()
        if wait_for_health(6.0):
            _log("ensure_recovered", strategy="kill_stale_then_start")
            return True

    # Final fallback: spawn the proxy directly so Claude can keep working.
    spawn_fallback()
    if wait_for_health(6.0):
        _log("ensure_recovered", strategy="direct_spawn")
        return True

    _log("ensure_failed")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-heal the token-saver proxy.")
    parser.add_argument("command", choices=["ensure", "health"])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.command == "health":
        ok = proxy_healthy()
        if not args.quiet:
            print("ok" if ok else "down")
        return 0 if ok else 1

    ok = ensure_proxy()
    if not args.quiet:
        print("proxy_ok" if ok else "proxy_down")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
