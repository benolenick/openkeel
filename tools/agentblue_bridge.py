"""
AgentBlue Bridge — Hardened bridge between MSN Messenger and RemoteBlue.com
All operations are policy-gated. Default: DENY.

Usage:
    python agentblue_bridge.py                  # Run bridge (poll mode)
    python agentblue_bridge.py --check          # Check messages without replying
    python agentblue_bridge.py --send "msg"     # Send a message as AgentBlue (operator mode)
    python agentblue_bridge.py --kill           # Create kill switch file
    python agentblue_bridge.py --revive         # Remove kill switch file
    python agentblue_bridge.py --audit          # Show recent audit log
"""

import os, sys, json, re, time, subprocess, hashlib, html
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
POLICY_FILE = SCRIPT_DIR / "agentblue_policy.json"
DATA_DIR = SCRIPT_DIR / "agentblue_data"
AUDIT_LOG = DATA_DIR / "agentblue_audit.jsonl"
KILL_FILE = DATA_DIR / "agentblue_kill"
STATE_FILE = DATA_DIR / "agentblue_state.json"
ALERT_LOG = DATA_DIR / "agentblue_alerts.jsonl"

DATA_DIR.mkdir(exist_ok=True)

# ─── Load Policy ───────────────────────────────────────────────────────────

with open(POLICY_FILE) as f:
    POLICY = json.load(f)

MSN_SERVER = POLICY["msn_bridge"]["server"]
OPERATOR_KEY = os.environ.get(
    POLICY["msn_bridge"]["operator_key_env"],
    os.environ.get("AGENTBLUE_OPERATOR_KEY", "")
)
SSH_KEY = POLICY["wp_cli"]["ssh_key"]
SSH_HOST = POLICY["wp_cli"]["ssh_host"]
WP_PATH = POLICY["wp_cli"]["wp_path"]

# ─── Rate Limiter ──────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self):
        self.writes = []        # timestamps of write operations
        self.reads = []         # timestamps of read operations
        self.messages = []      # timestamps of messages sent
        self.last_write = 0     # timestamp of last write

    def can_write(self):
        now = time.time()
        cutoff = now - 3600
        self.writes = [t for t in self.writes if t > cutoff]
        limit = POLICY["safety"]["rate_limits"]["max_writes_per_hour"]
        cooldown = POLICY["safety"]["rate_limits"]["cooldown_after_write_seconds"]
        if len(self.writes) >= limit:
            return False, f"Rate limit: {limit} writes/hour exceeded"
        if now - self.last_write < cooldown:
            return False, f"Cooldown: wait {int(cooldown - (now - self.last_write))}s after last write"
        return True, "ok"

    def record_write(self):
        now = time.time()
        self.writes.append(now)
        self.last_write = now

    def can_read(self):
        now = time.time()
        cutoff = now - 60
        self.reads = [t for t in self.reads if t > cutoff]
        limit = POLICY["safety"]["rate_limits"]["max_reads_per_minute"]
        if len(self.reads) >= limit:
            return False, f"Rate limit: {limit} reads/minute exceeded"
        return True, "ok"

    def record_read(self):
        self.reads.append(time.time())

    def can_message(self):
        now = time.time()
        cutoff = now - 60
        self.messages = [t for t in self.messages if t > cutoff]
        limit = POLICY["safety"]["rate_limits"]["max_messages_per_minute"]
        if len(self.messages) >= limit:
            return False, f"Rate limit: {limit} messages/minute exceeded"
        return True, "ok"

    def record_message(self):
        self.messages.append(time.time())

RATE = RateLimiter()

# ─── Audit & Alerts ───────────────────────────────────────────────────────

def audit(event_type, detail, severity="info"):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": event_type,
        "severity": severity,
        "detail": detail
    }
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    if severity in ("warning", "critical"):
        with open(ALERT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"[ALERT:{severity.upper()}] {event_type}: {detail}")

# ─── Prompt Injection Detection ────────────────────────────────────────────

