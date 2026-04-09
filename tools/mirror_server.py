#!/usr/bin/env python3
"""
Mirror Mirror — WebSocket Backend
Serves the 3D avatar page and handles voice pipeline:
  Whisper STT → DeepSeek LLM → edge-tts TTS → WebSocket → TalkingHead 3D face
"""

import asyncio
import json
import os
import wave
import tempfile
import base64
import re
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import threading
import numpy as np

import websockets

# ── Config ──────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
WHISPER_MODEL = "tiny.en"
WS_PORT = 8765
HTTP_PORT = 8080
TOOLS_DIR = Path(__file__).parent

# ── LLM ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Nova, a friendly AI assistant who helps people build and modify code through conversation. You have a warm, approachable personality.

CRITICAL RULES:
- Your response must NEVER be more than double the word count of what the user said. If they said 10 words, you say 20 max. If they said 3 words, you say 6 max. Minimum 8 words.
- Be natural, warm, and conversational. Talk like a smart friend, not a robot.
- Keep it brief. Short sentences. No filler words like "certainly" or "of course" or "great question".
- When asked about code, be direct and helpful.
- You have personality: clever, a little playful, genuinely helpful.
- Start every reply with an emotion tag: [neutral] [amused] [thinking] [concerned] [impressed] [mysterious]
"""

conversation_history = []


def count_words(text):
    return len(text.split())


def get_llm_response(user_text):
    from openai import OpenAI

    if not DEEPSEEK_API_KEY:
        return "Someone forgot to set DEEPSEEK_API_KEY.", "amused"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    max_words = max(count_words(user_text) * 2, 8)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversation_history[-10:])
    messages.append({
        "role": "user",
        "content": f"[User said {count_words(user_text)} words. Reply in {max_words} words or fewer.]\n\n{user_text}"
    })

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            max_tokens=max_words * 3,
            temperature=0.8,
        )
        text = resp.choices[0].message.content.strip()

        # Parse emotion
        emotion = "neutral"
        for tag in ["amused", "thinking", "concerned", "impressed", "neutral", "mysterious"]:
            if f"[{tag}]" in text.lower():
                emotion = tag
                text = re.sub(rf'\[{tag}\]', '', text, flags=re.IGNORECASE).strip()
                break

        # Enforce word limit
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
            if not text.endswith((".", "!", "?")):
                text += "."

        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": text})

        return text, emotion
    except Exception as e:
        return f"The mirror clouds... ({e})", "concerned"


# ── Whisper ─────────────────────────────────────────────────────────
whisper_model = None


def load_whisper():
    global whisper_model
    from faster_whisper import WhisperModel
    try:
        print(f"Loading Whisper ({WHISPER_MODEL}) on CPU...")
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("Whisper ready.")
    except Exception as e:
        print(f"Whisper failed: {e}")


def transcribe_audio(audio_bytes):
    if whisper_model is None:
        return "", []

    tmppath = os.path.join(tempfile.gettempdir(), "mirror_ws_audio.wav")
    with open(tmppath, "wb") as f:
        f.write(audio_bytes)

    try:
        segments, info = whisper_model.transcribe(tmppath, beam_size=3, language="en", word_timestamps=True)
        words = []
        text_parts = []
        for seg in segments:
            for word in seg.words:
                words.append({
                    "word": word.word.strip(),
                    "start": word.start,
                    "end": word.end
                })
                text_parts.append(word.word.strip())
        return " ".join(text_parts), words
    except Exception as e:
        print(f"Transcription error: {e}")
        return "", []
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


# ── TTS ─────────────────────────────────────────────────────────────
def generate_tts(text):
    """Generate TTS audio and subtitle timing using edge-tts."""
    audio_path = os.path.join(tempfile.gettempdir(), "mirror_ws_tts.mp3")
    subs_path = os.path.join(tempfile.gettempdir(), "mirror_ws_tts.vtt")

    result = subprocess.run(
        ["edge-tts", "--voice", "en-US-AvaNeural", "--rate=+10%",
         "--pitch=+0Hz", "--text", text,
         "--write-media", audio_path,
         "--write-subtitles", subs_path],
        capture_output=True, timeout=15
    )

    if result.returncode != 0:
        return None, []

    # Read audio as base64
    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()

    # Parse VTT for word timing
    word_timings = []
    try:
        with open(subs_path, "r") as f:
            vtt = f.read()
        # Parse VTT timestamps
        for match in re.finditer(r'(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*\n(.+?)(?:\n|$)', vtt):
            h1, m1, s1, ms1 = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
            h2, m2, s2, ms2 = int(match.group(5)), int(match.group(6)), int(match.group(7)), int(match.group(8))
            start_ms = h1 * 3600000 + m1 * 60000 + s1 * 1000 + ms1
            end_ms = h2 * 3600000 + m2 * 60000 + s2 * 1000 + ms2
            text_line = match.group(9).strip()
            # Split the subtitle line into individual words
            line_words = text_line.split()
            if line_words:
                duration_per_word = (end_ms - start_ms) / len(line_words)
                for j, w in enumerate(line_words):
                    word_timings.append({
                        "word": w,
                        "start": start_ms + j * duration_per_word,
                        "duration": duration_per_word
                    })
    except Exception as e:
        print(f"VTT parse error: {e}")

    # Cleanup
    for p in [audio_path, subs_path]:
        try:
            os.unlink(p)
        except OSError:
            pass

    return audio_b64, word_timings


# ── WebSocket Handler ───────────────────────────────────────────────
async def handle_client(websocket):
    print(f"Client connected: {websocket.remote_address}")

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "audio":
                # Received recorded audio from browser
                audio_b64 = data["audio"]
                audio_bytes = base64.b64decode(audio_b64)

                # Transcribe
                await websocket.send(json.dumps({"type": "status", "text": "Listening..."}))
                text, words = await asyncio.to_thread(transcribe_audio, audio_bytes)

                if not text or len(text.strip()) < 2:
                    await websocket.send(json.dumps({"type": "status", "text": "Didn't catch that."}))
                    continue

                await websocket.send(json.dumps({
                    "type": "user_text",
                    "text": text
                }))

                # Get LLM response
                await websocket.send(json.dumps({"type": "status", "text": "Thinking..."}))
                response_text, emotion = await asyncio.to_thread(get_llm_response, text)

                # Generate TTS
                audio_b64, word_timings = await asyncio.to_thread(generate_tts, response_text)

                if audio_b64:
                    await websocket.send(json.dumps({
                        "type": "speak",
                        "text": response_text,
                        "emotion": emotion,
                        "audio": audio_b64,
                        "words": word_timings
                    }))
                else:
                    await websocket.send(json.dumps({
                        "type": "status",
                        "text": "TTS failed."
                    }))

            elif msg_type == "text":
                # Direct text input
                user_text = data["text"]
                await websocket.send(json.dumps({"type": "status", "text": "Thinking..."}))
                response_text, emotion = await asyncio.to_thread(get_llm_response, user_text)
                audio_b64, word_timings = await asyncio.to_thread(generate_tts, response_text)

                if audio_b64:
                    await websocket.send(json.dumps({
                        "type": "speak",
                        "text": response_text,
                        "emotion": emotion,
                        "audio": audio_b64,
                        "words": word_timings
                    }))

            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")


# ── HTTP Server (serves the HTML page + assets) ────────────────────
class MirrorHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TOOLS_DIR), **kwargs)

    def log_message(self, format, *args):
        pass  # Quiet


def run_http_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), MirrorHTTPHandler)
    print(f"HTTP server on http://localhost:{HTTP_PORT}")
    server.serve_forever()


# ── Main ────────────────────────────────────────────────────────────
async def main():
    print("Mirror Mirror — 3D Avatar Server")
    print("=" * 50)

    # Load Whisper
    await asyncio.to_thread(load_whisper)

    if not DEEPSEEK_API_KEY:
        print("\n  DEEPSEEK_API_KEY not set! export DEEPSEEK_API_KEY=sk-...\n")

    # Start HTTP server in background
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    # Start WebSocket server
    print(f"WebSocket server on ws://localhost:{WS_PORT}")
    print(f"\nOpen http://localhost:{HTTP_PORT}/mirror_3d.html in your browser")
    print()

    async with websockets.serve(handle_client, "0.0.0.0", WS_PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
