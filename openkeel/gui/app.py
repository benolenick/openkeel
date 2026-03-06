"""OpenKeel Terminal — main window with neon-orange governance chrome."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from openkeel.core.overwatch import (
    OverwatchAlert, OverwatchConfig, OverwatchEngine, WATCHER_AGENT_NAMES,
)
from openkeel.gui.settings import SettingsDialog, load_settings, THEME_COLORS
from openkeel.gui.terminal import TerminalWidget

# ---------------------------------------------------------------------------
# Theme constants
# ---------------------------------------------------------------------------

ORANGE = "#FF6611"
DARK_BG = "#0d0d0d"
TOOLBAR_BG = "#1a1a1a"
BORDER_BG = "#111111"
TEXT_DIM = "#888888"
TEXT_LIGHT = "#cccccc"

BORDER_WIDTH = 3


def _build_stylesheet(accent: str) -> str:
    """Generate the main window stylesheet with the given accent color."""
    return f"""
QMainWindow {{
    background: {DARK_BG};
}}
#orange-border {{
    border: {BORDER_WIDTH}px solid {accent};
    background: {DARK_BG};
    border-radius: 4px;
}}
#toolbar {{
    background: {TOOLBAR_BG};
    border-bottom: 1px solid {accent};
    padding: 4px 8px;
}}
#toolbar QLabel {{
    color: {TEXT_DIM};
    font-size: 12px;
}}
#toolbar .value {{
    color: {TEXT_LIGHT};
}}
#brand {{
    color: {accent};
    font-size: 14px;
    font-weight: bold;
    letter-spacing: 2px;
}}
#status-bar {{
    background: {TOOLBAR_BG};
    border-top: 1px solid #333333;
    padding: 2px 8px;
}}
#status-bar QLabel {{
    color: {TEXT_DIM};
    font-size: 11px;
}}
QPushButton#toggle-on {{
    background: {accent};
    color: {DARK_BG};
    border: none;
    border-radius: 3px;
    padding: 3px 12px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton#toggle-on:hover {{
    background: #FF8833;
}}
QPushButton#toggle-off {{
    background: #444444;
    color: {TEXT_DIM};
    border: none;
    border-radius: 3px;
    padding: 3px 12px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton#toggle-off:hover {{
    background: #555555;
}}
QPushButton#settings-btn {{
    background: transparent;
    color: {TEXT_DIM};
    border: none;
    font-size: 16px;
    padding: 2px 6px;
}}
QPushButton#settings-btn:hover {{
    color: {accent};
}}
QPushButton#launch-btn {{
    background: {accent};
    color: {DARK_BG};
    border: none;
    border-radius: 3px;
    padding: 3px 10px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton#launch-btn:hover {{
    background: #FF8833;
}}
QComboBox {{
    background: #2a2a2a;
    color: {TEXT_LIGHT};
    border: 1px solid #444444;
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 11px;
    min-width: 90px;
}}
QComboBox:hover {{
    border-color: {accent};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: #2a2a2a;
    color: {TEXT_LIGHT};
    border: 1px solid {accent};
    selection-background-color: {accent};
    selection-color: {DARK_BG};
}}
QPushButton#overwatch-on {{
    background: #00AAFF;
    color: {DARK_BG};
    border: none;
    border-radius: 3px;
    padding: 3px 10px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton#overwatch-on:hover {{
    background: #33BBFF;
}}
QPushButton#overwatch-off {{
    background: #333333;
    color: {TEXT_DIM};
    border: none;
    border-radius: 3px;
    padding: 3px 10px;
    font-size: 11px;
}}
QPushButton#overwatch-off:hover {{
    background: #444444;
}}
#alert-bar {{
    background: #1a1a1a;
    border-top: 1px solid #333;
    padding: 4px 8px;
}}
#alert-bar QLabel {{
    font-size: 11px;
}}
#history-panel {{
    background: #111111;
    border-top: 1px solid #333;
}}
#history-panel QLabel {{
    font-size: 10px;
    font-family: "Cascadia Mono", monospace;
    padding: 1px 4px;
}}
QPushButton#history-toggle {{
    background: transparent;
    color: {TEXT_DIM};
    border: none;
    font-size: 11px;
    padding: 2px 8px;
}}
QPushButton#history-toggle:hover {{
    color: {accent};
}}
"""


STYLESHEET = _build_stylesheet(ORANGE)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class OpenKeelWindow(QMainWindow):
    """Neon-orange terminal window with OpenKeel governance toolbar."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpenKeel Terminal")
        self._governance_on = True

        # Load saved GUI settings
        self._gui_settings = load_settings()
        self._accent = self._gui_settings.get("theme_color", ORANGE)
        self.setStyleSheet(_build_stylesheet(self._accent))

        # Load OpenKeel state
        self._mission = self._load_mission()
        self._profile = self._load_profile()
        self._cached_profile_obj = None  # cached Profile for governance
        self._shell = "powershell.exe"
        self._active_mode = self._gui_settings.get("default_mode", "Normal")

        # -- Outer frame (neon accent border) --
        frame = QFrame()
        frame.setObjectName("orange-border")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        # -- Toolbar --
        toolbar = self._build_toolbar()
        frame_layout.addWidget(toolbar)

        # -- Overwatch engine --
        self._overwatch = OverwatchEngine(OverwatchConfig(
            enabled=False,
            on_alert=self._on_overwatch_alert,
        ))

        # -- Terminal --
        font_family = self._gui_settings.get("font_family", "Cascadia Mono")
        font_size = self._gui_settings.get("font_size", 11)
        self._terminal = TerminalWidget(shell="powershell.exe")
        self._terminal.process_finished.connect(self._on_shell_exit)
        self._terminal._overwatch_callback = self._overwatch.feed
        self._terminal._governance_callback = self._governance_check
        self._apply_terminal_font(font_family, font_size)

        # Governance counters
        self._count_blocked = 0
        self._count_allowed = 0
        self._count_gated = 0
        self._session_id = str(int(time.time()))
        frame_layout.addWidget(self._terminal, stretch=1)

        # -- Alert bar (Overwatch alerts) --
        self._alert_bar = self._build_alert_bar()
        self._alert_bar.setVisible(False)
        frame_layout.addWidget(self._alert_bar)

        # -- Command history panel --
        self._history_entries: list[dict] = []
        self._history_panel = self._build_history_panel()
        self._history_panel.setVisible(False)
        frame_layout.addWidget(self._history_panel)

        # -- Status bar --
        status = self._build_status_bar()
        frame_layout.addWidget(status)

        self.setCentralWidget(frame)

        # Size from terminal hint
        hint = self._terminal.sizeHint()
        self.resize(
            hint.width() + BORDER_WIDTH * 2,
            hint.height() + 80 + BORDER_WIDTH * 2,  # toolbar + status
        )

        # Apply saved opacity
        opacity = self._gui_settings.get("opacity", 100)
        if opacity < 100:
            self.setWindowOpacity(opacity / 100.0)

        # Focus terminal
        self._terminal.setFocus()

        # Periodic status refresh
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(5000)

        # Session start: search Memoria for relevant context
        self._memoria_context = ""
        QTimer.singleShot(2000, self._memoria_session_start)

    # ----- Toolbar -----

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(36)

        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(12)

        # Brand
        brand = QLabel("OPENKEEL")
        brand.setObjectName("brand")
        layout.addWidget(brand)

        # Separator
        layout.addWidget(self._sep())

        # Profile picker
        layout.addWidget(QLabel("Profile:"))
        self._profile_combo = QComboBox()
        self._profile_combo.addItem("(none)")
        for name in self._list_profiles():
            self._profile_combo.addItem(name)
        if self._profile:
            idx = self._profile_combo.findText(self._profile)
            if idx >= 0:
                self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.currentTextChanged.connect(self._on_profile_changed)
        layout.addWidget(self._profile_combo)

        # Separator
        layout.addWidget(self._sep())

        # Mission picker
        layout.addWidget(QLabel("Mission:"))
        self._mission_combo = QComboBox()
        self._mission_combo.addItem("(none)")
        for name in self._list_missions():
            self._mission_combo.addItem(name)
        if self._mission:
            idx = self._mission_combo.findText(self._mission)
            if idx >= 0:
                self._mission_combo.setCurrentIndex(idx)
        self._mission_combo.currentTextChanged.connect(self._on_mission_changed)
        layout.addWidget(self._mission_combo)

        # Separator
        layout.addWidget(self._sep())

        # Shell selector
        layout.addWidget(QLabel("Shell:"))
        self._shell_combo = QComboBox()
        shells = self._detect_shells()
        for display_name, _cmd in shells:
            self._shell_combo.addItem(display_name)
        self._shell_combo.currentIndexChanged.connect(self._on_shell_changed)
        self._available_shells = shells
        layout.addWidget(self._shell_combo)

        # Separator
        layout.addWidget(self._sep())

        # Mode selector
        layout.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        from openkeel.core.modes import list_modes
        for mode_name in list_modes():
            self._mode_combo.addItem(mode_name.capitalize())
        idx = self._mode_combo.findText(self._active_mode)
        if idx >= 0:
            self._mode_combo.setCurrentIndex(idx)
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        layout.addWidget(self._mode_combo)

        layout.addStretch()

        # Launch Claude button
        self._claude_btn = QPushButton("Claude")
        self._claude_btn.setObjectName("launch-btn")
        self._claude_btn.setToolTip("Launch Claude Code with --dangerously-skip-permissions")
        self._claude_btn.clicked.connect(self._launch_claude)
        layout.addWidget(self._claude_btn)

        # Overwatch agent picker + toggle
        self._overwatch_agent_combo = QComboBox()
        for name in WATCHER_AGENT_NAMES:
            self._overwatch_agent_combo.addItem(name.capitalize())
        self._overwatch_agent_combo.setToolTip("Choose which AI agent watches your session")
        self._overwatch_agent_combo.setFixedWidth(80)
        layout.addWidget(self._overwatch_agent_combo)

        self._overwatch_btn = QPushButton("Overwatch")
        self._overwatch_btn.setObjectName("overwatch-off")
        self._overwatch_btn.setToolTip("Toggle Overwatch — AI watcher that monitors terminal activity")
        self._overwatch_btn.clicked.connect(self._toggle_overwatch)
        layout.addWidget(self._overwatch_btn)

        # Settings button
        settings_btn = QPushButton("\u2699")
        settings_btn.setObjectName("settings-btn")
        settings_btn.setFixedWidth(32)
        settings_btn.setToolTip("Settings")
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)

        # Toggle button
        self._toggle_btn = QPushButton("ON")
        self._toggle_btn.setObjectName("toggle-on")
        self._toggle_btn.setFixedWidth(50)
        self._toggle_btn.clicked.connect(self._toggle_governance)
        layout.addWidget(self._toggle_btn)

        return toolbar

    @staticmethod
    def _sep() -> QLabel:
        s = QLabel("|")
        s.setStyleSheet("color: #333333;")
        return s

    # ----- Status bar -----

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("status-bar")
        bar.setFixedHeight(24)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(16)

        self._blocked_label = QLabel("Blocked: 0")
        self._allowed_label = QLabel("Allowed: 0")
        self._gated_label = QLabel("Gated: 0")

        layout.addWidget(self._blocked_label)
        layout.addWidget(self._allowed_label)
        layout.addWidget(self._gated_label)
        layout.addStretch()

        self._status_msg = QLabel("Ready")
        self._status_msg.setStyleSheet(f"color: {self._accent};")
        layout.addWidget(self._status_msg)

        # History toggle
        history_btn = QPushButton("History")
        history_btn.setObjectName("history-toggle")
        history_btn.setToolTip("Toggle command governance history")
        history_btn.clicked.connect(self._toggle_history_panel)
        layout.addWidget(history_btn)

        return bar

    # ----- Toggle -----

    def _toggle_governance(self) -> None:
        self._governance_on = not self._governance_on
        if self._governance_on:
            self._toggle_btn.setText("ON")
            self._toggle_btn.setObjectName("toggle-on")
            self._status_msg.setText("Governance active")
            # Re-apply accent border
            border = self.centralWidget()
            border.setStyleSheet(
                f"#orange-border {{ border: {BORDER_WIDTH}px solid {self._accent}; "
                f"background: {DARK_BG}; border-radius: 4px; }}"
            )
        else:
            self._toggle_btn.setText("OFF")
            self._toggle_btn.setObjectName("toggle-off")
            self._status_msg.setText("Governance off")
            # Gray border
            border = self.centralWidget()
            border.setStyleSheet(
                f"#orange-border {{ border: {BORDER_WIDTH}px solid #444444; "
                f"background: {DARK_BG}; border-radius: 4px; }}"
            )
        # Force style recalc
        self._toggle_btn.style().unpolish(self._toggle_btn)
        self._toggle_btn.style().polish(self._toggle_btn)

    # ----- Settings -----

    def _open_settings(self) -> None:
        profiles = self._list_profiles()
        dlg = SettingsDialog(self, profiles=profiles)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._gui_settings = dlg.get_settings()
            self._apply_settings()

    def _apply_settings(self) -> None:
        s = self._gui_settings

        # Theme color
        self._accent = s.get("theme_color", ORANGE)
        self.setStyleSheet(_build_stylesheet(self._accent))

        # Re-apply border based on governance state
        border = self.centralWidget()
        if self._governance_on:
            border.setStyleSheet(
                f"#orange-border {{ border: {BORDER_WIDTH}px solid {self._accent}; "
                f"background: {DARK_BG}; border-radius: 4px; }}"
            )
        # Update status label color
        self._status_msg.setStyleSheet(f"color: {self._accent};")

        # Font
        font_family = s.get("font_family", "Cascadia Mono")
        font_size = s.get("font_size", 11)
        self._apply_terminal_font(font_family, font_size)

        # Opacity
        opacity = s.get("opacity", 100)
        self.setWindowOpacity(opacity / 100.0)

    def _apply_terminal_font(self, family: str, size: int) -> None:
        from PySide6.QtGui import QFont, QFontMetrics

        font = QFont(family, size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._terminal._font = font
        fm = QFontMetrics(font)
        self._terminal._cell_w = fm.horizontalAdvance("M")
        self._terminal._cell_h = fm.height()
        self._terminal._ascent = fm.ascent()
        self._terminal.update()

    # ----- Command history panel -----

    def _build_history_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("history-panel")
        panel.setFixedHeight(120)

        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scrollable area for history entries
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: #111111; }")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._history_container = QWidget()
        self._history_layout = QVBoxLayout(self._history_container)
        self._history_layout.setContentsMargins(4, 2, 4, 2)
        self._history_layout.setSpacing(1)
        self._history_layout.addStretch()

        scroll.setWidget(self._history_container)
        outer.addWidget(scroll)
        self._history_scroll = scroll

        return panel

    def _toggle_history_panel(self) -> None:
        vis = not self._history_panel.isVisible()
        self._history_panel.setVisible(vis)
        if vis:
            # Scroll to bottom
            sb = self._history_scroll.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _add_history_entry(self, action: str, tier: str, command: str) -> None:
        """Add a governance decision to the history panel."""
        colors = {
            "deny": "#FF2244",
            "gate": "#FF6611",
            "allow": "#4e9a06",
            "warning": "#FF6611",
        }
        icons = {
            "deny": "X",
            "gate": "?",
            "allow": "+",
            "warning": "!",
        }
        color = colors.get(action, "#888")
        icon = icons.get(action, " ")
        ts = time.strftime("%H:%M:%S")

        label = QLabel(f"{ts} [{icon}] [{tier}] {command[:80]}")
        label.setStyleSheet(f"color: {color};")
        label.setWordWrap(False)

        # Insert before the stretch
        count = self._history_layout.count()
        self._history_layout.insertWidget(count - 1, label)

        # Keep max 100 entries
        while self._history_layout.count() > 101:
            item = self._history_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Auto-scroll if panel is visible
        if self._history_panel.isVisible():
            QTimer.singleShot(10, lambda: self._history_scroll.verticalScrollBar().setValue(
                self._history_scroll.verticalScrollBar().maximum()
            ))

        self._history_entries.append({
            "time": ts, "action": action, "tier": tier, "command": command,
        })

    # ----- Overwatch -----

    def _build_alert_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("alert-bar")
        bar.setFixedHeight(28)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)

        self._alert_icon = QLabel("")
        self._alert_text = QLabel("Overwatch active — watching...")
        self._alert_text.setStyleSheet(f"color: #00AAFF;")
        self._alert_dismiss = QPushButton("x")
        self._alert_dismiss.setFixedWidth(20)
        self._alert_dismiss.setStyleSheet("background: transparent; color: #666; border: none;")
        self._alert_dismiss.clicked.connect(lambda: self._alert_bar.setVisible(False))

        layout.addWidget(self._alert_icon)
        layout.addWidget(self._alert_text, 1)
        layout.addWidget(self._alert_dismiss)
        return bar

    def _toggle_overwatch(self) -> None:
        is_on = self._overwatch._running
        if is_on:
            # Turn off
            self._overwatch.enabled = False
            self._overwatch_btn.setObjectName("overwatch-off")
            self._overwatch_btn.setText("Overwatch")
            self._alert_bar.setVisible(False)
            self._status_msg.setText("Overwatch off")
            # Kill watcher subprocess if we launched one
            if hasattr(self, "_watcher_proc") and self._watcher_proc:
                try:
                    self._watcher_proc.terminate()
                except Exception:
                    pass
                self._watcher_proc = None
        else:
            # Set the chosen watcher agent
            agent = self._overwatch_agent_combo.currentText().lower()
            self._overwatch._config.watcher_agent = agent
            self._overwatch._config.enabled = True

            # Pull mission context
            mission_obj, mission_plan = self._get_mission_context()
            profile_name, profile_desc = self._get_profile_context()

            # Start with full context
            self._overwatch.start(
                mission_objective=mission_obj,
                mission_plan=mission_plan,
                profile_name=profile_name,
                profile_description=profile_desc,
            )

            self._overwatch_btn.setObjectName("overwatch-on")
            self._overwatch_btn.setText("Overwatch ON")
            self._alert_bar.setVisible(True)

            # Auto-launch watcher agent as subprocess
            self._auto_launch_watcher(agent)

            goal_preview = mission_obj[:60] if mission_obj else "no mission set"
            self._alert_text.setText(
                f"Feed active ({goal_preview}) — watcher launching..."
            )
            self._alert_text.setStyleSheet("color: #00AAFF;")
            self._alert_icon.setText("")
            self._status_msg.setText(f"Overwatch ({agent}) — launching watcher")

        # Force style recalc
        self._overwatch_btn.style().unpolish(self._overwatch_btn)
        self._overwatch_btn.style().polish(self._overwatch_btn)

    def _auto_launch_watcher(self, agent: str) -> None:
        """Spawn the watcher agent as a background subprocess."""
        import shutil
        import subprocess

        cmd = self._overwatch.get_launch_command(agent)

        # Check if the agent binary exists
        agent_bin = agent
        if agent == "claude":
            agent_bin = shutil.which("claude") or shutil.which("claudereal")
        elif agent == "codex":
            agent_bin = shutil.which("codex") or shutil.which("codexreal")
        elif agent == "gemini":
            agent_bin = shutil.which("gemini") or shutil.which("geminireal")

        if not agent_bin:
            # Agent not found — fall back to clipboard
            QApplication.clipboard().setText(cmd)
            self._alert_text.setText(
                f"{agent} not found — launch cmd copied to clipboard"
            )
            return

        try:
            # Launch in a new console window
            from openkeel.core.overwatch import OVERWATCH_DIR
            self._watcher_proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(OVERWATCH_DIR),
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
            self._status_msg.setText(
                f"Overwatch ({agent}) — watcher PID {self._watcher_proc.pid}"
            )
        except Exception as e:
            # Fall back to clipboard
            QApplication.clipboard().setText(cmd)
            self._alert_text.setText(f"Auto-launch failed — cmd copied to clipboard")
            self._watcher_proc = None

    def _get_mission_context(self) -> tuple[str, str]:
        """Pull the active mission's objective and formatted plan."""
        try:
            from openkeel.config import load_config
            from openkeel.keel.state import load_mission, get_missions_dir, get_active_mission_name
            cfg = load_config()
            name = get_active_mission_name(cfg)
            if not name:
                # Check toolbar selection
                sel = self._mission_combo.currentText()
                if sel and sel != "(none)":
                    name = sel
            if not name:
                return "", ""
            missions_dir = get_missions_dir(cfg)
            mission = load_mission(missions_dir, name)
            if not mission:
                return "", ""
            # Format plan
            plan_lines = []
            for step in mission.plan:
                markers = {"done": "[x]", "in_progress": "[>]", "skipped": "[-]"}
                m = markers.get(step.status, "[ ]")
                plan_lines.append(f"  {m} {step.id}. {step.step}")
            return mission.objective, "\n".join(plan_lines)
        except Exception:
            return "", ""

    def _get_profile_context(self) -> tuple[str, str]:
        """Pull the active profile's name and description."""
        try:
            import yaml
            name = self._profile_combo.currentText()
            if not name or name == "(none)":
                return "", ""
            # Find profile file
            from openkeel.gui.settings import SettingsDialog
            user_dir = Path.home() / ".openkeel" / "profiles"
            bundled = Path(__file__).resolve().parent.parent.parent / "profiles"
            for d in (user_dir, bundled):
                for ext in (".yaml", ".yml"):
                    p = d / f"{name}{ext}"
                    if p.exists():
                        with open(p, "r", encoding="utf-8") as f:
                            data = yaml.safe_load(f) or {}
                        return name, data.get("description", "")
            return name, ""
        except Exception:
            return "", ""

    def _on_overwatch_alert(self, alert: OverwatchAlert) -> None:
        """Called from Overwatch thread — use signal-safe approach."""
        # QTimer.singleShot is thread-safe for scheduling on the main thread
        QTimer.singleShot(0, lambda: self._show_alert(alert))

    def _show_alert(self, alert: OverwatchAlert) -> None:
        severity_colors = {
            "info": "#00AAFF",
            "warning": "#FF6611",
            "critical": "#FF2244",
        }
        severity_icons = {
            "info": "i",
            "warning": "!",
            "critical": "!!",
        }
        color = severity_colors.get(alert.severity, "#00AAFF")
        icon = severity_icons.get(alert.severity, "")
        self._alert_bar.setVisible(True)
        self._alert_icon.setText(icon)
        self._alert_icon.setStyleSheet(f"color: {color}; font-weight: bold;")
        self._alert_text.setText(f"[{alert.category}] {alert.message}")
        self._alert_text.setStyleSheet(f"color: {color};")

        # Seed warnings/criticals to Memoria for cross-session memory
        if alert.severity in ("warning", "critical"):
            self._memoria_seed_overwatch_alert(alert)

    # ----- Pickers -----

    @staticmethod
    def _list_profiles() -> list[str]:
        """Discover all available profiles (user + bundled)."""
        names: list[str] = []
        seen: set[str] = set()
        # User profiles
        user_dir = Path.home() / ".openkeel" / "profiles"
        if user_dir.is_dir():
            for f in sorted(user_dir.glob("*.yaml")):
                n = f.stem
                if n not in seen:
                    names.append(n)
                    seen.add(n)
        # Bundled profiles
        bundled = Path(__file__).resolve().parent.parent.parent / "profiles"
        if bundled.is_dir():
            for f in sorted(bundled.glob("*.yaml")):
                n = f.stem
                if n not in seen:
                    names.append(n)
                    seen.add(n)
        return names

    @staticmethod
    def _list_missions() -> list[str]:
        missions_dir = Path.home() / ".openkeel" / "missions"
        if not missions_dir.is_dir():
            return []
        return sorted(f.stem for f in missions_dir.glob("*.yaml"))

    @staticmethod
    def _detect_shells() -> list[tuple[str, str]]:
        """Return list of (display_name, command) for available shells."""
        import shutil

        shells: list[tuple[str, str]] = []
        if shutil.which("powershell.exe"):
            shells.append(("PowerShell", "powershell.exe"))
        if shutil.which("pwsh.exe") or shutil.which("pwsh"):
            shells.append(("PowerShell 7", "pwsh.exe"))
        if shutil.which("cmd.exe"):
            shells.append(("CMD", "cmd.exe"))
        if shutil.which("bash"):
            shells.append(("Bash", "bash"))
        if shutil.which("wsl"):
            shells.append(("WSL", "wsl.exe"))
        if not shells:
            shells.append(("PowerShell", "powershell.exe"))
        return shells

    def _on_profile_changed(self, text: str) -> None:
        profile = "" if text == "(none)" else text
        self._profile = profile
        self._cached_profile_obj = None  # invalidate cache
        try:
            from openkeel.config import load_config, save_config
            cfg = load_config()
            cfg.setdefault("profiles", {})["active"] = profile
            save_config(cfg)
        except Exception:
            pass
        self._status_msg.setText(f"Profile: {profile or 'none'}")

    def _on_mission_changed(self, text: str) -> None:
        mission = "" if text == "(none)" else text
        self._mission = mission
        try:
            from openkeel.config import load_config, save_config
            cfg = load_config()
            cfg.setdefault("keel", {})["active_mission"] = mission
            save_config(cfg)
        except Exception:
            pass
        self._status_msg.setText(f"Mission: {mission or 'none'}")

    def _on_shell_changed(self, index: int) -> None:
        if 0 <= index < len(self._available_shells):
            display, cmd = self._available_shells[index]
            self._shell = cmd
            self._status_msg.setText(f"Shell changed to {display} (restart to apply)")

    def _launch_claude(self) -> None:
        """Send claude launch command to the running terminal.

        Also injects context into CLAUDE.md (memory facts + profile info).
        """
        if not (self._terminal._pty and self._terminal._pty.isalive()):
            return

        # Inject context into CLAUDE.md in the current working directory
        try:
            from openkeel.launch import inject_context
            profile_name = self._profile_combo.currentText()
            if not profile_name or profile_name == "(none)":
                profile_name = "openkeel"

            # Build facts list from Memoria context if available
            facts = []
            if self._memoria_context:
                for line in self._memoria_context.split("\n"):
                    line = line.strip().lstrip("- ")
                    if line:
                        facts.append({"text": line})

            inject_context(".", profile_name, facts)
        except Exception:
            pass

        self._terminal._pty.write(
            "claude --dangerously-skip-permissions\r"
        )
        self._status_msg.setText("Launching Claude Code...")
        self._terminal.setFocus()

    def _on_mode_changed(self, text: str) -> None:
        self._active_mode = text
        from openkeel.core.modes import set_active_mode
        set_active_mode(text.lower())
        self._status_msg.setText(f"Mode: {text}")

        # Stop any existing mode polling
        if hasattr(self, "_mode_timer") and self._mode_timer.isActive():
            self._mode_timer.stop()

        mode_lower = text.lower()
        if mode_lower == "babysit":
            self._start_babysit_prompt()
        elif mode_lower == "stakeout":
            self._start_stakeout_prompt()

    # ----- Babysit / Stakeout polling -----

    def _start_babysit_prompt(self) -> None:
        """Prompt user for babysit target, then start polling."""
        from PySide6.QtWidgets import QInputDialog
        target, ok = QInputDialog.getText(
            self, "Babysit Mode",
            "Enter a process name, PID, or log file path to watch:",
        )
        if not ok or not target.strip():
            self._status_msg.setText("Babysit: no target set")
            return

        from openkeel.core.modes import BabysitConfig, save_babysit_config
        self._babysit_cfg = BabysitConfig(target=target.strip())
        save_babysit_config(self._babysit_cfg)

        if not hasattr(self, "_mode_timer"):
            self._mode_timer = QTimer(self)
            self._mode_timer.timeout.connect(self._mode_poll_tick)
        self._mode_timer.start(self._babysit_cfg.check_interval_seconds * 1000)
        self._status_msg.setText(
            f"Babysit: watching '{target.strip()}' every "
            f"{self._babysit_cfg.check_interval_seconds}s"
        )

    def _start_stakeout_prompt(self) -> None:
        """Prompt user for stakeout targets and patterns, then start polling."""
        from PySide6.QtWidgets import QInputDialog
        targets, ok = QInputDialog.getText(
            self, "Stakeout Mode",
            "Log file paths to watch (comma-separated):",
        )
        if not ok or not targets.strip():
            self._status_msg.setText("Stakeout: no targets set")
            return

        patterns, ok2 = QInputDialog.getText(
            self, "Stakeout Mode",
            "Regex patterns to alert on (comma-separated):",
            text=r"error|exception|fatal|denied|failed",
        )
        if not ok2 or not patterns.strip():
            self._status_msg.setText("Stakeout: no patterns set")
            return

        from openkeel.core.modes import StakeoutConfig, save_stakeout_config
        target_list = [t.strip() for t in targets.split(",") if t.strip()]
        pattern_list = [p.strip() for p in patterns.split(",") if p.strip()]
        self._stakeout_cfg = StakeoutConfig(
            targets=target_list, patterns=pattern_list,
        )
        save_stakeout_config(self._stakeout_cfg)

        if not hasattr(self, "_mode_timer"):
            self._mode_timer = QTimer(self)
            self._mode_timer.timeout.connect(self._mode_poll_tick)
        self._mode_timer.start(self._stakeout_cfg.check_interval_seconds * 1000)
        self._status_msg.setText(
            f"Stakeout: watching {len(target_list)} targets, "
            f"{len(pattern_list)} patterns"
        )

    def _mode_poll_tick(self) -> None:
        """Run one babysit or stakeout check."""
        mode = self._active_mode.lower()
        matches = []

        if mode == "babysit" and hasattr(self, "_babysit_cfg"):
            from openkeel.core.modes import babysit_check
            matches = babysit_check(self._babysit_cfg)
        elif mode == "stakeout" and hasattr(self, "_stakeout_cfg"):
            from openkeel.core.modes import stakeout_check
            matches = stakeout_check(self._stakeout_cfg)

        if matches:
            # Show in alert bar
            summary = f"{len(matches)} issues found"
            self._alert_bar.setVisible(True)
            self._alert_icon.setText("!")
            self._alert_icon.setStyleSheet("color: #FF6611; font-weight: bold;")
            self._alert_text.setText(f"[{mode}] {summary}: {matches[0][:80]}")
            self._alert_text.setStyleSheet("color: #FF6611;")

            # Log to audit
            log_path = Path.home() / ".openkeel" / "enforcement.log"
            from openkeel.core.audit import log_event
            for match in matches[:5]:
                log_event(log_path, f"{mode}_alert", {
                    "match": match,
                    "mode": mode,
                }, session_id=self._session_id)

            # Add to history
            for match in matches[:5]:
                self._add_history_entry("warning", mode, match)

    # ----- OpenKeel state -----

    def _load_mission(self) -> str:
        try:
            from openkeel.config import load_config
            cfg = load_config()
            return cfg.get("keel", {}).get("active_mission", "") or ""
        except Exception:
            return ""

    def _load_profile(self) -> str:
        try:
            from openkeel.config import load_config
            cfg = load_config()
            return cfg.get("profiles", {}).get("active", "") or ""
        except Exception:
            return ""

    def _refresh_status(self) -> None:
        # Counters are updated live by _governance_check, just refresh labels
        self._blocked_label.setText(f"Blocked: {self._count_blocked}")
        self._allowed_label.setText(f"Allowed: {self._count_allowed}")
        self._gated_label.setText(f"Gated: {self._count_gated}")

        # Update Overwatch watcher status in alert bar
        if self._overwatch._running:
            status = self._overwatch.watcher_status
            status_display = {
                "alive": ("Watcher active", "#00FF9C"),
                "waiting": ("Waiting for watcher to connect...", "#FF6611"),
                "dead": ("Watcher not responding!", "#FF2244"),
            }
            text, color = status_display.get(status, ("Unknown", "#888"))
            self._overwatch_btn.setText(f"Overwatch: {status}")
            # Only update alert bar if no real alert is showing
            if self._alert_icon.text() == "":
                self._alert_text.setText(text)
                self._alert_text.setStyleSheet(f"color: {color};")

    # ----- Memoria integration -----

    def _get_learning_config(self):
        """Load the LearningConfig from the active profile, or use GUI settings."""
        from openkeel.core.profile import LearningConfig
        try:
            profile_name = self._profile_combo.currentText()
            if profile_name and profile_name != "(none)":
                from openkeel.core.profile import load_profile
                profile = load_profile(profile_name)
                return profile.learning, profile.name
        except Exception:
            pass
        # Fallback to GUI settings
        endpoint = self._gui_settings.get("memoria_endpoint", "http://127.0.0.1:8000")
        enabled = self._gui_settings.get("memoria_enabled", True)
        return LearningConfig(enabled=enabled, endpoint=endpoint), ""

    def _memoria_session_start(self) -> None:
        """Search Memoria at session start for context relevant to the current mission/profile."""
        import threading

        def _search():
            try:
                learning_cfg, profile_name = self._get_learning_config()
                if not learning_cfg.enabled:
                    return

                from openkeel.integrations.memory import MemoryClient
                client = MemoryClient(
                    endpoint=learning_cfg.endpoint,
                    timeout=learning_cfg.timeout,
                )
                if not client.is_available():
                    QTimer.singleShot(0, lambda: self._status_msg.setText("Memoria offline"))
                    return

                # Build search queries from mission + profile
                queries = []
                mission_name = self._mission_combo.currentText()
                if mission_name and mission_name != "(none)":
                    queries.append(mission_name)
                if profile_name:
                    queries.append(profile_name)
                # Also search by mission objective
                obj, _ = self._get_mission_context()
                if obj:
                    queries.append(obj[:100])

                if not queries:
                    queries = ["openkeel session context"]

                results = client.search_multi(queries, top_k=learning_cfg.search_top_k)
                if results:
                    facts = [r.get("text", "") for r in results if r.get("text")]
                    self._memoria_context = "\n".join(f"- {f}" for f in facts[:10])
                    count = len(facts)
                    QTimer.singleShot(0, lambda: self._status_msg.setText(
                        f"Memoria: {count} relevant facts loaded"
                    ))
                else:
                    QTimer.singleShot(0, lambda: self._status_msg.setText("Memoria: connected, no prior context"))
            except Exception:
                pass

        threading.Thread(target=_search, daemon=True).start()

    def _memoria_session_end(self) -> None:
        """Extract lessons from this session and seed to Memoria."""
        try:
            learning_cfg, profile_name = self._get_learning_config()
            if not learning_cfg.enabled or not learning_cfg.auto_seed:
                return

            from openkeel.core.learning import run_post_session_learning
            log_path = Path.home() / ".openkeel" / "enforcement.log"
            if not log_path.exists():
                return

            stored = run_post_session_learning(
                log_path=log_path,
                config=learning_cfg,
                profile_name=profile_name,
                session_id=str(int(time.time())),
            )
            if stored > 0:
                self._log_overwatch(f"Session end: seeded {stored} lessons to Memoria")
        except Exception:
            pass

    def _memoria_seed_overwatch_alert(self, alert) -> None:
        """Seed an Overwatch alert to Memoria for cross-session learning."""
        import threading

        def _seed():
            try:
                learning_cfg, profile_name = self._get_learning_config()
                if not learning_cfg.enabled:
                    return
                from openkeel.integrations.memory import MemoryClient
                client = MemoryClient(
                    endpoint=learning_cfg.endpoint,
                    timeout=learning_cfg.timeout,
                )
                if not client.is_available():
                    return

                mission_name = self._mission_combo.currentText()
                fact = (
                    f"[OVERWATCH:{alert.category.upper()}] {alert.message} "
                    f"(mission: {mission_name}, profile: {profile_name})"
                )
                client.memorize(fact, metadata={
                    "source": "openkeel_overwatch",
                    "category": alert.category,
                    "severity": alert.severity,
                    "profile": profile_name,
                    "mission": mission_name,
                })
            except Exception:
                pass

        threading.Thread(target=_seed, daemon=True).start()

    def _log_overwatch(self, msg: str) -> None:
        """Write to overwatch log."""
        try:
            from openkeel.core.overwatch import OVERWATCH_LOG
            import time as _time
            ts = _time.strftime("%Y-%m-%d %H:%M:%S")
            with open(OVERWATCH_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    # ----- Governance -----

    def _load_active_profile(self):
        """Load the active Profile object (cached), or None if not set."""
        if self._cached_profile_obj is not None:
            return self._cached_profile_obj
        try:
            name = self._profile_combo.currentText()
            if not name or name == "(none)":
                return None
            from openkeel.core.profile import load_profile
            self._cached_profile_obj = load_profile(name)
            return self._cached_profile_obj
        except Exception:
            return None

    def _governance_check(self, command: str) -> str:
        """Classify a command and return 'allow', 'deny', or 'gate'.

        Called from terminal.py's keyPressEvent on Enter.
        """
        if not self._governance_on:
            return "allow"

        profile = self._load_active_profile()
        if not profile:
            return "allow"

        # Classify using the profile's tiers
        from openkeel.core.classifier import classify
        result = classify(command, profile)

        # Apply mode override
        from openkeel.core.modes import apply_mode_override
        mode = self._active_mode.lower()
        final_action, reason = apply_mode_override(
            mode, command, result.action, result.tier,
        )

        # Log to audit trail
        log_path = Path.home() / ".openkeel" / "enforcement.log"
        from openkeel.core.audit import log_event
        log_event(log_path, "classify", {
            "command": command,
            "action": final_action,
            "tier": result.tier,
            "rule_id": result.rule_id,
            "activity": result.activity,
            "message": result.message or reason,
            "mode": mode,
            "profile": profile.name,
        }, session_id=self._session_id)

        # Update counters + history panel
        if final_action == "deny":
            self._count_blocked += 1
            self._blocked_label.setText(f"Blocked: {self._count_blocked}")
            msg = result.message or reason or "Command blocked by governance"
            self._terminal._pty.write(f"\r\n\x1b[31m[BLOCKED] {msg}\x1b[0m\r\n")
            self._status_msg.setText(f"BLOCKED: {command[:50]}")
            self._add_history_entry("deny", result.tier or mode, command)
        elif final_action == "gate":
            self._count_gated += 1
            self._gated_label.setText(f"Gated: {self._count_gated}")
            self._add_history_entry("gate", result.tier or "gated", command)
            approved = self._show_gate_dialog(command, result.message or reason)
            if approved:
                self._count_allowed += 1
                self._allowed_label.setText(f"Allowed: {self._count_allowed}")
                log_event(log_path, "gate_approved", {
                    "command": command,
                    "profile": profile.name,
                }, session_id=self._session_id)
                self._terminal._pty.write("\r")
                return "allow"
            else:
                self._count_blocked += 1
                self._blocked_label.setText(f"Blocked: {self._count_blocked}")
                self._terminal._pty.write("\x03")
                self._terminal._pty.write(f"\r\n\x1b[33m[GATED] Command denied by user\x1b[0m\r\n")
                log_event(log_path, "gate_denied", {
                    "command": command,
                    "profile": profile.name,
                }, session_id=self._session_id)
                return "deny"
        else:
            self._count_allowed += 1
            self._allowed_label.setText(f"Allowed: {self._count_allowed}")
            self._add_history_entry("allow", result.tier or "default", command)

        return final_action

    def _show_gate_dialog(self, command: str, reason: str) -> bool:
        """Show a modal approval dialog for gated commands. Returns True if approved."""
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Gated Command — Approval Required")
        msg.setText(f"The following command requires approval:\n\n{command}")
        msg.setInformativeText(reason or "This command matched a GATED rule in your profile.")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        msg.setStyleSheet(f"""
            QMessageBox {{
                background: {DARK_BG};
                color: {TEXT_LIGHT};
            }}
            QMessageBox QLabel {{
                color: {TEXT_LIGHT};
                font-size: 12px;
            }}
            QPushButton {{
                background: #2a2a2a;
                color: {TEXT_LIGHT};
                border: 1px solid #444;
                border-radius: 3px;
                padding: 6px 16px;
                min-width: 60px;
            }}
            QPushButton:hover {{
                border-color: {self._accent};
            }}
        """)
        return msg.exec() == QMessageBox.StandardButton.Yes

    # ----- Events -----

    def _on_shell_exit(self) -> None:
        self._status_msg.setText("Shell exited")
        # Session end: extract and seed lessons
        self._memoria_session_end()

    def closeEvent(self, event) -> None:
        self._status_timer.stop()
        if hasattr(self, "_mode_timer"):
            self._mode_timer.stop()
        self._overwatch.stop()
        # Kill watcher subprocess
        if hasattr(self, "_watcher_proc") and self._watcher_proc:
            try:
                self._watcher_proc.terminate()
            except Exception:
                pass
        # Session end learning
        self._memoria_session_end()
        # Clean up injected CLAUDE.md context
        try:
            from openkeel.launch import remove_context
            remove_context(".")
        except Exception:
            pass
        self._terminal.cleanup()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="OpenKeel Terminal")
    parser.add_argument("--profile", "-p", help="Load this profile on startup")
    parser.add_argument("--mode", "-m", help="Set operational mode (normal/lockdown/audit/etc)")
    parser.add_argument("--mission", help="Set active mission")
    parser.add_argument("--claude", action="store_true", help="Auto-launch Claude Code with --dangerously-skip-permissions")
    parser.add_argument("--overwatch", action="store_true", help="Enable Overwatch agent monitoring on startup")
    args, qt_args = parser.parse_known_args()

    app = QApplication(qt_args)

    # Dark palette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(DARK_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_LIGHT))
    palette.setColor(QPalette.ColorRole.Base, QColor(DARK_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT_LIGHT))
    app.setPalette(palette)

    window = OpenKeelWindow()

    # Apply CLI overrides
    if args.profile:
        idx = window._profile_combo.findText(args.profile)
        if idx >= 0:
            window._profile_combo.setCurrentIndex(idx)
        window.setWindowTitle(f"OpenKeel Terminal — {args.profile}")

    if args.mode:
        idx = window._mode_combo.findText(args.mode.capitalize())
        if idx >= 0:
            window._mode_combo.setCurrentIndex(idx)

    if args.mission:
        idx = window._mission_combo.findText(args.mission)
        if idx >= 0:
            window._mission_combo.setCurrentIndex(idx)

    window.show()

    # Auto-enable Overwatch if requested
    if args.overwatch:
        QTimer.singleShot(500, window._toggle_overwatch)

    # Auto-launch Claude if requested
    if args.claude:
        QTimer.singleShot(1500, window._launch_claude)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