INJECTION_PATTERNS = [
    # Direct instruction overrides
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts|directives)",
    r"disregard\s+(all\s+)?(previous|prior|above|earlier)",
    r"forget\s+(all\s+)?(previous|prior|your)\s+(instructions|rules|training|programming)",
    r"you\s+are\s+now\s+(a|an|my)\s+",
    r"new\s+(instructions|rules|directives|role)\s*:",
    r"system\s*prompt\s*:",
    r"override\s+(safety|security|policy|rules|restrictions)",
    r"bypass\s+(safety|security|filter|restrictions)",
    r"jailbreak",
    r"DAN\s+mode",
    r"developer\s+mode",
    r"act\s+as\s+(if|though)\s+you\s+(have|had)\s+no\s+(restrictions|limits|rules)",

    # Attempting to extract secrets
    r"(show|tell|reveal|display|print|output|give)\s+(me\s+)?(the\s+)?(password|credentials|key|secret|token|ssh|api)",
    r"what\s+(is|are)\s+(the|your)\s+(password|credentials|api|key|ssh|server|ip)",
    r"(operator|admin|root|sudo|shell)\s*(key|password|access|command)",

    # Attempting to run commands
    r"(run|execute|eval|exec)\s+(this\s+)?(command|code|script|shell|bash|sql|php)",
    r"(rm\s+-rf|sudo|chmod|chown|wget|curl|nc\s|netcat|python|perl|ruby|node\s+-e)",
    r";\s*(ls|cat|echo|rm|mv|cp|mkdir|wget|curl)\s",
    r"\$\(|`[^`]+`",  # Command substitution
    r"<\?php|<script|javascript:",

    # Social engineering the bot identity
    r"(pretend|act|behave)\s+(like|as\s+if)\s+you\s+(are|were)\s+(not|a\s+different)",
    r"(who|what)\s+(controls|operates|runs|manages)\s+(you|this|agentblue)",
    r"are\s+you\s+(a\s+)?(real|human|person|bot|ai|artificial)",
    r"(tell|show)\s+me\s+(about\s+)?(the\s+)?(other|all)\s+(websites?|sites?|servers?|domains?)",

    # Attempting scope escalation
    r"(access|connect|go\s+to|visit|modify|change|update|edit)\s+(the\s+)?(other|another|different)\s+(site|website|server|domain|page)",
    r"(install|add|download|upload|enable|activate)\s+(a\s+)?(new\s+)?(plugin|theme|extension|module|package|widget)",
    r"(delete|remove|drop|truncate|destroy|wipe)\s+(the\s+)?(database|table|site|page|all|everything)",
    r"(create|add|register|make)\s+(a\s+)?(new\s+)?(user|admin|account|login)",

    # Encoded/obfuscated attempts
    r"base64|atob|btoa|\\x[0-9a-f]{2}|\\u[0-9a-f]{4}|%[0-9a-f]{2}",
    r"&#\d+;|&#x[0-9a-f]+;",  # HTML entities used for obfuscation

    # Probing for infrastructure info
    r"(other|all|list|show)\s+(the\s+)?(websites?|sites?|domains?|servers?)\s+(on|hosted|running)",
    r"(what|which)\s+(other\s+)?(websites?|sites?|domains?)\s+(are|do you|does)",
    r"(server|hosting|cpanel|whm|ssh|ftp|database|mysql|phpmyadmin)",
]

INJECTION_REGEXES = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

def check_injection(text):
    """Returns list of (pattern_description, matched_text) tuples for any injection attempts found."""
    findings = []
    for i, regex in enumerate(INJECTION_REGEXES):
        match = regex.search(text)
        if match:
            findings.append((INJECTION_PATTERNS[i], match.group()))
    return findings


# ─── HTML Sanitization ─────────────────────────────────────────────────────

