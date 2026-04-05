#!/usr/bin/env python3
"""Token Saver report — CLI dashboard showing token savings over time.

Usage:
  python -m openkeel.token_saver.report              # summary
  python -m openkeel.token_saver.report --events      # recent events
  python -m openkeel.token_saver.report --daily        # daily breakdown
  python -m openkeel.token_saver.report --live         # live tail (watch mode)
  python -m openkeel.token_saver.report --export       # CSV export
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".openkeel" / "token_ledger.db"
CHARS_PER_TOKEN = 4

# Real pricing from pricing.py
try:
    from openkeel.token_saver.pricing import MODELS, estimate_savings, format_pricing_table
    COST_PER_1K_INPUT_OPUS = MODELS["claude-opus-4"].input_per_1m / 1000
    COST_PER_1K_INPUT_SONNET = MODELS["claude-sonnet-4"].input_per_1m / 1000
    _HAS_PRICING = True
except ImportError:
    COST_PER_1K_INPUT_OPUS = 0.015
    COST_PER_1K_INPUT_SONNET = 0.003
    _HAS_PRICING = False


def _get_db() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        return sqlite3.connect(str(DB_PATH), timeout=5)
    except Exception:
        return None


def cmd_summary() -> str:
    """Full summary: session, all-time, and estimated cost savings."""
    conn = _get_db()
    if not conn:
        return "No token ledger found. Start the token saver daemon first."

    lines = []
    lines.append("")
    lines.append("=" * 62)
    lines.append("  TOKEN SAVER — SAVINGS REPORT")
    lines.append("=" * 62)

    # All-time stats
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(original_chars),0), COALESCE(SUM(saved_chars),0), "
        "COUNT(DISTINCT session_id), MIN(timestamp), MAX(timestamp) FROM savings"
    ).fetchone()
    events, orig_chars, saved_chars, sessions, first_ts, last_ts = row

    if events == 0:
        lines.append("\n  No events recorded yet. Use Claude Code with the token saver running.")
        return "\n".join(lines)

    orig_tokens = orig_chars // CHARS_PER_TOKEN
    saved_tokens = saved_chars // CHARS_PER_TOKEN
    pct = round(saved_chars / orig_chars * 100, 1) if orig_chars else 0

    first_dt = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if first_ts else "?"
    last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if last_ts else "?"

    lines.append(f"\n  ALL TIME ({first_dt} — {last_dt})")
    lines.append(f"  {'Sessions:':<20} {sessions}")
    lines.append(f"  {'Events:':<20} {events}")
    lines.append(f"  {'Tokens processed:':<20} {orig_tokens:,}")
    lines.append(f"  {'Tokens saved:':<20} {saved_tokens:,}")
    lines.append(f"  {'Savings rate:':<20} {pct}%")

    if _HAS_PRICING:
        savings = estimate_savings(saved_tokens)
        lines.append(f"\n  ESTIMATED COST SAVED (input tokens avoided):")
        for model_name, cost in sorted(savings.items(), key=lambda x: -x[1]):
            lines.append(f"    {model_name:<30} ${cost:.4f}")
    else:
        lines.append(f"  {'Est. cost saved:':<20} ${saved_tokens * COST_PER_1K_INPUT_OPUS / 1000:.4f} (Opus) / ${saved_tokens * COST_PER_1K_INPUT_SONNET / 1000:.4f} (Sonnet)")

    # Separate actual interceptions from tracking-only events
    actual_types = {"cache_hit", "command_rewrite", "bash_compress"}
    actual_row = conn.execute(
        "SELECT COALESCE(SUM(saved_chars),0) FROM savings WHERE event_type IN ('cache_hit','command_rewrite','bash_compress')"
    ).fetchone()
    actual_saved = (actual_row[0] if actual_row else 0) // CHARS_PER_TOKEN

    lines.append(f"\n  ACTUAL SAVINGS (pre-tool interceptions that reduced context):")
    lines.append(f"    Tokens saved by blocking/rewriting: {actual_saved:,}")
    lines.append(f"  TRACKING ONLY (measured but not intercepted — PostToolUse can't modify output):")
    lines.append(f"    Tokens that could be saved with pre-tool filters: {saved_tokens - actual_saved:,}")

    # Breakdown by event type
    lines.append(f"\n  BY EVENT TYPE")
    lines.append(f"  {'Type':<24} {'Count':>8} {'Saved tokens':>14} {'Avg saved':>12}")
    lines.append(f"  {'-'*24} {'-'*8} {'-'*14} {'-'*12}")

    rows = conn.execute(
        "SELECT event_type, COUNT(*), COALESCE(SUM(saved_chars),0), "
        "COALESCE(AVG(saved_chars),0) FROM savings GROUP BY event_type ORDER BY SUM(saved_chars) DESC"
    ).fetchall()
    for etype, cnt, total_saved, avg_saved in rows:
        tag = " *" if etype in actual_types else ""
        lines.append(
            f"  {etype + tag:<24} {cnt:>8} {total_saved // CHARS_PER_TOKEN:>14,} {int(avg_saved) // CHARS_PER_TOKEN:>12,}"
        )

    # Top files by savings
    lines.append(f"\n  TOP FILES (most tokens saved)")
    lines.append(f"  {'File':<45} {'Hits':>6} {'Saved':>12}")
    lines.append(f"  {'-'*45} {'-'*6} {'-'*12}")

    rows = conn.execute(
        "SELECT file_path, COUNT(*), COALESCE(SUM(saved_chars),0) FROM savings "
        "WHERE file_path != '' AND saved_chars > 0 "
        "GROUP BY file_path ORDER BY SUM(saved_chars) DESC LIMIT 10"
    ).fetchall()
    for fpath, cnt, total_saved in rows:
        short = os.path.basename(fpath) if len(fpath) > 45 else fpath
        lines.append(f"  {short:<45} {cnt:>6} {total_saved // CHARS_PER_TOKEN:>12,}")

    conn.close()
    lines.append("")
    lines.append("=" * 62)
    return "\n".join(lines)


def cmd_events(limit: int = 30) -> str:
    """Show recent events."""
    conn = _get_db()
    if not conn:
        return "No ledger found."

    rows = conn.execute(
        "SELECT timestamp, event_type, tool_name, file_path, original_chars, saved_chars, notes "
        "FROM savings ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        return "No events recorded."

    lines = []
    lines.append("")
    lines.append(f"  {'Time':<20} {'Type':<14} {'Tool':<8} {'Orig':>8} {'Saved':>8} {'File/Notes'}")
    lines.append(f"  {'-'*20} {'-'*14} {'-'*8} {'-'*8} {'-'*8} {'-'*30}")

    for ts, etype, tool, fpath, orig, saved, notes in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        orig_tok = orig // CHARS_PER_TOKEN
        saved_tok = saved // CHARS_PER_TOKEN
        detail = os.path.basename(fpath) if fpath else (notes[:30] if notes else "")
        lines.append(f"  {dt:<20} {etype:<14} {tool:<8} {orig_tok:>8} {saved_tok:>8} {detail}")

    return "\n".join(lines)


def cmd_daily() -> str:
    """Show daily breakdown."""
    conn = _get_db()
    if not conn:
        return "No ledger found."

    rows = conn.execute(
        "SELECT date(timestamp, 'unixepoch') as day, "
        "COUNT(*), COALESCE(SUM(original_chars),0), COALESCE(SUM(saved_chars),0), "
        "COUNT(DISTINCT session_id) "
        "FROM savings GROUP BY day ORDER BY day DESC LIMIT 30"
    ).fetchall()
    conn.close()

    if not rows:
        return "No events recorded."

    lines = []
    lines.append("")
    lines.append(f"  {'Date':<12} {'Sessions':>10} {'Events':>8} {'Processed':>12} {'Saved':>12} {'Rate':>8} {'Est. $':>10}")
    lines.append(f"  {'-'*12} {'-'*10} {'-'*8} {'-'*12} {'-'*12} {'-'*8} {'-'*10}")

    cost_per_token = COST_PER_1K_INPUT_SONNET / 1000  # Sonnet as default estimate
    total_saved = 0
    for day, events, orig, saved, sessions in rows:
        orig_tok = orig // CHARS_PER_TOKEN
        saved_tok = saved // CHARS_PER_TOKEN
        pct = round(saved / orig * 100, 1) if orig else 0
        cost = saved_tok * cost_per_token
        total_saved += saved_tok
        lines.append(
            f"  {day:<12} {sessions:>10} {events:>8} {orig_tok:>12,} {saved_tok:>12,} {pct:>7.1f}% ${cost:>8.4f}"
        )

    lines.append(f"  {'-'*12} {'-'*10} {'-'*8} {'-'*12} {'-'*12} {'-'*8} {'-'*10}")
    total_cost = total_saved * cost_per_token
    lines.append(f"  {'TOTAL':<12} {'':>10} {'':>8} {'':>12} {total_saved:>12,} {'':>8} ${total_cost:>8.4f}")

    return "\n".join(lines)


def cmd_live() -> str:
    """Live tail — watch savings events as they happen."""
    conn = _get_db()
    if not conn:
        print("No ledger found.")
        return ""

    print("\n  TOKEN SAVER — LIVE TAIL (Ctrl+C to stop)")
    print(f"  {'Time':<20} {'Type':<14} {'Saved':>8} {'Detail'}")
    print(f"  {'-'*20} {'-'*14} {'-'*8} {'-'*30}")

    last_id = ""
    row = conn.execute("SELECT MAX(id) FROM savings").fetchone()
    if row and row[0]:
        last_id = row[0]
    conn.close()

    try:
        while True:
            time.sleep(2)
            conn = _get_db()
            if not conn:
                continue
            rows = conn.execute(
                "SELECT id, timestamp, event_type, saved_chars, file_path, notes "
                "FROM savings WHERE id > ? ORDER BY timestamp",
                (last_id,),
            ).fetchall()
            conn.close()

            for rid, ts, etype, saved, fpath, notes in rows:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
                saved_tok = saved // CHARS_PER_TOKEN
                detail = os.path.basename(fpath) if fpath else (notes[:30] if notes else "")
                marker = f"+{saved_tok:,} tokens" if saved_tok > 0 else "tracked"
                print(f"  {dt:<20} {etype:<14} {marker:>8} {detail}")
                last_id = rid
    except KeyboardInterrupt:
        print("\n  Stopped.")
    return ""


def cmd_export() -> str:
    """Export all events as CSV."""
    conn = _get_db()
    if not conn:
        return "No ledger found."

    rows = conn.execute(
        "SELECT session_id, timestamp, event_type, tool_name, file_path, "
        "original_chars, saved_chars, notes FROM savings ORDER BY timestamp"
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "session_id", "timestamp", "datetime", "event_type", "tool_name",
        "file_path", "original_chars", "saved_chars", "original_tokens",
        "saved_tokens", "est_cost_saved_opus", "notes",
    ])
    for sid, ts, etype, tool, fpath, orig, saved, notes in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        orig_tok = orig // CHARS_PER_TOKEN
        saved_tok = saved // CHARS_PER_TOKEN
        cost = saved_tok * COST_PER_1K_INPUT_OPUS / 1000
        writer.writerow([sid, ts, dt, etype, tool, fpath, orig, saved, orig_tok, saved_tok, f"{cost:.6f}", notes])

    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description="Token Saver — savings report")
    parser.add_argument("--events", action="store_true", help="Show recent events")
    parser.add_argument("--daily", action="store_true", help="Daily breakdown")
    parser.add_argument("--live", action="store_true", help="Live tail (watch mode)")
    parser.add_argument("--export", action="store_true", help="CSV export to stdout")
    parser.add_argument("--pricing", action="store_true", help="Show model pricing table")
    parser.add_argument("--limit", type=int, default=30, help="Max events to show")
    args = parser.parse_args()

    if args.pricing:
        if _HAS_PRICING:
            print(format_pricing_table())
        else:
            print("Pricing module not available.")
    elif args.live:
        cmd_live()
    elif args.events:
        print(cmd_events(args.limit))
    elif args.daily:
        print(cmd_daily())
    elif args.export:
        print(cmd_export())
    else:
        print(cmd_summary())


if __name__ == "__main__":
    main()
