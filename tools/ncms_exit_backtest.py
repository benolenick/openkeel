#!/usr/bin/env python3
"""
ncms_exit_backtest.py — Test multiple exit strategies against v3 entry signals.

Iteratively tests and compares exit methods:
  1. Fixed hold (baseline: 5 days)
  2. Fed regime shift exit
  3. Gold caution ratio exit
  4. Sentiment flip exit (inst+retail both bullish)
  5. Convergence collapse exit
  6. Narrative freshness exit (no new groups)
  7. Stop loss / trailing stop
  8. Combined smart exit

Each method is tested against the same entry signals to get apples-to-apples comparison.
"""

import json
import logging
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("ncms.exit_backtest")

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")
sys.path.insert(0, str(DB_PATH.parent.parent))

# ---------------------------------------------------------------------------
# Keyword sets (matching v3_strategy.py)
# ---------------------------------------------------------------------------
GOLD_KW = {"gold", "precious metal", "bullion", "safe haven", "gld"}
ENERGY_KW = {"oil", "crude", "energy", "opec", "drill", "barrel", "refin", "xle", "pipeline", "natural gas"}
FED_KW = {"fed ", "rate cut", "interest rate", "inflation", "cpi", "fomc", "powell", "dovish", "hawkish", "monetary policy"}
GEO_KW = {"tariff", "trump", "trade war", "china", "war ", "iran", "sanction", "geopolit", "military", "conflict"}
GOLD_CAUTIONARY_KW = {"correction", "overvalued", "bubble", "crash", "too high", "too late", "pullback", "come down", "careful", "overextend", "top out"}
GOLD_EUPHORIC_KW = {"record", "all-time", "unstoppable", "keep going", "bullish", "higher", "rally", "flock", "soar", "10000", "safe haven", "shining"}
HAWKISH_KW = {"inflation", "hawkish", "higher for longer", "no cut", "rate hike", "higher rate"}
DOVISH_KW = {"rate cut", "dovish", "easing", "money printer", "lower rate"}

INSTITUTIONAL_GROUPS = {"bloomberg", "thomson_reuters", "versant_media"}
RETAIL_GROUPS = {"wealthion", "finfluencer", "finfluencer_pro", "crypto_media"}


def _text_matches(text, keywords):
    lower = text.lower()
    return sum(1 for kw in keywords if kw in lower)


def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Load all data upfront
# ---------------------------------------------------------------------------

def load_all_data():
    """Load prices, units per day, and v3 entry signals."""
    conn = get_conn()

    # Prices
    prices = defaultdict(dict)
    for r in conn.execute("SELECT symbol, price_date, close_price FROM daily_prices").fetchall():
        prices[r[0]][r[1]] = r[2]

    # Units per day (precompute daily context)
    all_dates = [r[0] for r in conn.execute("SELECT DISTINCT run_date FROM units ORDER BY run_date").fetchall()]

    daily_context = {}
    for run_date in all_dates:
        rows = conn.execute(
            "SELECT parent_group, text FROM units WHERE run_date=?", (run_date,)
        ).fetchall()

        ctx = {
            "groups": set(),
            "gold_groups": set(), "energy_groups": set(), "fed_groups": set(), "geo_groups": set(),
            "gold_units": 0, "energy_units": 0, "fed_units": 0, "geo_units": 0,
            "gold_cautionary": 0, "gold_euphoric": 0, "gold_total": 0,
            "inst_gold_bull": 0, "inst_gold_caut": 0,
            "retail_gold_bull": 0, "retail_gold_caut": 0,
            "total_units": len(rows),
            # Track which groups talk about gold specifically
            "gold_talkers": set(),
        }

        for r in rows:
            group = r[0]
            text = r[1] or ""
            ctx["groups"].add(group)

            is_gold = _text_matches(text, GOLD_KW) > 0
            is_energy = _text_matches(text, ENERGY_KW) > 0
            is_fed = _text_matches(text, FED_KW) > 0
            is_geo = _text_matches(text, GEO_KW) > 0

            if is_gold:
                ctx["gold_groups"].add(group)
                ctx["gold_units"] += 1
                ctx["gold_total"] += 1
                ctx["gold_talkers"].add(group)
                if _text_matches(text, GOLD_CAUTIONARY_KW) > 0:
                    ctx["gold_cautionary"] += 1
                if _text_matches(text, GOLD_EUPHORIC_KW) > 0:
                    ctx["gold_euphoric"] += 1
                if group in INSTITUTIONAL_GROUPS:
                    if _text_matches(text, GOLD_EUPHORIC_KW) > 0: ctx["inst_gold_bull"] += 1
                    if _text_matches(text, GOLD_CAUTIONARY_KW) > 0: ctx["inst_gold_caut"] += 1
                elif group in RETAIL_GROUPS:
                    if _text_matches(text, GOLD_EUPHORIC_KW) > 0: ctx["retail_gold_bull"] += 1
                    if _text_matches(text, GOLD_CAUTIONARY_KW) > 0: ctx["retail_gold_caut"] += 1

            if is_energy: ctx["energy_groups"].add(group); ctx["energy_units"] += 1
            if is_fed: ctx["fed_groups"].add(group); ctx["fed_units"] += 1
            if is_geo: ctx["geo_groups"].add(group); ctx["geo_units"] += 1

        daily_context[run_date] = ctx

    conn.close()
    return prices, daily_context, all_dates


