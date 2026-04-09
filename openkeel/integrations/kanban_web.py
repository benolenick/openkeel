"""Flask web UI for the OpenKeel Kanban Board.

Features:
- Kanban board with drag-and-drop
- Agent registry with heartbeat tracking
- Agent-facing API (claim, report, heartbeat)
- Hyphae memory integration (recall/remember)
- Local only (127.0.0.1)

Run:
    python -m openkeel.integrations.kanban_web [--port 8200]
    openkeel board-web
"""
from __future__ import annotations

import glob as _glob_mod
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, request, render_template_string

from openkeel.integrations.kanban import Kanban, _VALID_STATUSES, _VALID_PRIORITIES, _VALID_TYPES

try:
    from openkeel.integrations.dispatcher_governance import GovernedDispatcher
    _governor: GovernedDispatcher | None = GovernedDispatcher()
except Exception:
    _governor = None  # Governance module not available — run without it

app = Flask(__name__)

# Mount Duo agent dashboard
try:
    from openkeel.agents.dashboard import mount_duo_dashboard
    mount_duo_dashboard(app)
except Exception:
    pass  # Duo dashboard not available

# ---------------------------------------------------------------------------
# Claude chat session (Anthropic SDK, streaming)
# ---------------------------------------------------------------------------

_chat_histories: dict[str, list] = {}  # session_id -> messages
_CHAT_SYSTEM_PROMPT = """You are the OpenKeel assistant, embedded in the OpenKeel Command Board web UI.
You help the user manage their tasks, agents, infrastructure, and projects.

You have access to the kanban board at http://127.0.0.1:8200/api/ and Hyphae memory at http://127.0.0.1:8100/.

When the user asks about tasks, projects, or memory, you can reference what's on the board.
Keep responses concise and actionable. You're a command center copilot, not a general chatbot.

## Dispatch Actions
When the user asks you to create tasks, assign agents, or check status, emit ACTION blocks.
The frontend will intercept and execute these automatically. Always include a brief explanation too.

To create a task:
ACTION:CREATE_TASK{"title":"task title","description":"details","priority":"medium","project":"project-name","board":"default"}

To assign a task to an agent:
ACTION:ASSIGN_TASK{"task_id":123,"agent":"agent-name"}

To move a task:
ACTION:MOVE_TASK{"task_id":123,"status":"done"}

To check all agents:
ACTION:CHECK_AGENTS{}

To send a directive to an agent (they pick it up on next heartbeat):
ACTION:DIRECTIVE{"agent":"agent-name","message":"please prioritize task #23","priority":"normal"}

Priority can be "normal" or "urgent". Urgent directives should be used sparingly.

Examples:
- User: "create a task to fix the Paris scraper" → emit ACTION:CREATE_TASK with appropriate fields
- User: "assign task 15 to claude-ops" → emit ACTION:ASSIGN_TASK
- User: "mark task 12 as done" → emit ACTION:MOVE_TASK
- User: "tell claude-ops to stop and work on the InBloom bug" → emit ACTION:DIRECTIVE with urgent priority
- User: "have the agents focus on infrastructure tasks" → emit directives to all busy/idle agents"""

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_kb: Kanban | None = None
_HYPHAE_URL = "http://127.0.0.1:8100"
_HEARTBEAT_TIMEOUT = 120  # seconds before agent considered stalled

# Agent registry: {agent_name: {status, last_heartbeat, current_task, registered_at, capabilities}}
_agents: dict[str, dict] = {}
_agents_lock = threading.Lock()

# Agent commentary log (in-memory, last 500 entries)
_commentary: list[dict] = []
_commentary_lock = threading.Lock()

# Commands that should be filtered from commentary
_CMD_PREFIXES = (
    "curl ", "ssh ", "scp ", "docker ", "git ", "sudo ", "pip ", "npm ",
    "cat ", "grep ", "find ", "ls ", "cd /", "chmod ", "chown ", "mkdir ",
    "rm ", "mv ", "cp ", "sed ", "awk ", "echo ", "python ", "python3 ",
    "bash ", "sh ", "apt ", "brew ", "wget ", "tar ", "kill ", "ps ",
    "systemctl ", "journalctl ", "mount ", "umount ",
)


def _is_command(text: str) -> bool:
    """Heuristic: reject messages that look like raw shell commands."""
    stripped = text.strip()
    if not stripped:
        return True
    # Starts with a known command prefix
    lower = stripped.lower()
    if any(lower.startswith(p) for p in _CMD_PREFIXES):
        return True
    # Mostly non-alpha (pipes, paths, flags)
    alpha_ratio = sum(1 for c in stripped if c.isalpha()) / max(len(stripped), 1)
    if alpha_ratio < 0.3 and len(stripped) > 20:
        return True
    return False


# Agent directive queue: {agent_name: [{"message", "from", "timestamp", "priority"}]}
_directives: dict[str, list[dict]] = {}
_directives_lock = threading.Lock()


_conv_db_init = False


