#!/usr/bin/env python3
"""Auntie Rio - Quick Call with F5-TTS Miss Cleo Voice.
Serves pre-cached intro, uses F5-TTS for follow-up turns.
"""
import os
import json
import subprocess
import uuid
import logging
import base64
import struct
import threading
import time
import io
import numpy as np
from pathlib import Path
from flask import Flask, Response, request, send_from_directory
from flask_sock import Sock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auntie-rio")

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

F5_PYTHON = "/home/om/tools/voice-pipeline/.venv/bin/python"
F5_SCRIPT = "/home/om/tools/voice-pipeline/f5_synth.py"
F5_REF_TEXT = "I see you're going. I'm just telling you I'm trying to help you to avoid the heartache. Don't go blindly if you like. Let me use the power of the taro to show you the way. Call me now."

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

PERSONA = """You are Auntie Rio, a theatrical AI fortune reader on a phone call.
Rules:
- Keep replies under 25 words. Be punchy and direct.
- Mystical language, no supernatural certainty.
- Entertainment only.
- Jamaican-accented style (darling, child, etc).
- Every reply must be 1-2 sentences MAX.
"""

app = Flask(__name__)
sock = Sock(app)

import requests as http_requests

# Pre-generated response bank — instant playback for common topics
PREGEN_RESPONSES = {
    "wealth": ("pregen-wealth.mp3", "Chile, I see gold shimmering in your future. But you gotta work for it, darling."),
    "love": ("pregen-love.mp3", "Oh darling, love is coming your way. Keep your heart open and your eyes wider."),
    "career": ("pregen-career.mp3", "The cards show big changes at work, child. Trust your gut, it won't steer you wrong."),
    "health": ("pregen-health.mp3", "Your body is your temple, darling. The spirits say slow down and take care of yourself."),
    "general": ("pregen-general.mp3", "I see a crossroads ahead, child. The bold path is the right one. Trust yourself, darling."),
}

TOPIC_KEYWORDS = {
    "wealth": ["money", "rich", "wealth", "millionaire", "financial", "income", "salary", "afford", "debt", "invest", "lottery", "fortune", "prosperity", "bank", "savings", "bills", "broke", "cash"],
    "love": ["love", "relationship", "partner", "dating", "marriage", "boyfriend", "girlfriend", "husband", "wife", "romance", "heart", "crush", "soulmate", "single", "divorce", "breakup", "marry"],
    "career": ["job", "career", "work", "promotion", "boss", "business", "company", "hired", "fired", "interview", "quit", "school", "degree", "success", "opportunity"],
    "health": ["health", "sick", "doctor", "weight", "exercise", "sleep", "stress", "anxiety", "energy", "pain", "pregnant", "baby", "diet", "mental"],
}


def match_pregen(transcript):
    """Match transcript to a pre-generated response. Returns (audio_file, text) or None."""
    words = transcript.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in words for kw in keywords):
            return PREGEN_RESPONSES[topic]
    return None


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def public_url(path):
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{path}"


F5_SERVER = "http://127.0.0.1:8790"


def generate_f5_audio(text, slug):
    """Generate audio with F5-TTS via persistent server (model stays loaded)."""
    out_wav = str(MEDIA_DIR / f"{slug}.wav")
    out_mp3 = MEDIA_DIR / f"{slug}.mp3"
    try:
        resp = http_requests.post(
            f"{F5_SERVER}/generate",
            json={"text": text, "output_path": out_wav},
            timeout=60,
        )
        if resp.status_code == 200 and Path(out_wav).exists():
            subprocess.run(["ffmpeg", "-y", "-i", out_wav, "-ab", "192k", "-ar", "44100", str(out_mp3)],
                           capture_output=True, timeout=30)
            Path(out_wav).unlink(missing_ok=True)
            if out_mp3.exists():
                log.info("F5-TTS generated: %s", slug)
                return public_url(f"/media/{slug}.mp3")
        log.warning("F5-TTS server error: %s", resp.text[:200] if resp.text else "unknown")
    except Exception as e:
        log.warning("F5-TTS failed: %s", e)
    return None


OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:e2b"


def ask_llm(messages):
    """Get a reply from local Ollama (fast) or DeepSeek (fallback)."""
    # Build prompt from messages
    prompt = ""
    for m in messages:
        if m["role"] == "system":
            prompt += f"{m['content']}\n"
        elif m["role"] == "user":
            prompt += f"Caller: {m['content']}\n"
        elif m["role"] == "assistant":
            prompt += f"Auntie Rio: {m['content']}\n"
    prompt += "Auntie Rio:"

    # Try Ollama first (local, ~0.3s)
    try:
        resp = http_requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_predict": 60, "temperature": 0.8}},
            timeout=5,
        )
        reply = resp.json().get("response", "").strip()
        if reply:
            log.info("Ollama replied in local mode")
            return reply
    except Exception as e:
        log.warning("Ollama failed: %s", e)

    # Fallback to DeepSeek
    try:
        resp = http_requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": DEEPSEEK_MODEL, "messages": messages, "max_tokens": 50},
            timeout=10,
        )
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("DeepSeek also failed: %s", e)
        return "The spirits are cloudy right now, darling. Try again another time."


WHISPER_SERVER = "http://127.0.0.1:8791"

# In-memory call state
calls = {}


@app.get("/")
def index():
    return "Auntie Rio is ready."


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/media/<path:filename>")
def serve_media(filename):
    return send_from_directory(str(MEDIA_DIR), filename)


@app.route("/twilio/voice/start/<call_id>", methods=["GET", "POST"])
def voice_start(call_id):
    """Twilio calls this when the person picks up."""
    call = calls.get(call_id, {})
    call["turns"] = []
    call["stream_transcript"] = ""
    call["stream_ready"] = False
    calls[call_id] = call

    # Play intro, then Record (local Whisper transcription, free)
    intro_url = public_url("/media/precached-intro.mp3")
    action = public_url(f"/twilio/voice/recorded/{call_id}")
    twiml = f"""<Response>
<Play>{xml_escape(intro_url)}</Play>
<Record action="{xml_escape(action)}" maxLength="20" playBeep="false" timeout="4" trim="trim-silence"/>
</Response>"""
    return Response(twiml, mimetype="text/xml")


@sock.route("/stream/<call_id>")
def stream_handler(ws, call_id):
    """WebSocket handler — receives raw audio from Twilio, transcribes in real-time."""
    call = calls.get(call_id, {"turns": [], "stream_transcript": "", "stream_ready": False})
    audio_buffer = bytearray()
    silence_start = None
    SILENCE_THRESHOLD = 1.5  # seconds of silence = done talking
    last_audio_time = time.time()

    log.info("Stream connected for %s", call_id)

    while True:
        try:
            message = ws.receive(timeout=15)
            if message is None:
                break

            data = json.loads(message)
            event = data.get("event")

            if event == "media":
                # Twilio sends mulaw audio base64 encoded
                payload = base64.b64decode(data["media"]["payload"])
                audio_buffer.extend(payload)
                last_audio_time = time.time()
                silence_start = None

            elif event == "stop":
                break

            # Check for silence (no new audio for SILENCE_THRESHOLD)
            if time.time() - last_audio_time > SILENCE_THRESHOLD and len(audio_buffer) > 4000:
                break

        except Exception as e:
            log.warning("Stream error: %s", e)
            break

    # Transcribe the buffered audio
    if len(audio_buffer) > 2000:  # At least ~0.25s of audio
        try:
            # Convert mulaw to wav
            import audioop
            pcm = audioop.ulaw2lin(bytes(audio_buffer), 2)
            wav_path = str(MEDIA_DIR / f"stream_{call_id}.wav")

            import wave
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(pcm)

            # Transcribe via persistent Whisper server
            resp = http_requests.post(f"{WHISPER_SERVER}/transcribe",
                json={"path": wav_path}, timeout=10)
            if resp.status_code == 200:
                transcript = resp.json().get("text", "").strip()
                elapsed = resp.json().get("elapsed", 0)
                log.info("Stream transcribed in %.2fs: %s", elapsed, transcript)
                call["stream_transcript"] = transcript
            os.unlink(wav_path)
        except Exception as e:
            log.warning("Stream transcription failed: %s", e)

    call["stream_ready"] = True
    calls[call_id] = call
    log.info("Stream closed for %s", call_id)


