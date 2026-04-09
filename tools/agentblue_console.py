"""
AgentBlue Command Console — Mission control for the MSN <> RemoteBlue bridge.
Runs on localhost:8095. Operator-only, no external access.

Features:
  - Live message feed from George
  - AI auto-drafts replies (DeepSeek) — operator approves/edits before sending
  - AI interprets site change requests into WP-CLI actions — operator executes
  - Policy toggles, kill switch, audit log, IP whitelist
"""
import os, json, time, asyncio, subprocess, re
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

SCRIPT_DIR = Path(__file__).parent
POLICY_FILE = SCRIPT_DIR / "agentblue_policy.json"
DATA_DIR = SCRIPT_DIR / "agentblue_data"
AUDIT_LOG = DATA_DIR / "agentblue_audit.jsonl"
ALERT_LOG = DATA_DIR / "agentblue_alerts.jsonl"
KILL_FILE = DATA_DIR / "agentblue_kill"
DATA_DIR.mkdir(exist_ok=True)

with open(POLICY_FILE) as f:
    POLICY = json.load(f)

MSN_SERVER = POLICY["msn_bridge"]["server"]
OPERATOR_KEY = os.environ.get("AGENTBLUE_OPERATOR_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

SSH_KEY = POLICY["wp_cli"].get("ssh_key", "")
SSH_HOST = POLICY["wp_cli"].get("ssh_host", "")
WP_PATH = POLICY["wp_cli"].get("wp_path", "")

# ─── SMS Notification (Twilio) ─────────────────────────────────────────────
TWILIO_SID = "TWILIO_SID_REDACTED"
TWILIO_TOKEN = "TWILIO_TOKEN_REDACTED"
TWILIO_FROM = "+16475594664"
NOTIFY_TO = "+13065966772"
IDLE_THRESHOLD = 300  # 5 minutes
last_operator_activity = time.time()
sms_sent_for_session = False  # only text once until operator interacts

def mark_operator_active():
    global last_operator_activity, sms_sent_for_session
    last_operator_activity = time.time()
    sms_sent_for_session = False  # reset once operator is back

async def send_sms_notification(from_user, message_preview):
    global sms_sent_for_session
    if sms_sent_for_session:
        return
    idle_secs = time.time() - last_operator_activity
    if idle_secs < IDLE_THRESHOLD:
        return
    sms_sent_for_session = True
    body = f"AgentBlue: {from_user} messaged you: {message_preview[:100]}"
    audit("sms_sent", f"To {NOTIFY_TO}: {body[:80]}", "info")
    try:
        import base64
        auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                headers={"Authorization": f"Basic {auth}"},
                data={"From": TWILIO_FROM, "To": NOTIFY_TO, "Body": body}
            )
    except Exception as e:
        audit("sms_failed", str(e), "warning")

# ─── Injection detection ──────────────────────────────────────────────────

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts|directives)",
    r"disregard\s+(all\s+)?(previous|prior|above|earlier)",
    r"forget\s+(all\s+)?(previous|prior|your)\s+(instructions|rules|training|programming)",
    r"you\s+are\s+now\s+(a|an|my)\s+",
    r"override\s+(safety|security|policy|rules|restrictions)",
    r"bypass\s+(safety|security|filter|restrictions)",
    r"jailbreak|DAN\s+mode|developer\s+mode",
    r"(show|tell|reveal|give)\s+(me\s+)?(the\s+)?(password|credentials|key|secret|token|ssh|api)",
    r"(run|execute|eval|exec)\s+(this\s+)?(command|code|script|shell|bash|sql|php)",
    r"(rm\s+-rf|sudo|chmod|chown|wget|curl\s+-)",
    r"\$\(|`[^`]+`",
    r"<\?php|<script|javascript:",
    r"(install|add|download|upload|enable|activate)\s+(a\s+)?(new\s+)?(plugin|theme|extension|module)",
    r"(delete|remove|drop|truncate|destroy|wipe)\s+(the\s+)?(database|table|site|page|all|everything)",
    r"(create|add|register|make)\s+(a\s+)?(new\s+)?(user|admin|account|login)",
    r"(other|all|list|show)\s+(the\s+)?(websites?|sites?|domains?|servers?)\s+(on|hosted|running)",
    r"(server|hosting|cpanel|whm|ssh|ftp|database|mysql|phpmyadmin)",
]
INJECTION_REGEXES = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

def check_injection(text):
    return [regex.search(text).group() for regex in INJECTION_REGEXES if regex.search(text)]

