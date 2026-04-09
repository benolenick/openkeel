"""One-metric dashboard — weighted pool_units this week vs last week.

Per docs/token_saver_v6_routing.md, the honest metric is:
  pool_units = full_rate_tokens × model_weight + cache_read × 0.1 × model_weight

Model weights (approximate pool share on Max plan):
  opus   = 1.00
  sonnet = 0.20
  haiku  = 0.04

This single number is the only thing that matters. If it's going down, we're
saving your plan. If not, nothing else matters.

Usage:
    python3 -m openkeel.token_saver.one_metric
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".openkeel" / "token_ledger.db"
PROXY_TRACE = Path.home() / ".openkeel" / "proxy_trace.jsonl"

MODEL_WEIGHTS = {"opus": 1.0, "sonnet": 0.20, "haiku": 0.04}


def _model_bucket(m: str) -> str:
    m = (m or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "opus"


def _window(conn, start_ts, end_ts):
    rows = conn.execute(
        "SELECT model, COALESCE(SUM(cache_creation), 0), COALESCE(SUM(output_tokens), 0), "
        "COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cache_read), 0), COUNT(*) "
        "FROM billed_tokens WHERE timestamp BETWEEN ? AND ? GROUP BY model",
        (start_ts, end_ts),
    ).fetchall()
    agg = {"turns": 0, "pool_units": 0.0, "by_model": {}}
    for model, cc, out, in_, cr, turns in rows:
        bucket = _model_bucket(model)
        weight = MODEL_WEIGHTS.get(bucket, 1.0)
        full_rate = cc + out + in_
        pool = (full_rate + cr * 0.1) * weight
        agg["turns"] += turns
        agg["pool_units"] += pool
        m = agg["by_model"].setdefault(bucket, {"turns": 0, "full_rate": 0, "pool": 0.0})
        m["turns"] += turns
        m["full_rate"] += full_rate
        m["pool"] += pool
    return agg


def _fmt(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def main():
    if not DB_PATH.exists():
        print("No ledger found.")
        return
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    now = time.time()
    week = 7 * 86400
    this_week = _window(conn, now - week, now)
    last_week = _window(conn, now - 2 * week, now - week)
    conn.close()

    this_pool = this_week["pool_units"]
    last_pool = last_week["pool_units"]
    delta = this_pool - last_pool
    pct = (100 * delta / last_pool) if last_pool else 0

    arrow = "▼" if delta < 0 else ("▲" if delta > 0 else "•")
    color = "\033[32m" if delta < 0 else ("\033[31m" if delta > 0 else "\033[33m")
    reset = "\033[0m"

    print()
    print("  ══════════════════════════════════════════════════════════")
    print("    TOKEN SAVER — POOL UNITS (the only metric)")
    print("    pool = (full_rate + cache_read×0.1) × model_weight")
    print("    weights: opus=1.00  sonnet=0.20  haiku=0.04")
    print("  ══════════════════════════════════════════════════════════")
    print()
    print(f"    THIS WEEK: {_fmt(this_pool):>10}  ({this_week['turns']:,} turns)")
    print(f"    LAST WEEK: {_fmt(last_pool):>10}  ({last_week['turns']:,} turns)")
    print(f"    {color}DELTA:     {arrow} {_fmt(abs(delta)):>8}  ({pct:+.1f}%){reset}")
    print()
    if last_pool == 0:
        print("    (no prior week data yet)")
    elif delta < 0:
        print(f"    {color}✓ going down — plan is getting bigger{reset}")
    elif delta > 0:
        print(f"    {color}✗ going up — plan is getting smaller. NOT working.{reset}")
    print()

    # Per-model breakdown
    print("    BY MODEL (this week)")
    for bucket in ("opus", "sonnet", "haiku"):
        m = this_week["by_model"].get(bucket)
        if m:
            pct_pool = 100 * m["pool"] / max(this_pool, 1)
            print(f"      {bucket:<8} {m['turns']:>6,} turns   full_rate={_fmt(m['full_rate']):>8}   pool={_fmt(m['pool']):>8}   {pct_pool:>5.1f}%")
        else:
            print(f"      {bucket:<8}      0 turns")
    print()

    # Proxy contribution last 24h
    if PROXY_TRACE.exists():
        try:
            cutoff = now - 86400
            cc_saved = 0
            turns = 0
            routed = 0
            for line in PROXY_TRACE.open():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("ts", 0) < cutoff or "usage" not in d:
                    continue
                u = d["usage"]
                turns += 1
                cc_saved += max(0, 15000 - u.get("cache_create", 0))
                if d.get("req", {}).get("routed_haiku"):
                    routed += 1
            if turns:
                print(f"    PROXY (last 24h): {turns} turns, {routed} routed to Haiku, ~{_fmt(cc_saved)} cache_creation avoided")
                print()
        except Exception:
            pass

    # Target
    target_pct = -40
    print(f"    TARGET: -40% week-over-week. You are at {pct:+.1f}%.")
    if pct <= target_pct:
        print(f"    {color}✓ hitting target.{reset}")
    else:
        gap = abs(pct - target_pct)
        print(f"    gap to target: {gap:.1f} percentage points.")
    print()


if __name__ == "__main__":
    main()
