"""Terminal widget: pyte VT100 emulator + winpty ConPTY backend."""

from __future__ import annotations

import threading

import pyte
import sys
if sys.platform == 'win32':
    from winpty import PtyProcess
else:
    from ptyprocess import PtyProcessUnicode as PtyProcess

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# ---------------------------------------------------------------------------
# Color tables
# ---------------------------------------------------------------------------

_STANDARD = [
    "#000000", "#cc0000", "#4e9a06", "#c4a000",
    "#3465a4", "#75507b", "#06989a", "#d3d7cf",
]
_BRIGHT = [
    "#555753", "#ef2929", "#8ae234", "#fce94f",
    "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
]
_COLOR_NAMES = {
    "black": 0, "red": 1, "green": 2, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "brightblack": 8, "brightred": 9, "brightgreen": 10, "brightyellow": 11,
    "brightblue": 12, "brightmagenta": 13, "brightcyan": 14, "brightwhite": 15,
}

DEFAULT_FG = "#d3d7cf"
DEFAULT_BG = "#0d0d0d"
CURSOR_COLOR = "#FF6611"


def _idx_to_hex(n: int) -> str:
    if n < 8:
        return _STANDARD[n]
    if n < 16:
        return _BRIGHT[n - 8]
    if n < 232:
        n -= 16
        r, g, b = (n // 36) * 51, ((n % 36) // 6) * 51, (n % 6) * 51
        return f"#{r:02x}{g:02x}{b:02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _resolve(color: str, bold: bool = False, is_fg: bool = True) -> str:
    if color == "default" or color is None:
        return DEFAULT_FG if is_fg else DEFAULT_BG
    if color in _COLOR_NAMES:
        idx = _COLOR_NAMES[color]
        if bold and is_fg and idx < 8:
            idx += 8
        return _idx_to_hex(idx)
    try:
        return _idx_to_hex(int(color))
    except (ValueError, TypeError):
        pass
    if isinstance(color, str) and len(color) == 6:
        try:
            int(color, 16)
            return f"#{color}"
        except ValueError:
            pass
    return DEFAULT_FG if is_fg else DEFAULT_BG


# ---------------------------------------------------------------------------
# Key mapping
# ---------------------------------------------------------------------------

_KEY_MAP = {
    Qt.Key.Key_Up: "\x1b[A",
    Qt.Key.Key_Down: "\x1b[B",
    Qt.Key.Key_Right: "\x1b[C",
    Qt.Key.Key_Left: "\x1b[D",
    Qt.Key.Key_Home: "\x1b[H",
    Qt.Key.Key_End: "\x1b[F",
    Qt.Key.Key_Insert: "\x1b[2~",
    Qt.Key.Key_Delete: "\x1b[3~",
    Qt.Key.Key_PageUp: "\x1b[5~",
    Qt.Key.Key_PageDown: "\x1b[6~",
    Qt.Key.Key_F1: "\x1bOP",
    Qt.Key.Key_F2: "\x1bOQ",
    Qt.Key.Key_F3: "\x1bOR",
    Qt.Key.Key_F4: "\x1bOS",
    Qt.Key.Key_F5: "\x1b[15~",
    Qt.Key.Key_F6: "\x1b[17~",
    Qt.Key.Key_F7: "\x1b[18~",
    Qt.Key.Key_F8: "\x1b[19~",
    Qt.Key.Key_F9: "\x1b[20~",
    Qt.Key.Key_F10: "\x1b[21~",
    Qt.Key.Key_F11: "\x1b[23~",
    Qt.Key.Key_F12: "\x1b[24~",
    Qt.Key.Key_Backspace: "\x7f",
    Qt.Key.Key_Tab: "\t",
    Qt.Key.Key_Return: "\r",
    Qt.Key.Key_Enter: "\r",
    Qt.Key.Key_Escape: "\x1b",
}


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class TerminalWidget(QWidget):
    """Full terminal emulator widget backed by Windows ConPTY."""

    data_received = Signal(str)
    process_finished = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        shell: str = "powershell.exe",
        cols: int = 120,
        rows: int = 30,
    ) -> None:
        super().__init__(parent)

        self._cols = cols
        self._rows = rows
        self._shell = shell

        # Font
        self._font = QFont("Cascadia Mono", 11)
        self._font.setStyleHint(QFont.StyleHint.Monospace)
        fm = QFontMetrics(self._font)
        self._cell_w = fm.horizontalAdvance("M")
        self._cell_h = fm.height()
        self._ascent = fm.ascent()

        # pyte — HistoryScreen keeps scrollback lines
        self._screen = pyte.HistoryScreen(cols, rows, history=2000)
        self._screen.set_mode(pyte.modes.LNM)
        self._stream = pyte.Stream(self._screen)

        # Scrollback offset (0 = bottom, positive = scrolled up)
        self._scroll_offset = 0

        # Overwatch hook (set by app.py)
        self._overwatch_callback = None

        # Governance hook (set by app.py)
        # callable(command: str) -> str  ("allow", "deny", "gate")
        self._governance_callback = None

        # Mouse text selection
        self._selecting = False
        self._sel_start: tuple[int, int] | None = None  # (col, row) in screen coords
        self._sel_end: tuple[int, int] | None = None

        # Focus & input
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(self._cell_w * 40, self._cell_h * 10)

        # Cursor blink
        self._cursor_visible = True
        self._blink = QTimer(self)
        self._blink.timeout.connect(self._toggle_cursor)
        self._blink.start(530)

        # Wire signal for thread-safe data delivery
        self.data_received.connect(self._on_data)

        # PTY
        self._pty: PtyProcess | None = None
        self._reader_alive = False
        self._spawn()

    # ----- PTY management -----

    def _spawn(self) -> None:
        import os
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        self._pty = PtyProcess.spawn(
            self._shell if isinstance(self._shell, list) else [self._shell],
            dimensions=(self._rows, self._cols),
            env=env,
        )
        self._reader_alive = True
        t = threading.Thread(target=self._reader_loop, daemon=True)
        t.start()

    def _reader_loop(self) -> None:
        import time

        while self._reader_alive:
            try:
                if not self._pty or not self._pty.isalive():
                    break
                data = self._pty.read(8192)
                if data:
                    self.data_received.emit(data)
            except EOFError:
                break
            except Exception:
                time.sleep(0.01)
        self.process_finished.emit()

    def _on_data(self, data: str) -> None:
        self._stream.feed(data)
        # Only auto-scroll to bottom if user isn't scrolled up
        if self._scroll_offset == 0:
            pass  # already at bottom, stay there
        # If user is scrolled up, leave them where they are
        # Feed raw text to any attached overwatch
        if self._overwatch_callback:
            try:
                self._overwatch_callback(data)
            except Exception:
                pass
        self.update()

    # ----- Rendering -----

    def _get_history_lines(self) -> list:
        """Get scrollback history as a list of line dicts (oldest first)."""
        history = self._screen.history
        if not history or not history.top:
            return []
        return list(history.top)

    def _is_selected(self, col: int, row: int) -> bool:
        """Check if a cell is within the current selection."""
        if not self._sel_start or not self._sel_end:
            return False
        r1, c1 = self._sel_start[1], self._sel_start[0]
        r2, c2 = self._sel_end[1], self._sel_end[0]
        # Normalize so (r1,c1) <= (r2,c2)
        if (r1, c1) > (r2, c2):
            r1, c1, r2, c2 = r2, c2, r1, c1
        if row < r1 or row > r2:
            return False
        if row == r1 and row == r2:
            return c1 <= col <= c2
        if row == r1:
            return col >= c1
        if row == r2:
            return col <= c2
        return True

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setFont(self._font)
        p.fillRect(self.rect(), QColor(DEFAULT_BG))

        screen = self._screen
        cell_w, cell_h, ascent = self._cell_w, self._cell_h, self._ascent
        default_bg_q = QColor(DEFAULT_BG)
        sel_bg_q = QColor("#334466")

        # Build displayable lines: history (scrolled) + screen buffer
        history_lines = self._get_history_lines()
        hist_len = len(history_lines)
        offset = self._scroll_offset

        for row in range(screen.lines):
            y = row * cell_h
            # Which line to display: if scrolled up, show history
            display_row = row - offset
            hist_row = hist_len + display_row  # index into history

            if display_row < 0 and 0 <= hist_row < hist_len:
                # Rendering a history line
                line = history_lines[hist_row]
                for col in range(screen.columns):
                    char = line.get(col)
                    x = col * cell_w
                    if char is None:
                        if self._is_selected(col, row):
                            p.fillRect(x, y, cell_w, cell_h, sel_bg_q)
                        continue

                    fg = _resolve(char.fg, char.bold, True)
                    bg = _resolve(char.bg, False, False)
                    if char.reverse:
                        fg, bg = bg, fg

                    bg_q = sel_bg_q if self._is_selected(col, row) else QColor(bg)
                    if bg_q != default_bg_q:
                        p.fillRect(x, y, cell_w, cell_h, bg_q)

                    if char.data and char.data != " ":
                        p.setPen(QColor(fg))
                        p.drawText(x, y + ascent, char.data)
            elif 0 <= display_row < screen.lines:
                # Rendering a live screen line
                line = screen.buffer[display_row]
                for col in range(screen.columns):
                    char = line[col]
                    x = col * cell_w

                    fg = _resolve(char.fg, char.bold, True)
                    bg = _resolve(char.bg, False, False)
                    if char.reverse:
                        fg, bg = bg, fg

                    bg_q = sel_bg_q if self._is_selected(col, row) else QColor(bg)
                    if bg_q != default_bg_q:
                        p.fillRect(x, y, cell_w, cell_h, bg_q)

                    if char.data and char.data != " ":
                        needs_style = char.bold or char.italics
                        if needs_style:
                            styled = QFont(self._font)
                            if char.bold:
                                styled.setBold(True)
                            if char.italics:
                                styled.setItalic(True)
                            p.setFont(styled)

                        p.setPen(QColor(fg))
                        p.drawText(x, y + ascent, char.data)

                        if needs_style:
                            p.setFont(self._font)

        # Cursor (only when not scrolled up)
        if self._scroll_offset == 0 and self._cursor_visible and screen.cursor:
            cx = screen.cursor.x * cell_w
            cy = screen.cursor.y * cell_h
            p.fillRect(cx, cy, cell_w, cell_h, QColor(CURSOR_COLOR))
            cchar = screen.buffer[screen.cursor.y][screen.cursor.x]
            if cchar.data and cchar.data != " ":
                p.setPen(QColor(DEFAULT_BG))
                p.drawText(cx, cy + ascent, cchar.data)

        p.end()

    # ----- Input -----

    def _read_current_line(self) -> str:
        """Read the current command line from the pyte screen buffer.

        Reads the cursor's row and extracts text after the prompt.
        """
        screen = self._screen
        if not screen or not screen.cursor:
            return ""
        row = screen.cursor.y
        line_chars = screen.buffer[row]
        raw = "".join(line_chars[col].data for col in range(screen.columns)).rstrip()
        # Strip common shell prompts: PS C:\...>, $, >, C:\...>
        # Look for the last prompt marker and take everything after it
        for marker in ("> ", "$ ", "# "):
            idx = raw.rfind(marker)
            if idx >= 0:
                return raw[idx + len(marker):].strip()
        return raw.strip()

    def keyPressEvent(self, event) -> None:
        if not self._pty or not self._pty.isalive():
            return

        key = event.key()
        mods = event.modifiers()

        # Shift+PageUp / Shift+PageDown — scrollback
        if (mods & Qt.KeyboardModifier.ShiftModifier) and key == Qt.Key.Key_PageUp:
            hist_len = len(self._get_history_lines())
            self._scroll_offset = min(self._scroll_offset + self._rows, hist_len)
            self.update()
            return
        if (mods & Qt.KeyboardModifier.ShiftModifier) and key == Qt.Key.Key_PageDown:
            self._scroll_offset = max(self._scroll_offset - self._rows, 0)
            self.update()
            return

        # Ctrl+Shift+V — paste
        if (
            mods == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            and key == Qt.Key.Key_V
        ):
            text = QApplication.clipboard().text()
            if text:
                self._pty.write(text)
            return

        # Ctrl+Shift+C — copy selection
        if (
            mods == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            and key == Qt.Key.Key_C
        ):
            self._copy_selection()
            return

        # Ctrl+<letter>
        if mods & Qt.KeyboardModifier.ControlModifier and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self._pty.write(chr(key - Qt.Key.Key_A + 1))
            return

        # --- Governance interception on Enter/Return ---
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._governance_callback:
            command = self._read_current_line()
            if command:
                decision = self._governance_callback(command)
                if decision == "deny":
                    # Cancel the line (Ctrl+C) and don't send Enter
                    self._pty.write("\x03")
                    return
                if decision == "gate":
                    # Gate = don't send Enter yet, let app.py handle the dialog
                    return

        # Special keys
        seq = _KEY_MAP.get(key)
        if seq is not None:
            self._pty.write(seq)
            return

        # Regular text
        text = event.text()
        if text:
            self._pty.write(text)

    # ----- Mouse selection -----

    def _pixel_to_cell(self, x: int, y: int) -> tuple[int, int]:
        """Convert pixel coords to (col, row) in screen coords."""
        col = max(0, min(x // self._cell_w, self._cols - 1))
        row = max(0, min(y // self._cell_h, self._rows - 1))
        return col, row

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            col, row = self._pixel_to_cell(int(event.position().x()), int(event.position().y()))
            self._selecting = True
            self._sel_start = (col, row)
            self._sel_end = (col, row)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._selecting:
            col, row = self._pixel_to_cell(int(event.position().x()), int(event.position().y()))
            self._sel_end = (col, row)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._selecting = False
            # If start == end, clear selection (was just a click)
            if self._sel_start == self._sel_end:
                self._sel_start = None
                self._sel_end = None
                self.update()

    def contextMenuEvent(self, event) -> None:
        """Right-click context menu for Copy / Paste."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #1a1a1a; color: #d3d7cf; border: 1px solid #333; }"
            "QMenu::item:selected { background: #334466; }"
        )
        copy_act = menu.addAction("Copy")
        paste_act = menu.addAction("Paste")
        has_sel = bool(self._sel_start and self._sel_end and self._sel_start != self._sel_end)
        copy_act.setEnabled(has_sel)
        paste_act.setEnabled(bool(QApplication.clipboard().text()))
        action = menu.exec(event.globalPos())
        if action == copy_act:
            self._copy_selection()
        elif action == paste_act:
            text = QApplication.clipboard().text()
            if text and self._pty and self._pty.isalive():
                self._pty.write(text)

    def _get_selected_text(self) -> str:
        """Extract the text within the current selection."""
        if not self._sel_start or not self._sel_end:
            return ""
        r1, c1 = self._sel_start[1], self._sel_start[0]
        r2, c2 = self._sel_end[1], self._sel_end[0]
        if (r1, c1) > (r2, c2):
            r1, c1, r2, c2 = r2, c2, r1, c1

        screen = self._screen
        history_lines = self._get_history_lines()
        hist_len = len(history_lines)
        offset = self._scroll_offset
        lines = []

        for row in range(r1, r2 + 1):
            display_row = row - offset
            hist_row = hist_len + display_row

            start_col = c1 if row == r1 else 0
            end_col = c2 if row == r2 else screen.columns - 1

            chars = []
            if display_row < 0 and 0 <= hist_row < hist_len:
                line = history_lines[hist_row]
                for col in range(start_col, end_col + 1):
                    char = line.get(col)
                    chars.append(char.data if char else " ")
            elif 0 <= display_row < screen.lines:
                line = screen.buffer[display_row]
                for col in range(start_col, end_col + 1):
                    chars.append(line[col].data)

            lines.append("".join(chars).rstrip())

        return "\n".join(lines)

    def _copy_selection(self) -> None:
        """Copy selected text to clipboard."""
        text = self._get_selected_text()
        if text:
            QApplication.clipboard().setText(text)
            # Clear selection after copy
            self._sel_start = None
            self._sel_end = None
            self.update()

    def wheelEvent(self, event) -> None:
        """Mouse wheel scrolls through history."""
        delta = event.angleDelta().y()
        hist_len = len(self._get_history_lines())
        if delta > 0:
            # Scroll up
            self._scroll_offset = min(self._scroll_offset + 3, hist_len)
        elif delta < 0:
            # Scroll down
            self._scroll_offset = max(self._scroll_offset - 3, 0)
        self.update()

    # ----- Resize -----

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        new_cols = max(10, self.width() // self._cell_w)
        new_rows = max(3, self.height() // self._cell_h)
        if new_cols != self._cols or new_rows != self._rows:
            self._cols = new_cols
            self._rows = new_rows
            self._screen.resize(new_rows, new_cols)
            if self._pty and self._pty.isalive():
                self._pty.setwinsize(new_rows, new_cols)
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._cell_w * self._cols, self._cell_h * self._rows)

    # ----- Cursor blink -----

    def _toggle_cursor(self) -> None:
        self._cursor_visible = not self._cursor_visible
        if self._screen and self._screen.cursor:
            cx = self._screen.cursor.x * self._cell_w
            cy = self._screen.cursor.y * self._cell_h
            self.update(cx, cy, self._cell_w, self._cell_h)

    # ----- Cleanup -----

    def cleanup(self) -> None:
        self._reader_alive = False
        self._blink.stop()
        if self._pty and self._pty.isalive():
            self._pty.terminate()
