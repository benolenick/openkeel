#!/usr/bin/env python3
"""
ncms_options_model.py — Model NCMS backtest trades as options positions.

Uses Black-Scholes approximation to estimate option premiums and P&L.
Takes the existing backtest_results from the compound strategy and models
what each trade would have returned using ATM or slightly OTM options.

Key assumptions:
  - Buy calls for bullish signals, puts for bearish
  - Strike: ATM (at-the-money) or 2% OTM
  - DTE: 7 days (weekly options) — aligns with 5-day holding period
  - IV: estimated from historical vol (30-day rolling, annualized)
  - Exit at intrinsic value after holding period (conservative)
  - Position size: fixed dollar amount per trade (e.g., $100, $500)
"""

import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")


def black_scholes_call(S, K, T, r, sigma):
    """Black-Scholes call price."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def black_scholes_put(S, K, T, r, sigma):
    """Black-Scholes put price."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _norm_cdf(x):
    """Approximation of cumulative normal distribution."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def estimate_iv(prices: dict, date: str, symbol: str, lookback: int = 30) -> float:
    """Estimate implied volatility from historical daily returns."""
    symbol_prices = prices.get(symbol, {})
    sorted_dates = sorted(d for d in symbol_prices if d <= date)

    if len(sorted_dates) < lookback + 1:
        # Fallback IV estimates by asset class
        fallback = {"GLD": 0.25, "TLT": 0.15, "XLE": 0.30, "SPY": 0.18,
                    "QQQ": 0.22, "SMH": 0.35, "XLF": 0.20}
        return fallback.get(symbol, 0.25)

    recent = sorted_dates[-(lookback + 1):]
    returns = []
    for i in range(1, len(recent)):
        p0 = symbol_prices[recent[i - 1]]
        p1 = symbol_prices[recent[i]]
        if p0 > 0:
            returns.append(math.log(p1 / p0))

    if not returns:
        return 0.25

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
    daily_vol = math.sqrt(variance)
    annualized = daily_vol * math.sqrt(252)

    # Options IV is typically 1.2-1.5x realized vol
    return annualized * 1.3


def fetch_all_prices() -> dict:
    """Load all prices from DB."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT symbol, price_date, close_price FROM daily_prices").fetchall()
    conn.close()

    prices = defaultdict(dict)
    for r in rows:
        prices[r["symbol"]][r["price_date"]] = r["close_price"]
    return dict(prices)


