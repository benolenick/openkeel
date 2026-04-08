"""Calcifer's Ladder — runner pool monitor.

Shows real-time usage dials for all hard-wired runners:
  Cloud : Opus, Sonnet, Haiku        (from proxy_trace.jsonl)
  Local : gemma4:e2b  @kaloth 3070
          qwen2.5:3b  @jagg  3090
          gemma4:26b  @jagg  3090    (from Ollama /api/ps)

Launch:
    python -m openkeel.calcifer.ladder_window
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QLinearGradient, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow,
    QVBoxLayout, QWidget, QFrame,
)

# ── Runner registry ──────────────────────────────────────────────────────────

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

# Ollama host → runner mapping: (host_key, model_name) → runner_id
OLLAMA_HOSTS = {
    "kaloth": "http://127.0.0.1:11434",
    "jagg":   "http://192.168.0.224:11434",
}
OLLAMA_TOTAL_VRAM = {
    "kaloth": 8 * 1024**3,   # RTX 3070  ~8 GB
    "jagg":   24 * 1024**3,  # RTX 3090 ~24 GB
}
OLLAMA_MODEL_MAP: dict[tuple[str, str], str] = {
    ("kaloth", "gemma4:e2b"):   "gemma4_small",
    ("jagg",   "qwen2.5:3b"):   "qwen25",
    ("jagg",   "gemma4:26b"):   "gemma4_large",
}

PROXY_TRACE = Path.home() / ".openkeel/proxy_trace.jsonl"

DARK_BG   = "#0d0d0d"
PANEL_BG  = "#141414"
BORDER    = "#2a2a2a"
DIM_TEXT  = "#666666"
LIGHT_TXT = "#cccccc"
ORANGE    = "#FF6611"

DECAY_RATE   = 0.94   # multiplied per 500 ms tick  (half-life ≈ 5.5 s)
CLOUD_SPIKE  = 100.0  # spike on new proxy trace event
VRAM_SCALE   = 1.8    # stretch VRAM % so a 30% VRAM model shows ~55% on dial


# ── Shared usage state ────────────────────────────────────────────────────────

class UsageState:
    """Thread-safe dict of runner_id → 0.0-100.0."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values: Dict[str, float] = {r[0]: 0.0 for r in RUNNERS}
        self._last_spike: Dict[str, float] = {r[0]: 0.0 for r in RUNNERS}

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

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._values)


USAGE = UsageState()


# ── Background monitors ───────────────────────────────────────────────────────

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


def _local_monitor(stop: threading.Event):
    """Poll Ollama /api/ps on both hosts and set VRAM-based floor values."""
    while not stop.is_set():
        for host_key, base_url in OLLAMA_HOSTS.items():
            try:
                req = urllib.request.Request(
                    f"{base_url}/api/ps",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    data = json.loads(resp.read())

                total_vram = OLLAMA_TOTAL_VRAM[host_key]
                for m in data.get("models", []):
                    model_name = m.get("name", "").split(":")[0] + ":" + m.get("name", "").split(":")[-1]
                    # normalise: "gemma4:e2b" etc.
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
                    # Check if it's a "fresh" load (expiry within 5 min) vs permanent pin
                    # Permanent pin has year > 2100 in expires_at
                    is_permanent = "2318" in expires_at or "2200" in expires_at or "2100" in expires_at

                    if size_vram > 0:
                        vram_pct = min(100.0, (size_vram / total_vram) * 100 * VRAM_SCALE)
                        if is_permanent:
                            # loaded permanently: show a warm floor
                            USAGE.set_floor(rid, min(vram_pct, 25.0))
                        else:
                            # recently loaded/used: show real VRAM %
                            USAGE.spike(rid, vram_pct)

            except Exception:
                pass

        time.sleep(2.0)


# ── Dial widget ───────────────────────────────────────────────────────────────

def _value_color(v: float) -> QColor:
    """Green → orange → red gradient across 0-100."""
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
        b = int(20)
    return QColor(r, g, b)


class DialWidget(QWidget):
    """Analog-style gauge dial (270° sweep).

    The arc starts at 7:30 (SW) and sweeps clockwise through 12:00 to 4:30 (SE).
    """

    TRACK_WIDTH  = 10
    VALUE_WIDTH  = 10
    START_ANGLE  = 225 * 16   # Qt: CCW from East
    FULL_SWEEP   = -270 * 16  # negative = clockwise in Qt

    def __init__(self, runner_id: str, label: str, sublabel: str, accent: str):
        super().__init__()
        self.runner_id = runner_id
        self._label    = label
        self._sublabel = sublabel
        self._accent   = accent
        self._value    = 0.0
        self._anim     = 0.0  # smoothed display value
        self.setMinimumSize(130, 160)

    def set_value(self, v: float):
        self._value = max(0.0, min(100.0, v))
        # smooth toward target
        diff = self._value - self._anim
        self._anim += diff * 0.35
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        margin = 12
        label_h = 36
        diam = min(w - 2 * margin, h - label_h - 2 * margin)
        diam = max(diam, 60)
        x = (w - diam) // 2
        y = margin
        rect = QRectF(x, y, diam, diam)
        cx = x + diam / 2
        cy = y + diam / 2

        # ── Background circle ──
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(PANEL_BG))
        p.drawEllipse(rect)

        # ── Track arc ──
        pen = QPen(QColor("#222222"), self.TRACK_WIDTH, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect.adjusted(8, 8, -8, -8), self.START_ANGLE, self.FULL_SWEEP)

        # ── Value arc ──
        v = self._anim
        if v > 0.5:
            span = int(self.FULL_SWEEP * v / 100)
            arc_rect = rect.adjusted(8, 8, -8, -8)
            col = _value_color(v)
            pen2 = QPen(col, self.VALUE_WIDTH, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen2)
            p.drawArc(arc_rect, self.START_ANGLE, span)

        # ── Needle dot at tip ──
        if v > 0.5:
            angle_deg = 225 - (270 * v / 100)
            angle_rad = math.radians(angle_deg)
            r_needle = (diam / 2) - 12
            nx = cx + r_needle * math.cos(angle_rad)
            ny = cy - r_needle * math.sin(angle_rad)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_value_color(v))
            p.drawEllipse(QRectF(nx - 4, ny - 4, 8, 8))

        # ── Center value ──
        p.setPen(QPen(QColor("#ffffff")))
        font = QFont("Monospace", int(diam * 0.18), QFont.Weight.Bold)
        p.setFont(font)
        val_str = f"{int(v)}"
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(val_str)
        th = fm.ascent()
        p.drawText(int(cx - tw / 2), int(cy + th / 2.5), val_str)

        # ── Accent label ──
        p.setPen(QPen(QColor(self._accent)))
        font2 = QFont("Monospace", 8, QFont.Weight.Bold)
        p.setFont(font2)
        lw = p.fontMetrics().horizontalAdvance(self._label)
        p.drawText(int(cx - lw / 2), int(y + diam - 6), self._label)

        # ── Sub-label ──
        p.setPen(QPen(QColor(DIM_TEXT)))
        font3 = QFont("Monospace", 7)
        p.setFont(font3)
        sw = p.fontMetrics().horizontalAdvance(self._sublabel)
        p.drawText(int(cx - sw / 2), int(y + diam + 14), self._sublabel)

        p.end()


