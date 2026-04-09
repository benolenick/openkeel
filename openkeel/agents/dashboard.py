"""Duo Dashboard v2 — real-time web UI for the multi-agent system.

Polls the Command Board API directly (no SSE needed).
Shows: cycle progress, agent status with live timestamps, activity log, critic reviews.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, render_template_string, request

BOARD_URL = "http://127.0.0.1:8200"

duo_bp = Blueprint("duo", __name__)

# SSE event bus (still available for in-process use)
_log_subscribers: list[queue.Queue] = []
_log_lock = threading.Lock()


def broadcast_log(agent: str, message: str, level: str = "info"):
    event = {
        "agent": agent, "message": message, "level": level,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(event)
    with _log_lock:
        dead = []
        for q in _log_subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _log_subscribers.remove(q)


def _api(path: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(f"{BOARD_URL}{path}", timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError):
        return None


@duo_bp.route("/duo/api/agents")
def duo_agents():
    result = _api("/api/agents")
    return jsonify(result if result else [])


@duo_bp.route("/duo/api/commentary")
def duo_commentary():
    limit = request.args.get("limit", "100")
    agent = request.args.get("agent", "")
    qs = f"?limit={limit}"
    if agent:
        qs += f"&agent={agent}"
    result = _api(f"/api/agent/commentary{qs}")
    return jsonify(result if result else [])


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenKeel Duo — Agent Dashboard</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #c9d1d9; --text-dim: #8b949e; --text-bright: #f0f6fc;
  --cyan: #58a6ff; --green: #3fb950; --red: #f85149;
  --yellow: #d29922; --purple: #bc8cff; --orange: #f0883e;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
       background: var(--bg); color: var(--text); font-size: 13px; }

/* Header */
.header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 12px 20px; display: flex; align-items: center; gap: 16px; }
.header h1 { font-size: 16px; color: var(--text-bright); font-weight: 600; }

/* Status banner — the big "what's happening now" */
.status-banner { padding: 16px 20px; border-bottom: 1px solid var(--border);
                 background: linear-gradient(135deg, rgba(88,166,255,0.08), rgba(188,140,255,0.08)); }
.status-row { display: flex; gap: 24px; align-items: center; flex-wrap: wrap; }
.status-block { display: flex; flex-direction: column; gap: 2px; }
.status-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); }
.status-value { font-size: 18px; font-weight: 700; color: var(--text-bright); }
.status-value.running { color: var(--green); }
.status-value.testing { color: var(--yellow); }
.status-value.failed { color: var(--red); }
.status-sub { font-size: 11px; color: var(--text-dim); margin-top: 4px; }

/* Agent cards */
.agent-cards { display: flex; gap: 12px; margin-top: 12px; }
.agent-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
              padding: 10px 14px; flex: 1; min-width: 150px; }
.agent-name { font-weight: 600; font-size: 12px; display: flex; align-items: center; gap: 6px; }
.agent-name .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.busy { background: var(--yellow); animation: pulse 1.5s infinite; }
.dot.idle { background: var(--green); }
.dot.stalled { background: var(--orange); animation: pulse 2s infinite; }
.dot.offline { background: var(--red); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.agent-detail { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
.agent-activity { font-size: 11px; color: var(--text); margin-top: 6px;
                  max-height: 40px; overflow: hidden; }

/* Main grid */
.grid { display: grid; grid-template-columns: 1fr 300px;
        height: calc(100vh - 200px - 180px); gap: 0; }

/* Terminal strip */
.terminal-strip { display: flex; height: 180px; border-top: 2px solid var(--border);
                  background: var(--bg); }
.terminal-panel { flex: 1; border-right: 1px solid var(--border); display: flex;
                  flex-direction: column; overflow: hidden; min-width: 0; }
.terminal-panel:last-child { border-right: none; }
.terminal-header { padding: 4px 8px; font-size: 10px; font-weight: 600;
                   text-transform: uppercase; letter-spacing: 1px;
                   border-bottom: 1px solid var(--border); background: var(--surface);
                   flex-shrink: 0; }
.terminal-header.director { color: var(--cyan); }
.terminal-header.operator { color: var(--yellow); }
.terminal-header.critic { color: var(--red); }
.terminal-header.tester { color: var(--purple); }
.terminal-header.overwatch { color: var(--text-bright); }
.terminal-body { flex: 1; overflow-y: auto; padding: 4px 6px; font-size: 10px;
                 line-height: 1.4; color: var(--text-dim); white-space: pre-wrap;
                 word-break: break-all; font-family: inherit; }

/* Activity log */
.log-panel { overflow-y: auto; border-right: 1px solid var(--border); }
.panel-title { padding: 10px 16px; font-size: 11px; font-weight: 600;
               color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px;
               border-bottom: 1px solid var(--border); background: var(--surface);
               position: sticky; top: 0; z-index: 1; }
.log-entry { padding: 4px 16px; font-size: 12px; line-height: 1.6;
             display: flex; gap: 8px; border-bottom: 1px solid rgba(48,54,61,0.3); }
.log-entry:hover { background: rgba(255,255,255,0.02); }
.log-ts { color: var(--text-dim); min-width: 55px; font-size: 10px; flex-shrink: 0; }
.log-agent { min-width: 60px; font-weight: 600; font-size: 11px; flex-shrink: 0; }
.log-agent.director { color: var(--cyan); }
.log-agent.operator { color: var(--yellow); }
.log-agent.critic { color: var(--red); }
.log-agent.tester { color: var(--purple); }
.log-agent.overwatch { color: var(--text-bright); }
.log-msg { flex: 1; word-break: break-word; }
.log-msg.flaw { color: var(--red); }
.log-msg.approve { color: var(--green); }
.log-msg.cycle { color: var(--purple); font-weight: 600; }
.log-msg.test { color: var(--orange); }

/* Right panel — cycle history */
.cycle-panel { overflow-y: auto; }
.cycle-card { padding: 12px 16px; border-bottom: 1px solid var(--border); }
.cycle-header { display: flex; justify-content: space-between; align-items: center; }
.cycle-num { font-weight: 700; color: var(--purple); }
.cycle-result { font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
.cycle-result.pass { background: rgba(63,185,80,0.15); color: var(--green); }
.cycle-result.fail { background: rgba(248,81,73,0.15); color: var(--red); }
.cycle-focus { font-size: 11px; color: var(--text); margin-top: 6px; }
.cycle-changes { font-size: 11px; color: var(--text-dim); margin-top: 4px; }

/* Goal */
.goal-text { font-size: 12px; color: var(--text); margin-top: 8px;
             max-height: 36px; overflow: hidden; }

/* Mobile */
@media (max-width: 768px) {
  .grid { grid-template-columns: 1fr; }
  .agent-cards { flex-wrap: wrap; }
  .status-row { flex-direction: column; gap: 8px; }
}
</style>
</head>
<body>

<div class="header">
  <h1>⚡ OpenKeel Duo</h1>
  <span style="margin-left:auto;font-size:11px;color:var(--text-dim)" id="clockText"></span>
</div>

<div class="status-banner">
  <div class="status-row">
    <div class="status-block">
      <div class="status-label">Cycle</div>
      <div class="status-value" id="cycleNum">—</div>
    </div>
    <div class="status-block">
      <div class="status-label">Status</div>
      <div class="status-value running" id="currentStatus">Starting...</div>
    </div>
    <div class="status-block">
      <div class="status-label">Steps Done</div>
      <div class="status-value" id="stepsDone">0</div>
    </div>
    <div class="status-block">
      <div class="status-label">Tests Passed</div>
      <div class="status-value" id="testsPassed">0/0</div>
    </div>
    <div class="status-block">
      <div class="status-label">Elapsed</div>
      <div class="status-value" id="elapsed">0m</div>
    </div>
  </div>
  <div class="goal-text" id="goalText"></div>
  <div class="agent-cards" id="agentCards"></div>
</div>

<div class="grid">
  <div class="log-panel">
    <div class="panel-title">
      Activity Log
      <span style="float:right;font-weight:400">
        <select id="filterAgent" style="background:var(--bg);color:var(--text);border:1px solid var(--border);font-size:10px;padding:2px;">
          <option value="">all</option>
          <option value="director">director</option>
          <option value="operator">operator</option>
          <option value="critic">critic</option>
          <option value="tester">tester</option>
          <option value="overwatch">overwatch</option>
        </select>
      </span>
    </div>
    <div id="logEntries"></div>
  </div>

  <div class="cycle-panel">
    <div class="panel-title">Cycle History</div>
    <div id="cycleHistory"></div>
  </div>
</div>

<div class="terminal-strip">
  <div class="terminal-panel">
    <div class="terminal-header director">Director</div>
    <div class="terminal-body" id="term-director"></div>
  </div>
  <div class="terminal-panel">
    <div class="terminal-header operator">Operator</div>
    <div class="terminal-body" id="term-operator"></div>
  </div>
  <div class="terminal-panel">
    <div class="terminal-header critic">Critic</div>
    <div class="terminal-body" id="term-critic"></div>
  </div>
  <div class="terminal-panel">
    <div class="terminal-header tester">Tester</div>
    <div class="terminal-body" id="term-tester"></div>
  </div>
  <div class="terminal-panel">
    <div class="terminal-header overwatch">Overwatch</div>
    <div class="terminal-body" id="term-overwatch"></div>
  </div>
</div>

<script>
const state = {
  logs: [],
  seenKeys: new Set(),
  startTime: Date.now(),
  cycle: 0,
  stepsDone: 0,
  testsPassed: 0,
  testsTotal: 0,
  cycles: [],
  agents: {},
};

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function fetchCommentary() {
  try {
    const res = await fetch('/duo/api/commentary?limit=100');
    const items = await res.json();
    if (!Array.isArray(items)) return;

    // Process in chronological order
    const sorted = items.reverse();
    let newEntries = false;
    for (const c of sorted) {
      const key = (c.timestamp||'') + (c.agent||'') + (c.text||c.message||'').substring(0,50);
      if (state.seenKeys.has(key)) continue;
      state.seenKeys.add(key);
      newEntries = true;

      let ts = c.timestamp;
      // Handle both float (unix epoch) and string timestamps
      if (typeof ts === 'number') ts = new Date(ts * 1000).toISOString();
      else if (!ts) ts = new Date().toISOString();

      const entry = {
        agent: c.agent || 'system',
        message: c.text || c.message || '',
        timestamp: ts,
      };
      state.logs.push(entry);
      if (state.logs.length > 500) state.logs.shift();

      // Parse state from messages
      parseMessage(entry);
    }
    if (newEntries) renderLog();
  } catch {}
}

function parseMessage(entry) {
  const msg = entry.message;

  // Cycle detection
  const cycleMatch = msg.match(/=== CYCLE (\\d+)/);
  if (cycleMatch) {
    state.cycle = parseInt(cycleMatch[1]);
  }

  // Step completion
  if (msg.includes('step') && msg.includes('complete')) state.stepsDone++;

  // Test results
  if (msg.includes('Test ')) {
    state.testsTotal++;
    if (msg.includes('PASSED')) state.testsPassed++;
  }

  // Cycle done
  const cycleDone = msg.match(/Cycle (\\d+) done.*Test (PASSED|FAILED).*Next: (.+)/);
  if (cycleDone) {
    state.cycles.push({
      num: parseInt(cycleDone[1]),
      passed: cycleDone[2] === 'PASSED',
      focus: cycleDone[3],
    });
    renderCycles();
  }

  // Goal
  if (msg.includes('Planning steps for:') || msg.includes('OVERALL GOAL:')) {
    const goalText = msg.replace(/Planning steps for:|OVERALL GOAL:/, '').trim();
    if (goalText.length > 20) document.getElementById('goalText').textContent = goalText.substring(0, 200);
  }
}

// Track if user has manually scrolled up
let userScrolledUp = false;
let lastLogCount = 0;

(function() {
  const el = document.getElementById('logEntries');
  if (el) {
    el.addEventListener('scroll', () => {
      const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
      userScrolledUp = !atBottom;
    });
  }
})();

function renderLog() {
  const el = document.getElementById('logEntries');
  const filter = document.getElementById('filterAgent').value;
  const filtered = filter ? state.logs.filter(l => l.agent === filter) : state.logs;
  const recent = filtered.slice(-200);

  // Skip render if nothing changed
  if (recent.length === lastLogCount) return;
  lastLogCount = recent.length;

  let html = '';
  for (const entry of recent) {
    let ts = '';
    try { ts = new Date(entry.timestamp).toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit'}); } catch {}

    let msgClass = '';
    if (entry.message.includes('FLAW') || entry.message.includes('REJECT')) msgClass = 'flaw';
    else if (entry.message.includes('APPROVE')) msgClass = 'approve';
    else if (entry.message.includes('CYCLE')) msgClass = 'cycle';
    else if (entry.message.includes('Test ') || entry.message.includes('test') || entry.message.includes('Tester')) msgClass = 'test';

    html += '<div class="log-entry">' +
      '<span class="log-ts">' + ts + '</span>' +
      '<span class="log-agent ' + entry.agent + '">' + entry.agent + '</span>' +
      '<span class="log-msg ' + msgClass + '">' + esc(entry.message.substring(0,300)) + '</span>' +
      '</div>';
  }
  el.innerHTML = html;

  // Auto-scroll unless user manually scrolled up
  if (!userScrolledUp) el.scrollTop = el.scrollHeight;
}

async function fetchAgents() {
  try {
    const res = await fetch('/duo/api/agents');
    const agents = await res.json();
    if (!Array.isArray(agents)) return;
    state.agents = {};
    for (const a of agents) state.agents[a.name] = a;
    renderAgents();
  } catch {}
}

function renderAgents() {
  const el = document.getElementById('agentCards');
  const trio = ['director', 'operator', 'critic', 'tester', 'overwatch'];
  let html = '';
  for (const name of trio) {
    const a = state.agents[name];
    if (!a) {
      html += '<div class="agent-card"><div class="agent-name"><span class="dot offline"></span>' + name + '</div><div class="agent-detail">not registered</div></div>';
      continue;
    }
    let status = a.effective_status || a.status || 'offline';
    // On-demand agents (tester, critic) aren't stalled — they're waiting to be called
    const onDemandAgents = ['tester', 'critic'];
    if (status === 'stalled' && onDemandAgents.includes(name)) status = 'standby';
    const dotClass = status === 'busy' ? 'busy' : (status === 'idle' || status === 'standby') ? 'idle' : status === 'stalled' ? 'stalled' : 'offline';
    const age = a.last_heartbeat ? Math.floor(Date.now()/1000 - a.last_heartbeat) : 999;
    const ageStr = age < 60 ? age + 's ago' : Math.floor(age/60) + 'm ago';

    // Get last commentary for this agent
    const lastMsg = [...state.logs].reverse().find(l => l.agent === name);
    const lastActivity = lastMsg ? lastMsg.message.substring(0, 80) : '';

    html += '<div class="agent-card">' +
      '<div class="agent-name"><span class="dot ' + dotClass + '"></span>' + name + ' <span style="font-weight:400;color:var(--text-dim);font-size:10px">' + status + ' · ' + ageStr + '</span></div>' +
      (lastActivity ? '<div class="agent-activity">' + esc(lastActivity) + '</div>' : '') +
      '</div>';
  }
  el.innerHTML = html;
}

function renderCycles() {
  const el = document.getElementById('cycleHistory');
  if (state.cycles.length === 0) {
    el.innerHTML = '<div style="padding:16px;color:var(--text-dim)">No completed cycles yet...</div>';
    return;
  }
  let html = '';
  for (const c of [...state.cycles].reverse()) {
    html += '<div class="cycle-card">' +
      '<div class="cycle-header"><span class="cycle-num">Cycle ' + c.num + '</span>' +
      '<span class="cycle-result ' + (c.passed ? 'pass' : 'fail') + '">' + (c.passed ? 'PASS' : 'FAIL') + '</span></div>' +
      '<div class="cycle-focus">Next: ' + esc(c.focus.substring(0, 120)) + '</div></div>';
  }
  el.innerHTML = html;
}

function updateStatus() {
  document.getElementById('cycleNum').textContent = state.cycle || '—';
  document.getElementById('stepsDone').textContent = state.stepsDone;
  document.getElementById('testsPassed').textContent = state.testsPassed + '/' + state.testsTotal;
  const elapsed = Math.floor((Date.now() - state.startTime) / 60000);
  document.getElementById('elapsed').textContent = elapsed + 'm';
  document.getElementById('clockText').textContent = new Date().toLocaleTimeString('en-US', {hour12:false});

  // Determine current status from latest log
  const last = state.logs[state.logs.length - 1];
  const statusEl = document.getElementById('currentStatus');
  if (!last) return;
  const msg = last.message;
  if (msg.includes('CYCLE') && msg.includes('Planning')) {
    statusEl.textContent = 'Planning';
    statusEl.className = 'status-value running';
  } else if (msg.includes('Executing') || msg.includes('Starting work')) {
    statusEl.textContent = 'Executing';
    statusEl.className = 'status-value running';
  } else if (msg.includes('Running test') || msg.includes('test harness')) {
    statusEl.textContent = 'Testing';
    statusEl.className = 'status-value testing';
  } else if (msg.includes('FAILED')) {
    statusEl.textContent = 'Test Failed';
    statusEl.className = 'status-value failed';
  } else if (msg.includes('Reviewing')) {
    statusEl.textContent = 'Reviewing';
    statusEl.className = 'status-value running';
  } else if (msg.includes('Analyzing')) {
    statusEl.textContent = 'Analyzing';
    statusEl.className = 'status-value running';
  }
}

async function fetchTerminals() {
  const agents = ['director', 'operator', 'critic', 'tester', 'overwatch'];
  for (const agent of agents) {
    try {
      const res = await fetch('/duo/api/terminal/' + agent + '?lines=20');
      const data = await res.json();
      const el = document.getElementById('term-' + agent);
      if (el && data.lines) {
        const wasAtBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 30;
        el.textContent = data.lines.join('\n');
        if (wasAtBottom || el.scrollTop === 0) el.scrollTop = el.scrollHeight;
      }
    } catch {}
  }
}

function tick() {
  fetchAgents();
  fetchCommentary();
  fetchTerminals();
  updateStatus();
}

tick();
// Force scroll to bottom on initial load after first data arrives
setTimeout(() => {
  const el = document.getElementById('logEntries');
  if (el) el.scrollTop = el.scrollHeight;
}, 2000);
setInterval(tick, 3000);

document.getElementById('filterAgent').addEventListener('change', renderLog);
</script>
</body>
</html>
"""


@duo_bp.route("/duo/api/terminal/<agent>")
def duo_terminal(agent):
    """Return last N lines of an agent's terminal log."""
    import os
    path = f"/tmp/agent_terminals/{agent}.log"
    lines = int(request.args.get("lines", "30"))
    try:
        with open(path) as f:
            all_lines = f.readlines()
        return jsonify({"agent": agent, "lines": [l.rstrip() for l in all_lines[-lines:]]})
    except FileNotFoundError:
        return jsonify({"agent": agent, "lines": ["(no output yet)"]})


@duo_bp.route("/duo")
def duo_dashboard():
    return render_template_string(DASHBOARD_HTML)


def mount_duo_dashboard(app):
    app.register_blueprint(duo_bp)


def main():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(duo_bp)
    print("OpenKeel Duo Dashboard → http://127.0.0.1:8201/duo")
    app.run(host="127.0.0.1", port=8201, debug=False, threaded=True)


if __name__ == "__main__":
    main()
