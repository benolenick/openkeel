"""Settings dialog for OpenKeel 2.0 — Appearance, Models, Memory, Quota."""

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QComboBox, QSlider, QSpinBox, QPushButton, QLineEdit,
    QCheckBox, QProgressBar, QFormLayout, QGroupBox, QColorDialog,
)

from .theme import THEME_COLORS, FONT_FAMILIES, build_stylesheet

SETTINGS_FILE = Path.home() / ".openkeel2" / "settings.json"

DEFAULTS = {
    "theme_color_name": "Neon Orange",
    "theme_color": "#FF6611",
    "custom_color": None,
    "font_family": "Cascadia Mono",
    "font_size": 11,
    "opacity": 100,
    "cli_model": "sonnet",        # opus / sonnet / haiku
    "runner": "haiku_api",        # haiku_api / local / none
    "local_model": "gemma4:e2b",
    "routing": "flat",            # vanilla / flat / hierarchy / haiku_vanilla / haiku_local
    "hyphae_url": "http://127.0.0.1:8100",
    "hyphae_enabled": True,
    "weekly_limit": 5000000,
}


def load_settings() -> dict:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            merged = {**DEFAULTS, **saved}
            return merged
        except Exception:
            pass
    return dict(DEFAULTS)


def save_settings(s: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OpenKeel Settings")
        self.setMinimumSize(480, 420)
        self._settings = load_settings()
        self._build_ui()
        self.setStyleSheet(build_stylesheet(self._settings["theme_color"]))

    def _build_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_models_tab(), "Models")
        tabs.addTab(self._build_memory_tab(), "Memory")
        tabs.addTab(self._build_quota_tab(), "Quota")

        layout.addWidget(tabs)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._save)
        save.setStyleSheet(f"background-color: {self._settings['theme_color']}; color: white; padding: 6px 20px; border-radius: 4px; font-weight: bold;")
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        layout.addLayout(btn_row)

    # ── Appearance ────────────────────────────────────────────

    def _build_appearance_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        # Color theme
        self._color_combo = QComboBox()
        color_names = list(THEME_COLORS.keys()) + ["Custom..."]
        self._color_combo.addItems(color_names)
        current = self._settings.get("theme_color_name", "Neon Orange")
        if current in color_names:
            self._color_combo.setCurrentText(current)
        self._color_combo.currentTextChanged.connect(self._on_color_changed)
        form.addRow("Theme Color:", self._color_combo)

        # Color preview
        self._color_preview = QLabel("  ")
        self._color_preview.setFixedSize(60, 24)
        self._update_color_preview()
        form.addRow("Preview:", self._color_preview)

        # Font
        self._font_combo = QComboBox()
        self._font_combo.addItems(FONT_FAMILIES)
        self._font_combo.setCurrentText(self._settings.get("font_family", "Cascadia Mono"))
        form.addRow("Font:", self._font_combo)

        # Font size
        self._font_size = QSpinBox()
        self._font_size.setRange(8, 24)
        self._font_size.setValue(self._settings.get("font_size", 11))
        form.addRow("Font Size:", self._font_size)

        # Opacity
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(30, 100)
        self._opacity.setValue(self._settings.get("opacity", 100))
        form.addRow("Opacity:", self._opacity)

        return w

    def _on_color_changed(self, name):
        if name == "Custom...":
            color = QColorDialog.getColor()
            if color.isValid():
                self._settings["custom_color"] = color.name()
                self._settings["theme_color"] = color.name()
                self._settings["theme_color_name"] = "Custom"
                self._update_color_preview()
        elif name in THEME_COLORS:
            self._settings["theme_color"] = THEME_COLORS[name]
            self._settings["theme_color_name"] = name
            self._settings["custom_color"] = None
            self._update_color_preview()

    def _update_color_preview(self):
        c = self._settings.get("theme_color", "#FF6611")
        self._color_preview.setStyleSheet(f"background-color: {c}; border-radius: 4px;")

    # ── Models ────────────────────────────────────────────────

    def _build_models_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        # CLI Brain
        brain_group = QGroupBox("CLI Brain (the thinker)")
        brain_layout = QFormLayout(brain_group)
        self._cli_model = QComboBox()
        self._cli_model.addItems(["opus", "sonnet", "haiku"])
        self._cli_model.setCurrentText(self._settings.get("cli_model", "sonnet"))
        brain_layout.addRow("Model:", self._cli_model)

        desc = QLabel("Opus = best quality, most expensive\nSonnet = balanced (recommended)\nHaiku = cheapest, fastest")
        desc.setStyleSheet("color: #666; font-size: 10px;")
        brain_layout.addRow(desc)
        form.addRow(brain_group)

        # Runner
        runner_group = QGroupBox("Runner (the worker)")
        runner_layout = QFormLayout(runner_group)
        self._runner = QComboBox()
        self._runner.addItems(["haiku_api", "local", "none"])
        self._runner.setCurrentText(self._settings.get("runner", "haiku_api"))
        self._runner.currentTextChanged.connect(self._on_runner_changed)
        runner_layout.addRow("Backend:", self._runner)

        self._local_model = QLineEdit(self._settings.get("local_model", "gemma4:e2b"))
        self._local_model.setPlaceholderText("e.g., gemma4:e2b, llama3.2:3b")
        self._local_model_label = QLabel("Local Model:")
        runner_layout.addRow(self._local_model_label, self._local_model)
        self._on_runner_changed(self._runner.currentText())

        desc2 = QLabel("haiku_api = Anthropic API (~$0.05/task)\nlocal = Ollama on GPU (free)\nnone = CLI brain does everything")
        desc2.setStyleSheet("color: #666; font-size: 10px;")
        runner_layout.addRow(desc2)
        form.addRow(runner_group)

        # Routing
        routing_group = QGroupBox("Routing Strategy")
        routing_layout = QFormLayout(routing_group)
        self._routing = QComboBox()
        self._routing.addItems(["vanilla", "flat", "hierarchy", "haiku_vanilla", "haiku_local"])
        self._routing.setCurrentText(self._settings.get("routing", "flat"))
        routing_layout.addRow("Mode:", self._routing)

        desc3 = QLabel(
            "vanilla — brain does everything (baseline)\n"
            "flat — brain plans+synth, delegates sub-tasks\n"
            "hierarchy — brain plans, manager triages workers\n"
            "haiku_vanilla — all Haiku, zero CLI quota\n"
            "haiku_local — Haiku+local, ultra-saver"
        )
        desc3.setStyleSheet("color: #666; font-size: 10px;")
        routing_layout.addRow(desc3)
        form.addRow(routing_group)

        return w

    def _on_runner_changed(self, text):
        is_local = text == "local"
        self._local_model.setEnabled(is_local)
        self._local_model_label.setEnabled(is_local)

    # ── Memory ────────────────────────────────────────────────

    def _build_memory_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        self._hyphae_enabled = QCheckBox("Enable Hyphae Memory")
        self._hyphae_enabled.setChecked(self._settings.get("hyphae_enabled", True))
        form.addRow(self._hyphae_enabled)

        self._hyphae_url = QLineEdit(self._settings.get("hyphae_url", "http://127.0.0.1:8100"))
        form.addRow("Hyphae URL:", self._hyphae_url)

        self._hyphae_status = QLabel("Unknown")
        self._hyphae_status.setStyleSheet("color: #666;")
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_hyphae)
        row = QHBoxLayout()
        row.addWidget(self._hyphae_status)
        row.addWidget(test_btn)
        form.addRow("Status:", row)

        desc = QLabel(
            "Hyphae is a long-term memory server that remembers\n"
            "your project context across sessions. Install separately:\n"
            "  pip install hyphae && hyphae"
        )
        desc.setStyleSheet("color: #666; font-size: 10px;")
        form.addRow(desc)

        return w

    def _test_hyphae(self):
        import urllib.request
        url = self._hyphae_url.text().strip()
        try:
            req = urllib.request.Request(f"{url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                import json as _json
                data = _json.loads(resp.read())
                facts = data.get("total_facts", "?")
                self._hyphae_status.setText(f"Connected ({facts} facts)")
                self._hyphae_status.setStyleSheet("color: #44bb44;")
        except Exception as e:
            self._hyphae_status.setText(f"Offline")
            self._hyphae_status.setStyleSheet("color: #cc4444;")

    # ── Quota ─────────────────────────────────────────────────

    def _build_quota_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        from openkeel.quota import get_usage

        usage = get_usage()

        # Current usage bar
        self._quota_bar = QProgressBar()
        self._quota_bar.setRange(0, 100)
        self._quota_bar.setValue(int(min(usage["pct"], 100)))
        self._quota_bar.setFormat(f'{usage["pct"]:.1f}% used')
        form.addRow("This Week:", self._quota_bar)

        # Stats
        stats = QLabel(
            f'Runs: {usage["runs"]}  |  '
            f'Sonnet: {usage["sonnet_calls"]}  |  '
            f'Haiku: {usage["haiku_calls"]}  |  '
            f'Local: {usage["local_calls"]}\n'
            f'BPH: {usage["bph"]:.1f}%/hr  |  '
            f'Week started: {usage["week_start"]}'
        )
        stats.setStyleSheet("color: #999; font-size: 10px;")
        form.addRow(stats)

        # Weekly limit
        self._weekly_limit = QSpinBox()
        self._weekly_limit.setRange(100_000, 200_000_000)
        self._weekly_limit.setSingleStep(1_000_000)
        self._weekly_limit.setValue(self._settings.get("weekly_limit", 5_000_000))
        self._weekly_limit.setSuffix(" OEQ")
        form.addRow("Weekly Limit:", self._weekly_limit)

        desc = QLabel(
            "OEQ = Output-Equivalent Tokens (normalized cost unit)\n"
            "  Claude Pro:       ~5,000,000 OEQ/week\n"
            "  Claude Max (5x):  ~25,000,000 OEQ/week\n"
            "  Claude Max (20x): ~100,000,000 OEQ/week"
        )
        desc.setStyleSheet("color: #666; font-size: 10px;")
        form.addRow(desc)

        # Reset
        reset_btn = QPushButton("Reset Weekly Counter")
        reset_btn.clicked.connect(self._reset_quota)
        form.addRow(reset_btn)

        return w

    def _reset_quota(self):
        from openkeel.quota import reset
        reset()
        self._quota_bar.setValue(0)
        self._quota_bar.setFormat("0.0% used")

    # ── Save ──────────────────────────────────────────────────

    def _save(self):
        s = self._settings
        # Appearance
        s["font_family"] = self._font_combo.currentText()
        s["font_size"] = self._font_size.value()
        s["opacity"] = self._opacity.value()
        # Models
        s["cli_model"] = self._cli_model.currentText()
        s["runner"] = self._runner.currentText()
        s["local_model"] = self._local_model.text().strip()
        s["routing"] = self._routing.currentText()
        # Memory
        s["hyphae_enabled"] = self._hyphae_enabled.isChecked()
        s["hyphae_url"] = self._hyphae_url.text().strip()
        # Quota
        s["weekly_limit"] = self._weekly_limit.value()

        save_settings(s)
        self.accept()

    def get_settings(self) -> dict:
        return self._settings