# ── Section row ───────────────────────────────────────────────────────────────

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {DIM_TEXT}; font: 9px 'Monospace'; letter-spacing: 3px;")
    return lbl


def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {BORDER};")
    return line


# ── Main window ───────────────────────────────────────────────────────────────

class LadderWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Calcifer's Ladder")
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {DARK_BG}; color: {LIGHT_TXT}; }}
            QLabel {{ color: {LIGHT_TXT}; }}
        """)

        # Build dials
        self._dials: Dict[str, DialWidget] = {}
        for rid, label, sublabel, accent, _grp in RUNNERS:
            self._dials[rid] = DialWidget(rid, label, sublabel, accent)

        # Layout
        root = QWidget()
        self.setCentralWidget(root)
        vlay = QVBoxLayout(root)
        vlay.setContentsMargins(16, 12, 16, 12)
        vlay.setSpacing(8)

        # Title bar
        title_row = QHBoxLayout()
        brand = QLabel("🔥  CALCIFER'S LADDER")
        brand.setStyleSheet(f"color: {ORANGE}; font: bold 14px 'Monospace'; letter-spacing: 2px;")
        title_row.addWidget(brand)
        title_row.addStretch()
        self._status_lbl = QLabel("monitoring…")
        self._status_lbl.setStyleSheet(f"color: {DIM_TEXT}; font: 10px 'Monospace';")
        title_row.addWidget(self._status_lbl)
        vlay.addLayout(title_row)
        vlay.addWidget(_hline())

        # Cloud group
        vlay.addWidget(_section_label("CLOUD"))
        cloud_row = QHBoxLayout()
        cloud_row.setSpacing(6)
        for rid, _, _, _, grp in RUNNERS:
            if grp == "cloud":
                cloud_row.addWidget(self._dials[rid])
        vlay.addLayout(cloud_row)

        vlay.addSpacing(4)
        vlay.addWidget(_hline())

        # Local group
        vlay.addWidget(_section_label("LOCAL"))
        local_row = QHBoxLayout()
        local_row.setSpacing(6)
        for rid, _, _, _, grp in RUNNERS:
            if grp == "local":
                local_row.addWidget(self._dials[rid])
        vlay.addLayout(local_row)

        vlay.addWidget(_hline())
        vlay.addWidget(self._status_lbl)

        self.setMinimumSize(820, 400)

        # Timers
        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick)
        self._decay_timer.start(500)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(3000)

        # Background threads
        self._stop = threading.Event()
        threading.Thread(target=_cloud_monitor, args=(self._stop,), daemon=True).start()
        threading.Thread(target=_local_monitor, args=(self._stop,), daemon=True).start()

    def _tick(self):
        USAGE.tick_decay()
        snap = USAGE.snapshot()
        for rid, dial in self._dials.items():
            dial.set_value(snap[rid])

    def _update_status(self):
        snap = USAGE.snapshot()
        active = [rid for rid, v in snap.items() if v > 5]
        if active:
            names = ", ".join(
                next(r[1] for r in RUNNERS if r[0] == rid) for rid in active
            )
            self._status_lbl.setText(f"active: {names}")
        else:
            self._status_lbl.setText("idle — watching proxy + ollama")

    def closeEvent(self, event):
        self._stop.set()
        super().closeEvent(event)


# ── Public API (for routing code to call later) ───────────────────────────────

def report_usage(runner_id: str, value: float = 100.0):
    """Call this from routing code when dispatching to a runner."""
    USAGE.spike(runner_id, value)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication.instance() or QApplication(sys.argv)
    win = LadderWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