def model_options_pnl(budget_per_trade: float = 100.0):
    """Model each compound backtest trade as an options position."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    trades = conn.execute(
        """SELECT trade_date, symbol, direction, entry_price, exit_price,
                  pnl, holding_days, confidence
           FROM backtest_results
           WHERE strategy='compound' AND is_summary=0
           ORDER BY trade_date"""
    ).fetchall()
    conn.close()

    if not trades:
        print("No compound trades found in backtest_results")
        return

    prices = fetch_all_prices()

    print(f"\n{'=' * 90}")
    print(f"  NCMS OPTIONS MODEL — ${budget_per_trade:.0f} per trade")
    print(f"{'=' * 90}")

    r = 0.045  # risk-free rate (~current)
    results_atm = []
    results_otm = []

    header = (f"  {'Date':<12} {'Sym':<5} {'Dir':<5} {'Stock':>7} {'Exit':>7} "
              f"{'IV':>5} {'ATM$':>6} {'#':>3} {'ATM P&L':>9} "
              f"{'OTM$':>6} {'#':>3} {'OTM P&L':>9}")
    print(header)
    print(f"  {'─' * 86}")

    for t in trades:
        date = t["trade_date"]
        sym = t["symbol"]
        direction = t["direction"]
        S = t["entry_price"]
        S_exit = t["exit_price"]
        hold = t["holding_days"]

        iv = estimate_iv(prices, date, sym)
        T_entry = 7 / 365  # 7 DTE weekly
        T_exit = max((7 - hold) / 365, 0.5 / 365)  # remaining DTE at exit

        # ATM option
        K_atm = round(S, 0)
        if direction == "bullish":
            premium_atm = black_scholes_call(S, K_atm, T_entry, r, iv)
            exit_val_atm = black_scholes_call(S_exit, K_atm, T_exit, r, iv)
        else:
            premium_atm = black_scholes_put(S, K_atm, T_entry, r, iv)
            exit_val_atm = black_scholes_put(S_exit, K_atm, T_exit, r, iv)

        # 2% OTM option (cheaper, more leverage)
        if direction == "bullish":
            K_otm = round(S * 1.02, 0)
            premium_otm = black_scholes_call(S, K_otm, T_entry, r, iv)
            exit_val_otm = black_scholes_call(S_exit, K_otm, T_exit, r, iv)
        else:
            K_otm = round(S * 0.98, 0)
            premium_otm = black_scholes_put(S, K_otm, T_entry, r, iv)
            exit_val_otm = black_scholes_put(S_exit, K_otm, T_exit, r, iv)

        # Contract size = 100 shares. Premium is per-share.
        contract_cost_atm = premium_atm * 100
        contract_cost_otm = premium_otm * 100

        # How many contracts can we buy?
        n_atm = max(1, int(budget_per_trade / contract_cost_atm)) if contract_cost_atm > 0 else 0
        n_otm = max(1, int(budget_per_trade / contract_cost_otm)) if contract_cost_otm > 0 else 0

        # P&L
        if n_atm > 0:
            cost_atm = n_atm * contract_cost_atm
            value_atm = n_atm * exit_val_atm * 100
            pnl_atm = value_atm - cost_atm
            results_atm.append({"date": date, "sym": sym, "pnl": pnl_atm, "cost": cost_atm})
        else:
            pnl_atm = 0
            cost_atm = 0

        if n_otm > 0:
            cost_otm = n_otm * contract_cost_otm
            value_otm = n_otm * exit_val_otm * 100
            pnl_otm = value_otm - cost_otm
            results_otm.append({"date": date, "sym": sym, "pnl": pnl_otm, "cost": cost_otm})
        else:
            pnl_otm = 0
            cost_otm = 0

        pnl_atm_s = f"${pnl_atm:+,.0f}" if pnl_atm != 0 else "$0"
        pnl_otm_s = f"${pnl_otm:+,.0f}" if pnl_otm != 0 else "$0"

        print(f"  {date:<12} {sym:<5} {direction[:4]:<5} ${S:>6.1f} ${S_exit:>6.1f} "
              f"{iv:>4.0%} ${premium_atm:>4.1f} {n_atm:>3} {pnl_atm_s:>9} "
              f"${premium_otm:>4.1f} {n_otm:>3} {pnl_otm_s:>9}")

    # Summary
    print(f"\n{'=' * 90}")
    print(f"  SUMMARY — ${budget_per_trade:.0f}/trade budget")
    print(f"{'=' * 90}")

    for label, results in [("ATM", results_atm), ("2% OTM", results_otm)]:
        if not results:
            continue
        total_pnl = sum(r["pnl"] for r in results)
        total_cost = sum(r["cost"] for r in results)
        wins = [r for r in results if r["pnl"] > 0]
        losses = [r for r in results if r["pnl"] <= 0]
        win_rate = len(wins) / len(results) if results else 0
        avg_win = sum(r["pnl"] for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r["pnl"] for r in losses) / len(losses) if losses else 0
        max_loss = min(r["pnl"] for r in results) if results else 0
        max_win = max(r["pnl"] for r in results) if results else 0
        roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0

        print(f"\n  {label} Options:")
        print(f"    Trades:     {len(results)}")
        print(f"    Total P&L:  ${total_pnl:+,.2f}")
        print(f"    Total Cost: ${total_cost:,.2f}")
        print(f"    ROI:        {roi:+.1f}%")
        print(f"    Win Rate:   {win_rate:.1%}")
        print(f"    Avg Win:    ${avg_win:+,.2f}")
        print(f"    Avg Loss:   ${avg_loss:+,.2f}")
        print(f"    Best Trade: ${max_win:+,.2f}")
        print(f"    Worst:      ${max_loss:+,.2f}")

    print(f"\n{'=' * 90}")


if __name__ == "__main__":
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    model_options_pnl(budget)
