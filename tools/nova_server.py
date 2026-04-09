#!/usr/bin/env python3
"""
Nova Incident Response — Server
WebSocket backend: voice pipeline + scenario engine + 3D avatar
"""

import asyncio
import json
import os
import wave
import logging
from datetime import datetime

# Set up logging with timestamps
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/tmp/nova_debug.log', mode='w'),
    ]
)
log = logging.getLogger("nova")
import tempfile
import base64
import re
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import threading

import websockets
import numpy as np

from nova_scenario import ScenarioEngine, WORKERS
from nova_live_scenario import LiveScenarioEngine

# ── Config ──────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
WHISPER_MODEL = "tiny.en"
WS_PORT = 8765
HTTP_PORT = 8080
TOOLS_DIR = Path(__file__).parent

# ── LLM ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Nova, a calm and capable AI operations manager. You're managing a live incident response for Oracle Lens's infrastructure.

CRITICAL RULES:
- Your response must NEVER be more than double the word count of what the user said. Minimum 8 words.
- Be clear, confident, and direct. You're in charge during a crisis.
- When the situation is calm, be warm and approachable. When there's an incident, be focused and decisive.
- Explain technical concepts simply — the person you're talking to might not be an engineer.
- You have four workers: Ping (server health), Vault (database/data), Route (network), Scout (external services).
- Start every reply with an emotion tag: [neutral] [amused] [thinking] [concerned] [impressed]
"""

conversation_history = []


def count_words(text):
    return len(text.split())


def get_llm_response(user_text, context=""):
    from openai import OpenAI

    if not DEEPSEEK_API_KEY:
        return "Need DEEPSEEK_API_KEY to respond.", "neutral"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    max_words = max(count_words(user_text) * 2, 8)

    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context}]
    messages.extend(conversation_history[-10:])
    messages.append({
        "role": "user",
        "content": f"[Reply in {max_words} words or fewer.]\n\n{user_text}"
    })

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            max_tokens=max_words * 3,
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()

        emotion = "neutral"
        for tag in ["amused", "thinking", "concerned", "impressed", "neutral"]:
            if f"[{tag}]" in text.lower():
                emotion = tag
                text = re.sub(rf'\[{tag}\]', '', text, flags=re.IGNORECASE).strip()
                break

        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
            if not text.endswith((".", "!", "?")):
                text += "."

        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": text})
        return text, emotion
    except Exception as e:
        return f"Comms error: {e}", "concerned"


# ── Whisper ─────────────────────────────────────────────────────────
whisper_model = None


def load_whisper():
    global whisper_model
    from faster_whisper import WhisperModel
    try:
        log.info(f"Loading Whisper ({WHISPER_MODEL}) on CPU...")
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("Whisper ready.")
    except Exception as e:
        log.info(f"Whisper failed: {e}")


def transcribe_audio(audio_bytes):
    if whisper_model is None:
        return ""
    tmppath = os.path.join(tempfile.gettempdir(), "nova_audio.wav")
    with open(tmppath, "wb") as f:
        f.write(audio_bytes)
    try:
        segments, _ = whisper_model.transcribe(tmppath, beam_size=3, language="en")
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        log.info(f"Transcription error: {e}")
        return ""
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


# ── TTS ─────────────────────────────────────────────────────────────
def generate_tts(text):
    import uuid
    uid = uuid.uuid4().hex[:8]
    audio_path = os.path.join(tempfile.gettempdir(), f"nova_tts_{uid}.mp3")
    subs_path = os.path.join(tempfile.gettempdir(), f"nova_tts_{uid}.vtt")

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
            start_ms = h1 * 3600000 + m1 * 60000 + s1 * 1000 + ms1
            end_ms = h2 * 3600000 + m2 * 60000 + s2 * 1000 + ms2
            line_words = match.group(9).strip().split()
            if line_words:
                dur = (end_ms - start_ms) / len(line_words)
                for j, w in enumerate(line_words):
                    word_timings.append({"word": w, "start": start_ms + j * dur, "duration": dur})
    except Exception as e:
        log.info(f"VTT parse: {e}")

    for p in [audio_path, subs_path]:
        try:
            os.unlink(p)
        except OSError:
            pass

    return audio_b64, word_timings


# ── WebSocket Handler ───────────────────────────────────────────────
async def handle_client(websocket):
    log.info(f"Client connected: {websocket.remote_address}")
    scenario = ScenarioEngine()
    revenue_task = None

    async def send_event(event_type, data):
        """Callback for scenario engine events."""
        if event_type in ("nova_speak", "nova_subtitle", "show_stuck_options", "show_decisions", "hide_stuck_options", "phase_change", "worker_stuck", "score_update"):
            log.info(f"EVENT: {event_type} | {str(data.get('text', data.get('phase', data.get('options', ''))))[:80]}")
        msg = {"type": event_type, **data}

        if event_type == "nova_speak":
            # Send text immediately so subtitle appears instantly
            try:
                await websocket.send(json.dumps({"type": "nova_subtitle", "text": data["text"], "emotion": data.get("emotion", "neutral")}))
            except Exception:
                pass

            # Generate TTS in background, send audio when ready
            async def gen_and_send():
                try:
                    log.info(f"TTS generating for: {data['text'][:50]}...")
                    audio_b64, word_timings = await asyncio.to_thread(generate_tts, data["text"])
                    if audio_b64:
                        log.info(f"TTS done: {len(audio_b64)} chars audio, {len(word_timings)} words. Sending...")
                        audio_msg = {"type": "nova_speak", "text": data["text"],
                                     "emotion": data.get("emotion", "neutral"),
                                     "audio": audio_b64, "words": word_timings}
                        await websocket.send(json.dumps(audio_msg))
                        log.info("TTS sent to client")
                    else:
                        log.info("TTS returned no audio!")
                except (websockets.exceptions.ConnectionClosed, ConnectionError):
                    log.info("TTS send failed — client disconnected (non-fatal)")
                except Exception as e:
                    log.warning(f"TTS background error (non-fatal): {e}")

            asyncio.create_task(gen_and_send())
            return  # Don't send the original msg, we handled it

        try:
            await websocket.send(json.dumps(msg))
        except Exception:
            pass

    scenario.callbacks.append(send_event)

    try:
        # Start calm state
        await scenario.start_calm()

        # Start background tasks
        revenue_task = asyncio.create_task(scenario.revenue_ticker())
        commentary_task = asyncio.create_task(scenario.commentary_engine())

        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")
            log.info(f"CLIENT MSG: {msg_type} | {str(data.get('text', data.get('decision', '')))[:60]}")

            if msg_type == "audio":
                # Voice input
                audio_bytes = base64.b64decode(data["audio"])
                await websocket.send(json.dumps({"type": "status", "text": "Listening..."}))
                text = await asyncio.to_thread(transcribe_audio, audio_bytes)

                if not text or len(text.strip()) < 2:
                    await websocket.send(json.dumps({"type": "status", "text": ""}))
                    continue

                await websocket.send(json.dumps({"type": "user_text", "text": text}))
                await handle_user_input(text, websocket, scenario)

            elif msg_type == "text":
                await handle_user_input(data["text"], websocket, scenario)

            elif msg_type == "start_drill":
                asyncio.create_task(run_drill(websocket, scenario))

            elif msg_type == "start_live":
                # Switch to live security mode
                log.info("Switching to LIVE security mode")
                live = LiveScenarioEngine()
                live.callbacks.append(send_event)
                await live.start_calm()
                # Replace the active scenario
                scenario = live
                asyncio.create_task(live.trigger_alarm())

            elif msg_type == "decision":
                dec = data["decision"]
                # Check if this resolves a waiting_for_human coordination decision
                if scenario.waiting_for_human and scenario.waiting_for_human[0] == "_coordinate" and dec == "coordinate":
                    scenario.waiting_for_human = None
                else:
                    asyncio.create_task(scenario.execute_decision(dec))

            elif msg_type == "reset":
                await scenario.start_calm()
                conversation_history.clear()

            elif msg_type == "client_error":
                log.error(f"FRONTEND ERROR: {data.get('error', '?')} | event: {data.get('event_type', '?')} | line: {data.get('line', '?')}")
                if data.get('stack'):
                    log.error(f"  Stack: {data['stack']}")

            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        log.info("Client disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if revenue_task:
            revenue_task.cancel()
        if commentary_task:
            commentary_task.cancel()


async def handle_user_input(text, websocket, scenario):
    """Process user text based on current scenario phase."""
    text_lower = text.lower().strip()

    # Intro greeting after user taps BEGIN
    if scenario.phase == "calm" and text_lower in ["hello", "hi", "hey"]:
        # Send Nova's intro with audio (AudioContext is now resumed)
        intro = "Hey, I'm Nova. I manage infrastructure for Oracle Lens. Ten systems, four agents on standby. Everything's green. Let's see what happens when things go wrong."
        audio_b64, word_timings = await asyncio.to_thread(generate_tts, intro)
        await websocket.send(json.dumps({
            "type": "nova_speak", "text": intro, "emotion": "amused",
            "audio": audio_b64, "words": word_timings
        }))
        return

    # Check for drill trigger
    if scenario.phase == "calm" and any(w in text_lower for w in ["yes", "yeah", "sure", "ok", "drill", "start", "go", "let's", "ready"]):
        asyncio.create_task(run_drill(websocket, scenario))
        return

    # If a worker is stuck, route input to the stuck handler
    if scenario.waiting_for_human:
        handled = await scenario.handle_human_input(text)
        if handled:
            return

    # Check for decision keywords
    if scenario.phase == "deciding":
        if any(w in text_lower for w in ["kill", "query", "terminate", "stop the query"]):
            asyncio.create_task(scenario.execute_decision("kill_query"))
            return
        elif any(w in text_lower for w in ["restart", "reboot", "server"]):
            asyncio.create_task(scenario.execute_decision("restart_server"))
            return

    # General conversation — use LLM
    context = f"Current phase: {scenario.phase}. Score: {scenario.score}. "
    if scenario.phase == "calm":
        context += "Everything is running smoothly. You can offer to run an incident drill."
    elif scenario.phase in ("alarm", "investigating"):
        context += "There's an active incident. Systems are degrading. Agents are investigating."
        if scenario.waiting_for_human:
            wid, phase = scenario.waiting_for_human
            context += f" Agent {WORKERS[wid]['name']} is STUCK and needs human guidance."
    elif scenario.phase == "deciding":
        context += "All agents reported. Root cause: runaway DB query. Waiting for human to decide: kill the query or restart the server."
    elif scenario.phase == "resolved":
        context += "Incident is resolved. You're in debrief mode."

    response_text, emotion = await asyncio.to_thread(get_llm_response, text, context)
    audio_b64, word_timings = await asyncio.to_thread(generate_tts, response_text)

    await websocket.send(json.dumps({
        "type": "nova_speak",
        "text": response_text,
        "emotion": emotion,
        "audio": audio_b64,
        "words": word_timings
    }))


async def run_drill(websocket, scenario):
    """Run the full incident drill."""
    # Trigger alarm
    await scenario.trigger_alarm()
    # Deploy workers
    await asyncio.sleep(1)
    await scenario.deploy_workers()


# ── HTTP Server ─────────────────────────────────────────────────────
class NovaHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TOOLS_DIR), **kwargs)

    def log_message(self, format, *args):
        pass


def run_http_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), NovaHTTPHandler)
    log.info(f"HTTP: http://localhost:{HTTP_PORT}")
    server.serve_forever()


# ── Main ────────────────────────────────────────────────────────────
async def main():
    log.info("Nova Incident Response — Server")
    log.info("=" * 50)

    await asyncio.to_thread(load_whisper)

    if not DEEPSEEK_API_KEY:
        log.info("  WARNING: DEEPSEEK_API_KEY not set\n")

    threading.Thread(target=run_http_server, daemon=True).start()

    log.info(f"WebSocket: ws://localhost:{WS_PORT}")
    log.info(f"\nOpen http://localhost:{HTTP_PORT}/nova_demo.html")
    print()

    async with websockets.serve(handle_client, "0.0.0.0", WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
