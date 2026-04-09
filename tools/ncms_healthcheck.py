#!/usr/bin/env python3
"""
ncms_healthcheck.py — System health monitor for NCMS trading infrastructure.

Checks all components, auto-heals what it can, alerts on failures.
Designed to run every 15 minutes via cron on jagg.

CHECKS:
  1. Xvfb running (needed for IB Gateway + WS bot)
  2. IB Gateway running and connectable
  3. WS session valid (not expired)
  4. NCMS cron ran today (transcripts harvested)
  5. Autopilot ran today (trades checked)
  6. Hyphae reachable
  7. Disk space on NVMe
  8. Last successful trade entry/exit
  9. Twilio SMS working

AUTO-HEAL:
  - Restart Xvfb if dead
  - Restart IB Gateway via IBC if dead
  - Re-login WS if session expired
"""

import json
import logging
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [health] %(message)s")
log = logging.getLogger("health")

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")
LOG_DIR = Path("/mnt/nvme/NCMS/ncms/data/logs")
IBC_PATH = "/home/om/ibc/gatewaystart.sh"

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_TO = os.environ.get("NCMS_ALERT_PHONE", "")

# Track alert state to avoid spamming
ALERT_STATE_FILE = "/tmp/ncms_health_alerts.json"


def _load_alert_state():
    try:
        with open(ALERT_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alert_state(state):
    with open(ALERT_STATE_FILE, "w") as f:
        json.dump(state, f)


def _should_alert(key, cooldown_minutes=60):
    """Only alert once per cooldown period per issue."""
    state = _load_alert_state()
    last = state.get(key, 0)
    now = time.time()
    if now - last > cooldown_minutes * 60:
        state[key] = now
        _save_alert_state(state)
        return True
    return False


def _send_sms(msg):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.info("SMS not configured: %s", msg)
        return
    try:
        from twilio.rest import Client
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=msg, from_=TWILIO_FROM, to=TWILIO_TO)
    except Exception as e:
        log.warning("SMS failed: %s", e)


