#!/usr/bin/env python3
"""Auntie Rio — Async bidirectional streaming via Quart."""
import os
import json
import base64
import time
import uuid
import wave
import io
import asyncio
import logging
import subprocess
import audioop
from pathlib import Path

import aiohttp
from quart import Quart, Response, request, send_from_directory, websocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auntie-rio")

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_SERVER = "http://127.0.0.1:8791"
F5_SERVER = "http://127.0.0.1:8790"
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:e2b"

PERSONA = """You are Auntie Rio, a theatrical Jamaican fortune teller.
- Under 20 words. One sentence.
- Mystical, warm. Use "darling" and "child".
- Entertainment only."""

app = Quart(__name__)
calls = {}

TOPIC_KEYWORDS = {
    "wealth": ["money", "rich", "wealth", "millionaire", "lottery", "fortune", "broke", "cash"],
    "love": ["love", "relationship", "partner", "dating", "marriage", "soulmate", "crush"],
    "career": ["job", "career", "work", "promotion", "boss", "business", "success"],
    "health": ["health", "sick", "doctor", "stress", "anxiety", "sleep"],
}
PREGEN_AUDIO = {}


def load_pregen():
    for name in ["wealth", "love", "career", "health", "general",
                  "filler-cards", "filler-spirits", "filler-reading", "filler-energy",
                  "precached-intro"]:
        src = f"pregen-{name}" if name in TOPIC_KEYWORDS or name == "general" else name
        mp3 = MEDIA_DIR / f"{src}.mp3"
        if not mp3.exists():
            continue
        wav = str(MEDIA_DIR / f"_tmp_{name}.wav")
        subprocess.run(["ffmpeg", "-y", "-i", str(mp3), "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", wav],
                       capture_output=True, timeout=10)
        if os.path.exists(wav):
            with wave.open(wav, "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
            PREGEN_AUDIO[name] = audioop.lin2ulaw(pcm, 2)
            os.unlink(wav)
            log.info("Loaded: %s (%d bytes)", name, len(PREGEN_AUDIO[name]))


def match_topic(text):
    words = text.lower()
    for topic, kws in TOPIC_KEYWORDS.items():
        if any(k in words for k in kws):
            return topic
    return None


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def public_url(path):
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + path


async def transcribe(mulaw_bytes):
    pcm = audioop.ulaw2lin(mulaw_bytes, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(8000); wf.writeframes(pcm)
    tmp = str(MEDIA_DIR / f"_asr_{uuid.uuid4().hex[:8]}.wav")
    with open(tmp, "wb") as f:
        f.write(buf.getvalue())
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{WHISPER_SERVER}/transcribe", json={"path": tmp},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    return d.get("text", "").strip()
    except Exception as e:
        log.warning("ASR error: %s", e)
    finally:
        try: os.unlink(tmp)
        except: pass
    return ""


async def ask_llm(messages):
    prompt = "\n".join(
        f"{'Caller' if m['role']=='user' else 'System' if m['role']=='system' else 'Auntie Rio'}: {m['content']}"
        for m in messages) + "\nAuntie Rio:"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{OLLAMA_URL}/api/generate",
                              json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                                    "options": {"num_predict": 40, "temperature": 0.8}},
                              timeout=aiohttp.ClientTimeout(total=8)) as r:
                d = await r.json()
                return d.get("response", "").strip() or "The spirits are cloudy, darling."
    except Exception as e:
        log.warning("LLM error: %s", e)
    return "The spirits are cloudy, darling."


async def generate_tts(text):
    out = str(MEDIA_DIR / f"_tts_{uuid.uuid4().hex[:8]}.wav")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{F5_SERVER}/generate", json={"text": text, "output_path": out},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200 and os.path.exists(out):
                    pcm_wav = out.replace(".wav", "_8k.wav")
                    p = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", out, "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", pcm_wav,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await p.wait()
                    if os.path.exists(pcm_wav):
                        with wave.open(pcm_wav, "rb") as wf:
                            pcm = wf.readframes(wf.getnframes())
                        mulaw = audioop.lin2ulaw(pcm, 2)
                        os.unlink(pcm_wav); os.unlink(out)
                        return mulaw
    except Exception as e:
        log.warning("TTS error: %s", e)
    try: os.unlink(out)
    except: pass
    return None


async def play_audio(mulaw_bytes, stream_sid):
    """Send mulaw to Twilio with proper pacing."""
    CHUNK = 160
    for i in range(0, len(mulaw_bytes), CHUNK):
        chunk = mulaw_bytes[i:i + CHUNK]
        if len(chunk) < CHUNK:
            chunk += b'\xff' * (CHUNK - len(chunk))
        msg = json.dumps({
            "event": "media", "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode("ascii")}
        })
        await websocket.send(msg)
        await asyncio.sleep(0.018)