def get_v3_entries(daily_context, all_dates, prices):
    """Generate v3 entry signals (simplified version of evaluate_v3)."""
    from features.v3_strategy import evaluate_v3

    conn = get_conn()
    entries = []

    for run_date in all_dates:
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
        for sig in signals:
            symbol = sig["symbol"]
            entry_price = prices.get(symbol, {}).get(run_date)
            if not entry_price:
                continue
            entries.append({
                "entry_date": run_date,
                "symbol": symbol,
                "direction": sig["direction"],
                "confidence": sig["confidence"],
                "entry_price": entry_price,
            })

    conn.close()
    return entries


# ---------------------------------------------------------------------------
# Exit strategy functions
# Each returns the exit_date for a given entry, or None if max_hold reached
# ---------------------------------------------------------------------------

def exit_fixed(entry, prices, daily_context, all_dates, hold_days=5, **kwargs):
    """Baseline: exit after N trading days."""
    sorted_dates = sorted(prices.get(entry["symbol"], {}).keys())
    try:
        idx = sorted_dates.index(entry["entry_date"])
    except ValueError:
        return None, 0
    exit_idx = min(idx + hold_days, len(sorted_dates) - 1)
    return sorted_dates[exit_idx], exit_idx - idx


def exit_fed_regime(entry, prices, daily_context, all_dates, max_hold=10, **kwargs):
    """Exit when Fed reclaims dominant narrative (3-day rolling)."""
    symbol = entry["symbol"]
    sorted_dates = sorted(d for d in all_dates if d >= entry["entry_date"])
    symbol_dates = sorted(prices.get(symbol, {}).keys())

    days_held = 0
    for i, d in enumerate(sorted_dates[1:], 1):
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        if d not in prices.get(symbol, {}):
            continue
        days_held += 1

        if days_held >= 2:  # minimum 2 days hold
            # Check 3-day rolling fed dominance
            lookback = [sorted_dates[max(0, i-2)], sorted_dates[max(0, i-1)], d]
            fed_total = sum(daily_context.get(dd, {}).get("fed_units", 0) for dd in lookback)
            geo_total = sum(daily_context.get(dd, {}).get("geo_units", 0) for dd in lookback)
            gold_total = sum(daily_context.get(dd, {}).get("gold_units", 0) for dd in lookback)
            energy_total = sum(daily_context.get(dd, {}).get("energy_units", 0) for dd in lookback)
            non_fed = geo_total + gold_total + energy_total

            if fed_total > non_fed * 1.5:
                return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[-1] if sorted_dates else None, days_held