def _get_conv_db():
    """Get SQLite connection for agent conversations (persistent)."""
    global _conv_db_init
    kb = _get_kb()
    if not _conv_db_init:
        kb._conn.execute("""CREATE TABLE IF NOT EXISTS agent_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            direction TEXT NOT NULL,
            message TEXT NOT NULL,
            from_who TEXT DEFAULT '',
            task_id INTEGER,
            priority TEXT DEFAULT 'normal',
            timestamp REAL NOT NULL
        )""")
        kb._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conv_agent ON agent_conversations(agent, timestamp)""")
        kb._conn.commit()
        _conv_db_init = True
    return kb._conn


# ---------------------------------------------------------------------------
# Topic channels DB (persistent conversations)
# ---------------------------------------------------------------------------

_topics_db_init = False


def _get_topics_db():
    """Get SQLite connection for topic channels (persistent)."""
    global _topics_db_init
    kb = _get_kb()
    if not _topics_db_init:
        kb._conn.execute("""CREATE TABLE IF NOT EXISTS topic_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            project TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at REAL,
            updated_at REAL,
            pinned INTEGER DEFAULT 0,
            model TEXT DEFAULT 'sonnet'
        )""")
        try:
            kb._conn.execute("ALTER TABLE topic_channels ADD COLUMN model TEXT DEFAULT 'sonnet'")
        except Exception:
            pass
        kb._conn.execute("""CREATE TABLE IF NOT EXISTS topic_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            agent_name TEXT DEFAULT '',
            content TEXT NOT NULL,
            timestamp REAL,
            FOREIGN KEY (channel_id) REFERENCES topic_channels(id)
        )""")
        kb._conn.execute("""CREATE INDEX IF NOT EXISTS idx_topic_msgs_channel
            ON topic_messages(channel_id, timestamp)""")
        kb._conn.commit()
        _topics_db_init = True
    return kb._conn


def _persist_conversation(agent: str, direction: str, message: str,
                          from_who: str = "", task_id: int | None = None,
                          priority: str = "normal") -> None:
    """Save a conversation entry (inbound directive or outbound commentary)."""
    try:
        conn = _get_conv_db()
        conn.execute(
            "INSERT INTO agent_conversations (agent, direction, message, from_who, task_id, priority, timestamp) VALUES (?,?,?,?,?,?,?)",
            (agent, direction, message, from_who, task_id, priority, time.time()),
        )
        conn.commit()
    except Exception:
        pass


def _send_directive(to_agent: str, message: str, from_agent: str = "dispatcher", priority: str = "normal") -> bool:
    """Queue a directive for an agent. Agent picks it up on next heartbeat."""
    with _directives_lock:
        if to_agent not in _directives:
            _directives[to_agent] = []
        _directives[to_agent].append({
            "message": message,
            "from": from_agent,
            "priority": priority,  # normal, urgent
            "timestamp": _now(),
        })
        # Cap at 20 pending directives per agent
        if len(_directives[to_agent]) > 20:
            _directives[to_agent] = _directives[to_agent][-20:]
    # Persist to conversation log
    _persist_conversation(to_agent, "inbound", message, from_who=from_agent, priority=priority)
    return True


def _pop_directives(agent: str) -> list[dict]:
    """Pop all pending directives for an agent (called on heartbeat)."""
    with _directives_lock:
        items = _directives.pop(agent, [])
    return items


def _add_commentary(agent: str, message: str, task_id: int | None = None) -> bool:
    """Add a commentary entry if it passes the filter. Returns True if accepted."""
    if _is_command(message):
        return False
    with _commentary_lock:
        _commentary.insert(0, {
            "agent": agent,
            "message": message,
            "task_id": task_id,
            "timestamp": _now(),
        })
        if len(_commentary) > 500:
            _commentary.pop()
    # Persist to conversation log
    _persist_conversation(agent, "outbound", message, task_id=task_id)
    return True

# ---------------------------------------------------------------------------
# Infrastructure monitoring config
# ---------------------------------------------------------------------------

_INFRA_SERVICES: list[dict] = [
    # Local services (this machine)
    {"name": "Hyphae", "host": "local", "url": "http://127.0.0.1:8100/health", "type": "api",
     "desc": "Long-term memory (37k+ facts)", "category": "memory"},
    {"name": "Embeddings Server", "host": "local", "url": "http://127.0.0.1:7437/status", "type": "api",
     "desc": "Semantic search (all-MiniLM-L6-v2)", "category": "memory"},
    {"name": "Kanban Web", "host": "local", "url": "http://127.0.0.1:8200/api/stats", "type": "api",
     "desc": "This dashboard", "category": "openkeel"},
    # jagg services (192.168.0.224)
    {"name": "Memoria", "host": "jagg", "url": "http://192.168.0.224:8000/health", "type": "api",
     "desc": "6.26M fact FAISS vector store", "category": "memory",
     "note": "Listens on 127.0.0.1 only — check via SSH"},
    {"name": "Glances", "host": "jagg", "url": "http://192.168.0.224:8855", "type": "api",
     "desc": "System monitoring (CPU/RAM/GPU)", "category": "infra"},
    {"name": "Wazuh", "host": "jagg", "url": None, "type": "process",
     "desc": "HIDS agent", "category": "security", "check_cmd": "wazuh-apid"},
    {"name": "Argus", "host": "jagg", "url": None, "type": "process",
     "desc": "Endpoint agent (17 monitors)", "category": "security"},
    {"name": "Security Shallots", "host": "jagg", "url": None, "type": "process",
     "desc": "Security ops dashboard", "category": "security"},
    # Network
    {"name": "jagg", "host": "jagg", "url": None, "type": "ping", "ip": "192.168.0.224",
     "desc": "Dual 3090 server", "category": "infra"},
    {"name": "kagg", "host": "kagg", "url": None, "type": "ping", "ip": "192.168.0.204",
     "desc": "NAS", "category": "infra"},
    {"name": "pfsense", "host": "pfsense", "url": None, "type": "ping", "ip": "192.168.0.1",
     "desc": "Firewall/router", "category": "infra"},
]

# Cache for health results
_health_cache: dict[str, dict] = {}
_health_lock = threading.Lock()


def _get_kb() -> Kanban:
    global _kb
    if _kb is None:
        _kb = Kanban()
    return _kb


def _now() -> float:
    return time.time()


def _ts_fmt(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _agent_effective_status(agent: dict) -> str:
    """Compute effective status based on heartbeat freshness."""
    if agent["status"] == "offline":
        return "offline"
    elapsed = _now() - agent.get("last_heartbeat", 0)
    if elapsed > _HEARTBEAT_TIMEOUT * 3:
        return "offline"
    if elapsed > _HEARTBEAT_TIMEOUT:
        return "stalled"
    return agent["status"]


# ---------------------------------------------------------------------------
# Hyphae helpers
# ---------------------------------------------------------------------------

def _hyphae_recall(query: str, top_k: int = 10, scoped: bool = True) -> list[dict]:
    try:
        payload = {"query": query, "top_k": top_k}
        if not scoped:
            payload["scope"] = {}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_HYPHAE_URL}/recall",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("results", [])
    except Exception:
        return []


def _hyphae_remember(text: str, source: str = "board") -> bool:
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{_HYPHAE_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Infrastructure health checks
# ---------------------------------------------------------------------------

def _check_url(url: str, timeout: float = 3) -> dict:
    """Check an HTTP endpoint, return {ok, latency_ms, detail}."""
    try:
        t0 = time.time()
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            latency = round((time.time() - t0) * 1000)
            try:
                detail = json.loads(body)
            except Exception:
                detail = {"raw": body.decode("utf-8", errors="replace")[:200]}
            return {"ok": True, "latency_ms": latency, "detail": detail}
    except Exception as e:
        return {"ok": False, "latency_ms": 0, "detail": str(e)[:100]}


def _check_ping(ip: str, timeout: float = 2) -> dict:
    """Ping a host, return {ok, latency_ms}."""
    import subprocess
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), ip],
            capture_output=True, text=True, timeout=timeout + 1,
        )
        if result.returncode == 0:
            # Extract latency from "time=X.XX ms"
            import re
            m = re.search(r"time[=<]([\d.]+)", result.stdout)
            latency = float(m.group(1)) if m else 0
            return {"ok": True, "latency_ms": round(latency)}
        return {"ok": False, "latency_ms": 0}
    except Exception:
        return {"ok": False, "latency_ms": 0}


def _check_ssh_process(host_ip: str, process_name: str, timeout: float = 3) -> dict:
    """Check if a process is running on a remote host via SSH."""
    import subprocess
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
             f"om@{host_ip}", f"pgrep -f '{process_name}' | head -1"],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        pid = result.stdout.strip()
        return {"ok": bool(pid), "detail": f"PID {pid}" if pid else "not running"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


_HOST_IPS = {
    "jagg": "192.168.0.224",
    "kagg": "192.168.0.204",
    "pfsense": "192.168.0.1",
}


def run_health_checks() -> list[dict]:
    """Run all infrastructure health checks. Returns list of results."""
    results = []
    for svc in _INFRA_SERVICES:
        r = {"name": svc["name"], "host": svc["host"], "desc": svc["desc"],
             "category": svc.get("category", ""), "type": svc["type"],
             "note": svc.get("note", "")}

        if svc["type"] == "api" and svc.get("url"):
            check = _check_url(svc["url"])
            r.update(check)
        elif svc["type"] == "ping":
            ip = svc.get("ip", _HOST_IPS.get(svc["host"], ""))
            check = _check_ping(ip)
            r.update(check)
        elif svc["type"] == "process":
            ip = _HOST_IPS.get(svc["host"], "")
            check_name = svc.get("check_cmd", svc["name"].lower().replace(" ", "_"))
            check = _check_ssh_process(ip, check_name)
            r.update(check)
        else:
            r["ok"] = None

        r["checked_at"] = _now()
        results.append(r)

    with _health_lock:
        for r in results:
            _health_cache[r["name"]] = r

    return results


# ---------------------------------------------------------------------------
# HTML — full single-page app with tabs
# ---------------------------------------------------------------------------

BOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenKeel Command</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --surface2: #1c2128;
  --border: #30363d;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --accent: #f97316;
  --accent-dim: #c2410c;
  --todo: #3b82f6;
  --progress: #f59e0b;
  --done: #22c55e;
  --blocked: #ef4444;
  --critical: #ef4444;
  --high: #f97316;
  --medium: #3b82f6;
  --low: #8b949e;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* Header */
header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 10px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
}
header h1 { font-size: 16px; font-weight: 600; color: var(--accent); letter-spacing: 0.5px; }
header h1 span { color: var(--text); font-weight: 400; }

/* Tabs */
.tabs {
  display: flex;
  gap: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 78px; z-index: 99;
}
.tab {
  padding: 10px 20px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-dim);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-badge {
  background: var(--accent);
  color: #fff;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 8px;
  margin-left: 5px;
}

.tab-content { display: none; flex: 1; }
.tab-content.active { display: flex; flex-direction: column; flex: 1; }

/* Controls row */
.controls {
  display: flex; gap: 8px; align-items: center; padding: 8px 16px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.controls select, .controls input[type="text"] {
  background: var(--surface2); border: 1px solid var(--border); color: var(--text);
  padding: 5px 8px; border-radius: 6px; font-size: 12px;
}
.btn {
  background: var(--accent); color: #fff; border: none; padding: 6px 12px;
  border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500;
  transition: background 0.15s; white-space: nowrap;
}
.btn:hover { background: var(--accent-dim); }
.btn-sm { padding: 4px 8px; font-size: 11px; }
.btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text-dim); }
.btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
.btn-danger { background: var(--blocked); }

/* Usage tracker banner */
.usage-banner {
  display: flex; align-items: center; gap: 16px;
  padding: 6px 20px;
  background: linear-gradient(90deg, rgba(249,115,22,0.08) 0%, rgba(59,130,246,0.06) 100%);
  border-bottom: 1px solid var(--border);
  font-size: 11px; color: var(--text-dim);
  position: sticky; top: 44px; z-index: 100;
  overflow-x: auto;
  white-space: nowrap;
}
.usage-banner .usage-label {
  font-weight: 600; color: var(--accent); text-transform: uppercase;
  letter-spacing: 0.5px; font-size: 10px; flex-shrink: 0;
}
.usage-banner .usage-group {
  display: flex; align-items: center; gap: 6px;
  padding: 2px 10px;
  background: rgba(255,255,255,0.03);
  border-radius: 6px; border: 1px solid rgba(255,255,255,0.04);
}
.usage-banner .usage-val {
  font-weight: 600; color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
}
.usage-banner .usage-val.cost { color: var(--accent); }
.usage-banner .usage-val.tokens { color: var(--done); }
.usage-banner .usage-sep {
  width: 1px; height: 16px; background: var(--border); flex-shrink: 0;
}
.usage-sparkline {
  display: flex; align-items: flex-end; gap: 2px; height: 20px; flex-shrink: 0;
}
.usage-sparkline .bar {
  width: 6px; border-radius: 2px 2px 0 0; background: var(--accent);
  min-height: 2px; transition: height 0.3s;
  opacity: 0.6;
}
.usage-sparkline .bar:last-child { opacity: 1; }
.usage-sparkline .bar:hover { opacity: 1; }
.usage-toggle {
  margin-left: auto; flex-shrink: 0;
  font-size: 10px; color: var(--text-dim); cursor: pointer;
  padding: 2px 6px; border-radius: 4px;
  border: 1px solid transparent;
}
.usage-toggle:hover { border-color: var(--border); color: var(--text); }

/* Stats bar */
.stats-bar {
  display: flex; gap: 14px; padding: 8px 16px;
  font-size: 12px; color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}
.stat-item { display: flex; align-items: center; gap: 4px; }
.stat-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }

/* Board layout */
.board {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 0; flex: 1; min-height: 0;
}
.column {
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; min-height: 300px;
}
.column:last-child { border-right: none; }
.column-header {
  padding: 10px 12px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px;
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 2px solid var(--border);
  background: var(--bg); position: sticky; top: 122px; z-index: 50;
}
.column-header .count {
  background: var(--surface2); border-radius: 10px;
  padding: 1px 7px; font-size: 10px; color: var(--text-dim);
}
.col-todo .column-header { border-bottom-color: var(--todo); }
.col-in_progress .column-header { border-bottom-color: var(--progress); }
.col-done .column-header { border-bottom-color: var(--done); }
.col-blocked .column-header { border-bottom-color: var(--blocked); }
.column-body { padding: 6px; flex: 1; overflow-y: auto; }
.column-body.drag-over { background: rgba(249,115,22,0.05); }

/* Task card */
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px; margin-bottom: 5px;
  cursor: grab; transition: border-color 0.15s; position: relative;
}
.card:hover { border-color: var(--accent); }
.card.dragging { opacity: 0.4; }
.card-id { font-size: 10px; color: var(--text-dim); margin-bottom: 3px;
  display: flex; align-items: center; justify-content: space-between; }
.card-title { font-size: 13px; font-weight: 500; margin-bottom: 4px; line-height: 1.3; }
.card-meta { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
.badge {
  font-size: 9px; padding: 1px 5px; border-radius: 3px;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
}
.badge-critical { background: rgba(239,68,68,0.15); color: var(--critical); }
.badge-high { background: rgba(249,115,22,0.15); color: var(--high); }
.badge-medium { background: rgba(59,130,246,0.15); color: var(--medium); }
.badge-low { background: rgba(139,149,158,0.15); color: var(--low); }
.badge-type { background: rgba(139,149,158,0.1); color: var(--text-dim); }
.badge-assignee { background: rgba(249,115,22,0.1); color: var(--accent); }
.card-desc { font-size: 11px; color: var(--text-dim); margin-top: 4px;
  line-height: 1.3; max-height: 32px; overflow: hidden; }
.card-actions { display: none; position: absolute; top: 6px; right: 6px; gap: 3px; }
.card:hover .card-actions { display: flex; }
.card-btn {
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--text-dim); width: 22px; height: 22px; border-radius: 4px;
  cursor: pointer; font-size: 11px; display: flex; align-items: center; justify-content: center;
}
.card-btn:hover { color: var(--accent); border-color: var(--accent); }
.card-heartbeat { font-size: 10px; color: var(--text-dim); margin-top: 3px; }

/* Agents tab */
.agents-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px; padding: 16px;
}
.agents-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; padding: 16px;
}
.agent-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px; position: relative; overflow: hidden;
  transition: border-color 0.3s;
}
.agent-card.busy { border-color: var(--done); }
.agent-card.idle { border-color: var(--blocked); }
.agent-card.stalled { border-color: var(--progress); }
.agent-card.offline { border-color: var(--text-dim); opacity: 0.6; }
.agent-card h3 { font-size: 15px; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }
.agent-status {
  width: 14px; height: 14px; border-radius: 50%; display: inline-block;
  flex-shrink: 0;
}
.status-idle { background: var(--blocked); animation: idlePulse 2s ease-in-out infinite; }
.status-busy { background: var(--done); animation: busyPulse 1.5s ease-in-out infinite; box-shadow: 0 0 8px var(--done); }
.status-stalled { background: var(--progress); animation: stalledBlink 1s step-end infinite; }
.status-offline { background: #555; }
@keyframes busyPulse { 0%,100%{box-shadow:0 0 4px var(--done)} 50%{box-shadow:0 0 16px var(--done)} }
@keyframes idlePulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
@keyframes stalledBlink { 0%{opacity:1} 50%{opacity:0.2} }
.agent-status-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
}
.agent-status-label.busy { color: var(--done); }
.agent-status-label.idle { color: var(--blocked); }
.agent-status-label.stalled { color: var(--progress); }
.agent-status-label.offline { color: var(--text-dim); }
.agent-meta { font-size: 12px; color: var(--text-dim); line-height: 1.8; }
.agent-meta strong { color: var(--text); }
.agent-task-badge {
  display: inline-block; background: rgba(59,130,246,0.15); color: var(--todo);
  font-size: 11px; padding: 2px 8px; border-radius: 4px; margin-top: 4px;
}
.agent-directive-badge {
  display: inline-block; background: rgba(249,115,22,0.15); color: var(--accent);
  font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-top: 4px;
}
.agent-card .agent-glow {
  position: absolute; top: -50%; left: -50%; width: 200%; height: 200%;
  background: radial-gradient(circle, transparent 60%, currentColor 100%);
  opacity: 0.03; pointer-events: none;
}
.agent-card.busy .agent-glow { color: var(--done); opacity: 0.06; }

/* Inline conversation summary on agent cards */
.agent-summary {
  margin-top: 10px; padding: 8px 10px;
  background: rgba(0,0,0,0.3); border-radius: 6px;
  border-left: 2px solid var(--accent);
  max-height: 140px; overflow-y: auto;
}
.agent-summary-line {
  font-size: 11px; line-height: 1.6; color: var(--text);
  padding: 2px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
}
.agent-summary-line:last-child { border-bottom: none; }
.agent-summary-line .summary-time {
  color: var(--text-dim); font-size: 10px; margin-right: 6px;
  font-family: monospace;
}

/* Memory tab */
.memory-panel { padding: 16px; display: flex; flex-direction: column; gap: 12px; flex: 1; }
.memory-search {
  display: flex; gap: 8px;
}
.memory-search input {
  flex: 1; background: var(--surface2); border: 1px solid var(--border);
  color: var(--text); padding: 10px 14px; border-radius: 8px; font-size: 14px;
}
.memory-results {
  flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;
}
.memory-result {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; font-size: 13px; line-height: 1.5;
}
.memory-result .score {
  font-size: 10px; color: var(--accent); font-weight: 600;
  float: right; margin-left: 8px;
}
.memory-result .source {
  font-size: 10px; color: var(--text-dim); margin-top: 4px;
}
.memory-remember {
  display: flex; gap: 8px;
}
.memory-remember textarea {
  flex: 1; background: var(--surface2); border: 1px solid var(--border);
  color: var(--text); padding: 10px; border-radius: 8px; font-size: 13px;
  font-family: inherit; min-height: 60px; resize: vertical;
}

/* Activity feed */
.activity-feed {
  padding: 16px; flex: 1; overflow-y: auto;
}
.activity-item {
  padding: 8px 0; border-bottom: 1px solid var(--border);
  font-size: 12px; color: var(--text-dim);
}
.activity-item .time { color: var(--text-dim); margin-right: 8px; font-size: 11px; }
.activity-item .agent-name { color: var(--accent); font-weight: 500; }

/* Modal */
.modal-overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.6); z-index: 200;
  align-items: center; justify-content: center;
}
.modal-overlay.active { display: flex; }
.modal {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 20px; width: 480px;
  max-width: 95vw; max-height: 85vh; overflow-y: auto;
}
.modal h2 { font-size: 15px; margin-bottom: 14px; color: var(--accent); }
.modal label {
  display: block; font-size: 11px; color: var(--text-dim);
  margin-bottom: 3px; margin-top: 10px; text-transform: uppercase; letter-spacing: 0.5px;
}
.modal input, .modal select, .modal textarea {
  width: 100%; background: var(--surface2); border: 1px solid var(--border);
  color: var(--text); padding: 7px 9px; border-radius: 6px; font-size: 13px; font-family: inherit;
}
.modal textarea { min-height: 70px; resize: vertical; }
.modal-buttons { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }

/* Toast */
.toast {
  position: fixed; bottom: 16px; right: 16px;
  background: var(--surface); border: 1px solid var(--accent);
  color: var(--text); padding: 8px 16px; border-radius: 8px;
  font-size: 12px; z-index: 300; opacity: 0;
  transform: translateY(10px); transition: all 0.2s;
}
.toast.show { opacity: 1; transform: translateY(0); }

/* Monitor cards */
.health-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; display: flex; align-items: flex-start; gap: 10px;
}
.health-card.up { border-left: 3px solid var(--done); }
.health-card.down { border-left: 3px solid var(--blocked); }
.health-card.unknown { border-left: 3px solid var(--text-dim); }
.health-icon { font-size: 18px; flex-shrink: 0; margin-top: 2px; }
.health-info { flex: 1; }
.health-info h4 { font-size: 13px; font-weight: 500; margin-bottom: 2px; }
.health-info .desc { font-size: 11px; color: var(--text-dim); }
.health-info .detail { font-size: 11px; color: var(--text-dim); margin-top: 4px; font-family: monospace; }
.health-info .latency { font-size: 10px; color: var(--accent); }
.health-category {
  font-size: 10px; padding: 1px 6px; border-radius: 3px;
  background: rgba(139,149,158,0.1); color: var(--text-dim);
  text-transform: uppercase; letter-spacing: 0.5px; margin-left: auto; flex-shrink: 0;
}
.health-summary-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; text-align: center; min-width: 100px;
}
.health-summary-card .num { font-size: 24px; font-weight: 600; }
.health-summary-card .label { font-size: 11px; color: var(--text-dim); }

/* Automation cards on Monitor tab */
.automation-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; display: flex; align-items: center; gap: 10px;
  border-left: 3px solid var(--done);
}
.automation-card.broken { border-left-color: var(--blocked); }
.auto-dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  background: var(--done); animation: autoPulse 2s ease-in-out infinite;
}
.automation-card.broken .auto-dot { background: var(--blocked); animation: none; }
@keyframes autoPulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
.auto-info { flex: 1; }
.auto-info h4 { font-size: 13px; font-weight: 500; margin-bottom: 2px; }
.auto-info .desc { font-size: 11px; color: var(--text-dim); }

/* Commentary feed on Agents tab */
.commentary-feed {
  padding: 16px; flex: 1; overflow-y: auto; max-height: 400px;
}
.commentary-item {
  padding: 10px 0; border-bottom: 1px solid var(--border);
  font-size: 13px; line-height: 1.5;
}
.commentary-item .c-time { font-size: 10px; color: var(--text-dim); margin-right: 8px; }
.commentary-item .c-agent { color: var(--accent); font-weight: 600; margin-right: 4px; }
.commentary-item .c-msg { color: var(--text); }
.commentary-item .c-task { font-size: 10px; color: var(--text-dim); margin-left: 4px; }

/* Conversation / Topic channels view */
.conv-layout { display:flex; height:calc(100vh - 160px); overflow:hidden; }
.conv-sidebar { width:250px; border-right:1px solid var(--border); overflow-y:auto; flex-shrink:0;
  display:flex; flex-direction:column; }
.conv-sidebar-title { padding:12px 14px; font-size:11px; font-weight:600; text-transform:uppercase;
  letter-spacing:1px; color:var(--accent); border-bottom:1px solid var(--border);
  display:flex; align-items:center; }
.topic-list { flex:1; overflow-y:auto; }
.topic-item { padding:12px 14px; cursor:pointer; border-bottom:1px solid var(--border);
  transition:background 0.1s; }
.topic-item:hover { background:var(--surface2); }
.topic-item.active { background:rgba(88,166,255,0.12); border-left:3px solid var(--accent); }
.topic-item .topic-name { font-size:13px; font-weight:500; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.topic-item .topic-preview { font-size:11px; color:var(--text-dim); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; max-width:210px; margin-top:2px; }
.topic-item .topic-time { font-size:10px; color:var(--text-dim); margin-top:2px; }
.topic-item.pinned .topic-name::before { content:"📌 "; font-size:10px; }
.conv-main { flex:1; display:flex; flex-direction:column; min-width:0; }
.conv-header { padding:10px 16px; border-bottom:1px solid var(--border); display:flex;
  align-items:center; gap:8px; }
.model-toggle { display:inline-flex; background:var(--surface); border:1px solid var(--border);
  border-radius:6px; overflow:hidden; margin-left:12px; }
.model-btn { padding:4px 14px; font-size:12px; border:none; background:transparent;
  color:var(--text-dim); cursor:pointer; transition:all 0.15s; }
.model-btn.active[data-model="sonnet"] { background:var(--accent); color:#fff; }
.model-btn.active[data-model="opus"] { background:#8b5cf6; color:#fff; }
.topic-model-indicator { font-size:10px; margin-left:4px; opacity:0.7; }
.conv-agent-name { font-weight:600; font-size:14px; }
.conv-messages { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:8px; }
.topic-empty-state { color:var(--text-dim); text-align:center; padding:60px 20px; font-size:13px; }
/* Message bubbles */
.msg-bubble { max-width:80%; padding:10px 14px; border-radius:12px; font-size:13px; line-height:1.5;
  word-break:break-word; position:relative; white-space:pre-wrap; }
.msg-bubble.role-user { align-self:flex-end; background:var(--accent); color:#fff;
  border-bottom-right-radius:4px; }
.msg-bubble.role-assistant { align-self:flex-start; background:var(--surface2); color:var(--text);
  border-bottom-left-radius:4px; }
.msg-bubble.role-agent { align-self:flex-start; background:rgba(139,92,246,0.15); color:var(--text);
  border-bottom-left-radius:4px; border:1px solid rgba(139,92,246,0.3); }
.msg-bubble.role-agent .agent-badge { display:inline-block; font-size:10px; font-weight:600;
  background:rgba(139,92,246,0.3); color:#a78bfa; padding:1px 6px; border-radius:4px; margin-bottom:4px; }
.msg-bubble.role-system { align-self:center; background:transparent; color:var(--text-dim);
  font-size:11px; text-align:center; max-width:90%; padding:6px 10px; }
.msg-meta { font-size:10px; margin-top:4px; opacity:0.7; }
.msg-bubble.role-user .msg-meta { color:rgba(255,255,255,0.7); }
.conv-day-divider { text-align:center; font-size:10px; color:var(--text-dim); padding:8px 0;
  text-transform:uppercase; letter-spacing:1px; }
.conv-input { padding:10px 16px; border-top:1px solid var(--border); display:flex; gap:8px; }
.conv-input input { flex:1; background:var(--surface2); border:1px solid var(--border); color:var(--text);
  padding:10px 12px; border-radius:8px; font-size:13px; }

@media (max-width: 900px) {
  .board { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 600px) {
  .board { grid-template-columns: 1fr; }
  .column { border-right: none; border-bottom: 1px solid var(--border); min-height: 200px; }

  /* Mobile touch targets */
  header { padding: 8px 12px; }
  header h1 { font-size: 14px; }
  .tabs { overflow-x: auto; -webkit-overflow-scrolling: touch; white-space: nowrap; }
  .tab { padding: 12px 14px; font-size: 13px; min-height: 44px; display:inline-flex; align-items:center; }
  .btn { padding: 12px 16px; font-size: 13px; min-height: 44px; }
  .btn-sm { padding: 10px 14px; min-height: 44px; }
  .card { padding: 14px; }
  .card-actions { display: flex !important; }
  .controls { padding: 8px 12px; flex-wrap: wrap; }
  .controls select, .controls input { font-size: 14px; padding: 10px; }
  .agents-grid { grid-template-columns: 1fr !important; padding: 12px; }

  /* Chat/Security panels: full width on mobile */
  #chatPanel, #secPanel { width: 100vw !important; right: 0 !important; bottom: 0 !important;
    max-height: 80vh; border-radius: 12px 12px 0 0; }
  #chatBubble, #secBubble { width: 48px; height: 48px; }

  /* Conversations: sidebar collapses on mobile */
  .conv-layout { flex-direction: column; height: auto; }
  .conv-sidebar { width:100%; max-height:150px; border-right:none; border-bottom:1px solid var(--border); }
  .conv-messages { min-height: 50vh; max-height: 60vh; }
  .msg-bubble { max-width: 90%; }
  .conv-input input { font-size: 16px; }
}
</style>
</head>
<body>

<header>
  <h1>OPENKEEL <span>Command</span></h1>
  <div style="display:flex;gap:8px;align-items:center;">
    <span id="hyphaeStatus" style="font-size:11px;color:var(--text-dim)"></span>
  </div>
</header>

<!-- Usage Tracker -->
<div class="usage-banner" id="usageBanner">
  <span class="usage-label">Claude Usage</span>
  <div class="usage-group">
    <span>Today</span>
    <span class="usage-val cost" id="usageTodayCost">--</span>
  </div>
  <div class="usage-group">
    <span>Tokens</span>
    <span class="usage-val tokens" id="usageTodayTokens">--</span>
  </div>
  <div class="usage-group">
    <span>Requests</span>
    <span class="usage-val" id="usageTodayReqs">--</span>
  </div>
  <div class="usage-sep"></div>
  <div class="usage-group">
    <span>7d</span>
    <span class="usage-val cost" id="usageWeekCost">--</span>
  </div>
  <div class="usage-group">
    <span>Sessions</span>
    <span class="usage-val" id="usageWeekSessions">--</span>
  </div>
  <div class="usage-sep"></div>
  <div class="usage-sparkline" id="usageSparkline" title="Cost per day (last 7 days)"></div>
  <div class="usage-sep"></div>
  <div class="usage-group">
    <span>All time</span>
    <span class="usage-val cost" id="usageAllCost">--</span>
  </div>
  <span class="usage-toggle" id="usageToggle" onclick="toggleUsageDetail()">details</span>
</div>

<!-- Tabs -->
<div class="tabs" style="display:flex;justify-content:space-evenly;">
  <div class="tab active" data-tab="board" onclick="switchTab('board')" style="flex:1;text-align:center;">Board</div>
  <div class="tab" data-tab="conversations" onclick="switchTab('conversations')" style="flex:1;text-align:center;">Conversations</div>
  <div class="tab" data-tab="agents" onclick="switchTab('agents')" style="flex:1;text-align:center;">Agents <span class="tab-badge" id="agentCount">0</span></div>
  <div class="tab" data-tab="governance" onclick="switchTab('governance')" style="flex:1;text-align:center;">Governance <span class="tab-badge" id="govPendingCount" style="display:none">0</span></div>
  <div class="tab" data-tab="monitor" onclick="switchTab('monitor')" style="flex:1;text-align:center;">Monitor</div>
  <div class="tab" data-tab="processes" onclick="switchTab('processes')" style="flex:1;text-align:center;">Processes <span class="tab-badge" id="procCount" style="display:none">0</span></div>
  <div class="tab" data-tab="roadmap" onclick="switchTab('roadmap')" style="flex:1;text-align:center;">Roadmap</div>
</div>

<!-- ==================== BOARD TAB ==================== -->
<div class="tab-content active" id="tab-board">
  <div class="controls">
    <select id="filterProject" onchange="reload()"><option value="">All projects</option></select>
    <select id="filterBoard" onchange="reload()"><option value="">All boards</option></select>
    <button class="btn" onclick="openNewTask()">+ Task</button>
  </div>
  <div class="stats-bar" id="statsBar"></div>
  <div class="board" id="board">
    <div class="column col-todo" data-status="todo">
      <div class="column-header">Todo <span class="count" id="cnt-todo">0</span></div>
      <div class="column-body" id="col-todo"></div>
    </div>
    <div class="column col-in_progress" data-status="in_progress">
      <div class="column-header">In Progress <span class="count" id="cnt-in_progress">0</span></div>
      <div class="column-body" id="col-in_progress"></div>
    </div>
    <div class="column col-done" data-status="done">
      <div class="column-header">Done <span class="count" id="cnt-done">0</span></div>
      <div class="column-body" id="col-done"></div>
    </div>
    <div class="column col-blocked" data-status="blocked">
      <div class="column-header">Blocked <span class="count" id="cnt-blocked">0</span></div>
      <div class="column-body" id="col-blocked"></div>
    </div>
  </div>
</div>

<!-- WAR ROOMS, BLOCKERS, HANDOFFS tabs removed from UI (API endpoints kept) -->

<!-- ==================== AGENTS TAB ==================== -->
<div class="tab-content" id="tab-agents">
  <div class="controls">
    <button class="btn" onclick="openRegisterAgent()">+ Register Agent</button>
    <button class="btn btn-ghost" onclick="loadAgents();loadCommentary()">Refresh</button>
  </div>
  <div class="agents-grid" id="agentsGrid">
    <div style="color:var(--text-dim);padding:40px;text-align:center">No agents registered yet. Agents register via the API or the button above.</div>
  </div>
  <div style="margin-top:16px;padding:0 16px;">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--accent);margin-bottom:8px;">Live Feed</div>
    <div class="commentary-feed" id="commentaryFeed">
      <div style="color:var(--text-dim);text-align:center;padding:20px;font-size:12px;">Agent commentary will appear here as agents work</div>
    </div>
  </div>
</div>

<!-- ==================== CONVERSATIONS TAB ==================== -->
<div class="tab-content" id="tab-conversations">
  <div class="conv-layout">
    <div class="conv-sidebar" id="convSidebar">
      <div class="conv-sidebar-title">
        <span>Topics</span>
        <button class="btn-sm" onclick="createTopicPrompt()" style="padding:2px 8px;font-size:11px;margin-left:auto;">+</button>
      </div>
      <div id="topicList" class="topic-list"></div>
    </div>
    <div class="conv-main" id="convMain">
      <div class="conv-header" id="topicHeader" style="display:none;">
        <span class="conv-agent-name" id="topicHeaderName"></span>
        <div class="model-toggle" id="modelToggle">
          <button class="model-btn active" data-model="sonnet" onclick="setTopicModel('sonnet')">⚡ Fast</button>
          <button class="model-btn" data-model="opus" onclick="setTopicModel('opus')">🧠 Deep</button>
        </div>
        <div style="display:flex;gap:6px;margin-left:auto;">
          <button class="btn-sm" onclick="togglePinTopic()" id="topicPinBtn" style="font-size:11px;padding:2px 8px;" title="Pin/unpin">Pin</button>
          <button class="btn-sm" onclick="archiveCurrentTopic()" style="font-size:11px;padding:2px 8px;color:var(--blocked);" title="Archive">Archive</button>
        </div>
      </div>
      <div class="conv-messages" id="topicMessages">
        <div class="topic-empty-state" id="topicEmptyState">
          <div style="font-size:14px;margin-bottom:12px;">No topics yet</div>
          <button class="btn" onclick="createTopicPrompt()">+ Create a Topic</button>
          <div style="margin-top:8px;font-size:11px;color:var(--text-dim);">or just start typing below to create "General"</div>
        </div>
      </div>
      <div class="conv-input" id="topicInput">
        <input type="text" id="topicMsgInput" placeholder="Type a message..."
          onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendTopicMessage();}">
        <button class="btn" onclick="sendTopicMessage()">Send</button>
      </div>
    </div>
  </div>
</div>

<!-- MEMORY tab removed from UI (API endpoints kept) -->

<!-- ACTIVITY tab removed from UI (API endpoints kept) -->

<!-- ==================== MONITOR TAB ==================== -->
<div class="tab-content" id="tab-monitor">
  <div class="controls">
    <button class="btn" onclick="runHealthCheck()">Run Health Check</button>
    <button class="btn btn-ghost" onclick="loadCachedHealth();loadAutomations()">Refresh</button>
    <span id="healthTime" style="font-size:11px;color:var(--text-dim)"></span>
  </div>
  <div style="padding:16px;display:flex;flex-direction:column;gap:16px;" id="monitorPanel">
    <!-- Automations section -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--accent);margin-bottom:8px;">Automations</div>
      <div id="automationsGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;">
        <div style="color:var(--text-dim);font-size:12px;">Loading automations...</div>
      </div>
    </div>
    <!-- Health checks section -->
    <div id="healthSummary" style="display:flex;gap:12px;flex-wrap:wrap;"></div>
    <div id="healthGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;">
      <div style="color:var(--text-dim);text-align:center;padding:40px;grid-column:1/-1">
        Click "Run Health Check" to scan all services and servers
      </div>
    </div>
  </div>
</div>

<!-- ==================== PROCESSES TAB ==================== -->
<div class="tab-content" id="tab-processes">
  <div class="controls">
    <button class="btn" onclick="loadProcesses()">Refresh</button>
    <span id="procLastRefresh" style="font-size:11px;color:var(--text-dim)"></span>
  </div>
  <div style="padding:16px;display:flex;flex-direction:column;gap:16px;" id="processPanel">
    <!-- Local processes -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--accent);margin-bottom:8px;">Local (this machine)</div>
      <div id="procLocal" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;">
        <div style="color:var(--text-dim);font-size:12px;">Loading...</div>
      </div>
    </div>
    <!-- jagg processes -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:#3a9bdc;margin-bottom:8px;">jagg (192.168.0.224)</div>
      <div id="procJagg" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;">
        <div style="color:var(--text-dim);font-size:12px;">Loading...</div>
      </div>
    </div>
    <!-- DO containers -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:#2db36a;margin-bottom:8px;">DigitalOcean (138.197.145.132)</div>
      <div id="procDO" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;">
        <div style="color:var(--text-dim);font-size:12px;">Loading...</div>
      </div>
    </div>
  </div>
</div>

<!-- ==================== ROADMAP TAB ==================== -->
<div class="tab-content" id="tab-roadmap">
  <div class="controls">
    <select id="roadmapProject" onchange="loadRoadmaps()" style="padding:6px 10px;background:var(--surface-alt);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;">
      <option value="">All projects</option>
    </select>
    <button class="btn" onclick="showNewRoadmapDialog()">+ Roadmap</button>
    <button class="btn btn-ghost" onclick="loadRoadmaps()">Refresh</button>
  </div>
  <div style="padding:16px;" id="roadmapPanel">
    <div style="color:var(--text-dim);font-size:13px;text-align:center;padding:40px;">Loading roadmaps...</div>
  </div>
</div>

<!-- ==================== GOVERNANCE TAB ==================== -->
<div class="tab-content" id="tab-governance">
  <div class="controls">
    <button class="btn" onclick="loadGovernance()">Refresh</button>
    <span id="govLastRefresh" style="font-size:11px;color:var(--text-dim)"></span>
  </div>
  <div style="padding:16px;display:flex;flex-direction:column;gap:16px;" id="governancePanel">
    <!-- Pending Approvals -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--accent);margin-bottom:8px;">Pending Approvals</div>
      <div id="govPendingGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px;">
        <div style="color:var(--text-dim);font-size:12px;padding:20px;text-align:center;">Loading...</div>
      </div>
    </div>
    <!-- Recent Decisions -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:8px;">Recent Decisions</div>
      <div id="govRecentGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px;">
        <div style="color:var(--text-dim);font-size:12px;padding:20px;text-align:center;">Loading...</div>
      </div>
    </div>
    <!-- Policy Status -->
    <div>
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:8px;">Policy Status</div>
      <div id="govStatusPanel" style="display:flex;gap:12px;flex-wrap:wrap;">
        <div style="color:var(--text-dim);font-size:12px;">Loading...</div>
      </div>
    </div>
  </div>
</div>

<!-- WORKSPACE tab removed from UI (API endpoints kept) -->

<!-- Doc modal removed (workspace tab removed from UI) -->

<!-- Task Modal -->
<div class="modal-overlay" id="taskModal">
  <div class="modal">
    <h2 id="modalTitle">New Task</h2>
    <input type="hidden" id="taskId">
    <label>Title</label>
    <input type="text" id="taskTitleInput" placeholder="What needs to be done?">
    <label>Description</label>
    <textarea id="taskDesc" placeholder="Details, acceptance criteria..."></textarea>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div><label>Priority</label><select id="taskPriority">
        <option value="low">Low</option><option value="medium" selected>Medium</option>
        <option value="high">High</option><option value="critical">Critical</option>
      </select></div>
      <div><label>Type</label><select id="taskType">
        <option value="task">Task</option><option value="bug">Bug</option>
        <option value="feature">Feature</option><option value="idea">Idea</option>
      </select></div>
      <div><label>Status</label><select id="taskStatus">
        <option value="todo">Todo</option><option value="in_progress">In Progress</option>
        <option value="done">Done</option><option value="blocked">Blocked</option>
      </select></div>
      <div><label>Assigned To</label><input type="text" id="taskAssignee" placeholder="agent name"></div>
      <div><label>Project</label><input type="text" id="taskProject" placeholder="project name"></div>
      <div><label>Board</label><input type="text" id="taskBoard" placeholder="default" value="default"></div>
      <div><label>Tags</label><input type="text" id="taskTags" placeholder="comma separated"></div>
      <div><label>Due Date</label><input type="date" id="taskDue"></div>
    </div>
    <div class="modal-buttons">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="deleteBtn" style="display:none" onclick="deleteTask()">Delete</button>
      <button class="btn" onclick="saveTask()">Save</button>
    </div>
  </div>
</div>

<!-- View Task Detail Modal -->
<div class="modal-overlay" id="viewTaskModal" onclick="if(event.target===this)closeViewTask()">
  <div class="modal" style="max-width:700px;width:90%;max-height:85vh;overflow-y:auto;">
    <div id="viewTaskContent"></div>
  </div>
</div>

<!-- Register Agent Modal -->
<div class="modal-overlay" id="agentModal">
  <div class="modal">
    <h2>Register Agent</h2>
    <label>Agent Name</label>
    <input type="text" id="agentName" placeholder="e.g. claude-opus, codex, gemini-pro">
    <label>Capabilities</label>
    <input type="text" id="agentCaps" placeholder="e.g. coding, research, pentesting">
    <label>Model / Provider</label>
    <input type="text" id="agentModel" placeholder="e.g. claude-opus-4, gpt-4.1, gemini-2.5">
    <div class="modal-buttons">
      <button class="btn btn-ghost" onclick="document.getElementById('agentModal').classList.remove('active')">Cancel</button>
      <button class="btn" onclick="registerAgent()">Register</button>
    </div>
  </div>
</div>

<!-- Security Agent Readout Bubble -->
<div id="secBubble" onclick="toggleSecPanel()" style="
  position:fixed; bottom:20px; right:82px; width:52px; height:52px;
  background:#1a6b3c; border-radius:50%; cursor:pointer; z-index:400;
  display:flex; align-items:center; justify-content:center;
  box-shadow:0 4px 12px rgba(0,0,0,0.4); transition:transform 0.15s;
  font-size:20px; color:#fff;
" onmouseenter="this.style.transform='scale(1.1)'" onmouseleave="this.style.transform='scale(1)'"
  title="Security Agent Readout">&#128737;</div>

<div id="secPanel" style="
  display:none; position:fixed; bottom:80px; right:82px; width:460px; max-height:560px;
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  z-index:400; box-shadow:0 8px 30px rgba(0,0,0,0.5); flex-direction:column;
  overflow:hidden;
">
  <div style="padding:10px 14px;background:#1a6b3c;color:#fff;font-size:13px;font-weight:600;
    display:flex;align-items:center;justify-content:space-between;border-radius:12px 12px 0 0;">
    <span><span id="secDot" style="display:inline-block;width:8px;height:8px;border-radius:50%;
      background:#555;margin-right:6px;"></span>Security Agent</span>
    <div style="display:flex;gap:6px;">
      <button onclick="loadSecStatus()" style="background:rgba(255,255,255,0.2);border:none;color:#fff;
        padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;">Refresh</button>
      <button onclick="toggleSecPanel()" style="background:rgba(255,255,255,0.2);border:none;color:#fff;
        padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;">X</button>
    </div>
  </div>
  <div id="secStatus" style="padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted);">
    Loading...
  </div>
  <div id="secActivity" style="flex:1;overflow-y:auto;padding:10px 14px;min-height:300px;max-height:420px;
    display:flex;flex-direction:column;gap:6px;font-size:12px;">
  </div>
</div>

<!-- Chat Bubble -->
<div id="chatBubble" onclick="toggleChat()" style="
  position:fixed; bottom:20px; right:20px; width:52px; height:52px;
  background:var(--accent); border-radius:50%; cursor:pointer; z-index:400;
  display:flex; align-items:center; justify-content:center;
  box-shadow:0 4px 12px rgba(0,0,0,0.4); transition:transform 0.15s;
  font-size:22px; color:#fff;
" onmouseenter="this.style.transform='scale(1.1)'" onmouseleave="this.style.transform='scale(1)'">&#9697;</div>

<div id="chatPanel" style="
  display:none; position:fixed; bottom:80px; right:20px; width:420px; max-height:520px;
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  z-index:400; box-shadow:0 8px 30px rgba(0,0,0,0.5); flex-direction:column;
  overflow:hidden;
">
  <div style="padding:10px 14px;background:var(--accent);color:#fff;font-size:13px;font-weight:600;
    display:flex;align-items:center;justify-content:space-between;border-radius:12px 12px 0 0;">
    <span>Claude Assistant</span>
    <div style="display:flex;gap:6px;">
      <button onclick="clearChat()" style="background:rgba(255,255,255,0.2);border:none;color:#fff;
        padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;">Clear</button>
      <button onclick="toggleChat()" style="background:rgba(255,255,255,0.2);border:none;color:#fff;
        padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;">X</button>
    </div>
  </div>
  <div id="chatMessages" style="flex:1;overflow-y:auto;padding:12px;display:flex;
    flex-direction:column;gap:8px;min-height:300px;max-height:380px;"></div>
  <div style="padding:8px;border-top:1px solid var(--border);display:flex;gap:6px;">
    <input type="text" id="chatInput" placeholder="Ask Claude anything..."
      style="flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--text);
        padding:8px 10px;border-radius:6px;font-size:13px;"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}">
    <button class="btn" onclick="sendChat()" style="padding:8px 14px;">Send</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = '';
let boardData = {};
let activityLog = [];

// ========== Tab switching ==========
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === `tab-${name}`));
  if (name === 'agents') { loadAgents(); loadCommentary(); }
  if (name === 'monitor') { loadCachedHealth(); loadAutomations(); }
  if (name === 'processes') { loadProcesses(); }
  if (name === 'conversations') loadTopics();
  if (name === 'roadmap') loadRoadmaps();
}

// ========== Board ==========
async function reload() {
  const project = document.getElementById('filterProject').value;
  const board = document.getElementById('filterBoard').value;
  const params = new URLSearchParams();
  if (project) params.set('project', project);
  if (board) params.set('board', board);
  const [boardRes, statsRes] = await Promise.all([
    fetch(`${API}/api/board?${params}`),
    fetch(`${API}/api/stats?${params}`)
  ]);
  boardData = await boardRes.json();
  renderBoard(boardData);
  renderStats(await statsRes.json());
}

function renderBoard(data) {
  for (const status of ['todo','in_progress','done','blocked']) {
    const col = document.getElementById(`col-${status}`);
    const cnt = document.getElementById(`cnt-${status}`);
    const tasks = data[status] || [];
    cnt.textContent = tasks.length;
    col.innerHTML = tasks.map(cardHTML).join('');
  }
  setupDragDrop();
  initTouchDrag();
}

function cardHTML(t) {
  const desc = t.description ? `<div class="card-desc">${esc(t.description.substring(0,100))}</div>` : '';
  const assignee = t.assigned_to ? `<span class="badge badge-assignee">@${esc(t.assigned_to)}</span>` : '';
  const typeBadge = t.type !== 'task' ? `<span class="badge badge-type">${esc(t.type)}</span>` : '';
  const tags = t.tags ? t.tags.split(',').map(tg => `<span class="badge badge-type">${esc(tg.trim())}</span>`).join('') : '';
  return `<div class="card" draggable="true" data-id="${t.id}" ondragstart="dragStart(event)" onclick="viewTask(${t.id})" style="cursor:pointer;">
    <div class="card-id"><span>#${t.id}</span>
      <div class="card-actions"><button class="card-btn" onclick="event.stopPropagation();editTask(${t.id})" title="Edit">&#9998;</button></div>
    </div>
    <div class="card-title">${esc(t.title)}</div>
    <div class="card-meta"><span class="badge badge-${t.priority}">${t.priority}</span>${typeBadge}${assignee}${tags}</div>
    ${desc}</div>`;
}

async function viewTask(id) {
  const t = await (await fetch(`${API}/api/task/${id}`)).json();
  const created = t.created_at ? new Date(t.created_at*1000).toLocaleString() : '';
  const updated = t.updated_at ? new Date(t.updated_at*1000).toLocaleString() : '';
  const due = t.due_date ? new Date(t.due_date*1000).toLocaleDateString() : 'None';
  const assignee = t.assigned_to || 'Unassigned';
  const tags = t.tags ? t.tags.split(',').map(tg => `<span class="badge badge-type">${esc(tg.trim())}</span>`).join(' ') : '<span style="color:var(--text-dim)">None</span>';
  const descHTML = t.description ? t.description.replace(/\n/g,'<br>') : '<span style="color:var(--text-dim)">No description</span>';
  const subtasksHTML = (t.subtasks && t.subtasks.length) ? t.subtasks.map(s =>
    `<div style="padding:6px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;">
      <span style="font-size:11px;color:var(--text-dim)">#${s.id}</span>
      <span style="flex:1">${esc(s.title)}</span>
      <span class="badge badge-${s.priority}">${s.priority}</span>
      <span class="badge" style="background:var(--${s.status === 'done' ? 'done' : s.status === 'in_progress' ? 'progress' : 'todo'})">${s.status}</span>
    </div>`
  ).join('') : '<span style="color:var(--text-dim)">None</span>';

  document.getElementById('viewTaskContent').innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <h2 style="margin:0;color:var(--accent);font-size:18px;">#${t.id} — ${esc(t.title)}</h2>
      <div style="display:flex;gap:8px;">
        <button class="btn" onclick="closeViewTask();editTask(${t.id});" style="font-size:12px;padding:6px 14px;">&#9998; Edit</button>
        <button class="btn btn-ghost" onclick="closeViewTask()" style="font-size:12px;padding:6px 14px;">Close</button>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px;">
      <div><span style="color:var(--text-dim);font-size:11px;">Status</span><br><span class="badge" style="background:var(--${t.status === 'done' ? 'done' : t.status === 'in_progress' ? 'progress' : t.status === 'blocked' ? 'blocked' : 'todo'})">${t.status.replace('_',' ')}</span></div>
      <div><span style="color:var(--text-dim);font-size:11px;">Priority</span><br><span class="badge badge-${t.priority}">${t.priority}</span></div>
      <div><span style="color:var(--text-dim);font-size:11px;">Type</span><br><span class="badge badge-type">${t.type}</span></div>
      <div><span style="color:var(--text-dim);font-size:11px;">Assigned To</span><br>${esc(assignee)}</div>
      <div><span style="color:var(--text-dim);font-size:11px;">Project</span><br>${esc(t.project || 'None')}</div>
      <div><span style="color:var(--text-dim);font-size:11px;">Board</span><br>${esc(t.board || 'default')}</div>
      <div><span style="color:var(--text-dim);font-size:11px;">Due Date</span><br>${due}</div>
      <div><span style="color:var(--text-dim);font-size:11px;">Created</span><br>${created}</div>
      <div><span style="color:var(--text-dim);font-size:11px;">Updated</span><br>${updated}</div>
    </div>
    <div style="margin-bottom:12px;">
      <span style="color:var(--text-dim);font-size:11px;">Tags</span><br>${tags}
    </div>
    <div style="margin-bottom:16px;">
      <span style="color:var(--text-dim);font-size:11px;">Description</span>
      <div style="margin-top:6px;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;font-size:13px;line-height:1.6;white-space:pre-wrap;max-height:400px;overflow-y:auto;">${descHTML}</div>
    </div>
    <div>
      <span style="color:var(--text-dim);font-size:11px;">Subtasks</span>
      <div style="margin-top:6px;">${subtasksHTML}</div>
    </div>
  `;
  document.getElementById('viewTaskModal').classList.add('active');
}
function closeViewTask() { document.getElementById('viewTaskModal').classList.remove('active'); }

function renderStats(s) {
  const bs = s.by_status || {};
  document.getElementById('statsBar').innerHTML = `
    <span class="stat-item"><span class="stat-dot" style="background:var(--text)"></span> ${s.total} total</span>
    <span class="stat-item"><span class="stat-dot" style="background:var(--todo)"></span> ${bs.todo||0} todo</span>
    <span class="stat-item"><span class="stat-dot" style="background:var(--progress)"></span> ${bs.in_progress||0} in progress</span>
    <span class="stat-item"><span class="stat-dot" style="background:var(--done)"></span> ${bs.done||0} done</span>
    <span class="stat-item"><span class="stat-dot" style="background:var(--blocked)"></span> ${bs.blocked||0} blocked</span>`;
}

// Drag and drop
function dragStart(e) { e.dataTransfer.setData('text/plain', e.target.dataset.id); e.target.classList.add('dragging'); }
function setupDragDrop() {
  document.querySelectorAll('.column-body').forEach(col => {
    col.ondragover = e => { e.preventDefault(); col.classList.add('drag-over'); };
    col.ondragleave = () => col.classList.remove('drag-over');
    col.ondrop = async e => {
      e.preventDefault(); col.classList.remove('drag-over');
      const id = e.dataTransfer.getData('text/plain');
      const status = col.closest('.column').dataset.status;
      await fetch(`${API}/api/task/${id}/move`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
      toast(`Task #${id} → ${status.replace('_',' ')}`);
      reload();
    };
  });
}

// ========== Task modal ==========
function openNewTask() {
  document.getElementById('modalTitle').textContent = 'New Task';
  document.getElementById('taskId').value = '';
  ['taskTitleInput','taskDesc','taskAssignee','taskTags'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('taskPriority').value = 'medium';
  document.getElementById('taskType').value = 'task';
  document.getElementById('taskStatus').value = 'todo';
  document.getElementById('taskProject').value = document.getElementById('filterProject').value;
  document.getElementById('taskBoard').value = document.getElementById('filterBoard').value || 'default';
  document.getElementById('taskDue').value = '';
  document.getElementById('deleteBtn').style.display = 'none';
  document.getElementById('taskModal').classList.add('active');
  document.getElementById('taskTitleInput').focus();
}
async function editTask(id) {
  const t = await (await fetch(`${API}/api/task/${id}`)).json();
  document.getElementById('modalTitle').textContent = `Edit Task #${id}`;
  document.getElementById('taskId').value = id;
  document.getElementById('taskTitleInput').value = t.title||'';
  document.getElementById('taskDesc').value = t.description||'';
  document.getElementById('taskPriority').value = t.priority||'medium';
  document.getElementById('taskType').value = t.type||'task';
  document.getElementById('taskStatus').value = t.status||'todo';
  document.getElementById('taskAssignee').value = t.assigned_to||'';
  document.getElementById('taskProject').value = t.project||'';
  document.getElementById('taskBoard').value = t.board||'default';
  document.getElementById('taskTags').value = t.tags||'';
  document.getElementById('taskDue').value = t.due_date ? new Date(t.due_date*1000).toISOString().split('T')[0] : '';
  document.getElementById('deleteBtn').style.display = 'inline-block';
  document.getElementById('taskModal').classList.add('active');
  document.getElementById('taskTitleInput').focus();
}
function closeModal() { document.getElementById('taskModal').classList.remove('active'); }
async function saveTask() {
  const id = document.getElementById('taskId').value;
  const dueVal = document.getElementById('taskDue').value;
  const body = {
    title: document.getElementById('taskTitleInput').value,
    description: document.getElementById('taskDesc').value,
    priority: document.getElementById('taskPriority').value,
    type: document.getElementById('taskType').value,
    status: document.getElementById('taskStatus').value,
    assigned_to: document.getElementById('taskAssignee').value,
    project: document.getElementById('taskProject').value,
    board: document.getElementById('taskBoard').value,
    tags: document.getElementById('taskTags').value,
    due_date: dueVal ? new Date(dueVal).getTime()/1000 : null,
  };
  if (!body.title.trim()) { toast('Title required'); return; }
  if (id) {
    await fetch(`${API}/api/task/${id}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(`Task #${id} updated`);
  } else {
    const data = await (await fetch(`${API}/api/task`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    toast(`Task #${data.id} created`);
  }
  closeModal(); reload(); loadFilters();
}
async function deleteTask() {
  const id = document.getElementById('taskId').value;
  if (!id || !confirm(`Delete task #${id}?`)) return;
  await fetch(`${API}/api/task/${id}`, {method:'DELETE'});
  toast(`Task #${id} deleted`); closeModal(); reload();
}

// ========== Agents ==========
async function loadAgents() {
  const res = await fetch(`${API}/api/agents`);
  const agents = await res.json();
  document.getElementById('agentCount').textContent = agents.length;
  const grid = document.getElementById('agentsGrid');
  if (!agents.length) {
    grid.innerHTML = '<div style="color:var(--text-dim);padding:40px;text-align:center;grid-column:1/-1">No agents registered yet. Agents register via the API or the button above.</div>';
    return;
  }
  // Sort: busy first, then idle, then stalled, then offline
  const order = {busy:0, idle:1, stalled:2, offline:3};
  agents.sort((a,b) => (order[a.effective_status]||9) - (order[b.effective_status]||9));

  // Fetch commentary for all agents in one call
  let allCommentary = [];
  try {
    const cRes = await fetch(`${API}/api/agent/commentary?limit=100`);
    allCommentary = await cRes.json();
  } catch(e) {}

  grid.innerHTML = agents.map(a => {
    const st = a.effective_status;
    const hb = a.last_heartbeat ? timeAgo(a.last_heartbeat) : 'never';
    const task = a.current_task
      ? `<div class="agent-task-badge">Working on #${a.current_task}</div>`
      : '';
    const caps = a.capabilities ? `<div>Capabilities: ${esc(a.capabilities)}</div>` : '';
    const model = a.model ? `<div>Model: <strong>${esc(a.model)}</strong></div>` : '';
    const pending = a.pending_directives
      ? `<div class="agent-directive-badge">${a.pending_directives} pending directive(s)</div>`
      : '';

    // Inline summary: last 5 commentary entries for this agent
    const agentComments = allCommentary.filter(c => c.agent === a.name).slice(0, 5);
    let summaryHtml = '';
    if (agentComments.length) {
      summaryHtml = '<div class="agent-summary">' +
        agentComments.map(c => {
          const t = new Date(c.timestamp * 1000).toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit'});
          return `<div class="agent-summary-line"><span class="summary-time">${t}</span>${esc(c.message)}</div>`;
        }).join('') + '</div>';
    }

    return `<div class="agent-card ${st}" onclick="switchTab('conversations');setTimeout(()=>loadConversation('${esc(a.name)}'),100)" style="cursor:pointer;">
      <div class="agent-glow"></div>
      <h3>
        <span class="agent-status status-${st}"></span>
        <span>${esc(a.name)}</span>
        <span class="agent-status-label ${st}" style="margin-left:auto">${st.toUpperCase()}</span>
      </h3>
      <div class="agent-meta">
        <div>Heartbeat: ${hb}</div>
        ${model}${caps}${task}${pending}
      </div>
      ${summaryHtml}
      <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;" onclick="event.stopPropagation()">
        <button class="btn btn-sm btn-ghost" style="min-height:44px;padding:10px 14px;" onclick="sendDirectivePrompt('${esc(a.name)}')">Send Directive</button>
        <button class="btn btn-sm btn-ghost" style="min-height:44px;padding:10px 14px;" onclick="pingAgent('${esc(a.name)}')">Ping</button>
        <button class="btn btn-sm btn-ghost" style="min-height:44px;padding:10px 14px;" onclick="switchTab('conversations');setTimeout(()=>loadConversation('${esc(a.name)}'),100)">Chat</button>
        <button class="btn btn-sm btn-ghost btn-danger" style="min-height:44px;padding:10px 14px;" onclick="removeAgent('${esc(a.name)}')">Remove</button>
      </div>
    </div>`;
  }).join('');
}

async function showAgentFeed(agentName) {
  // Filter commentary to just this agent
  try {
    const res = await fetch(`${API}/api/agent/commentary?agent=${encodeURIComponent(agentName)}&limit=30`);
    const items = await res.json();
    const el = document.getElementById('commentaryFeed');
    if (!items.length) {
      el.innerHTML = `<div style="color:var(--text-dim);text-align:center;padding:20px;font-size:12px;">No commentary from ${esc(agentName)} yet</div>`;
    } else {
      el.innerHTML = `<div style="font-size:10px;color:var(--accent);margin-bottom:8px;cursor:pointer" onclick="loadCommentary()">Showing: ${esc(agentName)} (click to show all)</div>` +
        items.map(c => {
          const t = new Date(c.timestamp * 1000).toLocaleTimeString();
          const task = c.task_id ? `<span class="c-task">#${c.task_id}</span>` : '';
          return `<div class="commentary-item"><span class="c-time">${t}</span>${task}<div class="c-msg">${esc(c.message)}</div></div>`;
        }).join('');
    }
  } catch(e) {}
}

async function sendDirectivePrompt(agentName) {
  const msg = prompt(`Send directive to ${agentName}:`);
  if (!msg) return;
  const priority = msg.toLowerCase().includes('urgent') ? 'urgent' : 'normal';
  await fetch(`${API}/api/agent/${encodeURIComponent(agentName)}/directive`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: msg, from: 'user', priority})
  });
  toast(`Directive sent to ${agentName}`);
  loadAgents(); loadCommentary();
}

function openRegisterAgent() {
  ['agentName','agentCaps','agentModel'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('agentModal').classList.add('active');
  document.getElementById('agentName').focus();
}
async function registerAgent() {
  const name = document.getElementById('agentName').value.trim();
  if (!name) { toast('Name required'); return; }
  await fetch(`${API}/api/agent/register`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      name,
      capabilities: document.getElementById('agentCaps').value,
      model: document.getElementById('agentModel').value
    })
  });
  document.getElementById('agentModal').classList.remove('active');
  toast(`Agent "${name}" registered`);
  loadAgents();
}
async function removeAgent(name) {
  if (!confirm(`Remove agent "${name}"?`)) return;
  await fetch(`${API}/api/agent/${encodeURIComponent(name)}`, {method:'DELETE'});
  toast(`Agent "${name}" removed`); loadAgents();
}
async function pingAgent(name) {
  toast(`Pinging ${name}...`);
  // Just trigger a heartbeat check display
  loadAgents();
}

// ========== Memory (tab removed from UI) ==========
// recallMemory, rememberFact — removed with Memory tab

// ========== Activity (tab removed from UI) ==========
// function loadActivity() — removed with Activity tab

// ========== Filters ==========
async function loadFilters() {
  const [pRes, bRes] = await Promise.all([fetch(`${API}/api/projects`), fetch(`${API}/api/boards`)]);
  const projects = await pRes.json();
  const boards = await bRes.json();
  const sel = document.getElementById('filterProject');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All projects</option>';
  projects.forEach(p => { const o = new Option(p, p); if (p===cur) o.selected=true; sel.add(o); });
  const bsel = document.getElementById('filterBoard');
  const bcur = bsel.value;
  bsel.innerHTML = '<option value="">All boards</option>';
  boards.forEach(b => { const o = new Option(`${b.board} (${b.count})`, b.board); if (b.board===bcur) o.selected=true; bsel.add(o); });
}

// ========== Hyphae status ==========
async function checkHyphae() {
  try {
    const res = await fetch(`${API}/api/hyphae/status`);
    const d = await res.json();
    document.getElementById('hyphaeStatus').innerHTML = d.online
      ? `<span style="color:var(--done)">&#9679;</span> Hyphae: ${d.facts} facts`
      : `<span style="color:var(--blocked)">&#9679;</span> Hyphae offline`;
    const hfc = document.getElementById('hyphaeFactCount'); if (hfc) hfc.textContent = d.online ? `${d.facts} facts` : 'offline';
  } catch(e) {
    document.getElementById('hyphaeStatus').innerHTML = '<span style="color:var(--blocked)">&#9679;</span> Hyphae offline';
  }
}

// ========== Topic Conversations ==========
let currentTopicId = null;
let currentTopicPinned = false;
let topicsCache = [];
let topicStreaming = false;

async function loadTopics() {
  try {
    const res = await fetch(`${API}/api/topics`);
    topicsCache = await res.json();
    renderTopicList();
    // If we had a selected topic, keep it selected
    if (currentTopicId) {
      const still = topicsCache.find(t => t.id === currentTopicId);
      if (!still) { currentTopicId = null; showTopicEmptyState(); }
    }
    // Show empty state if no topics
    if (!topicsCache.length) showTopicEmptyState();
  } catch(e) {}
}

function renderTopicList() {
  const list = document.getElementById('topicList');
  if (!topicsCache.length) {
    list.innerHTML = '<div style="padding:20px;color:var(--text-dim);font-size:12px;text-align:center;">No topics yet</div>';
    return;
  }
  let html = '';
  for (const t of topicsCache) {
    const isActive = currentTopicId === t.id ? ' active' : '';
    const isPinned = t.pinned ? ' pinned' : '';
    const preview = t.last_message ? t.last_message.substring(0, 40) + (t.last_message.length > 40 ? '...' : '') : 'No messages';
    const timeStr = t.updated_at ? timeAgo(t.updated_at) : '';
    const modelIcon = (t.model === 'opus') ? '🧠' : '⚡';
    html += `<div class="topic-item${isActive}${isPinned}" onclick="selectTopic(${t.id})">
      <div class="topic-name">${esc(t.name)}<span class="topic-model-indicator">${modelIcon}</span></div>
      <div class="topic-preview">${esc(preview)}</div>
      <div class="topic-time">${esc(timeStr)}</div>
    </div>`;
  }
  list.innerHTML = html;
}

function showTopicEmptyState() {
  document.getElementById('topicEmptyState').style.display = 'block';
  document.getElementById('topicHeader').style.display = 'none';
}

async function setTopicModel(model) {
  if (!currentTopicId) return;
  await fetch(`${API}/api/topics/${currentTopicId}/model`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model})
  });
  document.querySelectorAll('.model-btn').forEach(b => b.classList.remove('active'));
  document.querySelector(`.model-btn[data-model="${model}"]`).classList.add('active');
  const topic = topicsCache.find(t => t.id === currentTopicId);
  if (topic) topic.model = model;
  renderTopicList();
}

function updateModelToggle(model) {
  const m = model || 'sonnet';
  document.querySelectorAll('.model-btn').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector(`.model-btn[data-model="${m}"]`);
  if (btn) btn.classList.add('active');
}

async function selectTopic(topicId) {
  currentTopicId = topicId;
  const topic = topicsCache.find(t => t.id === topicId);
  if (!topic) return;
  currentTopicPinned = !!topic.pinned;

  // Update sidebar
  document.querySelectorAll('.topic-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.topic-item').forEach((el, i) => {
    if (topicsCache[i] && topicsCache[i].id === topicId) el.classList.add('active');
  });

  // Show header
  document.getElementById('topicHeader').style.display = 'flex';
  document.getElementById('topicHeaderName').textContent = topic.name;
  document.getElementById('topicPinBtn').textContent = currentTopicPinned ? 'Unpin' : 'Pin';
  document.getElementById('topicEmptyState').style.display = 'none';
  updateModelToggle(topic.model);

  // Load messages
  try {
    const res = await fetch(`${API}/api/topics/${topicId}/messages?limit=100`);
    const msgs = await res.json();
    renderTopicMessages(msgs);
  } catch(e) {
    document.getElementById('topicMessages').innerHTML = '<div style="color:#e74c3c;padding:20px;">Error loading messages</div>';
  }

  document.getElementById('topicMsgInput').focus();
}

function renderTopicMessages(messages) {
  const container = document.getElementById('topicMessages');
  if (!messages.length) {
    container.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:40px;font-size:13px;">No messages yet. Start the conversation!</div>';
    return;
  }
  let html = '';
  let lastDay = '';
  for (const m of messages) {
    const d = new Date(m.timestamp * 1000);
    const day = d.toLocaleDateString();
    if (day !== lastDay) {
      html += `<div class="conv-day-divider">${day}</div>`;
      lastDay = day;
    }
    const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    if (m.role === 'system') {
      html += `<div class="msg-bubble role-system">${esc(m.content)}<div class="msg-meta">${timeStr}</div></div>`;
    } else if (m.role === 'agent') {
      html += `<div class="msg-bubble role-agent"><div class="agent-badge">${esc(m.agent_name || 'agent')}</div><br>${esc(m.content)}<div class="msg-meta">${timeStr}</div></div>`;
    } else if (m.role === 'user') {
      html += `<div class="msg-bubble role-user">${esc(m.content)}<div class="msg-meta">${timeStr}</div></div>`;
    } else {
      // assistant
      const display = m.content.replace(/ACTION:\w+\{[^}]*\}/g, '').trim();
      html += `<div class="msg-bubble role-assistant">${esc(display)}<div class="msg-meta">${timeStr}</div></div>`;
    }
  }
  container.innerHTML = html;
  container.scrollTop = container.scrollHeight;
}

async function createTopicPrompt() {
  const name = prompt('Topic name:');
  if (!name || !name.trim()) return;
  try {
    const res = await fetch(`${API}/api/topics`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim()})
    });
    const data = await res.json();
    await loadTopics();
    selectTopic(data.id);
  } catch(e) {
    showToast('Failed to create topic');
  }
}

async function ensureGeneralTopic() {
  // Auto-create General topic if none exist
  if (topicsCache.length === 0) {
    try {
      const res = await fetch(`${API}/api/topics`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: 'General', description: 'General discussion'})
      });
      const data = await res.json();
      await loadTopics();
      selectTopic(data.id);
      return data.id;
    } catch(e) { return null; }
  }
  return currentTopicId || topicsCache[0]?.id;
}

async function sendTopicMessage() {
  if (topicStreaming) return;
  const input = document.getElementById('topicMsgInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';

  // If no topic selected, auto-create General
  if (!currentTopicId) {
    const tid = await ensureGeneralTopic();
    if (!tid) return;
    currentTopicId = tid;
  }

  // Append user message immediately
  const container = document.getElementById('topicMessages');
  // Remove empty state if present
  const emptyDiv = container.querySelector('.topic-empty-state, [style*="text-align:center"]');
  if (emptyDiv && container.children.length <= 1) container.innerHTML = '';

  const userBubble = document.createElement('div');
  userBubble.className = 'msg-bubble role-user';
  const timeStr = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  userBubble.innerHTML = `${esc(msg)}<div class="msg-meta">${timeStr}</div>`;
  container.appendChild(userBubble);

  // Create assistant bubble for streaming
  const assistBubble = document.createElement('div');
  assistBubble.className = 'msg-bubble role-assistant';
  assistBubble.textContent = '';
  container.appendChild(assistBubble);
  container.scrollTop = container.scrollHeight;

  topicStreaming = true;
  let fullText = '';
  try {
    const res = await fetch(`${API}/api/topics/${currentTopicId}/chat`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.text) {
            fullText += data.text;
            assistBubble.textContent = fullText.replace(/ACTION:\w+\{[^}]*\}/g, '').trim();
            container.scrollTop = container.scrollHeight;
          }
          if (data.error) {
            assistBubble.textContent += '\n[Error: ' + data.error + ']';
          }
        } catch(e) {}
      }
    }
    // Add timestamp to assistant bubble
    const atime = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    meta.textContent = atime;
    assistBubble.appendChild(meta);

    // Process ACTION blocks
    processActions(fullText);

    // Refresh topic list to update previews
    loadTopics();
  } catch(e) {
    assistBubble.textContent = 'Connection error: ' + e.message;
  }
  topicStreaming = false;
}

async function togglePinTopic() {
  if (!currentTopicId) return;
  currentTopicPinned = !currentTopicPinned;
  await fetch(`${API}/api/topics/${currentTopicId}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pinned: currentTopicPinned ? 1 : 0})
  });
  document.getElementById('topicPinBtn').textContent = currentTopicPinned ? 'Unpin' : 'Pin';
  loadTopics();
}

async function archiveCurrentTopic() {
  if (!currentTopicId) return;
  if (!confirm('Archive this topic?')) return;
  await fetch(`${API}/api/topics/${currentTopicId}`, {method: 'DELETE'});
  currentTopicId = null;
  showTopicEmptyState();
  document.getElementById('topicMessages').innerHTML = '';
  loadTopics();
}

// ========== Touch Drag and Drop ==========
let touchDragCard = null;
let touchDragClone = null;
let touchStartX = 0;
let touchStartY = 0;

function initTouchDrag() {
  document.querySelectorAll('.card[draggable]').forEach(card => {
    card.addEventListener('touchstart', onTouchStart, {passive: false});
    card.addEventListener('touchmove', onTouchMove, {passive: false});
    card.addEventListener('touchend', onTouchEnd, {passive: false});
  });
}

function onTouchStart(e) {
  if (e.touches.length !== 1) return;
  const touch = e.touches[0];
  touchStartX = touch.clientX;
  touchStartY = touch.clientY;
  touchDragCard = e.currentTarget;
  // Don't start drag immediately — wait for movement
}

function onTouchMove(e) {
  if (!touchDragCard) return;
  const touch = e.touches[0];
  const dx = Math.abs(touch.clientX - touchStartX);
  const dy = Math.abs(touch.clientY - touchStartY);

  // Only start drag after 10px movement
  if (!touchDragClone && (dx > 10 || dy > 10)) {
    e.preventDefault();
    touchDragClone = touchDragCard.cloneNode(true);
    touchDragClone.style.position = 'fixed';
    touchDragClone.style.zIndex = '9999';
    touchDragClone.style.opacity = '0.85';
    touchDragClone.style.width = touchDragCard.offsetWidth + 'px';
    touchDragClone.style.pointerEvents = 'none';
    touchDragClone.style.transform = 'rotate(2deg)';
    document.body.appendChild(touchDragClone);
    touchDragCard.style.opacity = '0.3';
  }

  if (touchDragClone) {
    e.preventDefault();
    touchDragClone.style.left = (touch.clientX - 20) + 'px';
    touchDragClone.style.top = (touch.clientY - 20) + 'px';

    // Highlight target column
    document.querySelectorAll('.column').forEach(c => c.classList.remove('drag-over'));
    const el = document.elementFromPoint(touch.clientX, touch.clientY);
    if (el) {
      const col = el.closest('.column');
      if (col) col.classList.add('drag-over');
    }
  }
}

async function onTouchEnd(e) {
  if (!touchDragCard) return;

  if (touchDragClone) {
    const touch = e.changedTouches[0];
    const el = document.elementFromPoint(touch.clientX, touch.clientY);
    const col = el ? el.closest('.column') : null;

    if (col) {
      const newStatus = col.dataset.status;
      const taskId = touchDragCard.dataset.id;
      if (taskId && newStatus) {
        await fetch(`${API}/api/task/${taskId}/move`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: newStatus})
        });
        toast(`Moved to ${newStatus}`);
        reload();
      }
    }

    document.querySelectorAll('.column').forEach(c => c.classList.remove('drag-over'));
    touchDragClone.remove();
    touchDragCard.style.opacity = '1';
  }

  touchDragCard = null;
  touchDragClone = null;
}

// ========== Security Agent Readout ==========
let secOpen = false;
let secInterval = null;

function toggleSecPanel() {
  secOpen = !secOpen;
  const panel = document.getElementById('secPanel');
  panel.style.display = secOpen ? 'flex' : 'none';
  if (secOpen) {
    loadSecStatus();
    if (!secInterval) secInterval = setInterval(loadSecStatus, 15000);
  } else {
    if (secInterval) { clearInterval(secInterval); secInterval = null; }
  }
}

async function loadSecStatus() {
  try {
    const res = await fetch(`${API}/api/security-agent/status`);
    const data = await res.json();
    const s = data.status || {};
    const dot = document.getElementById('secDot');
    const bubble = document.getElementById('secBubble');

    // Status dot color
    if (s.state === 'triaging') {
      dot.style.background = '#f0b429';
      bubble.style.background = '#b8860b';
    } else if (s.state === 'idle') {
      dot.style.background = '#2ecc71';
      bubble.style.background = '#1a6b3c';
    } else {
      dot.style.background = '#e74c3c';
      bubble.style.background = '#8b1a1a';
    }

    // Status bar
    const statusEl = document.getElementById('secStatus');
    const ls = s.last_stats || {};
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;">`;
    html += `<span style="color:${s.state==='triaging'?'#f0b429':'#2ecc71'};font-weight:600;text-transform:uppercase;">${s.state || 'unknown'}</span>`;
    html += `<span style="color:var(--muted)">${s.model || ''} via ${s.backend || '?'}</span>`;
    html += `</div>`;
    if (s.last_pass) {
      const ago = Math.round((Date.now() - new Date(s.last_pass + 'Z').getTime()) / 1000);
      const agoStr = ago < 60 ? ago + 's ago' : Math.round(ago/60) + 'm ago';
      html += `<div style="margin-top:4px;display:flex;gap:12px;flex-wrap:wrap;">`;
      html += `<span>Last pass: <strong>${agoStr}</strong></span>`;
      html += `<span>Verdicts: <strong>${ls.verdicts_set||0}</strong></span>`;
      html += `<span>Rules: <strong>${ls.silence_rules_created||0}</strong></span>`;
      html += `<span>Squawks: <strong style="color:${(ls.squawks_raised||0)>0?'#e74c3c':'inherit'}">${ls.squawks_raised||0}</strong></span>`;
      html += `</div>`;
      if (s.next_pass_seconds) {
        const nextIn = Math.max(0, s.next_pass_seconds - ago);
        html += `<div style="margin-top:2px;color:var(--muted);">Next pass in ~${Math.round(nextIn/60)}m</div>`;
      }
    }
    statusEl.innerHTML = html;

    // Activity feed
    const actEl = document.getElementById('secActivity');
    const acts = data.activity || [];
    if (acts.length === 0) {
      actEl.innerHTML = '<div style="color:var(--muted);text-align:center;padding:20px;">No activity yet</div>';
      return;
    }

    let feedHtml = '';
    for (const a of acts) {
      const ts = a.timestamp ? new Date(a.timestamp + 'Z').toLocaleTimeString() : '';
      let icon = '&#9679;';
      let color = 'var(--muted)';
      let text = '';

      if (a.type === 'triage_pass') {
        icon = '&#9881;';
        color = '#58a6ff';
        const st = a.stats || {};
        text = `Triage pass: ${st.verdicts_set||0} verdicts, ${st.silence_rules_created||0} rules, ${st.tool_calls||0} actions`;
      } else if (a.type === 'action') {
        if (a.action === 'BULK_SUPPRESS') { icon = '&#128263;'; color = '#8b949e'; text = 'Bulk suppressed alerts'; }
        else if (a.action === 'SET_VERDICT') { icon = '&#9989;'; color = '#3fb950'; text = `Verdict: ${(a.payload_summary||'').substring(0,120)}...`; }
        else if (a.action === 'SILENCE_RULE') { icon = '&#128264;'; color = '#d29922'; text = `Silence rule: ${(a.payload_summary||'').substring(0,120)}...`; }
        else if (a.action === 'CHECK_IP') { icon = '&#128269;'; color = '#58a6ff'; text = `IP check: ${(a.payload_summary||'')}`; }
        else { icon = '&#9654;'; color = '#8b949e'; text = `${a.action}: ${(a.payload_summary||'').substring(0,100)}`; }
      } else if (a.type === 'squawk') {
        icon = '&#128680;'; color = '#f85149';
        text = `SQUAWK [${a.severity}]: ${a.title}`;
      } else if (a.type === 'shift_report') {
        icon = '&#128203;'; color = '#a371f7';
        text = `Shift report: ${a.summary || ''}`;
      } else {
        text = JSON.stringify(a).substring(0, 120);
      }

      feedHtml += `<div style="display:flex;gap:8px;align-items:flex-start;padding:4px 0;border-bottom:1px solid var(--border);">`;
      feedHtml += `<span style="color:${color};flex-shrink:0;font-size:14px;">${icon}</span>`;
      feedHtml += `<div style="flex:1;min-width:0;"><div style="color:var(--text);word-break:break-word;">${esc(text)}</div>`;
      feedHtml += `<div style="color:var(--muted);font-size:10px;">${ts}</div></div></div>`;
    }
    actEl.innerHTML = feedHtml;

  } catch (e) {
    document.getElementById('secStatus').innerHTML = `<span style="color:#e74c3c;">Error: ${e.message}</span>`;
  }
}

// Auto-poll security status (even when panel closed, for bubble color)
setInterval(async () => {
  if (secOpen) return; // panel refresh handles it
  try {
    const res = await fetch(`${API}/api/security-agent/status`);
    const data = await res.json();
    const s = data.status || {};
    const dot = document.getElementById('secDot');
    const bubble = document.getElementById('secBubble');
    if (s.state === 'triaging') { bubble.style.background = '#b8860b'; }
    else if (s.state === 'idle') { bubble.style.background = '#1a6b3c'; }
    else { bubble.style.background = '#8b1a1a'; }
  } catch(e) {}
}, 30000);

// ========== Chat ==========
const chatSessionId = 'board-' + Date.now();
let chatOpen = false;

function toggleChat() {
  chatOpen = !chatOpen;
  const panel = document.getElementById('chatPanel');
  panel.style.display = chatOpen ? 'flex' : 'none';
  if (chatOpen) {
    document.getElementById('chatInput').focus();
    const msgs = document.getElementById('chatMessages');
    if (!msgs.children.length) {
      addChatMsg('assistant', "Hey! I'm your board copilot. Ask me about tasks, agents, memory, or anything else.");
    }
  }
}

function addChatMsg(role, text) {
  const msgs = document.getElementById('chatMessages');
  const div = document.createElement('div');
  const isUser = role === 'user';
  div.style.cssText = `padding:8px 12px;border-radius:8px;font-size:13px;line-height:1.5;max-width:85%;word-wrap:break-word;white-space:pre-wrap;${
    isUser
      ? 'background:var(--accent);color:#fff;align-self:flex-end;'
      : 'background:var(--surface2);color:var(--text);align-self:flex-start;'
  }`;
  div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  addChatMsg('user', msg);

  // Create streaming response bubble
  const bubble = addChatMsg('assistant', '');
  bubble.textContent = '';
  let fullText = '';

  try {
    const res = await fetch(`${API}/api/chat`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, session_id: chatSessionId})
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});

      // Process SSE lines
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.text) {
            fullText += data.text;
            // Show text without ACTION blocks
            bubble.textContent = fullText.replace(/ACTION:\w+\{[^}]*\}/g, '').trim();
            document.getElementById('chatMessages').scrollTop = document.getElementById('chatMessages').scrollHeight;
          }
          if (data.error) {
            bubble.textContent += `\n[Error: ${data.error}]`;
          }
        } catch(e) {}
      }
    }
    // Process any ACTION blocks after stream completes
    processActions(fullText);
  } catch(e) {
    bubble.textContent = `Connection error: ${e.message}`;
  }
}

async function processActions(text) {
  const pattern = /ACTION:(\w+)(\{[^}]*\})/g;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    const action = match[1];
    let payload;
    try { payload = JSON.parse(match[2]); } catch(e) { continue; }
    try {
      if (action === 'CREATE_TASK') {
        if (!payload.status) payload.status = 'todo';
        if (!payload.board) payload.board = 'default';
        const r = await (await fetch(`${API}/api/task`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})).json();
        addChatMsg('assistant', `Created task #${r.id}: ${payload.title}`).style.cssText += 'background:rgba(34,197,94,0.15);border:1px solid var(--done);color:var(--done);font-size:11px;';
        reload();
      } else if (action === 'ASSIGN_TASK') {
        await fetch(`${API}/api/task/${payload.task_id}/claim`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent:payload.agent})});
        addChatMsg('assistant', `Assigned task #${payload.task_id} to ${payload.agent}`).style.cssText += 'background:rgba(59,130,246,0.15);border:1px solid var(--todo);color:var(--todo);font-size:11px;';
        reload(); loadAgents();
      } else if (action === 'MOVE_TASK') {
        await fetch(`${API}/api/task/${payload.task_id}/move`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:payload.status})});
        addChatMsg('assistant', `Moved task #${payload.task_id} to ${payload.status}`).style.cssText += 'background:rgba(249,115,22,0.15);border:1px solid var(--accent);color:var(--accent);font-size:11px;';
        reload();
      } else if (action === 'CHECK_AGENTS') {
        const agents = await (await fetch(`${API}/api/agents`)).json();
        const summary = agents.length ? agents.map(a => `${a.name}: ${a.effective_status}`).join(', ') : 'No agents registered';
        addChatMsg('assistant', `Agents: ${summary}`).style.cssText += 'background:rgba(139,92,246,0.15);border:1px solid #8b5cf6;color:#a78bfa;font-size:11px;';
      } else if (action === 'DIRECTIVE') {
        await fetch(`${API}/api/agent/${encodeURIComponent(payload.agent)}/directive`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:payload.message,from:'dispatcher',priority:payload.priority||'normal'})});
        addChatMsg('assistant', `Directive sent to ${payload.agent}: ${payload.message}`).style.cssText += 'background:rgba(249,115,22,0.15);border:1px solid var(--accent);color:var(--accent);font-size:11px;';
        loadAgents(); loadCommentary();
      }
    } catch(e) {
      addChatMsg('assistant', `Action failed: ${e.message}`).style.cssText += 'background:rgba(239,68,68,0.15);border:1px solid var(--blocked);color:var(--blocked);font-size:11px;';
    }
  }
}

async function clearChat() {
  await fetch(`${API}/api/chat/clear`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id: chatSessionId})
  });
  document.getElementById('chatMessages').innerHTML = '';
  addChatMsg('assistant', "Chat cleared. What can I help with?");
}

// ========== Automations (Monitor tab) ==========
async function loadAutomations() {
  try {
    const res = await fetch(`${API}/api/board?board=monitor`);
    const data = await res.json();
    const all = [...(data.done||[]), ...(data.in_progress||[]), ...(data.todo||[]), ...(data.blocked||[])];
    const grid = document.getElementById('automationsGrid');
    if (!all.length) {
      grid.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">No automations tracked yet</div>';
      return;
    }
    grid.innerHTML = all.map(t => {
      const broken = t.status === 'blocked';
      const cls = broken ? 'automation-card broken' : 'automation-card';
      const desc = t.description ? esc(t.description.substring(0,120)) : '';
      return `<div class="${cls}">
        <div class="auto-dot"></div>
        <div class="auto-info">
          <h4>${esc(t.title.replace('WATCH: ',''))}</h4>
          <div class="desc">${desc}</div>
        </div>
        <span class="health-category">${broken ? 'ALERT' : 'RUNNING'}</span>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('automationsGrid').innerHTML = '<div style="color:var(--text-dim);font-size:12px;">Could not load automations</div>';
  }
}

// ========== Commentary (Agents tab) ==========
async function loadCommentary() {
  try {
    const res = await fetch(`${API}/api/agent/commentary`);
    const items = await res.json();
    const el = document.getElementById('commentaryFeed');
    if (!items.length) {
      el.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:20px;font-size:12px;">No agent commentary yet</div>';
      return;
    }
    el.innerHTML = items.map(c => {
      const t = new Date(c.timestamp * 1000).toLocaleTimeString();
      const task = c.task_id ? `<span class="c-task">#${c.task_id}</span>` : '';
      return `<div class="commentary-item"><span class="c-time">${t}</span><span class="c-agent">${esc(c.agent)}</span>${task}<div class="c-msg">${esc(c.message)}</div></div>`;
    }).join('');
  } catch(e) {}
}

// ========== Workspace (tab removed from UI) ==========
// loadWorkspace, openUploadDoc, saveDoc, deleteDoc, openDoc — removed with Workspace tab

// ========== Monitor ==========
async function runHealthCheck() {
  document.getElementById('healthTime').textContent = 'Checking...';
  document.getElementById('healthGrid').innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:40px;grid-column:1/-1">Running health checks... (pings + SSH may take a few seconds)</div>';
  const res = await fetch(`${API}/api/health/check`, {method:'POST'});
  const data = await res.json();
  renderHealth(data);
}
async function loadCachedHealth() {
  const res = await fetch(`${API}/api/health`);
  const data = await res.json();
  if (data.length) renderHealth(data);
}
function renderHealth(items) {
  const up = items.filter(i => i.ok === true).length;
  const down = items.filter(i => i.ok === false).length;
  const unknown = items.filter(i => i.ok === null).length;

  document.getElementById('healthSummary').innerHTML = `
    <div class="health-summary-card"><div class="num" style="color:var(--done)">${up}</div><div class="label">Healthy</div></div>
    <div class="health-summary-card"><div class="num" style="color:var(--blocked)">${down}</div><div class="label">Down</div></div>
    <div class="health-summary-card"><div class="num" style="color:var(--text-dim)">${unknown}</div><div class="label">Unknown</div></div>
    <div class="health-summary-card"><div class="num" style="color:var(--text)">${items.length}</div><div class="label">Total</div></div>
  `;

  // Group by category
  const categories = {};
  items.forEach(i => {
    const cat = i.category || 'other';
    if (!categories[cat]) categories[cat] = [];
    categories[cat].push(i);
  });

  let html = '';
  for (const [cat, svcs] of Object.entries(categories)) {
    html += `<div style="grid-column:1/-1;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-top:8px;padding:4px 0;border-bottom:1px solid var(--border)">${cat}</div>`;
    svcs.forEach(s => {
      const cls = s.ok === true ? 'up' : s.ok === false ? 'down' : 'unknown';
      const icon = s.ok === true ? '&#9679;' : s.ok === false ? '&#9888;' : '&#63;';
      const iconColor = s.ok === true ? 'var(--done)' : s.ok === false ? 'var(--blocked)' : 'var(--text-dim)';
      const latency = s.latency_ms ? `<span class="latency">${s.latency_ms}ms</span>` : '';
      const detail = s.detail && typeof s.detail === 'object'
        ? `<div class="detail">${esc(JSON.stringify(s.detail).substring(0,120))}</div>`
        : s.detail && typeof s.detail === 'string' && s.ok === false
        ? `<div class="detail">${esc(s.detail.substring(0,100))}</div>`
        : '';
      const note = s.note ? `<div class="detail" style="color:var(--progress)">${esc(s.note)}</div>` : '';
      html += `<div class="health-card ${cls}">
        <span class="health-icon" style="color:${iconColor}">${icon}</span>
        <div class="health-info">
          <h4>${esc(s.name)} <small style="color:var(--text-dim);font-weight:400">(${esc(s.host)})</small> ${latency}</h4>
          <div class="desc">${esc(s.desc)}</div>
          ${detail}${note}
        </div>
        <span class="health-category">${esc(s.type)}</span>
      </div>`;
    });
  }

  document.getElementById('healthGrid').innerHTML = html;
  const checkedAt = items[0]?.checked_at;
  document.getElementById('healthTime').textContent = checkedAt ? `Last check: ${new Date(checkedAt*1000).toLocaleTimeString()}` : '';
}

// ========== Utils ==========
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}
function esc(s) { const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML; }
function timeAgo(ts) {
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ago`;
  return `${Math.floor(s/86400)}d ago`;
}

// ========== War Rooms (tab removed from UI) ==========
// function loadWarRooms() — removed with War Rooms tab

// openWarRoom, editWarRoom, saveWarRoom, openNewWarRoom, createWarRoom — removed with War Rooms tab

// ========== Blockers (tab removed from UI) ==========
// function loadBlockers() — removed with Blockers tab

// ========== Handoffs (tab removed from UI) ==========
// loadHandoffs, viewHandoff, openNewHandoff, createHandoff — removed with Handoffs tab

// ========== Activity Feed (tab removed from UI) ==========
// function loadActivityFeed() — removed with Activity tab

// ========== Tab switching (upgraded) ==========
const _origSwitchTab = switchTab;
switchTab = function(name) {
  _origSwitchTab(name);
  if (name === 'governance') loadGovernance();
};

// Keyboard
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeModal(); closeViewTask(); document.getElementById('agentModal').classList.remove('active'); }
  if (e.key === 'n' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName === 'BODY') { e.preventDefault(); openNewTask(); }
});

// ---- Claude Usage Tracker ----
function fmtCost(v) {
  if (v >= 1) return '$' + v.toFixed(2);
  if (v >= 0.01) return '$' + v.toFixed(2);
  return '$' + v.toFixed(3);
}
function fmtTokens(v) {
  if (v >= 1e6) return (v/1e6).toFixed(1) + 'M';
  if (v >= 1e3) return (v/1e3).toFixed(1) + 'K';
  return String(v);
}
let _usageDetail = false;
function toggleUsageDetail() {
  _usageDetail = !_usageDetail;
  const el = document.getElementById('usageToggle');
  el.textContent = _usageDetail ? 'compact' : 'details';
  loadUsage();
}
function loadUsage() {
  fetch(`${API}/api/claude-usage`).then(r => r.json()).then(d => {
    if (d.error) return;
    const t = d.today || {};
    const w = d.week || {};
    const a = d.all_time || {};
    document.getElementById('usageTodayCost').textContent = fmtCost(t.cost || 0);
    document.getElementById('usageTodayTokens').textContent = fmtTokens((t.input_tokens||0) + (t.output_tokens||0));
    document.getElementById('usageTodayReqs').textContent = String(t.requests || 0);
    document.getElementById('usageWeekCost').textContent = fmtCost(w.cost || 0);
    document.getElementById('usageWeekSessions').textContent = String(w.sessions || 0);
    document.getElementById('usageAllCost').textContent = fmtCost(a.cost || 0);

    // Sparkline (last 7 days)
    const spark = document.getElementById('usageSparkline');
    const byDay = d.by_day || {};
    const days = [];
    for (let i = 6; i >= 0; i--) {
      const dt = new Date(Date.now() - i * 86400000);
      const key = dt.toISOString().split('T')[0];
      days.push({date: key, cost: (byDay[key]||{}).cost || 0, reqs: (byDay[key]||{}).requests || 0});
    }
    const maxCost = Math.max(...days.map(x => x.cost), 0.001);
    spark.innerHTML = days.map(x => {
      const h = Math.max(2, Math.round((x.cost / maxCost) * 20));
      const label = x.date.slice(5) + ': ' + fmtCost(x.cost) + ' (' + x.reqs + ' reqs)';
      return '<div class="bar" style="height:' + h + 'px" title="' + label + '"></div>';
    }).join('');

  }).catch(() => {});
}

// ========== Governance Tab ==========
async function loadGovernance() {
  try { await loadGovPending(); } catch(e) { console.warn('loadGovPending error:', e); }
  try { await loadGovRecent(); } catch(e) { console.warn('loadGovRecent error:', e); }
  try { await loadGovStatus(); } catch(e) { console.warn('loadGovStatus error:', e); }
  const el = document.getElementById('govLastRefresh');
  if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function loadGovPending() {
  const res = await fetch('/api/governance/approvals');
  if (!res.ok) { document.getElementById('govPendingGrid').innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:20px;">Governance not available</div>'; return; }
  const items = await res.json();
  const badge = document.getElementById('govPendingCount');
  if (badge) {
    badge.textContent = items.length;
    badge.style.display = items.length > 0 ? 'inline' : 'none';
  }
  const grid = document.getElementById('govPendingGrid');
  if (!items.length) {
    grid.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:20px;text-align:center;">No pending approvals</div>';
    return;
  }
  grid.innerHTML = items.map(item => {
    const tierColors = {0:'var(--text-dim)',1:'var(--accent)',2:'#f59e0b',3:'#ef4444'};
    const tierColor = tierColors[item.tier] || 'var(--text-dim)';
    const tierLabel = 'Tier ' + item.tier;
    const submitted = item.submitted ? new Date(item.submitted).toLocaleString() : 'unknown';
    const warnings = item.precheck_warnings || [];
    return '<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;border-left:3px solid ' + tierColor + ';">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
        '<span style="font-size:11px;font-weight:600;color:' + tierColor + ';text-transform:uppercase;letter-spacing:0.5px;">' + tierLabel + '</span>' +
        '<span style="font-size:10px;color:var(--text-dim);">' + esc(submitted) + '</span>' +
      '</div>' +
      '<div style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:6px;word-break:break-word;">' + esc(item.action || '') + '</div>' +
      '<div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">Agent: <span style="color:var(--accent);">' + esc(item.agent || 'unknown') + '</span>' +
        (item.task_id ? ' &middot; Task #' + item.task_id : '') + '</div>' +
      '<div style="font-size:11px;color:var(--text-dim);margin-bottom:6px;">Reason: ' + esc(item.reason || 'none') + '</div>' +
      (warnings.length ? '<div style="font-size:11px;color:#f59e0b;margin-bottom:6px;background:rgba(245,158,11,0.1);padding:6px 8px;border-radius:4px;">⚠ ' + warnings.map(w => esc(w)).join('<br>⚠ ') + '</div>' : '') +
      '<div style="display:flex;gap:8px;margin-top:8px;">' +
        '<button class="btn" style="font-size:11px;padding:5px 14px;" onclick="govApprove(\'' + esc(item.approval_id) + '\')">Approve</button>' +
        '<button class="btn btn-ghost" style="font-size:11px;padding:5px 14px;color:#ef4444;border-color:#ef4444;" onclick="govDeny(\'' + esc(item.approval_id) + '\')">Deny</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

async function loadGovRecent() {
  const res = await fetch('/api/governance/approvals/recent');
  if (!res.ok) return;
  const items = await res.json();
  const grid = document.getElementById('govRecentGrid');
  if (!items.length) {
    grid.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:20px;text-align:center;">No recent decisions</div>';
    return;
  }
  grid.innerHTML = items.slice(0, 20).map(item => {
    const statusColors = {approved:'#22c55e', denied:'#ef4444', expired:'var(--text-dim)'};
    const sColor = statusColors[item.status] || 'var(--text-dim)';
    const resolved = item.resolved ? new Date(item.resolved).toLocaleString() : '';
    return '<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;opacity:0.85;">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">' +
        '<span style="font-size:11px;font-weight:600;color:' + sColor + ';text-transform:uppercase;">' + esc(item.status) + '</span>' +
        '<span style="font-size:10px;color:var(--text-dim);">' + esc(resolved) + '</span>' +
      '</div>' +
      '<div style="font-size:12px;color:var(--text);margin-bottom:4px;word-break:break-word;">' + esc(item.action || '') + '</div>' +
      '<div style="font-size:11px;color:var(--text-dim);">Agent: ' + esc(item.agent || 'unknown') +
        ' &middot; Tier ' + (item.tier || '?') +
        (item.deny_reason ? ' &middot; Reason: ' + esc(item.deny_reason) : '') +
      '</div>' +
    '</div>';
  }).join('');
}

async function loadGovStatus() {
  const res = await fetch('/api/governance/status');
  if (!res.ok) return;
  const data = await res.json();
  const panel = document.getElementById('govStatusPanel');
  if (!data.active) {
    panel.innerHTML = '<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;color:#ef4444;">Governance module not loaded</div>';
    return;
  }
  const comps = data.components || {};
  const compHtml = Object.entries(comps).map(([name, ok]) => {
    const dot = ok ? '#22c55e' : '#ef4444';
    const label = name.replace(/_/g, ' ');
    return '<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-dim);">' +
      '<span style="width:6px;height:6px;border-radius:50%;background:' + dot + ';display:inline-block;"></span>' +
      label + '</span>';
  }).join('');
  panel.innerHTML =
    '<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;flex:1;">' +
      '<div style="font-size:12px;color:var(--accent);font-weight:600;margin-bottom:8px;">System Health</div>' +
      '<div style="display:flex;gap:12px;flex-wrap:wrap;">' + compHtml + '</div>' +
      '<div style="margin-top:8px;font-size:11px;color:var(--text-dim);">Policy: ' + (data.policy_loaded ? '<span style="color:#22c55e;">loaded</span>' : '<span style="color:#f59e0b;">not loaded</span>') +
        ' &middot; Pending: <span style="color:var(--accent);">' + (data.pending_approvals || 0) + '</span></div>' +
    '</div>';
}

async function govApprove(id) {
  try {
    const res = await fetch('/api/governance/approvals/' + id + '/approve', {method:'POST'});
    if (res.ok) loadGovernance();
    else alert('Failed to approve: ' + (await res.text()));
  } catch(e) { alert('Error: ' + e.message); }
}

async function govDeny(id) {
  const reason = prompt('Reason for denial (optional):');
  if (reason === null) return; // cancelled
  try {
    const res = await fetch('/api/governance/approvals/' + id + '/deny', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason})
    });
    if (res.ok) loadGovernance();
    else alert('Failed to deny: ' + (await res.text()));
  } catch(e) { alert('Error: ' + e.message); }
}

// Init — each in try-catch so one failure doesn't break the whole page
(async () => {
  for (const fn of [loadTopics, loadFilters, reload, checkHyphae, loadAgents, loadUsage, loadGovPending]) {
    try { await fn(); } catch(e) { console.warn('Init error:', fn.name, e); }
  }
})();
setInterval(() => { try { reload(); } catch(e) {} }, 30000);
setInterval(() => { try { checkHyphae(); } catch(e) {} }, 60000);
setInterval(loadAgents, 10000);
setInterval(loadUsage, 30000);
setInterval(() => { try { loadGovPending(); } catch(e) {} }, 10000);

// ========== Processes ==========
async function loadProcesses() {
  try {
    const res = await fetch('/api/processes');
    const data = await res.json();
    document.getElementById('procLastRefresh').textContent = 'Updated: ' + new Date().toLocaleTimeString();

    let totalCount = 0;

    // Render each host section
    ['local', 'jagg', 'do'].forEach(host => {
      const procs = data[host] || [];
      totalCount += procs.length;
      const containerId = host === 'local' ? 'procLocal' : host === 'jagg' ? 'procJagg' : 'procDO';
      const container = document.getElementById(containerId);
      if (!procs.length) {
        container.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">No processes detected</div>';
        return;
      }
      container.innerHTML = procs.map(p => {
        const statusColor = p.status === 'running' ? 'var(--done, #2db36a)' :
                            p.status === 'error' ? 'var(--blocked, #e53935)' : 'var(--text-dim)';
        const dot = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + statusColor + ';margin-right:6px;"></span>';
        const uptime = p.uptime ? '<span style="color:var(--text-dim);font-size:10px;"> · ' + p.uptime + '</span>' : '';
        const port = p.port ? '<span style="color:var(--accent);font-size:10px;margin-left:4px;">:' + p.port + '</span>' : '';
        const cpu = p.cpu ? '<span style="font-size:10px;color:var(--text-dim);"> · CPU ' + p.cpu + '</span>' : '';
        const mem = p.mem ? '<span style="font-size:10px;color:var(--text-dim);"> · ' + p.mem + '</span>' : '';
        const url = p.url ? ' onclick="window.open(\'' + p.url + '\',\'_blank\')" style="cursor:pointer;"' : '';
        return '<div style="background:var(--card, #1a1a1a);border:1px solid var(--border, #333);border-radius:8px;padding:10px 14px;"' + url + '>' +
          '<div style="font-size:12px;font-weight:600;">' + dot + esc(p.name) + port + '</div>' +
          '<div style="font-size:10px;color:var(--text-dim);margin-top:2px;">' + esc(p.desc || '') + uptime + cpu + mem + '</div>' +
        '</div>';
      }).join('');
    });

    // Update badge
    const badge = document.getElementById('procCount');
    badge.textContent = totalCount;
    badge.style.display = totalCount > 0 ? '' : 'none';
  } catch(e) {
    console.error('loadProcesses error:', e);
  }
}

// ========== Roadmaps ==========
async function loadRoadmaps() {
  const panel = document.getElementById('roadmapPanel');
  const project = document.getElementById('roadmapProject').value;
  try {
    // Populate project filter
    const projResp = await fetch(`${API}/api/projects`);
    const projData = await projResp.json();
    const sel = document.getElementById('roadmapProject');
    const cur = sel.value;
    sel.innerHTML = '<option value="">All projects</option>';
    (projData.projects || []).forEach(p => {
      sel.innerHTML += `<option value="${p.project}" ${p.project===cur?'selected':''}>${p.project} (${p.count})</option>`;
    });

    const params = project ? `?project=${encodeURIComponent(project)}` : '';
    const resp = await fetch(`${API}/api/roadmaps${params}`);
    const data = await resp.json();
    const roadmaps = data.roadmaps || [];

    if (roadmaps.length === 0) {
      panel.innerHTML = '<div style="color:var(--text-dim);font-size:13px;text-align:center;padding:40px;">No roadmaps yet. Create one to start planning.</div>';
      return;
    }

    let html = '';
    for (const rm of roadmaps) {
      // Fetch full view with milestones
      const vResp = await fetch(`${API}/api/roadmap/${rm.id}`);
      const view = await vResp.json();
      const milestones = view.milestones || [];
      const progress = view.progress || 0;
      const statusColor = rm.status === 'completed' ? 'var(--green)' : rm.status === 'archived' ? 'var(--text-dim)' : 'var(--accent)';

      html += `<div style="background:var(--surface-alt);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
          <div>
            <span style="font-size:15px;font-weight:600;">${view.title}</span>
            <span style="font-size:11px;color:var(--text-dim);margin-left:8px;">${view.project}</span>
            <span style="font-size:10px;padding:2px 8px;border-radius:10px;background:${statusColor};color:white;margin-left:8px;">${rm.status}</span>
          </div>
          <div style="font-size:12px;color:var(--text-dim);">${view.total_done || 0}/${view.total_tasks || 0} tasks (${progress}%)</div>
        </div>
        ${view.description ? `<div style="font-size:12px;color:var(--text-dim);margin-bottom:12px;">${view.description}</div>` : ''}
        <div style="height:6px;background:var(--border);border-radius:3px;margin-bottom:16px;overflow:hidden;">
          <div style="height:100%;width:${progress}%;background:var(--accent);border-radius:3px;transition:width 0.3s;"></div>
        </div>`;

      if (milestones.length === 0) {
        html += '<div style="color:var(--text-dim);font-size:12px;padding:8px;">No milestones yet.</div>';
      } else {
        for (const ms of milestones) {
          const msProgress = ms.progress || 0;
          const msStatusIcon = ms.status === 'completed' ? '&#10003;' : ms.status === 'in_progress' ? '&#9654;' : '&#9675;';
          const msColor = ms.status === 'completed' ? 'var(--green)' : ms.status === 'in_progress' ? 'var(--accent)' : 'var(--text-dim)';
          const targetStr = ms.target_date ? new Date(ms.target_date * 1000).toLocaleDateString() : '';

          html += `<div style="border-left:3px solid ${msColor};padding:8px 12px;margin-bottom:8px;background:rgba(255,255,255,0.02);border-radius:0 6px 6px 0;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span style="font-size:13px;font-weight:500;"><span style="color:${msColor}">${msStatusIcon}</span> ${ms.title}</span>
              <span style="font-size:11px;color:var(--text-dim);">${targetStr ? 'Target: '+targetStr : ''} ${ms.done_count}/${ms.task_count}</span>
            </div>
            ${ms.description ? `<div style="font-size:11px;color:var(--text-dim);margin-top:4px;">${ms.description}</div>` : ''}
            <div style="height:3px;background:var(--border);border-radius:2px;margin-top:6px;overflow:hidden;">
              <div style="height:100%;width:${msProgress}%;background:${msColor};border-radius:2px;"></div>
            </div>`;

          // Show linked tasks
          if (ms.tasks && ms.tasks.length > 0) {
            html += '<div style="margin-top:6px;">';
            for (const t of ms.tasks) {
              const tColor = t.status === 'done' ? 'var(--green)' : t.status === 'in_progress' ? 'var(--accent)' : t.status === 'blocked' ? '#f44336' : 'var(--text-dim)';
              const tIcon = t.status === 'done' ? '&#10003;' : t.status === 'in_progress' ? '&#9654;' : t.status === 'blocked' ? '&#10007;' : '&#9675;';
              html += `<div style="font-size:11px;padding:2px 0;color:${tColor};">${tIcon} #${t.id} ${t.title}</div>`;
            }
            html += '</div>';
          }
          html += '</div>';
        }
      }

      html += `<div style="margin-top:8px;display:flex;gap:6px;">
        <button class="btn btn-ghost" onclick="addMilestoneDialog(${rm.id})" style="font-size:11px;padding:4px 8px;">+ Milestone</button>
        <button class="btn btn-ghost" onclick="archiveRoadmap(${rm.id})" style="font-size:11px;padding:4px 8px;">Archive</button>
      </div>`;
      html += '</div>';
    }
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = `<div style="color:#f44336;padding:20px;">Error loading roadmaps: ${e.message}</div>`;
  }
}

function showNewRoadmapDialog() {
  const title = prompt('Roadmap title (e.g., "LLMOS v1.0"):');
  if (!title) return;
  const project = prompt('Project name:', document.getElementById('roadmapProject').value || '');
  if (project === null) return;
  const desc = prompt('Description (optional):', '');

  fetch(`${API}/api/roadmap`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, project, description: desc || ''}),
  }).then(() => loadRoadmaps());
}

function addMilestoneDialog(roadmapId) {
  const title = prompt('Milestone title:');
  if (!title) return;
  const desc = prompt('Description (optional):', '');
  const dateStr = prompt('Target date (YYYY-MM-DD, optional):', '');
  let targetDate = null;
  if (dateStr) {
    targetDate = new Date(dateStr).getTime() / 1000;
    if (isNaN(targetDate)) targetDate = null;
  }

  fetch(`${API}/api/roadmap/${roadmapId}/milestone`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, description: desc || '', target_date: targetDate}),
  }).then(() => {
    // Ask to link tasks
    const taskIds = prompt('Link task IDs (comma-separated, or leave empty):', '');
    if (taskIds && taskIds.trim()) {
      // Get the milestone ID from the response... we need to reload first
      loadRoadmaps();
    } else {
      loadRoadmaps();
    }
  });
}

function archiveRoadmap(roadmapId) {
  if (!confirm('Archive this roadmap?')) return;
  fetch(`${API}/api/roadmap/${roadmapId}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: 'archived'}),
  }).then(() => loadRoadmaps());
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Activity log (in-memory, last 200 events)
# ---------------------------------------------------------------------------

_activity: list[dict] = []
_activity_lock = threading.Lock()


def _log_activity(message: str, agent: str = "") -> None:
    with _activity_lock:
        _activity.insert(0, {
            "timestamp": _now(),
            "agent": agent,
            "message": message,
        })
        if len(_activity) > 200:
            _activity.pop()


# ---------------------------------------------------------------------------
# Board API routes (existing)
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(BOARD_HTML)


@app.route("/api/board")
def api_board():
    kb = _get_kb()
    return jsonify(kb.board_view(
        project=request.args.get("project", ""),
        board=request.args.get("board", ""),
    ))


@app.route("/api/stats")
def api_stats():
    kb = _get_kb()
    return jsonify(kb.stats(project=request.args.get("project", "")))


@app.route("/api/task/<int:task_id>")
def api_get_task(task_id: int):
    kb = _get_kb()
    task = kb.get_task(task_id)
    if task is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(task)


@app.route("/api/task", methods=["POST"])
def api_create_task():
    kb = _get_kb()
    data = request.get_json(force=True)
    tid = kb.add_task(
        title=data.get("title", "Untitled"),
        description=data.get("description", ""),
        status=data.get("status", "todo"),
        priority=data.get("priority", "medium"),
        type=data.get("type", "task"),
        project=data.get("project", ""),
        tags=data.get("tags", ""),
        assigned_to=data.get("assigned_to", ""),
        board=data.get("board", "default"),
        due_date=data.get("due_date"),
    )
    _log_activity(f"Created task #{tid}: {data.get('title', 'Untitled')}")
    return jsonify({"id": tid}), 201


@app.route("/api/task/<int:task_id>", methods=["PATCH"])
def api_update_task(task_id: int):
    kb = _get_kb()
    data = request.get_json(force=True)
    ok = kb.update_task(task_id, **data)
    if not ok:
        return jsonify({"error": "not found or no changes"}), 404
    _log_activity(f"Updated task #{task_id}")
    return jsonify({"ok": True})


@app.route("/api/task/<int:task_id>", methods=["DELETE"])
def api_delete_task(task_id: int):
    kb = _get_kb()
    ok = kb.delete_task(task_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    _log_activity(f"Deleted task #{task_id}")
    return jsonify({"ok": True})


@app.route("/api/task/<int:task_id>/move", methods=["POST"])
def api_move_task(task_id: int):
    kb = _get_kb()
    data = request.get_json(force=True)
    status = data.get("status", "")
    ok = kb.move(task_id, status)
    if not ok:
        return jsonify({"error": "invalid status or not found"}), 400
    _log_activity(f"Moved task #{task_id} to {status}")
    return jsonify({"ok": True})


@app.route("/api/projects")
def api_projects():
    kb = _get_kb()
    rows = kb._conn.execute(
        "SELECT DISTINCT project FROM tasks WHERE project != '' ORDER BY project"
    ).fetchall()
    return jsonify([r["project"] for r in rows])


@app.route("/api/boards")
def api_boards():
    kb = _get_kb()
    return jsonify(kb.list_boards())


# ---------------------------------------------------------------------------
# Agent API routes
# ---------------------------------------------------------------------------

@app.route("/api/agent/register", methods=["POST"])
def api_register_agent():
    """Register a new agent or update an existing one."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    with _agents_lock:
        _agents[name] = {
            "name": name,
            "status": "idle",
            "last_heartbeat": _now(),
            "current_task": None,
            "registered_at": _now(),
            "capabilities": data.get("capabilities", ""),
            "model": data.get("model", ""),
        }

    _log_activity(f"Agent registered", agent=name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/agent/<name>/heartbeat", methods=["POST"])
def api_agent_heartbeat(name: str):
    """Agent heartbeat — call every 30-60s to stay alive."""
    with _agents_lock:
        if name not in _agents:
            return jsonify({"error": "agent not registered"}), 404
        _agents[name]["last_heartbeat"] = _now()
        data = request.get_json(silent=True) or {}
        if "status" in data:
            _agents[name]["status"] = data["status"]
    # Accept optional commentary with heartbeat
    commentary = (data.get("commentary") or "").strip()
    if commentary:
        _add_commentary(name, commentary, data.get("task_id"))
        # Record action for drift detection
        if _governor is not None:
            _governor.on_agent_heartbeat(name, data.get("status", ""), commentary)
    # Return any pending directives
    pending = _pop_directives(name)
    return jsonify({"ok": True, "directives": pending})


@app.route("/api/agent/<name>", methods=["DELETE"])
def api_remove_agent(name: str):
    """Remove an agent from the registry."""
    with _agents_lock:
        if name not in _agents:
            return jsonify({"error": "not found"}), 404
        del _agents[name]
    _log_activity(f"Agent removed", agent=name)
    return jsonify({"ok": True})


@app.route("/api/agents")
def api_list_agents():
    """List all registered agents with effective status and pending directives."""
    with _agents_lock:
        result = []
        for a in _agents.values():
            info = dict(a)
            info["effective_status"] = _agent_effective_status(a)
            with _directives_lock:
                info["pending_directives"] = len(_directives.get(a["name"], []))
            result.append(info)
    return jsonify(result)


@app.route("/api/agent/<name>/directive", methods=["POST"])
def api_agent_directive(name: str):
    """Send a directive to an agent. Agent picks it up on next heartbeat."""
    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    from_agent = data.get("from", "dispatcher")
    priority = data.get("priority", "normal")
    target_host = data.get("target_host", "")
    task_id = data.get("task_id")
    if not message:
        return jsonify({"error": "empty message"}), 400
    with _agents_lock:
        if name not in _agents:
            return jsonify({"error": "agent not registered"}), 404

    # Governance check
    if _governor is not None:
        dr = _governor.dispatch(message, agent=name, task_id=task_id, target_host=target_host or None)
        if not dr.allowed:
            reason = dr.warnings[0] if dr.warnings else (dr.policy.reason if dr.policy else "Denied by governance")
            return jsonify({"error": "governance_denied", "reason": reason}), 403

    _send_directive(name, message, from_agent, priority)
    _log_activity(f"Directive to {name}: {message[:80]}", agent=from_agent)
    _add_commentary(from_agent, f"→ {name}: {message[:120]}")
    return jsonify({"ok": True})


@app.route("/api/agent/<name>/directives")
def api_agent_get_directives(name: str):
    """Peek at pending directives without consuming them."""
    with _directives_lock:
        items = _directives.get(name, [])
    return jsonify(items)


@app.route("/api/agent/<name>/commentary", methods=["POST"])
def api_agent_commentary(name: str):
    """Agent posts a natural-language commentary update."""
    with _agents_lock:
        if name not in _agents:
            return jsonify({"error": "agent not registered"}), 404
    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    task_id = data.get("task_id")
    if not message:
        return jsonify({"error": "empty message"}), 400
    accepted = _add_commentary(name, message, task_id)
    if accepted:
        _log_activity(f"{message[:80]}", agent=name)
    return jsonify({"ok": True, "accepted": accepted})


@app.route("/api/agent/commentary")
def api_list_commentary():
    """Get recent agent commentary entries."""
    agent = request.args.get("agent", "")
    limit = min(int(request.args.get("limit", "50")), 200)
    with _commentary_lock:
        items = _commentary[:limit]
    if agent:
        items = [c for c in items if c["agent"] == agent]
    return jsonify(items)


@app.route("/api/task/<int:task_id>/claim", methods=["POST"])
def api_claim_task(task_id: int):
    """Agent claims a task — sets assigned_to + moves to in_progress atomically."""
    kb = _get_kb()
    data = request.get_json(force=True)
    agent_name = data.get("agent", "").strip()
    if not agent_name:
        return jsonify({"error": "agent name required"}), 400

    task = kb.get_task(task_id)
    if task is None:
        return jsonify({"error": "task not found"}), 404
    if task["status"] == "in_progress" and task["assigned_to"] and task["assigned_to"] != agent_name:
        return jsonify({"error": f"task already claimed by {task['assigned_to']}"}), 409

    kb.update_task(task_id, assigned_to=agent_name, status="in_progress")

    with _agents_lock:
        if agent_name in _agents:
            _agents[agent_name]["status"] = "busy"
            _agents[agent_name]["current_task"] = task_id
            _agents[agent_name]["last_heartbeat"] = _now()

    _log_activity(f"Claimed task #{task_id}: {task['title']}", agent=agent_name)
    return jsonify({"ok": True, "task": kb.get_task(task_id)})


@app.route("/api/task/<int:task_id>/report", methods=["POST"])
def api_report_task(task_id: int):
    """Agent reports task completion or blockage."""
    kb = _get_kb()
    data = request.get_json(force=True)
    agent_name = data.get("agent", "").strip()
    status = data.get("status", "done")  # done or blocked
    report = data.get("report", "")

    if status not in ("done", "blocked"):
        return jsonify({"error": "status must be done or blocked"}), 400

    task = kb.get_task(task_id)
    if task is None:
        return jsonify({"error": "task not found"}), 404

    # Append report to description
    if report:
        existing = task.get("description", "")
        separator = "\n\n---\n" if existing else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        updated_desc = f"{existing}{separator}**[{agent_name} {timestamp}]** {report}"
        kb.update_task(task_id, description=updated_desc, status=status)
    else:
        kb.update_task(task_id, status=status)

    with _agents_lock:
        if agent_name in _agents:
            _agents[agent_name]["status"] = "idle"
            _agents[agent_name]["current_task"] = None
            _agents[agent_name]["last_heartbeat"] = _now()

    _log_activity(f"Reported task #{task_id} as {status}" + (f": {report[:80]}" if report else ""), agent=agent_name)

    # Governance evaluation on completion
    if _governor is not None and status == "done" and report:
        eval_result = _governor.on_task_complete(agent_name, task_id, report, task)
        if not eval_result.approved:
            # Revert to in_progress with feedback
            kb.update_task(task_id, status="in_progress")
            with _agents_lock:
                if agent_name in _agents:
                    _agents[agent_name]["status"] = "busy"
                    _agents[agent_name]["current_task"] = task_id
            return jsonify({"ok": False, "eval_approved": False,
                            "score": eval_result.score, "feedback": eval_result.feedback})

    # Auto-remember significant completions in Hyphae
    if status == "done" and report:
        _hyphae_remember(f"Task #{task_id} completed by {agent_name}: {task['title']}. Result: {report[:200]}")

    return jsonify({"ok": True})


@app.route("/api/task/next", methods=["GET"])
def api_next_task():
    """Get the highest-priority unclaimed todo task for an agent to pick up."""
    kb = _get_kb()
    project = request.args.get("project", "")
    tasks = kb.list_tasks(status="todo", project=project, limit=10)
    # Filter to unassigned tasks
    available = [t for t in tasks if not t.get("assigned_to")]
    if not available:
        return jsonify({"task": None, "message": "no tasks available"})
    return jsonify({"task": available[0]})


# ---------------------------------------------------------------------------
# Activity API
# ---------------------------------------------------------------------------

@app.route("/api/activity_legacy")
def api_activity_legacy():
    with _activity_lock:
        return jsonify(_activity[:100])


# ---------------------------------------------------------------------------
# Workspace API (document/file storage per project)
# ---------------------------------------------------------------------------

_workspace_db_init = False


def _get_workspace_db():
    global _workspace_db_init
    kb = _get_kb()
    if not _workspace_db_init:
        kb._conn.execute("""CREATE TABLE IF NOT EXISTS workspace_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            project TEXT DEFAULT '',
            type TEXT DEFAULT 'document',
            path TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at REAL NOT NULL
        )""")
        kb._conn.commit()
        _workspace_db_init = True
    return kb._conn


@app.route("/api/workspace")
def api_workspace_list():
    conn = _get_workspace_db()
    project = request.args.get("project", "")
    if project:
        rows = conn.execute(
            "SELECT * FROM workspace_docs WHERE project = ? ORDER BY created_at DESC", (project,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workspace_docs ORDER BY project, created_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/workspace", methods=["POST"])
def api_workspace_add():
    conn = _get_workspace_db()
    data = request.get_json(force=True)
    conn.execute(
        "INSERT INTO workspace_docs (title, project, type, path, notes, created_at) VALUES (?,?,?,?,?,?)",
        (data.get("title", ""), data.get("project", ""), data.get("type", "document"),
         data.get("path", ""), data.get("notes", ""), _now()),
    )
    conn.commit()
    _log_activity(f"Added workspace doc: {data.get('title', '')}")
    return jsonify({"ok": True}), 201


@app.route("/api/workspace/<int:doc_id>", methods=["DELETE"])
def api_workspace_delete(doc_id: int):
    conn = _get_workspace_db()
    conn.execute("DELETE FROM workspace_docs WHERE id = ?", (doc_id,))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/workspace/projects")
def api_workspace_projects():
    conn = _get_workspace_db()
    rows = conn.execute(
        "SELECT DISTINCT project FROM workspace_docs WHERE project != '' ORDER BY project"
    ).fetchall()
    return jsonify([r["project"] for r in rows])


# ---------------------------------------------------------------------------
# Health / Monitor API
# ---------------------------------------------------------------------------

@app.route("/api/health")
def api_health():
    """Return cached health check results."""
    with _health_lock:
        results = list(_health_cache.values())
    return jsonify(results)


@app.route("/api/health/check", methods=["POST"])
def api_health_check():
    """Run all health checks and return fresh results."""
    results = run_health_checks()
    _log_activity("Ran infrastructure health check")
    return jsonify(results)


# ---------------------------------------------------------------------------
# Hyphae proxy API
# ---------------------------------------------------------------------------

@app.route("/api/hyphae/status")
def api_hyphae_status():
    """Check if Hyphae is online and return fact count."""
    try:
        req = urllib.request.Request(f"{_HYPHAE_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return jsonify({"online": True, "facts": data.get("facts", 0)})
    except Exception:
        return jsonify({"online": False, "facts": 0})


@app.route("/api/hyphae/recall")
def api_hyphae_recall():
    """Proxy recall to Hyphae."""
    query = request.args.get("query", "")
    top_k = int(request.args.get("top_k", "15"))
    unscoped = request.args.get("unscoped") == "1"
    results = _hyphae_recall(query, top_k=top_k, scoped=not unscoped)
    return jsonify({"results": results})


@app.route("/api/hyphae/remember", methods=["POST"])
def api_hyphae_remember():
    """Proxy remember to Hyphae."""
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty text"}), 400
    ok = _hyphae_remember(text, source=data.get("source", "board"))
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Claude Usage Tracker
# ---------------------------------------------------------------------------

_CLAUDE_DIR = Path.home() / ".claude"
_usage_cache: dict = {}
_usage_cache_ts: float = 0
_USAGE_CACHE_TTL = 15  # seconds

# Pricing per million tokens (USD) — updated as of 2026-03
_MODEL_PRICING = {
    "claude-opus-4-6":   {"input": 15.0, "output": 75.0, "cache_read": 1.5,  "cache_create": 18.75},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_create": 3.75},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_create": 1.0},
}
# Fallback for unknown models — use sonnet pricing
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create": 3.75}


def _estimate_cost(model: str, inp: int, out: int, cache_read: int, cache_create: int) -> float:
    """Estimate USD cost from token counts."""
    p = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
    return (
        inp * p["input"] / 1e6
        + out * p["output"] / 1e6
        + cache_read * p["cache_read"] / 1e6
        + cache_create * p["cache_create"] / 1e6
    )


def _scan_claude_usage() -> dict:
    """Scan Claude Code session JSONL files for usage/cost data."""
    global _usage_cache, _usage_cache_ts
    now = time.time()
    if now - _usage_cache_ts < _USAGE_CACHE_TTL and _usage_cache:
        return _usage_cache

    today = datetime.now(timezone.utc).date()
    week_ago = today - timedelta(days=7)

    def _bucket():
        return {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_create": 0, "sessions": 0, "requests": 0}

    totals = {
        "today": _bucket(),
        "week": _bucket(),
        "all_time": _bucket(),
        "by_model": {},
        "by_day": {},
    }

    # Scan all project dirs and the sessions dir
    jsonl_dirs = []
    projects_dir = _CLAUDE_DIR / "projects"
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            if d.is_dir():
                jsonl_dirs.append(d)
    sessions_dir = _CLAUDE_DIR / "sessions"
    if sessions_dir.exists():
        jsonl_dirs.append(sessions_dir)

    for scan_dir in jsonl_dirs:
        for fp in scan_dir.glob("*.jsonl"):
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                continue
            file_date = datetime.fromtimestamp(mtime, tz=timezone.utc).date()

            session_counted = {"today": False, "week": False, "all_time": False}

            # First pass: deduplicate by message ID (keep last occurrence)
            msg_map: dict[str, dict] = {}  # msg_id -> {model, usage, date}
            try:
                with open(fp, "r") as f:
                    for line in f:
                        if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if evt.get("type") != "assistant":
                            continue

                        msg = evt.get("message", {})
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        msg_id = msg.get("id")
                        if not msg_id:
                            continue

                        evt_date = file_date
                        ts = evt.get("timestamp")
                        if ts:
                            try:
                                evt_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                            except (ValueError, AttributeError):
                                pass

                        # Overwrite — last occurrence has final output token count
                        msg_map[msg_id] = {
                            "model": msg.get("model", "unknown"),
                            "input_tokens": usage.get("input_tokens", 0) or 0,
                            "output_tokens": usage.get("output_tokens", 0) or 0,
                            "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                            "cache_create": usage.get("cache_creation_input_tokens", 0) or 0,
                            "date": evt_date,
                        }
            except (OSError, UnicodeDecodeError):
                continue

            # Second pass: aggregate deduplicated messages
            for rec in msg_map.values():
                model = rec["model"]
                inp = rec["input_tokens"]
                out = rec["output_tokens"]
                cache_r = rec["cache_read"]
                cache_c = rec["cache_create"]
                evt_date = rec["date"]
                cost = _estimate_cost(model, inp, out, cache_r, cache_c)

                # All time
                totals["all_time"]["cost"] += cost
                totals["all_time"]["input_tokens"] += inp
                totals["all_time"]["output_tokens"] += out
                totals["all_time"]["cache_read"] += cache_r
                totals["all_time"]["cache_create"] += cache_c
                totals["all_time"]["requests"] += 1
                if not session_counted["all_time"]:
                    totals["all_time"]["sessions"] += 1
                    session_counted["all_time"] = True

                # By model
                if model not in totals["by_model"]:
                    totals["by_model"][model] = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0}
                totals["by_model"][model]["cost"] += cost
                totals["by_model"][model]["input_tokens"] += inp
                totals["by_model"][model]["output_tokens"] += out
                totals["by_model"][model]["requests"] += 1

                # By day (last 7 days)
                if evt_date >= week_ago:
                    day_key = evt_date.isoformat()
                    if day_key not in totals["by_day"]:
                        totals["by_day"][day_key] = {"cost": 0.0, "requests": 0}
                    totals["by_day"][day_key]["cost"] += cost
                    totals["by_day"][day_key]["requests"] += 1

                # Week
                if evt_date >= week_ago:
                    totals["week"]["cost"] += cost
                    totals["week"]["input_tokens"] += inp
                    totals["week"]["output_tokens"] += out
                    totals["week"]["cache_read"] += cache_r
                    totals["week"]["cache_create"] += cache_c
                    totals["week"]["requests"] += 1
                    if not session_counted["week"]:
                        totals["week"]["sessions"] += 1
                        session_counted["week"] = True

                # Today
                if evt_date == today:
                    totals["today"]["cost"] += cost
                    totals["today"]["input_tokens"] += inp
                    totals["today"]["output_tokens"] += out
                    totals["today"]["cache_read"] += cache_r
                    totals["today"]["cache_create"] += cache_c
                    totals["today"]["requests"] += 1
                    if not session_counted["today"]:
                        totals["today"]["sessions"] += 1
                        session_counted["today"] = True

    _usage_cache = totals
    _usage_cache_ts = now
    return totals


@app.route("/api/claude-usage")
def api_claude_usage():
    """Return Claude Code usage stats (today / week / all-time)."""
    try:
        data = _scan_claude_usage()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Agent Conversation API
# ---------------------------------------------------------------------------

@app.route("/api/agent/<name>/conversation", methods=["GET", "POST"])
def api_agent_conversation(name):
    """Get or add conversation history for an agent."""
    if request.method == "POST":
        data = request.get_json(force=True)
        direction = data.get("direction", "outbound")
        message = data.get("message", "").strip()
        from_who = data.get("from_who", "")
        if message:
            _persist_conversation(name, direction, message, from_who=from_who)
        return jsonify({"ok": True})

    limit = int(request.args.get("limit", 100))
    try:
        conn = _get_conv_db()
        rows = conn.execute(
            "SELECT agent, direction, message, from_who, task_id, priority, timestamp "
            "FROM agent_conversations WHERE agent = ? ORDER BY timestamp ASC LIMIT ?",
            (name, limit),
        ).fetchall()
        entries = [
            {
                "agent": r[0], "direction": r[1], "message": r[2],
                "from_who": r[3], "task_id": r[4], "priority": r[5],
                "timestamp": r[6],
            }
            for r in rows
        ]
        return jsonify(entries)
    except Exception as e:
        return jsonify([])


# ---------------------------------------------------------------------------
# Security Agent Readout API
# ---------------------------------------------------------------------------

@app.route("/api/security-agent/status")
def api_security_agent_status():
    """Get current Claude security agent status and recent activity."""
    import sys
    sys.path.insert(0, str(Path.home() / "security-shallots"))
    try:
        from shallots.ai.claude_agent import read_status, read_activity
        status = read_status()
        activity = read_activity(limit=30)
        return jsonify({"status": status, "activity": activity})
    except Exception as e:
        return jsonify({"status": {"state": "error", "error": str(e)}, "activity": []})


# ---------------------------------------------------------------------------
# Topic Channels API
# ---------------------------------------------------------------------------


@app.route("/api/topics", methods=["GET"])
def api_topics_list():
    """List all active topic channels (pinned first, then by updated_at desc)."""
    conn = _get_topics_db()
    rows = conn.execute(
        """SELECT t.*, (SELECT content FROM topic_messages
           WHERE channel_id = t.id ORDER BY timestamp DESC LIMIT 1) as last_message
           FROM topic_channels t WHERE t.status = 'active'
           ORDER BY t.pinned DESC, t.updated_at DESC"""
    ).fetchall()
    cols = ["id", "name", "description", "project", "status", "created_at",
            "updated_at", "pinned", "model", "last_message"]
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route("/api/topics", methods=["POST"])
def api_topics_create():
    """Create a new topic channel."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    desc = data.get("description", "")
    project = data.get("project", "")
    model = data.get("model", "sonnet")
    if model not in ("sonnet", "opus"):
        model = "sonnet"
    now = time.time()
    conn = _get_topics_db()
    cur = conn.execute(
        "INSERT INTO topic_channels (name, description, project, status, created_at, updated_at, pinned, model) VALUES (?,?,?,?,?,?,?,?)",
        (name, desc, project, "active", now, now, 0, model),
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid, "name": name}), 201


@app.route("/api/topics/<int:topic_id>", methods=["PATCH"])
def api_topics_update(topic_id):
    """Update a topic channel (name, description, status, pinned)."""
    data = request.get_json(force=True)
    conn = _get_topics_db()
    row = conn.execute("SELECT id FROM topic_channels WHERE id=?", (topic_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    updates = []
    vals = []
    for field in ("name", "description", "status", "pinned", "project", "model"):
        if field in data:
            updates.append(f"{field}=?")
            vals.append(data[field])
    if updates:
        updates.append("updated_at=?")
        vals.append(time.time())
        vals.append(topic_id)
        conn.execute(f"UPDATE topic_channels SET {','.join(updates)} WHERE id=?", vals)
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/topics/<int:topic_id>/model", methods=["POST"])
def api_topic_model(topic_id):
    """Quick toggle for topic model (sonnet/opus)."""
    data = request.get_json() or {}
    model = data.get("model", "sonnet")
    if model not in ("sonnet", "opus"):
        return jsonify({"error": "model must be 'sonnet' or 'opus'"}), 400
    db = _get_topics_db()
    db.execute("UPDATE topic_channels SET model=?, updated_at=? WHERE id=?", (model, time.time(), topic_id))
    db.commit()
    return jsonify({"ok": True, "model": model})


@app.route("/api/topics/<int:topic_id>", methods=["DELETE"])
def api_topics_delete(topic_id):
    """Archive a topic channel."""
    conn = _get_topics_db()
    conn.execute("UPDATE topic_channels SET status='archived', updated_at=? WHERE id=?",
                 (time.time(), topic_id))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/topics/<int:topic_id>/messages", methods=["GET"])
def api_topic_messages(topic_id):
    """Get messages for a topic channel."""
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    conn = _get_topics_db()
    rows = conn.execute(
        """SELECT id, channel_id, role, agent_name, content, timestamp
           FROM topic_messages WHERE channel_id=?
           ORDER BY timestamp ASC LIMIT ? OFFSET ?""",
        (topic_id, limit, offset),
    ).fetchall()
    cols = ["id", "channel_id", "role", "agent_name", "content", "timestamp"]
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route("/api/topics/<int:topic_id>/message", methods=["POST"])
def api_topic_add_message(topic_id):
    """Manually add a message to a topic (for agent reports)."""
    data = request.get_json(force=True)
    role = data.get("role", "system")
    agent_name = data.get("agent_name", "")
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    now = time.time()
    conn = _get_topics_db()
    conn.execute(
        "INSERT INTO topic_messages (channel_id, role, agent_name, content, timestamp) VALUES (?,?,?,?,?)",
        (topic_id, role, agent_name, content, now),
    )
    conn.execute("UPDATE topic_channels SET updated_at=? WHERE id=?", (now, topic_id))
    conn.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/topics/<int:topic_id>/chat", methods=["POST"])
def api_topic_chat(topic_id):
    """Send a message to a topic and stream Claude's response."""
    import subprocess

    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    conn = _get_topics_db()
    topic_row = conn.execute(
        "SELECT name, description, project, model FROM topic_channels WHERE id=?", (topic_id,)
    ).fetchone()
    if not topic_row:
        return jsonify({"error": "topic not found"}), 404
    topic_name, topic_desc, topic_project, topic_model = topic_row
    if topic_model not in ("sonnet", "opus"):
        topic_model = "sonnet"

    # Save user message
    now = time.time()
    conn.execute(
        "INSERT INTO topic_messages (channel_id, role, agent_name, content, timestamp) VALUES (?,?,?,?,?)",
        (topic_id, "user", "", message, now),
    )
    conn.execute("UPDATE topic_channels SET updated_at=? WHERE id=?", (now, topic_id))
    conn.commit()

    # Build context: board state
    kb = _get_kb()
    board_context = ""
    try:
        stats = kb.stats()
        if stats["total"] > 0:
            view = kb.board_view()
            parts = [f"Board has {stats['total']} tasks: {stats['by_status']}"]
            for status, tasks in view.items():
                for t in tasks[:3]:
                    parts.append(f"  #{t['id']} [{status}] {t['title']} (pri={t['priority']}, assigned={t.get('assigned_to', '-')})")
            board_context = "\n".join(parts)
    except Exception:
        pass

    # Agent context
    agent_context = ""
    with _agents_lock:
        if _agents:
            parts = []
            for a in _agents.values():
                es = _agent_effective_status(a)
                task_info = f", working on #{a['current_task']}" if a.get('current_task') else ""
                parts.append(f"{a['name']}({es}{task_info})")
            agent_context = "Registered agents: " + ", ".join(parts)

    # Hyphae recall for topic context
    hyphae_context = ""
    try:
        search_terms = f"{topic_name} {message}"[:200]
        req_data = json.dumps({"query": search_terms, "top_k": 5}).encode()
        hreq = urllib.request.Request(
            f"{_HYPHAE_URL}/recall",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(hreq, timeout=3) as hresp:
            hresult = json.loads(hresp.read().decode())
            memories = hresult.get("results", [])
            if memories:
                mem_parts = []
                for m in memories[:5]:
                    text = m.get("text", "")
                    if text:
                        mem_parts.append(f"- {text}")
                if mem_parts:
                    hyphae_context = "## Relevant Memory\n" + "\n".join(mem_parts)
    except Exception:
        pass

    # Topic message history (last 20)
    history_rows = conn.execute(
        """SELECT role, agent_name, content FROM topic_messages
           WHERE channel_id=? ORDER BY timestamp DESC LIMIT 20""",
        (topic_id,),
    ).fetchall()
    history_rows.reverse()  # chronological order

    # Build system prompt — varies by model
    if topic_model == "opus":
        system = "You are a deep-thinking assistant for the OpenKeel Command Board. Plan carefully, reason through problems, and orchestrate multi-step operations. Use ACTION blocks to create tasks, dispatch agents, and coordinate work. Think step by step."
    else:
        system = "You are a fast assistant for the OpenKeel Command Board. Answer questions quickly using the context provided. For complex work, use ACTION blocks to create tasks and dispatch agents."
    system += f"\n\n## Current Topic: {topic_name}"
    if topic_desc:
        system += f"\n{topic_desc}"
    if topic_project:
        system += f"\nProject: {topic_project}"
    if board_context:
        system += f"\n\nCurrent board state:\n{board_context}"
    if agent_context:
        system += f"\n\n{agent_context}"
    if hyphae_context:
        system += f"\n\n{hyphae_context}"

    # Build prompt with history
    prompt_parts = [system, ""]
    for role, aname, content in history_rows[:-1]:  # exclude the just-added user msg
        if role == "user":
            prompt_parts.append(f"User: {content}")
        elif role == "assistant":
            prompt_parts.append(f"Assistant: {content}")
        elif role == "agent":
            prompt_parts.append(f"[Agent {aname}]: {content}")
        elif role == "system":
            prompt_parts.append(f"[System]: {content}")
    prompt_parts.append(f"\nUser: {message}")
    full_prompt = "\n".join(prompt_parts)

    def generate():
        try:
            proc = subprocess.Popen(
                ["claude", "-p", "--output-format", "stream-json", "--verbose",
                 "--no-session-persistence", "--model", topic_model],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc.stdin.write(full_prompt)
            proc.stdin.close()

            full_response = ""
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    etype = event.get("type", "")
                    if etype == "assistant":
                        msg = event.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                text = block["text"]
                                if text and text not in full_response:
                                    new_text = text[len(full_response):] if text.startswith(full_response) else text
                                    if new_text:
                                        full_response += new_text
                                        yield f"data: {json.dumps({'text': new_text})}\n\n"
                    elif etype == "result":
                        result_text = event.get("result", "")
                        if result_text and not full_response:
                            full_response = result_text
                            yield f"data: {json.dumps({'text': result_text})}\n\n"
                except json.JSONDecodeError:
                    continue

            proc.wait(timeout=10)

            if not full_response:
                stderr = proc.stderr.read()
                if stderr:
                    yield f"data: {json.dumps({'error': stderr[:200]})}\n\n"

            # Save assistant response to DB
            try:
                save_now = time.time()
                conn2 = _get_topics_db()
                conn2.execute(
                    "INSERT INTO topic_messages (channel_id, role, agent_name, content, timestamp) VALUES (?,?,?,?,?)",
                    (topic_id, "assistant", "", full_response, save_now),
                )
                conn2.execute("UPDATE topic_channels SET updated_at=? WHERE id=?", (save_now, topic_id))
                conn2.commit()
            except Exception:
                pass

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Chat API (Claude via Anthropic SDK, streaming) — backwards compat
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Send a message to Claude CLI and stream the response."""
    import subprocess

    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    session_id = data.get("session_id", "default")
    if not message:
        return jsonify({"error": "empty message"}), 400

    # Build context about current board state
    kb = _get_kb()
    board_context = ""
    try:
        stats = kb.stats()
        if stats["total"] > 0:
            view = kb.board_view()
            parts = [f"Board has {stats['total']} tasks: {stats['by_status']}"]
            for status, tasks in view.items():
                for t in tasks[:3]:
                    parts.append(f"  #{t['id']} [{status}] {t['title']} (pri={t['priority']}, assigned={t.get('assigned_to', '-')})")
            board_context = "\n".join(parts)
    except Exception:
        pass

    agent_context = ""
    with _agents_lock:
        if _agents:
            parts = []
            for a in _agents.values():
                es = _agent_effective_status(a)
                task_info = f", working on #{a['current_task']}" if a.get('current_task') else ""
                parts.append(f"{a['name']}({es}{task_info})")
            agent_context = "Registered agents: " + ", ".join(parts)

    commentary_context = ""
    with _commentary_lock:
        recent = _commentary[:5]
    if recent:
        commentary_context = "Recent agent commentary:\n" + "\n".join(
            f"  [{c['agent']}] {c['message']}" for c in recent
        )

    system = _CHAT_SYSTEM_PROMPT
    if board_context:
        system += f"\n\nCurrent board state:\n{board_context}"
    if agent_context:
        system += f"\n\n{agent_context}"
    if commentary_context:
        system += f"\n\n{commentary_context}"

    # Build the full prompt with conversation history
    if session_id not in _chat_histories:
        _chat_histories[session_id] = []
    history = _chat_histories[session_id]
    history.append({"role": "user", "content": message})
    if len(history) > 20:
        history[:] = history[-20:]

    # Build prompt with history context for claude -p (single-turn)
    prompt_parts = [system, ""]
    for msg in history[:-1]:  # prior messages as context
        role = "User" if msg["role"] == "user" else "Assistant"
        prompt_parts.append(f"{role}: {msg['content']}")
    prompt_parts.append(f"\nUser: {message}")
    full_prompt = "\n".join(prompt_parts)

    def generate():
        try:
            proc = subprocess.Popen(
                ["claude", "-p", "--output-format", "stream-json", "--verbose",
                 "--no-session-persistence", "--model", "sonnet"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc.stdin.write(full_prompt)
            proc.stdin.close()

            full_response = ""
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    etype = event.get("type", "")

                    # Assistant message with content blocks
                    if etype == "assistant":
                        msg = event.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                text = block["text"]
                                if text and text not in full_response:
                                    new_text = text[len(full_response):] if text.startswith(full_response) else text
                                    if new_text:
                                        full_response += new_text
                                        yield f"data: {json.dumps({'text': new_text})}\n\n"

                    # Final result
                    elif etype == "result":
                        result_text = event.get("result", "")
                        if result_text and not full_response:
                            full_response = result_text
                            yield f"data: {json.dumps({'text': result_text})}\n\n"

                except json.JSONDecodeError:
                    continue

            proc.wait(timeout=10)

            if not full_response:
                stderr = proc.stderr.read()
                if stderr:
                    yield f"data: {json.dumps({'error': stderr[:200]})}\n\n"

            history.append({"role": "assistant", "content": full_response})
            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/chat/clear", methods=["POST"])
def api_chat_clear():
    """Clear chat history for a session."""
    data = request.get_json(force=True)
    session_id = data.get("session_id", "default")
    _chat_histories.pop(session_id, None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _prune_offline_agents():
    """Remove agents that aren't from the scanner or are stale."""
    cutoff = _now() - 300  # 5 minutes
    with _agents_lock:
        to_remove = []
        for name, a in _agents.items():
            # Always remove non-scanner agents that go stale
            if not name.startswith("claude-pts-"):
                if a.get("last_heartbeat", 0) < cutoff:
                    to_remove.append(name)
            # Remove scanner agents that are offline for 5+ minutes
            elif _agent_effective_status(a) == "offline" and a.get("last_heartbeat", 0) < cutoff:
                to_remove.append(name)
        for name in to_remove:
            del _agents[name]
    if to_remove:
        _log_activity(f"Auto-pruned {len(to_remove)} stale agent(s): {', '.join(to_remove)}")


def _start_pruner():
    """Background thread that prunes offline agents every 2 minutes."""
    def _loop():
        while True:
            time.sleep(120)
            _prune_offline_agents()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _start_session_scanner():
    """Background thread that discovers running Claude sessions and registers them."""
    import subprocess as _sp
    import re as _re

    def _scan():
        while True:
            try:
                proc = _sp.run(
                    ["ps", "-eo", "pid,tty,lstart,args"],
                    capture_output=True, text=True, timeout=10,
                )
                seen_pids = set()
                for line in (proc.stdout or "").strip().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 8:
                        continue
                    pid = parts[0]
                    tty = parts[1]
                    # lstart is like "Thu Mar 26 10:58:42 2026" (5 fields)
                    args_start = line.find(parts[7]) if len(parts) > 7 else -1
                    cmd_str = line[args_start:].strip() if args_start > 0 else ""
                    # Only match standalone "claude" processes (not subprocesses)
                    if not _re.match(r'^(claude|/[^\s]*claude)(\s|$)', cmd_str):
                        continue
                    # Skip our own subprocess calls (claude -p)
                    if "-p" in cmd_str or "--print" in cmd_str:
                        continue

                    seen_pids.add(pid)
                    # Only track interactive sessions (with a TTY)
                    if tty == "?" or tty == "":
                        continue
                    agent_name = f"claude-{tty.replace('/', '-')}"

                    # Detect project from cwd
                    try:
                        cwd = os.readlink(f"/proc/{pid}/cwd")
                        project = os.path.basename(cwd)
                    except (OSError, FileNotFoundError):
                        project = "unknown"

                    with _agents_lock:
                        if agent_name not in _agents:
                            _agents[agent_name] = {
                                "name": agent_name,
                                "status": "busy",
                                "last_heartbeat": time.time(),
                                "current_task": None,
                                "registered_at": time.time(),
                                "capabilities": f"project: {project}",
                                "model": "",
                                "pid": pid,
                                "tty": tty,
                                "project": project,
                            }
                        else:
                            _agents[agent_name]["last_heartbeat"] = time.time()
                            _agents[agent_name]["status"] = "busy"
                            _agents[agent_name]["pid"] = pid
                            # Update project if changed
                            try:
                                cwd = os.readlink(f"/proc/{pid}/cwd")
                                _agents[agent_name]["project"] = os.path.basename(cwd)
                                _agents[agent_name]["capabilities"] = f"project: {os.path.basename(cwd)}"
                            except (OSError, FileNotFoundError):
                                pass

                # Mark agents whose pids are gone
                with _agents_lock:
                    for name, agent in list(_agents.items()):
                        if "pid" in agent and agent["pid"] not in seen_pids:
                            if name.startswith("claude-pts-") or name.startswith("claude-pid-"):
                                agent["status"] = "offline"

            except Exception:
                pass
            time.sleep(15)

    t = threading.Thread(target=_scan, daemon=True)
    t.start()


# =====================================================================
# WAR ROOMS API
# =====================================================================

@app.route("/api/warrooms")
def api_warrooms():
    kb = _get_kb()
    rows = kb._conn.execute("SELECT * FROM war_rooms ORDER BY updated_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/warroom/<project>")
def api_warroom(project: str):
    kb = _get_kb()
    row = kb._conn.execute("SELECT * FROM war_rooms WHERE project = ?", (project,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    wr = dict(row)
    # Include project tasks
    tasks = kb.list_tasks(project=project, limit=100)
    wr["tasks"] = tasks
    # Include recent activity
    activity = kb._conn.execute(
        "SELECT * FROM activity_feed WHERE project = ? ORDER BY timestamp DESC LIMIT 20",
        (project,)
    ).fetchall()
    wr["activity"] = [dict(a) for a in activity]
    # Include pending handoffs
    handoffs = kb._conn.execute(
        "SELECT * FROM handoffs WHERE project = ? ORDER BY timestamp DESC LIMIT 5",
        (project,)
    ).fetchall()
    wr["handoffs"] = [dict(h) for h in handoffs]
    return jsonify(wr)

@app.route("/api/warroom", methods=["POST"])
def api_create_warroom():
    kb = _get_kb()
    d = request.json or {}
    project = d.get("project", "").strip()
    if not project:
        return jsonify({"error": "project required"}), 400
    now = _now()
    try:
        kb._conn.execute(
            "INSERT INTO war_rooms (project, summary, blockers, key_files, decisions, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (project, d.get("summary",""), d.get("blockers",""), d.get("key_files",""), d.get("decisions",""), d.get("notes",""), now, now)
        )
        kb._conn.commit()
    except Exception:
        # Already exists, update instead
        kb._conn.execute(
            "UPDATE war_rooms SET summary=?, blockers=?, key_files=?, decisions=?, notes=?, updated_at=?, status=? WHERE project=?",
            (d.get("summary",""), d.get("blockers",""), d.get("key_files",""), d.get("decisions",""), d.get("notes",""), now, d.get("status","active"), project)
        )
        kb._conn.commit()
    return jsonify({"ok": True, "project": project})

@app.route("/api/warroom/<project>", methods=["PATCH"])
def api_update_warroom(project: str):
    kb = _get_kb()
    d = request.json or {}
    fields = {k: v for k, v in d.items() if k in ("summary","blockers","key_files","decisions","notes","status")}
    if not fields:
        return jsonify({"error": "no fields"}), 400
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    kb._conn.execute(f"UPDATE war_rooms SET {set_clause} WHERE project=?", list(fields.values()) + [project])
    kb._conn.commit()
    return jsonify({"ok": True})

# =====================================================================
# ACTIVITY FEED API
# =====================================================================

@app.route("/api/activity")
def api_activity():
    kb = _get_kb()
    project = request.args.get("project", "")
    limit = int(request.args.get("limit", 50))
    if project:
        rows = kb._conn.execute(
            "SELECT * FROM activity_feed WHERE project = ? ORDER BY timestamp DESC LIMIT ?",
            (project, limit)
        ).fetchall()
    else:
        rows = kb._conn.execute(
            "SELECT * FROM activity_feed ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/activity", methods=["POST"])
def api_post_activity():
    kb = _get_kb()
    d = request.json or {}
    kb._conn.execute(
        "INSERT INTO activity_feed (timestamp, agent, project, action_type, summary, details, task_id) VALUES (?,?,?,?,?,?,?)",
        (_now(), d.get("agent",""), d.get("project",""), d.get("action_type","update"), d.get("summary",""), d.get("details",""), d.get("task_id"))
    )
    kb._conn.commit()
    return jsonify({"ok": True})

# =====================================================================
# HANDOFF PACKETS API
# =====================================================================

@app.route("/api/handoffs")
def api_handoffs():
    kb = _get_kb()
    project = request.args.get("project", "")
    if project:
        rows = kb._conn.execute("SELECT * FROM handoffs WHERE project = ? ORDER BY timestamp DESC LIMIT 20", (project,)).fetchall()
    else:
        rows = kb._conn.execute("SELECT * FROM handoffs ORDER BY timestamp DESC LIMIT 20").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/handoff", methods=["POST"])
def api_create_handoff():
    kb = _get_kb()
    d = request.json or {}
    kb._conn.execute(
        "INSERT INTO handoffs (project, from_agent, to_agent, timestamp, status_summary, in_progress, blocked_on, next_steps, files_touched, key_decisions, warnings) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (d.get("project",""), d.get("from_agent",""), d.get("to_agent",""), _now(),
         d.get("status_summary",""), d.get("in_progress",""), d.get("blocked_on",""),
         d.get("next_steps",""), d.get("files_touched",""), d.get("key_decisions",""), d.get("warnings",""))
    )
    kb._conn.commit()
    return jsonify({"ok": True})

@app.route("/api/handoff/<int:hid>/pickup", methods=["POST"])
def api_pickup_handoff(hid: int):
    kb = _get_kb()
    d = request.json or {}
    kb._conn.execute("UPDATE handoffs SET picked_up=1, picked_up WHERE id=?", (hid,))
    kb._conn.commit()
    return jsonify({"ok": True})

# =====================================================================
# DIRECTIVES API
# =====================================================================

@app.route("/api/directives")
def api_directives():
    kb = _get_kb()
    show_all = request.args.get("all", "0") == "1"
    if show_all:
        rows = kb._conn.execute("SELECT * FROM directives ORDER BY created_at DESC LIMIT 50").fetchall()
    else:
        rows = kb._conn.execute("SELECT * FROM directives WHERE picked_up = 0 ORDER BY priority DESC, created_at ASC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/directive", methods=["POST"])
def api_create_directive():
    kb = _get_kb()
    d = request.json or {}
    kb._conn.execute(
        "INSERT INTO directives (target_agent, project, priority, message, created_at) VALUES (?,?,?,?,?)",
        (d.get("target_agent",""), d.get("project",""), d.get("priority","normal"), d.get("message",""), _now())
    )
    kb._conn.commit()
    return jsonify({"ok": True})

# =====================================================================
# BLOCKERS VIEW API
# =====================================================================

@app.route("/api/blockers")
def api_blockers():
    kb = _get_kb()
    # Blocked tasks
    blocked_tasks = kb.list_tasks(status="blocked", limit=50)
    # War room blockers
    wr_rows = kb._conn.execute("SELECT project, blockers FROM war_rooms WHERE blockers != '' AND status='active'").fetchall()
    war_room_blockers = [{"project": r["project"], "blockers": r["blockers"]} for r in wr_rows]
    return jsonify({"blocked_tasks": blocked_tasks, "war_room_blockers": war_room_blockers})


def main():
    import argparse

    parser = argparse.ArgumentParser(description="OpenKeel Kanban Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8200, help="Port number")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    parser.add_argument("--remote", action="store_true",
                        help="Bind to 0.0.0.0 with PIN auth for remote access")
    parser.add_argument("--pin", default=os.environ.get("COMMAND_PIN", ""),
                        help="PIN for remote access auth")
    args = parser.parse_args()

    if args.remote:
        args.host = "0.0.0.0"
        if args.pin:
            @app.before_request
            def _check_remote_auth():
                # Localhost always allowed
                if request.remote_addr in ("127.0.0.1", "::1"):
                    return None
                # Check session cookie
                if request.cookies.get("command_pin") == args.pin:
                    return None
                # Check if this is the auth endpoint
                if request.path == "/auth" and request.method == "POST":
                    return None
                # Check query param (for initial auth)
                if request.args.get("pin") == args.pin:
                    resp = app.make_response(
                        __import__("flask").redirect(request.path))
                    resp.set_cookie("command_pin", args.pin, max_age=86400 * 30,
                                    httponly=True, samesite="Lax")
                    return resp
                # Show login page
                return Response("""<!DOCTYPE html>
<html><head><title>OpenKeel Command — Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui;display:flex;
justify-content:center;align-items:center;min-height:100vh;margin:0;}
.box{background:#161b22;padding:40px;border-radius:12px;text-align:center;
border:1px solid #30363d;max-width:300px;width:90%;}
h1{font-size:18px;margin:0 0 20px;}
input{width:100%;padding:14px;font-size:18px;background:#0d1117;color:#c9d1d9;
border:1px solid #30363d;border-radius:8px;text-align:center;letter-spacing:8px;
box-sizing:border-box;margin-bottom:12px;}
button{width:100%;padding:14px;font-size:14px;background:#238636;color:#fff;
border:none;border-radius:8px;cursor:pointer;font-weight:600;}
</style></head><body>
<div class="box"><h1>OpenKeel Command</h1>
<form method="POST" action="/auth">
<input type="password" name="pin" placeholder="PIN" autofocus>
<button type="submit">Unlock</button>
</form></div></body></html>""", 401, {"Content-Type": "text/html"})

            @app.route("/auth", methods=["POST"])
            def _auth_pin():
                pin = request.form.get("pin", "")
                if pin == args.pin:
                    resp = app.make_response(
                        __import__("flask").redirect("/"))
                    resp.set_cookie("command_pin", args.pin, max_age=86400 * 30,
                                    httponly=True, samesite="Lax")
                    return resp
                return __import__("flask").redirect("/")

    _start_pruner()
    _start_session_scanner()
    print(f"OpenKeel Command Board -> http://{args.host}:{args.port}")
    if args.remote:
        print(f"Remote access enabled (PIN {'set' if args.pin else 'NOT SET — open access!'})")
    app.run(host=args.host, port=args.port, debug=args.debug)


# ---------------------------------------------------------------------------
# Processes API endpoint
# ---------------------------------------------------------------------------

# Define what to scan per host
_PROCESS_DEFS = {
    "local": [
        {"match": "uvicorn hyphae.server:app", "name": "Hyphae", "desc": "Long-term memory (39k+ facts)", "port": "8100", "url": "http://127.0.0.1:8100"},
        {"match": "openkeel.integrations.kanban_web", "name": "Command Board", "desc": "This dashboard", "port": "8200", "url": "http://127.0.0.1:8200"},
        {"match": "openkeel.gui.app", "name": "OpenKeel GUI", "desc": "Desktop terminal + governance"},
        {"match": "agentblue_console", "name": "AgentBlue Console", "desc": "MSN bridge command center", "port": "8095", "url": "http://127.0.0.1:8095"},
        {"match": "embeddings.*7437", "name": "Embeddings Server", "desc": "Semantic search vectors", "port": "7437"},
        {"match": "amyloidosis_hyphae", "name": "Amyloidosis Corpus", "desc": "Medical research DB", "port": "8101", "url": "http://127.0.0.1:8101"},
        {"match": "job-bot", "name": "Job Bot", "desc": "LinkedIn/Indeed auto-applier"},
    ],
    "jagg": [
        {"match": "continuous_embed", "name": "Embed Pipeline", "desc": "PMC/PubMed embedding (23M papers)"},
        {"match": "chemister_server", "name": "Chemister Server", "desc": "Chemical search API"},
        {"match": "chemister_babysitter", "name": "Chemister Babysitter", "desc": "Embed pipeline watchdog"},
        {"match": "uvicorn hyphae.server:app", "name": "Hyphae (jagg)", "desc": "Memory on jagg", "port": "8100"},
        {"match": "hyphae.mcp_server", "name": "Hyphae MCP", "desc": "MCP bridge for Claude"},
        {"match": "node server.js", "name": "MSN Messenger", "desc": "Retro chat for dad", "port": "3737", "url": "https://msn.automaite.ca"},
    ],
}

def _scan_local_processes():
    """Scan local machine for known processes."""
    import subprocess as sp
    results = []
    try:
        ps_out = sp.run(["ps", "aux"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return results
    for pdef in _PROCESS_DEFS["local"]:
        matching = [l for l in ps_out.split("\n") if pdef["match"] in l and "grep" not in l]
        if matching:
            # Parse uptime from ps
            parts = matching[0].split()
            cpu = parts[2] if len(parts) > 2 else ""
            mem_mb = ""
            try:
                rss_kb = int(parts[5]) if len(parts) > 5 else 0
                mem_mb = f"{rss_kb // 1024}MB"
            except (ValueError, IndexError):
                pass
            started = parts[8] if len(parts) > 8 else ""
            count = len(matching)
            name = pdef["name"] + (f" (x{count})" if count > 1 else "")
            results.append({
                "name": name, "desc": pdef["desc"], "status": "running",
                "port": pdef.get("port"), "url": pdef.get("url"),
                "cpu": cpu + "%", "mem": mem_mb, "uptime": f"since {started}",
            })
    return results

def _scan_jagg_processes():
    """Scan jagg via SSH for known processes."""
    import subprocess as sp
    results = []
    try:
        ps_out = sp.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", "om@192.168.0.224", "ps aux"],
            capture_output=True, text=True, timeout=8
        ).stdout
    except Exception:
        return [{"name": "jagg", "desc": "SSH unreachable", "status": "error"}]
    for pdef in _PROCESS_DEFS["jagg"]:
        matching = [l for l in ps_out.split("\n") if pdef["match"] in l and "grep" not in l]
        if matching:
            parts = matching[0].split()
            cpu = parts[2] if len(parts) > 2 else ""
            mem_mb = ""
            try:
                rss_kb = int(parts[5]) if len(parts) > 5 else 0
                mem_mb = f"{rss_kb // 1024}MB"
            except (ValueError, IndexError):
                pass
            started = parts[8] if len(parts) > 8 else ""
            results.append({
                "name": pdef["name"], "desc": pdef["desc"], "status": "running",
                "port": pdef.get("port"), "url": pdef.get("url"),
                "cpu": cpu + "%", "mem": mem_mb, "uptime": f"since {started}",
            })
    return results

def _scan_do_containers():
    """Scan DigitalOcean docker containers via SSH."""
    import subprocess as sp
    results = []
    try:
        out = sp.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", "root@138.197.145.132",
             "docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'"],
            capture_output=True, text=True, timeout=8
        ).stdout
    except Exception:
        return [{"name": "DigitalOcean", "desc": "SSH unreachable", "status": "error"}]

    # Friendly names
    friendly = {
        "jukebox-caddy-1": ("Caddy (reverse proxy)", "HTTPS termination + routing"),
        "jukebox-remoteblue-admin-1": ("RemoteBlue Admin", "AI website manager for dad"),
        "jukebox-greenloan-bot-1": ("Sage (Green Loans)", "Chatbot on remoteblue.com"),
        "jukebox-paris-bot-1": ("Scout (Paris)", "Chatbot for itsnotthatparis.ca"),
        "jukebox-relay-1": ("Automaite Relay", "WebSocket relay + API"),
        "jukebox-inbloom-bot-1": ("Bloom (InBloom)", "Cannabis dispensary chatbot"),
        "jukebox-gymcoach-1": ("Gym Coach", "Fitness chatbot"),
        "openbank": ("OpenBank", "Banking demo app"),
        "openbank-demo-agent": ("OpenBank Agent", "Demo AI agent"),
        "openbank-postgres": ("OpenBank DB", "PostgreSQL"),
        "openbank-redis": ("OpenBank Cache", "Redis"),
        "claw-demo": ("OpenClaw Demo", "Lead capture demo wizard"),
    }

    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        cname = parts[0] if parts else ""
        status_str = parts[1] if len(parts) > 1 else ""
        ports = parts[2] if len(parts) > 2 else ""
        fname, fdesc = friendly.get(cname, (cname, ""))
        st = "running" if "Up" in status_str else "error"
        # Extract uptime
        uptime = status_str.strip() if status_str else ""
        # Extract port
        port = ""
        if ports:
            import re as _re
            m = _re.search(r"->(\d+)", ports)
            if m:
                port = m.group(1)
        results.append({
            "name": fname, "desc": fdesc, "status": st,
            "port": port, "uptime": uptime,
        })
    return results

_proc_cache = {}
_proc_cache_time = 0

@app.route("/api/processes")
def api_processes():
    global _proc_cache, _proc_cache_time
    # Cache for 10 seconds to avoid hammering SSH
    if time.time() - _proc_cache_time < 10 and _proc_cache:
        return jsonify(_proc_cache)

    data = {
        "local": _scan_local_processes(),
        "jagg": _scan_jagg_processes(),
        "do": _scan_do_containers(),
    }
    _proc_cache = data
    _proc_cache_time = time.time()
    return jsonify(data)


# ---------------------------------------------------------------------------
# Governance API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/governance/approvals")
def api_governance_approvals():
    """List pending governance approvals."""
    if _governor is None:
        return jsonify({"error": "governance not available"}), 503
    pending = _governor.approvals.list_pending()
    return jsonify(pending)


@app.route("/api/governance/approvals/<approval_id>/approve", methods=["POST"])
def api_governance_approve(approval_id: str):
    """Approve a pending governance action."""
    if _governor is None:
        return jsonify({"error": "governance not available"}), 503
    result = _governor.approvals.approve(approval_id)
    if result is None:
        return jsonify({"error": "not found or not pending"}), 404
    return jsonify({"ok": True, "approval": result})


@app.route("/api/governance/approvals/<approval_id>/deny", methods=["POST"])
def api_governance_deny(approval_id: str):
    """Deny a pending governance action."""
    if _governor is None:
        return jsonify({"error": "governance not available"}), 503
    data = request.get_json(force=True) if request.is_json else {}
    reason = data.get("reason", "")
    _governor.approvals.deny(approval_id, reason)
    return jsonify({"ok": True})


@app.route("/api/governance/approvals/recent")
def api_governance_recent():
    """List recently resolved governance approvals (approved/denied/expired)."""
    if _governor is None:
        return jsonify({"error": "governance not available"}), 503
    resolved = []
    approvals_dir = _governor.approvals.APPROVALS_DIR
    for fname in os.listdir(approvals_dir):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(approvals_dir, fname)
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if data.get("status") in ("approved", "denied", "expired"):
                resolved.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    resolved.sort(key=lambda d: d.get("resolved", ""), reverse=True)
    return jsonify(resolved[:50])


@app.route("/api/governance/status")
def api_governance_status():
    """Governance health check."""
    if _governor is None:
        return jsonify({"active": False, "reason": "governance module not loaded"})
    return jsonify({
        "active": True,
        "policy_loaded": bool(_governor.policy.rules),
        "policy_path": _governor.policy.rules_path,
        "components": {
            "policy_gate": True,
            "precheck": True,
            "progress_tracker": True,
            "drift_detector": True,
            "evaluator": True,
            "skill_library": True,
            "saga_manager": True,
            "approval_queue": True,
        },
        "pending_approvals": len(_governor.approvals.list_pending()),
    })


# ---------------------------------------------------------------------------
# Roadmap API
# ---------------------------------------------------------------------------


@app.route("/api/roadmaps")
def api_list_roadmaps():
    """List roadmaps, optionally filtered by project."""
    kb = _get_kb()
    project = request.args.get("project", "")
    return jsonify({"roadmaps": kb.list_roadmaps(project=project)})


@app.route("/api/roadmap", methods=["POST"])
def api_create_roadmap():
    """Create a new roadmap."""
    kb = _get_kb()
    data = request.get_json()
    rid = kb.create_roadmap(
        project=data.get("project", ""),
        title=data["title"],
        description=data.get("description", ""),
    )
    return jsonify({"id": rid, "status": "created"})


@app.route("/api/roadmap/<int:roadmap_id>")
def api_get_roadmap(roadmap_id: int):
    """Get full roadmap view with milestones and task progress."""
    kb = _get_kb()
    return jsonify(kb.roadmap_view(roadmap_id))


@app.route("/api/roadmap/<int:roadmap_id>", methods=["PATCH"])
def api_update_roadmap(roadmap_id: int):
    """Update roadmap fields."""
    kb = _get_kb()
    data = request.get_json()
    kb.update_roadmap(roadmap_id, **data)
    return jsonify({"status": "updated"})


@app.route("/api/roadmap/<int:roadmap_id>", methods=["DELETE"])
def api_delete_roadmap(roadmap_id: int):
    """Delete a roadmap and its milestones."""
    kb = _get_kb()
    kb.delete_roadmap(roadmap_id)
    return jsonify({"status": "deleted"})


@app.route("/api/roadmap/<int:roadmap_id>/milestone", methods=["POST"])
def api_add_milestone(roadmap_id: int):
    """Add a milestone to a roadmap."""
    kb = _get_kb()
    data = request.get_json()
    mid = kb.add_milestone(
        roadmap_id=roadmap_id,
        title=data["title"],
        description=data.get("description", ""),
        target_date=data.get("target_date"),
        sort_order=data.get("sort_order", 0),
    )
    return jsonify({"id": mid, "status": "created"})


@app.route("/api/milestone/<int:milestone_id>", methods=["PATCH"])
def api_update_milestone(milestone_id: int):
    """Update milestone fields (title, description, status, target_date)."""
    kb = _get_kb()
    data = request.get_json()
    kb.update_milestone(milestone_id, **data)
    return jsonify({"status": "updated"})


@app.route("/api/milestone/<int:milestone_id>", methods=["DELETE"])
def api_delete_milestone(milestone_id: int):
    """Delete a milestone."""
    kb = _get_kb()
    kb.delete_milestone(milestone_id)
    return jsonify({"status": "deleted"})


@app.route("/api/milestone/<int:milestone_id>/task/<int:task_id>", methods=["POST"])
def api_link_task(milestone_id: int, task_id: int):
    """Link an existing task to a milestone."""
    kb = _get_kb()
    ok = kb.link_task_to_milestone(milestone_id, task_id)
    return jsonify({"status": "linked" if ok else "failed"})


@app.route("/api/milestone/<int:milestone_id>/task/<int:task_id>", methods=["DELETE"])
def api_unlink_task(milestone_id: int, task_id: int):
    """Remove a task from a milestone."""
    kb = _get_kb()
    kb.unlink_task_from_milestone(milestone_id, task_id)
    return jsonify({"status": "unlinked"})


if __name__ == "__main__":
    main()