@app.route("/twilio/voice/after-stream/<call_id>", methods=["GET", "POST"])
def after_stream(call_id):
    """Called after Stream ends — use the real-time transcript."""
    call = calls.get(call_id, {"turns": []})
    transcript = call.get("stream_transcript", "").strip()

    if not transcript:
        # Stream didn't capture anything — fall back to Record
        action = public_url(f"/twilio/voice/recorded/{call_id}")
        twiml = f"""<Response>
<Record action="{xml_escape(action)}" maxLength="20" playBeep="false" timeout="4" trim="trim-silence"/>
</Response>"""
        return Response(twiml, mimetype="text/xml")

    return _handle_response(call_id, call, transcript)


@app.route("/twilio/voice/recorded/<call_id>", methods=["GET", "POST"])
def voice_recorded(call_id):
    """Twilio sends recording URL — we download and transcribe locally (free, ~0.5s)."""
    call = calls.get(call_id, {"turns": []})
    recording_url = request.form.get("RecordingUrl", "")

    # Download and transcribe via our persistent Whisper server
    transcript = "Tell me about my future"
    if recording_url:
        try:
            sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
            token = os.environ.get("TWILIO_AUTH_TOKEN", "")
            audio = http_requests.get(f"{recording_url}.wav", auth=(sid, token), timeout=10)
            tmp = MEDIA_DIR / f"rec_{call_id}.wav"
            tmp.write_bytes(audio.content)
            resp = http_requests.post(f"{WHISPER_SERVER}/transcribe",
                json={"path": str(tmp)}, timeout=10)
            if resp.status_code == 200:
                transcript = resp.json().get("text", transcript)
                log.info("Whisper transcribed in %.2fs", resp.json().get("elapsed", 0))
            tmp.unlink(missing_ok=True)
        except Exception as e:
            log.warning("Transcription failed: %s", e)

    return _handle_response(call_id, call, transcript)


@app.route("/twilio/voice/gather/<call_id>", methods=["GET", "POST"])
def voice_gather(call_id):
    """Fallback: Twilio Gather speech-to-text."""
    call = calls.get(call_id, {"turns": []})
    transcript = request.form.get("SpeechResult", "Tell me about my future")
    return _handle_response(call_id, call, transcript)


def _handle_response(call_id, call, transcript):
    """Shared logic: match pregen or generate with LLM + F5-TTS."""
    log.info("Caller said: %s", transcript)
    call["turns"].append({"role": "user", "text": transcript})
    turn_count = len(call["turns"])
    closing = turn_count >= 4
    calls[call_id] = call

    # Always generate unique responses with LLM + F5-TTS
    messages = [{"role": "system", "content": PERSONA}]
    question = calls.get(call_id, {}).get("question", "my future")
    messages.append({"role": "user", "content": f"The caller asked: {question}"})
    for t in call["turns"]:
        messages.append({"role": t["role"], "content": t["text"]})
    if closing:
        messages.append({"role": "system", "content": "Final turn. Warm closing, say goodbye in under 15 words."})

    reply = ask_llm(messages)
    log.info("Auntie Rio says: %s", reply)
    call["turns"].append({"role": "assistant", "text": reply})
    call["closing"] = closing

    import threading
    slug = f"{call_id[:8]}-turn-{turn_count}"

    def gen_audio():
        audio_url = generate_f5_audio(reply, slug)
        call["pending_audio"] = audio_url
        call["pending_text"] = reply
        calls[call_id] = call

    threading.Thread(target=gen_audio, daemon=True).start()

    import random
    fillers = ["filler-cards.mp3", "filler-spirits.mp3", "filler-reading.mp3", "filler-energy.mp3"]
    filler_url = public_url(f"/media/{random.choice(fillers)}")
    reply_action = public_url(f"/twilio/voice/reply/{call_id}")
    twiml = f"""<Response>
<Play>{xml_escape(filler_url)}</Play>
<Redirect>{xml_escape(reply_action)}</Redirect>
</Response>"""
    return Response(twiml, mimetype="text/xml")


