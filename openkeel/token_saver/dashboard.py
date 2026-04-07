#!/usr/bin/env python3
"""Token Saver Dashboard — real-time scrolling token usage monitor with system tray.

A dark-themed tkinter app that shows token usage scrolling left like an
audio level meter. Red bars = gross tokens used, green overlay = tokens
saved by the token saver. Polls the SQLite ledger for live data.

Minimizes to system tray. Click tray icon to restore.

Usage:
    python -m openkeel.token_saver.dashboard
"""

import sqlite3
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path

DB_PATH = Path.home() / ".openkeel" / "token_ledger.db"
CHARS_PER_TOKEN = 4

# -- Visual config --
BG = "#0d0d0d"
GRID_COLOR = "#1a1a1a"
TEXT_COLOR = "#888888"
TEXT_BRIGHT = "#cccccc"
RED_DIM = "#551122"       # Irreducible work (edits, small commands)
RED = "#cc2233"           # Compressible gross (where savings happened)
RED_GLOW = "#ff3344"
GREEN = "#22cc66"         # Actual savings
GREEN_GLOW = "#33ff88"
ACCENT = "#4488ff"

BAR_WIDTH = 6
BAR_GAP = 2
BAR_STEP = BAR_WIDTH + BAR_GAP
POLL_MS = 1000
SCROLL_MS = 500
WINDOW_W = 900
WINDOW_H = 450
GRAPH_PAD_LEFT = 70
GRAPH_PAD_RIGHT = 20


