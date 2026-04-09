#!/usr/bin/env python3
"""
ncms_morning_report.py — Check WS order status and send report.
Runs at 10am ET (after market open) to verify orders filled.
"""
import os, sys, time, logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [report] %(message)s")
log = logging.getLogger("report")

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")

# Twilio
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_TO = os.environ.get("NCMS_ALERT_PHONE", "")


def send_sms(msg):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.info("SMS not configured: %s", msg)
        return
    try:
        from twilio.rest import Client
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=msg, from_=TWILIO_FROM, to=TWILIO_TO)
        log.info("SMS sent")
    except Exception as e:
        log.warning("SMS failed: %s", e)


def check_ws_orders():
    """Screenshot WS account to check order status."""
    try:
        import subprocess
        result = subprocess.run(
            ["python3", "-B", "/mnt/nvme/NCMS/ncms/features/ws_bot.py",
             "status", "--headless"],
            capture_output=True, text=True, timeout=90,
            env={**os.environ, "DISPLAY": ":99"},
        )
        return result.stdout
    except Exception as e:
        return f"WS check failed: {e}"


def generate_report():
    """Generate morning report with all system status."""
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    # Open trades
    open_trades = conn.execute(
        "SELECT * FROM autopilot_trades WHERE status='open'"
    ).fetchall()

    # Recent closed
    closed = conn.execute(
        "SELECT * FROM autopilot_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 5"
    ).fetchall()

    # Lifetime stats
    all_closed = conn.execute(
        "SELECT * FROM autopilot_trades WHERE status='closed'"
    ).fetchall()

    total_pnl = sum(r["pnl"] or 0 for r in all_closed)
    wins = sum(1 for r in all_closed if (r["pnl"] or 0) > 0)
    win_rate = wins / len(all_closed) * 100 if all_closed else 0

    # Last v3 signals
    last_signals = conn.execute(
        "SELECT * FROM strategy_trades WHERE strategy='v3_compound' ORDER BY run_date DESC LIMIT 5"
    ).fetchall()

    # Keep conn open as conn2 for more queries
    conn2 = conn

    # WS account status
    ws_status = check_ws_orders()

    # This week's trades
    week_closed = conn2.execute(
        "SELECT * FROM autopilot_trades WHERE status='closed' AND closed_at >= date('now', '-7 days')"
    ).fetchall()
    week_pnl = sum(r["pnl"] or 0 for r in week_closed)
    week_wins = sum(1 for r in week_closed if (r["pnl"] or 0) > 0)

    # This month's trades
    month_closed = conn2.execute(
        "SELECT * FROM autopilot_trades WHERE status='closed' AND closed_at >= date('now', '-30 days')"
    ).fetchall()
    month_pnl = sum(r["pnl"] or 0 for r in month_closed)

    # Best and worst trades ever
    best_trade = conn2.execute(
        "SELECT symbol, pnl, pnl_pct, run_date FROM autopilot_trades WHERE status='closed' ORDER BY pnl DESC LIMIT 1"
    ).fetchone()
    worst_trade = conn2.execute(
        "SELECT symbol, pnl, pnl_pct, run_date FROM autopilot_trades WHERE status='closed' ORDER BY pnl ASC LIMIT 1"
    ).fetchone()

    # Win streak
    recent_results = conn2.execute(
        "SELECT pnl FROM autopilot_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 20"
    ).fetchall()
    streak = 0
    for r in recent_results:
        if (r["pnl"] or 0) > 0:
            streak += 1
        else:
            break

    # Total invested and ROI
    total_invested = sum(r["trade_amount"] or 0 for r in all_closed)
    roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    # Per-symbol breakdown
    symbol_stats = {}
    for r in all_closed:
        sym = r["symbol"]
        if sym not in symbol_stats:
            symbol_stats[sym] = {"trades": 0, "wins": 0, "pnl": 0}
        symbol_stats[sym]["trades"] += 1
        symbol_stats[sym]["pnl"] += r["pnl"] or 0
        if (r["pnl"] or 0) > 0:
            symbol_stats[sym]["wins"] += 1

    conn2.close()

    # Build report
    lines = ["NCMS DAILY REPORT", f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    lines.append("")

    # Portfolio summary
    lines.append(f"PORTFOLIO:")
    lines.append(f"  Open: {len(open_trades)} trades")
    lines.append(f"  Lifetime: {len(all_closed)} closed, ${total_pnl:+.2f} P&L")
    lines.append(f"  Win rate: {win_rate:.0f}% | ROI: {roi:+.1f}%")
    lines.append(f"  This week: ${week_pnl:+.2f} ({len(week_closed)} trades)")
    lines.append(f"  This month: ${month_pnl:+.2f} ({len(month_closed)} trades)")
    if streak > 0:
        lines.append(f"  Win streak: {streak}")

    # Open positions
    if open_trades:
        lines.append(f"\nOPEN POSITIONS:")
        for t in open_trades:
            lines.append(f"  {t['direction'].upper()} {t['symbol']} @ ${t['entry_price']:.2f} (day {t['holding_days']}/{10})")

    # Per-symbol performance
    if symbol_stats:
        lines.append(f"\nBY SYMBOL:")
        for sym, s in sorted(symbol_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            lines.append(f"  {sym}: {s['trades']} trades, {wr:.0f}% win, ${s['pnl']:+.2f}")

    # Best/worst
    if best_trade:
        lines.append(f"\nBEST: {best_trade['symbol']} ${best_trade['pnl']:+.2f} ({best_trade['run_date']})")
    if worst_trade and worst_trade["pnl"]:
        lines.append(f"WORST: {worst_trade['symbol']} ${worst_trade['pnl']:+.2f} ({worst_trade['run_date']})")

    # Recent closes
    if closed:
        lines.append(f"\nRECENT:")
        for t in closed[:3]:
            lines.append(f"  {t['symbol']} ${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)")

    # V3 signals
    if last_signals:
        lines.append(f"\nSIGNALS:")
        for s in last_signals[:3]:
            lines.append(f"  {s['run_date']} {s['target_id']} {s['direction']}")

    lines.append(f"\nWS: {ws_status[:100]}")

    report = "\n".join(lines)
    log.info(report)
    send_sms(report[:1500])  # SMS limit
    return report


if __name__ == "__main__":
    generate_report()
