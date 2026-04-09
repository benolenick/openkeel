"""
ncms_autopilot.py — Fully automatic paper trading system for NCMS v3.

Called from runner.py after v3 signals are generated. Handles:
  1. Auto-enter paper trades at current market price
  2. Track open positions with entry date/price
  3. Auto-exit after holding_days (default 5 trading days)
  4. Calculate and log P&L
  5. SMS alerts on entry/exit via Twilio
  6. Weekly summary report

Tables:
  autopilot_trades — all open and closed paper trades
  autopilot_summary — daily/weekly performance snapshots

Usage:
  from features.autopilot import process_signals, check_exits, daily_summary
"""

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("ncms.autopilot")

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")

HOLDING_DAYS = 5  # trading days
DEFAULT_TRADE_AMOUNT = 100.0  # dollars per trade

# IB Gateway config
IB_HOST = "127.0.0.1"
IB_PORT = 4002
IB_CLIENT_ID = 50
IB_ENABLED = True  # set False to disable IB execution (paper-log only)

# Wealthsimple config
WS_ENABLED = True  # enable real WS trading via Playwright
WS_TRADE_AMOUNT = 1.00  # dollars per trade on WS (start small)

# Twilio config (from env)
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_TO = os.environ.get("NCMS_ALERT_PHONE", "")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL,
    trigger_type TEXT,
    reasons TEXT,
    entry_price REAL,
    entry_date TEXT,
    exit_price REAL,
    exit_date TEXT,
    pnl REAL,
    pnl_pct REAL,
    status TEXT DEFAULT 'open',
    trade_amount REAL,
    qty REAL,
    holding_days INTEGER DEFAULT 0,
    created_at TEXT,
    closed_at TEXT,
    UNIQUE(run_date, symbol, direction)
);

CREATE TABLE IF NOT EXISTS autopilot_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date TEXT NOT NULL,
    summary_type TEXT NOT NULL,
    open_trades INTEGER,
    closed_trades INTEGER,
    total_pnl REAL,
    win_rate REAL,
    best_trade TEXT,
    worst_trade TEXT,
    details TEXT,
    created_at TEXT,
    UNIQUE(summary_date, summary_type)
);
"""


def _get_conn(db_path: Path = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection):
    for stmt in _SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
    conn.commit()


def _fetch_price(symbol: str) -> float | None:
    """Fetch current price via yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", symbol, e)
    return None


def _send_sms(message: str):
    """Send SMS via Twilio if configured."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.info("SMS not configured. Message: %s", message)
        return

    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_FROM,
            to=TWILIO_TO,
        )
        log.info("SMS sent: %s", message[:80])
    except Exception as e:
        log.warning("SMS failed: %s", e)


def _store_price(conn: sqlite3.Connection, symbol: str, price: float, date: str):
    """Store price in daily_prices table."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_prices (price_date, symbol, close_price, volume) VALUES (?,?,?,0)",
            (date, symbol, price),
        )
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# IB Gateway connection and execution
# ---------------------------------------------------------------------------

_ib_instance = None


def _get_ib():
    """Get or create IB connection. Returns None if unavailable."""
    global _ib_instance
    if not IB_ENABLED:
        return None

    try:
        from ib_insync import IB
        if _ib_instance and _ib_instance.isConnected():
            return _ib_instance
        ib = IB()
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)
        _ib_instance = ib
        log.info("IB connected: %s", ib.managedAccounts())
        return ib
    except Exception as e:
        log.warning("IB connection failed (will use paper-only): %s", e)
        _ib_instance = None
        return None


def _disconnect_ib():
    """Disconnect IB if connected."""
    global _ib_instance
    if _ib_instance and _ib_instance.isConnected():
        _ib_instance.disconnect()
        _ib_instance = None


def _ib_get_price(ib, symbol: str) -> float | None:
    """Get current price from IB."""
    try:
        from ib_insync import Stock
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(3)
        price = ticker.last
        if price != price or price is None or price <= 0:  # NaN check
            price = ticker.close
        ib.cancelMktData(contract)
        if price and price > 0:
            return float(price)
    except Exception as e:
        log.warning("IB price fetch failed for %s: %s", symbol, e)
    return None


