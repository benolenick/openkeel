"""Week-end analysis — runs after a week of real traffic through the v6 stack.

Usage: python3 -m openkeel.token_saver.week_report
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

DB = Path.home() / ".openkeel" / "token_ledger.db"
TRACE = Path.home() / ".openkeel" / "proxy_trace.jsonl"
MODEL_WEIGHTS = {"opus": 1.0, "sonnet": 0.20, "haiku": 0.04}


def _bucket(m):
    m = (m or "").lower()
    if "opus" in m: return "opus"
    if "sonnet" in m: return "sonnet"
    if "haiku" in m: return "haiku"
    return "opus"


def _fmt(n):
    n = int(n)
    if n >= 1e9: return f"{n/1e9:.2f}B"
    if n >= 1e6: return f"{n/1e6:.2f}M"
    if n >= 1e3: return f"{n/1e3:.1f}K"
    return str(n)


def main():
    now = time.time()
    week = 7 * 86400

    print("=" * 68)
    print("  TOKEN SAVER v6 — WEEK-END REPORT")
    print("=" * 68)
    print()

    # 1) Pool units week over week
    if DB.exists():
        conn = sqlite3.connect(str(DB), timeout=5)
        def pool(a, b):
            rows = conn.execute(
                "SELECT model, SUM(cache_creation), SUM(output_tokens), SUM(input_tokens), SUM(cache_read), COUNT(*) "
                "FROM billed_tokens WHERE timestamp BETWEEN ? AND ? GROUP BY model",
                (a, b)).fetchall()
            p = 0.0; turns = 0; by = defaultdict(lambda: {"turns":0,"pool":0.0,"full":0})
            for model, cc, out, in_, cr, t in rows:
                bk = _bucket(model); w = MODEL_WEIGHTS[bk]
                full = (cc or 0) + (out or 0) + (in_ or 0)
                pt = (full + (cr or 0) * 0.1) * w
                p += pt; turns += t
                by[bk]["turns"] += t; by[bk]["pool"] += pt; by[bk]["full"] += full
            return p, turns, dict(by)

        this_p, this_t, this_by = pool(now - week, now)
        last_p, last_t, last_by = pool(now - 2*week, now - week)
        delta = this_p - last_p
        pct = (100 * delta / last_p) if last_p else 0

        print("  POOL UNITS")
        print(f"    this week: {_fmt(this_p):>10}   ({this_t:,} turns)")
        print(f"    last week: {_fmt(last_p):>10}   ({last_t:,} turns)")
        print(f"    delta:     {'▼' if delta<0 else '▲'} {_fmt(abs(delta)):>8}   ({pct:+.1f}%)")
        print(f"    target:    -40%   {'✓ HIT' if pct <= -40 else 'miss'}")
        print()
        print("  BY MODEL (this week)")
        for bk in ("opus", "sonnet", "haiku"):
            m = this_by.get(bk, {"turns":0,"pool":0,"full":0})
            share = 100 * m["pool"] / max(this_p, 1)
            print(f"    {bk:<7} {m['turns']:>7,} turns   full={_fmt(m['full']):>8}   pool={_fmt(m['pool']):>8}   {share:5.1f}%")
        print()
        conn.close()

    # 2) Proxy routing decisions
    if TRACE.exists():
        decisions = []
        with TRACE.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("ts", 0) < now - week: continue
                if "usage" not in d: continue
                decisions.append(d)

        if decisions:
            sources = Counter()
            routed_models = Counter()
            confs = Counter()
            total_latency_qwen = 0
            qwen_calls = 0
            cc_saved = 0

            for d in decisions:
                r = d.get("req", {})
                rd = r.get("route_decision") or {}
                sources[rd.get("source", "none")] += 1
                routed_models[r.get("routed_model") or "kept_opus"] += 1
                if rd.get("confidence"):
                    confs[rd["confidence"]] += 1
                if rd.get("qwen_latency_ms"):
                    total_latency_qwen += rd["qwen_latency_ms"]
                    qwen_calls += 1
                cc_saved += max(0, 15000 - d["usage"].get("cache_create", 0))

            print("  PROXY (last 7 days)")
            print(f"    total traced turns: {len(decisions):,}")
            print(f"    cache_creation avoided: ~{_fmt(cc_saved)}")
            print()
            print("    routing source breakdown:")
            for s, n in sources.most_common():
                print(f"      {s:<24} {n}")
            print()
            print("    routed_model distribution:")
            for m, n in routed_models.most_common():
                pct = 100 * n / len(decisions)
                print(f"      {m:<40} {n:>5}  ({pct:5.1f}%)")
            print()
            if confs:
                print("    qwen confidence:")
                for c, n in confs.most_common():
                    print(f"      {c:<8} {n}")
                print()
            if qwen_calls:
                print(f"    qwen avg latency: {total_latency_qwen // qwen_calls}ms ({qwen_calls} calls)")
                print()

    # 3) Hook-layer LLM firings (ledger)
    if DB.exists():
        conn = sqlite3.connect(str(DB), timeout=5)
        rows = conn.execute(
            "SELECT event_type, COUNT(*), SUM(saved_chars) FROM savings "
            "WHERE timestamp > ? GROUP BY event_type ORDER BY 3 DESC LIMIT 20",
            (now - week,)).fetchall()
        if rows:
            print("  HOOK LAYER (last 7 days, by event type)")
            for et, n, s in rows:
                print(f"    {et:<30} n={n:>5}   ~{_fmt((s or 0)//4):>8} tok saved (first-pass)")
            print()
        conn.close()

    print("=" * 68)
    print("  Run: python3 -m openkeel.token_saver.one_metric   for live view")
    print("=" * 68)


if __name__ == "__main__":
    main()
