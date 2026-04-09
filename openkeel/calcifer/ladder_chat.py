"""Calcifer's Ladder — chat window with runner dials.

Implements Calcifer's Ladder theory:
  start at the cheapest local runner, escalate outward only as needed.

Router ladder (bottom → top):
  gemma4:e2b  @kaloth 3070  ← default cheap/fast
  qwen2.5:3b  @jagg  3090
  gemma4:26b  @jagg  3090
  Haiku       cloud
  Sonnet      cloud
  Opus        cloud          ← most expensive

Override tags: @local @qwen @big @haiku @sonnet @opus
Force heavy:   "think hard" "architect" "audit" "ultrathink"

Launch:
    python -m openkeel.calcifer.ladder_chat
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from typing import Callable, Iterator

import anthropic
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPalette, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QScrollArea,
    QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from openkeel.calcifer.brain import (
    CALCIFER_SYSTEM_PROMPT, build_context, save_turn, get_recent_history,
)
from openkeel.calcifer.ladder_window import (
    DialWidget, USAGE, RUNNERS,
    _cloud_monitor, _local_monitor,
    DARK_BG, PANEL_BG, BORDER, DIM_TEXT, LIGHT_TXT, ORANGE,
)
from openkeel.calcifer.broker_gui_adapter import BrokerGUIAdapter

# ── Runner config ────────────────────────────────────────────────────────────

RUNNER_CFG: dict[str, dict] = {
    "gemma4_small": {
        "label": "gemma4·e2b",
        "host": "http://127.0.0.1:11434",
        "model": "gemma4:e2b",
        "kind": "ollama",
        "color": "#FF6611",
    },
    "qwen25": {
        "label": "qwen2.5·3b",
        "host": "http://192.168.0.224:11434",
        "model": "qwen2.5:3b",
        "kind": "ollama",
        "color": "#aa66ff",
    },
    "gemma4_large": {
        "label": "gemma4·26b",
        "host": "http://192.168.0.224:11434",
        "model": "gemma4:26b",
        "kind": "ollama",
        "color": "#ffaa33",
    },
    "haiku": {
        "label": "Haiku",
        "model": "claude-haiku-4-5-20251001",
        "kind": "anthropic",
        "color": "#55aa88",
    },
    "sonnet": {
        "label": "Sonnet",
        "model": "claude-sonnet-4-6",
        "kind": "anthropic",
        "color": "#5588cc",
    },
    "opus": {
        "label": "Opus",
        "model": "claude-opus-4-6",
        "kind": "anthropic",
        "color": "#cc5555",
    },
    "conductor": {
        "label": "conductor",
        "kind": "meta",
        "color": "#888888",
    },
}

# Anthropic client — picks up ANTHROPIC_BASE_URL automatically (routes through proxy)
_anth_client = anthropic.Anthropic()

SESSION_ID = f"ladder-{int(time.time())}"


# ── Routing ──────────────────────────────────────────────────────────────────
#
# Implements the Calcifer Routing Matrix (calcifer_routing_matrix_2026-04-08.md).
# Scores each message across 6 dimensions → Band A-E → runner.
#
# The Ladder (cheapest → most expensive):
#   gemma4_small  kaloth 3070  2B  — Band A: trivial, casual, instant
#   qwen25        jagg  3090  3B  — Band B with code signal (code-tuned)
#   gemma4_large  jagg  3090  26B — Band B general + light Band C
#   haiku         cloud           — Band B when local drift is annoying
#   sonnet        cloud           — Band C-D strong execution
#   opus          cloud           — Band D-E strategic governor

BAND_RUNNER = {
    "A": "gemma4_small",   # trivial / instant
    "B_code": "qwen25",    # lightweight code Q&A (code-tuned 3b)
    "B_gen": "gemma4_large", # lightweight general reasoning (26b)
    "B_fmt": "haiku",      # lightweight but formatting/consistency matters
    "C": "sonnet",         # operational — bounded multi-step
    "D": "sonnet",         # high-judgment — sonnet first (user can @opus)
    "E": "opus",           # strategic / governor
}


def _score_message(message: str) -> dict[str, int]:
    """Score message across 6 routing dimensions (0=low, 1=medium, 2=high)."""
    low = message.lower()
    n = len(message.split())

    # 1. Structural complexity
    s_high = any(k in low for k in [
        "design", "architect", "subsystem", "compare", "alternative",
        "trade-off", "tradeoff", "strategy", "overview", "approach",
        "plan the", "plan out", "roadmap", "high-level",
    ])
    structural = 2 if s_high else (0 if n < 12 else 1)

    # 2. Operational depth — multiple dependent steps
    d_high = any(k in low for k in [
        "implement", "build", "write a", "create a", "generate",
        "debug", "fix", "refactor", "migrate", "migration",
        "step by step", "pipeline", "multiple files", "throughout",
        "across the", "parse", "convert",
    ])
    depth = 2 if d_high else (0 if n < 8 else 1)

    # 3. Evidence need
    e_high = any(k in low for k in [
        "why", "what broke", "failure", "error", "exception",
        "investigate", "diagnose", "stack trace", "traceback",
        "log", "what caused", "root cause", "explain this",
        "what does this", "how does this",
    ])
    evidence = 2 if e_high else 0

    # 4. Verifiability risk — hard to objectively check the answer
    v_high = any(k in low for k in [
        "architecture", "correct approach", "right way", "should we",
        "best way", "is this right", "review", "audit", "sound",
        "good idea", "better approach",
    ])
    verifiability = 2 if v_high else 0

    # 5. Consequence of error
    c_high = any(k in low for k in [
        "deploy", "production", "delete", "drop table", "migration",
        "security", "auth", "authentication", "database", "critical",
        "breaking change", "data loss",
    ])
    consequence = 2 if c_high else 0

    # 6. Loop difficulty — iterative tool use required
    l_high = any(k in low for k in [
        "debug", "fix the", "investigate", "find", "locate",
        "inspect", "look at", "search", "run", "execute",
    ])
    loop = 2 if l_high else 0

    return {
        "structural": structural,
        "depth": depth,
        "evidence": evidence,
        "verifiability": verifiability,
        "consequence": consequence,
        "loop": loop,
    }


def _is_code_flavoured(message: str) -> bool:
    low = message.lower()
    return any(k in low for k in [
        "code", "function", "class", "method", "variable", "syntax",
        "python", "javascript", "typescript", "rust", "go ", "sql",
        "import", "bug", "error", "exception", "compile", "lint",
        "test", "patch", "diff", "pr ", "pull request",
    ])


def _needs_clean_formatting(message: str) -> bool:
    """Local models drift on formatting-heavy tasks."""
    low = message.lower()
    return any(k in low for k in [
        "write a report", "format", "markdown", "table", "bullet",
        "summary for", "email", "document",
    ])


def route(message: str) -> tuple[str, str]:
    """Return (runner_id, band_label) using the Calcifer routing matrix."""
    low = message.lower()

    # ── Explicit override tags (user always wins) ──────────────────────────
    for tag, rid in [
        ("@opus",   "opus"),
        ("@sonnet", "sonnet"),
        ("@haiku",  "haiku"),
        ("@big",    "gemma4_large"),
        ("@qwen",   "qwen25"),
        ("@local",  "gemma4_small"),
    ]:
        if tag in low:
            return rid, f"override·{rid}"

    # ── Hard escalation keywords (override band calc) ─────────────────────
    if any(k in low for k in ["think hard", "ultrathink", "deep dive",
                               "architect the", "plan and execute"]):
        return "opus", "E·strategic"

    # ── Score dimensions ───────────────────────────────────────────────────
    scores = _score_message(message)
    total = sum(scores.values())  # 0–12
    max_dim = max(scores.values())

    # ── Map to band ────────────────────────────────────────────────────────
    # Band A (0-1): truly trivial — pure casual/chitchat, no clear task
    if total <= 1:
        return "gemma4_small", "A·trivial"

    # Band B (2-5): lightweight — one main dimension triggered
    if total <= 5:
        if scores["consequence"] == 2 or scores["verifiability"] == 2:
            return "haiku", "B·cloud-lite"
        if _is_code_flavoured(message):
            return "qwen25", "B·code"
        if _needs_clean_formatting(message):
            return "haiku", "B·format"
        return "gemma4_large", "B·local-26b"

    # Band C (6-8): operational — multiple dimensions, bounded execution
    if total <= 8:
        return "sonnet", "C·operational"

    # Band D (9-10): high-judgment
    if total <= 10:
        return "sonnet", "D·judgment"

    # Band E (11-12): strategic / governor
    return "opus", "E·strategic"


# ── Streaming backends ───────────────────────────────────────────────────────

def _stream_ollama(
    runner_id: str,
    history: list[dict],
    on_token: Callable[[str], None],
) -> str:
    cfg = RUNNER_CFG[runner_id]
    messages = [{"role": "system", "content": CALCIFER_SYSTEM_PROMPT}] + history
    payload = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.7, "num_predict": 2048},
    }).encode()
    req = urllib.request.Request(
        f"{cfg['host']}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    full = []
    token_count = 0
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            try:
                chunk = json.loads(raw.decode().strip())
                token = chunk.get("message", {}).get("content", "")
                if token:
                    full.append(token)
                    token_count += 1
                    on_token(token)
                if chunk.get("done"):
                    break
            except Exception:
                pass
    with open("/tmp/calcifer_send.log", "a") as f:
        f.write(f"    _stream_ollama: {token_count} tokens, {len(''.join(full))} chars\n")
    return "".join(full)


def _stream_anthropic(
    runner_id: str,
    history: list[dict],
    on_token: Callable[[str], None],
) -> str:
    cfg = RUNNER_CFG[runner_id]
    full = []
    with _anth_client.messages.stream(
        model=cfg["model"],
        max_tokens=2048,
        system=CALCIFER_SYSTEM_PROMPT,
        messages=history,
    ) as stream:
        for text in stream.text_stream:
            full.append(text)
            on_token(text)
    return "".join(full)


# ── Qt signal bridge ─────────────────────────────────────────────────────────

class _Bridge(QObject):
    """Thread-safe signal bridge. Emits auto-queue to main thread."""
    token   = Signal(str)
    done    = Signal(str, str)   # (runner_id, full_text)
    error   = Signal(str)


# ── Message bubble ────────────────────────────────────────────────────────────

USER_BUBBLE_STYLE = f"""
    background: #1e2a1e;
    border: 1px solid #2a3a2a;
    border-radius: 8px;
    padding: 8px 12px;
    color: #aaddaa;
    font: 11px 'Cascadia Mono', monospace;
