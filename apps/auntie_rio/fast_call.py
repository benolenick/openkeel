#!/usr/bin/env python3
"""Auntie Rio — Bidirectional streaming with Kokoro+RVC.
Confirmed working: <Connect><Stream> sends inbound media events.
"""
import os, json, base64, time, uuid, wave, io, asyncio, logging, subprocess, audioop, random
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = "GPU-26ea05a0-c6cf-f491-7210-a683fe498509"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import numpy as np
import aiohttp
from quart import Quart, Response, request, send_from_directory, websocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("auntie-rio")

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_SERVER = "http://127.0.0.1:8791"
RVC_SERVER = "http://127.0.0.1:8792"
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:e2b"

PERSONA = """You are Auntie Rio, a theatrical Jamaican fortune teller.
- Under 15 words. One short sentence.
- Mystical, warm. Use "darling" or "child".
- Entertainment only."""

app = Quart(__name__)
calls = {}

log.info("Loading Kokoro TTS...")
from kokoro import KPipeline
kokoro_pipe = KPipeline(lang_code='a')
list(kokoro_pipe("Test.", voice="af_heart"))
log.info("Kokoro ready!")

PREGEN_MULAW = {}

def kokoro_to_mulaw(text):
    """Generate with Kokoro, convert to 8kHz mulaw."""
    import soundfile as sf
    chunks = list(kokoro_pipe(text, voice="af_heart"))
    if not chunks: return None
    audio = np.concatenate([c[2] for c in chunks])
    # Save to wav, convert to 8kHz, then mulaw
    tmp = f"/tmp/_k_{uuid.uuid4().hex[:6]}.wav"
    tmp8 = tmp.replace(".wav","_8k.wav")
    sf.write(tmp, audio, 24000)
    subprocess.run(["ffmpeg","-y","-i",tmp,"-ar","8000","-ac","1","-sample_fmt","s16",tmp8],
                   capture_output=True, timeout=5)
    if os.path.exists(tmp8):
        with wave.open(tmp8,"rb") as wf: pcm = wf.readframes(wf.getnframes())
        mulaw = audioop.lin2ulaw(pcm, 2)
        os.unlink(tmp); os.unlink(tmp8)
        return mulaw
    try: os.unlink(tmp)
    except: pass
    return None

def kokoro_rvc_to_mulaw(text):
    """Kokoro → RVC Miss Cleo → mulaw."""
    import soundfile as sf, requests
    t0 = time.time()
    chunks = list(kokoro_pipe(text, voice="af_heart"))
    if not chunks: return None
    audio = np.concatenate([c[2] for c in chunks])
    kokoro_wav = f"/tmp/_k_{uuid.uuid4().hex[:6]}.wav"
    rvc_wav = f"/tmp/_r_{uuid.uuid4().hex[:6]}.wav"
    sf.write(kokoro_wav, audio, 24000)
    k_ms = (time.time()-t0)*1000

    try:
        resp = requests.post(f"{RVC_SERVER}/convert",
            json={"input_path": kokoro_wav, "output_path": rvc_wav}, timeout=10)
        if resp.status_code == 200 and os.path.exists(rvc_wav):
            rvc_ms = resp.json().get("elapsed",0)*1000
            tmp8 = rvc_wav.replace(".wav","_8k.wav")
            subprocess.run(["ffmpeg","-y","-i",rvc_wav,"-ar","8000","-ac","1","-sample_fmt","s16",tmp8],
                           capture_output=True, timeout=5)
            if os.path.exists(tmp8):
                with wave.open(tmp8,"rb") as wf: pcm = wf.readframes(wf.getnframes())
                mulaw = audioop.lin2ulaw(pcm, 2)
                os.unlink(tmp8); os.unlink(rvc_wav); os.unlink(kokoro_wav)
                log.info("TTS: Kokoro %.0fms + RVC %.0fms = %.0fms", k_ms, rvc_ms, (time.time()-t0)*1000)
                return mulaw
    except Exception as e:
        log.warning("RVC failed: %s, using Kokoro raw", e)

    # Fallback: no RVC
    tmp8 = kokoro_wav.replace(".wav","_8k.wav")
    subprocess.run(["ffmpeg","-y","-i",kokoro_wav,"-ar","8000","-ac","1","-sample_fmt","s16",tmp8],
                   capture_output=True, timeout=5)
    if os.path.exists(tmp8):
        with wave.open(tmp8,"rb") as wf: pcm = wf.readframes(wf.getnframes())
        mulaw = audioop.lin2ulaw(pcm, 2)
        os.unlink(tmp8)
    else:
        mulaw = None
    try: os.unlink(kokoro_wav)
    except: pass
    try: os.unlink(rvc_wav)
    except: pass
    return mulaw

