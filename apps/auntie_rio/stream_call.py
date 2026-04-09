#!/usr/bin/env python3
"""Auntie Rio — Full streaming pipeline.
Twilio WebSocket → Whisper (streaming) → Ollama (streaming) → CosyVoice2/F5 (streaming) → Twilio audio out.
"""
import os
import json
import base64
import time
import uuid
import wave
import io
import struct
import logging
import threading
import audioop
import subprocess
from pathlib import Path

import requests as http_requests
from flask import Flask, Response, request, send_from_directory
from flask_sock import Sock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auntie-rio-stream")

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_SERVER = "http://127.0.0.1:8791"
F5_SERVER = "http://127.0.0.1:8790"
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:e2b"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PERSONA = """You are Auntie Rio, a theatrical Jamaican fortune teller on a phone call.
- Keep replies under 20 words. One sentence only.
- Mystical, warm, punchy. Use "darling" and "child".
- Entertainment only."""

app = Flask(__name__)
sock = Sock(app)
calls = {}


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def public_url(path):
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{path}"


def mulaw_to_wav(mulaw_bytes, sample_rate=8000):
    """Convert mulaw audio bytes to WAV file bytes."""
    pcm = audioop.ulaw2lin(mulaw_bytes, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def wav_to_mulaw(wav_path):
    """Convert WAV file to mulaw bytes for Twilio playback."""
    # First convert to 8kHz mono 16-bit PCM
    pcm_path = wav_path.replace(".wav", "_8k.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", pcm_path],
        capture_output=True, timeout=10,
    )
    with wave.open(pcm_path, "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
    os.unlink(pcm_path)
    return audioop.lin2ulaw(pcm, 2)


def transcribe_audio(audio_bytes):
    """Send audio to Whisper server for transcription."""
    wav_data = mulaw_to_wav(audio_bytes)
    tmp = str(MEDIA_DIR / f"stream_{uuid.uuid4().hex[:8]}.wav")
    with open(tmp, "wb") as f:
        f.write(wav_data)
    try:
        resp = http_requests.post(f"{WHISPER_SERVER}/transcribe",
            json={"path": tmp}, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            log.info("Whisper: %.2fs → %s", result.get("elapsed", 0), result.get("text", ""))
            return result.get("text", "").strip()
    except Exception as e:
        log.warning("Whisper failed: %s", e)
    finally:
        try:
            os.unlink(tmp)
        except:
            pass
    return ""


def stream_ollama(messages):
    """Stream tokens from Ollama, yield partial text as sentences form."""
    prompt = ""
    for m in messages:
        if m["role"] == "system":
            prompt += f"{m['content']}\n"
        elif m["role"] == "user":
            prompt += f"Caller: {m['content']}\n"
        elif m["role"] == "assistant":
            prompt += f"Auntie Rio: {m['content']}\n"
    prompt += "Auntie Rio:"

    try:
        resp = http_requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": True,
                  "options": {"num_predict": 40, "temperature": 0.8}},
            stream=True, timeout=10,
        )
        full_text = ""
        for line in resp.iter_lines():
            if line:
                data = json.loads(line)
                token = data.get("response", "")
                full_text += token
                # Yield when we hit sentence end
                if token.rstrip().endswith((".", "!", "?", ",")):
                    yield full_text.strip()
        if full_text.strip():
            yield full_text.strip()
    except Exception as e:
        log.warning("Ollama streaming failed: %s", e)
        yield "The spirits are unclear right now, darling."


def generate_tts_audio(text):
    """Generate audio via F5-TTS server, return mulaw bytes."""
    out_wav = str(MEDIA_DIR / f"stream_{uuid.uuid4().hex[:8]}.wav")
    try:
        resp = http_requests.post(f"{F5_SERVER}/generate",
            json={"text": text, "output_path": out_wav}, timeout=30)
        if resp.status_code == 200 and os.path.exists(out_wav):
            mulaw = wav_to_mulaw(out_wav)
            os.unlink(out_wav)
            return mulaw
    except Exception as e:
        log.warning("F5-TTS failed: %s", e)
    try:
        os.unlink(out_wav)
    except:
        pass
    return None


def send_audio_to_twilio(ws, mulaw_bytes, stream_sid):
    """Send mulaw audio back to Twilio via WebSocket."""
    # Twilio expects 20ms chunks of mulaw (160 bytes at 8kHz)
    CHUNK_SIZE = 160
    for i in range(0, len(mulaw_bytes), CHUNK_SIZE):
        chunk = mulaw_bytes[i:i + CHUNK_SIZE]
        if len(chunk) < CHUNK_SIZE:
            chunk += b'\xff' * (CHUNK_SIZE - len(chunk))  # pad with silence
        payload = base64.b64encode(chunk).decode("ascii")
        msg = json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload}
        })
        try:
            ws.send(msg)
        except:
            break
        time.sleep(0.018)  # ~20ms pacing


# Pre-generated response matching
TOPIC_KEYWORDS = {
    "wealth": ["money", "rich", "wealth", "millionaire", "financial", "lottery", "fortune", "broke", "cash", "invest"],
    "love": ["love", "relationship", "partner", "dating", "marriage", "soulmate", "boyfriend", "girlfriend", "crush"],
    "career": ["job", "career", "work", "promotion", "boss", "business", "success", "quit", "opportunity"],
    "health": ["health", "sick", "doctor", "stress", "anxiety", "sleep", "pregnant", "diet"],
}
PREGEN_AUDIO = {}  # Loaded at startup


def load_pregen():
    """Load pre-generated mulaw audio for instant responses."""
    for topic in ["wealth", "love", "career", "health", "general"]:
        mp3 = MEDIA_DIR / f"pregen-{topic}.mp3"
        if mp3.exists():
            wav = str(MEDIA_DIR / f"pregen-{topic}-8k.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3), "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", wav],
                capture_output=True, timeout=10,
            )
            if os.path.exists(wav):
                with wave.open(wav, "rb") as wf:
                    pcm = wf.readframes(wf.getnframes())
                PREGEN_AUDIO[topic] = audioop.lin2ulaw(pcm, 2)
                os.unlink(wav)
                log.info("Loaded pregen: %s (%d bytes)", topic, len(PREGEN_AUDIO[topic]))

    # Load filler
    for name in ["filler-cards", "filler-spirits", "filler-reading", "filler-energy"]:
        mp3 = MEDIA_DIR / f"{name}.mp3"
        if mp3.exists():
            wav = str(MEDIA_DIR / f"{name}-8k.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3), "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", wav],
                capture_output=True, timeout=10,
            )
            if os.path.exists(wav):
                with wave.open(wav, "rb") as wf:
                    pcm = wf.readframes(wf.getnframes())
                PREGEN_AUDIO[name] = audioop.lin2ulaw(pcm, 2)
                os.unlink(wav)

    # Load intro
    mp3 = MEDIA_DIR / "precached-intro.mp3"
    if mp3.exists():
        wav = str(MEDIA_DIR / "intro-8k.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3), "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", wav],
            capture_output=True, timeout=10,
        )
        if os.path.exists(wav):
            with wave.open(wav, "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
            PREGEN_AUDIO["intro"] = audioop.lin2ulaw(pcm, 2)
            os.unlink(wav)
            log.info("Loaded intro (%d bytes)", len(PREGEN_AUDIO["intro"]))


def match_topic(text):
    words = text.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in words for kw in keywords):
            return topic
    return None


@app.get("/")
def index():
    return "Auntie Rio Streaming"


@app.get("/health")
def health():
    return {"ok": True, "mode": "streaming"}


@app.get("/media/<path:filename>")
def serve_media(filename):
    return send_from_directory(str(MEDIA_DIR), filename)


@app.route("/twilio/voice/start/<call_id>", methods=["GET", "POST"])
def voice_start(call_id):
    """Connect bidirectional stream."""
    calls[call_id] = {"turns": [], "question": ""}
    ws_url = public_url(f"/stream/{call_id}").replace("https://", "wss://")
    twiml = f"""<Response>