"""

def _runner_bubble_style(color: str) -> str:
    return f"""
    background: {PANEL_BG};
    border: 1px solid {color}44;
    border-left: 3px solid {color};
    border-radius: 8px;
    padding: 8px 12px;
    color: {LIGHT_TXT};
    font: 11px 'Cascadia Mono', monospace;
"""

LABEL_STYLE = f"color: {DIM_TEXT}; font: 9px 'Monospace'; margin-bottom: 2px;"


class MessageWidget(QWidget):
    """A single chat message bubble — supports live token appending."""

    def __init__(self, role: str, runner_id: str | None = None):
        super().__init__()
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 6)
        vlay.setSpacing(2)

        if role == "user":
            label = QLabel("you")
            label.setStyleSheet(LABEL_STYLE + " margin-left: 4px;")
            vlay.addWidget(label, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            cfg = RUNNER_CFG.get(runner_id or "gemma4_small", {})
            label_text = f"calcifer  ·  {cfg.get('label', runner_id)}"
            label = QLabel(label_text)
            label.setStyleSheet(LABEL_STYLE + f" color: {cfg.get('color', ORANGE)};")
            vlay.addWidget(label)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self._text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._text.setMinimumHeight(36)

        if role == "user":
            self._text.setStyleSheet(USER_BUBBLE_STYLE)
        else:
            cfg = RUNNER_CFG.get(runner_id or "gemma4_small", {})
            self._text.setStyleSheet(_runner_bubble_style(cfg.get("color", ORANGE)))

        vlay.addWidget(self._text)

        if role == "user":
            # right-align the bubble
            vlay.setAlignment(self._text, Qt.AlignmentFlag.AlignRight)
            self._text.setMaximumWidth(600)

    def append_token(self, token: str) -> None:
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(token)
        self._text.setTextCursor(cursor)
        self._text.setMinimumHeight(
            min(self._text.document().size().height() + 20, 400)
        )

    def set_text(self, text: str) -> None:
        self._text.setPlainText(text)
        self._text.setMinimumHeight(
            min(self._text.document().size().height() + 20, 400)
        )


# ── Input bar ─────────────────────────────────────────────────────────────────

class _InputEdit(QTextEdit):
    submit = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self.submit.emit()
                return
        super().keyPressEvent(event)


# ── Main window ───────────────────────────────────────────────────────────────

STYLESHEET = f"""
QMainWindow, QWidget {{ background: {DARK_BG}; color: {LIGHT_TXT}; }}
#frame {{
    border: 2px solid {ORANGE};
    background: {DARK_BG};
    border-radius: 4px;
}}
#toolbar {{
    background: #0f0f0f;
    border-bottom: 1px solid #1e1e1e;
}}
#input-area {{
    background: #111111;
    border-top: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
}}
#dial-strip {{
    background: {PANEL_BG};
    border-top: 1px solid {BORDER};
}}
QTextEdit#input-box {{
    background: #1a1a1a;
    color: {LIGHT_TXT};
    border: 1px solid #333;
    border-radius: 4px;
    font: 11px 'Cascadia Mono', monospace;
    padding: 4px 8px;
}}
QPushButton#send-btn {{
    background: {ORANGE};
    color: #0d0d0d;
    border: none;
    border-radius: 4px;
    font: bold 11px 'Monospace';
    padding: 6px 18px;
    min-width: 60px;
}}
QPushButton#send-btn:hover {{ background: #FF8833; }}
QPushButton#send-btn:disabled {{ background: #333; color: #666; }}
QPushButton#ghost {{
    background: transparent;
    color: {DIM_TEXT};
    border: none;
    font-size: 16px;
    padding: 2px 8px;
}}
QPushButton#ghost:hover {{ color: {ORANGE}; }}
"""

DIAL_STRIP_H = 130


class LadderChatWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Calcifer's Ladder")
        self.setStyleSheet(STYLESHEET)
        self.resize(960, 720)

        self._history: list[dict] = []   # {role, content}
        self._busy = False
        self._bridge = _Bridge()
        self._bridge.token.connect(self._on_token)
        self._bridge.done.connect(self._on_done)
        self._bridge.error.connect(self._on_error)
        self._active_runner: str | None = None
        self._active_bubble: MessageWidget | None = None
        self._full_response: list[str] = []

        # ── Broker ──
        self._session_id = SESSION_ID
        self._adapter = BrokerGUIAdapter()

        # ── Frame ──
        frame = QFrame()
        frame.setObjectName("frame")
        vlay = QVBoxLayout(frame)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        vlay.addWidget(self._build_toolbar())
        vlay.addWidget(self._build_chat_area(), stretch=1)
        vlay.addWidget(self._build_input_area())
        vlay.addWidget(self._build_dial_strip())

        self.setCentralWidget(frame)

        # ── Decay timer ──
        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick)
        self._decay_timer.start(500)

        # ── Background monitors ──
        self._stop = threading.Event()
        threading.Thread(target=_cloud_monitor, args=(self._stop,), daemon=True).start()
        threading.Thread(target=_local_monitor, args=(self._stop,), daemon=True).start()

        # Boot message + focus input
        QTimer.singleShot(200, self._show_boot)
        QTimer.singleShot(300, self._input.setFocus)

    # ── Toolbar ──────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("toolbar")
        bar.setFixedHeight(36)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 10, 0)
        lay.setSpacing(12)

        brand = QLabel("🔥  CALCIFER'S LADDER")
        brand.setStyleSheet(
            f"color: {ORANGE}; font: bold 13px 'Monospace'; letter-spacing: 2px;"
        )
        lay.addWidget(brand)

        lay.addStretch()

        self._route_lbl = QLabel("runner: —")
        self._route_lbl.setStyleSheet(
            f"color: {DIM_TEXT}; font: 10px 'Monospace';"
        )
        lay.addWidget(self._route_lbl)

        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("ghost")
        clear_btn.clicked.connect(self._clear_chat)
        lay.addWidget(clear_btn)

        return bar

    # ── Chat area ─────────────────────────────────────────────────────────────

    def _build_chat_area(self) -> QScrollArea:
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        self._chat_container = QWidget()
        self._chat_layout = QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(16, 12, 16, 12)
        self._chat_layout.setSpacing(8)
        self._chat_layout.addStretch()

        self._scroll.setWidget(self._chat_container)
        return self._scroll

    def _add_bubble(self, role: str, text: str = "", runner_id: str | None = None) -> MessageWidget:
        bubble = MessageWidget(role, runner_id)
        if text:
            bubble.set_text(text)
        idx = self._chat_layout.count() - 1  # before stretch
        self._chat_layout.insertWidget(idx, bubble)
        QTimer.singleShot(30, self._scroll_bottom)
        return bubble

    def _scroll_bottom(self) -> None:
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Input area ────────────────────────────────────────────────────────────

    def _build_input_area(self) -> QWidget:
        area = QWidget()
        area.setObjectName("input-area")
        area.setFixedHeight(70)
        lay = QHBoxLayout(area)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        self._input = _InputEdit()
        self._input.setObjectName("input-box")
        self._input.setPlaceholderText(
            "Message Calcifer…  (Shift+Enter for newline)  "
            "@local @qwen @big @haiku @sonnet @opus to force a runner"
        )
        self._input.setFixedHeight(54)
        self._input.submit.connect(self._send)
        lay.addWidget(self._input)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("send-btn")
        self._send_btn.setFixedHeight(54)
        self._send_btn.clicked.connect(self._send)
        lay.addWidget(self._send_btn)

        return area

    # ── Dial strip ────────────────────────────────────────────────────────────

    def _build_dial_strip(self) -> QWidget:
        strip = QWidget()
        strip.setObjectName("dial-strip")
        strip.setFixedHeight(DIAL_STRIP_H)

        lay = QHBoxLayout(strip)
        lay.setContentsMargins(12, 4, 12, 4)
        lay.setSpacing(0)

        self._dials: dict[str, DialWidget] = {}
        cloud_done = False

        for rid, label, sublabel, accent, group in RUNNERS:
            if group == "local" and not cloud_done:
                div = QFrame()
                div.setFrameShape(QFrame.Shape.VLine)
                div.setFixedWidth(1)
                div.setStyleSheet(f"background: {BORDER};")
                lay.addSpacing(8)
                lay.addWidget(div)
                lay.addSpacing(8)
                cloud_done = True

            dial = DialWidget(rid, label, sublabel, accent)
            dial.setMinimumSize(90, DIAL_STRIP_H - 8)
            self._dials[rid] = dial
            lay.addWidget(dial, stretch=1)

        return strip

    # ── Send / stream ─────────────────────────────────────────────────────────

    def _send(self) -> None:
        if self._busy:
            with open("/tmp/calcifer_send.log", "a") as f:
                f.write("  busy, returning\n")
            return
        text = self._input.toPlainText().strip()
        with open("/tmp/calcifer_send.log", "a") as f:
            f.write(f"  text: {repr(text[:50])}\n")
        if not text:
            with open("/tmp/calcifer_send.log", "a") as f:
                f.write("  empty, returning\n")
            return

        self._input.clear()
        self._busy = True
        self._send_btn.setEnabled(False)

        # Show user message
        self._add_bubble("user", text)
        save_turn(SESSION_ID, "user", text)

        self._route_lbl.setText("broker: planning → executing → judging")
        self._route_lbl.setStyleSheet("color: #FF6611; font: 10px 'Monospace';")
        USAGE.spike("opus", 100.0)

        # Create assistant bubble
        self._active_bubble = self._add_bubble("assistant", "", "broker")
        self._full_response = []
        bridge = self._bridge

        def _run():
            try:
                response = self._adapter.handle_user_message(text, self._session_id)
                bridge.done.emit("broker", response)
            except Exception as e:
                import traceback
                traceback.print_exc()
                bridge.error.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_token(self, token: str) -> None:
        if self._active_bubble:
            self._active_bubble.append_token(token)
        self._scroll_bottom()

    def _on_done(self, runner_id: str, full_text: str) -> None:
        if self._active_bubble:
            self._active_bubble.set_text(full_text)
        save_turn(SESSION_ID, "assistant", full_text)
        self._route_lbl.setStyleSheet(f"color: {DIM_TEXT}; font: 10px 'Monospace';")
        self._busy = False
        self._send_btn.setEnabled(True)
        self._active_bubble = None
        self._active_runner = None
        self._full_response = []

    def _on_error(self, msg: str) -> None:
        if self._active_bubble:
            self._active_bubble.append_token(f"\n\n*flame flickers*  {msg}")
        self._busy = False
        self._send_btn.setEnabled(True)
        self._active_bubble = None
        self._active_runner = None

    # ── Boot message ──────────────────────────────────────────────────────────

    def _show_boot(self) -> None:
        self._add_bubble(
            "assistant", "", "gemma4_small"
        ).set_text(
            "The fire's lit. I'm routing on the Ladder — "
            "cheapest local runner first, cloud only if you push me.\n\n"
            "Underneath, every turn now goes through Opus first for chat, planning, and delegation."
        )

    def _clear_chat(self) -> None:
        while self._chat_layout.count() > 1:
            item = self._chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._history.clear()

    # ── Tick / cleanup ────────────────────────────────────────────────────────

    def _tick(self) -> None:
        USAGE.tick_decay()
        snap = USAGE.snapshot()
        for rid, dial in self._dials.items():
            dial.set_value(snap[rid])

    def closeEvent(self, event) -> None:
        self._stop.set()
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    win = LadderChatWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
