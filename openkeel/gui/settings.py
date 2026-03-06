"""OpenKeel Terminal — Settings dialog."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Theme constants (mirrors app.py)
# ---------------------------------------------------------------------------

DARK_BG = "#0d0d0d"
TOOLBAR_BG = "#1a1a1a"
TEXT_DIM = "#888888"
TEXT_LIGHT = "#cccccc"

SETTINGS_PATH = Path.home() / ".openkeel" / "gui_settings.json"

THEME_COLORS = {
    "Neon Orange": "#FF6611",
    "Neon Green": "#00FF9C",
    "Neon Blue": "#00AAFF",
    "Neon Red": "#FF2244",
    "Neon Purple": "#AA44FF",
    "Neon Pink": "#FF44AA",
}

FONT_FAMILIES = ["Cascadia Mono", "Consolas", "Courier New", "JetBrains Mono"]

GOVERNANCE_MODES = [
    "Normal", "Babysit", "Stakeout", "Lockdown", "Audit", "Pair", "Training",
]

SHELL_OPTIONS = ["PowerShell", "PowerShell 7", "CMD", "Bash", "WSL"]

DEFAULTS = {
    "theme_color_name": "Neon Orange",
    "theme_color": "#FF6611",
    "font_family": "Cascadia Mono",
    "font_size": 11,
    "opacity": 100,
    "default_profile": "(none)",
    "default_mode": "Normal",
    "memoria_endpoint": "http://127.0.0.1:8000",
    "memoria_enabled": True,
    "default_shell": "PowerShell",
    "startup_command": "",
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_settings() -> dict:
    """Load settings from disk, falling back to defaults."""
    settings = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            settings.update(saved)
        except Exception:
            pass
    return settings


def save_settings(settings: dict) -> None:
    """Persist settings to disk."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


def _make_stylesheet(accent: str) -> str:
    return f"""
QDialog {{ background: {DARK_BG}; color: {TEXT_LIGHT}; }}
QTabWidget::pane {{ border: 1px solid #333; background: {TOOLBAR_BG}; }}
QTabBar::tab {{ background: {TOOLBAR_BG}; color: {TEXT_DIM}; padding: 8px 16px; }}
QTabBar::tab:selected {{ background: #2a2a2a; color: {accent}; border-bottom: 2px solid {accent}; }}
QComboBox, QSpinBox, QLineEdit {{ background: #2a2a2a; color: #ccc; border: 1px solid #444; border-radius: 3px; padding: 4px; }}
QCheckBox {{ color: #ccc; }}
QSlider::groove:horizontal {{ background: #333; height: 6px; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {accent}; width: 16px; margin: -5px 0; border-radius: 8px; }}
QPushButton {{ background: #2a2a2a; color: #ccc; border: 1px solid #444; border-radius: 3px; padding: 6px 16px; }}
QPushButton:hover {{ border-color: {accent}; }}
QLabel {{ color: {TEXT_LIGHT}; }}
"""