def exit_caution_ratio(entry, prices, daily_context, all_dates, threshold=0.40, max_hold=10, **kwargs):
    """Exit GLD when gold caution ratio exceeds threshold."""
    if entry["symbol"] != "GLD":
        return exit_fixed(entry, prices, daily_context, all_dates, hold_days=max_hold)

    sorted_dates = sorted(d for d in all_dates if d >= entry["entry_date"])
    days_held = 0

    for d in sorted_dates[1:]:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        if d not in prices.get("GLD", {}):
            continue
        days_held += 1

        if days_held >= 2:
            ctx = daily_context.get(d, {})
            if ctx.get("gold_total", 0) > 0:
                ratio = ctx["gold_cautionary"] / ctx["gold_total"]
                if ratio > threshold:
                    return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[-1] if sorted_dates else None, days_held


def exit_sentiment_flip(entry, prices, daily_context, all_dates, max_hold=10, **kwargs):
    """Exit when institutional and retail both go bullish (Finding #9)."""
    if entry["symbol"] not in ("GLD", "XLE"):
        return exit_fixed(entry, prices, daily_context, all_dates, hold_days=max_hold)

    sorted_dates = sorted(d for d in all_dates if d >= entry["entry_date"])
    days_held = 0

    for d in sorted_dates[1:]:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        if d not in prices.get(entry["symbol"], {}):
            continue
        days_held += 1

        if days_held >= 2:
            ctx = daily_context.get(d, {})
            inst_bull = ctx.get("inst_gold_bull", 0) > ctx.get("inst_gold_caut", 0)
            retail_bull = ctx.get("retail_gold_bull", 0) > ctx.get("retail_gold_caut", 0)
            if inst_bull and retail_bull:
                return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[-1] if sorted_dates else None, days_held


def exit_convergence_collapse(entry, prices, daily_context, all_dates, min_groups=3, max_hold=10, **kwargs):
    """Exit when the number of groups talking about the asset drops below threshold."""
    symbol = entry["symbol"]
    topic_key = {"GLD": "gold_groups", "XLE": "energy_groups", "TLT": "fed_groups", "XLF": "fed_groups"}.get(symbol, "gold_groups")

    sorted_dates = sorted(d for d in all_dates if d >= entry["entry_date"])
    days_held = 0

    for d in sorted_dates[1:]:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        if d not in prices.get(symbol, {}):
            continue
        days_held += 1

        if days_held >= 2:
            ctx = daily_context.get(d, {})
            n_groups = len(ctx.get(topic_key, set()))
            if n_groups < min_groups:
                return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[-1] if sorted_dates else None, days_held


def exit_narrative_freshness(entry, prices, daily_context, all_dates, max_hold=10, **kwargs):
    """Exit when no new groups are joining the narrative (stale story)."""
    if entry["symbol"] not in ("GLD", "XLE"):
        return exit_fixed(entry, prices, daily_context, all_dates, hold_days=max_hold)

    symbol = entry["symbol"]
    topic_key = "gold_talkers" if symbol == "GLD" else "energy_groups"

    sorted_dates = sorted(d for d in all_dates if d >= entry["entry_date"])
    prev_groups = daily_context.get(entry["entry_date"], {}).get(topic_key, set())
    days_held = 0
    stale_days = 0

    for d in sorted_dates[1:]:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        if d not in prices.get(symbol, {}):
            continue
        days_held += 1

        if days_held >= 2:
            ctx = daily_context.get(d, {})
            curr_groups = ctx.get(topic_key, set())
            new_joiners = curr_groups - prev_groups
            if len(new_joiners) == 0:
                stale_days += 1
            else:
                stale_days = 0
            prev_groups = curr_groups

            if stale_days >= 2:  # 2 consecutive days with no new groups
                return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[-1] if sorted_dates else None, days_held


def exit_stop_loss(entry, prices, daily_context, all_dates, stop_pct=-3.0, max_hold=10, **kwargs):
    """Exit on stop loss percentage."""
    symbol = entry["symbol"]
    entry_price = entry["entry_price"]
    direction = entry["direction"]

    sorted_dates = sorted(prices.get(symbol, {}).keys())
    try:
        idx = sorted_dates.index(entry["entry_date"])
    except ValueError:
        return None, 0

    days_held = 0
    for i in range(idx + 1, min(idx + max_hold + 3, len(sorted_dates))):
        d = sorted_dates[i]
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        days_held += 1

        price = prices[symbol][d]
        if direction == "bearish":
            pnl_pct = ((entry_price - price) / entry_price) * 100
        else:
            pnl_pct = ((price - entry_price) / entry_price) * 100

        if pnl_pct <= stop_pct:
            return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[min(idx + max_hold, len(sorted_dates) - 1)], days_held


