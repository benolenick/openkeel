"""Weekly quota tracking for OpenKeel 2.0."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

QUOTA_FILE = Path.home() / ".openkeel2" / "quota.json"
SONNET_OEQ_PER_CALL = 2600  # output-equivalent tokens per Sonnet CLI call
DEFAULT_WEEKLY_LIMIT = 5_000_000  # OEQ tokens


def _load():
    """Load quota data, auto-reset on new week."""
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not QUOTA_FILE.exists():
        return _fresh()
    try:
        data = json.loads(QUOTA_FILE.read_text())
    except Exception:
        return _fresh()
    # Auto-reset if new week
    try:
        ws = datetime.strptime(data["week_start"], "%Y-%m-%d")
        if (datetime.now() - ws).days >= 7:
            return _fresh()
    except Exception:
        return _fresh()
    return data


def _fresh():
    data = {
        "week_start": datetime.now().strftime("%Y-%m-%d"),
        "weekly_limit": DEFAULT_WEEKLY_LIMIT,
        "runs": [],
        "sonnet_calls": 0,
        "haiku_calls": 0,
        "local_calls": 0,
        "oeq_used": 0,
        "wall_ms": 0,
    }
    _save(data)
    return data


def _save(data):
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(data, indent=2))


def log_run(config, sonnet_calls, haiku_calls=0, local_calls=0, wall_ms=0):
    """Log a bubble run to quota tracker."""
    data = _load()
    oeq = sonnet_calls * SONNET_OEQ_PER_CALL
    run = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "config": config,
        "sonnet_calls": sonnet_calls,
        "haiku_calls": haiku_calls,
        "local_calls": local_calls,
        "wall_ms": wall_ms,
        "oeq": oeq,
    }
    data["runs"].append(run)
    data["sonnet_calls"] += sonnet_calls
    data["haiku_calls"] += haiku_calls
    data["local_calls"] += local_calls
    data["oeq_used"] += oeq
    data["wall_ms"] += wall_ms
    _save(data)
    return data


def get_usage():
    """Return (oeq_used, weekly_limit, bph, runs_count, sonnet_calls)."""
    data = _load()
    used = data.get("oeq_used", 0)
    limit = data.get("weekly_limit", DEFAULT_WEEKLY_LIMIT)
    wall = data.get("wall_ms", 0)
    runs = data.get("runs", [])
    sonnet = data.get("sonnet_calls", 0)

    # Calculate BPH
    pct = (used / limit * 100) if limit > 0 else 0
    hours = wall / 3_600_000 if wall > 0 else 0
    bph = pct / hours if hours > 0 else 0

    return {
        "oeq_used": used,
        "weekly_limit": limit,
        "pct": pct,
        "bph": bph,
        "runs": len(runs),
        "sonnet_calls": sonnet,
        "haiku_calls": data.get("haiku_calls", 0),
        "local_calls": data.get("local_calls", 0),
        "week_start": data.get("week_start", ""),
    }


def set_limit(limit: int):
    data = _load()
    data["weekly_limit"] = limit
    _save(data)


def reset():
    _save(_fresh())