class SettingsDialog(QDialog):
    """Multi-tab settings dialog for OpenKeel Terminal."""

    def __init__(self, parent=None, profiles: list[str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(480, 400)

        self._settings = load_settings()
        self._profiles = profiles or []

        accent = self._settings.get("theme_color", "#FF6611")
        self.setStyleSheet(_make_stylesheet(accent))

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_governance_tab(), "Governance")
        tabs.addTab(self._build_shell_tab(), "Shell")
        tabs.addTab(self._build_rules_tab(), "Rules")
        layout.addWidget(tabs)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    # ----- Tab builders -----

    def _build_appearance_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        # Theme color
        self._color_combo = QComboBox()
        for name in THEME_COLORS:
            self._color_combo.addItem(name)
        current_name = self._settings.get("theme_color_name", "Neon Orange")
        idx = self._color_combo.findText(current_name)
        if idx >= 0:
            self._color_combo.setCurrentIndex(idx)
        form.addRow("Theme color:", self._color_combo)

        # Font family
        self._font_combo = QComboBox()
        for fam in FONT_FAMILIES:
            self._font_combo.addItem(fam)
        current_font = self._settings.get("font_family", "Cascadia Mono")
        idx = self._font_combo.findText(current_font)
        if idx >= 0:
            self._font_combo.setCurrentIndex(idx)
        form.addRow("Font family:", self._font_combo)

        # Font size
        self._font_size = QSpinBox()
        self._font_size.setRange(8, 24)
        self._font_size.setValue(self._settings.get("font_size", 11))
        form.addRow("Font size:", self._font_size)

        # Opacity
        opacity_row = QHBoxLayout()
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(50, 100)
        self._opacity_slider.setValue(self._settings.get("opacity", 100))
        self._opacity_label = QLabel(f"{self._opacity_slider.value()}%")
        self._opacity_slider.valueChanged.connect(
            lambda v: self._opacity_label.setText(f"{v}%")
        )
        opacity_row.addWidget(self._opacity_slider)
        opacity_row.addWidget(self._opacity_label)
        form.addRow("Opacity:", opacity_row)

        return w

    def _build_governance_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        # Default profile
        self._profile_combo = QComboBox()
        self._profile_combo.addItem("(none)")
        for p in self._profiles:
            self._profile_combo.addItem(p)
        current = self._settings.get("default_profile", "(none)")
        idx = self._profile_combo.findText(current)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        form.addRow("Default profile:", self._profile_combo)

        # Default mode
        self._mode_combo = QComboBox()
        for m in GOVERNANCE_MODES:
            self._mode_combo.addItem(m)
        current_mode = self._settings.get("default_mode", "Normal")
        idx = self._mode_combo.findText(current_mode)
        if idx >= 0:
            self._mode_combo.setCurrentIndex(idx)
        form.addRow("Default mode:", self._mode_combo)

        # Memoria endpoint
        self._memoria_endpoint = QLineEdit()
        self._memoria_endpoint.setText(
            self._settings.get("memoria_endpoint", "http://127.0.0.1:8000")
        )
        form.addRow("Memoria endpoint:", self._memoria_endpoint)

        # Memoria enabled
        self._memoria_enabled = QCheckBox("Enabled")
        self._memoria_enabled.setChecked(
            self._settings.get("memoria_enabled", True)
        )
        form.addRow("Memoria:", self._memoria_enabled)

        # Test connection
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(self._test_memoria)
        self._test_result = QLabel("")
        test_row = QHBoxLayout()
        test_row.addWidget(self._test_btn)
        test_row.addWidget(self._test_result)
        test_row.addStretch()
        form.addRow("", test_row)

        return w

    def _build_shell_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        # Default shell
        self._shell_combo = QComboBox()
        for s in SHELL_OPTIONS:
            self._shell_combo.addItem(s)
        current_shell = self._settings.get("default_shell", "PowerShell")
        idx = self._shell_combo.findText(current_shell)
        if idx >= 0:
            self._shell_combo.setCurrentIndex(idx)
        form.addRow("Default shell:", self._shell_combo)

        # Startup command
        self._startup_cmd = QLineEdit()
        self._startup_cmd.setPlaceholderText("Optional command to run on shell start")
        self._startup_cmd.setText(self._settings.get("startup_command", ""))
        form.addRow("Startup command:", self._startup_cmd)

        return w

    # ----- Rules tab -----

    def _find_profile_paths(self) -> dict[str, Path]:
        """Discover profile YAML files from user + bundled directories."""
        profiles: dict[str, Path] = {}
        # User profiles
        user_dir = Path.home() / ".openkeel" / "profiles"
        if user_dir.is_dir():
            for p in sorted(user_dir.glob("*.yaml")):
                profiles[p.stem] = p
            for p in sorted(user_dir.glob("*.yml")):
                profiles[p.stem] = p
        # Bundled profiles (sibling to openkeel package)
        bundled = Path(__file__).resolve().parent.parent.parent / "profiles"
        if bundled.is_dir():
            for p in sorted(bundled.glob("*.yaml")):
                if p.stem not in profiles:
                    profiles[p.stem] = p
            for p in sorted(bundled.glob("*.yml")):
                if p.stem not in profiles:
                    profiles[p.stem] = p
        return profiles

    def _build_rules_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Profile selector
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Profile:"))
        self._rules_profile_combo = QComboBox()
        self._profile_paths = self._find_profile_paths()
        for name in sorted(self._profile_paths):
            self._rules_profile_combo.addItem(name)
        self._rules_profile_combo.currentTextChanged.connect(self._load_rules_for_profile)
        sel_row.addWidget(self._rules_profile_combo, 1)
        layout.addLayout(sel_row)

        # Table: Tier | Pattern | Enabled
        self._rules_table = QTableWidget(0, 3)
        self._rules_table.setHorizontalHeaderLabels(["Tier", "Pattern", "Enabled"])
        header = self._rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._rules_table.setStyleSheet(
            "QTableWidget { background: #1a1a1a; color: #ccc; gridline-color: #333; }"
            "QHeaderView::section { background: #222; color: #aaa; border: 1px solid #333; padding: 4px; }"
        )
        layout.addWidget(self._rules_table, 1)

        # Buttons: Add / Remove / Move tier
        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Rule")
        add_btn.clicked.connect(self._add_rule_row)
        remove_btn = QPushButton("- Remove")
        remove_btn.clicked.connect(self._remove_rule_row)
        move_blocked = QPushButton("→ Blocked")
        move_blocked.clicked.connect(lambda: self._set_row_tier("blocked"))
        move_gated = QPushButton("→ Gated")
        move_gated.clicked.connect(lambda: self._set_row_tier("gated"))
        move_safe = QPushButton("→ Safe")
        move_safe.clicked.connect(lambda: self._set_row_tier("safe"))
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(move_blocked)
        btn_row.addWidget(move_gated)
        btn_row.addWidget(move_safe)
        layout.addLayout(btn_row)

        # Save rules button
        save_rules_btn = QPushButton("Save Rules to Profile")
        save_rules_btn.clicked.connect(self._save_rules_to_profile)
        layout.addWidget(save_rules_btn)

        # Load initial profile
        if self._rules_profile_combo.count() > 0:
            self._load_rules_for_profile(self._rules_profile_combo.currentText())

        return w

    def _load_rules_for_profile(self, profile_name: str) -> None:
        """Populate the rules table from a profile YAML."""
        self._rules_table.setRowCount(0)
        path = self._profile_paths.get(profile_name)
        if not path or not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return
        self._rules_yaml_data = data
        self._rules_yaml_path = path

        tier_colors = {
            "blocked": "#FF2244",
            "gated": "#FF6611",
            "safe": "#00FF9C",
        }

        for tier in ("blocked", "gated", "safe"):
            section = data.get(tier, {})
            patterns = section.get("patterns", []) if isinstance(section, dict) else []
            for pattern in patterns:
                row = self._rules_table.rowCount()
                self._rules_table.insertRow(row)
                # Tier cell
                tier_item = QTableWidgetItem(tier)
                tier_item.setForeground(QColor(tier_colors.get(tier, "#ccc")))
                tier_item.setFlags(tier_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._rules_table.setItem(row, 0, tier_item)
                # Pattern cell (editable)
                self._rules_table.setItem(row, 1, QTableWidgetItem(pattern))
                # Enabled checkbox
                chk = QCheckBox()
                chk.setChecked(True)
                chk_widget = QWidget()
                chk_layout = QHBoxLayout(chk_widget)
                chk_layout.addWidget(chk)
                chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                chk_layout.setContentsMargins(0, 0, 0, 0)
                self._rules_table.setCellWidget(row, 2, chk_widget)

    def _add_rule_row(self) -> None:
        """Add a new empty rule row defaulting to 'gated'."""
        row = self._rules_table.rowCount()
        self._rules_table.insertRow(row)
        tier_item = QTableWidgetItem("gated")
        tier_item.setForeground(QColor("#FF6611"))
        tier_item.setFlags(tier_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._rules_table.setItem(row, 0, tier_item)
        self._rules_table.setItem(row, 1, QTableWidgetItem(""))
        chk = QCheckBox()
        chk.setChecked(True)
        chk_widget = QWidget()
        chk_layout = QHBoxLayout(chk_widget)
        chk_layout.addWidget(chk)
        chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chk_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_table.setCellWidget(row, 2, chk_widget)
        self._rules_table.editItem(self._rules_table.item(row, 1))

    def _remove_rule_row(self) -> None:
        """Remove the currently selected row."""
        row = self._rules_table.currentRow()
        if row >= 0:
            self._rules_table.removeRow(row)

    def _set_row_tier(self, tier: str) -> None:
        """Change the tier of the selected row."""
        row = self._rules_table.currentRow()
        if row < 0:
            return
        tier_colors = {"blocked": "#FF2244", "gated": "#FF6611", "safe": "#00FF9C"}
        item = self._rules_table.item(row, 0)
        if item:
            item.setText(tier)
            item.setForeground(QColor(tier_colors.get(tier, "#ccc")))

    def _save_rules_to_profile(self) -> None:
        """Write current rules table back to the profile YAML."""
        if not hasattr(self, "_rules_yaml_path"):
            QMessageBox.warning(self, "No Profile", "No profile loaded to save to.")
            return

        # Collect enabled rules by tier
        tiers: dict[str, list[str]] = {"blocked": [], "gated": [], "safe": []}
        for row in range(self._rules_table.rowCount()):
            tier_item = self._rules_table.item(row, 0)
            pattern_item = self._rules_table.item(row, 1)
            chk_widget = self._rules_table.cellWidget(row, 2)
            if not tier_item or not pattern_item:
                continue
            # Check if enabled
            chk = chk_widget.findChild(QCheckBox) if chk_widget else None
            if chk and not chk.isChecked():
                continue
            tier = tier_item.text()
            pattern = pattern_item.text().strip()
            if pattern and tier in tiers:
                tiers[tier].append(pattern)

        # Update YAML data
        data = self._rules_yaml_data
        for tier in ("blocked", "gated", "safe"):
            if tier not in data:
                data[tier] = {}
            data[tier]["patterns"] = tiers[tier]

        # Write back
        try:
            with open(self._rules_yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            QMessageBox.information(self, "Saved", f"Rules saved to {self._rules_yaml_path.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to save: {exc}")

    # ----- Actions -----

    def _test_memoria(self) -> None:
        endpoint = self._memoria_endpoint.text().strip().rstrip("/")
        url = f"{endpoint}/health"
        self._test_result.setText("Testing...")
        try:
            import urllib.request
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")[:200]
            self._test_result.setStyleSheet("color: #00FF9C;")
            self._test_result.setText(f"OK ({status}): {body}")
        except Exception as exc:
            self._test_result.setStyleSheet("color: #FF2244;")
            self._test_result.setText(f"Failed: {exc}")

    def _on_save(self) -> None:
        color_name = self._color_combo.currentText()
        self._settings.update({
            "theme_color_name": color_name,
            "theme_color": THEME_COLORS.get(color_name, "#FF6611"),
            "font_family": self._font_combo.currentText(),
            "font_size": self._font_size.value(),
            "opacity": self._opacity_slider.value(),
            "default_profile": self._profile_combo.currentText(),
            "default_mode": self._mode_combo.currentText(),
            "memoria_endpoint": self._memoria_endpoint.text().strip(),
            "memoria_enabled": self._memoria_enabled.isChecked(),
            "default_shell": self._shell_combo.currentText(),
            "startup_command": self._startup_cmd.text(),
        })
        save_settings(self._settings)
        self.accept()

    def get_settings(self) -> dict:
        """Return the current settings dict (valid after accept)."""
        return dict(self._settings)