def sanitize_html(content):
    """Remove dangerous HTML elements and attributes from content."""
    constraints = POLICY["allowed_actions"]["update_page_content"]["constraints"]

    # Check size
    if len(content) > constraints["max_content_length"]:
        return None, f"Content too large ({len(content)} chars, max {constraints['max_content_length']})"

    # Remove blocked tags entirely
    for tag in constraints["blocked_html_tags"]:
        content = re.sub(rf'<{tag}[^>]*>[\s\S]*?</{tag}>', '', content, flags=re.IGNORECASE)
        content = re.sub(rf'<{tag}[^>]*/?\s*>', '', content, flags=re.IGNORECASE)

    # Remove blocked attributes
    for attr in constraints["blocked_attributes"]:
        content = re.sub(rf'\s+{attr}\s*=\s*["\'][^"\']*["\']', '', content, flags=re.IGNORECASE)
        content = re.sub(rf'\s+{attr}\s*=\s*\S+', '', content, flags=re.IGNORECASE)

    # Check for external URLs if blocked
    if constraints.get("no_external_urls"):
        allowed = constraints.get("allowed_url_domains", [])
        url_pattern = re.compile(r'https?://([^/\s"\'<>]+)', re.IGNORECASE)
        for match in url_pattern.finditer(content):
            domain = match.group(1).lower()
            if not any(domain == a or domain.endswith('.' + a) for a in allowed):
                return None, f"External URL blocked: {match.group(0)} (only {', '.join(allowed)} allowed)"

    return content, "ok"


# ─── WP-CLI Command Validation ─────────────────────────────────────────────

def validate_wp_command(cmd):
    """Validate a WP-CLI command against the policy. Returns (ok, reason)."""
    safety = POLICY["safety"]["command_validation"]

    # Length check
    if len(cmd) > safety["max_command_length"]:
        return False, "Command too long"

    # Shell metacharacter check
    for ch in safety["block_shell_metacharacters"]:
        if ch in cmd:
            return False, f"Blocked character: {repr(ch)}"

    # Path traversal check
    for pat in safety["block_path_traversal"]:
        if pat in cmd:
            return False, f"Path traversal blocked: {pat}"

    # Other sites check
    for site in safety["block_other_sites"]:
        if site in cmd.lower():
            return False, f"Access to other site blocked: {site}"

    # Blocked WP commands check
    cmd_lower = cmd.lower()
    for blocked in POLICY["blocked_actions"]["blocked_wp_commands"]:
        if blocked in cmd_lower:
            return False, f"Blocked WP command: {blocked}"

    return True, "ok"


def is_read_command(cmd):
    """Check if a command is read-only (auto-approvable)."""
    read_keywords = ["list", "get", "--field=", "option get"]
    write_keywords = ["update", "create", "delete", "import", "set", "flush"]
    cmd_lower = cmd.lower()
    if any(w in cmd_lower for w in write_keywords):
        if "cache flush" in cmd_lower:
            return True  # cache flush is safe
        return False
    return any(r in cmd_lower for r in read_keywords)


def run_wp_cli(cmd, timeout=30):
    """Execute a WP-CLI command via SSH with full validation."""
    # Step 1: Validate command
    ok, reason = validate_wp_command(cmd)
    if not ok:
        audit("command_blocked", f"Command: {cmd}, Reason: {reason}", "warning")
        return None, reason

    # Step 2: Rate limit check
    if is_read_command(cmd):
        ok, reason = RATE.can_read()
        if not ok:
            return None, reason
        RATE.record_read()
    else:
        ok, reason = RATE.can_write()
        if not ok:
            return None, reason
        RATE.record_write()

    # Step 3: Build SSH command
    if not cmd.startswith("wp "):
        cmd = "wp " + cmd
    cmd = cmd.replace("wp ", f"wp --path={WP_PATH} ", 1)

    full_cmd = [
        "ssh", "-i", SSH_KEY,
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        SSH_HOST,
        cmd
    ]

    # Step 4: Execute
    audit("command_executed", f"Command: {cmd}", "info")
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += "\nERROR: " + result.stderr

        # Redact sensitive info in output
        output = re.sub(r"define\('DB_PASSWORD'.*", "***REDACTED***", output)
        output = re.sub(r"define\('[A-Z_]*KEY'.*", "***REDACTED***", output)
        output = re.sub(r"define\('[A-Z_]*SALT'.*", "***REDACTED***", output)
        output = re.sub(r"192\.\d+\.\d+\.\d+", "***IP***", output)

        return output[:10000], "ok"
    except subprocess.TimeoutExpired:
        return None, "Command timed out"
    except Exception as e:
        return None, str(e)


