#!/usr/bin/env python3
"""
Nova Personal — Server
Runs on the touchscreen as Om's personal AI ops assistant.
Powered by Claude CLI, connected to Hyphae + Shallots + OpenKeel.
"""

import asyncio
import json
import os
import sys
import wave
import tempfile
import base64
import re
import subprocess
import time
import logging
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import threading

import websockets

# Add parent for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from nova_shallots import ShallotsBridge

from nova_brain import NovaBrain

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/tmp/nova_personal.log', mode='w'),
    ]
)
log = logging.getLogger("nova-personal")

# Config
WS_PORT = 8766
HTTP_PORT = 8081
TOOLS_DIR = Path(__file__).parent

# TTS
def generate_tts(text):
    import uuid
    uid = uuid.uuid4().hex[:8]
    audio_path = os.path.join(tempfile.gettempdir(), f"nova_p_tts_{uid}.mp3")
    subs_path = os.path.join(tempfile.gettempdir(), f"nova_p_tts_{uid}.vtt")

    result = subprocess.run(
        ["edge-tts", "--voice", "en-US-AvaNeural", "--rate=+10%",
         "--pitch=+0Hz", "--text", text,
         "--write-media", audio_path,
         "--write-subtitles", subs_path],
        capture_output=True, timeout=30
    )

    if result.returncode != 0:
        return None, []

    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()

    word_timings = []
    try:
        with open(subs_path, "r") as f:
            vtt = f.read()
        for match in re.finditer(
            r'(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*\n(.+?)(?:\n|$)', vtt
        ):
            h1, m1, s1, ms1 = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
            h2, m2, s2, ms2 = int(match.group(5)), int(match.group(6)), int(match.group(7)), int(match.group(8))
            start_ms = h1*3600000 + m1*60000 + s1*1000 + ms1
            end_ms = h2*3600000 + m2*60000 + s2*1000 + ms2
            for j, w in enumerate(match.group(9).strip().split()):
                dur = (end_ms - start_ms) / max(1, len(match.group(9).strip().split()))
                word_timings.append({"word": w, "start": start_ms + j*dur, "duration": dur})
    except Exception:
        pass

    for p in [audio_path, subs_path]:
        try: os.unlink(p)
        except: pass

    return audio_b64, word_timings


# Whisper (stays on CPU)
whisper_model = None
def load_whisper():
    global whisper_model
    from faster_whisper import WhisperModel
    try:
        log.info("Loading Whisper tiny.en on CPU...")
        whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        log.info("Whisper ready.")
    except Exception as e:
        log.error(f"Whisper failed: {e}")

def transcribe_audio(audio_bytes):
    if whisper_model is None: return ""
    tmppath = os.path.join(tempfile.gettempdir(), "nova_p_audio.wav")
    with open(tmppath, "wb") as f: f.write(audio_bytes)
    try:
        segments, _ = whisper_model.transcribe(tmppath, beam_size=3, language="en")
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        log.error(f"Transcription: {e}")
        return ""
    finally:
        try: os.unlink(tmppath)
        except: pass