def load_pregen():
    items = {
        "intro": "Hello darling, this is Auntie Rio. The spirits have been waiting for you. Tell me, what question burns in your heart?",
        "filler1": "Hmm, let me see what the cards have to say, darling.",
        "filler2": "The spirits are speaking to me now, one moment.",
    }
    for key, text in items.items():
        mulaw = kokoro_to_mulaw(text)
        if mulaw:
            PREGEN_MULAW[key] = mulaw
            log.info("Pregen: %s (%.1fs)", key, len(mulaw)/8000)

async def transcribe_mulaw(mulaw_bytes):
    pcm = audioop.ulaw2lin(mulaw_bytes, 2)
    buf = io.BytesIO()
    with wave.open(buf,"wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(8000); wf.writeframes(pcm)
    tmp = str(MEDIA_DIR / f"_asr_{uuid.uuid4().hex[:6]}.wav")
    with open(tmp,"wb") as f: f.write(buf.getvalue())
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{WHISPER_SERVER}/transcribe", json={"path":tmp},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    return d.get("text","").strip(), d.get("elapsed",0)
    except Exception as e:
        log.warning("ASR err: %s", e)
    finally:
        try: os.unlink(tmp)
        except: pass
    return "", 0

async def ask_llm(turns):
    prompt = PERSONA + "\n" + "\n".join(
        f"{'Caller' if t['role']=='user' else 'Auntie Rio'}: {t['text']}" for t in turns) + "\nAuntie Rio:"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{OLLAMA_URL}/api/generate",
                json={"model":OLLAMA_MODEL,"prompt":prompt,"stream":False,"options":{"num_predict":30,"temperature":0.8}},
                timeout=aiohttp.ClientTimeout(total=5)) as r:
                return (await r.json()).get("response","").strip() or "The spirits are cloudy, darling."
    except: return "The spirits are cloudy, darling."

def public_url(path):
    return os.environ.get("PUBLIC_BASE_URL","").rstrip("/") + path

async def send_mulaw(mulaw_bytes, stream_sid):
    """Send mulaw audio back to caller via WebSocket."""
    CHUNK = 160  # 20ms at 8kHz
    for i in range(0, len(mulaw_bytes), CHUNK):
        chunk = mulaw_bytes[i:i+CHUNK]
        if len(chunk) < CHUNK: chunk += b'\xff' * (CHUNK - len(chunk))
        await websocket.send(json.dumps({
            "event":"media","streamSid":stream_sid,
            "media":{"payload":base64.b64encode(chunk).decode("ascii")}
        }))
        await asyncio.sleep(0.018)

@app.get("/health")
async def health(): return {"ok":True,"tts":"kokoro+rvc","mode":"bidirectional"}

@app.get("/media/<path:filename>")
async def serve_media(filename): return await send_from_directory(str(MEDIA_DIR), filename)

@app.route("/twilio/voice/start/<call_id>", methods=["GET","POST"])
async def voice_start(call_id):
    calls[call_id] = {"turns":[]}
    ws_url = public_url(f"/stream/{call_id}").replace("https://","wss://")
    # Stream only — intro sent via WebSocket
    return Response(f'<Response><Connect><Stream url="{ws_url}" /></Connect></Response>', mimetype="text/xml")

@app.route("/twilio/status", methods=["POST"])
async def twilio_status(): return "", 204

