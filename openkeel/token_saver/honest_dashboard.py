#!/usr/bin/env python3
"""Token Saver — Honest Monitor (v7.1, unified).

Single source of truth: ~/.openkeel/proxy_trace.jsonl (live, real-time).
Fallback for historical: ~/.openkeel/token_ledger.db `billed_tokens` (lag ok for weekly).

Everything shown here is either a direct read from the proxy trace, or a live
nvidia-smi snapshot. No counterfactuals, no "x28 plan" multipliers, no savings overlays.

Panels
------
  1. Pool units (7d vs prior 7d)          — proxy_trace.jsonl (live)
  2. Per-model bar breakdown (this week)  — proxy_trace.jsonl (live)
  3. Scrolling live meter                 — proxy_trace.jsonl (live), last N minutes,
                                            stacked by model (opus/sonnet/haiku/local)
  4. Proxy router (last 24h)              — proxy_trace.jsonl
  5. Local LLM status                     — savings table filtered to the
                                            known-honest local-LLM engines +
                                            live /api/ps
  6. Runner dials (Calcifer)              — proxy_trace.jsonl + Ollama /api/ps

Usage:
    python3 -m openkeel.token_saver.honest_dashboard
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
try:
    import urllib.request
except ImportError:
    urllib = None

DB_PATH = Path.home() / ".openkeel" / "token_ledger.db"
PROXY_TRACE = Path.home() / ".openkeel" / "proxy_trace.jsonl"
OLLAMA_URL = os.environ.get("TSPROXY_OLLAMA_URL", "http://192.168.0.224:11434")
JAGG_HOST = "om@192.168.0.224"

POLL_MS = 5000       # headline numbers
METER_MS = 2000      # scrolling meter
GPU_POLL_MS = 4000

WINDOW_W = 960
WINDOW_H = 900

# -- Colors --
BG = "#0b0b0c"
PANEL = "#141417"
GRID = "#22232a"
TEXT_DIM = "#666"
TEXT = "#b5b9c0"
TEXT_BRIGHT = "#e8e8ef"
GREEN = "#39d98a"
RED = "#ff5e5e"
YELLOW = "#e0c060"
CYAN = "#66d9ef"
OPUS_COLOR = "#c678dd"
SONNET_COLOR = "#61afef"
HAIKU_COLOR = "#98c379"
LOCAL_COLOR = "#777"
GPU_COLOR = "#ffb86c"

MODEL_WEIGHTS = {"opus": 1.0, "sonnet": 0.20, "haiku": 0.04, "local": 0.0}
MODEL_COLORS = {
    "opus": OPUS_COLOR,
    "sonnet": SONNET_COLOR,
    "haiku": HAIKU_COLOR,
    "local": LOCAL_COLOR,
}
MODEL_ORDER = ("opus", "sonnet", "haiku", "local")

# -- Runner registry (from Calcifer) --
RUNNERS = [
    # id              label           sub-label           accent      group
    ("opus",          "Opus",         "cloud · opus-4-6",       "#cc5555", "cloud"),
    ("sonnet",        "Sonnet",       "cloud · sonnet-4-6",     "#5588cc", "cloud"),
    ("haiku",         "Haiku",        "cloud · haiku-4-5",      "#55aa88", "cloud"),
    ("gemma4_small",  "gemma4·e2b",   "kaloth · RTX 3070",      "#FF6611", "local"),
    ("qwen25",        "qwen2.5·3b",   "jagg  · RTX 3090",       "#aa66ff", "local"),
    ("gemma4_large",  "gemma4·26b",   "jagg  · RTX 3090",       "#ffaa33", "local"),
]

# Proxy trace → runner mapping
CLOUD_MODEL_MAP = {
    "claude-opus-4-6":           "opus",
    "claude-sonnet-4-6":         "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-haiku-4-5":          "haiku",
}

# Ollama host → runner mapping
OLLAMA_HOSTS = {
    "kaloth": "http://127.0.0.1:11434",
    "jagg":   "http://192.168.0.224:11434",
}
OLLAMA_TOTAL_VRAM = {
    "kaloth": 8 * 1024**3,   # RTX 3070  ~8 GB
    "jagg":   24 * 1024**3,  # RTX 3090 ~24 GB
}
OLLAMA_MODEL_MAP = {
    ("kaloth", "gemma4:e2b"):   "gemma4_small",
    ("jagg",   "qwen2.5:3b"):   "qwen25",
    ("jagg",   "gemma4:26b"):   "gemma4_large",
}

# Dial rendering constants
DECAY_RATE   = 0.94   # per 500ms tick (half-life ~5.5s)
CLOUD_SPIKE  = 100.0  # spike on new proxy event
VRAM_SCALE   = 1.8    # stretch VRAM % so 30% shows ~55%
PINNED_FLOOR = 3.0    # permanently loaded models baseline

# Events that reflect real local-LLM work (not inflated v3 counterfactuals).
HONEST_LLM_EVENTS = {
    "bash_llm_summarize", "output_compress", "recall_rerank",
    "grep_llm_summarize", "large_file_compress", "webfetch_compress",
    "v4_semantic_skeleton", "goal_filter", "bash_predict",
    "working_set_block", "working_set_bash_block",
}

# -- Helpers --

def _model_bucket(m: str | None) -> str:
    m = (m or "").lower()
    if "opus" in m: return "opus"
    if "sonnet" in m: return "sonnet"
    if "haiku" in m: return "haiku"
    if "local" in m: return "local"
    return "opus"


def _fmt(n) -> str:
    n = int(n)
    if n >= 1_000_000_000: return f"{n/1e9:.2f}B"
    if n >= 1_000_000:     return f"{n/1e6:.2f}M"
    if n >= 1_000:         return f"{n/1000:.1f}K"
    return str(n)


# -- Runner usage state (thread-safe) --

class UsageState:
    """Thread-safe dict of runner_id → 0.0-100.0."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {r[0]: 0.0 for r in RUNNERS}
        self._last_spike = {r[0]: 0.0 for r in RUNNERS}

    def spike(self, runner_id: str, value: float = 100.0):
        with self._lock:
            if runner_id in self._values:
                self._values[runner_id] = min(100.0, max(self._values[runner_id], value))
                self._last_spike[runner_id] = time.time()

    def set_floor(self, runner_id: str, floor: float):
        """Set a floor (e.g. VRAM load) without overriding a higher spike."""
        with self._lock:
            if runner_id in self._values:
                age = time.time() - self._last_spike.get(runner_id, 0)
                if age > 3.0:  # only apply floor when spike has faded
                    self._values[runner_id] = max(self._values[runner_id], floor)

    def tick_decay(self):
        with self._lock:
            for rid in self._values:
                self._values[rid] *= DECAY_RATE

    def snapshot(self):
        with self._lock:
            return dict(self._values)


