#!/usr/bin/env python3
"""
ncms_v3_backtest.py — Run v3 strategy across all historical dates and calculate P&L.
"""
import json
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("ncms.v3_backtest")

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")
sys.path.insert(0, str(DB_PATH.parent.parent))

from features.v3_strategy import evaluate_v3


def run():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    # Get all dates with units
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT run_date FROM units ORDER BY run_date"
    ).fetchall()]

    # Load all prices
    prices = defaultdict(dict)
    for r in conn.execute("SELECT symbol, price_date, close_price FROM daily_prices").fetchall():
        prices[r[0]][r[1]] = r[2]

    log.info("Backtesting v3 across %d dates", len(dates))

    all_trades = []
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})

    for run_date in dates:
        # Skip weekends
        dt = datetime.strptime(run_date, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue

        rows = conn.execute(
            "SELECT unit_id, episode_id, channel_id, parent_group, run_date, seq, text, tickers_json "
            "FROM units WHERE run_date=?", (run_date,)
        ).fetchall()
        units = [dict(r) for r in rows]
        if not units:
            continue

        signals = evaluate_v3(run_date, units, DB_PATH)
        if not signals:
            continue

        for sig in signals:
            symbol = sig["symbol"]
            direction = sig["direction"]
            confidence = sig["confidence"]

            # Get entry price
            entry_price = prices.get(symbol, {}).get(run_date)
            if not entry_price:
                # Try next trading day
                for i in range(1, 4):
                    nd = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
                    entry_price = prices.get(symbol, {}).get(nd)
                    if entry_price:
                        break
            if not entry_price:
                continue

            # Get exit price (5 trading days later)
            sorted_dates = sorted(prices.get(symbol, {}).keys())
            try:
                idx = sorted_dates.index(run_date)
            except ValueError:
                idx = None
                for i, d in enumerate(sorted_dates):
                    if d >= run_date:
                        idx = i
                        break
            if idx is None:
                continue

            exit_idx = min(idx + 5, len(sorted_dates) - 1)
            exit_date = sorted_dates[exit_idx]
            exit_price = prices[symbol][exit_date]
            hold_days = exit_idx - idx

            # P&L
            trade_amount = 100.0  # $100 per trade
            qty = trade_amount / entry_price
            if direction == "bearish":
                pnl = (entry_price - exit_price) * qty
            else:
                pnl = (exit_price - entry_price) * qty

            pnl_pct = ((exit_price / entry_price) - 1) * 100
            if direction == "bearish":
                pnl_pct = -pnl_pct

            month = run_date[:7]
            monthly[month]["trades"] += 1
            monthly[month]["pnl"] += pnl
            if pnl > 0:
                monthly[month]["wins"] += 1

            all_trades.append({
                "date": run_date,
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "entry": entry_price,
                "exit": exit_price,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "hold": hold_days,
                "reasons": ", ".join(sig.get("reasons", [])),
            })

    conn.close()

    # Print results
    print(f"\n{'='*80}")
    print(f"  NCMS V3 STRATEGY BACKTEST — $100/trade")
    print(f"{'='*80}")
    print(f"{'Date':<12} {'Sym':<5} {'Dir':<8} {'Conf':>5} {'Entry':>8} {'Exit':>8} {'P&L':>8} {'%':>7} Note")
    print("-" * 80)

    for t in all_trades:
        result = "WIN" if t["pnl"] > 0 else "LOSS"
        print(f"{t['date']:<12} {t['symbol']:<5} {t['direction']:<8} {t['confidence']:>5.2f} "
              f"${t['entry']:>7.2f} ${t['exit']:>7.2f} ${t['pnl']:>+7.2f} {t['pnl_pct']:>+6.1f}% {result}")

    # Monthly summary
    print(f"\n{'='*60}")
    print(f"MONTHLY BREAKDOWN:")
    print(f"{'Month':<10} {'Trades':>7} {'Wins':>5} {'Win%':>6} {'P&L':>10}")
    print("-" * 45)
    total_pnl = 0
    total_trades = 0
    total_wins = 0
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        total_pnl += m["pnl"]
        total_trades += m["trades"]
        total_wins += m["wins"]
        print(f"{month:<10} {m['trades']:>7} {m['wins']:>5} {wr:>5.0f}% ${m['pnl']:>+9.2f}")

    wr_total = total_wins / total_trades * 100 if total_trades > 0 else 0
    print("-" * 45)
    print(f"{'TOTAL':<10} {total_trades:>7} {total_wins:>5} {wr_total:>5.0f}% ${total_pnl:>+9.2f}")

    # Performance metrics
    pnls = [t["pnl"] for t in all_trades]
    if len(pnls) >= 2:
        mean_pnl = sum(pnls) / len(pnls)
        var = sum((p - mean_pnl)**2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.001
        sharpe = (mean_pnl / std) * math.sqrt(252 / max(len(pnls), 1))
    else:
        sharpe = 0

    max_dd = 0
    equity = [0]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = 0
    for e in equity:
        if e > peak: peak = e
        dd = peak - e
        if dd > max_dd: max_dd = dd

    print(f"\n{'='*60}")
    print(f"  Total trades:  {total_trades}")
    print(f"  Total P&L:     ${total_pnl:+,.2f}")
    print(f"  Win rate:      {wr_total:.1f}%")
    print(f"  Sharpe ratio:  {sharpe:.2f}")
    print(f"  Max drawdown:  ${max_dd:,.2f}")
    print(f"  Avg P&L/trade: ${mean_pnl:+,.2f}" if pnls else "")
    total_invested = total_trades * 100
    roi = total_pnl / total_invested * 100 if total_invested > 0 else 0
    print(f"  Total invested:${total_invested:,.0f}")
    print(f"  ROI:           {roi:+.1f}%")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()