def _ib_place_order(ib, symbol: str, direction: str, dollar_amount: float) -> dict | None:
    """
    Place a market order through IB.
    Returns {qty, fill_price, order_id} or None on failure.
    """
    try:
        from ib_insync import Stock, MarketOrder

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        # Get price for qty calculation
        price = _ib_get_price(ib, symbol)
        if not price:
            log.warning("IB: No price for %s, cannot place order", symbol)
            return None

        qty = int(dollar_amount // price)
        if qty <= 0:
            log.warning("IB: $%.0f too small for %s @ $%.2f", dollar_amount, symbol, price)
            return None

        # Determine order action
        if direction == "bearish":
            action = "SELL"  # short sell
        else:
            action = "BUY"

        order = MarketOrder(action, qty)
        trade = ib.placeOrder(contract, order)
        ib.sleep(5)  # wait for fill

        fill_price = price  # default to market price
        if trade.orderStatus.avgFillPrice > 0:
            fill_price = trade.orderStatus.avgFillPrice

        log.info("IB ORDER: %s %d %s @ $%.2f (order %d, status=%s)",
                 action, qty, symbol, fill_price,
                 trade.order.orderId, trade.orderStatus.status)

        return {
            "qty": qty,
            "fill_price": fill_price,
            "order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
        }

    except Exception as e:
        log.error("IB order failed for %s: %s", symbol, e)
        return None


def _ib_close_position(ib, symbol: str, direction: str, qty: float) -> dict | None:
    """Close an existing position through IB."""
    try:
        from ib_insync import Stock, MarketOrder

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        # Reverse the original direction
        if direction == "bearish":
            action = "BUY"  # buy to cover short
        else:
            action = "SELL"  # sell to close long

        int_qty = int(abs(qty))
        if int_qty <= 0:
            return None

        order = MarketOrder(action, int_qty)
        trade = ib.placeOrder(contract, order)
        ib.sleep(5)

        fill_price = 0
        if trade.orderStatus.avgFillPrice > 0:
            fill_price = trade.orderStatus.avgFillPrice
        else:
            fill_price = _ib_get_price(ib, symbol) or 0

        log.info("IB CLOSE: %s %d %s @ $%.2f (order %d)",
                 action, int_qty, symbol, fill_price, trade.order.orderId)

        return {
            "fill_price": fill_price,
            "order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
        }

    except Exception as e:
        log.error("IB close failed for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# Wealthsimple execution via Playwright bot
# ---------------------------------------------------------------------------

def _ws_place_order(symbol: str, direction: str, amount: float) -> dict | None:
    """Place a trade on Wealthsimple via the Playwright bot."""
    if not WS_ENABLED:
        return None

    try:
        import subprocess
        cmd_action = "buy" if direction != "bearish" else "sell"
        result = subprocess.run(
            ["python3", "-B", "/mnt/nvme/NCMS/ncms/features/ws_bot.py",
             cmd_action, symbol, str(amount), "--headless"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "DISPLAY": ":99"},
        )
        output = result.stdout + result.stderr
        log.info("WS bot output: %s", output[-200:])

        if "SUCCESS" in output:
            log.info("WS ORDER PLACED: %s $%.2f of %s", cmd_action.upper(), amount, symbol)
            return {"status": "success", "amount": amount, "symbol": symbol}
        else:
            log.warning("WS ORDER FAILED: %s", output[-200:])
            return None
    except Exception as e:
        log.error("WS execution failed: %s", e)
        return None


def _ws_close_position(symbol: str, direction: str, amount: float) -> dict | None:
    """Close a WS position (sell what we bought, or buy back what we shorted)."""
    if not WS_ENABLED:
        return None

    try:
        import subprocess
        # Reverse direction
        cmd_action = "sell" if direction != "bearish" else "buy"
        result = subprocess.run(
            ["python3", "-B", "/mnt/nvme/NCMS/ncms/features/ws_bot.py",
             cmd_action, symbol, str(amount), "--headless"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "DISPLAY": ":99"},
        )
        output = result.stdout + result.stderr
        if "SUCCESS" in output:
            log.info("WS CLOSE PLACED: %s $%.2f of %s", cmd_action.upper(), amount, symbol)
            return {"status": "success"}
        else:
            log.warning("WS CLOSE FAILED: %s", output[-200:])
            return None
    except Exception as e:
        log.error("WS close failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def process_signals(
    run_date: str,
    signals: list[dict],
    trade_amount: float = DEFAULT_TRADE_AMOUNT,
    db_path: Path = None,
) -> list[dict]:
    """
    Process v3 signals: enter paper trades for new signals.

    Args:
        run_date: YYYY-MM-DD
        signals: list from evaluate_v3()
        trade_amount: dollars per trade

    Returns:
        list of trade dicts that were entered
    """
    if not signals:
        return []

    conn = _get_conn(db_path)
    _ensure_tables(conn)

    entered = []

    for sig in signals:
        symbol = sig["symbol"]
        direction = sig["direction"]
        confidence = sig.get("confidence", 0.5)
        reasons = ", ".join(sig.get("reasons", []))

        # Check if we already have an open trade for this symbol
        existing = conn.execute(
            "SELECT id FROM autopilot_trades WHERE symbol=? AND status='open'",
            (symbol,),
        ).fetchone()
        if existing:
            log.info("Autopilot: Already have open %s trade. Skipping.", symbol)
            continue

        # Fetch current price
        price = _fetch_price(symbol)
        if price is None:
            # Try from daily_prices table
            row = conn.execute(
                "SELECT close_price FROM daily_prices WHERE symbol=? ORDER BY price_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if row:
                price = row["close_price"]
            else:
                log.warning("Autopilot: No price for %s. Skipping.", symbol)
                continue

        # Calculate quantity
        qty = trade_amount / price

        # Store price
        _store_price(conn, symbol, price, run_date)

        # Enter trade
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            conn.execute(
                """INSERT OR REPLACE INTO autopilot_trades
                   (run_date, symbol, direction, confidence, trigger_type, reasons,
                    entry_price, entry_date, status, trade_amount, qty,
                    holding_days, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_date, symbol, direction, confidence,
                 sig.get("trigger_type", "v3"), reasons,
                 price, run_date, "open", trade_amount, qty,
                 0, now),
            )
            conn.commit()
            log.info("Autopilot ENTER: %s %s @ $%.2f (conf=%.2f, $%.0f)",
                     direction.upper(), symbol, price, confidence, trade_amount)

            trade = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": price,
                "confidence": confidence,
                "trade_amount": trade_amount,
                "reasons": reasons,
                "ib_executed": False,
            }

            # Execute through IB Gateway (paper trading)
            ib = _get_ib()
            if ib:
                ib_result = _ib_place_order(ib, symbol, direction, trade_amount)
                if ib_result:
                    trade["ib_executed"] = True
                    trade["entry_price"] = ib_result["fill_price"] or price
                    trade["ib_order_id"] = ib_result["order_id"]
                    conn.execute(
                        "UPDATE autopilot_trades SET entry_price=? WHERE run_date=? AND symbol=? AND status='open'",
                        (trade["entry_price"], run_date, symbol),
                    )
                    conn.commit()
                    log.info("Autopilot IB FILL: %s %s @ $%.2f (order %d)",
                             direction.upper(), symbol, trade["entry_price"], ib_result["order_id"])
                else:
                    log.warning("Autopilot: IB order failed for %s, paper-only", symbol)

            # Execute through Wealthsimple (real money, small amount)
            trade["ws_executed"] = False
            if WS_ENABLED and direction != "bearish":  # WS doesn't support shorting
                ws_result = _ws_place_order(symbol, direction, WS_TRADE_AMOUNT)
                if ws_result:
                    trade["ws_executed"] = True
                    log.info("Autopilot WS: %s $%.2f of %s", direction.upper(), WS_TRADE_AMOUNT, symbol)
                else:
                    log.warning("Autopilot: WS order failed for %s", symbol)

            entered.append(trade)

        except Exception as e:
            log.error("Autopilot: Failed to enter %s: %s", symbol, e)

    conn.close()
    _disconnect_ib()

    # Send SMS for new entries
    if entered:
        lines = [f"NCMS V3 — {len(entered)} new trade(s):"]
        for t in entered:
            emoji = "SHORT" if t["direction"] == "bearish" else "LONG"
            tags = []
            if t.get("ib_executed"): tags.append("IB")
            if t.get("ws_executed"): tags.append("WS")
            if not tags: tags.append("paper")
            tag_str = " [" + "+".join(tags) + "]"
            lines.append(f"{emoji} {t['symbol']} @ ${t['entry_price']:.2f} (conf {t['confidence']:.0%}){tag_str}")
        _send_sms("\n".join(lines))

    return entered


STOP_LOSS_PCT = -3.0
MAX_HOLD_DAYS = 10
CAUTION_THRESHOLD = 0.35

GOLD_KW = {"gold", "precious metal", "bullion", "safe haven", "gld"}
GOLD_CAUTIONARY_KW = {"correction", "overvalued", "bubble", "crash", "too high", "too late", "pullback", "come down", "careful", "overextend", "top out"}


def _check_exit_conditions(trade, run_date: str, conn) -> str | None:
    """
    Check narrative-based exit conditions. Returns exit reason or None.

    Exit triggers (checked every run):
      1. Stop loss at -3% (immediate)
      2. Gold caution ratio > 35% (GLD only, after 2 days)
      3. Max hold 10 trading days
    """
    symbol = trade["symbol"]
    entry_price = trade["entry_price"]
    direction = trade["direction"]
    holding_days = trade["holding_days"]

    # Get current price
    price = _fetch_price(symbol)
    if not price:
        row = conn.execute(
            "SELECT close_price FROM daily_prices WHERE symbol=? ORDER BY price_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        price = row["close_price"] if row else None

    if price:
        if direction == "bearish":
            pnl_pct = ((entry_price - price) / entry_price) * 100
        else:
            pnl_pct = ((price - entry_price) / entry_price) * 100

        # 1. Stop loss
        if pnl_pct <= STOP_LOSS_PCT:
            return "stop_loss"

    # After minimum 2 days hold, check narrative exits
    if holding_days >= 2:
        # 2. Gold caution ratio (GLD only)
        if symbol == "GLD":
            units = conn.execute(
                "SELECT text FROM units WHERE run_date=?", (run_date,)
            ).fetchall()
            gold_total = 0
            gold_cautionary = 0
            for u in units:
                text = (u[0] or "").lower()
                if any(kw in text for kw in GOLD_KW):
                    gold_total += 1
                    if any(kw in text for kw in GOLD_CAUTIONARY_KW):
                        gold_cautionary += 1
            if gold_total > 0 and (gold_cautionary / gold_total) > CAUTION_THRESHOLD:
                return "caution_ratio"

    # 3. Max hold
    if holding_days >= MAX_HOLD_DAYS:
        return "max_hold"

    return None


def check_exits(run_date: str, db_path: Path = None) -> list[dict]:
    """
    Check open trades for exit conditions (narrative-based + stop loss).

    Called on every cron run (2x daily). Uses:
      - Stop loss at -3% (immediate, any time)
      - Gold caution ratio > 35% (after 2 days, GLD only)
      - Max hold 10 trading days
    """
    conn = _get_conn(db_path)
    _ensure_tables(conn)

    open_trades = conn.execute(
        "SELECT * FROM autopilot_trades WHERE status='open'"
    ).fetchall()

    if not open_trades:
        return []

    closed = []
    dt_now = datetime.strptime(run_date, "%Y-%m-%d")

    for trade in open_trades:
        entry_date = trade["entry_date"]
        dt_entry = datetime.strptime(entry_date, "%Y-%m-%d")

        # Count trading days
        trading_days = 0
        d = dt_entry + timedelta(days=1)
        while d <= dt_now:
            if d.weekday() < 5:  # Mon-Fri
                trading_days += 1
            d += timedelta(days=1)

        # Update holding_days in DB
        conn.execute(
            "UPDATE autopilot_trades SET holding_days=? WHERE id=?",
            (trading_days, trade["id"]),
        )

        # Check smart exit conditions
        # Build a temporary trade dict with updated holding_days
        trade_dict = dict(trade)
        trade_dict["holding_days"] = trading_days
        exit_reason = _check_exit_conditions(trade_dict, run_date, conn)

        if exit_reason:
            # Time to exit
            symbol = trade["symbol"]
            ib_closed = False

            # Try IB close first
            ib = _get_ib()
            if ib:
                ib_result = _ib_close_position(ib, symbol, trade["direction"], trade["qty"])
                if ib_result and ib_result["fill_price"] > 0:
                    price = ib_result["fill_price"]
                    ib_closed = True
                    log.info("Autopilot IB CLOSE: %s @ $%.2f", symbol, price)

            # Fallback to yfinance price
            if not ib_closed:
                price = _fetch_price(symbol)
                if price is None:
                    row = conn.execute(
                        "SELECT close_price FROM daily_prices WHERE symbol=? ORDER BY price_date DESC LIMIT 1",
                        (symbol,),
                    ).fetchone()
                    price = row["close_price"] if row else trade["entry_price"]

            # Store exit price
            _store_price(conn, symbol, price, run_date)

            # Calculate P&L
            entry_price = trade["entry_price"]
            qty = trade["qty"]
            if trade["direction"] == "bearish":
                pnl = (entry_price - price) * qty
            else:
                pnl = (price - entry_price) * qty

            pnl_pct = ((price / entry_price) - 1) * 100
            if trade["direction"] == "bearish":
                pnl_pct = -pnl_pct

            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                """UPDATE autopilot_trades SET
                   exit_price=?, exit_date=?, pnl=?, pnl_pct=?,
                   status='closed', closed_at=?, holding_days=?
                   WHERE id=?""",
                (price, run_date, round(pnl, 2), round(pnl_pct, 2),
                 now, trading_days, trade["id"]),
            )

            log.info("Autopilot EXIT [%s]: %s %s @ $%.2f → $%.2f, P&L $%+.2f (%+.1f%%)%s",
                     exit_reason, trade["direction"].upper(), symbol,
                     entry_price, price, pnl, pnl_pct,
                     " [IB]" if ib_closed else " [paper]")

            closed.append({
                "symbol": symbol,
                "direction": trade["direction"],
                "entry_price": entry_price,
                "exit_price": price,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": trading_days,
                "ib_executed": ib_closed,
                "exit_reason": exit_reason,
            })

    conn.commit()
    conn.close()
    _disconnect_ib()

    # Send SMS for exits
    if closed:
        total_pnl = sum(t["pnl"] for t in closed)
        lines = [f"NCMS V3 — {len(closed)} trade(s) closed:"]
        for t in closed:
            lines.append(
                f"{'SHORT' if t['direction']=='bearish' else 'LONG'} {t['symbol']}: "
                f"${t['entry_price']:.2f}→${t['exit_price']:.2f} = ${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) [{t['exit_reason']}]"
            )
        lines.append(f"Day total: ${total_pnl:+.2f}")
        _send_sms("\n".join(lines))

    return closed


def daily_summary(run_date: str, db_path: Path = None) -> dict:
    """Generate daily summary of autopilot performance."""
    conn = _get_conn(db_path)
    _ensure_tables(conn)

    open_trades = conn.execute(
        "SELECT * FROM autopilot_trades WHERE status='open'"
    ).fetchall()

    closed_trades = conn.execute(
        "SELECT * FROM autopilot_trades WHERE status='closed'"
    ).fetchall()

    total_pnl = sum(t["pnl"] or 0 for t in closed_trades)
    wins = [t for t in closed_trades if (t["pnl"] or 0) > 0]
    losses = [t for t in closed_trades if (t["pnl"] or 0) <= 0]
    win_rate = len(wins) / len(closed_trades) if closed_trades else 0

    # Total invested
    total_invested = sum(t["trade_amount"] or 0 for t in closed_trades)
    roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    summary = {
        "date": run_date,
        "open_trades": len(open_trades),
        "closed_trades": len(closed_trades),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "roi_pct": round(roi, 2),
        "total_invested": round(total_invested, 2),
    }

    # Store summary
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    best = max(closed_trades, key=lambda t: t["pnl"] or 0) if closed_trades else None
    worst = min(closed_trades, key=lambda t: t["pnl"] or 0) if closed_trades else None

    try:
        conn.execute(
            """INSERT OR REPLACE INTO autopilot_summary
               (summary_date, summary_type, open_trades, closed_trades,
                total_pnl, win_rate, best_trade, worst_trade, details, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_date, "daily", len(open_trades), len(closed_trades),
             total_pnl, win_rate,
             f"{best['symbol']} ${best['pnl']:+.2f}" if best else "",
             f"{worst['symbol']} ${worst['pnl']:+.2f}" if worst else "",
             json.dumps(summary), now),
        )
        conn.commit()
    except Exception as e:
        log.warning("Failed to store summary: %s", e)

    conn.close()

    # Log it
    log.info(
        "Autopilot summary: %d open, %d closed, P&L $%+.2f, win rate %.0f%%, ROI %.1f%%",
        summary["open_trades"], summary["closed_trades"],
        summary["total_pnl"], summary["win_rate"] * 100, summary["roi_pct"],
    )

    # Open positions detail
    if open_trades:
        lines = [f"NCMS Autopilot — {len(open_trades)} open:"]
        for t in open_trades:
            lines.append(f"  {t['direction']} {t['symbol']} @ ${t['entry_price']:.2f} (day {t['holding_days']}/{HOLDING_DAYS})")
        if closed_trades:
            lines.append(f"Lifetime: {len(closed_trades)} closed, ${total_pnl:+.2f}, {win_rate:.0%} win rate")
        log.info("\n".join(lines))

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python3 autopilot.py [status|summary|test-enter DATE]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        conn = _get_conn()
        _ensure_tables(conn)
        open_t = conn.execute("SELECT * FROM autopilot_trades WHERE status='open'").fetchall()
        closed_t = conn.execute("SELECT * FROM autopilot_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 10").fetchall()
        conn.close()

        print(f"\nOPEN TRADES ({len(open_t)}):")
        for t in open_t:
            print(f"  {t['direction']} {t['symbol']} @ ${t['entry_price']:.2f} "
                  f"(day {t['holding_days']}/{HOLDING_DAYS}, conf={t['confidence']:.2f})")

        print(f"\nRECENT CLOSED ({len(closed_t)}):")
        for t in closed_t:
            print(f"  {t['direction']} {t['symbol']} ${t['entry_price']:.2f}→${t['exit_price']:.2f} "
                  f"P&L ${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)")

        if closed_t:
            total = sum(t['pnl'] for t in closed_t)
            print(f"\n  Recent total: ${total:+.2f}")

    elif cmd == "summary":
        date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")
        daily_summary(date)

    elif cmd == "test-enter":
        date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")
        # Import v3 and run
        sys.path.insert(0, str(DB_PATH.parent.parent))
        from features.v3_strategy import evaluate_v3
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM units WHERE run_date=?", (date,)
        ).fetchall()
        conn.close()
        units = [dict(r) for r in rows]
        print(f"Loaded {len(units)} units for {date}")
        signals = evaluate_v3(date, units)
        if signals:
            entered = process_signals(date, signals)
            print(f"Entered {len(entered)} trades")
        else:
            print("No signals")