USAGE = UsageState()


def _value_color(v: float) -> str:
    """Green → orange → red gradient across 0-100. Returns hex color."""
    if v <= 40:
        t = v / 40
        r = int(68  + t * (255 - 68))
        g = int(210 - t * (140 - 130))
        b = int(120 - t * 80)
    elif v <= 70:
        t = (v - 40) / 30
        r = 255
        g = int(130 - t * 80)
        b = int(40  - t * 20)
    else:
        t = (v - 70) / 30
        r = 255
        g = int(50  - t * 30)
        b = 20
    return f"#{r:02x}{g:02x}{b:02x}"


def _cloud_monitor(stop: threading.Event):
    """Tail proxy_trace.jsonl and spike cloud dials on new entries."""
    try:
        fh = PROXY_TRACE.open("r")
        fh.seek(0, 2)  # start at end
    except OSError:
        return

    while not stop.is_set():
        line = fh.readline()
        if not line:
            time.sleep(0.15)
            continue
        try:
            entry = json.loads(line)
            model = (entry.get("req") or {}).get("routed_model", "")
            if not model:
                model = (entry.get("req") or {}).get("model", "")
            runner_id = CLOUD_MODEL_MAP.get(model)
            if runner_id:
                USAGE.spike(runner_id, CLOUD_SPIKE)
        except Exception:
            pass

    fh.close()


def _host_gpu_util(host_key: str) -> float:
    """Return live GPU utilization for the host that owns this runner."""
    if host_key == "kaloth":
        cmd = [
            "nvidia-smi",
            "--query-gpu=utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
    elif host_key == "jagg":
        cmd = [
            "ssh", "-o", "ConnectTimeout=2", "-o", "BatchMode=yes", "om@192.168.0.224",
            "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits",
        ]
    else:
        return 0.0

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2.5)
    except Exception:
        return 0.0
    if r.returncode != 0:
        return 0.0

    vals = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            vals.append(int(line))
    if not vals:
        return 0.0
    return float(max(vals))