@app.websocket("/stream/<call_id>")
async def stream(call_id):
    call = calls.get(call_id, {"turns":[]})
    stream_sid = None

    # Wait for start event
    while not stream_sid:
        raw = await asyncio.wait_for(websocket.receive(), timeout=10)
        data = json.loads(raw)
        if data.get("event") == "start":
            stream_sid = data["start"]["streamSid"]
            log.info("Stream: %s", stream_sid)
        elif data.get("event") == "connected":
            continue

    # Send intro via WebSocket
    if "intro" in PREGEN_MULAW:
        log.info("Sending intro...")
        await send_mulaw(PREGEN_MULAW["intro"], stream_sid)
        # Drain audio that arrived during intro playback
        intro_dur = len(PREGEN_MULAW["intro"]) / 8000
        drain_end = time.time() + intro_dur + 0.5
        while time.time() < drain_end:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=0.5)
                data = json.loads(raw)
                if data.get("event") == "stop":
                    return
            except asyncio.TimeoutError:
                break
        log.info("Intro done, listening...")

    for turn in range(6):
        buf = bytearray()
        speech_started = False
        silence_frames = 0
        SILENCE_NEEDED = 30  # ~0.6s of silence after speech
        ENERGY_THRESHOLD = 100  # mulaw: silence=0, noise=65-90, speech=120-150

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=10)
                data = json.loads(raw)
                if data.get("event") == "media":
                    payload = base64.b64decode(data["media"]["payload"])
                    buf.extend(payload)
                    energy = sum(abs(b - 255) for b in payload) / max(len(payload), 1)
                    if len(buf) < 3200 or len(buf) % 8000 == 0: log.info("Energy: %.1f (threshold: %d)", energy, ENERGY_THRESHOLD)
                    if energy > ENERGY_THRESHOLD:
                        speech_started = True
                        silence_frames = 0
                    elif speech_started:
                        silence_frames += 1
                    if speech_started and silence_frames >= SILENCE_NEEDED:
                        log.info("VAD: speech ended (%d bytes)", len(buf))
                        break
                elif data.get("event") == "stop":
                    return
            except asyncio.TimeoutError:
                if speech_started and len(buf) > 1600:
                    break
                return

        if not speech_started or len(buf) < 1600:
            continue

        # PROCESS: ASR → LLM → TTS (~1s total)
        t0 = time.time()
        transcript, asr_t = await transcribe_mulaw(bytes(buf))
        if not transcript or len(transcript) < 3:
            log.info("Empty transcript, skipping")
            continue

        call["turns"].append({"role":"user","text":transcript})
        closing = turn >= 5  # More turns before closing

        reply = await ask_llm(call["turns"])
        call["turns"].append({"role":"assistant","text":reply})

        mulaw = await asyncio.to_thread(kokoro_rvc_to_mulaw, reply)
        total = (time.time()-t0)*1000
        log.info("[%.0fms] '%s' → '%s'", total, transcript, reply)

        if mulaw:
            await send_mulaw(mulaw, stream_sid)
            # Drain incoming audio while response plays (ignore echo/feedback)
            play_duration = len(mulaw) / 8000  # seconds of audio we sent
            drain_until = time.time() + play_duration + 0.5
            while time.time() < drain_until:
                try:
                    raw = await asyncio.wait_for(websocket.receive(), timeout=0.5)
                    data = json.loads(raw)
                    if data.get("event") == "stop":
                        return
                except asyncio.TimeoutError:
                    break

    log.info("Call done")

def make_call(to):
    import requests
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    base = os.environ["PUBLIC_BASE_URL"]
    call_id = uuid.uuid4().hex[:16]
    calls[call_id] = {"turns":[]}
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
        data={"To":to,"From":os.environ["TWILIO_FROM_NUMBER"],
              "Url":f"{base}/twilio/voice/start/{call_id}",
              "StatusCallback":f"{base}/twilio/status","StatusCallbackMethod":"POST"},
        auth=(sid,token), timeout=30)
    log.info("Call: %s (SID: %s)", to, r.json().get("sid"))
    return r.json()

if __name__ == "__main__":
    import sys
    load_pregen()
    if len(sys.argv) > 1 and sys.argv[1] == "call":
        print(json.dumps(make_call(sys.argv[2] if len(sys.argv)>2 else "+13065966772"), indent=2))
    else:
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT","8787")))
