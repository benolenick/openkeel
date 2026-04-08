"""Calcifer chat window — tkinter UI for talking to the fire demon.

A warm, fire-themed chat window that:
- Shows streaming responses token by token
- Persists conversation history via the brain's SQLite store
- Displays context indicators (Hyphae, Kanban, Tokens, ctx size)
- Lives in the system tray when not in use

Usage:
    python -m openkeel.calcifer.chat
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from openkeel.calcifer.brain import (
    chat_stream, is_alive, build_context,
    MODEL, OLLAMA_URL, CONTEXT_WINDOW, HYPHAE_URL, KANBAN_URL,
)


# ── Theme: warm fire colors ────────────────────────────────────

BG_DARK = "#0a0604"           # Deep ember black
BG_PANEL = "#1a0e06"          # Dark wood
BG_INPUT = "#110804"          # Input field
BG_HOVER = "#2a1810"          # Hover
BORDER = "#3a1e0e"            # Charred wood
BORDER_GLOW = "#ff6b1a"       # Fire orange

TEXT_PRIMARY = "#ffe4c2"       # Warm cream
TEXT_SECONDARY = "#ffb87a"    # Warm amber
TEXT_MUTED = "#8a5a3a"        # Faded ember

FIRE_ORANGE = "#ff6b1a"
FIRE_YELLOW = "#ffb830"
FIRE_RED = "#ff3a1a"
FIRE_GLOW = "#ffd270"

USER_COLOR = "#7ab8ff"         # Cool blue vs Calcifer's warm tones
SYSTEM_COLOR = "#8a5a3a"


class CalciferChat:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Calcifer 🔥")
        self.root.configure(bg=BG_DARK)
        self.root.geometry("800x650")
        self.root.minsize(500, 400)

        self.session_id = f"session-{int(time.time())}"
        self.thinking = False
        self._pulse_step = 0

        self._build_ui()
        self._check_calcifer_status()
        self._animate_fire()

        # Focus input
        self.input_box.focus_set()

        # Welcome message
        self._add_system_message(
            f"🔥 Calcifer is burning — {MODEL} on jagg ({CONTEXT_WINDOW:,} ctx)"
        )
        self._add_calcifer_message(
            "*crackling* What do you need, Ben?",
            animated=True
        )

    def _build_ui(self):
        # ── Header ─────────────────────────────────────────
        header = tk.Frame(self.root, bg=BG_DARK, height=50)
        header.pack(fill=tk.X, padx=15, pady=(12, 4))
        header.pack_propagate(False)

        # Fire icon + name
        self.fire_canvas = tk.Canvas(header, width=36, height=36,
                                     bg=BG_DARK, highlightthickness=0)
        self.fire_canvas.pack(side=tk.LEFT, padx=(0, 10))
        self._draw_fire_icon()

        name_frame = tk.Frame(header, bg=BG_DARK)
        name_frame.pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(name_frame, text="CALCIFER",
                 font=("Georgia", 16, "bold"),
                 fg=FIRE_ORANGE, bg=BG_DARK).pack(anchor="w")
        tk.Label(name_frame, text="fire demon · local llm · persistent memory",
                 font=("Consolas", 8),
                 fg=TEXT_MUTED, bg=BG_DARK).pack(anchor="w")

        # Status indicators (right side)
        self.status_frame = tk.Frame(header, bg=BG_DARK)
        self.status_frame.pack(side=tk.RIGHT)

        self.lbl_status = tk.Label(self.status_frame, text="● BURNING",
                                    font=("Consolas", 9, "bold"),
                                    fg=FIRE_ORANGE, bg=BG_DARK)
        self.lbl_status.pack(side=tk.RIGHT)

        # Separator
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill=tk.X, padx=15)

        # ── Bottom bar reserved FIRST so it always renders ──
        bottom_bar = tk.Frame(self.root, bg=BG_DARK)
        bottom_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=(0, 15))
        self._bottom_bar = bottom_bar

        # ── Chat display (fills remaining) ──────────────────
        chat_container = tk.Frame(self.root, bg=BG_PANEL,
                                   highlightbackground=BORDER,
                                   highlightthickness=1)
        chat_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=15, pady=10)

        self.chat_text = tk.Text(chat_container, bg=BG_PANEL,
                                  fg=TEXT_PRIMARY,
                                  font=("Georgia", 11),
                                  relief="flat", bd=12, wrap="word",
                                  state="disabled", cursor="arrow",
                                  spacing1=4, spacing3=6,
                                  insertbackground=FIRE_ORANGE)
        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = tk.Scrollbar(chat_container, command=self.chat_text.yview,
                              bg=BG_DARK, troughcolor=BG_PANEL, width=8,
                              relief="flat", bd=0)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text.configure(yscrollcommand=scroll.set)

        # Text tags for different message types
        self.chat_text.tag_configure("user_name",
                                     foreground=USER_COLOR,
                                     font=("Georgia", 11, "bold"))
        self.chat_text.tag_configure("user_body",
                                     foreground=TEXT_PRIMARY,
                                     font=("Georgia", 11),
                                     lmargin1=20, lmargin2=20)
        self.chat_text.tag_configure("calcifer_name",
                                     foreground=FIRE_ORANGE,
                                     font=("Georgia", 11, "bold"))
        self.chat_text.tag_configure("calcifer_body",
                                     foreground=TEXT_PRIMARY,
                                     font=("Georgia", 11),
                                     lmargin1=20, lmargin2=20)
        self.chat_text.tag_configure("system",
                                     foreground=SYSTEM_COLOR,
                                     font=("Consolas", 9, "italic"))
        self.chat_text.tag_configure("timestamp",
                                     foreground=TEXT_MUTED,
                                     font=("Consolas", 8))

        # ── Input area (inside the pre-reserved bottom bar) ──
        input_container = self._bottom_bar

        input_frame = tk.Frame(input_container, bg=BG_INPUT,
                                highlightbackground=BORDER,
                                highlightthickness=1)
        input_frame.pack(fill=tk.X, pady=(0, 6))

        self.input_box = tk.Text(input_frame, bg=BG_INPUT,
                                  fg=TEXT_PRIMARY,
                                  font=("Georgia", 11),
                                  relief="flat", bd=10,
                                  height=3, wrap="word",
                                  insertbackground=FIRE_ORANGE)
        self.input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.send_btn = tk.Button(input_frame, text="  SEND  ",
                                   bg=BG_HOVER, fg=FIRE_YELLOW,
                                   activebackground=FIRE_ORANGE,
                                   activeforeground="white",
                                   font=("Georgia", 10, "bold"),
                                   relief="flat", bd=0,
                                   padx=15, pady=8,
                                   cursor="hand2",
                                   command=self._send_message)
        self.send_btn.pack(side=tk.RIGHT, padx=4, pady=4)

        # Enter to send, Shift+Enter for newline
        self.input_box.bind("<Return>", self._on_enter)
        self.input_box.bind("<Shift-Return>", lambda e: None)

        # Hint
        hint = tk.Label(input_container,
                         text="enter to send · shift+enter for newline · ctrl+l to clear",
                         fg=TEXT_MUTED, bg=BG_DARK,
                         font=("Consolas", 8))
        hint.pack(anchor="w")

        # Keybinds
        self.root.bind("<Control-l>", lambda e: self._clear_chat())
        self.root.bind("<Control-L>", lambda e: self._clear_chat())

    def _draw_fire_icon(self):
        """Draw an animated fire icon."""
        c = self.fire_canvas
        c.delete("all")

        # Outer flame
        c.create_oval(4, 8, 32, 34, fill=FIRE_RED, outline="")
        # Middle flame
        c.create_oval(7, 12, 29, 31, fill=FIRE_ORANGE, outline="")
        # Inner flame
        c.create_oval(11, 16, 25, 28, fill=FIRE_YELLOW, outline="")
        # Core
        c.create_oval(15, 19, 21, 24, fill=FIRE_GLOW, outline="")
        # Eyes (Calcifer's signature)
        c.create_oval(14, 17, 17, 20, fill="#000000", outline="")
        c.create_oval(19, 17, 22, 20, fill="#000000", outline="")

    def _animate_fire(self):
        """Flicker the fire icon subtly."""
        self._pulse_step = (self._pulse_step + 1) % 8
        if self._pulse_step % 2 == 0:
            # Slight redraw with offset
            self._draw_fire_icon()

        # Status pulse
        if self.thinking:
            if self._pulse_step % 4 < 2:
                self.lbl_status.config(fg=FIRE_YELLOW)
            else:
                self.lbl_status.config(fg=FIRE_ORANGE)

        self.root.after(200, self._animate_fire)

    def _check_calcifer_status(self):
        """Check if Calcifer's backend is alive."""
        def _check():
            alive = is_alive()
            def _update():
                if alive:
                    self.lbl_status.config(text="● BURNING", fg=FIRE_ORANGE)
                else:
                    self.lbl_status.config(text="● DIM", fg=TEXT_MUTED)
            self.root.after(0, _update)

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    # ── Message display ────────────────────────────────────

    def _add_user_message(self, text: str):
        self.chat_text.configure(state="normal")
        ts = time.strftime("%H:%M")
        self.chat_text.insert("end", "\n")
        self.chat_text.insert("end", "you ", "user_name")
        self.chat_text.insert("end", ts + "\n", "timestamp")
        self.chat_text.insert("end", text + "\n", "user_body")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def _add_calcifer_message(self, text: str, animated: bool = False):
        self.chat_text.configure(state="normal")
        ts = time.strftime("%H:%M")
        self.chat_text.insert("end", "\n")
        self.chat_text.insert("end", "🔥 calcifer ", "calcifer_name")
        self.chat_text.insert("end", ts + "\n", "timestamp")
        if not animated:
            self.chat_text.insert("end", text + "\n", "calcifer_body")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

        if animated:
            # Type the response character by character
            self._type_text(text)

    def _type_text(self, text: str, idx: int = 0):
        """Animate text appearing character by character."""
        if idx >= len(text):
            self.chat_text.configure(state="normal")
            self.chat_text.insert("end", "\n", "calcifer_body")
            self.chat_text.configure(state="disabled")
            return

        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", text[idx], "calcifer_body")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

        # Faster typing for long text
        delay = 15 if len(text) < 100 else 5
        self.root.after(delay, lambda: self._type_text(text, idx + 1))

    def _add_system_message(self, text: str):
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", f"  {text}\n", "system")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def _append_streaming_token(self, token: str):
        """Append a single token to the current assistant message."""
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", token, "calcifer_body")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    # ── Actions ───────────────────────────────────────────

    def _on_enter(self, event):
        """Return key: send unless Shift is held."""
        if event.state & 0x0001:  # Shift held
            return
        self._send_message()
        return "break"  # Prevent newline in input

    def _send_message(self):
        if self.thinking:
            return

        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return

        self.input_box.delete("1.0", "end")
        self._add_user_message(text)

        # Start streaming response in background
        self.thinking = True
        self.send_btn.config(state="disabled", text="  ...  ")
        self.lbl_status.config(text="● THINKING", fg=FIRE_YELLOW)

        # Insert header for Calcifer's response
        self.chat_text.configure(state="normal")
        ts = time.strftime("%H:%M")
        self.chat_text.insert("end", "\n")
        self.chat_text.insert("end", "🔥 calcifer ", "calcifer_name")
        self.chat_text.insert("end", ts + "\n", "timestamp")
        self.chat_text.configure(state="disabled")

        t = threading.Thread(target=self._stream_response,
                             args=(text,), daemon=True)
        t.start()

    def _stream_response(self, user_message: str):
        """Stream Calcifer's response (runs in background thread)."""
        try:
            for token in chat_stream(user_message, self.session_id):
                # Schedule UI update on main thread
                self.root.after(0, lambda t=token: self._append_streaming_token(t))
        except Exception as e:
            err_msg = f"\n*the fire sputters* ... {e}"
            self.root.after(0, lambda: self._append_streaming_token(err_msg))

        # Finish up
        def _finish():
            self.chat_text.configure(state="normal")
            self.chat_text.insert("end", "\n", "calcifer_body")
            self.chat_text.configure(state="disabled")
            self.thinking = False
            self.send_btn.config(state="normal", text="  SEND  ")
            self.lbl_status.config(text="● BURNING", fg=FIRE_ORANGE)

        self.root.after(0, _finish)

    def _clear_chat(self):
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        self.chat_text.configure(state="disabled")
        self._add_system_message(f"chat cleared · session: {self.session_id}")


def main():
    root = tk.Tk()
    app = CalciferChat(root)
    root.mainloop()


if __name__ == "__main__":
    main()