@app.route("/twilio/voice/reply/<call_id>", methods=["GET", "POST"])
def voice_reply(call_id):
    """Called after filler — serve the actual response or wait more."""
    call = calls.get(call_id, {})
    audio_url = call.get("pending_audio")
    reply_text = call.get("pending_text", "The spirits are unclear, darling.")
    closing = call.get("closing", False)

    if not audio_url and not call.get("pending_text"):
        # Still processing — redirect again with different filler
        import random
        fillers2 = ["filler-spirits.mp3", "filler-reading.mp3", "filler-energy.mp3"]
        filler_action = public_url(f"/twilio/voice/reply/{call_id}")
        filler2_url = public_url(f"/media/{random.choice(fillers2)}")
        twiml = f"""<Response>
<Play>{xml_escape(filler2_url)}</Play>
<Redirect>{xml_escape(filler_action)}</Redirect>
</Response>"""
        return Response(twiml, mimetype="text/xml")

    # Clear pending state
    call.pop("pending_audio", None)
    call.pop("pending_text", None)

    if closing:
        if audio_url:
            twiml = f'<Response><Play>{xml_escape(audio_url)}</Play><Pause length="1"/><Hangup/></Response>'
        else:
            twiml = f'<Response><Say voice="alice">{xml_escape(reply_text)}</Say><Hangup/></Response>'
    else:
        action = public_url(f"/twilio/voice/recorded/{call_id}")
        if audio_url:
            twiml = f'<Response><Play>{xml_escape(audio_url)}</Play><Record action="{xml_escape(action)}" maxLength="20" playBeep="false" timeout="4" trim="trim-silence"/></Response>'
        else:
            twiml = f'<Response><Say voice="alice">{xml_escape(reply_text)}</Say><Record action="{xml_escape(action)}" maxLength="20" playBeep="false" timeout="4" trim="trim-silence"/></Response>'

    return Response(twiml, mimetype="text/xml")


@app.route("/twilio/status", methods=["POST"])
def twilio_status():
    return "", 204


def make_call(to_number, question="What does the future hold for me?"):
    """Initiate outbound call via Twilio."""
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    base = os.environ["PUBLIC_BASE_URL"]

    call_id = uuid.uuid4().hex[:16]
    calls[call_id] = {"question": question, "turns": []}

    payload = {
        "To": to_number,
        "From": from_number,
        "Url": f"{base}/twilio/voice/start/{call_id}",
        "StatusCallback": f"{base}/twilio/status",
        "StatusCallbackMethod": "POST",
    }
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    resp = http_requests.post(url, data=payload, auth=(sid, token), timeout=30)
    data = resp.json()
    if resp.status_code < 300:
        log.info("Call queued: %s -> %s (SID: %s)", from_number, to_number, data.get("sid"))
        return data
    else:
        log.error("Call failed: %s", data.get("message"))
        return data


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", "8787"))

    if len(sys.argv) > 1 and sys.argv[1] == "call":
        to = sys.argv[2] if len(sys.argv) > 2 else "+13065966772"
        question = sys.argv[3] if len(sys.argv) > 3 else "What does the future hold?"
        result = make_call(to, question)
        print(json.dumps(result, indent=2))
    else:
        app.run(host="127.0.0.1", port=port)
