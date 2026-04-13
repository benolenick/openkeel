"""OpenKeel 2.0 — Main application window.

Clean terminal + bubble token saver + hyphae memory.
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame,
)

from .terminal import TerminalWidget
from .widgets import BPHDialWidget, MiniDialWidget, StatusDot
from .session_watcher import SessionWatcher
from .settings import SettingsDialog, load_settings, save_settings
from .theme import build_stylesheet, DARK_BG, TOOLBAR_BG


class OpenKeelWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._settings = load_settings()
        self.setWindowTitle("OpenKeel 2.0")
        self.resize(1000, 650)

        self._build_ui()
        self._apply_theme()
        self._start_timers()
        self._terminal.setFocus()

    def _build_ui(self):
        # Central frame with accent border
        self._outer = QFrame()
        self._outer.setFrameShape(QFrame.Shape.Box)
        self.setCentralWidget(self._outer)

        main_layout = QVBoxLayout(self._outer)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Toolbar
        main_layout.addWidget(self._build_toolbar())

        # Terminal
        self._terminal = TerminalWidget(self)
        main_layout.addWidget(self._terminal, 1)

        # Status bar
        main_layout.addWidget(self._build_status_bar())

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(52)
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(12)

        # Brand
        brand = QLabel("OPENKEEL")
        brand.setObjectName("brand")
        layout.addWidget(brand)

        # Launch Claude button
        self._launch_btn = QPushButton("\u25B6 Claude")
        self._launch_btn.setObjectName("launch-btn")
        self._launch_btn.setFixedHeight(28)
        self._launch_btn.setToolTip("Launch Claude Code in the terminal")
        self._launch_btn.clicked.connect(self._launch_claude)
        layout.addWidget(self._launch_btn)

        layout.addStretch()

        # BPH dial
        self._bph_dial = BPHDialWidget()
        layout.addWidget(self._bph_dial)

        # Model dials (Opus, Sonnet, Haiku, Local)
        self._model_dials = {}
        for lane in ("opus", "sonnet", "haiku", "local"):
            dial = MiniDialWidget(lane)
            self._model_dials[lane] = dial
            layout.addWidget(dial)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #333;")
        layout.addWidget(sep)

        # Hyphae status
        hyphae_label = QLabel("Hyphae")
        hyphae_label.setStyleSheet("color: #666; font-size: 10px;")
        self._hyphae_dot = StatusDot()
        layout.addWidget(hyphae_label)
        layout.addWidget(self._hyphae_dot)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color: #333;")
        layout.addWidget(sep2)

        # Local LLM status
        llm_label = QLabel("LLM")
        llm_label.setStyleSheet("color: #666; font-size: 10px;")
        self._llm_dot = StatusDot()
        layout.addWidget(llm_label)
        layout.addWidget(self._llm_dot)

        # Settings gear
        gear = QPushButton("\u2699")
        gear.setObjectName("settings-btn")
        gear.setFixedSize(32, 32)
        gear.setFont(QFont("", 16))
        gear.setToolTip("Settings")
        gear.clicked.connect(self._open_settings)
        layout.addWidget(gear)

        return toolbar

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(24)
        bar.setStyleSheet(f"background-color: {TOOLBAR_BG}; border-top: 1px solid #333;")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(16)

        self._status_bph = QLabel("BPH: --")
        self._status_bph.setObjectName("status")
        layout.addWidget(self._status_bph)

        self._status_quota = QLabel("Quota: --")
        self._status_quota.setObjectName("status")
        layout.addWidget(self._status_quota)

        layout.addStretch()

        self._status_routing = QLabel("")
        self._status_routing.setObjectName("status")
        layout.addWidget(self._status_routing)

        self._status_model = QLabel("")
        self._status_model.setObjectName("status")
        layout.addWidget(self._status_model)

        return bar

    def _apply_theme(self):
        s = self._settings
        accent = s.get("theme_color", "#FF6611")

        # Stylesheet
        self.setStyleSheet(build_stylesheet(accent))

        # Border
        self._outer.setStyleSheet(
            f"QFrame {{ border: 3px solid {accent}; background-color: {DARK_BG}; }}"
        )

        # Terminal font
        font_family = s.get("font_family", "Cascadia Mono")
        font_size = s.get("font_size", 11)
        self._terminal.setFont(QFont(font_family, font_size))

        # Opacity
        opacity = s.get("opacity", 100)
        self.setWindowOpacity(opacity / 100.0)

        # BPH dial accent
        self._bph_dial.set_accent(accent)

        # Status bar info
        routing = s.get("routing", "flat")
        cli = s.get("cli_model", "sonnet")
        runner = s.get("runner", "haiku_api")
        self._status_routing.setText(f"Route: {routing}")
        self._status_model.setText(f"{cli} + {runner}")

    def _start_timers(self):
        # Session watcher — tails Claude Code JSONL for exact token usage
        self._watcher = SessionWatcher(self, poll_ms=2000)
        self._watcher.token_update.connect(self._on_token_update)
        self._watcher.session_found.connect(
            lambda p: self._status_bph.setText(f"Tracking: {Path(p).stem[:12]}...")
        )
        self._watcher.start()

        # Refresh status every 10s
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(10_000)
        # Initial refresh
        QTimer.singleShot(500, self._refresh_status)

    def _on_token_update(self, lane: str, input_tok: int, output_tok: int,
                         cache_read: int, cache_create: int):
        """Route exact token counts from session watcher to the correct dial."""
        dial = self._model_dials.get(lane)
        if dial:
            dial.add_tokens(input_tok, output_tok, cache_read, cache_create)

        # Also update BPH if it's a Sonnet call (main quota burner)
        if lane == "sonnet":
            self._bph_dial.log_completion(sonnet_calls=1)

    def _refresh_status(self):
        """Update status bar, hyphae dot, LLM dot."""
        from openkeel.quota import get_usage

        # Quota
        try:
            usage = get_usage()
            bph = usage["bph"]
            pct = usage["pct"]
            self._status_bph.setText(f"BPH: {bph:.1f}%/hr")
            self._status_quota.setText(f"Quota: {pct:.1f}%")
        except Exception:
            pass

        # Hyphae
        if self._settings.get("hyphae_enabled", True):
            try:
                from openkeel.hyphae import is_available
                if is_available():
                    self._hyphae_dot.set_status(True, "Hyphae connected")
                else:
                    self._hyphae_dot.set_status(False, "Hyphae offline")
            except Exception:
                self._hyphae_dot.set_offline("Hyphae error")
        else:
            self._hyphae_dot.set_offline("Hyphae disabled")

        # Local LLM
        runner = self._settings.get("runner", "haiku_api")
        if runner == "local":
            try:
                import urllib.request
                import json
                req = urllib.request.Request("http://localhost:11434/api/tags")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                    models = [m["name"] for m in data.get("models", [])]
                    local_model = self._settings.get("local_model", "")
                    if any(local_model in m for m in models):
                        self._llm_dot.set_status(True, f"{local_model} loaded")
                    else:
                        self._llm_dot.set_status(False, f"Ollama up, {local_model} not loaded")
            except Exception:
                self._llm_dot.set_status(False, "Ollama offline")
        else:
            self._llm_dot.set_offline("Local LLM not configured")

    def _launch_claude(self):
        """Send 'claude' command to the terminal PTY."""
        pty = getattr(self._terminal, "_pty", None)
        if pty and pty.isalive():
            pty.write("claude\n")
            self._launch_btn.setEnabled(False)
            self._launch_btn.setText("Running...")
            # Re-enable after 3s in case they exit and want to relaunch
            QTimer.singleShot(3000, self._reset_launch_btn)

    def _reset_launch_btn(self):
        self._launch_btn.setEnabled(True)
        self._launch_btn.setText("\u25B6 Claude")

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec():
            self._settings = load_settings()
            self._apply_theme()
            self._refresh_status()

    def closeEvent(self, event):
        self._terminal.close()
        super().closeEvent(event)


def main():
    """Launch the OpenKeel 2.0 GUI."""
    app = QApplication(sys.argv)
    app.setApplicationName("OpenKeel")

    # Dark palette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(DARK_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#cccccc"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#111111"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#cccccc"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1a1a1a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#cccccc"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#FF6611"))
    app.setPalette(palette)

    window = OpenKeelWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