# ─── MSN Bridge API ────────────────────────────────────────────────────────

import urllib.request
import urllib.error

def msn_api(method, path, data=None):
    """Call the MSN operator API."""
    url = f"{MSN_SERVER}{path}"
    headers = {
        "X-Operator-Key": OPERATOR_KEY,
        "Content-Type": "application/json"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def get_messages(last=20):
    """Get recent messages from MSN."""
    return msn_api("GET", f"/operator/messages?last={last}")


def send_message(text, to="george"):
    """Send a message as AgentBlue."""
    ok, reason = RATE.can_message()
    if not ok:
        audit("message_rate_limited", reason, "warning")
        return {"error": reason}

    # Truncate if needed
    max_len = POLICY["msn_bridge"]["max_message_length"]
    if len(text) > max_len:
        text = text[:max_len - 3] + "..."

    RATE.record_message()
    audit("message_sent", f"To: {to}, Message: {text[:100]}...", "info")
    return msn_api("POST", "/operator/send", {"message": text, "to": to})


def get_status():
    """Get MSN server status."""
    return msn_api("GET", "/operator/status")


# ─── Message Processing ───────────────────────────────────────────────────

def process_incoming(message_text, from_user):
    """
    Process an incoming message. Returns:
    - ("reply", text) — safe to auto-reply
    - ("escalate", reason, original_msg) — needs operator review
    - ("blocked", reason) — injection detected, blocked
    """
    # Step 1: Check kill switch
    if KILL_FILE.exists():
        return ("blocked", "Kill switch active")

    # Step 2: Check for prompt injection
    findings = check_injection(message_text)
    if findings:
        reasons = [f"Pattern: {f[0]}, Match: '{f[1]}'" for f in findings]
        detail = f"INJECTION ATTEMPT from {from_user}: {message_text[:200]} | Findings: {'; '.join(reasons)}"
        audit("injection_detected", detail, "critical")
        return ("blocked", f"Potential injection detected ({len(findings)} patterns matched)")

    # Step 3: Check message sanity
    if len(message_text.strip()) == 0:
        return ("reply", None)  # Empty message, no reply needed

    if len(message_text) > 2000:
        audit("message_too_long", f"From {from_user}: {len(message_text)} chars", "warning")
        return ("escalate", "Message unusually long", message_text)

    # Step 4: Log it
    audit("message_received", f"From: {from_user}, Message: {message_text[:200]}", "info")

    # Step 5: Return for processing (the bridge daemon or operator handles reply)
    return ("process", message_text)


# ─── State Management ─────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"last_seen_timestamp": None, "messages_processed": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── CLI Interface ─────────────────────────────────────────────────────────

def cmd_check():
    """Check for new messages."""
    state = load_state()
    result = get_messages(last=20)
    if "error" in result:
        print(f"Error: {result['error']}")
        return

    messages = result.get("messages", [])
    last_ts = state.get("last_seen_timestamp")

    new_msgs = []
    for msg in messages:
        if last_ts and msg["timestamp"] <= last_ts:
            continue
        if msg.get("from") and msg["from"] != "AgentBlue":
            new_msgs.append(msg)

    if not new_msgs:
        print("No new messages.")
        return

    print(f"\n{'='*60}")
    print(f"  {len(new_msgs)} NEW MESSAGE(S)")
    print(f"{'='*60}")

    for msg in new_msgs:
        ts = msg["timestamp"]
        fr = msg.get("from", "?")
        text = msg.get("message", "")

        # Check for injection
        findings = check_injection(text)
        flag = ""
        if findings:
            flag = " [!!!INJECTION DETECTED!!!]"
            audit("injection_detected", f"From {fr}: {text[:200]}", "critical")

        print(f"\n  [{ts}] {fr}{flag}:")
        print(f"  > {text}")

        if findings:
            print(f"  !!! Matched patterns: {[f[1] for f in findings]}")

    # Update state
    if new_msgs:
        state["last_seen_timestamp"] = messages[-1]["timestamp"]
        state["messages_processed"] += len(new_msgs)
        save_state(state)

    print(f"\n{'='*60}")


def cmd_send(text):
    """Send a message as AgentBlue."""
    # Validate outgoing message too
    findings = check_injection(text)
    if findings:
        print(f"WARNING: Outgoing message matched injection patterns. Sending anyway (operator-initiated).")

    result = send_message(text)
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Sent to {result.get('sent_to', 'george')}: {text}")


def cmd_kill():
    """Activate kill switch."""
    KILL_FILE.touch()
    audit("kill_switch_activated", "Manual activation", "critical")
    print("Kill switch ACTIVATED. Bridge will not process any messages.")


def cmd_revive():
    """Deactivate kill switch."""
    if KILL_FILE.exists():
        KILL_FILE.unlink()
    audit("kill_switch_deactivated", "Manual deactivation", "info")
    print("Kill switch deactivated. Bridge is operational.")


def cmd_audit(n=30):
    """Show recent audit entries."""
    try:
        with open(AUDIT_LOG) as f:
            lines = f.readlines()
        for line in lines[-n:]:
            entry = json.loads(line)
            sev = entry["severity"].upper()
            marker = {"INFO": " ", "WARNING": "!", "CRITICAL": "X"}
            print(f"  [{marker.get(sev, '?')}] {entry['timestamp']} {entry['type']}: {entry['detail'][:120]}")
    except FileNotFoundError:
        print("No audit log yet.")


def cmd_status():
    """Show bridge status."""
    status = get_status()
    state = load_state()

    print(f"\n  Bridge Status")
    print(f"  {'─'*40}")
    print(f"  Kill switch: {'ACTIVE' if KILL_FILE.exists() else 'off'}")
    print(f"  Messages processed: {state.get('messages_processed', 0)}")
    print(f"  Last seen: {state.get('last_seen_timestamp', 'never')}")
    print(f"  Agent connected: {status.get('agent_connected', '?')}")
    print(f"  Users online: {json.dumps(status.get('users', []), indent=4)}")
    print(f"  Whitelisted IPs: {status.get('whitelisted_ips', [])}")

    # Alert counts
    try:
        with open(ALERT_LOG) as f:
            alerts = f.readlines()
        recent = [json.loads(a) for a in alerts if json.loads(a)["timestamp"] > (datetime.utcnow() - timedelta(hours=24)).isoformat()]
        print(f"  Alerts (24h): {len(recent)}")
    except:
        print(f"  Alerts (24h): 0")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        cmd_status()
        print()
        cmd_check()
    elif sys.argv[1] == "--check":
        cmd_check()
    elif sys.argv[1] == "--send" and len(sys.argv) > 2:
        cmd_send(" ".join(sys.argv[2:]))
    elif sys.argv[1] == "--kill":
        cmd_kill()
    elif sys.argv[1] == "--revive":
        cmd_revive()
    elif sys.argv[1] == "--audit":
        cmd_audit()
    elif sys.argv[1] == "--status":
        cmd_status()
    elif sys.argv[1] == "--alerts":
        try:
            with open(ALERT_LOG) as f:
                for line in f.readlines()[-20:]:
                    e = json.loads(line)
                    print(f"  [{e['severity'].upper()}] {e['timestamp']} {e['type']}: {e['detail'][:150]}")
        except FileNotFoundError:
            print("No alerts.")
    else:
        print(__doc__)