async def collect_speech(timeout=1.8):
    """Collect audio from WebSocket until silence detected."""
    buf = bytearray()
    last = time.time()
    got = False
    while True:
        try:
            raw = await asyncio.wait_for(websocket.receive(), timeout=timeout + 0.5)
            data = json.loads(raw)
            if data.get("event") == "media":
                buf.extend(base64.b64decode(data["media"]["payload"]))
                last = time.time()
                got = True
            elif data.get("event") == "stop":
                return None
        except asyncio.TimeoutError:
            if got and len(buf) > 1600:
                return bytes(buf)
            if not got:
                return None
            continue
        if got and len(buf) > 3200 and (time.time() - last) > timeout:
            return bytes(buf)


# --- Routes ---

@app.get("/")
async def index():
    return "Auntie Rio (async)"

@app.get("/health")
async def health():
    return {"ok": True, "mode": "async"}

@app.get("/media/<path:filename>")
async def serve_media(filename):
    return await send_from_directory(str(MEDIA_DIR), filename)

@app.route("/twilio/voice/start/<call_id>", methods=["GET", "POST"])
async def voice_start(call_id):
    calls[call_id] = {"turns": []}
    ws_url = public_url(f"/stream/{call_id}").replace("https://", "wss://")
    twiml = f'<Response><Connect><Stream url="{xml_escape(ws_url)}" bidirectional="true" /></Connect></Response>'
    return Response(twiml, mimetype="text/xml")

@app.route("/twilio/status", methods=["POST"])
async def twilio_status():
    return "", 204


@app.websocket("/stream/<call_id>")
async def stream(call_id):
    call = calls.get(call_id, {"turns": []})
    stream_sid = None

    # Get stream SID
    while not stream_sid:
        raw = await asyncio.wait_for(websocket.receive(), timeout=10)
        data = json.loads(raw)
        if data.get("event") == "start":
            stream_sid = data["start"]["streamSid"]
            log.info("Connected: %s", stream_sid)

    # Play intro
    if "precached-intro" in PREGEN_AUDIO:
        log.info("Playing intro...")
        await play_audio(PREGEN_AUDIO["precached-intro"], stream_sid)
        log.info("Intro done, listening...")

    for turn in range(3):
        # Listen
        audio = await collect_speech()
        if audio is None:
            log.info("No speech, ending")
            return

        # Transcribe
        t0 = time.time()
        text = await transcribe(audio)
        log.info("Turn %d (%.1fs): %s", turn, time.time() - t0, text)
        if not text or len(text) < 3:
            continue

        call["turns"].append({"role": "user", "text": text})
        closing = turn >= 2

        # Pregen match?
        topic = match_topic(text)
        if topic and topic in PREGEN_AUDIO and not closing:
            log.info("INSTANT: %s", topic)
            await play_audio(PREGEN_AUDIO[topic], stream_sid)
            call["turns"].append({"role": "assistant", "text": f"[{topic}]"})
            continue

        # Play filler + generate concurrently
        import random
        filler_keys = [k for k in PREGEN_AUDIO if k.startswith("filler-")]

        async def generate():
            msgs = [{"role": "system", "content": PERSONA}] + call["turns"]
            if closing:
                msgs.append({"role": "system", "content": "Say goodbye warmly in under 10 words."})
            t1 = time.time()
            reply = await ask_llm(msgs)
            log.info("LLM (%.1fs): %s", time.time() - t1, reply)
            call["turns"].append({"role": "assistant", "text": reply})
            t2 = time.time()
            mulaw = await generate_tts(reply)
            log.info("TTS (%.1fs)", time.time() - t2)
            return mulaw

        gen_task = asyncio.create_task(generate())

        # Play filler (awaited — caller hears this while gen runs)
        if filler_keys:
            await play_audio(PREGEN_AUDIO[random.choice(filler_keys)], stream_sid)

        # Wait for generation
        mulaw = await gen_task
        if mulaw:
            log.info("Playing response")
            await play_audio(mulaw, stream_sid)

    log.info("Call done")


def make_call(to):
    import requests
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    base = os.environ["PUBLIC_BASE_URL"]
    call_id = uuid.uuid4().hex[:16]
    calls[call_id] = {"turns": []}
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
        data={"To": to, "From": os.environ["TWILIO_FROM_NUMBER"],
              "Url": f"{base}/twilio/voice/start/{call_id}",
              "StatusCallback": f"{base}/twilio/status", "StatusCallbackMethod": "POST"},
        auth=(sid, token), timeout=30)
    log.info("Call queued: %s (SID: %s)", to, r.json().get("sid"))
    return r.json()


if __name__ == "__main__":
    import sys
    load_pregen()
    if len(sys.argv) > 1 and sys.argv[1] == "call":
        print(json.dumps(make_call(sys.argv[2] if len(sys.argv) > 2 else "+13065966772"), indent=2))
    else:
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8787")))
