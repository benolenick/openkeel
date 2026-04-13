"""Custom widgets for OpenKeel 2.0 — BPH dial, status indicators."""

import math
import time

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QConicalGradient
from PySide6.QtWidgets import QWidget


class BPHDialWidget(QWidget):
    """Arc gauge showing Burn Per Hour — estimated % of weekly quota consumed per hour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 64)
        self._value = 0.0        # current BPH (0-10 typical range)
        self._display = 0.0      # animated display value
        self._max_val = 5.0      # scale max
        self._accent = "#FF6611"
        self._completions = []   # timestamps of recent bubble completions
        self._sonnet_calls = []  # timestamps + call counts

        # Animation timer
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate)
        self._anim_timer.start(50)

    def set_accent(self, color: str):
        self._accent = color
        self.update()

    def log_completion(self, sonnet_calls: int = 1):
        """Record a bubble completion with its Sonnet call count."""
        now = time.time()
        self._sonnet_calls.append((now, sonnet_calls))
        # Prune old entries (> 1 hour)
        cutoff = now - 3600
        self._sonnet_calls = [(t, c) for t, c in self._sonnet_calls if t > cutoff]
        self._recalc()

    def _recalc(self):
        """Recalculate BPH from recent completions."""
        if not self._sonnet_calls:
            self._value = 0
            return
        now = time.time()
        cutoff = now - 3600
        recent = [(t, c) for t, c in self._sonnet_calls if t > cutoff]
        if not recent:
            self._value = 0
            return
        total_calls = sum(c for _, c in recent)
        span_hours = (now - recent[0][0]) / 3600 if len(recent) > 1 else 1.0
        # Estimate: each Sonnet call ~ 2600 OEQ tokens, weekly quota ~ 5M
        oeq_per_call = 2600
        weekly_quota = 5_000_000  # TODO: read from settings
        pct_per_call = (oeq_per_call / weekly_quota) * 100
        self._value = total_calls * pct_per_call / max(span_hours, 0.01)

    def _animate(self):
        """Smooth animation toward target value."""
        diff = self._value - self._display
        if abs(diff) < 0.01:
            self._display = self._value
        else:
            self._display += diff * 0.15
        self.update()

    def _value_color(self, val: float) -> QColor:
        """Green (low BPH) → Yellow → Red (high BPH)."""
        t = min(val / self._max_val, 1.0)
        if t < 0.5:
            r = int(100 + 155 * (t * 2))
            g = int(200)
            b = 50
        else:
            r = 255
            g = int(200 * (1 - (t - 0.5) * 2))
            b = 50
        return QColor(r, g, b)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 6

        # Background arc
        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        pen = QPen(QColor("#333333"), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 225 * 16, -270 * 16)

        # Value arc
        frac = min(self._display / self._max_val, 1.0)
        if frac > 0.01:
            color = self._value_color(self._display)
            pen = QPen(color, 4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            span = int(-270 * frac * 16)
            p.drawArc(rect, 225 * 16, span)

        # Center text
        p.setPen(QColor("#cccccc"))
        font = QFont("monospace", 9, QFont.Weight.Bold)
        p.setFont(font)
        text = f"{self._display:.1f}"
        p.drawText(QRectF(0, cy - 10, w, 20), Qt.AlignmentFlag.AlignCenter, text)

        # Label
        p.setPen(QColor("#666666"))
        font.setPointSize(7)
        font.setBold(False)
        p.setFont(font)
        p.drawText(QRectF(0, cy + 6, w, 16), Qt.AlignmentFlag.AlignCenter, "BPH")

        p.end()


class MiniDialWidget(QWidget):
    """Small arc gauge for per-model token rate (tokens/min, decays over time).

    Shows recent token throughput rather than cumulative total. The arc fills
    based on tokens/min over a sliding window, and decays back to zero when idle.
    Tooltip shows cumulative totals for reference.
    """

    LANE_COLORS = {
        "opus": "#CC77FF",   # purple
        "sonnet": "#4499FF",  # blue
        "haiku": "#44DDAA",   # teal
        "local": "#FFAA22",   # amber
    }

    WINDOW_SECS = 120  # sliding window for rate calculation

    def __init__(self, lane: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(48, 48)
        self._lane = lane
        self._color = self.LANE_COLORS.get(lane, "#888888")

        # Rate tracking — list of (timestamp, token_count)
        self._events = []

        # Cumulative totals (for tooltip)
        self._total_tokens = 0
        self._input_tok = 0
        self._output_tok = 0
        self._cache_read = 0
        self._calls = 0

        # Display
        self._rate = 0.0          # tokens/min (current)
        self._display_rate = 0.0  # animated
        self._max_rate = 5000.0   # auto-scales

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate)
        self._anim_timer.start(50)

    def add_tokens(self, input_tok: int, output_tok: int, cache_read: int = 0, cache_create: int = 0):
        """Add tokens from a single API call."""
        total = input_tok + output_tok + cache_read + cache_create
        self._input_tok += input_tok + cache_read + cache_create
        self._output_tok += output_tok
        self._total_tokens += total
        self._calls += 1
        self._events.append((time.time(), total))
        self._recalc_rate()
        self._update_tooltip()

    def _recalc_rate(self):
        """Calculate tokens/min over the sliding window."""
        now = time.time()
        cutoff = now - self.WINDOW_SECS
        self._events = [(t, n) for t, n in self._events if t > cutoff]

        if not self._events:
            self._rate = 0.0
            return

        total_in_window = sum(n for _, n in self._events)
        window_span = now - self._events[0][0]
        if window_span < 1:
            window_span = 1  # avoid division by zero on first event

        self._rate = (total_in_window / window_span) * 60  # tokens per minute

        # Auto-scale
        if self._rate > self._max_rate * 0.8:
            self._max_rate = self._rate * 1.5

    def _update_tooltip(self):
        self.setToolTip(
            f"{self._lane.upper()}\n"
            f"Rate: {self._rate:,.0f} tok/min\n"
            f"Calls: {self._calls}\n"
            f"Input: {self._input_tok:,}\n"
            f"Output: {self._output_tok:,}\n"
            f"Total: {self._total_tokens:,}"
        )

    def _animate(self):
        # Recalc rate every frame so it decays when no new tokens arrive
        self._recalc_rate()

        diff = self._rate - self._display_rate
        if abs(diff) < 1:
            self._display_rate = self._rate
        else:
            self._display_rate += diff * 0.12
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 4

        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # Background arc
        pen = QPen(QColor("#282828"), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 225 * 16, -270 * 16)

        # Value arc
        frac = min(self._display_rate / max(self._max_rate, 1), 1.0)
        if frac > 0.005:
            pen = QPen(QColor(self._color), 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            span = int(-270 * frac * 16)
            p.drawArc(rect, 225 * 16, span)

        # Rate display (compact)
        p.setPen(QColor("#bbbbbb"))
        font = QFont("monospace", 7, QFont.Weight.Bold)
        p.setFont(font)
        count_text = self._format_count(self._display_rate)
        p.drawText(QRectF(0, cy - 8, w, 16), Qt.AlignmentFlag.AlignCenter, count_text)

        # Lane label
        p.setPen(QColor(self._color))
        font.setPointSize(6)
        font.setBold(False)
        p.setFont(font)
        label = self._lane[:3].upper()
        p.drawText(QRectF(0, cy + 5, w, 12), Qt.AlignmentFlag.AlignCenter, label)

        p.end()

    @staticmethod
    def _format_count(n: float) -> str:
        """Compact rate: 0, 1.2K, 45K, 1.2M tok/min."""
        n = int(n)
        if n < 1000:
            return str(n)
        elif n < 100_000:
            return f"{n / 1000:.1f}K"
        elif n < 1_000_000:
            return f"{n // 1000}K"
        else:
            return f"{n / 1_000_000:.1f}M"


class StatusDot(QWidget):
    """Small colored dot indicating service status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self._color = "#666666"  # gray = unknown
        self._tooltip_text = ""

    def set_status(self, ok: bool, label: str = ""):
        self._color = "#44bb44" if ok else "#cc4444"
        self._tooltip_text = label
        self.setToolTip(label)
        self.update()

    def set_offline(self, label: str = ""):
        self._color = "#666666"
        self._tooltip_text = label
        self.setToolTip(label)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(self._color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 10, 10)
        p.end()