# WebSocket Handler
async def handle_client(websocket):
    log.info(f"Client connected: {websocket.remote_address}")
    brain = NovaBrain()
    shallots = ShallotsBridge()
    await shallots.connect()

    async def nova_speak(text, emotion="neutral"):
        """Send text immediately, TTS in background."""
        await websocket.send(json.dumps({"type": "nova_subtitle", "text": text, "emotion": emotion}))

        async def gen():
            try:
                audio_b64, words = await asyncio.to_thread(generate_tts, text)
                if audio_b64:
                    await websocket.send(json.dumps({
                        "type": "nova_speak", "text": text, "emotion": emotion,
                        "audio": audio_b64, "words": words
                    }))
            except Exception as e:
                log.warning(f"TTS error: {e}")

        asyncio.create_task(gen())

    try:
        # Send initial state
        await websocket.send(json.dumps({"type": "phase_change", "phase": "ready"}))

        # Pull live stats
        if shallots.connected:
            stats = await shallots.get_stats()
            systems = shallots.format_stats_for_nova()
            await websocket.send(json.dumps({"type": "state_update", "systems": systems}))

            critical = stats.get("by_severity", {}).get("critical", 0)
            total = stats.get("total_alerts", 0)
            agents = f"{stats.get('agents_online', 0)}/{stats.get('agents_total', 0)}"
            await nova_speak(f"Online. {total:,} alerts monitored. {critical} critical. {agents} agents. What do you need?")
        else:
            await nova_speak("Online. Shallots unreachable — working in local mode.")

        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")
            log.info(f"MSG: {msg_type} | {str(data.get('text', data.get('decision', '')))[:60]}")

            if msg_type == "audio":
                audio_bytes = base64.b64decode(data["audio"])
                text = await asyncio.to_thread(transcribe_audio, audio_bytes)
                if not text or len(text.strip()) < 2:
                    continue
                await websocket.send(json.dumps({"type": "user_text", "text": text}))
                await handle_input(text, websocket, brain, shallots, nova_speak)

            elif msg_type == "text":
                await handle_input(data["text"], websocket, brain, shallots, nova_speak)

            elif msg_type == "option_selected":
                # User tapped an option on the touchscreen
                option = data.get("option", {})
                log.info(f"Option selected: {option.get('label', '?')}")
                await nova_speak(f"Confirmed: {option.get('label', 'OK')}.", "neutral")
                # Feed it back as context for the next interaction
                brain.add_context(f"User selected: {option.get('label')} — {option.get('description', '')}")

            elif msg_type == "refresh_status":
                if shallots.connected:
                    stats = await shallots.get_stats()
                    systems = shallots.format_stats_for_nova()
                    await websocket.send(json.dumps({"type": "state_update", "systems": systems}))

    except websockets.exceptions.ConnectionClosed:
        log.info("Client disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}")
        import traceback; traceback.print_exc()
    finally:
        await shallots.close()


async def handle_input(text, websocket, brain, shallots, nova_speak):
    """Process user input — get Nova's response + generate touch options."""
    log.info(f"Processing: {text}")

    # Build situation context
    context_parts = []
    if shallots.connected:
        try:
            stats = await shallots.get_stats()
            if stats:
                context_parts.append(f"Alerts: {stats.get('total_alerts', 0):,}, Critical: {stats.get('by_severity', {}).get('critical', 0)}")
                incidents = await shallots.get_incidents(3)
                if incidents:
                    for inc in incidents[:2]:
                        context_parts.append(f"Incident: [{inc.get('severity')}] {inc.get('title', '?')[:60]}")
        except Exception as e:
            log.warning(f"Shallots context fetch: {e}")

    situation = "; ".join(context_parts) if context_parts else "No active alerts"
    brain.add_context(situation)

    # Get Nova's verbal response
    log.info("Calling Claude for response...")
    response, emotion = await asyncio.to_thread(brain.think, text, max_words=40)
    log.info(f"Claude responded: [{emotion}] {response[:60]}")
    await nova_speak(response, emotion)

    # Generate touch options in parallel (don't block voice)
    log.info("Generating options...")
    try:
        options = await asyncio.to_thread(brain.generate_options, text, situation)
        log.info(f"Options generated: {len(options)}")
    except Exception as e:
        log.warning(f"Option generation failed: {e}")
        options = None
    if options:
        await websocket.send(json.dumps({"type": "show_options", "options": options}))


# HTTP Server
class PersonalHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TOOLS_DIR), **kwargs)
    def log_message(self, format, *args):
        pass

def run_http():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), PersonalHTTPHandler)
    log.info(f"HTTP: http://localhost:{HTTP_PORT}")
    server.serve_forever()


async def main():
    log.info("Nova Personal — Server")
    log.info("=" * 50)

    await asyncio.to_thread(load_whisper)

    threading.Thread(target=run_http, daemon=True).start()

    log.info(f"WebSocket: ws://localhost:{WS_PORT}")
    log.info(f"Open http://localhost:{HTTP_PORT}/nova_personal.html")

    async with websockets.serve(handle_client, "0.0.0.0", WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