def _create_tray_icon_image():
    """Create a small green/red icon for the system tray."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Red background bar
        draw.rectangle([8, 8, 56, 56], fill=(204, 34, 51, 255))
        # Green overlay (bottom portion — represents savings)
        draw.rectangle([8, 32, 56, 56], fill=(34, 204, 102, 255))
        # Small "T" letter
        draw.rectangle([24, 12, 40, 16], fill=(255, 255, 255, 220))
        draw.rectangle([30, 12, 34, 28], fill=(255, 255, 255, 220))
        return img
    except ImportError:
        # Fallback: 1x1 pixel
        from PIL import Image
        return Image.new("RGB", (64, 64), (34, 204, 102))


class TokenDashboard:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Token Saver — Live Monitor (T2)")
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.resizable(True, True)

        # Tray state
        self.tray_icon = None
        self.tray_available = False
        self._setup_tray()

        # Override close button to minimize to tray
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Data: deque of (timestamp, gross, saved, compressible_gross) per time bucket
        # compressible_gross = gross from events that had savings > 0
        # Large enough to hold 2h of 10s buckets (720) plus headroom
        self.bars: deque[tuple[float, int, int, int]] = deque(maxlen=10000)
        self.last_poll_ts = time.time() - 300
        self.peak_tokens = 1000
        self.total_gross = 0
        self.total_saved = 0
        self.total_events = 0

        # View modes: "history" (30 min) vs "realtime" (2 min, fast rescale)
        self.view_mode = "history"
        self.MODES = {
            "realtime": {"window_sec": 600, "label": "REALTIME (10m)", "shrink_rate": 0.92},
            "history":  {"window_sec": 3600, "label": "HISTORY (1h)", "shrink_rate": 0.998},
            "session":  {"window_sec": 14400, "label": "SESSION (4h)", "shrink_rate": 0.999},
        }
        self.mode_cycle = ["realtime", "history", "session"]

        # --- Layout ---

        # Stats header
        self.header = tk.Frame(root, bg=BG, height=30)
        self.header.pack(fill=tk.X, padx=10, pady=(8, 0))

        self.lbl_title = tk.Label(self.header, text="TOKEN SAVER", font=("JetBrains Mono", 11, "bold"),
                                  fg=ACCENT, bg=BG)
        self.lbl_title.pack(side=tk.LEFT)

        # Mode toggle button
        self.btn_mode = tk.Label(self.header, text="HISTORY (1h)", font=("JetBrains Mono", 9, "bold"),
                                 fg="#000000", bg=GREEN, padx=8, pady=1, cursor="hand2")
        self.btn_mode.pack(side=tk.LEFT, padx=(12, 0))
        self.btn_mode.bind("<Button-1>", self._toggle_mode)

        # Reset scale button
        self.btn_reset = tk.Label(self.header, text="RESET SCALE", font=("JetBrains Mono", 9),
                                  fg=TEXT_COLOR, bg="#222222", padx=6, pady=1, cursor="hand2")
        self.btn_reset.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_reset.bind("<Button-1>", self._reset_scale)

        self.lbl_stats = tk.Label(self.header, text="", font=("JetBrains Mono", 10),
                                  fg=TEXT_BRIGHT, bg=BG)
        self.lbl_stats.pack(side=tk.RIGHT)

        self.lbl_rate = tk.Label(self.header, text="", font=("JetBrains Mono", 10),
                                 fg=GREEN, bg=BG)
        self.lbl_rate.pack(side=tk.RIGHT, padx=(0, 20))

        # Main canvas
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 4))

        # Engine breakdown strip
        self.engine_frame = tk.Frame(root, bg="#111111", height=24)
        self.engine_frame.pack(fill=tk.X, padx=10, pady=(0, 2))

        # Engine colors
        self.ENGINE_COLORS = {
            "local_edit":        ("#ffdd33", "LocalEdit"),
            "bash_compress":     ("#e8a735", "Bash"),
            "grep_compress":     ("#35b8e8", "Grep"),
            "glob_compress":     ("#b835e8", "Glob"),
            "large_file_compress": ("#e87835", "LargeFile"),
            "cache_hit":         ("#35e8a7", "Cache"),
            "conversation_compress": ("#e835a7", "Convo"),
            "prefill_index":     ("#7888aa", "Prefill"),
            "prefill_ranked_map": ("#6878aa", "Map"),
            "search_filter":     ("#55aacc", "Search"),
            "output_compress":   ("#aa8855", "Output"),
            "bash_predict":      ("#ff6688", "Predict"),
            "bash_llm_summarize": ("#66ffaa", "LLM-Bash"),
            "edit_trim":         ("#ffaa33", "EditTrim"),
            "write_trim":        ("#ff8866", "WriteTrim"),
        }
        self.engine_labels: dict[str, tk.Label] = {}

        tk.Label(self.engine_frame, text="ENGINES", font=("JetBrains Mono", 8, "bold"),
                 fg=TEXT_COLOR, bg="#111111").pack(side=tk.LEFT, padx=(4, 8))

        for etype, (color, short_name) in self.ENGINE_COLORS.items():
            lbl = tk.Label(self.engine_frame, text=f"{short_name}: -",
                          font=("JetBrains Mono", 8), fg=color, bg="#111111")
            lbl.pack(side=tk.LEFT, padx=(0, 10))
            self.engine_labels[etype] = lbl

        # GPU / Model status strip
        self.gpu_frame = tk.Frame(root, bg="#0a0a0a", height=20)
        self.gpu_frame.pack(fill=tk.X, padx=10, pady=(0, 2))

        tk.Label(self.gpu_frame, text="GPU", font=("JetBrains Mono", 8, "bold"),
                 fg=TEXT_COLOR, bg="#0a0a0a").pack(side=tk.LEFT, padx=(4, 6))
        self.lbl_gpu = tk.Label(self.gpu_frame, text="...", font=("JetBrains Mono", 8),
                                fg=TEXT_COLOR, bg="#0a0a0a")
        self.lbl_gpu.pack(side=tk.LEFT, padx=(0, 15))

        tk.Label(self.gpu_frame, text="MODEL", font=("JetBrains Mono", 8, "bold"),
                 fg=TEXT_COLOR, bg="#0a0a0a").pack(side=tk.LEFT, padx=(0, 6))
        self.lbl_model = tk.Label(self.gpu_frame, text="...", font=("JetBrains Mono", 8),
                                  fg=TEXT_COLOR, bg="#0a0a0a")
        self.lbl_model.pack(side=tk.LEFT, padx=(0, 15))

        tk.Label(self.gpu_frame, text="DAEMON", font=("JetBrains Mono", 8, "bold"),
                 fg=TEXT_COLOR, bg="#0a0a0a").pack(side=tk.LEFT, padx=(0, 6))
        self.lbl_daemon = tk.Label(self.gpu_frame, text="...", font=("JetBrains Mono", 8),
                                   fg=TEXT_COLOR, bg="#0a0a0a")
        self.lbl_daemon.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_last_edit = tk.Label(self.gpu_frame, text="", font=("JetBrains Mono", 8),
                                      fg="#ffdd33", bg="#0a0a0a")
        self.lbl_last_edit.pack(side=tk.RIGHT, padx=(0, 4))

        # Start GPU polling (every 5s, lightweight)
        self._poll_gpu()

        # Debug log panel (collapsible)
        self.debug_visible = False
        self.debug_header = tk.Frame(root, bg="#111111", height=20)
        self.debug_header.pack(fill=tk.X, padx=10, pady=(0, 0))

        self.btn_debug = tk.Label(self.debug_header, text="▶ LEAK LOG (click to expand)",
                                   font=("JetBrains Mono", 8, "bold"),
                                   fg="#cc2233", bg="#111111", cursor="hand2")
        self.btn_debug.pack(side=tk.LEFT, padx=4)
        self.btn_debug.bind("<Button-1>", self._toggle_debug)

        self.lbl_leak_count = tk.Label(self.debug_header, text="",
                                        font=("JetBrains Mono", 8),
                                        fg="#cc2233", bg="#111111")
        self.lbl_leak_count.pack(side=tk.RIGHT, padx=4)

        self.debug_frame = tk.Frame(root, bg="#0a0a0a")
        # Don't pack yet — collapsed by default

        self.debug_text = tk.Text(self.debug_frame, bg="#0a0a0a", fg="#888888",
                                   font=("JetBrains Mono", 8), height=8,
                                   relief="flat", bd=4, wrap="none",
                                   state="disabled", cursor="arrow")
        debug_scroll = tk.Scrollbar(self.debug_frame, command=self.debug_text.yview,
                                     bg="#0a0a0a", troughcolor="#111111", width=6)
        self.debug_text.configure(yscrollcommand=debug_scroll.set)
        self.debug_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        debug_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Tag styles for debug log
        self.debug_text.tag_configure("leak_high", foreground="#ff3344")
        self.debug_text.tag_configure("leak_med", foreground="#e8a735")
        self.debug_text.tag_configure("leak_low", foreground="#888888")
        self.debug_text.tag_configure("timestamp", foreground="#555555")

        self._last_debug_ts = time.time() - 300
        self._poll_leaks()

        # Legend footer
        self.footer = tk.Frame(root, bg=BG, height=20)
        self.footer.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._legend_box(self.footer, RED_DIM, "Irreducible")
        tk.Label(self.footer, text="   ", bg=BG).pack(side=tk.LEFT)
        self._legend_box(self.footer, RED, "Compressible")
        tk.Label(self.footer, text="   ", bg=BG).pack(side=tk.LEFT)
        self._legend_box(self.footer, GREEN, "Saved")
        tk.Label(self.footer, text="   ", bg=BG).pack(side=tk.LEFT)

        self.lbl_latest = tk.Label(self.footer, text="", font=("JetBrains Mono", 9),
                                   fg=TEXT_COLOR, bg=BG)
        self.lbl_latest.pack(side=tk.RIGHT)

        # Load data + start loops
        self._load_history()
        self._poll_ledger()
        self._draw()

    # -- System tray --

    def _setup_tray(self):
        """Set up system tray icon (pystray)."""
        try:
            import pystray
            self.tray_available = True
        except ImportError:
            self.tray_available = False

    def _minimize_to_tray(self):
        """Hide window and show tray icon."""
        if not self.tray_available:
            self.root.iconify()
            return

        import pystray

        self.root.withdraw()

        icon_image = _create_tray_icon_image()

        menu = pystray.Menu(
            pystray.MenuItem("Show Dashboard", self._restore_from_tray, default=True),
            pystray.MenuItem("Quit", self._quit_from_tray),
        )

        self.tray_icon = pystray.Icon("token_saver", icon_image, "Token Saver", menu)

        # Run tray in a thread so tkinter mainloop continues
        tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        tray_thread.start()

    def _restore_from_tray(self, icon=None, item=None):
        """Restore window from tray."""
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.after(0, self._do_restore)

    def _do_restore(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit_from_tray(self, icon=None, item=None):
        """Fully quit from tray."""
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.after(0, self.root.destroy)

    def _on_close(self):
        """Window close → minimize to tray instead of quitting."""
        if self.tray_available:
            self._minimize_to_tray()
        else:
            self.root.iconify()

    def _toggle_mode(self, event=None):
        """Cycle through view modes."""
        idx = self.mode_cycle.index(self.view_mode)
        self.view_mode = self.mode_cycle[(idx + 1) % len(self.mode_cycle)]
        mode = self.MODES[self.view_mode]
        self.btn_mode.config(text=mode["label"])
        # Reset scale on mode switch
        self._reset_scale()

    def _reset_scale(self, event=None):
        """Reset Y-axis scale to fit current visible data."""
        self.peak_tokens = 500  # Will auto-grow on next draw

    # -- UI helpers --

    def _legend_box(self, parent, color, label):
        c = tk.Canvas(parent, width=12, height=12, bg=BG, highlightthickness=0)
        c.create_rectangle(0, 0, 12, 12, fill=color, outline="")
        c.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(parent, text=label, font=("JetBrains Mono", 9), fg=TEXT_COLOR, bg=BG).pack(side=tk.LEFT)

    @staticmethod
    def _fmt(tokens: int) -> str:
        if tokens >= 1_000_000:
            return f"{tokens / 1_000_000:.1f}M"
        if tokens >= 1_000:
            return f"{tokens / 1_000:.1f}K"
        return str(tokens)

    # -- Data loading --

    def _load_history(self):
        """Load all events from ledger — we filter by view mode at draw time."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            # Load everything from today (covers all view modes)
            cutoff = time.time() - 86400
            rows = conn.execute(
                "SELECT timestamp, original_chars, saved_chars FROM savings "
                "WHERE timestamp > ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()

            totals = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(original_chars),0), COALESCE(SUM(saved_chars),0) "
                "FROM savings"
            ).fetchone()
            conn.close()

            if totals:
                self.total_events = totals[0]
                self.total_gross = totals[1] // CHARS_PER_TOKEN
                self.total_saved = totals[2] // CHARS_PER_TOKEN

            if not rows:
                return

            # Bucket into 10-second intervals — only buckets with data (no gap filling)
            bucket_size = 1
            buckets: dict[int, tuple[int, int, int]] = {}
            for ts, orig, saved in rows:
                key = int(ts // bucket_size)
                g, s, cg = buckets.get(key, (0, 0, 0))
                g += orig // CHARS_PER_TOKEN
                s += saved // CHARS_PER_TOKEN
                if saved > 0:
                    cg += orig // CHARS_PER_TOKEN
                buckets[key] = (g, s, cg)

            for k in sorted(buckets.keys()):
                g, s, cg = buckets[k]
                self.bars.append((k * bucket_size, g, s, cg))

            self.last_poll_ts = time.time()

        except Exception:
            pass

    def _poll_ledger(self):
        """Poll for new events since last check."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=2)
            rows = conn.execute(
                "SELECT timestamp, original_chars, saved_chars, event_type FROM savings "
                "WHERE timestamp > ? ORDER BY timestamp ASC",
                (self.last_poll_ts,),
            ).fetchall()
            conn.close()

            if rows:
                self.last_poll_ts = rows[-1][0]

                bucket_size = 1
                new_buckets: dict[int, tuple[int, int, int]] = {}
                latest_event = ""
                for ts, orig, saved, etype in rows:
                    key = int(ts // bucket_size)
                    g, s, cg = new_buckets.get(key, (0, 0, 0))
                    g += orig // CHARS_PER_TOKEN
                    s += saved // CHARS_PER_TOKEN
                    if saved > 0:
                        cg += orig // CHARS_PER_TOKEN
                    new_buckets[key] = (g, s, cg)
                    self.total_gross += orig // CHARS_PER_TOKEN
                    self.total_saved += saved // CHARS_PER_TOKEN
                    self.total_events += 1
                    latest_event = etype

                for key in sorted(new_buckets.keys()):
                    g, s, cg = new_buckets[key]
                    if self.bars and int(self.bars[-1][0] // bucket_size) == key:
                        _, og, os_, ocg = self.bars[-1]
                        self.bars[-1] = (key * bucket_size, og + g, os_ + s, ocg + cg)
                    else:
                        # No gap filling — only bars with actual data
                        self.bars.append((key * bucket_size, g, s, cg))

                if latest_event:
                    self.lbl_latest.config(text=f"latest: {latest_event}")

        except Exception:
            pass

        # Don't fill empty bars to "now" — bars only appear when there's activity
        # This keeps the graph right-anchored to the latest actual event

        # Update engine breakdown labels
        self._update_engine_stats()

        self.root.after(POLL_MS, self._poll_ledger)

    def _update_engine_stats(self):
        """Query per-engine savings and update the breakdown strip."""
        try:
            mode = self.MODES[self.view_mode]
            cutoff = time.time() - mode["window_sec"]

            conn = sqlite3.connect(str(DB_PATH), timeout=2)
            rows = conn.execute(
                "SELECT event_type, SUM(saved_chars) FROM savings "
                "WHERE timestamp > ? AND saved_chars > 0 GROUP BY event_type "
                "ORDER BY SUM(saved_chars) DESC",
                (cutoff,),
            ).fetchall()
            conn.close()

            # Build lookup
            engine_savings = {r[0]: r[1] // CHARS_PER_TOKEN for r in rows}

            for etype, lbl in self.engine_labels.items():
                saved = engine_savings.get(etype, 0)
                color, short_name = self.ENGINE_COLORS[etype]
                if saved > 0:
                    lbl.config(text=f"{short_name}: {self._fmt(saved)}", fg=color)
                else:
                    lbl.config(text=f"{short_name}: -", fg="#333333")

        except Exception:
            pass

    def _poll_gpu(self):
        """Lightweight GPU + model + daemon status poll (every 5s)."""
        try:
            import subprocess
            # GPU utilization — single nvidia-smi call
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    parts = result.stdout.strip().split(", ")
                    if len(parts) == 3:
                        util, mem_used, mem_total = parts
                        mem_gb = f"{int(mem_used)/1024:.1f}/{int(mem_total)/1024:.1f}GB"
                        color = GREEN if int(util) < 50 else "#e8a735" if int(util) < 85 else RED
                        self.lbl_gpu.config(text=f"{util}% {mem_gb}", fg=color)
                    else:
                        self.lbl_gpu.config(text="parse err", fg=TEXT_COLOR)
                else:
                    self.lbl_gpu.config(text="no GPU", fg="#333333")
            except FileNotFoundError:
                self.lbl_gpu.config(text="no nvidia-smi", fg="#333333")
            except Exception:
                self.lbl_gpu.config(text="err", fg=RED)

            # Ollama model status
            try:
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:11434/api/ps", method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    import json
                    data = json.loads(resp.read())
                    models = data.get("models", [])
                    if models:
                        names = ", ".join(m["name"] for m in models)
                        self.lbl_model.config(text=names, fg=GREEN)
                    else:
                        self.lbl_model.config(text="none loaded", fg="#555555")
            except Exception:
                self.lbl_model.config(text="ollama down", fg=RED)

            # GPU Tier status
            try:
                from openkeel.token_saver.engines.gpu_tier import get_tier
                t = get_tier()
                tier_colors = {0: RED, 1: "#e8a735", 2: GREEN, 3: ACCENT}
                self.lbl_model.config(
                    text=f"T{t.tier} {t.model_name} ({t.model_params_b:.0f}B) @ {t.endpoint_name}",
                    fg=tier_colors.get(t.tier, TEXT_COLOR),
                )
            except Exception:
                pass

            # Daemon status
            try:
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:11450/health", method="GET")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    import json
                    data = json.loads(resp.read())
                    cache = data.get("cache_entries", 0)
                    self.lbl_daemon.config(text=f"up ({cache} cached)", fg=GREEN)
            except Exception:
                self.lbl_daemon.config(text="down", fg=RED)

            # Last LocalEdit activity
            try:
                conn = sqlite3.connect(str(DB_PATH), timeout=1)
                row = conn.execute(
                    "SELECT notes, timestamp FROM savings "
                    "WHERE event_type = 'local_edit' ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                conn.close()
                if row:
                    notes, ts = row
                    ago = int(time.time() - ts)
                    if ago < 60:
                        ago_str = f"{ago}s ago"
                    elif ago < 3600:
                        ago_str = f"{ago//60}m ago"
                    else:
                        ago_str = f"{ago//3600}h ago"
                    # Extract just the filename from notes
                    short = notes.replace("local_edit OK: ", "").split(" — ")[0] if notes else ""
                    self.lbl_last_edit.config(text=f"LocalEdit: {short} ({ago_str})")
            except Exception:
                pass

        except Exception:
            pass

        self.root.after(5000, self._poll_gpu)

    # -- Rendering --

    def _draw(self):
        """Render the scrolling bar graph."""
        c = self.canvas
        c.delete("all")

        cw = c.winfo_width() or WINDOW_W - 20
        ch = c.winfo_height() or 300

        gl = GRAPH_PAD_LEFT
        gr = cw - GRAPH_PAD_RIGHT
        gt = 10
        gb = ch - 30
        gw = gr - gl
        gh = gb - gt

        if gh < 20 or gw < 40:
            self.root.after(SCROLL_MS, self._draw)
            return

        # Fixed number of slots that fill the full width
        mode = self.MODES[self.view_mode]
        now = time.time()
        window_sec = mode["window_sec"]
        shrink = mode["shrink_rate"]

        # Calculate how many slots fit the width
        num_slots = gw // BAR_STEP
        if num_slots < 10:
            num_slots = 10
        slot_duration = window_sec / num_slots

        # Anchor cutoff to a round slot boundary so bars don't jitter
        cutoff_ts = now - window_sec
        cutoff_ts = int(cutoff_ts / slot_duration) * slot_duration  # seconds per slot

        # Bucket ALL raw events from the bars deque into fixed slots
        slots = [(0, 0, 0)] * num_slots  # (gross, saved, comp_gross)
        for bar in self.bars:
            ts = bar[0]
            if ts < cutoff_ts:
                continue
            slot_idx = min(int((ts - cutoff_ts) / slot_duration), num_slots - 1)
            g, s, cg = slots[slot_idx]
            slots[slot_idx] = (g + bar[1], s + bar[2], cg + (bar[3] if len(bar) > 3 else 0))

        # Bar sizing
        bar_w = max(2, (gw // num_slots) - BAR_GAP)
        dyn_step = bar_w + BAR_GAP

        if slots:
            max_val = max((s[0] for s in slots), default=1000)
            target_peak = max(max_val * 1.3, 500)
            if target_peak > self.peak_tokens:
                self.peak_tokens = target_peak
            else:
                self.peak_tokens = self.peak_tokens * shrink + target_peak * (1 - shrink)

        # Grid lines + Y labels
        num_grid = 5
        for i in range(num_grid + 1):
            y = gt + (gh * i / num_grid)
            c.create_line(gl, y, gr, y, fill=GRID_COLOR, width=1)
            val = int(self.peak_tokens * (1 - i / num_grid))
            label = f"{val // 1000}k" if val >= 1000 else str(val)
            c.create_text(gl - 8, y, text=label, anchor="e",
                          font=("JetBrains Mono", 8), fill=TEXT_COLOR)

        # Y axis label
        c.create_text(12, (gt + gb) // 2, text="tokens",
                      anchor="w", font=("JetBrains Mono", 8), fill=TEXT_COLOR, angle=90)

        # Draw bars — one per slot, left to right, filling full width
        for i, (gross, saved, comp_gross) in enumerate(slots):
            x = gl + i * dyn_step

            if gross > 0:
                bar_h = min(gross / self.peak_tokens, 1.0) * gh
                yt = gb - bar_h
                c.create_rectangle(x, yt, x + bar_w, gb, fill=RED_DIM, outline="")

            if comp_gross > 0:
                bar_h = min(comp_gross / self.peak_tokens, 1.0) * gh
                yt = gb - bar_h
                c.create_rectangle(x, yt, x + bar_w, gb, fill=RED, outline="")
                if bar_h > gh * 0.6:
                    c.create_rectangle(x, yt, x + bar_w, yt + 2, fill=RED_GLOW, outline="")

            if saved > 0:
                bar_h = min(saved / self.peak_tokens, 1.0) * gh
                yt = gb - bar_h
                c.create_rectangle(x, yt, x + bar_w, gb, fill=GREEN, outline="")
                if bar_h > gh * 0.3:
                    c.create_rectangle(x, yt, x + bar_w, yt + 2, fill=GREEN_GLOW, outline="")

        # Time axis labels — 6 evenly spaced
        label_step = max(1, num_slots // 6)
        for i in range(0, num_slots, label_step):
            x = gl + i * dyn_step
            slot_ts = cutoff_ts + i * slot_duration
            label = time.strftime("%H:%M", time.localtime(slot_ts))
            c.create_text(x, gb + 12, text=label, anchor="n",
                          font=("JetBrains Mono", 8), fill=TEXT_COLOR)

        # Bottom axis
        c.create_line(gl, gb, gr, gb, fill=TEXT_COLOR, width=1)

        # Update header
        pct = round(self.total_saved / max(self.total_gross, 1) * 100, 1)
        self.lbl_stats.config(
            text=f"{self.total_events:,} events  |  {self._fmt(self.total_gross)} gross"
        )
        self.lbl_rate.config(text=f"{self._fmt(self.total_saved)} saved ({pct}%)")

        # Update tray tooltip if minimized
        if self.tray_icon:
            self.tray_icon.title = f"Token Saver: {self._fmt(self.total_saved)} saved ({pct}%)"

        self.root.after(SCROLL_MS, self._draw)

    # -- Debug leak log --

    def _toggle_debug(self, event=None):
        """Toggle debug leak log panel visibility."""
        if self.debug_visible:
            self.debug_frame.pack_forget()
            self.btn_debug.config(text="▶ LEAK LOG (click to expand)")
            self.debug_visible = False
        else:
            self.debug_frame.pack(fill=tk.BOTH, padx=10, pady=(0, 2), before=self.footer)
            self.btn_debug.config(text="▼ LEAK LOG (click to collapse)")
            self.debug_visible = True

    def _poll_leaks(self):
        """Poll for zero-savings events (>500 tokens) and display in debug log."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=2)
            rows = conn.execute(
                "SELECT timestamp, event_type, tool_name, original_chars, saved_chars, notes "
                "FROM savings WHERE timestamp > ? AND saved_chars = 0 AND original_chars > 2000 "
                "ORDER BY timestamp ASC",
                (self._last_debug_ts,),
            ).fetchall()

            # Leak summary for header badge
            leak_summary = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(original_chars)/4, 0) as tokens "
                "FROM savings WHERE saved_chars = 0 AND original_chars > 2000 "
                "AND timestamp > ?",
                (time.time() - self.MODES[self.view_mode]["window_sec"],),
            ).fetchone()
            conn.close()

            if leak_summary and leak_summary[0] > 0:
                self.lbl_leak_count.config(
                    text=f"{leak_summary[0]} leaks ({self._fmt(leak_summary[1])} tokens)")
            else:
                self.lbl_leak_count.config(text="no leaks")

            if rows:
                self._last_debug_ts = rows[-1][0]
                self.debug_text.configure(state="normal")

                for ts, etype, tool, orig, saved, notes in rows:
                    tokens = orig // CHARS_PER_TOKEN
                    time_str = time.strftime("%H:%M:%S", time.localtime(ts))
                    notes_short = (notes or "")[:80]

                    if tokens > 5000:
                        tag = "leak_high"
                    elif tokens > 1000:
                        tag = "leak_med"
                    else:
                        tag = "leak_low"

                    self.debug_text.insert("end", f"[{time_str}] ", "timestamp")
                    self.debug_text.insert("end",
                        f"{etype:<22} {tool:<6} {tokens:>7,}t leaked  {notes_short}\n", tag)

                self.debug_text.see("end")
                self.debug_text.configure(state="disabled")

        except Exception:
            pass

        self.root.after(2000, self._poll_leaks)


def main():
    root = tk.Tk()
    app = TokenDashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
