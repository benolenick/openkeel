"""Token Saver v4 benchmark harness.

Reads real events from ~/.openkeel/token_ledger.db, replays file_read,
grep_output, file_write, bash_output, and session_start blobs through
the v4 lingua_compressor, and reports additional savings on top of what
v3 already caught.

Does NOT call live APIs. Pure measurement on historical data.

Usage:
    python -m openkeel.token_saver_v4.bench [--limit N] [--event-type X]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# Make import work whether run as module or script
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from openkeel.token_saver_v4.engines import lingua_compressor
from openkeel.token_saver_v4.engines import subagent_offload

LEDGER = Path(os.path.expanduser("~/.openkeel/token_ledger.db"))

# Events worth compressing — ones where v3 currently records 0 or partial savings
TARGET_EVENTS = (
    "file_read",
    "grep_output",
    "file_write",
    "bash_output",
    "glob_output",
    "session_start",
)


def fetch_samples(limit_per_event: int = 200) -> dict[str, list[dict]]:
    """Pull a sample of notes for each target event type.

    The ledger doesn't store full blob contents (only char counts), so we
    synthesize realistic blobs from the `notes` field where available and
    fall back to a char-count-only measurement for events without notes.
    """
    conn = sqlite3.connect(str(LEDGER))
    samples: dict[str, list[dict]] = {}
    for evt in TARGET_EVENTS:
        rows = conn.execute(
            "SELECT original_chars, saved_chars, notes, file_path "
            "FROM savings WHERE event_type = ? "
            "AND original_chars > 0 "
            "ORDER BY original_chars DESC LIMIT ?",
            (evt, limit_per_event),
        ).fetchall()
        samples[evt] = [
            {
                "original_chars": r[0],
                "saved_chars_v3": r[1] or 0,
                "notes": r[2] or "",
                "file_path": r[3] or "",
            }
            for r in rows
        ]
    conn.close()
    return samples


def _build_probe_blob(sample: dict) -> str:
    """Build a test blob from real sources to feed into the v4 compressor.

    Strategy: if file_path exists and we can read it, use real content.
    Otherwise synthesize a representative blob from notes (capped).
    """
    fp = sample.get("file_path")
    if fp and os.path.exists(fp):
        try:
            with open(fp, "r", errors="replace") as f:
                content = f.read(sample["original_chars"] + 1000)
            if len(content) >= 200:
                return content
        except Exception:
            pass
    # Fallback: inflate notes to original_chars with repetition of a known
    # boilerplate pattern. Not ideal but measurable.
    notes = sample.get("notes") or "log line"
    target = max(500, sample["original_chars"])
    pad = (notes + "\n") * (1 + target // max(1, len(notes)))
    return pad[:target]


def bench_lingua() -> dict:
    samples = fetch_samples()
    totals = {
        "events": 0,
        "original_chars": 0,
        "v3_saved_chars": 0,
        "v4_additional_chars": 0,
        "by_event": {},
    }
    for evt, rows in samples.items():
        ev_total = {
            "count": len(rows),
            "original": 0,
            "v3_saved": 0,
            "v4_additional": 0,
            "samples_tested": 0,
        }
        for sample in rows[:50]:  # cap work
            blob = _build_probe_blob(sample)
            if len(blob) < lingua_compressor.MIN_CHARS:
                continue
            # Simulate v3 state: the blob after v3 already ran
            # (v3 savings were already applied; v4 operates on what remains)
            v3_remaining = max(
                lingua_compressor.MIN_CHARS,
                len(blob) - sample["saved_chars_v3"],
            )
            v3_blob = blob[:v3_remaining] if v3_remaining < len(blob) else blob

            result = lingua_compressor.compress(v3_blob)
            ev_total["samples_tested"] += 1
            ev_total["original"] += sample["original_chars"]
            ev_total["v3_saved"] += sample["saved_chars_v3"]
            ev_total["v4_additional"] += result.saved_chars

        totals["events"] += ev_total["count"]
        totals["original_chars"] += ev_total["original"]
        totals["v3_saved_chars"] += ev_total["v3_saved"]
        totals["v4_additional_chars"] += ev_total["v4_additional"]
        totals["by_event"][evt] = ev_total
    return totals


def bench_subagent_offload() -> dict:
    """Replay session tool sequences from the ledger and count nudge opportunities."""
    if not LEDGER.exists():
        return {"error": "no ledger"}
    conn = sqlite3.connect(str(LEDGER))
    rows = conn.execute(
        "SELECT session_id, timestamp, event_type "
        "FROM savings ORDER BY session_id, timestamp"
    ).fetchall()
    conn.close()

    # Map event_type back to tool names (approximate)
    evt_to_tool = {
        "file_read": "Read",
        "grep_output": "Grep",
        "glob_output": "Glob",
        "file_edit": "Edit",
        "file_write": "Write",
        "bash_output": "Bash",
    }

    sessions: dict[str, list[str]] = {}
    for sid, _ts, evt in rows:
        tool = evt_to_tool.get(evt)
        if tool:
            sessions.setdefault(sid, []).append(tool)

    nudges = 0
    total_chains = 0
    for sid, tools in sessions.items():
        # Slide through the session, firing nudges with cooldown
        events_since = 999
        for i in range(len(tools)):
            decision = subagent_offload.evaluate(
                tools[: i + 1], events_since_last_nudge=events_since
            )
            events_since += 1
            if decision.should_nudge:
                nudges += 1
                total_chains += decision.chain_len
                events_since = 0
    return {
        "sessions_analyzed": len(sessions),
        "nudges_fired": nudges,
        "avg_chain_len": round(total_chains / nudges, 2) if nudges else 0,
        "avg_nudges_per_session": round(nudges / max(1, len(sessions)), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("=" * 60)
    print("TOKEN SAVER v4 — BENCHMARK")
    print("=" * 60)
    print(f"ledger: {LEDGER}")
    print()

    t0 = time.time()
    lingua = bench_lingua()
    t1 = time.time()

    print(f"[lingua] ran in {t1 - t0:.1f}s")
    print()
    print("LINGUA COMPRESSOR (additional savings on top of v3)")
    print("-" * 60)
    print(f"{'event_type':<20} {'samples':>8} {'orig_chars':>12} "
          f"{'v4_add':>10} {'ratio':>7}")
    for evt, s in lingua["by_event"].items():
        ratio = 0.0
        if s["original"]:
            ratio = s["v4_additional"] / s["original"] * 100
        print(f"{evt:<20} {s['samples_tested']:>8} "
              f"{s['original']:>12,} {s['v4_additional']:>10,} "
              f"{ratio:>6.1f}%")
    print("-" * 60)
    total_ratio = 0.0
    if lingua["original_chars"]:
        total_ratio = lingua["v4_additional_chars"] / lingua["original_chars"] * 100
    print(f"{'TOTAL':<20} {lingua['events']:>8} "
          f"{lingua['original_chars']:>12,} "
          f"{lingua['v4_additional_chars']:>10,} "
          f"{total_ratio:>6.1f}%")
    print()

    offload = bench_subagent_offload()
    print("SUBAGENT OFFLOAD (nudge opportunities on historical sessions)")
    print("-" * 60)
    for k, v in offload.items():
        print(f"  {k:<28} {v}")
    print()

    # Combined projection
    v3_total = lingua["v3_saved_chars"]
    v4_add = lingua["v4_additional_chars"]
    orig = lingua["original_chars"]
    if orig:
        print(f"PROJECTED combined savings on replayed events:")
        print(f"  v3 alone:  {v3_total:>10,} / {orig:,}  "
              f"({v3_total / orig * 100:.1f}%)")
        print(f"  v3 + v4:   {v3_total + v4_add:>10,} / {orig:,}  "
              f"({(v3_total + v4_add) / orig * 100:.1f}%)")
        print(f"  v4 delta:  +{v4_add / orig * 100:.1f} percentage points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
