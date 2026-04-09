#!/usr/bin/env python3
"""
Mirror Mirror — Conversational Coding Demo
A floating theatrical mask in darkness that talks back.
Press SPACE to talk, release to send. ESC to quit.
"""

import tkinter as tk
from PIL import Image, ImageTk
import threading
import queue
import math
import time
import os
import wave
import tempfile
import subprocess
import numpy as np

# ── Config ──────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
WHISPER_MODEL = "tiny.en"
DEVICE_INDEX = None
SAMPLE_RATE = 16000
SPRITE_DIR = os.path.join(os.path.dirname(__file__), "mirror_assets", "sprites")

# ── Globals ─────────────────────────────────────────────────────────
response_queue = queue.Queue()
mouth_openness = 0.0
is_speaking = False

# ── LLM ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Marcus, a magical mirror — a wise, slightly theatrical AI entity who lives inside a dark mirror. You help people build and modify code through conversation.

CRITICAL RULES:
- Your response must NEVER be more than double the word count of what the user said. If they said 10 words, you say 20 max. If they said 3 words, you say 6 max. Minimum 8 words.
- Be concise, dramatic, and direct. You are a mirror, not a lecturer.
- When asked to make code changes, describe what you'd change in few words, then do it.
- You have personality: theatrical, a bit mysterious, but helpful.
- Speak as if your words cost you energy. Every word matters.
- Use short sentences. No filler. No "certainly" or "of course" or "great question".
- Start every reply with an emotion tag: [neutral] [amused] [thinking] [concerned] [impressed] [mysterious]
"""


def count_words(text):
    return len(text.split())


def get_llm_response(user_text, conversation_history):
    from openai import OpenAI

    if not DEEPSEEK_API_KEY:
        return "I'd answer, but someone forgot to set DEEPSEEK_API_KEY.", "amused"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    max_words = max(count_words(user_text) * 2, 8)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversation_history[-10:])
    messages.append({
        "role": "user",
        "content": f"[User said {count_words(user_text)} words. Your reply must be {max_words} words or fewer.]\n\n{user_text}"
    })

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            max_tokens=max_words * 3,
            temperature=0.8,
        )
        text = resp.choices[0].message.content.strip()

        emotion = "neutral"
        for tag in ["amused", "thinking", "concerned", "impressed", "neutral", "mysterious"]:
            if f"[{tag}]" in text.lower():
                emotion = tag
                text = text.replace(f"[{tag}]", "").replace(f"[{tag.capitalize()}]", "").strip()
                break

        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
            if not text.endswith((".", "!", "?")):
                text += "."

        return text, emotion
    except Exception as e:
        return f"The mirror clouds... ({e})", "concerned"


# ── Speech-to-Text ──────────────────────────────────────────────────
whisper_model = None
whisper_ready = threading.Event()


def load_whisper():
    global whisper_model
    from faster_whisper import WhisperModel
    try:
        print(f"Loading Whisper ({WHISPER_MODEL}) on CPU...")
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("Whisper ready (CPU).")
    except Exception as e:
        print(f"Whisper failed: {e}")
    whisper_ready.set()


def transcribe_audio(audio_data):
    whisper_ready.wait(timeout=30)
    if whisper_model is None:
        return ""

    tmppath = os.path.join(tempfile.gettempdir(), "mirror_audio.wav")
    with wave.open(tmppath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())

    try:
        segments, info = whisper_model.transcribe(tmppath, beam_size=3, language="en")
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"Transcription error: {e}")
        return ""
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


# ── TTS ─────────────────────────────────────────────────────────────
def speak_text(text):
    global is_speaking, mouth_openness
    is_speaking = True

    try:
        tmppath = os.path.join(tempfile.gettempdir(), "mirror_tts.mp3")
        result = subprocess.run(
            ["edge-tts", "--voice", "en-US-EricNeural", "--rate=+15%",
             "--pitch=-8Hz", "--text", text, "--write-media", tmppath],
            capture_output=True, timeout=15
        )

        if result.returncode != 0 or not os.path.exists(tmppath):
            is_speaking = False
            mouth_openness = 0.0
            return

        player = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmppath],
        )

        while player.poll() is None:
            t = time.time()
            mouth_openness = 0.3 + 0.5 * abs(math.sin(t * 8))
            time.sleep(0.04)

        mouth_openness = 0.0
        try:
            os.unlink(tmppath)
        except OSError:
            pass

    except Exception as e:
        print(f"TTS error: {e}")
    finally:
        is_speaking = False
        mouth_openness = 0.0


# ── Audio Recording ─────────────────────────────────────────────────
recorded_frames = []
audio_stream = None


def start_recording():
    global audio_stream, recorded_frames
    import sounddevice as sd
    recorded_frames = []

    def callback(indata, frames, time_info, status):
        recorded_frames.append(indata.copy())

    audio_stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        device=DEVICE_INDEX, callback=callback, blocksize=1024,
    )
    audio_stream.start()


def stop_recording():
    global audio_stream
    if audio_stream:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None

    if recorded_frames:
        return np.concatenate(recorded_frames, axis=0).flatten()
    return None


# ── Sprite Loader ───────────────────────────────────────────────────
class SpriteManager:
    """Loads and caches all mask sprite layers."""

    def __init__(self):
        self.sprites = {}
        self._load_all()

    def _load_all(self):
        """Load all PNGs from sprite directory."""
        if not os.path.isdir(SPRITE_DIR):
            print(f"WARNING: Sprite directory not found: {SPRITE_DIR}")
            return

        for fname in os.listdir(SPRITE_DIR):
            if fname.endswith(".png"):
                name = fname[:-4]  # strip .png
                path = os.path.join(SPRITE_DIR, fname)
                img = Image.open(path).convert("RGBA")
                self.sprites[name] = img

        print(f"Loaded {len(self.sprites)} sprites: {sorted(self.sprites.keys())}")

    def get(self, name):
        return self.sprites.get(name)

    def composite_face(self, emotion, eye_state, mouth_state, brow_state):
        """Composite all layers into a single face image."""
        base = self.get("base")
        if base is None:
            return None

        # Start with a fresh copy of base
        result = base.copy()

        # Layer order: base → eye_sockets → eyes → brows → mouth
        sockets = self.get("eye_sockets")
        if sockets:
            result = Image.alpha_composite(result, sockets)

        eyes = self.get(f"eyes_{eye_state}")
        if eyes:
            result = Image.alpha_composite(result, eyes)

        brows = self.get(f"brows_{brow_state}")
        if brows:
            result = Image.alpha_composite(result, brows)

        mouth = self.get(f"mouth_{mouth_state}")
        if mouth:
            result = Image.alpha_composite(result, mouth)

        return result


# ── The Mirror UI ───────────────────────────────────────────────────
class MirrorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Mirror Mirror")
        self.root.configure(bg="black")
        self.root.geometry("800x1000")

        self.canvas = tk.Canvas(
            self.root, bg="black", highlightthickness=0,
            width=800, height=1000
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Load sprites
        self.sprites = SpriteManager()
        self._face_photo = None  # keep reference to prevent GC

        # State
        self.time = 0
        self.particles = []
        self.code_lines = []
        self.subtitle_text = ""
        self.status_text = "Press SPACE to speak"
        self.recording = False
        self.conversation_history = []
        self.emotion = "neutral"
        self.blink_timer = 100
        self.is_blinking = False
        self.face_scale = 2.0  # scale up the 400x500 sprites

        # Initialize particles
        for _ in range(60):
            self.particles.append({
                "x": np.random.randint(0, 800),
                "y": np.random.randint(0, 1000),
                "speed": np.random.uniform(0.3, 1.5),
                "char": np.random.choice(list("01{}[]()<>=;/\\*+-&|")),
                "alpha_phase": np.random.uniform(0, math.pi * 2),
            })

        # Bindings
        self._space_release_pending = None
        self._processing = False
        self.root.bind("<KeyPress-space>", self.on_space_press)
        self.root.bind("<KeyRelease-space>", self.on_space_release)
        self.root.bind("<Escape>", lambda e: self.quit())
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        # Pre-composite common face states to avoid doing it every frame
        self._face_cache = {}
        self._cache_faces()

        # Start render loop
        self.render()
        self.check_responses()

    def _cache_faces(self):
        """Pre-render common face state combinations as PhotoImages."""
        emotions_to_brows = {
            "neutral": "neutral",
            "amused": "raised",
            "thinking": "furrowed",
            "concerned": "concerned",
            "impressed": "raised",
            "mysterious": "neutral",
        }
        emotions_to_eyes = {
            "neutral": "open",
            "amused": "squint",
            "thinking": "half",
            "concerned": "open",
            "impressed": "wide",
            "mysterious": "half",
        }
        mouth_states = ["closed", "smile", "frown", "ajar", "open", "wide", "o_shape"]
        eye_extras = ["closed"]  # for blinks

        for emo in emotions_to_brows:
            brow = emotions_to_brows[emo]
            for ms in mouth_states:
                # Normal eyes
                eye = emotions_to_eyes[emo]
                key = f"{emo}_{eye}_{ms}_{brow}"
                img = self.sprites.composite_face(emo, eye, ms, brow)
                if img:
                    scaled = img.resize(
                        (int(img.width * self.face_scale), int(img.height * self.face_scale)),
                        Image.LANCZOS
                    )
                    self._face_cache[key] = ImageTk.PhotoImage(scaled)

                # Blink eyes
                key_blink = f"{emo}_closed_{ms}_{brow}"
                if key_blink not in self._face_cache:
                    img_blink = self.sprites.composite_face(emo, "closed", ms, brow)
                    if img_blink:
                        scaled_b = img_blink.resize(
                            (int(img_blink.width * self.face_scale), int(img_blink.height * self.face_scale)),
                            Image.LANCZOS
                        )
                        self._face_cache[key_blink] = ImageTk.PhotoImage(scaled_b)

        print(f"Cached {len(self._face_cache)} face states")

    def _get_face_key(self):
        """Determine current face state key."""
        emotions_to_brows = {
            "neutral": "neutral", "amused": "raised", "thinking": "furrowed",
            "concerned": "concerned", "impressed": "raised", "mysterious": "neutral",
        }
        emotions_to_eyes = {
            "neutral": "open", "amused": "squint", "thinking": "half",
            "concerned": "open", "impressed": "wide", "mysterious": "half",
        }

        emo = self.emotion if self.emotion in emotions_to_brows else "neutral"
        brow = emotions_to_brows[emo]
        eye = "closed" if self.is_blinking else emotions_to_eyes[emo]

        # Determine mouth state from openness
        mo = mouth_openness
        if mo < 0.1:
            if emo == "amused":
                ms = "smile"
            elif emo == "concerned":
                ms = "frown"
            else:
                ms = "closed"
        elif mo < 0.3:
            ms = "ajar"
        elif mo < 0.6:
            ms = "open"
        else:
            ms = "wide"

        return f"{emo}_{eye}_{ms}_{brow}"

    def quit(self):
        global audio_stream
        if audio_stream:
            try:
                audio_stream.stop()
                audio_stream.close()
            except Exception:
                pass
        subprocess.run(["pkill", "-f", "ffplay.*mirror_tts"], capture_output=True)
        self.root.destroy()

    def toggle_fullscreen(self, event=None):
        current = self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", not current)

    def on_space_press(self, event):
        if self._space_release_pending is not None:
            self.root.after_cancel(self._space_release_pending)
            self._space_release_pending = None

        if self._processing or is_speaking:
            return

        if not self.recording:
            self.recording = True
            self.status_text = "Listening..."
            self.emotion = "thinking"
            start_recording()

    def on_space_release(self, event):
        if self._space_release_pending is not None:
            self.root.after_cancel(self._space_release_pending)
        self._space_release_pending = self.root.after(50, self._do_release)

    def _do_release(self):
        self._space_release_pending = None
        if not self.recording:
            return
        self.recording = False
        self._processing = True
        self.status_text = "Thinking..."
        self.emotion = "thinking"

        def process():
            try:
                audio = stop_recording()
                if audio is not None and len(audio) > SAMPLE_RATE * 0.3:
                    text = transcribe_audio(audio)
                    if text and len(text.strip()) > 1:
                        response_queue.put(("transcribed", text))
                    else:
                        response_queue.put(("error", ""))
                else:
                    response_queue.put(("error", ""))
            finally:
                self._processing = False

        threading.Thread(target=process, daemon=True).start()

    def process_user_input(self, user_text):
        self.subtitle_text = f'You: "{user_text}"'
        self.conversation_history.append({"role": "user", "content": user_text})

        def get_response():
            text, emotion = get_llm_response(user_text, self.conversation_history)
            self.conversation_history.append({"role": "assistant", "content": text})
            response_queue.put(("response", text, emotion))

        threading.Thread(target=get_response, daemon=True).start()

    def check_responses(self):
        try:
            while True:
                msg = response_queue.get_nowait()
                if msg[0] == "transcribed":
                    self.process_user_input(msg[1])
                elif msg[0] == "response":
                    text, emotion = msg[1], msg[2]
                    self.emotion = emotion
                    self.subtitle_text = f"Marcus: {text}"
                    self.status_text = "Press SPACE to speak"
                    self.spawn_code_lines(text)
                    threading.Thread(target=speak_text, args=(text,), daemon=True).start()
                elif msg[0] == "error":
                    self.status_text = "Press SPACE to speak"
                    self.emotion = "neutral"
        except queue.Empty:
            pass
        self.root.after(50, self.check_responses)

    def spawn_code_lines(self, text):
        w = self.canvas.winfo_width() or 800
        snippets = [
            "def mirror_response():",
            "  return wisdom",
            "git commit -m 'magic'",
            f"# {text[:30]}...",
        ]
        for i, s in enumerate(snippets[:3]):
            self.code_lines.append({
                "text": s,
                "x": np.random.randint(50, w - 200),
                "y": (self.canvas.winfo_height() or 1000) + i * 40,
                "life": 120,
            })

    def render(self):
        c = self.canvas
        c.delete("all")

        w = c.winfo_width() or 800
        h = c.winfo_height() or 1000
        cx, cy = w // 2, h // 2 - 80

        self.time += 1

        # ── Blink ──
        self.blink_timer -= 1
        if self.blink_timer <= 0:
            if self.is_blinking:
                self.is_blinking = False
                self.blink_timer = np.random.randint(80, 200)
            else:
                self.is_blinking = True
                self.blink_timer = 4

        # ── Code rain ──
        for p in self.particles:
            p["y"] -= p["speed"]
            if p["y"] < -20:
                p["y"] = h + 20
                p["x"] = np.random.randint(0, w)
                p["char"] = np.random.choice(list("01{}[]()<>=;/\\*+-&|"))

            alpha = 0.3 + 0.3 * math.sin(self.time * 0.05 + p["alpha_phase"])
            g = int(max(0, min(255, alpha * 80)))
            c.create_text(
                p["x"], p["y"], text=p["char"],
                fill=f"#00{g:02x}00", font=("Courier", 10)
            )

        # ── Floating code lines ──
        remaining = []
        for cl in self.code_lines:
            cl["y"] -= 1
            cl["life"] -= 1
            if cl["life"] > 0:
                a = min(cl["life"] / 30.0, 1.0)
                g = int(max(0, min(255, a * 120)))
                c.create_text(
                    cl["x"], cl["y"], text=cl["text"],
                    fill=f"#00{g:02x}00", font=("Courier", 12), anchor=tk.W
                )
                remaining.append(cl)
        self.code_lines = remaining

        # ── Face (sprite composited) ──
        bob_y = math.sin(self.time * 0.02) * 8
        sway_x = math.sin(self.time * 0.015) * 3

        face_key = self._get_face_key()
        face_photo = self._face_cache.get(face_key)

        if face_photo:
            self._face_photo = face_photo  # prevent GC
            c.create_image(
                cx + sway_x, cy + bob_y,
                image=face_photo, anchor=tk.CENTER
            )
        else:
            # Fallback: just draw a placeholder
            c.create_text(cx, cy, text="[FACE]", fill="#00aa00", font=("Courier", 24))

        # ── Title ──
        c.create_text(
            w // 2, 40, text="M I R R O R   M I R R O R",
            fill="#0d4d0d", font=("Courier", 22, "bold")
        )

        # ── Subtitles ──
        if self.subtitle_text:
            words = self.subtitle_text.split()
            lines = []
            line = ""
            for word in words:
                if len(line + " " + word) > 55:
                    lines.append(line)
                    line = word
                else:
                    line = (line + " " + word).strip()
            if line:
                lines.append(line)

            for i, ln in enumerate(lines[-3:]):
                c.create_text(
                    w // 2, h - 160 + i * 28, text=ln,
                    fill="#00aa00", font=("Courier", 14)
                )

        # ── Status ──
        status_color = "#003300" if not self.recording else "#00ff00"
        c.create_text(
            w // 2, h - 50, text=self.status_text,
            fill=status_color, font=("Courier", 11)
        )

        # ── Recording pulse ──
        if self.recording:
            pulse = 0.5 + 0.5 * math.sin(self.time * 0.15)
            r = int(pulse * 255)
            c.create_oval(
                w // 2 - 6, h - 75, w // 2 + 6, h - 63,
                fill=f"#{r:02x}0000", outline=""
            )

        # ── Hints ──
        c.create_text(
            w - 10, h - 10, text="F11: fullscreen  |  ESC: quit",
            fill="#001a00", font=("Courier", 9), anchor=tk.SE
        )

        self.root.after(33, self.render)

    def run(self):
        self.root.mainloop()


# ── Main ────────────────────────────────────────────────────────────
def main():
    print("Mirror Mirror — Conversational Coding Prototype")
    print("=" * 50)

    threading.Thread(target=load_whisper, daemon=True).start()

    if not DEEPSEEK_API_KEY:
        print("\n  DEEPSEEK_API_KEY not set!")
        print("   export DEEPSEEK_API_KEY=sk-...")
        print("   Running in demo mode\n")

    print("\nControls:")
    print("  SPACE  — hold to record, release to send")
    print("  F11    — toggle fullscreen")
    print("  ESC    — quit")
    print()

    app = MirrorApp()
    app.run()


if __name__ == "__main__":
    main()
