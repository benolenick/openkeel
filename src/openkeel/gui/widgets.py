"""Custom widgets for OpenKeel 2.0 — BPH dial, status indicators."""

import math
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QConicalGradient
from PySide6.QtWidgets import QWidget


class BPHDialWidget(QWidget):
    """Pace gauge — are you ahead or behind your weekly token budget?

    Left/green = under budget (good), center = on pace, right/red = over budget.
    Budget is split into 8-hour working blocks across the week.
    """

    OEQ_WEIGHTS = {
        "sonnet": (0.2, 1.0),
        "opus":   (0.2, 1.0),
        "haiku":  (0.05, 0.25),
        "local":  (0.0, 0.0),
    }
    HOURS_PER_DAY = 8   # working hours per day
    DAYS_PER_WEEK = 7   # days in reset window

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 64)
        self._accent = "#FF6611"

        # Quota state
        self._weekly_limit = 5_000_000
        self._week_used = 0          # OEQ used this week (from quota.json)
        self._session_oeq = 0        # OEQ used this session
        self._hours_elapsed = 0.0    # working hours elapsed in this week
        self._total_working_hours = self.HOURS_PER_DAY * self.DAYS_PER_WEEK  # 56h

        # Pace: 0.0 = exactly on budget, negative = under, positive = over
        # Range roughly -1.0 to +1.0
        self._pace = 0.0
        self._display_pace = 0.0

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate)
        self._anim_timer.start(80)

    def set_accent(self, color: str):
        self._accent = color
        self.update()

    def set_quota_info(self, week_used: int, weekly_limit: int, hours_to_reset: float):
        """Update quota state from quota.json."""
        self._week_used = week_used
        self._weekly_limit = weekly_limit
        # Convert hours_to_reset into working hours elapsed
        total_hours = self.HOURS_PER_DAY * self.DAYS_PER_WEEK
        hours_gone = (self.DAYS_PER_WEEK * 24) - hours_to_reset
        # Scale calendar hours to working hours (8h/24h ratio)
        self._hours_elapsed = hours_gone * (self.HOURS_PER_DAY / 24.0)
        self._total_working_hours = total_hours
        self._calc_pace()

    def log_tokens(self, input_tok: int, output_tok: int, lane: str = "sonnet"):
        """Add tokens from session."""
        w = self.OEQ_WEIGHTS.get(lane, (0.2, 1.0))
        self._session_oeq += input_tok * w[0] + output_tok * w[1]
        self._calc_pace()

    def log_completion(self, sonnet_calls: int = 1):
        self.log_tokens(2600, 800, "sonnet")

    def _calc_pace(self):
        """Calculate pace: how far ahead/behind budget.

        budget_now = (hours_elapsed / total_working_hours) * weekly_limit
        actual = week_used + session_oeq
        pace = (actual - budget_now) / weekly_limit
        """
        if self._weekly_limit <= 0 or self._total_working_hours <= 0:
            self._pace = 0
            return

        budget_fraction = min(self._hours_elapsed / self._total_working_hours, 1.0)
        budget_now = budget_fraction * self._weekly_limit
        actual = self._week_used + self._session_oeq

        # Normalize: 0 = on pace, +1 = used entire week budget already, -1 = used nothing
        if self._weekly_limit > 0:
            self._pace = (actual - budget_now) / self._weekly_limit
        else:
            self._pace = 0

        # Clamp to [-1, 1]
        self._pace = max(-1.0, min(1.0, self._pace))

    def _animate(self):
        diff = self._pace - self._display_pace
        if abs(diff) < 0.002:
            self._display_pace = self._pace
        else:
            self._display_pace += diff * 0.12
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 6
        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # Background arc (full sweep)
        pen = QPen(QColor("#282828"), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 225 * 16, -270 * 16)

        # Gradient ticks — green on left, red on right
        # Draw colored background arc segments
        segments = 20
        for i in range(segments):
            frac = i / segments  # 0=left, 1=right
            # Color: green -> yellow -> red
            if frac < 0.4:
                color = QColor(80, 200, 80)
            elif frac < 0.55:
                color = QColor(180, 200, 60)
            elif frac < 0.7:
                color = QColor(220, 180, 50)
            elif frac < 0.85:
                color = QColor(240, 120, 50)
            else:
                color = QColor(240, 60, 50)

            pen = QPen(color, 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            seg_start = 225 * 16 + int(-270 * frac * 16)
            seg_span = int(-270 * (1.0 / segments) * 16)
            p.drawArc(rect.adjusted(2, 2, -2, -2), seg_start, seg_span)

        # Needle position: pace maps to arc position
        # pace -1 = far left (green), 0 = center, +1 = far right (red)
        # Shift so center of arc = pace 0
        needle_frac = (self._display_pace + 1.0) / 2.0  # map [-1,1] to [0,1]
        needle_frac = max(0.0, min(1.0, needle_frac))

        # Draw needle
        import math
        angle_deg = 225 - 270 * needle_frac
        angle_rad = math.radians(angle_deg)
        nr = radius - 2
        nx = cx + nr * math.cos(angle_rad)
        ny = cy - nr * math.sin(angle_rad)
        # Inner point
        ir = radius * 0.3
        ix = cx + ir * math.cos(angle_rad)
        iy = cy - ir * math.sin(angle_rad)

        pen = QPen(QColor("#eeeeee"), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(ix, iy), QPointF(nx, ny))

        # Center dot
        p.setBrush(QColor("#cccccc"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 3, 3)

        # Pace text
        p.setPen(QColor("#999999"))
        font = QFont("monospace", 6)
        p.setFont(font)
        if self._display_pace < -0.05:
            label = "UNDER"
        elif self._display_pace > 0.05:
            label = "OVER"
        else:
            label = "ON PACE"
        p.drawText(QRectF(0, cy + 10, w, 12), Qt.AlignmentFlag.AlignCenter, label)

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
        real = input_tok + output_tok  # actual billable tokens at full rate
        self._input_tok += input_tok
        self._output_tok += output_tok
        self._cache_read += cache_read + cache_create
        self._total_tokens += real
        self._calls += 1
        self._events.append((time.time(), real))
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
            f"Cache: {self._cache_read:,}\n"
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