def exit_trailing_stop(entry, prices, daily_context, all_dates, trail_pct=2.0, max_hold=10, **kwargs):
    """Exit on trailing stop — locks in gains, exits on pullback from peak."""
    symbol = entry["symbol"]
    entry_price = entry["entry_price"]
    direction = entry["direction"]

    sorted_dates = sorted(prices.get(symbol, {}).keys())
    try:
        idx = sorted_dates.index(entry["entry_date"])
    except ValueError:
        return None, 0

    peak_pnl_pct = 0
    days_held = 0

    for i in range(idx + 1, min(idx + max_hold + 3, len(sorted_dates))):
        d = sorted_dates[i]
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        days_held += 1

        price = prices[symbol][d]
        if direction == "bearish":
            pnl_pct = ((entry_price - price) / entry_price) * 100
        else:
            pnl_pct = ((price - entry_price) / entry_price) * 100

        if pnl_pct > peak_pnl_pct:
            peak_pnl_pct = pnl_pct

        # Trail: if we've gained at least 1% and then pulled back trail_pct from peak
        if peak_pnl_pct >= 1.0 and (peak_pnl_pct - pnl_pct) >= trail_pct:
            return d, days_held

        if days_held >= max_hold:
            return d, days_held

    return sorted_dates[min(idx + max_hold, len(sorted_dates) - 1)], days_held


def exit_smart_combined(entry, prices, daily_context, all_dates,
                        stop_pct=-3.0, trail_pct=2.0, caution_threshold=0.40,
                        min_hold=2, max_hold=10, **kwargs):
    """
    Combined smart exit — checks ALL conditions daily:
      1. Stop loss (immediate)
      2. Fed regime shift (after min_hold)
      3. Gold caution ratio (GLD only, after min_hold)
      4. Sentiment flip (after min_hold)
      5. Trailing stop (after peak > 1%)
      6. Convergence collapse (after min_hold)
      7. Max hold
    First condition to trigger = exit.
    """
    symbol = entry["symbol"]
    entry_price = entry["entry_price"]
    direction = entry["direction"]
    topic_key = {"GLD": "gold_groups", "XLE": "energy_groups", "TLT": "fed_groups", "XLF": "fed_groups"}.get(symbol, "gold_groups")

    sorted_price_dates = sorted(prices.get(symbol, {}).keys())
    sorted_dates = sorted(d for d in all_dates if d >= entry["entry_date"])

    try:
        price_idx = sorted_price_dates.index(entry["entry_date"])
    except ValueError:
        return None, 0, "no_data"

    peak_pnl_pct = 0
    days_held = 0

    for i in range(price_idx + 1, min(price_idx + max_hold + 3, len(sorted_price_dates))):
        d = sorted_price_dates[i]
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() >= 5:
            continue
        days_held += 1

        price = prices[symbol][d]
        if direction == "bearish":
            pnl_pct = ((entry_price - price) / entry_price) * 100
        else:
            pnl_pct = ((price - entry_price) / entry_price) * 100

        # 1. Stop loss — always active
        if pnl_pct <= stop_pct:
            return d, days_held, "stop_loss"

        # Track peak for trailing stop
        if pnl_pct > peak_pnl_pct:
            peak_pnl_pct = pnl_pct

        # 2. Trailing stop — after we've been up at least 1%
        if peak_pnl_pct >= 1.0 and (peak_pnl_pct - pnl_pct) >= trail_pct:
            return d, days_held, "trailing_stop"

        # After minimum hold, check narrative conditions
        if days_held >= min_hold and d in daily_context:
            ctx = daily_context.get(d, {})

            # 3. Fed regime shift
            # Look at 3-day rolling
            d_idx = sorted_dates.index(d) if d in sorted_dates else -1
            if d_idx >= 2:
                lookback = sorted_dates[d_idx-2:d_idx+1]
                fed_t = sum(daily_context.get(dd, {}).get("fed_units", 0) for dd in lookback)
                non_fed = sum(daily_context.get(dd, {}).get("geo_units", 0) + daily_context.get(dd, {}).get("gold_units", 0) + daily_context.get(dd, {}).get("energy_units", 0) for dd in lookback)
                if fed_t > non_fed * 1.5:
                    return d, days_held, "fed_regime"

            # 4. Gold caution ratio (GLD only)
            if symbol == "GLD" and ctx.get("gold_total", 0) > 0:
                ratio = ctx["gold_cautionary"] / ctx["gold_total"]
                if ratio > caution_threshold:
                    return d, days_held, "caution_ratio"

            # 5. Sentiment flip (both inst + retail bullish)
            if symbol in ("GLD", "XLE"):
                inst_bull = ctx.get("inst_gold_bull", 0) > ctx.get("inst_gold_caut", 0)
                retail_bull = ctx.get("retail_gold_bull", 0) > ctx.get("retail_gold_caut", 0)
                if inst_bull and retail_bull and ctx.get("inst_gold_bull", 0) > 0:
                    return d, days_held, "sentiment_flip"

            # 6. Convergence collapse
            n_groups = len(ctx.get(topic_key, set()))
            if n_groups < 3:
                return d, days_held, "convergence_collapse"

        # 7. Max hold
        if days_held >= max_hold:
            return d, days_held, "max_hold"

    return sorted_price_dates[min(price_idx + max_hold, len(sorted_price_dates) - 1)], days_held, "end_of_data"


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def compute_pnl(entry, exit_date, prices):
    """Compute P&L for a trade."""
    symbol = entry["symbol"]
    entry_price = entry["entry_price"]
    exit_price = prices.get(symbol, {}).get(exit_date, entry_price)
    trade_amount = 100.0
    qty = trade_amount / entry_price

    if entry["direction"] == "bearish":
        pnl = (entry_price - exit_price) * qty
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
    else:
        pnl = (exit_price - entry_price) * qty
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

    return round(pnl, 2), round(pnl_pct, 2), exit_price


