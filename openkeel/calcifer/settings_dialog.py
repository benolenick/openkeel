#!/usr/bin/env python3
"""Settings dialog for Calcifer routing configuration in Ladder GUI."""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox
)
from PyQt5.QtCore import Qt
from openkeel.calcifer.routing_policy import RoutingPolicy


class SettingsDialog(QDialog):
    """Modal dialog for routing settings."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calcifer Router Settings")
        self.setGeometry(100, 100, 500, 400)
        self.policy = RoutingPolicy()
        self.setup_ui()

    def setup_ui(self):
        """Build the settings UI."""
        layout = QVBoxLayout()

        # Preset selector
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("Routing Preset:"))

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["cheap", "balanced", "quality", "local"])
        self.preset_combo.setCurrentText(self.policy.config.preset)
        self.preset_combo.currentTextChanged.connect(self.on_preset_changed)
        preset_layout.addWidget(self.preset_combo)

        layout.addLayout(preset_layout)

        # Preset descriptions
        descriptions = {
            "cheap": "Minimize cost: use Sonnet for hard tasks",
            "balanced": "Default: Opus for design, Sonnet for standard",
            "quality": "Maximum quality: use Opus for everything",
            "local": "Prefer local Ollama models when available",
        }

        self.desc_label = QLabel(descriptions.get(self.policy.config.preset, ""))
        self.desc_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(self.desc_label)

        # Model assignments table
        layout.addWidget(QLabel("\nModel Assignments:"))

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Band", "Model"])
        self.table.setRowCount(5)

        bands = ["Band A (Chat)", "Band B (Simple)", "Band C (Standard)", "Band D (Hard)", "Judge"]
        band_keys = ["band_a", "band_b", "band_c", "band_d", "judge"]

        for i, (band, key) in enumerate(zip(bands, band_keys)):
            self.table.setItem(i, 0, QTableWidgetItem(band))
            model = self.policy.config.models.get(key, "unknown")
            self.table.setItem(i, 1, QTableWidgetItem(model))

        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)

        # Buttons
        button_layout = QHBoxLayout()

        save_btn = QPushButton("Save Settings")
        save_btn.clicked.connect(self.on_save)
        button_layout.addWidget(save_btn)

        reset_btn = QPushButton("Reset to Default")
        reset_btn.clicked.connect(self.on_reset)
        button_layout.addWidget(reset_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)

        self.setLayout(layout)

    def on_preset_changed(self, preset_name):
        """Update description and table when preset changes."""
        descriptions = {
            "cheap": "Minimize cost: use Sonnet for hard tasks",
            "balanced": "Default: Opus for design, Sonnet for standard",
            "quality": "Maximum quality: use Opus for everything",
            "local": "Prefer local Ollama models when available",
        }
        self.desc_label.setText(descriptions.get(preset_name, ""))

        # Update table with new models
        self.policy = RoutingPolicy(preset=preset_name)
        band_keys = ["band_a", "band_b", "band_c", "band_d", "judge"]
        for i, key in enumerate(band_keys):
            model = self.policy.config.models.get(key, "unknown")
            self.table.setItem(i, 1, QTableWidgetItem(model))

    def on_save(self):
        """Save settings to file."""
        try:
            self.policy.save()
            QMessageBox.information(self, "Success", f"Settings saved!\nPreset: {self.policy.config.preset}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def on_reset(self):
        """Reset to default settings."""
        try:
            RoutingPolicy.CONFIG_FILE.unlink(missing_ok=True)
            self.policy = RoutingPolicy.get_preset("balanced")
            self.preset_combo.setCurrentText("balanced")
            QMessageBox.information(self, "Success", "Settings reset to defaults")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to reset: {e}")

    def get_selected_preset(self):
        """Return the selected preset."""
        return self.preset_combo.currentText()