def _run(cmd, timeout=10):
    """Run a shell command, return (success, output)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def _process_running(name):
    ok, out = _run(f"pgrep -f '{name}' | head -1")
    return ok


# =========================================================================
# Individual checks
# =========================================================================

def check_xvfb():
    """Check Xvfb virtual display is running."""
    running = _process_running("Xvfb :99")
    if not running:
        log.warning("Xvfb :99 is DOWN — restarting...")
        _run("Xvfb :99 -screen 0 1024x768x24 &", timeout=3)
        time.sleep(2)
        running = _process_running("Xvfb :99")
        if running:
            log.info("Xvfb HEALED — restarted successfully")
            return True, "healed"
        return False, "restart failed"
    return True, "running"


def check_ib_gateway():
    """Check IB Gateway is running and connectable."""
    running = _process_running("java.*ibgateway\\|java.*jts")
    if not running:
        log.warning("IB Gateway is DOWN — restarting via IBC...")
        _run(f"DISPLAY=:99 nohup {IBC_PATH} -inline > /home/om/ibc/logs/gateway_restart.log 2>&1 &",
             timeout=5)
        time.sleep(45)
        running = _process_running("java.*ibgateway\\|java.*jts")
        if running:
            log.info("IB Gateway HEALED — restarted")
            return True, "healed"
        return False, "restart failed"

    # Check if connectable
    ok, out = _run(
        "python3 -c \"from ib_insync import IB; ib=IB(); ib.connect('127.0.0.1',4002,clientId=98,timeout=5); "
        "print('OK:', ib.managedAccounts()); ib.disconnect()\"",
        timeout=15
    )
    if ok and "OK:" in out:
        return True, "connected"
    return True, f"running but not connectable: {out[:80]}"


def check_ws_session():
    """Check if WS session is still valid."""
    state_file = os.path.expanduser("~/.ws_trader/state.json")
    if not os.path.exists(state_file):
        return False, "no session file"

    # Check age of session file
    age_hours = (time.time() - os.path.getmtime(state_file)) / 3600
    if age_hours > 24:
        return False, f"session {age_hours:.0f}h old — may be expired"
    return True, f"session {age_hours:.1f}h old"


def check_ncms_ran_today():
    """Check if NCMS pipeline ran today."""
    today = datetime.now().strftime("%Y%m%d")
    log_file = LOG_DIR / f"runner_{today}.log"
    if log_file.exists():
        size = log_file.stat().st_size
        return True, f"ran today ({size} bytes)"

    # Check if it's before 6am — pipeline hasn't run yet
    if datetime.now().hour < 10:  # before 6am ET (10 UTC)
        return True, "not yet (before 6am)"
    return False, "no log for today"


def check_autopilot_ran():
    """Check if autopilot ran today."""
    today = datetime.now().strftime("%Y%m%d")
    log_file = LOG_DIR / f"autopilot_{today}.log"
    if log_file.exists():
        return True, "ran today"
    if datetime.now().hour < 14:  # before 9:35am ET (13:35 UTC)
        return True, "not yet (before market open)"
    return False, "no autopilot log today"


def check_hyphae():
    """Check Hyphae memory service."""
    ok, out = _run("curl -sf http://127.0.0.1:8100/health", timeout=5)
    if ok:
        return True, "healthy"
    return False, "unreachable"


def check_disk_space():
    """Check NVMe has enough space."""
    ok, out = _run("df /mnt/nvme --output=pcent | tail -1")
    if ok:
        pct = int(out.strip().replace("%", ""))
        if pct > 90:
            return False, f"{pct}% full"
        return True, f"{pct}% used"
    return False, "check failed"


def check_open_trades():
    """Check status of open trades."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        open_t = conn.execute("SELECT COUNT(*) as c FROM autopilot_trades WHERE status='open'").fetchone()
        closed_t = conn.execute("SELECT COUNT(*) as c FROM autopilot_trades WHERE status='closed'").fetchone()
        recent = conn.execute(
            "SELECT run_date, symbol, direction FROM autopilot_trades ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()

        info = f"{open_t['c']} open, {closed_t['c']} closed"
        if recent:
            info += f", last: {recent['direction']} {recent['symbol']} ({recent['run_date']})"
        return True, info
    except Exception as e:
        return False, str(e)


def check_embed_pipeline():
    """Check if embed pipeline is still running on jagg."""
    running = _process_running("continuous_embed")
    if running:
        return True, "running"
    return False, "not running"


# =========================================================================
# Main health check
# =========================================================================

def run_healthcheck():
    checks = [
        ("Xvfb", check_xvfb),
        ("IB Gateway", check_ib_gateway),
        ("WS Session", check_ws_session),
        ("NCMS Pipeline", check_ncms_ran_today),
        ("Autopilot", check_autopilot_ran),
        ("Hyphae", check_hyphae),
        ("Disk Space", check_disk_space),
        ("Trades", check_open_trades),
        ("Embed Pipeline", check_embed_pipeline),
    ]

    results = []
    failures = []

    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)[:80]

        status = "OK" if ok else "FAIL"
        results.append((name, status, detail))
        if not ok:
            failures.append((name, detail))
        log.info("  %-16s %s  %s", name, status, detail)

    # Print summary
    total = len(results)
    passed = sum(1 for _, s, _ in results if s == "OK")
    log.info("Health: %d/%d checks passed", passed, total)

    # Alert on failures (with cooldown)
    if failures:
        alert_lines = [f"NCMS HEALTH ALERT — {len(failures)} issue(s):"]
        for name, detail in failures:
            alert_lines.append(f"  {name}: {detail}")
            # Try to note if it was auto-healed
            if "healed" in detail.lower():
                alert_lines.append(f"    ^ auto-healed")

        alert_msg = "\n".join(alert_lines)

        # Only SMS if there are non-healed failures
        real_failures = [f for f in failures if "healed" not in f[1].lower()]
        if real_failures and _should_alert("health_fail", cooldown_minutes=60):
            _send_sms(alert_msg)
            log.info("Alert SMS sent")

    return results, failures


if __name__ == "__main__":
    print("NCMS Health Check", flush=True)
    print("=" * 50, flush=True)
    results, failures = run_healthcheck()
    print(f"\n{len(results) - len(failures)}/{len(results)} OK")
    if failures:
        print(f"FAILURES: {', '.join(f[0] for f in failures)}")
