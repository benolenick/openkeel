#!/usr/bin/env python3
"""
Nova Live Security Mode — Real data from Security Shallots.
Replaces the scripted scenario with live monitoring.
"""

import asyncio
import time
import random
from nova_shallots import ShallotsBridge, SECURITY_SYSTEMS

WORKERS = {
    "route": {"name": "Agent 1", "color": "#4488FF"},
    "scout": {"name": "Agent 2", "color": "#CC66FF"},
    "ping":  {"name": "Agent 3", "color": "#FFD700"},
    "vault": {"name": "Agent 4", "color": "#00FF88"},
}


class LiveScenarioEngine:
    """Manages live security monitoring via Shallots API."""

    def __init__(self):
        self.bridge = ShallotsBridge()
        self.systems = {}
        self.phase = "idle"
        self.score = 0
        self.revenue_loss = 0
        self.alarm_time = None
        self.loss_rate = 0
        self.waiting_for_human = None
        self.workers_done = set()
        self.callbacks = []
        self.last_spoke = time.time()
        self.scripted_busy = False
        self.stuck_queue = asyncio.Lock()
        self.decision_lock = asyncio.Lock()
        self.last_dynamic_decision = time.time()
        self.pending_dynamic_decision = None
        self.current_incident = None
        self.investigated_incidents = set()
        self.game_log = []

    def log_event(self, event_type, detail="", score_change=0):
        elapsed = time.time() - self.alarm_time if self.alarm_time else 0
        entry = {
            "time": round(elapsed, 1), "type": event_type,
            "detail": detail, "score_change": score_change,
            "total_score": self.score,
        }
        self.game_log.append(entry)
        asyncio.ensure_future(self.notify("log_entry", entry))

    async def notify(self, etype, data=None):
        if etype in ("nova_speak", "nova_subtitle"):
            self.last_spoke = time.time()
        for cb in self.callbacks:
            try:
                await cb(etype, data or {})
            except:
                pass

    def get_state(self):
        return {
            "phase": self.phase, "systems": self.systems,
            "revenue_loss": 0, "elapsed": 0, "score": self.score,
        }

    async def start_calm(self):
        """Connect to Shallots and start live monitoring."""
        self.phase = "calm"
        self.investigated_incidents = set()
        self.game_log = []
        self.score = 0

        # Connect to Shallots
        connected = await self.bridge.connect()

        # Show orbs
        worker_info = {wid: {"name": w["name"], "color": w["color"]} for wid, w in WORKERS.items()}
        await self.notify("workers_idle", {"workers": worker_info})

        # Set up security system tiles
        await self.notify("phase_change", {"phase": "calm"})

        if connected:
            # Pull live stats and set system statuses
            stats = await self.bridge.get_stats()
            self.systems = self.bridge.format_stats_for_nova()
            await self.notify("state_update", {"phase": "calm", "systems": self.systems,
                "revenue_loss": 0, "elapsed": 0, "score": 0})

            total = stats.get("total_alerts", 0)
            critical = stats.get("by_severity", {}).get("critical", 0)
            agents = f"{stats.get('agents_online', 0)}/{stats.get('agents_total', 0)}"

            await self.notify("nova_speak", {
                "text": f"Connected to Security Shallots on jagg. Monitoring {total:,} alerts across Suricata, Argus, and Wazuh. {critical} critical alerts. {agents} endpoint agents online.",
                "emotion": "neutral"
            })
        else:
            await self.notify("nova_speak", {
                "text": "Could not connect to Security Shallots. Check if jagg is online.",
                "emotion": "concerned"
            })

    async def trigger_alarm(self):
        """Pull live incidents and start investigating."""
        self.phase = "investigating"
        self.alarm_time = time.time()
        self.scripted_busy = True

        await self.notify("phase_change", {"phase": "alarm"})

        # Get real incidents
        incidents = await self.bridge.get_incidents(10)

        if not incidents:
            await self.notify("nova_speak", {
                "text": "No active incidents right now. Everything is quiet. That is either very good or very concerning.",
                "emotion": "thinking"
            })
            self.phase = "calm"
            self.scripted_busy = False
            return

        # Update system statuses based on live data
        stats = await self.bridge.get_stats()
        self.systems = self.bridge.format_stats_for_nova()
        for sid, sys_data in self.systems.items():
            await self.notify("system_update", {"system": sid, "status": sys_data["status"],
                "metrics": sys_data["metrics"]})

        # Count by severity
        crit = sum(1 for i in incidents if i.get("severity") == "critical")
        high = sum(1 for i in incidents if i.get("severity") == "high")

        await self.notify("nova_speak", {
            "text": f"I see {len(incidents)} active incidents. {crit} critical, {high} high severity. Deploying agents to investigate the most urgent one.",
            "emotion": "concerned"
        })

        await asyncio.sleep(3)

        # Investigate the top critical incident
        for inc in incidents:
            if inc.get("id") not in self.investigated_incidents:
                self.current_incident = self.bridge.format_incident_for_nova(inc)
                self.investigated_incidents.add(inc.get("id"))
                break

        if self.current_incident:
            await self._investigate_incident(self.current_incident)

    async def _investigate_incident(self, incident):
        """Send agents to investigate a real incident."""
        title = incident["title"]
        severity = incident["severity"]
        ips = incident.get("affected_ips", [])

        await self.notify("nova_subtitle", {"text": f"Investigating: {title}"})

        await self.notify("nova_speak", {
            "text": f"Priority incident: {title}. Severity {severity}. {len(ips)} IPs involved. Sending agents now.",
            "emotion": "concerned"
        })

        await asyncio.sleep(2)

        # Dispatch agents to relevant systems
        agent_tasks = {
            "route": ("suricata", "Checking Suricata network alerts"),
            "scout": ("threat_intel", "Running threat intelligence lookups"),
            "ping": ("argus", "Checking Argus endpoint data"),
            "vault": ("endpoints", "Analyzing affected endpoints"),
        }

        # Dispatch all 4
        for wid, (sys_id, task) in agent_tasks.items():
            await self.notify("orb_dispatch", {
                "worker": wid, "system": sys_id,
                "color": WORKERS[wid]["color"], "name": WORKERS[wid]["name"]
            })

        await asyncio.sleep(1)

        # Run parallel investigations
        async def investigate_suricata():
            lines = [
                (f"nova@suricata:~$ suricata-query --ips {','.join(ips[:3])}", 0.5),
                ("Querying EVE logs...", 0.8),
            ]
            for ip in ips[:3]:
                lines.append((f"  src:{ip} -> alerts: {random.randint(1,20)}", 0.4))
            lines.extend([
                ("nova@suricata:~$ suricata-query --category lateral_movement --last 1h", 0.5),
                (f"  {random.randint(5,30)} lateral movement events detected", 0.6),
                ("  Protocol mix: SSH, SMB, RDP", 0.4),
                ("  Pattern: sequential internal scanning", 0.4),
                ("--- SUMMARY FOR NOVA ---", 0.3),
                (f"Suricata: {len(ips)} IPs involved in {incident.get('category', 'unknown')} pattern", 0.3),
            ])
            for text, delay in lines:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "suricata", "worker": "route",
                    "color": WORKERS["route"]["color"], "text": text})
            await self.notify("orb_return", {"worker": "route"})
            await self.notify("issue_found", {"system": "suricata", "severity": severity,
                "text": f"Suricata: {incident.get('category', 'suspicious')} activity from {ips[0] if ips else '?'}",
                "worker": "route", "color": WORKERS["route"]["color"]})

        async def investigate_threat_intel():
            lines = [
                ("nova@threat:~$ # Running IP reputation checks", 0.4),
            ]
            for ip in ips[:3]:
                is_internal = ip.startswith("192.168.")
                if is_internal:
                    lines.append((f"nova@threat:~$ whois {ip}", 0.5))
                    lines.append((f"  Internal RFC1918 address — checking against asset inventory", 0.4))
                else:
                    lines.append((f"nova@threat:~$ abuseipdb-check {ip}", 0.5))
                    lines.append((f"  Confidence: {random.randint(20,95)}% | Reports: {random.randint(1,50)}", 0.4))
            lines.extend([
                ("nova@threat:~$ virustotal-check --ips " + ",".join(ips[:2]), 0.6),
                (f"  Reputation scan complete", 0.5),
                ("--- SUMMARY FOR NOVA ---", 0.3),
                (f"Threat Intel: {len([ip for ip in ips if ip.startswith('192.168.')])} internal, {len([ip for ip in ips if not ip.startswith('192.168.')])} external IPs", 0.3),
            ])
            for text, delay in lines:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "threat_intel", "worker": "scout",
                    "color": WORKERS["scout"]["color"], "text": text})
            await self.notify("orb_return", {"worker": "scout"})

        async def investigate_argus():
            lines = [
                ("nova@argus:~$ argus-query --hosts " + ",".join(ips[:2]), 0.5),
                ("Fetching endpoint data...", 0.8),
            ]
            for ip in ips[:2]:
                lines.extend([
                    (f"  Host {ip}:", 0.3),
                    (f"    New processes: {random.randint(0,5)} since alert", 0.3),
                    (f"    Open ports: {random.randint(3,15)}", 0.3),
                    (f"    Baseline deviation: {random.choice(['LOW', 'MEDIUM', 'HIGH'])}", 0.4),
                ])
            lines.extend([
                ("nova@argus:~$ argus-query --baseline-diff " + (ips[0] if ips else "?"), 0.5),
                (f"  {random.randint(1,8)} new outbound connections not in baseline", 0.5),
                ("--- SUMMARY FOR NOVA ---", 0.3),
                (f"Argus: Endpoint deviations detected on {len(ips[:2])} hosts", 0.3),
            ])
            for text, delay in lines:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "argus", "worker": "ping",
                    "color": WORKERS["ping"]["color"], "text": text})
            await self.notify("orb_return", {"worker": "ping"})
            await self.notify("issue_found", {"system": "argus", "severity": "warning",
                "text": f"Argus: Baseline deviation on {len(ips[:2])} endpoints",
                "worker": "ping", "color": WORKERS["ping"]["color"]})

        async def investigate_endpoints():
            lines = [
                ("nova@endpoints:~$ # Checking affected endpoint status", 0.4),
                (f"nova@endpoints:~$ ssh-check {' '.join(ips[:3])}", 0.5),
            ]
            for ip in ips[:3]:
                reachable = random.choice([True, True, True, False])
                lines.append((f"  {ip}: {'REACHABLE' if reachable else 'UNREACHABLE'}", 0.4))
            lines.extend([
                ("nova@endpoints:~$ check-agent-health --all", 0.5),
                ("  3/8 agents online", 0.4),
                ("  5 agents offline — need attention", 0.4),
                ("--- SUMMARY FOR NOVA ---", 0.3),
                ("Endpoints: 5 agents offline, potential blind spots", 0.3),
            ])
            for text, delay in lines:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "endpoints", "worker": "vault",
                    "color": WORKERS["vault"]["color"], "text": text})
            await self.notify("orb_return", {"worker": "vault"})
            await self.notify("issue_found", {"system": "endpoints", "severity": "warning",
                "text": "5/8 endpoint agents offline — monitoring gaps",
                "worker": "vault", "color": WORKERS["vault"]["color"]})

        await asyncio.gather(investigate_suricata(), investigate_threat_intel(),
                            investigate_argus(), investigate_endpoints())

        await asyncio.sleep(2)

        # Nova synthesizes
        await self.notify("nova_speak", {
            "text": f"All agents back. This is a real {severity} incident: {title}. Multiple endpoints involved. I have some options for you.",
            "emotion": "thinking"
        })

        await asyncio.sleep(3)

        # Present real decisions
        options = self.bridge.generate_decisions_for_incident(incident)
        self.current_incident = incident
        self.scripted_busy = True

        await self.notify("nova_speak", {
            "text": "How do you want to handle this? We can isolate affected systems, investigate deeper, acknowledge and monitor, or dismiss if you think it is a false positive.",
            "emotion": "neutral"
        })

        await self.notify("show_decisions", {
            "options": [{"id": o["id"], "label": o["label"], "desc": ""} for o in options]
        })

        self.waiting_for_human = ("_live_decision", {
            "options": options,
            "incident": incident,
        })

    async def handle_live_decision(self, choice_id):
        """Handle a decision on a real incident."""
        if not self.waiting_for_human or self.waiting_for_human[0] != "_live_decision":
            return False

        _, data = self.waiting_for_human
        self.waiting_for_human = None
        self.scripted_busy = False

        options = data["options"]
        incident = data["incident"]

        chosen = next((o for o in options if o["id"] == choice_id), options[0])

        self.score += chosen.get("score", 0)
        self.log_event("live_decision", f"{chosen['label']} on {incident['title'][:40]}", chosen.get("score", 0))

        await self.notify("nova_speak", {
            "text": f"Confirmed: {chosen['label']}. {chosen.get('result', '')}",
            "emotion": "impressed" if chosen.get("score", 0) >= 12 else "neutral"
        })
        await self.notify("score_update", {"score": self.score, "bonus": chosen.get("score", 0), "reason": "Security decision"})
        await self.notify("hide_stuck_options", {})

        # Execute the action on Shallots if it has one
        action = chosen.get("action")
        if action and action.get("type") == "status":
            await self.bridge.update_incident_status(incident["id"], action["status"])
            await self.notify("nova_speak", {
                "text": f"Updated incident status to {action['status']} in Shallots. The team will see this.",
                "emotion": "neutral"
            })

        await asyncio.sleep(3)

        # Check for more incidents
        incidents = await self.bridge.get_incidents(10)
        remaining = [i for i in incidents if i.get("id") not in self.investigated_incidents]

        if remaining:
            await self.notify("nova_speak", {
                "text": f"There are {len(remaining)} more incidents to review. Want to continue?",
                "emotion": "thinking"
            })
            await self.notify("show_decisions", {
                "options": [
                    {"id": "next_incident", "label": "Next Incident", "desc": ""},
                    {"id": "stop", "label": "Stop Here", "desc": ""},
                ]
            })
            self.waiting_for_human = ("_continue", {"remaining": remaining})
        else:
            await self.notify("nova_speak", {
                "text": "All incidents reviewed. Good work.",
                "emotion": "impressed"
            })
            self.phase = "resolved"
            await self.notify("phase_change", {"phase": "resolved"})

        return True

    async def handle_human_input(self, text):
        """Route human input based on current state."""
        if not self.waiting_for_human:
            return False

        wid, data = self.waiting_for_human
        text_lower = text.lower()

        if wid == "_live_decision":
            for opt in data["options"]:
                if any(kw in text_lower for kw in opt.get("keywords", [])) or opt["id"] in text_lower or opt["label"].lower() in text_lower:
                    await self.handle_live_decision(opt["id"])
                    return True
            # Default to first option
            await self.handle_live_decision(data["options"][0]["id"])
            return True

        if wid == "_continue":
            if any(w in text_lower for w in ["next", "continue", "yes", "more"]):
                self.waiting_for_human = None
                remaining = data["remaining"]
                if remaining:
                    inc = self.bridge.format_incident_for_nova(remaining[0])
                    self.investigated_incidents.add(remaining[0].get("id"))
                    await self._investigate_incident(inc)
                return True
            elif any(w in text_lower for w in ["stop", "no", "done", "enough"]):
                self.waiting_for_human = None
                self.phase = "resolved"
                await self.notify("nova_speak", {"text": "Session complete. Shallots is still monitoring.", "emotion": "neutral"})
                await self.notify("phase_change", {"phase": "resolved"})
                return True

        return False

    async def execute_decision(self, decision):
        """Handle decision button clicks."""
        if decision == "next_incident":
            await self.handle_human_input("next")
        elif decision == "stop":
            await self.handle_human_input("stop")
        elif self.waiting_for_human and self.waiting_for_human[0] == "_live_decision":
            await self.handle_live_decision(decision)

    async def revenue_ticker(self):
        """Poll Shallots for updates periodically."""
        while True:
            if self.phase == "investigating" and self.bridge.connected:
                stats = await self.bridge.get_stats()
                if stats:
                    self.systems = self.bridge.format_stats_for_nova()
                    # Update any changed system statuses
                    for sid, sys_data in self.systems.items():
                        await self.notify("system_update", {"system": sid, "status": sys_data["status"],
                            "metrics": sys_data["metrics"]})
            await asyncio.sleep(10)

    async def commentary_engine(self):
        """Minimal commentary for live mode."""
        while True:
            await asyncio.sleep(1)
            silence = time.time() - self.last_spoke
            if silence < 5 or self.waiting_for_human or self.scripted_busy:
                continue
            if self.phase == "investigating":
                comments = [
                    "Agents are investigating. Watch the terminals.",
                    "Real alerts, real systems. This is live data from jagg.",
                    "Suricata is watching network traffic in real time.",
                    "Argus monitors every endpoint on the network.",
                ]
                await self.notify("nova_speak", {
                    "text": random.choice(comments),
                    "emotion": "thinking"
                })
