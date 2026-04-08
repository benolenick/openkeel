"""Calcifer Window — terminal + runner dials.

Clean shell: terminal fills the space, 6 agent dials pinned along the bottom.
No profile, mission, shell-selector, mode, governance cruft.

Launch:
    python -m openkeel.calcifer.calcifer_window
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

from openkeel.gui.terminal import TerminalWidget
from openkeel.calcifer.ladder_window import (
    DialWidget, USAGE, RUNNERS,
    _cloud_monitor, _local_monitor,
    DARK_BG, PANEL_BG, BORDER, DIM_TEXT, LIGHT_TXT, ORANGE,
)

BORDER_WIDTH = 2
TOOLBAR_BG  = "#111111"
DIAL_STRIP_H = 140     # px — height of the bottom dial strip


def _stylesheet(accent: str) -> str:
    return f"""
QMainWindow, QWidget {{ background: {DARK_BG}; color: {LIGHT_TXT}; }}
#frame {{
    border: {BORDER_WIDTH}px solid {accent};
    background: {DARK_BG};
    border-radius: 4px;
}}
#toolbar {{
    background: {TOOLBAR_BG};
    border-bottom: 1px solid #1e1e1e;
}}
#dial-strip {{
    background: {PANEL_BG};
    border-top: 1px solid {BORDER};
}}
QPushButton#btn {{
    background: {accent};
    color: {DARK_BG};
    border: none;
    border-radius: 3px;
    padding: 3px 12px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton#btn:hover {{ background: #FF8833; }}
QPushButton#btn-ghost {{
    background: transparent;
    color: {DIM_TEXT};
    border: none;
    font-size: 16px;
    padding: 2px 6px;
}}
QPushButton#btn-ghost:hover {{ color: {accent}; }}
"""


class CalciferWindow(QMainWindow):
    """Terminal window with Calcifer Ladder dials along the bottom."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Calcifer")
        self.setStyleSheet(_stylesheet(ORANGE))

        # ── Outer border frame ──
        frame = QFrame()
        frame.setObjectName("frame")
        vlay = QVBoxLayout(frame)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        # ── Toolbar ──
        vlay.addWidget(self._build_toolbar())

        # ── Terminal ──
        self._terminal = TerminalWidget(shell="bash")
        self._terminal.process_finished.connect(self.close)
        vlay.addWidget(self._terminal, stretch=1)

        # ── Dial strip ──
        vlay.addWidget(self._build_dial_strip())

        self.setCentralWidget(frame)

        # Size: match terminal hint + chrome
        hint = self._terminal.sizeHint()
        self.resize(
            max(hint.width() + BORDER_WIDTH * 2, 900),
            hint.height() + 40 + DIAL_STRIP_H + BORDER_WIDTH * 2,
        )

        self._terminal.setFocus()

        # ── Decay timer ──
        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick)
        self._decay_timer.start(500)

        # ── Background monitors ──
        self._stop = threading.Event()
        threading.Thread(target=_cloud_monitor, args=(self._stop,), daemon=True).start()
        threading.Thread(target=_local_monitor, args=(self._stop,), daemon=True).start()

    # ── Toolbar ──────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("toolbar")
        bar.setFixedHeight(36)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(10)

        brand = QLabel("🔥  CALCIFER")
        brand.setStyleSheet(
            f"color: {ORANGE}; font: bold 13px 'Monospace'; letter-spacing: 2px;"
        )
        lay.addWidget(brand)
        lay.addStretch()

        claude_btn = QPushButton("Claude")
        claude_btn.setObjectName("btn")
        claude_btn.setToolTip("Launch Claude Code")
        claude_btn.clicked.connect(self._launch_claude)
        lay.addWidget(claude_btn)

        settings_btn = QPushButton("⚙")
        settings_btn.setObjectName("btn-ghost")
        settings_btn.setFixedWidth(32)
        lay.addWidget(settings_btn)

        return bar

    def _launch_claude(self) -> None:
        self._terminal._write_input(
            b"claude --dangerously-skip-permissions\r"
        )

    # ── Dial strip ────────────────────────────────────────────────────────────

    def _build_dial_strip(self) -> QWidget:
        strip = QWidget()
        strip.setObjectName("dial-strip")
        strip.setFixedHeight(DIAL_STRIP_H)

        lay = QHBoxLayout(strip)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(4)

        self._dials: dict[str, DialWidget] = {}

        # Divider between cloud and local
        cloud_done = False
        for rid, label, sublabel, accent, group in RUNNERS:
            if group == "local" and not cloud_done:
                div = QFrame()
                div.setFrameShape(QFrame.Shape.VLine)
                div.setStyleSheet(f"color: {BORDER};")
                lay.addWidget(div)
                cloud_done = True

            dial = DialWidget(rid, label, sublabel, accent)
            dial.setMinimumSize(100, DIAL_STRIP_H - 12)
            self._dials[rid] = dial
            lay.addWidget(dial, stretch=1)

        return strip

    # ── Tick ─────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        USAGE.tick_decay()
        snap = USAGE.snapshot()
        for rid, dial in self._dials.items():
            dial.set_value(snap[rid])

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._stop.set()
        self._terminal.cleanup()
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    win = CalciferWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
