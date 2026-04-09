"""
Token Saver v5 — ground-truth billed-token tracker.

WHY THIS EXISTS:
    The existing `savings` table measures tool-output volume reduction — which
    is honest on what it measures (48% as of 2026-04-07) but only covers a
    slice of what Claude actually bills. A typical turn looks like:

        system + tool defs       ~6-10K   (ledger: invisible)
        cached conversation      20-100K  (ledger: invisible)
        user message             50-1K    (ledger: invisible)
        tool outputs this turn    5-20K   (ledger: THIS is what's tracked)
        claude output             2-8K    (ledger: invisible)
        hook chatter               .5-2K   (ledger: token saver ADDS these!)

    So the dashboard can honestly report 48% savings while the user burns 1.5%
    of their weekly quota per hour. Both are true — different layers.

    This module reads Claude Code's transcript .jsonl files and records the
    EXACT token usage each assistant turn billed, via the `usage` field the
    API returns on every response. That gives a ground-truth number the
    dashboard can show alongside the interception-rate metric.

SCHEMA:
    New table `billed_tokens` in the existing ~/.openkeel/token_ledger.db —
    sibling to `savings`, never touches existing data. Primary key on the
    turn's uuid so backfill + live recording are both idempotent.

ENTRY POINTS:
    ensure_schema(conn)        — CREATE TABLE IF NOT EXISTS
    record_turn(...)           — insert one row (idempotent)
    parse_transcript(path)     — yield usage dicts from a .jsonl
    backfill(glob_pattern)     — scan all transcripts under a dir
    summarize(since_ts)        — aggregate for reporting
    process_stop_hook(stdin)   — orchestrator called by stop.py hook
"""

from __future__ import annotations

import glob
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import CFG, ensure_dirs
from .debug_log import note, swallow


SCHEMA = """
CREATE TABLE IF NOT EXISTS billed_tokens (
    turn_uuid       TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    cache_creation  INTEGER DEFAULT 0,
    cache_read      INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    total_billed    INTEGER DEFAULT 0,
    model           TEXT,
    transcript_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_billed_ts ON billed_tokens(timestamp);
CREATE INDEX IF NOT EXISTS idx_billed_session ON billed_tokens(session_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CFG.ledger_db), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # fixes kanban #267 along the way
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """Add the billed_tokens table next to the existing savings table."""
    close_after = False
    if conn is None:
        conn = _connect()
        close_after = True
    try:
        for stmt in SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                conn.execute(s)
        conn.commit()
    finally:
        if close_after:
            conn.close()


def _iso_to_epoch(iso: str) -> float:
    """Transcript timestamps are ISO8601 with Z suffix."""
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def parse_transcript(path: str) -> Iterator[dict[str, Any]]:
    """
    Yield one dict per assistant turn that has a usage field.

    Each dict contains:
        turn_uuid, session_id, timestamp, input_tokens, cache_creation,
        cache_read, output_tokens, total_billed, model, transcript_path

    NOTE: model is corrected from proxy_trace.jsonl if Opus was routed down.
    """
    # Load proxy routing for Opus→Sonnet/Haiku corrections
    proxy_models = {}
    try:
        from pathlib import Path
        proxy_file = Path.home() / ".openkeel" / "proxy_trace.jsonl"
        if proxy_file.exists():
            with open(proxy_file) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        ts = d.get("ts", 0)
                        req = d.get("req", {}) or {}
                        routed = req.get("routed_model")
                        if routed and ts:
                            proxy_models[int(ts)] = routed
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                msg = row.get("message")
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                uid = row.get("uuid") or msg.get("id") or ""
                if not uid:
                    continue
                session_id = row.get("sessionId") or ""
                ts_iso = row.get("timestamp") or ""
                ts = _iso_to_epoch(ts_iso) if ts_iso else 0.0
                model = msg.get("model") or ""
                # Correct Opus if proxy routed it down
                if "opus" in model.lower() and int(ts) in proxy_models:
                    model = proxy_models[int(ts)]
                inp = int(usage.get("input_tokens") or 0)
                cc = int(usage.get("cache_creation_input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                total = inp + cc + cr + out
                yield {
                    "turn_uuid": uid,
                    "session_id": session_id,
                    "timestamp": ts,
                    "input_tokens": inp,
                    "cache_creation": cc,
                    "cache_read": cr,
                    "output_tokens": out,
                    "total_billed": total,
                    "model": model,
                    "transcript_path": path,
                }
    except FileNotFoundError:
        return
    except Exception as e:
        swallow("billed_tracker.parse_transcript", error=e,
                extra={"path": path})


def record_rows(rows: list[dict[str, Any]]) -> int:
    """
    Insert rows idempotently (PRIMARY KEY on turn_uuid → ON CONFLICT IGNORE).
    Returns count of newly-inserted rows.
    """
    if not rows:
        return 0
    ensure_dirs()
    ensure_schema()
    conn = _connect()
    try:
        cur = conn.cursor()
        new_count = 0
        for r in rows:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO billed_tokens "
                    "(turn_uuid, session_id, timestamp, input_tokens, "
                    " cache_creation, cache_read, output_tokens, total_billed, "
                    " model, transcript_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        r["turn_uuid"], r["session_id"], r["timestamp"],
                        r["input_tokens"], r["cache_creation"], r["cache_read"],
                        r["output_tokens"], r["total_billed"],
                        r["model"], r["transcript_path"],
                    ),
                )
                if cur.rowcount:
                    new_count += 1
            except Exception as e:
                swallow("billed_tracker.insert_row", error=e,
                        extra={"uuid": r.get("turn_uuid")})
        conn.commit()
        return new_count
    finally:
        conn.close()


def backfill(transcript_dir: str | None = None) -> dict[str, int]:
    """
    Scan all *.jsonl in transcript_dir (default ~/.claude/projects/*/) and
    populate billed_tokens. Idempotent: existing rows are not touched.
    Returns {files_scanned, turns_found, rows_inserted}.
    """
    if transcript_dir is None:
        transcript_dir = str(Path.home() / ".claude" / "projects")
    patterns = [
        f"{transcript_dir}/*/*.jsonl",
        f"{transcript_dir}/*.jsonl",
    ]
    files: list[str] = []
    for p in patterns:
        files.extend(glob.glob(p))
    files = sorted(set(files))

    turns_total = 0
    inserted_total = 0
    for path in files:
        rows = list(parse_transcript(path))
        turns_total += len(rows)
        inserted_total += record_rows(rows)

    note("billed_tracker.backfill",
         f"scanned={len(files)} turns={turns_total} inserted={inserted_total}")
    return {
        "files_scanned": len(files),
        "turns_found": turns_total,
        "rows_inserted": inserted_total,
    }