def _local_monitor(stop: threading.Event):
    """Poll Ollama /api/ps on both hosts and set VRAM-based floor values."""
    while not stop.is_set():
        for host_key, base_url in OLLAMA_HOSTS.items():
            try:
                gpu_util = _host_gpu_util(host_key)
                req = urllib.request.Request(
                    f"{base_url}/api/ps",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    data = json.loads(resp.read())

                total_vram = OLLAMA_TOTAL_VRAM[host_key]
                for m in data.get("models", []):
                    raw_name = m.get("name", "")
                    rid = OLLAMA_MODEL_MAP.get((host_key, raw_name))
                    if rid is None:
                        # try stripping digest
                        base = raw_name.split(":")[0]
                        for (hk, mn), rid2 in OLLAMA_MODEL_MAP.items():
                            if hk == host_key and mn.startswith(base):
                                rid = rid2
                                break
                    if rid is None:
                        continue

                    size_vram = m.get("size_vram", 0)
                    expires_at = m.get("expires_at", "")
                    is_permanent = "2318" in expires_at or "2200" in expires_at or "2100" in expires_at

                    if size_vram > 0:
                        vram_pct = min(100.0, (size_vram / total_vram) * 100 * VRAM_SCALE)
                        if is_permanent:
                            USAGE.set_floor(rid, min(vram_pct, PINNED_FLOOR))
                            if gpu_util > PINNED_FLOOR:
                                USAGE.spike(rid, gpu_util)
                        else:
                            USAGE.spike(rid, max(gpu_util, min(vram_pct, 15.0)))

            except Exception:
                pass

        time.sleep(2.0)


def _pool_row(cc, out, in_, cr, bucket):
    """Compute pool units for one row."""
    full_rate = (cc or 0) + (out or 0) + (in_ or 0)
    weight = MODEL_WEIGHTS.get(bucket, 1.0)
    return (full_rate + (cr or 0) * 0.1) * weight, full_rate


def _query_window(start_ts: float, end_ts: float) -> dict:
    """Sum pool_units and turns in [start_ts, end_ts], grouped by model.

    Reads from proxy_trace.jsonl (live) for accuracy and freshness.
    """
    empty = {"turns": 0, "pool_units": 0.0, "by_model": {}}
    if not PROXY_TRACE.exists():
        return empty
    agg = {"turns": 0, "pool_units": 0.0, "by_model": {}}
    try:
        with PROXY_TRACE.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("ts", 0)
                if not (start_ts <= ts <= end_ts):
                    continue
                u = d.get("usage", {})
                req = d.get("req", {})
                m = req.get("routed_model") or req.get("model") or ""
                bucket = _model_bucket(m)
                cc = u.get("cache_create", 0)
                out = u.get("out", 0)
                in_ = u.get("in", 0)
                cr = u.get("cache_read", 0)
                pool, full_rate = _pool_row(cc, out, in_, cr, bucket)
                agg["turns"] += 1
                agg["pool_units"] += pool
                m_data = agg["by_model"].setdefault(bucket, {"turns": 0, "full_rate": 0, "pool": 0.0})
                m_data["turns"] += 1
                m_data["full_rate"] += full_rate
                m_data["pool"] += pool
    except Exception:
        pass
    return agg


def _query_live_buckets(window_sec: int, bucket_sec: int) -> list[tuple[int, dict]]:
    """Fetch per-turn rows in the last window_sec and bucket them.

    Reads from proxy_trace.jsonl (live, real-time).
    Returns a list of (bucket_ts, {model_bucket: pool_units}) sorted ascending.
    """
    if not PROXY_TRACE.exists():
        return []
    cutoff = time.time() - window_sec
    buckets: dict[int, dict[str, float]] = {}
    try:
        with PROXY_TRACE.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("ts", 0)
                if ts < cutoff:
                    continue
                u = d.get("usage", {})
                req = d.get("req", {})
                m = req.get("routed_model") or req.get("model") or ""
                bucket = _model_bucket(m)
                cc = u.get("cache_create", 0)
                out = u.get("out", 0)
                in_ = u.get("in", 0)
                cr = u.get("cache_read", 0)
                pool, _ = _pool_row(cc, out, in_, cr, bucket)
                key = int(ts // bucket_sec) * bucket_sec
                b = buckets.setdefault(key, {})
                b[bucket] = b.get(bucket, 0.0) + pool
    except Exception:
        pass
    return sorted(buckets.items(), key=lambda x: x[0])


def _read_proxy_stats() -> dict:
    stats = {"turns": 0, "routed_haiku": 0, "routed_sonnet": 0, "kept_opus": 0,
             "cc_headroom_approx": 0}
    if not PROXY_TRACE.exists():
        return stats
    cutoff = time.time() - 86400
    try:
        with PROXY_TRACE.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("ts", 0) < cutoff or "usage" not in d:
                    continue
                u = d["usage"]
                r = d.get("req", {})
                stats["turns"] += 1
                stats["cc_headroom_approx"] += max(0, 15000 - u.get("cache_create", 0))
                routed = (r.get("routed_model") or "").lower()
                if "haiku" in routed:
                    stats["routed_haiku"] += 1
                elif "sonnet" in routed:
                    stats["routed_sonnet"] += 1
                else:
                    stats["kept_opus"] += 1
    except Exception:
        pass
    return stats


def _read_local_llm_stats() -> dict:
    """Local-LLM engines from `savings` table (filtered to honest events) + live ollama /api/ps."""
    stats = {"total_calls": 0, "chars_deferred": 0, "by_engine": {},
             "ollama_up": False, "loaded_models": []}
    if DB_PATH.exists():
        try:
            cutoff = time.time() - 86400
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            placeholders = ",".join("?" * len(HONEST_LLM_EVENTS))
            rows = conn.execute(
                f"SELECT event_type, COUNT(*), COALESCE(SUM(saved_chars),0) "
                f"FROM savings WHERE timestamp > ? AND event_type IN ({placeholders}) "
                f"GROUP BY event_type ORDER BY 2 DESC",
                (cutoff, *HONEST_LLM_EVENTS),
            ).fetchall()
            conn.close()
            for et, cnt, saved in rows:
                stats["total_calls"] += cnt
                stats["chars_deferred"] += saved or 0
                stats["by_engine"][et] = cnt
        except Exception:
            pass
    try:
        import urllib.request
        req = urllib.request.Request(f"{OLLAMA_URL}/api/ps")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            data = json.loads(r.read())
        stats["ollama_up"] = True
        stats["loaded_models"] = [m.get("name", "?") for m in data.get("models", [])]
    except Exception:
        pass
    return stats


# -- GPU polling (background thread) --

_gpu_cache = {"local": [], "jagg": [], "ts": 0}
_gpu_lock = threading.Lock()


def _parse_nvidia_smi(text: str) -> list[dict]:
    cards = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            cards.append({
                "name": parts[0],
                "util": int(parts[1]) if parts[1].isdigit() else 0,
                "mem_used": int(parts[2]) if parts[2].isdigit() else 0,
                "mem_total": int(parts[3]) if parts[3].isdigit() else 0,
                "temp": int(parts[4]) if parts[4].isdigit() else 0,
                "power": float(parts[5]) if parts[5] not in ("[N/A]", "") else 0.0,
            })
        except Exception:
            continue
    return cards


def _fetch_gpu_stats():
    local, jagg = [], []
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            local = _parse_nvidia_smi(r.stdout)
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "BatchMode=yes", JAGG_HOST,
             "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode == 0:
            jagg = _parse_nvidia_smi(r.stdout)
    except Exception:
        pass
    with _gpu_lock:
        _gpu_cache["local"] = local
        _gpu_cache["jagg"] = jagg
        _gpu_cache["ts"] = time.time()


def _gpu_snapshot() -> dict:
    with _gpu_lock:
        return dict(_gpu_cache)


# -- UI --

class HonestMonitor:
    METER_MODES = [
        ("10m",  600,   5),   # label, window_sec, bucket_sec
        ("1h",   3600,  30),
        ("6h",   21600, 120),
        ("24h",  86400, 600),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Token Saver — Honest Monitor (v7)")
        root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        root.configure(bg=BG)
        root.minsize(820, 780)
        self.meter_mode_idx = 1  # default: 1h
        self._stop = threading.Event()
        self._build_ui()
        # Start monitoring threads
        cloud_thread = threading.Thread(target=_cloud_monitor, args=(self._stop,), daemon=True)
        cloud_thread.start()
        local_thread = threading.Thread(target=_local_monitor, args=(self._stop,), daemon=True)
        local_thread.start()
        threading.Thread(target=_fetch_gpu_stats, daemon=True).start()
        self._refresh()
        self._refresh_meter()
        self._schedule_dials()

    # --- layout ---

    def _build_ui(self):
        top = tk.Frame(self.root, bg=BG, padx=18, pady=12)
        top.pack(fill=tk.X)
        tk.Label(top, text="TOKEN SAVER — HONEST MONITOR",
                 font=("JetBrains Mono", 14, "bold"),
                 fg=TEXT_BRIGHT, bg=BG).pack(anchor="w")
        tk.Label(top,
                 text="pool = (cache_creation + output + input + cache_read·0.1) × weight   "
                      "opus=1.00  sonnet=0.20  haiku=0.04",
                 font=("JetBrains Mono", 8),
                 fg=TEXT_DIM, bg=BG).pack(anchor="w", pady=(2, 0))

        # Weekly pool box
        numbers = tk.Frame(self.root, bg=PANEL, padx=18, pady=14)
        numbers.pack(fill=tk.X, padx=14, pady=(0, 6))
        self.lbl_this = tk.Label(numbers, text="THIS WEEK: —", font=("JetBrains Mono", 12),
                                 fg=TEXT, bg=PANEL, anchor="w")
        self.lbl_this.pack(fill=tk.X)
        self.lbl_last = tk.Label(numbers, text="LAST WEEK: —", font=("JetBrains Mono", 12),
                                 fg=TEXT, bg=PANEL, anchor="w")
        self.lbl_last.pack(fill=tk.X, pady=(2, 0))
        self.lbl_delta = tk.Label(numbers, text="DELTA:     —",
                                  font=("JetBrains Mono", 14, "bold"),
                                  fg=YELLOW, bg=PANEL, anchor="w")
        self.lbl_delta.pack(fill=tk.X, pady=(8, 0))
        self.lbl_verdict = tk.Label(numbers, text="", font=("JetBrains Mono", 10),
                                    fg=TEXT_DIM, bg=PANEL, anchor="w")
        self.lbl_verdict.pack(fill=tk.X, pady=(4, 0))

        # Per-model bars
        model_panel = tk.Frame(self.root, bg=PANEL, padx=18, pady=12)
        model_panel.pack(fill=tk.X, padx=14, pady=(0, 6))
        tk.Label(model_panel, text="BY MODEL (this week)",
                 font=("JetBrains Mono", 9, "bold"), fg=TEXT_DIM, bg=PANEL).pack(anchor="w")
        self.model_canvas = tk.Canvas(model_panel, height=100, bg=PANEL,
                                      highlightthickness=0, bd=0)
        self.model_canvas.pack(fill=tk.X, pady=(6, 0))

        # Live scrolling meter
        meter_panel = tk.Frame(self.root, bg=PANEL, padx=18, pady=10)
        meter_panel.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 6))
        meter_hdr = tk.Frame(meter_panel, bg=PANEL)
        meter_hdr.pack(fill=tk.X)
        tk.Label(meter_hdr, text="LIVE POOL METER",
                 font=("JetBrains Mono", 9, "bold"), fg=TEXT_DIM, bg=PANEL).pack(side=tk.LEFT)
        self.lbl_meter_mode = tk.Label(meter_hdr, text=self._meter_mode_label(),
                                       font=("JetBrains Mono", 9, "bold"),
                                       fg="#000", bg=GREEN, padx=8, pady=1, cursor="hand2")
        self.lbl_meter_mode.pack(side=tk.LEFT, padx=(10, 0))
        self.lbl_meter_mode.bind("<Button-1>", self._cycle_meter_mode)
        tk.Label(meter_hdr, text="(stacked by model — opus / sonnet / haiku / local)",
                 font=("JetBrains Mono", 8), fg=TEXT_DIM, bg=PANEL).pack(side=tk.LEFT, padx=(10, 0))
        self.lbl_meter_peak = tk.Label(meter_hdr, text="",
                                       font=("JetBrains Mono", 8), fg=TEXT_DIM, bg=PANEL)
        self.lbl_meter_peak.pack(side=tk.RIGHT)
        self.meter_canvas = tk.Canvas(meter_panel, bg=PANEL, highlightthickness=0, bd=0, height=220)
        self.meter_canvas.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        # Proxy + local-LLM combined row
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill=tk.X, padx=14, pady=(0, 6))

        proxy_panel = tk.Frame(mid, bg=PANEL, padx=18, pady=10)
        proxy_panel.pack(fill=tk.X, pady=(0, 6))
        tk.Label(proxy_panel, text="PROXY ROUTER (last 24h)",
                 font=("JetBrains Mono", 9, "bold"), fg=TEXT_DIM, bg=PANEL).pack(anchor="w")
        self.lbl_proxy = tk.Label(proxy_panel, text="—",
                                  font=("JetBrains Mono", 10),
                                  fg=CYAN, bg=PANEL, anchor="w")
        self.lbl_proxy.pack(fill=tk.X, pady=(3, 0))

        llm_panel = tk.Frame(mid, bg=PANEL, padx=18, pady=10)
        llm_panel.pack(fill=tk.X)
        tk.Label(llm_panel, text="LOCAL LLM (ollama @ jagg)",
                 font=("JetBrains Mono", 9, "bold"), fg=TEXT_DIM, bg=PANEL).pack(anchor="w")
        self.lbl_llm_status = tk.Label(llm_panel, text="—",
                                       font=("JetBrains Mono", 10),
                                       fg=TEXT, bg=PANEL, anchor="w")
        self.lbl_llm_status.pack(fill=tk.X, pady=(3, 0))
        self.lbl_llm_engines = tk.Label(llm_panel, text="",
                                        font=("JetBrains Mono", 9),
                                        fg=TEXT_DIM, bg=PANEL, anchor="w")
        self.lbl_llm_engines.pack(fill=tk.X, pady=(2, 0))

        # Runner Dials (Calcifer-style)
        dials_panel = tk.Frame(self.root, bg=PANEL, padx=18, pady=10)
        dials_panel.pack(fill=tk.X, padx=14, pady=(0, 8))
        tk.Label(dials_panel, text="RUNNER LOAD (cloud + local)",
                 font=("JetBrains Mono", 9, "bold"), fg=TEXT_DIM, bg=PANEL).pack(anchor="w")
        self.dials_canvas = tk.Canvas(dials_panel, bg=PANEL, highlightthickness=0, bd=0, height=140)
        self.dials_canvas.pack(fill=tk.X, pady=(6, 0))

        # Footer
        footer = tk.Frame(self.root, bg=BG, padx=18, pady=6)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        self.lbl_target = tk.Label(footer, text="TARGET: -40% week-over-week",
                                   font=("JetBrains Mono", 9), fg=TEXT_DIM, bg=BG, anchor="w")
        self.lbl_target.pack(fill=tk.X)
        self.lbl_feels_like = tk.Label(footer, text="",
                                       font=("JetBrains Mono", 12, "bold"),
                                       fg=YELLOW, bg=BG, anchor="w")
        self.lbl_feels_like.pack(fill=tk.X, pady=(4, 0))
        tk.Label(footer,
                 text="source: proxy_trace.jsonl (live) + ollama /api/ps  |  "
                      "numbers 5s · meter 2s · dials 500ms  |  no counterfactuals",
                 font=("JetBrains Mono", 7), fg="#444", bg=BG, anchor="w").pack(fill=tk.X, pady=(2, 0))

    # --- drawing ---

    def _meter_mode_label(self) -> str:
        return f"WINDOW: {self.METER_MODES[self.meter_mode_idx][0]}"

    def _cycle_meter_mode(self, _evt=None):
        self.meter_mode_idx = (self.meter_mode_idx + 1) % len(self.METER_MODES)
        self.lbl_meter_mode.config(text=self._meter_mode_label())
        self._refresh_meter(force=True)

    def _draw_model_bars(self, this_week: dict):
        c = self.model_canvas
        c.delete("all")
        w = c.winfo_width() or (WINDOW_W - 60)
        total = this_week["pool_units"] or 1.0
        row_h, label_w = 18, 80
        max_bar = max(w - label_w - 200, 50)
        for i, bucket in enumerate(MODEL_ORDER):
            m = this_week["by_model"].get(bucket, {"turns": 0, "pool": 0, "full_rate": 0})
            pool = m["pool"]; turns = m["turns"]
            pct = 100 * pool / total
            bar_w = int((pool / total) * max_bar) if total else 0
            if pool > 0 and bar_w < 3:
                bar_w = 3  # floor so tiny shares remain visible
            color = MODEL_COLORS[bucket]
            y = 4 + i * (row_h + 4)
            c.create_text(6, y + row_h/2, anchor="w", text=bucket.upper(),
                          font=("JetBrains Mono", 9, "bold"), fill=color)
            c.create_rectangle(label_w, y, label_w + max_bar, y + row_h, fill=GRID, outline="")
            if bar_w > 0:
                c.create_rectangle(label_w, y, label_w + bar_w, y + row_h, fill=color, outline="")
            stats = f"{turns:>6,} t   {_fmt(pool):>8}   {pct:5.1f}%"
            c.create_text(label_w + max_bar + 10, y + row_h/2, anchor="w",
                          text=stats, font=("JetBrains Mono", 9), fill=TEXT)

    def _draw_meter(self, buckets: list[tuple[int, dict]], window_sec: int, bucket_sec: int):
        c = self.meter_canvas
        c.delete("all")
        w = c.winfo_width() or (WINDOW_W - 60)
        h = c.winfo_height() or 220
        pad_l, pad_r, pad_t, pad_b = 60, 10, 10, 22

        # Plot area
        pw = max(w - pad_l - pad_r, 20)
        ph = max(h - pad_t - pad_b, 20)
        c.create_rectangle(pad_l, pad_t, pad_l + pw, pad_t + ph, fill=GRID, outline="")

        if not buckets:
            c.create_text(pad_l + pw/2, pad_t + ph/2, anchor="center",
                          text="(no turns in window)",
                          font=("JetBrains Mono", 10), fill=TEXT_DIM)
            return

        # Map bucket_ts -> totals
        now = time.time()
        start = now - window_sec
        n_slots = max(1, int(window_sec / bucket_sec))
        totals = [0.0] * n_slots
        stacks: list[dict[str, float]] = [dict() for _ in range(n_slots)]
        peak = 0.0
        for bts, models in buckets:
            slot = int((bts - start) // bucket_sec)
            if slot < 0 or slot >= n_slots:
                continue
            s = sum(models.values())
            totals[slot] += s
            for k, v in models.items():
                stacks[slot][k] = stacks[slot].get(k, 0.0) + v
            if totals[slot] > peak:
                peak = totals[slot]

        if peak <= 0:
            c.create_text(pad_l + pw/2, pad_t + ph/2, anchor="center",
                          text="(all buckets empty)",
                          font=("JetBrains Mono", 10), fill=TEXT_DIM)
            return

        self.lbl_meter_peak.config(text=f"peak bucket: {_fmt(peak)} pool")

        # Y-axis labels
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = pad_t + ph - ph * frac
            c.create_line(pad_l, y, pad_l + pw, y, fill="#1a1b20", dash=(1, 3))
            c.create_text(pad_l - 4, y, anchor="e",
                          text=_fmt(peak * frac),
                          font=("JetBrains Mono", 8), fill=TEXT_DIM)

        # Bars
        bar_w = pw / n_slots
        gap = 0.5 if bar_w > 3 else 0
        for i, s in enumerate(stacks):
            if not s:
                continue
            x0 = pad_l + i * bar_w + gap
            x1 = pad_l + (i + 1) * bar_w - gap
            if x1 <= x0:
                x1 = x0 + 1
            # Stack in MODEL_ORDER from bottom up
            y_cursor = pad_t + ph
            for bucket in MODEL_ORDER:
                v = s.get(bucket, 0)
                if v <= 0:
                    continue
                seg_h = (v / peak) * ph
                if seg_h < 2:
                    seg_h = 2  # floor so sub-% models stay visible in the stack
                y0 = y_cursor - seg_h
                c.create_rectangle(x0, y0, x1, y_cursor,
                                   fill=MODEL_COLORS[bucket], outline="")
                y_cursor = y0

        # X-axis time labels
        label_y = pad_t + ph + 4
        for frac, text in ((0.0, "-" + self.METER_MODES[self.meter_mode_idx][0]),
                           (0.5, ""), (1.0, "now")):
            x = pad_l + pw * frac
            c.create_text(x, label_y, anchor="n", text=text,
                          font=("JetBrains Mono", 8), fill=TEXT_DIM)

        # Legend
        lx = pad_l + 4
        ly = pad_t + 4
        for bucket in MODEL_ORDER:
            c.create_rectangle(lx, ly, lx + 10, ly + 10,
                               fill=MODEL_COLORS[bucket], outline="")
            c.create_text(lx + 14, ly + 5, anchor="w", text=bucket,
                          font=("JetBrains Mono", 8), fill=TEXT_DIM)
            lx += 66

    def _draw_dials(self, usage: dict):
        """Draw 6 runner dials (cloud + local)."""
        c = self.dials_canvas
        c.delete("all")
        w = c.winfo_width() or (WINDOW_W - 60)
        h = 140

        # Draw 2 rows of 3 dials
        dial_size = 60
        spacing_x = 20
        spacing_y = 20
        top_margin = 10
        left_margin = 10

        total_w = 3 * (dial_size + spacing_x) - spacing_x + left_margin
        total_h = 2 * (dial_size + spacing_y) - spacing_y + top_margin

        for i, (rid, label, sublabel, accent, group) in enumerate(RUNNERS):
            row = i // 3
            col = i % 3
            x = left_margin + col * (dial_size + spacing_x)
            y = top_margin + row * (dial_size + spacing_y)

            val = usage.get(rid, 0.0)
            self._draw_single_dial(c, x, y, dial_size, rid, label, sublabel, accent, val)

    def _draw_single_dial(self, c: tk.Canvas, x: float, y: float, size: float,
                          rid: str, label: str, sublabel: str, accent: str, val: float):
        """Draw one circular dial."""
        cx = x + size / 2
        cy = y + size / 2
        r = size / 2 - 4

        # Background circle
        c.create_oval(x, y, x + size, y + size, fill=PANEL, outline=BORDER, width=1)

        # Track (0-100% arc, 270° sweep)
        # Arc from 225° to -45° (270° total, SW to SE)
        angle_start = 225
        angle_end = -45
        extent = -270
        c.create_arc(x + 2, y + 2, x + size - 2, y + size - 2,
                     start=angle_start, extent=extent,
                     fill="", outline=BORDER, width=2)

        # Value arc
        val_extent = (val / 100.0) * extent
        if val_extent != 0:
            val_color = _value_color(val)
            c.create_arc(x + 2, y + 2, x + size - 2, y + size - 2,
                         start=angle_start, extent=val_extent,
                         fill="", outline=val_color, width=3)

        # Center dot
        dot_r = 3
        c.create_oval(cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r,
                      fill=accent, outline="")

        # Value text (center)
        c.create_text(cx, cy - 4, anchor="center", text=f"{val:.0f}%",
                      font=("JetBrains Mono", 7, "bold"), fill=TEXT_BRIGHT)

        # Label (below dial)
        c.create_text(cx, y + size + 4, anchor="n", text=label,
                      font=("JetBrains Mono", 7, "bold"), fill=TEXT)

        # Sublabel (below label)
        c.create_text(cx, y + size + 15, anchor="n", text=sublabel,
                      font=("JetBrains Mono", 5), fill=TEXT_DIM)

    # --- refresh loops ---

    def _refresh(self):
        try:
            now = time.time()
            week = 7 * 86400
            this_week = _query_window(now - week, now)
            last_week = _query_window(now - 2 * week, now - week)
            this_pool = this_week["pool_units"]
            last_pool = last_week["pool_units"]
            delta = this_pool - last_pool
            pct = (100 * delta / last_pool) if last_pool else 0

            self.lbl_this.config(
                text=f"THIS WEEK: {_fmt(this_pool):>10}   ({this_week['turns']:,} turns)")
            self.lbl_last.config(
                text=f"LAST WEEK: {_fmt(last_pool):>10}   ({last_week['turns']:,} turns)")
            arrow = "▼" if delta < 0 else ("▲" if delta > 0 else "•")
            if delta < 0:
                color, verdict = GREEN, "✓ going down — doing more with less"
            elif delta > 0:
                color, verdict = RED, "✗ going up — burning more per week"
            else:
                color, verdict = YELLOW, "• flat"
            self.lbl_delta.config(
                text=f"DELTA:     {arrow} {_fmt(abs(delta)):>8}   ({pct:+.1f}%)", fg=color)
            self.lbl_verdict.config(text=verdict, fg=color)

            target_pct = -40
            if pct <= target_pct:
                self.lbl_target.config(
                    text=f"TARGET: -40%   STATUS: ✓ hit ({pct:+.1f}%)", fg=GREEN)
            else:
                gap = target_pct - pct
                self.lbl_target.config(
                    text=f"TARGET: -40%   gap: {gap:+.1f} points   (at {pct:+.1f}%)",
                    fg=TEXT_DIM if pct < 0 else YELLOW,
                )

            self._draw_model_bars(this_week)

            # "Feels like X__ plan" — what tier sustained at this burn rate
            X20_POOL_PER_MONTH = 15.22e9  # your baseline from last 30d
            weekly_to_monthly = this_pool * 4.33  # weeks per month
            tier_multiplier = weekly_to_monthly / X20_POOL_PER_MONTH
            tier_name = "X20"
            if tier_multiplier > 2.5:
                tier_name = "X50"
                tier_multiplier /= 2.5
            if tier_multiplier > 2.5:
                tier_name = "X100"
                tier_multiplier /= 2.5
            if tier_multiplier > 2.5:
                tier_name = "X250"
                tier_multiplier /= 2.5
            feels_pct = 100 * tier_multiplier
            feels_color = GREEN if feels_pct < 60 else YELLOW if feels_pct < 90 else RED
            self.lbl_feels_like.config(
                text=f"FEELS LIKE: {feels_pct:5.0f}% of {tier_name} PLAN  (~${int(300*tier_multiplier)} CAD/mo)",
                fg=feels_color)

            px = _read_proxy_stats()
            if px["turns"]:
                self.lbl_proxy.config(
                    text=(f"{px['turns']:,} turns   "
                          f"haiku {px['routed_haiku']}   "
                          f"sonnet {px['routed_sonnet']}   "
                          f"opus {px['kept_opus']}   "
                          f"cc headroom (approx) ~{_fmt(px['cc_headroom_approx'])}")
                )
            else:
                self.lbl_proxy.config(text="no proxy traffic yet (24h)")

            llm = _read_local_llm_stats()
            up_dot = "●" if llm["ollama_up"] else "○"
            loaded = ", ".join(llm["loaded_models"]) if llm["loaded_models"] else "idle"
            status_text = (
                f"{up_dot} endpoint {'up' if llm['ollama_up'] else 'DOWN'}   "
                f"{llm['total_calls']:,} calls today   "
                f"{_fmt(llm['chars_deferred'])} chars deferred   "
                f"loaded: {loaded}"
            )
            self.lbl_llm_status.config(
                text=status_text, fg=GREEN if llm["ollama_up"] else RED)
            if llm["by_engine"]:
                parts = [f"{et}={cnt}" for et, cnt in
                         sorted(llm["by_engine"].items(), key=lambda x: -x[1])[:6]]
                self.lbl_llm_engines.config(text="  ".join(parts))
            else:
                self.lbl_llm_engines.config(text="(no honest local-LLM calls today)")

            self._draw_dials(USAGE.snapshot())
        except Exception as e:
            print(f"[honest_dashboard] refresh error: {e}")
        self.root.after(POLL_MS, self._refresh)

    def _refresh_meter(self, force: bool = False):
        try:
            _, window_sec, bucket_sec = self.METER_MODES[self.meter_mode_idx]
            buckets = _query_live_buckets(window_sec, bucket_sec)
            self._draw_meter(buckets, window_sec, bucket_sec)
        except Exception as e:
            print(f"[honest_dashboard] meter error: {e}")
        self.root.after(METER_MS, self._refresh_meter)

    def _schedule_dials(self):
        """Tick decay and refresh dials every 500ms."""
        try:
            USAGE.tick_decay()
            self._draw_dials(USAGE.snapshot())
        except Exception as e:
            print(f"[honest_dashboard] dials error: {e}")
        self.root.after(500, self._schedule_dials)


def main():
    root = tk.Tk()
    HonestMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
