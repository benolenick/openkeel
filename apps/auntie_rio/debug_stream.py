#!/usr/bin/env python3
"""Minimal bidirectional stream test — just log all events."""
import os, json, asyncio, logging, base64, uuid
from quart import Quart, Response, websocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("debug")

app = Quart(__name__)

def public_url(path):
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + path

@app.get("/health")
async def health():
    return {"ok": True}

@app.route("/twilio/voice/start/<call_id>", methods=["GET", "POST"])
async def start(call_id):
    ws_url = public_url(f"/stream/{call_id}").replace("https://", "wss://")
    return Response(
        f'<Response><Connect><Stream url="{ws_url}" /></Connect></Response>',
        mimetype="text/xml")

@app.route("/twilio/status", methods=["POST"])
async def status():
    return "", 204

@app.websocket("/stream/<call_id>")
async def stream(call_id):
    log.info("WS connected")
    stream_sid = None
    media_count = 0

    for i in range(500):  # ~25 seconds at 20ms
        try:
            raw = await asyncio.wait_for(websocket.receive(), timeout=30)
            data = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                log.info("Event: connected")
            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                log.info("Event: start, SID=%s, tracks=%s", stream_sid, data["start"].get("tracks"))
            elif event == "media":
                media_count += 1
                if media_count <= 3 or media_count % 50 == 0:
                    payload_len = len(data["media"].get("payload", ""))
                    log.info("Event: media #%d, payload=%d bytes, track=%s",
                             media_count, payload_len, data["media"].get("track"))
            elif event == "mark":
                log.info("Event: mark, name=%s", data.get("mark", {}).get("name"))
            elif event == "stop":
                log.info("Event: stop")
                break
            else:
                log.info("Event: %s", event)

        except asyncio.TimeoutError:
            log.info("Timeout after %d media events", media_count)
            break
        except Exception as e:
            log.info("Error: %s", e)
            break

    log.info("WS closed. Total media events: %d", media_count)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8787)