def process_stop_hook(stdin_json: dict[str, Any]) -> None:
    """
    Called from hooks/stop.py after each assistant response.
    Reads the transcript path from the hook payload and records any new
    turns that aren't already in the DB.
    """
    path = stdin_json.get("transcript_path") or ""
    if not path:
        note("billed_tracker.stop_hook", "missing transcript_path")
        return
    try:
        rows = list(parse_transcript(path))
        inserted = record_rows(rows)
        note("billed_tracker.stop_hook",
             f"turns={len(rows)} new={inserted}",
             session=stdin_json.get("session_id", ""))
    except Exception as e:
        swallow("billed_tracker.stop_hook", error=e,
                extra={"path": path})


def summarize(since_ts: float | None = None, hours: float | None = None) -> dict[str, Any]:
    """
    Aggregate billed_tokens. If `hours` is set, compute stats for the last N
    hours. Otherwise use `since_ts` or lifetime.

    Returns a dict with totals + per-model breakdown + hourly rate + session
    count.
    """
    ensure_schema()
    conn = _connect()
    try:
        cur = conn.cursor()
        if hours is not None:
            since_ts = time.time() - hours * 3600
        where = ""
        params: tuple = ()
        if since_ts is not None:
            where = "WHERE timestamp > ?"
            params = (since_ts,)

        cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT session_id), "
            f"  SUM(input_tokens), SUM(cache_creation), SUM(cache_read), "
            f"  SUM(output_tokens), SUM(total_billed), "
            f"  MIN(timestamp), MAX(timestamp) "
            f"FROM billed_tokens {where}",
            params,
        )
        row = cur.fetchone()
        turns, sessions, inp, cc, cr, out, total, t_min, t_max = row
        turns = turns or 0; sessions = sessions or 0
        inp = inp or 0; cc = cc or 0; cr = cr or 0
        out = out or 0; total = total or 0
        t_min = t_min or 0; t_max = t_max or 0
        elapsed_h = (t_max - t_min) / 3600 if t_min and t_max else 0

        # Per-model breakdown
        cur.execute(
            f"SELECT model, COUNT(*), SUM(total_billed) "
            f"FROM billed_tokens {where} "
            f"GROUP BY model ORDER BY SUM(total_billed) DESC",
            params,
        )
        models = [
            {"model": m or "(unknown)", "turns": c, "total_billed": t or 0}
            for m, c, t in cur.fetchall()
        ]

        return {
            "turns": turns,
            "sessions": sessions,
            "input_tokens": inp,
            "cache_creation_tokens": cc,
            "cache_read_tokens": cr,
            "output_tokens": out,
            "total_billed": total,
            "elapsed_hours": round(elapsed_h, 2),
            "tokens_per_hour": round(total / elapsed_h, 0) if elapsed_h else 0,
            "since": t_min,
            "until": t_max,
            "models": models,
        }
    finally:
        conn.close()


# --- CLI -----------------------------------------------------------------

def _cli_report(hours: float | None = None) -> None:
    stats = summarize(hours=hours)
    label = f"last {hours}h" if hours else "lifetime"
    print(f"\n=== billed tokens ({label}) ===")
    print(f"  turns            {stats['turns']:>14,}")
    print(f"  sessions         {stats['sessions']:>14,}")
    print(f"  input_tokens     {stats['input_tokens']:>14,}")
    print(f"  cache_creation   {stats['cache_creation_tokens']:>14,}")
    print(f"  cache_read       {stats['cache_read_tokens']:>14,}")
    print(f"  output_tokens    {stats['output_tokens']:>14,}")
    print(f"  {'—'*30}")
    print(f"  total_billed     {stats['total_billed']:>14,}")
    print(f"  elapsed_hours    {stats['elapsed_hours']:>14.2f}")
    print(f"  tokens/hour      {stats['tokens_per_hour']:>14,.0f}")
    if stats["models"]:
        print(f"\n  per model:")
        for m in stats["models"][:5]:
            print(f"    {m['model'][:40]:<40}  {m['turns']:>6} turns  {m['total_billed']:>14,} tok")


def _cli_backfill() -> None:
    print("Scanning ~/.claude/projects/ for .jsonl transcripts...")
    result = backfill()
    print(f"  files scanned: {result['files_scanned']}")
    print(f"  turns found:   {result['turns_found']}")
    print(f"  rows inserted: {result['rows_inserted']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: python -m openkeel.token_saver_v5.billed_tracker "
              "[backfill | report [hours] | init]")
        sys.exit(0)
    cmd = args[0]
    if cmd == "init":
        ensure_schema()
        print("schema ready")
    elif cmd == "backfill":
        _cli_backfill()
    elif cmd == "report":
        h = float(args[1]) if len(args) > 1 else None
        _cli_report(hours=h)
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)