<Connect>
<Stream url="{xml_escape(ws_url)}" />
</Connect>
</Response>"""
    return Response(twiml, mimetype="text/xml")


@app.route("/twilio/status", methods=["POST"])
def twilio_status():
    return "", 204


@sock.route("/stream/<call_id>")
def handle_stream(ws, call_id):
    """Bidirectional WebSocket — receive audio, process, send audio back."""
    call = calls.get(call_id, {"turns": []})
    stream_sid = None
    audio_buffer = bytearray()
    last_audio_time = time.time()
    SILENCE_TIMEOUT = 1.8  # seconds
    turn = 0
    MAX_TURNS = 3

    log.info("Stream opened: %s", call_id)

    # Phase 0: Wait for stream start event
    while stream_sid is None:
        try:
            message = ws.receive(timeout=10)
            if message is None:
                return
            data = json.loads(message)
            if data.get("event") == "start":
                stream_sid = data["start"]["streamSid"]
                log.info("Stream SID: %s", stream_sid)
            elif data.get("event") == "stop":
                return
        except:
            return

    # Play intro (blocking — caller hears this first)
    if "intro" in PREGEN_AUDIO:
        log.info("Playing intro")
        send_audio_to_twilio(ws, PREGEN_AUDIO["intro"], stream_sid)

    # Drain any audio that arrived during intro playback
    drain_until = time.time()

    while turn < MAX_TURNS:
        # Phase 1: Listen for caller speech
        audio_buffer = bytearray()
        last_audio_time = time.time()
        listening = True
        got_audio = False

        while listening:
            try:
                message = ws.receive(timeout=SILENCE_TIMEOUT + 1)
                if message is None:
                    return

                data = json.loads(message)
                event = data.get("event")

                if event == "media":
                    payload = base64.b64decode(data["media"]["payload"])
                    # Skip audio that arrived during intro/response playback
                    ts = float(data["media"].get("timestamp", "0")) / 1000.0
                    audio_buffer.extend(payload)
                    last_audio_time = time.time()
                    got_audio = True

                elif event == "stop":
                    return

                # Check for silence after getting some audio
                if got_audio and len(audio_buffer) > 3200 and (time.time() - last_audio_time) > SILENCE_TIMEOUT:
                    listening = False

            except Exception as e:
                if len(audio_buffer) > 1600:
                    listening = False
                else:
                    log.warning("Listen timeout with %d bytes", len(audio_buffer))
                    return

        if len(audio_buffer) < 1600:
            continue

        # Phase 2: Transcribe
        t0 = time.time()
        transcript = transcribe_audio(bytes(audio_buffer))
        log.info("Turn %d transcription (%.2fs): %s", turn, time.time() - t0, transcript)

        if not transcript or len(transcript.strip()) < 3:
            continue

        call["turns"].append({"role": "user", "text": transcript})
        turn += 1
        closing = turn >= MAX_TURNS

        # Phase 3: Check pregen match (instant)
        topic = match_topic(transcript)
        if topic and topic in PREGEN_AUDIO and not closing:
            log.info("INSTANT pregen: %s", topic)
            send_audio_to_twilio(ws, PREGEN_AUDIO[topic], stream_sid)
            call["turns"].append({"role": "assistant", "text": f"[pregen:{topic}]"})
            continue

        # Phase 4: Play filler while we generate in a thread
        import random
        filler_keys = [k for k in PREGEN_AUDIO if k.startswith("filler-")]

        # Start LLM + TTS generation in background
        gen_result = {"mulaw": None, "text": ""}
        messages = [{"role": "system", "content": PERSONA}]
        for t in call["turns"]:
            messages.append({"role": t["role"], "content": t["text"]})
        if closing:
            messages.append({"role": "system", "content": "Final turn. Say goodbye warmly in under 10 words."})

        def generate():
            t1 = time.time()
            full_reply = ""
            for partial in stream_ollama(messages):
                full_reply = partial
            log.info("LLM (%.2fs): %s", time.time() - t1, full_reply)
            gen_result["text"] = full_reply

            t2 = time.time()
            gen_result["mulaw"] = generate_tts_audio(full_reply)
            log.info("TTS (%.2fs)", time.time() - t2)

        gen_thread = threading.Thread(target=generate, daemon=True)
        gen_thread.start()

        # Play filler (blocking) while generation happens in background
        if filler_keys:
            filler = random.choice(filler_keys)
            send_audio_to_twilio(ws, PREGEN_AUDIO[filler], stream_sid)

        # Wait for generation to complete
        gen_thread.join(timeout=20)

        call["turns"].append({"role": "assistant", "text": gen_result["text"]})

        # Play the generated response
        if gen_result["mulaw"]:
            log.info("Playing response (%d bytes)", len(gen_result["mulaw"]))
            send_audio_to_twilio(ws, gen_result["mulaw"], stream_sid)
        else:
            log.warning("TTS failed, no audio")

    # Done — close gracefully
    log.info("Call complete: %d turns", turn)
    calls.pop(call_id, None)


def make_call(to_number):
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    base = os.environ["PUBLIC_BASE_URL"]
    call_id = uuid.uuid4().hex[:16]
    calls[call_id] = {"turns": [], "question": ""}

    payload = {
        "To": to_number, "From": from_number,
        "Url": f"{base}/twilio/voice/start/{call_id}",
        "StatusCallback": f"{base}/twilio/status",
        "StatusCallbackMethod": "POST",
    }
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    resp = http_requests.post(url, data=payload, auth=(sid, token), timeout=30)
    data = resp.json()
    log.info("Call queued: %s (SID: %s)", to_number, data.get("sid"))
    return data


if __name__ == "__main__":
    import sys
    load_pregen()
    port = int(os.environ.get("PORT", "8787"))
    if len(sys.argv) > 1 and sys.argv[1] == "call":
        to = sys.argv[2] if len(sys.argv) > 2 else "+13065966772"
        result = make_call(to)
        print(json.dumps(result, indent=2))
    else:
        app.run(host="127.0.0.1", port=port)
