"""Theme constants and stylesheet builder for OpenKeel 2.0."""

DARK_BG = "#0d0d0d"
TOOLBAR_BG = "#1a1a1a"
PANEL_BG = "#141414"
TEXT_DIM = "#666666"
TEXT_LIGHT = "#cccccc"
TEXT_WHITE = "#eeeeee"
BORDER_WIDTH = 3

THEME_COLORS = {
    "Neon Orange": "#FF6611",
    "Electric Blue": "#0088FF",
    "Cyber Green": "#00FF88",
    "Hot Pink": "#FF1177",
    "Purple Rain": "#9944FF",
    "Solar Yellow": "#FFCC00",
    "Ice White": "#DDDDDD",
    "Blood Red": "#CC0000",
}

DEFAULT_FONT = "Cascadia Mono"
FONT_FAMILIES = [
    "Cascadia Mono", "JetBrains Mono", "Fira Code", "Source Code Pro",
    "Ubuntu Mono", "Consolas", "Courier New", "monospace",
]


def build_stylesheet(accent: str) -> str:
    """Build the application stylesheet with the given accent color."""
    return f"""
    QMainWindow {{
        background-color: {DARK_BG};
        color: {TEXT_LIGHT};
    }}
    QWidget#toolbar {{
        background-color: {TOOLBAR_BG};
        border-bottom: 1px solid #333333;
    }}
    QLabel {{
        color: {TEXT_LIGHT};
    }}
    QLabel#brand {{
        color: {accent};
        font-size: 16px;
        font-weight: bold;
        letter-spacing: 3px;
    }}
    QLabel#status {{
        color: {TEXT_DIM};
        font-size: 10px;
    }}
    QLabel#status-ok {{
        color: #44bb44;
        font-size: 10px;
    }}
    QLabel#status-warn {{
        color: {accent};
        font-size: 10px;
    }}
    QPushButton#launch-btn {{
        background-color: {accent};
        border: none;
        border-radius: 4px;
        color: #000000;
        padding: 4px 14px;
        font-size: 11px;
        font-weight: bold;
    }}
    QPushButton#launch-btn:hover {{
        background-color: {TEXT_LIGHT};
    }}
    QPushButton#launch-btn:disabled {{
        background-color: #333333;
        color: #666666;
    }}
    QPushButton#settings-btn {{
        background-color: transparent;
        border: 1px solid #444444;
        border-radius: 4px;
        color: {TEXT_LIGHT};
        padding: 4px 10px;
        font-size: 12px;
    }}
    QPushButton#settings-btn:hover {{
        border-color: {accent};
        color: {accent};
    }}
    QComboBox {{
        background-color: #222222;
        color: {TEXT_LIGHT};
        border: 1px solid #444444;
        border-radius: 3px;
        padding: 3px 8px;
        font-size: 11px;
    }}
    QComboBox:hover {{
        border-color: {accent};
    }}
    QComboBox QAbstractItemView {{
        background-color: #222222;
        color: {TEXT_LIGHT};
        selection-background-color: {accent};
    }}
    QSlider::groove:horizontal {{
        background: #333333;
        height: 4px;
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {accent};
        width: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }}
    QSpinBox {{
        background-color: #222222;
        color: {TEXT_LIGHT};
        border: 1px solid #444444;
        border-radius: 3px;
        padding: 2px 6px;
    }}
    QLineEdit {{
        background-color: #222222;
        color: {TEXT_LIGHT};
        border: 1px solid #444444;
        border-radius: 3px;
        padding: 4px 8px;
    }}
    QLineEdit:focus {{
        border-color: {accent};
    }}
    QTabWidget::pane {{
        border: 1px solid #333333;
        background-color: {DARK_BG};
    }}
    QTabBar::tab {{
        background-color: #1a1a1a;
        color: {TEXT_DIM};
        padding: 6px 16px;
        border: 1px solid #333333;
        border-bottom: none;
    }}
    QTabBar::tab:selected {{
        color: {accent};
        background-color: {DARK_BG};
    }}
    QCheckBox {{
        color: {TEXT_LIGHT};
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid #555;
        border-radius: 3px;
        background: #222;
    }}
    QCheckBox::indicator:checked {{
        background: {accent};
        border-color: {accent};
    }}
    QProgressBar {{
        background-color: #222222;
        border: 1px solid #444;
        border-radius: 4px;
        text-align: center;
        color: {TEXT_LIGHT};
        font-size: 10px;
    }}
    QProgressBar::chunk {{
        background-color: {accent};
        border-radius: 3px;
    }}
    QDialog {{
        background-color: {DARK_BG};
        color: {TEXT_LIGHT};
    }}
    """