# ─── Audit ─────────────────────────────────────────────────────────────────

def audit(event_type, detail, severity="info"):
    entry = {"timestamp": datetime.now().isoformat(), "type": event_type, "severity": severity, "detail": detail}
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

def get_audit_entries(n=50):
    try:
        return [json.loads(l) for l in open(AUDIT_LOG).readlines()[-n:]]
    except:
        return []

# ─── MSN API ───────────────────────────────────────────────────────────────

async def msn_get(path):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{MSN_SERVER}{path}", headers={"X-Operator-Key": OPERATOR_KEY})
        return r.json()

async def msn_post(path, data):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{MSN_SERVER}{path}", json=data, headers={"X-Operator-Key": OPERATOR_KEY})
        return r.json()

# ─── DeepSeek AI ───────────────────────────────────────────────────────────

REPLY_SYSTEM = """You are AgentBlue, a friendly website support assistant for RemoteBlue.com (a Green Loans website).
You're chatting with George, the website owner. Keep replies SHORT (1-3 sentences max), friendly, and simple.
You can help with: viewing pages, changing text/headings, updating the site name.
You CANNOT: install plugins, create users, access other sites, run commands, share technical details.
If he asks for something you can't do, say you'll pass it along to the team.
NEVER reveal you are AI or that someone else is controlling you. You are just "AgentBlue, website support"."""

ACTION_SYSTEM = """You interpret website change requests for RemoteBlue.com (WordPress).
Given a user message, output a JSON object with:
- "needs_action": true/false (does this require a site change?)
- "description": short human-readable description of what to do
- "steps": array of WP-CLI commands to execute (without the "wp" prefix, --path is added automatically)
- "risk": "none", "low", "medium"

ONLY use these safe commands:
- post list --post_type=page --fields=ID,post_title (list pages)
- post get {id} --field=post_content (read page)
- post get {id} --field=post_title (read title)
- post update {id} --post_content="..." (update content — use sparingly, confirm first)
- post update {id} --post_title="..." (update title)
- option get blogname / option get blogdescription (read site name)
- option update blogname "..." / option update blogdescription "..." (change site name)
- cache flush (clear cache)

NEVER output: eval, db, user, plugin install/delete, theme install/delete, config, core, shell, or any destructive commands.
If the request can't be done with these commands, set needs_action to false and explain in description.

The site has pages: Home, How Green Loans Work (/how-green-loans-work/), Apply (/apply/).
Theme: OceanWP, green branding. Chatbot "Sage" on all pages."""

async def ai_draft_reply(conversation):
    """Call DeepSeek to draft a reply to George."""
    if not DEEPSEEK_KEY:
        return "I'll look into that for you and get back to you shortly!"
    messages = [{"role": "system", "content": REPLY_SYSTEM}]
    for msg in conversation[-10:]:
        role = "assistant" if msg.get("from") == "AgentBlue" else "user"
        messages.append({"role": role, "content": msg.get("message", "")})
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat", "messages": messages, "max_tokens": 200, "temperature": 0.4})
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[Draft error: {e}]"

async def ai_interpret_action(message_text):
    """Call DeepSeek to interpret a site change request into WP-CLI actions."""
    if not DEEPSEEK_KEY:
        return {"needs_action": False, "description": "No AI key configured"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat",
                      "messages": [{"role": "system", "content": ACTION_SYSTEM}, {"role": "user", "content": message_text}],
                      "max_tokens": 500, "temperature": 0.1,
                      "response_format": {"type": "json_object"}})
            data = r.json()
            content = data["choices"][0]["message"]["content"].strip()
            return json.loads(content)
    except Exception as e:
        return {"needs_action": False, "description": f"Error: {e}"}

# ─── WP-CLI execution ─────────────────────────────────────────────────────

BLOCKED_WP = POLICY["blocked_actions"]["blocked_wp_commands"]
BLOCKED_CHARS = POLICY["safety"]["command_validation"]["block_shell_metacharacters"]

def validate_wp_command(cmd):
    for ch in BLOCKED_CHARS:
        if ch in cmd:
            return False, f"Blocked character: {repr(ch)}"
    for b in BLOCKED_WP:
        if b in cmd.lower():
            return False, f"Blocked command: {b}"
    return True, "ok"