def run_strategy(name, exit_fn, entries, prices, daily_context, all_dates, **kwargs):
    """Run a single exit strategy across all entries."""
    results = []
    for entry in entries:
        if name == "smart_combined":
            exit_date, hold_days, reason = exit_fn(entry, prices, daily_context, all_dates, **kwargs)
        else:
            exit_date, hold_days = exit_fn(entry, prices, daily_context, all_dates, **kwargs)
            reason = name

        if exit_date is None:
            continue

        pnl, pnl_pct, exit_price = compute_pnl(entry, exit_date, prices)
        results.append({
            "entry_date": entry["entry_date"],
            "exit_date": exit_date,
            "symbol": entry["symbol"],
            "direction": entry["direction"],
            "entry_price": entry["entry_price"],
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_days": hold_days,
            "exit_reason": reason,
        })

    return results


def summarize(name, results):
    """Compute summary metrics."""
    if not results:
        return {"name": name, "trades": 0}

    pnls = [r["pnl"] for r in results]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls)
    avg_hold = sum(r["hold_days"] for r in results) / len(results)

    if len(pnls) >= 2:
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.001
        sharpe = (mean / std) * math.sqrt(252 / max(len(pnls), 1))
    else:
        sharpe = 0

    # Max drawdown
    equity = [0]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = 0
    max_dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = peak - e
        if dd > max_dd: max_dd = dd

    return {
        "name": name,
        "trades": len(pnls),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "avg_pnl": round(total_pnl / len(pnls), 2),
        "avg_hold": round(avg_hold, 1),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading data...")
    prices, daily_context, all_dates = load_all_data()
    print(f"Loaded {len(all_dates)} dates, {sum(len(v) for v in prices.values())} price rows")

    print("Generating v3 entry signals...")
    entries = get_v3_entries(daily_context, all_dates, prices)
    print(f"Got {len(entries)} entry signals")

    if not entries:
        print("No entries — check v3 strategy")
        return

    # =========================================================================
    # ROUND 1: Test all individual exit methods
    # =========================================================================
    strategies = {
        "fixed_3d": (exit_fixed, {"hold_days": 3}),
        "fixed_5d": (exit_fixed, {"hold_days": 5}),
        "fixed_7d": (exit_fixed, {"hold_days": 7}),
        "fixed_10d": (exit_fixed, {"hold_days": 10}),
        "fed_regime": (exit_fed_regime, {"max_hold": 10}),
        "caution_ratio_35": (exit_caution_ratio, {"threshold": 0.35, "max_hold": 10}),
        "caution_ratio_40": (exit_caution_ratio, {"threshold": 0.40, "max_hold": 10}),
        "caution_ratio_45": (exit_caution_ratio, {"threshold": 0.45, "max_hold": 10}),
        "sentiment_flip": (exit_sentiment_flip, {"max_hold": 10}),
        "convergence_collapse_2": (exit_convergence_collapse, {"min_groups": 2, "max_hold": 10}),
        "convergence_collapse_3": (exit_convergence_collapse, {"min_groups": 3, "max_hold": 10}),
        "convergence_collapse_4": (exit_convergence_collapse, {"min_groups": 4, "max_hold": 10}),
        "freshness": (exit_narrative_freshness, {"max_hold": 10}),
        "stop_loss_2pct": (exit_stop_loss, {"stop_pct": -2.0, "max_hold": 10}),
        "stop_loss_3pct": (exit_stop_loss, {"stop_pct": -3.0, "max_hold": 10}),
        "stop_loss_5pct": (exit_stop_loss, {"stop_pct": -5.0, "max_hold": 10}),
        "trailing_1.5pct": (exit_trailing_stop, {"trail_pct": 1.5, "max_hold": 10}),
        "trailing_2pct": (exit_trailing_stop, {"trail_pct": 2.0, "max_hold": 10}),
        "trailing_3pct": (exit_trailing_stop, {"trail_pct": 3.0, "max_hold": 10}),
    }

    all_results = {}
    for name, (fn, kwargs) in strategies.items():
        results = run_strategy(name, fn, entries, prices, daily_context, all_dates, **kwargs)
        all_results[name] = results

    # Print comparison
    print(f"\n{'='*90}")
    print(f"  EXIT STRATEGY COMPARISON — {len(entries)} entries, $100/trade")
    print(f"{'='*90}")
    print(f"{'Strategy':<28} {'Trades':>6} {'Win%':>6} {'P&L':>9} {'Avg':>7} {'Hold':>5} {'Sharpe':>7} {'MaxDD':>8}")
    print("-" * 90)

    summaries = []
    for name in strategies:
        s = summarize(name, all_results[name])
        summaries.append(s)
        print(f"{s['name']:<28} {s['trades']:>6} {s['win_rate']:>5.0%} ${s['total_pnl']:>+8.2f} ${s['avg_pnl']:>+6.2f} {s['avg_hold']:>5.1f} {s['sharpe']:>7.2f} ${s['max_dd']:>7.2f}")

    # =========================================================================
    # ROUND 2: Test smart combined with different parameter combos
    # =========================================================================
    print(f"\n{'='*90}")
    print(f"  SMART COMBINED EXIT — Parameter Sweep")
    print(f"{'='*90}")

    combos = [
        {"stop_pct": -2.0, "trail_pct": 1.5, "caution_threshold": 0.35, "min_hold": 2, "max_hold": 8},
        {"stop_pct": -2.0, "trail_pct": 2.0, "caution_threshold": 0.40, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 1.5, "caution_threshold": 0.35, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 2.0, "caution_threshold": 0.40, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 2.0, "caution_threshold": 0.40, "min_hold": 3, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 2.5, "caution_threshold": 0.40, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 2.0, "caution_threshold": 0.45, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 2.0, "caution_threshold": 0.40, "min_hold": 2, "max_hold": 7},
        {"stop_pct": -4.0, "trail_pct": 2.0, "caution_threshold": 0.40, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -3.0, "trail_pct": 3.0, "caution_threshold": 0.40, "min_hold": 2, "max_hold": 10},
        {"stop_pct": -2.5, "trail_pct": 1.5, "caution_threshold": 0.38, "min_hold": 2, "max_hold": 8},
        {"stop_pct": -3.0, "trail_pct": 2.0, "caution_threshold": 0.35, "min_hold": 2, "max_hold": 8},
    ]

    print(f"{'Params':<55} {'Trades':>6} {'Win%':>6} {'P&L':>9} {'Avg':>7} {'Hold':>5} {'Sharpe':>7} {'MaxDD':>8}")
    print("-" * 105)

    best_sharpe = -999
    best_combo = None
    best_results = None

    for combo in combos:
        label = f"SL={combo['stop_pct']}% TS={combo['trail_pct']}% CR={combo['caution_threshold']} MH={combo['max_hold']}"
        results = run_strategy("smart_combined", exit_smart_combined, entries, prices, daily_context, all_dates, **combo)
        s = summarize(label, results)

        print(f"{label:<55} {s['trades']:>6} {s['win_rate']:>5.0%} ${s['total_pnl']:>+8.2f} ${s['avg_pnl']:>+6.2f} {s['avg_hold']:>5.1f} {s['sharpe']:>7.2f} ${s['max_dd']:>7.2f}")

        if s['sharpe'] > best_sharpe and s['trades'] >= 10:
            best_sharpe = s['sharpe']
            best_combo = combo
            best_results = results

    # =========================================================================
    # Print best result details
    # =========================================================================
    if best_results:
        print(f"\n{'='*90}")
        print(f"  BEST SMART EXIT: Sharpe {best_sharpe:.2f}")
        print(f"  Params: {best_combo}")
        print(f"{'='*90}")

        # Exit reason breakdown
        reasons = Counter(r["exit_reason"] for r in best_results)
        print(f"\nExit reasons:")
        for reason, count in reasons.most_common():
            subset = [r for r in best_results if r["exit_reason"] == reason]
            avg_pnl = sum(r["pnl"] for r in subset) / len(subset)
            win_r = sum(1 for r in subset if r["pnl"] > 0) / len(subset)
            print(f"  {reason:<25} {count:>4} trades, {win_r:>5.0%} win, ${avg_pnl:>+6.2f} avg")

        # Trade details
        print(f"\n{'Date':<12} {'Sym':<5} {'Dir':<6} {'Entry':>7} {'Exit':>7} {'P&L':>8} {'Hold':>5} {'Reason'}")
        print("-" * 70)
        for r in best_results:
            result = "WIN" if r["pnl"] > 0 else "LOSS"
            print(f"{r['entry_date']:<12} {r['symbol']:<5} {r['direction']:<6} "
                  f"${r['entry_price']:>6.2f} ${r['exit_price']:>6.2f} "
                  f"${r['pnl']:>+7.2f} {r['hold_days']:>5} {r['exit_reason']}")

    # Compare best smart vs fixed 5d
    print(f"\n{'='*90}")
    fixed5 = summarize("fixed_5d", all_results["fixed_5d"])
    best_s = summarize("best_smart", best_results) if best_results else {"total_pnl": 0, "sharpe": 0, "win_rate": 0, "max_dd": 0, "avg_hold": 0}
    print(f"  FIXED 5D:    P&L ${fixed5['total_pnl']:>+8.2f}, Sharpe {fixed5['sharpe']:.2f}, Win {fixed5['win_rate']:.0%}, MaxDD ${fixed5['max_dd']:.2f}, AvgHold {fixed5['avg_hold']:.1f}d")
    print(f"  BEST SMART:  P&L ${best_s['total_pnl']:>+8.2f}, Sharpe {best_s['sharpe']:.2f}, Win {best_s['win_rate']:.0%}, MaxDD ${best_s['max_dd']:.2f}, AvgHold {best_s['avg_hold']:.1f}d")
    improvement = best_s['total_pnl'] - fixed5['total_pnl']
    print(f"  IMPROVEMENT: ${improvement:>+8.2f}")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