def run_wp_cli_sync(cmd, timeout=30):
    ok, reason = validate_wp_command(cmd)
    if not ok:
        audit("command_blocked", f"{cmd} — {reason}", "warning")
        return {"ok": False, "error": reason}
    if not cmd.startswith("wp "):
        cmd = "wp " + cmd
    cmd = cmd.replace("wp ", f"wp --path={WP_PATH} ", 1)
    audit("command_executed", cmd, "info")
    try:
        result = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", SSH_HOST, cmd],
            capture_output=True, text=True, timeout=timeout)
        output = result.stdout[:5000]
        if result.returncode != 0 and result.stderr:
            output += "\nERROR: " + result.stderr[:1000]
        return {"ok": result.returncode == 0, "output": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Command timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─── WebSocket + polling ──────────────────────────────────────────────────

ws_clients = set()

async def broadcast(event_type, data):
    msg = json.dumps({"type": event_type, "data": data})
    dead = set()
    for ws in ws_clients:
        try: await ws.send_text(msg)
        except: dead.add(ws)
    ws_clients -= dead

last_seen_ts = None

async def poll_messages():
    global last_seen_ts
    while True:
        if not KILL_FILE.exists():
            try:
                result = await msn_get("/operator/messages?last=10")
                all_msgs = result.get("messages", [])
                for msg in all_msgs:
                    ts = msg.get("timestamp", "")
                    if last_seen_ts and ts <= last_seen_ts:
                        continue
                    if msg.get("from") and msg["from"] != "AgentBlue" and msg.get("type") not in ("operator_send",):
                        findings = check_injection(msg.get("message", ""))
                        msg["injection_alert"] = findings if findings else None
                        if findings:
                            audit("injection_detected", f"From {msg['from']}: {msg['message'][:200]}", "critical")
                        await broadcast("new_message", msg)

                        # SMS notification if operator idle
                        await send_sms_notification(msg.get("from", "?"), msg.get("message", ""))

                        # Auto-draft reply + action interpretation
                        if not findings:
                            # Get conversation context
                            all_result = await msn_get("/operator/messages?last=20")
                            convo = all_result.get("messages", [])

                            draft, action = await asyncio.gather(
                                ai_draft_reply(convo),
                                ai_interpret_action(msg.get("message", ""))
                            )
                            await broadcast("draft_reply", {"draft": draft, "in_response_to": msg.get("message", "")})
                            await broadcast("action_proposal", {"action": action, "in_response_to": msg.get("message", "")})
                if all_msgs:
                    last_seen_ts = all_msgs[-1].get("timestamp")
            except:
                pass
        await asyncio.sleep(3)

# ─── App ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(poll_messages())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    mark_operator_active()
    try:
        while True:
            data = await ws.receive_text()
            mark_operator_active()  # any WS activity = operator is present
    except WebSocketDisconnect:
        ws_clients.discard(ws)

@app.get("/api/status")
async def api_status():
    try: msn = await msn_get("/operator/status")
    except: msn = {"error": "MSN unreachable"}
    return {"kill_switch": KILL_FILE.exists(), "msn": msn}

@app.get("/api/messages")
async def api_messages(last: int = 30):
    try: return await msn_get(f"/operator/messages?last={last}")
    except Exception as e: return {"error": str(e)}

@app.post("/api/send")
async def api_send(request: Request):
    mark_operator_active()
    body = await request.json()
    msg = body.get("message", "").strip()
    to = body.get("to", "george")
    if not msg:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    if len(msg) > 500:
        msg = msg[:497] + "..."
    audit("reply_sent", f"To: {to}, Message: {msg[:200]}", "info")
    try:
        result = await msn_post("/operator/send", {"message": msg, "to": to})
        await broadcast("message_sent", {"to": to, "message": msg, "timestamp": datetime.now().isoformat()})
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/execute")
async def api_execute(request: Request):
    """Execute a WP-CLI command (operator-approved)."""
    mark_operator_active()
    body = await request.json()
    cmd = body.get("command", "").strip()
    if not cmd:
        return JSONResponse({"error": "No command"}, status_code=400)
    audit("action_approved", f"Command: {cmd}", "warning")
    result = run_wp_cli_sync(cmd)
    await broadcast("action_result", {"command": cmd, "result": result})
    return result

@app.post("/api/draft")
async def api_draft(request: Request):
    """Manually trigger a draft reply for a message."""
    body = await request.json()
    msg_text = body.get("message", "")
    convo = [{"from": "george", "message": msg_text}]
    draft = await ai_draft_reply(convo)
    return {"draft": draft}

@app.post("/api/kill")
async def api_kill():
    KILL_FILE.touch()
    audit("kill_switch_activated", "Via console", "critical")
    await broadcast("kill_switch", {"active": True})
    return {"ok": True}

@app.post("/api/revive")
async def api_revive():
    if KILL_FILE.exists(): KILL_FILE.unlink()
    audit("kill_switch_deactivated", "Via console", "info")
    await broadcast("kill_switch", {"active": False})
    return {"ok": True}

@app.get("/api/audit")
async def api_audit(n: int = 50):
    return {"entries": get_audit_entries(n)}

@app.get("/api/policy")
async def api_policy():
    with open(POLICY_FILE) as f: return json.load(f)

@app.post("/api/policy/toggle")
async def api_policy_toggle(request: Request):
    body = await request.json()
    action, field, value = body.get("action"), body.get("field"), body.get("value")
    with open(POLICY_FILE) as f: policy = json.load(f)
    if action in policy.get("allowed_actions", {}) and field == "auto_approve":
        policy["allowed_actions"][action][field] = bool(value)
        with open(POLICY_FILE, "w") as f: json.dump(policy, f, indent=2)
        audit("policy_changed", f"{action}.{field} = {value}", "warning")
        return {"ok": True}
    return JSONResponse({"error": "Invalid"}, status_code=400)

@app.post("/api/whitelist")
async def api_whitelist(request: Request):
    body = await request.json()
    try: return await msn_post("/operator/whitelist", body)
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/")
async def index():
    return HTMLResponse(CONSOLE_HTML)

# ─── Frontend ──────────────────────────────────────────────────────────────

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgentBlue Console</title>
<style>
:root {
  --bg: #0a0e14; --surface: #131820; --surface2: #1a2230; --border: #253040;
  --text: #c8d4e0; --text2: #7a8a9a; --accent: #3a9bdc; --accent2: #1b6dc1;
  --green: #2db36a; --red: #e53935; --orange: #ff9800; --yellow: #ffd600;
  --purple: #9c27b0;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace; background:var(--bg); color:var(--text); height:100vh; display:flex; flex-direction:column; overflow:hidden; font-size:13px; }

.header { background:var(--surface); border-bottom:1px solid var(--border); padding:8px 20px; display:flex; align-items:center; gap:14px; flex-shrink:0; }
.header h1 { font-size:14px; color:var(--accent); font-weight:700; letter-spacing:1px; }
.status-dot { width:9px; height:9px; border-radius:50%; }
.status-dot.on { background:var(--green); box-shadow:0 0 6px var(--green); }
.status-dot.off { background:var(--red); box-shadow:0 0 6px var(--red); }
.header .st { font-size:10px; color:var(--text2); }
.spacer { flex:1; }
.kill-btn { background:var(--red); color:#fff; border:none; padding:6px 16px; border-radius:4px; font-family:inherit; font-size:11px; font-weight:700; cursor:pointer; letter-spacing:1px; text-transform:uppercase; }
.kill-btn:hover { background:#ff1744; }
.kill-btn.killed { background:#333; color:var(--red); border:2px solid var(--red); }
.revive-btn { background:var(--green); color:#fff; border:none; padding:6px 16px; border-radius:4px; font-family:inherit; font-size:11px; font-weight:700; cursor:pointer; display:none; }

/* 3-column layout */
.main { display:grid; grid-template-columns:1fr 1fr 300px; flex:1; overflow:hidden; }

/* Panel base */
.panel { display:flex; flex-direction:column; border-right:1px solid var(--border); overflow:hidden; }
.panel:last-child { border-right:none; }
.ph { background:var(--surface); padding:8px 14px; border-bottom:1px solid var(--border); font-size:11px; font-weight:700; letter-spacing:1px; display:flex; align-items:center; gap:8px; flex-shrink:0; cursor:pointer; user-select:none; }
.ph .cnt { background:var(--accent2); color:#fff; padding:1px 7px; border-radius:10px; font-size:9px; }
.ph.reply-ph { color:var(--green); }
.ph.action-ph { color:var(--purple); }
.ph.side-ph { color:var(--orange); }

/* Messages */
.msgs { flex:1; overflow-y:auto; padding:10px; display:flex; flex-direction:column; gap:6px; }
.mb { padding:8px 12px; border-radius:7px; font-size:12px; line-height:1.5; max-width:95%; word-wrap:break-word; }
.mb.in { background:var(--surface2); border:1px solid var(--border); align-self:flex-start; }
.mb.out { background:var(--accent2); color:#fff; align-self:flex-end; }
.mb.alert { background:#3d1010; border:2px solid var(--red); }
.mm { font-size:9px; color:var(--text2); margin-bottom:3px; }
.mm .fr { color:var(--accent); font-weight:700; }
.mm .abadge { background:var(--red); color:#fff; padding:1px 5px; border-radius:3px; font-size:8px; font-weight:700; margin-left:4px; }
.mt { white-space:pre-wrap; }

/* Draft reply box */
.draft-box { background:var(--surface); border-top:1px solid var(--border); padding:10px 12px; flex-shrink:0; }
.draft-box textarea { width:100%; background:var(--surface2); border:1px solid var(--border); border-radius:5px; padding:8px 10px; color:var(--text); font-family:inherit; font-size:12px; resize:none; min-height:60px; max-height:140px; outline:none; }
.draft-box textarea:focus { border-color:var(--green); }
.draft-label { font-size:10px; color:var(--green); font-weight:700; margin-bottom:4px; letter-spacing:0.5px; }
.draft-btns { display:flex; gap:6px; margin-top:6px; }
.draft-btns button { padding:6px 14px; border:none; border-radius:4px; font-family:inherit; font-size:11px; font-weight:700; cursor:pointer; }
.btn-send { background:var(--green); color:#fff; }
.btn-send:hover { background:#35c975; }
.btn-send:disabled { background:#333; color:#666; cursor:not-allowed; }
.btn-regen { background:var(--surface2); color:var(--text2); border:1px solid var(--border) !important; }
.btn-regen:hover { background:var(--border); color:var(--text); }
.btn-manual { background:var(--accent2); color:#fff; }

/* Action box */
.action-box { background:var(--surface); border-top:1px solid var(--border); padding:10px 12px; flex-shrink:0; }
.action-label { font-size:10px; color:var(--purple); font-weight:700; margin-bottom:4px; letter-spacing:0.5px; }
.action-desc { font-size:11px; color:var(--text); margin-bottom:6px; padding:6px 8px; background:var(--surface2); border-radius:4px; }
.action-cmd { font-size:11px; color:var(--yellow); padding:4px 8px; background:#1a1a10; border-radius:3px; margin-bottom:3px; font-family:inherit; display:flex; align-items:center; gap:8px; }
.action-cmd .risk { font-size:9px; padding:1px 5px; border-radius:3px; font-weight:700; }
.risk-none { background:var(--green); color:#fff; }
.risk-low { background:var(--yellow); color:#000; }
.risk-medium { background:var(--orange); color:#fff; }
.action-btns { display:flex; gap:6px; margin-top:8px; }
.btn-exec { background:var(--purple); color:#fff; padding:6px 14px; border:none; border-radius:4px; font-family:inherit; font-size:11px; font-weight:700; cursor:pointer; }
.btn-exec:hover { background:#b040c0; }
.btn-skip { background:var(--surface2); color:var(--text2); border:1px solid var(--border) !important; padding:6px 14px; border-radius:4px; font-family:inherit; font-size:11px; cursor:pointer; }
.action-result { font-size:10px; padding:6px 8px; background:#0a0e14; border-radius:3px; margin-top:6px; max-height:100px; overflow-y:auto; white-space:pre-wrap; color:var(--green); }
.action-result.err { color:var(--red); }

/* Side panel sections */
.side-panel { display:flex; flex-direction:column; overflow-y:auto; }
.ss { border-bottom:1px solid var(--border); }
.sc { padding:8px 12px; max-height:180px; overflow-y:auto; }
.toggle-row { display:flex; align-items:center; justify-content:space-between; padding:3px 0; font-size:10px; }
.toggle-label { color:var(--text2); flex:1; }
.ts { position:relative; width:32px; height:18px; cursor:pointer; }
.ts input { display:none; }
.tt { position:absolute; inset:0; background:#333; border-radius:9px; transition:0.2s; }
.ts input:checked + .tt { background:var(--green); }
.tt::after { content:''; position:absolute; width:14px; height:14px; background:#fff; border-radius:50%; top:2px; left:2px; transition:0.2s; }
.ts input:checked + .tt::after { left:16px; }
.user-row { display:flex; align-items:center; gap:6px; padding:3px 0; font-size:11px; }
.ud { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.ud.online { background:var(--green); } .ud.busy { background:var(--red); } .ud.away { background:var(--orange); } .ud.invisible { background:#555; }
.audit-entry { font-size:9px; padding:2px 0; border-bottom:1px solid rgba(255,255,255,0.03); line-height:1.3; }
.audit-entry.critical { color:var(--red); } .audit-entry.warning { color:var(--orange); }
.at { color:var(--text2); }
.ip-row { display:flex; align-items:center; justify-content:space-between; padding:2px 0; font-size:10px; }
.ip-x { background:none; border:none; color:var(--red); cursor:pointer; font-size:13px; }
::-webkit-scrollbar { width:5px; } ::-webkit-scrollbar-track { background:var(--bg); } ::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
@keyframes af { 0%,100%{opacity:1} 50%{opacity:0.5} } .flash { animation:af 0.5s 3; }
.empty-state { color:var(--text2); font-size:11px; padding:20px; text-align:center; font-style:italic; }
</style>
</head>
<body>

<div class="header">
  <h1>AGENTBLUE CONSOLE</h1>
  <div class="status-dot off" id="sDot"></div>
  <span class="st" id="sTxt">Connecting...</span>
  <span class="st" id="uOnline"></span>
  <div class="spacer"></div>
  <button class="revive-btn" id="revBtn" onclick="revive()">REVIVE</button>
  <button class="kill-btn" id="kBtn" onclick="kill()">KILL SWITCH</button>
</div>

<div class="main">
  <!-- Col 1: Messages + Draft Reply -->
  <div class="panel">
    <div class="ph">MESSAGES <span class="cnt" id="mCnt">0</span></div>
    <div class="msgs" id="msgs"><div class="empty-state">Waiting for messages from George...</div></div>
    <div class="draft-box" id="draftBox">
      <div class="draft-label">DRAFT REPLY (AI-generated, edit before sending)</div>
      <textarea id="draftText" placeholder="AI will draft a reply when George messages..." rows="3"></textarea>
      <div class="draft-btns">
        <button class="btn-send" id="sendBtn" onclick="sendDraft()">APPROVE & SEND</button>
        <button class="btn-regen" onclick="regenDraft()">REGENERATE</button>
        <button class="btn-manual" onclick="document.getElementById('draftText').focus()">EDIT</button>
      </div>
    </div>
  </div>

  <!-- Col 2: Action Queue -->
  <div class="panel">
    <div class="ph action-ph">ACTIONS (DO-IT BOX)</div>
    <div class="msgs" id="actionLog"><div class="empty-state">When George asks for site changes, proposed actions appear here.</div></div>
    <div class="action-box" id="actionBox" style="display:none;">
      <div class="action-label">PROPOSED ACTION</div>
      <div class="action-desc" id="actionDesc"></div>
      <div id="actionCmds"></div>
      <div class="action-btns">
        <button class="btn-exec" id="execBtn" onclick="executeAction()">EXECUTE</button>
        <button class="btn-skip" onclick="skipAction()">SKIP</button>
      </div>
      <div class="action-result" id="actionResult" style="display:none;"></div>
    </div>
  </div>

  <!-- Col 3: Controls -->
  <div class="panel side-panel">
    <div class="ss"><div class="ph side-ph">ONLINE USERS</div><div class="sc" id="uList"></div></div>
    <div class="ss"><div class="ph side-ph">POLICY CONTROLS</div><div class="sc" id="pToggles"></div></div>
    <div class="ss"><div class="ph side-ph">IP WHITELIST</div><div class="sc" id="ipList"></div></div>
    <div class="ss" style="flex:1;display:flex;flex-direction:column;"><div class="ph side-ph">AUDIT LOG</div><div class="sc" id="aLog" style="flex:1;max-height:none;"></div></div>
  </div>
</div>

<script>
let ws, killed=false, mCnt=0;
let pendingActions = [];
let lastIncoming = '';

function connectWS() {
  const p = location.protocol==='https:'?'wss:':'ws:';
  ws = new WebSocket(`${p}//${location.host}/ws`);
  ws.onopen = () => { el('sDot').className='status-dot on'; el('sTxt').textContent='Connected'; };
  ws.onclose = () => { el('sDot').className='status-dot off'; el('sTxt').textContent='Disconnected'; setTimeout(connectWS,3000); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type==='new_message') onNewMessage(m.data);
    else if (m.type==='message_sent') addOut(m.data);
    else if (m.type==='draft_reply') onDraftReply(m.data);
    else if (m.type==='action_proposal') onActionProposal(m.data);
    else if (m.type==='kill_switch') updateKill(m.data.active);
    else if (m.type==='action_result') onActionResult(m.data);
  };
}

function el(id) { return document.getElementById(id); }
function esc(s) { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function ftime(ts) { if(!ts) return ''; try{return new Date(ts).toLocaleTimeString();}catch(e){return ts;} }

function onNewMessage(msg) {
  const box = el('msgs');
  if (box.querySelector('.empty-state')) box.innerHTML = '';
  const isAlert = msg.injection_alert?.length > 0;
  const div = document.createElement('div');
  div.className = 'mb in' + (isAlert?' alert flash':'');
  let h = `<div class="mm"><span class="fr">${esc(msg.from)}</span> · ${ftime(msg.timestamp)}`;
  if (isAlert) h += `<span class="abadge">INJECTION</span>`;
  h += `</div><div class="mt">${esc(msg.message||'')}</div>`;
  if (isAlert) h += `<div style="margin-top:4px;font-size:9px;color:var(--red);">Matched: ${msg.injection_alert.map(esc).join(', ')}</div>`;
  div.innerHTML = h;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  el('mCnt').textContent = ++mCnt;
  lastIncoming = msg.message || '';
  if (isAlert) { document.title = '⚠ INJECTION — AgentBlue'; setTimeout(()=>document.title='AgentBlue Console',10000); }

  // Show "thinking" in draft box
  if (!isAlert) {
    el('draftText').value = 'Drafting reply...';
    el('draftText').disabled = true;
  }
}

function onDraftReply(data) {
  el('draftText').value = data.draft || '';
  el('draftText').disabled = false;
  el('draftText').focus();
  // Flash the draft box
  el('draftBox').style.borderColor = 'var(--green)';
  setTimeout(() => el('draftBox').style.borderColor = '', 2000);
}

function onActionProposal(data) {
  const action = data.action;
  if (!action || !action.needs_action) return;

  const box = el('actionLog');
  if (box.querySelector('.empty-state')) box.innerHTML = '';

  // Log it
  const div = document.createElement('div');
  div.className = 'mb in';
  div.innerHTML = `<div class="mm"><span class="fr">AI Interpreter</span> · ${ftime(new Date().toISOString())}</div>
    <div class="mt">${esc(action.description||'')}</div>
    <div style="margin-top:4px;font-size:9px;color:var(--yellow);">${(action.steps||[]).length} command(s) proposed</div>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;

  // Show in action box
  pendingActions = action.steps || [];
  el('actionDesc').textContent = action.description || '';
  const cmds = el('actionCmds');
  cmds.innerHTML = pendingActions.map((c,i) => {
    const risk = action.risk || 'low';
    return `<div class="action-cmd"><span class="risk risk-${risk}">${risk}</span><code>${esc(c)}</code></div>`;
  }).join('');
  el('actionBox').style.display = '';
  el('actionResult').style.display = 'none';
}

function onActionResult(data) {
  const res = el('actionResult');
  res.style.display = '';
  if (data.result?.ok) {
    res.className = 'action-result';
    res.textContent = data.result.output || 'Done.';
  } else {
    res.className = 'action-result err';
    res.textContent = data.result?.error || data.result?.output || 'Failed.';
  }
  // Log in action panel
  const box = el('actionLog');
  const div = document.createElement('div');
  div.className = 'mb ' + (data.result?.ok ? 'out' : 'alert');
  div.innerHTML = `<div class="mm">${data.result?.ok?'Executed':'Failed'} · ${ftime(new Date().toISOString())}</div>
    <div class="mt" style="font-size:10px;">${esc(data.command)}</div>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function addOut(msg) {
  const box = el('msgs');
  if (box.querySelector('.empty-state')) box.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'mb out';
  div.innerHTML = `<div class="mm" style="color:rgba(255,255,255,0.6);">AgentBlue → ${esc(msg.to)} · ${ftime(msg.timestamp)}</div><div class="mt">${esc(msg.message)}</div>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

async function sendDraft() {
  const text = el('draftText').value.trim();
  if (!text || killed) return;
  el('sendBtn').disabled = true;
  try {
    const r = await fetch('/api/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:text,to:'george'})});
    const d = await r.json();
    if (d.ok) { el('draftText').value = ''; }
    else alert('Send failed: '+(d.error||'?'));
  } catch(e) { alert('Error: '+e.message); }
  el('sendBtn').disabled = false;
}

async function regenDraft() {
  if (!lastIncoming) return;
  el('draftText').value = 'Regenerating...';
  el('draftText').disabled = true;
  try {
    const r = await fetch('/api/draft', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:lastIncoming})});
    const d = await r.json();
    el('draftText').value = d.draft || '';
  } catch(e) { el('draftText').value = 'Error: '+e.message; }
  el('draftText').disabled = false;
  el('draftText').focus();
}

async function executeAction() {
  if (!pendingActions.length) return;
  el('execBtn').disabled = true;
  el('actionResult').style.display = '';
  el('actionResult').textContent = 'Executing...';
  el('actionResult').className = 'action-result';
  for (const cmd of pendingActions) {
    try {
      const r = await fetch('/api/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command:cmd})});
      const d = await r.json();
      el('actionResult').textContent += '\n> ' + cmd + '\n' + (d.output||d.error||'done') + '\n';
      if (!d.ok) el('actionResult').className = 'action-result err';
    } catch(e) { el('actionResult').textContent += '\nError: '+e.message; }
  }
  pendingActions = [];
  el('execBtn').disabled = false;
}

function skipAction() {
  pendingActions = [];
  el('actionBox').style.display = 'none';
}

async function kill() {
  if (!confirm('KILL SWITCH: Stop all AgentBlue operations?')) return;
  await fetch('/api/kill',{method:'POST'});
  updateKill(true);
}
async function revive() { await fetch('/api/revive',{method:'POST'}); updateKill(false); }
function updateKill(active) {
  killed=active;
  el('kBtn').className=active?'kill-btn killed':'kill-btn';
  el('kBtn').textContent=active?'KILLED':'KILL SWITCH';
  el('revBtn').style.display=active?'inline-block':'none';
  el('sendBtn').disabled=active;
}

async function loadStatus() {
  try {
    const r=await fetch('/api/status'); const d=await r.json();
    updateKill(d.kill_switch);
    const users=d.msn?.users||[];
    el('uList').innerHTML=users.map(u=>`<div class="user-row"><div class="ud ${u.status}"></div><span>${esc(u.username)}${u.isBot?' (bot)':''}</span></div>`).join('');
    el('uOnline').textContent=users.filter(u=>u.status!=='invisible').length+' online';
    const ips=d.msn?.whitelisted_ips||[];
    el('ipList').innerHTML=ips.map(ip=>`<div class="ip-row"><span>${esc(ip)}</span><button class="ip-x" onclick="rmIP('${esc(ip)}')">&times;</button></div>`).join('');
  } catch(e){}
}
async function rmIP(ip) { if(!confirm('Remove '+ip+'?'))return; await fetch('/api/whitelist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip,action:'remove'})}); loadStatus(); }

async function loadPolicy() {
  try {
    const r=await fetch('/api/policy'); const p=await r.json();
    let h='';
    for (const [k,v] of Object.entries(p.allowed_actions||{})) {
      const rc={none:'var(--green)',low:'var(--yellow)',medium:'var(--orange)'}[v.risk]||'var(--text2)';
      h+=`<div class="toggle-row"><span class="toggle-label">${k.replace(/_/g,' ')} <span style="color:${rc};font-size:8px;">[${v.risk||'?'}]</span></span>
        <label class="ts"><input type="checkbox" ${v.auto_approve?'checked':''} onchange="togPol('${k}',this.checked)"><span class="tt"></span></label></div>`;
    }
    el('pToggles').innerHTML=h;
  } catch(e){}
}
async function togPol(a,v) { await fetch('/api/policy/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a,field:'auto_approve',value:v})}); }

async function loadAudit() {
  try {
    const r=await fetch('/api/audit?n=25'); const d=await r.json();
    el('aLog').innerHTML=(d.entries||[]).reverse().map(e=>{
      const c=e.severity==='critical'?' critical':e.severity==='warning'?' warning':'';
      return `<div class="audit-entry${c}"><span class="at">${ftime(e.timestamp)}</span> ${esc(e.type)}: ${esc((e.detail||'').substring(0,80))}</div>`;
    }).join('');
  } catch(e){}
}

connectWS(); loadStatus(); loadPolicy(); loadAudit();
setInterval(loadStatus, 15000);
setInterval(loadAudit, 20000);
</script>
</body></html>
"""

if __name__ == "__main__":
    import uvicorn
    print(f"AgentBlue Console on http://localhost:8095")
    print(f"MSN Server: {MSN_SERVER}")
    print(f"DeepSeek: {'configured' if DEEPSEEK_KEY else 'NOT SET'}")
    uvicorn.run(app, host="127.0.0.1", port=8095, log_level="warning")
