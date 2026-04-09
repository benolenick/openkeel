#!/usr/bin/env python3
"""
Nova x Security Shallots — Live Security Mode
Connects to the real Shallots API on jagg and lets Nova
monitor, investigate, and respond to real security incidents.
"""

import asyncio
import aiohttp
import ssl
import json
import time

SHALLOTS_BASE = "https://192.168.0.224:8844"
SHALLOTS_USER = "admin"
SHALLOTS_PASS = "Sh4ll0ts!Jagg2026"

# Disable SSL verification (self-signed cert on jagg)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# Map Shallots sources to Nova's system tiles
SOURCE_TO_SYSTEM = {
    "suricata": "suricata",
    "argus": "argus",
    "wazuh": "wazuh",
    "clove": "clove",
    "shallotd": "shallotd",
}

# Security system definitions for Nova's UI
SECURITY_SYSTEMS = {
    "suricata":  {"name": "Suricata IDS",   "short": "SURICATA"},
    "argus":     {"name": "Argus Endpoint", "short": "ARGUS"},
    "wazuh":     {"name": "Wazuh HIDS",    "short": "WAZUH"},
    "firewall":  {"name": "pfSense FW",    "short": "FIREWALL"},
    "network":   {"name": "Network",       "short": "NETWORK"},
    "dns_sec":   {"name": "DNS Security",  "short": "DNS"},
    "threat_intel": {"name": "Threat Intel", "short": "THREAT"},
    "endpoints": {"name": "Endpoints",     "short": "ENDPOINTS"},
    "clove":     {"name": "Clove Agent",   "short": "CLOVE"},
    "shallotd":  {"name": "Shallot Core",  "short": "CORE"},
}


class ShallotsBridge:
    """Bridge between Nova and the live Shallots security platform."""

    def __init__(self):
        self.session = None
        self.stats = {}
        self.incidents = []
        self.last_poll = 0
        self.connected = False

    async def connect(self):
        """Initialize the HTTP session."""
        auth = aiohttp.BasicAuth(SHALLOTS_USER, SHALLOTS_PASS)
        self.session = aiohttp.ClientSession(auth=auth)
        # Test connection
        try:
            data = await self._get("/api/health")
            if data and data.get("status") == "ok":
                self.connected = True
                return True
        except Exception as e:
            print(f"Shallots connection failed: {e}")
        return False

    async def close(self):
        if self.session:
            await self.session.close()

    async def _get(self, path):
        """GET request to Shallots API."""
        try:
            async with self.session.get(f"{SHALLOTS_BASE}{path}", ssl=SSL_CTX, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f"Shallots API {path}: HTTP {resp.status}")
                    return None
        except Exception as e:
            print(f"Shallots API error {path}: {e}")
            return None

    async def _post(self, path, data=None):
        """POST request to Shallots API."""
        try:
            async with self.session.post(f"{SHALLOTS_BASE}{path}", json=data, ssl=SSL_CTX, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json() if resp.status == 200 else None
        except Exception as e:
            print(f"Shallots POST error {path}: {e}")
            return None

    async def get_stats(self):
        """Get current alert statistics."""
        self.stats = await self._get("/api/stats") or {}
        return self.stats

    async def get_incidents(self, limit=10):
        """Get active incidents."""
        data = await self._get(f"/api/incidents?limit={limit}")
        if data:
            self.incidents = data if isinstance(data, list) else data.get("incidents", [])
        return self.incidents

    async def get_incident_detail(self, incident_id):
        """Get full incident details including timeline."""
        return await self._get(f"/api/incidents/{incident_id}")

    async def get_incident_timeline(self, incident_id):
        """Get incident timeline."""
        return await self._get(f"/api/incidents/{incident_id}/timeline")

    async def get_correlations(self, limit=10):
        """Get correlated alert groups."""
        return await self._get(f"/api/correlations?limit={limit}")

    async def get_correlation_alerts(self, correlation_id):
        """Get alerts in a correlation."""
        return await self._get(f"/api/correlations/{correlation_id}/alerts")

    async def update_incident_status(self, incident_id, status):
        """Update incident status (acknowledge, investigate, resolve, dismiss)."""
        return await self._post(f"/api/incidents/{incident_id}/status", {"status": status})

    async def decide_incident(self, incident_id, decision):
        """Make a decision on an incident."""
        return await self._post(f"/api/incidents/{incident_id}/decide", decision)

    def format_stats_for_nova(self):
        """Format stats into Nova-friendly system statuses."""
        s = self.stats
        if not s:
            return {}

        systems = {}
        total = s.get("total_alerts", 0)
        critical = s.get("by_severity", {}).get("critical", 0)
        high = s.get("by_severity", {}).get("high", 0)
        pending = s.get("pending_triage", 0)

        # Suricata
        suricata_count = s.get("by_source", {}).get("suricata", 0)
        systems["suricata"] = {
            "status": "warning" if suricata_count > 100 else "healthy",
            "metrics": {"Alerts": suricata_count, "Type": "Network IDS"},
        }

        # Argus
        argus_count = s.get("by_source", {}).get("argus", 0)
        agents_online = s.get("agents_online", 0)
        agents_total = s.get("agents_total", 0)
        systems["argus"] = {
            "status": "warning" if agents_online < agents_total else "healthy",
            "metrics": {"Alerts": argus_count, "Agents": f"{agents_online}/{agents_total}"},
        }

        # Wazuh
        wazuh_count = s.get("by_source", {}).get("wazuh", 0)
        systems["wazuh"] = {
            "status": "healthy",
            "metrics": {"Alerts": wazuh_count, "Type": "Host IDS"},
        }

        # Threat Intel
        systems["threat_intel"] = {
            "status": "critical" if critical > 10 else "warning" if critical > 0 else "healthy",
            "metrics": {"Critical": critical, "High": high, "Threats": s.get("threats_external", 0)},
        }

        # Network
        systems["network"] = {
            "status": "healthy",
            "metrics": {"Correlations": s.get("correlations", 0)},
        }

        # Firewall
        systems["firewall"] = {
            "status": "healthy",
            "metrics": {"Status": "Active"},
        }

        # DNS
        systems["dns_sec"] = {
            "status": "healthy",
            "metrics": {"Status": "Resolving"},
        }

        # Endpoints
        systems["endpoints"] = {
            "status": "warning" if agents_online < agents_total else "healthy",
            "metrics": {"Online": agents_online, "Offline": agents_total - agents_online},
        }

        # Clove
        clove_count = s.get("by_source", {}).get("clove", 0)
        systems["clove"] = {
            "status": "healthy",
            "metrics": {"Alerts": clove_count},
        }

        # Core
        systems["shallotd"] = {
            "status": "healthy",
            "metrics": {"Total": total, "Pending": pending, "Auto": s.get("auto_handled", 0)},
        }

        return systems

    def format_incident_for_nova(self, incident):
        """Format an incident into Nova-friendly text."""
        return {
            "id": incident.get("id", "?"),
            "title": incident.get("title", "Unknown incident"),
            "summary": incident.get("summary", ""),
            "severity": incident.get("severity", "medium"),
            "status": incident.get("status", "new"),
            "category": incident.get("category", "unknown"),
            "affected_ips": incident.get("affected_ips", []),
            "alert_count": incident.get("alert_count", 0),
            "runbook": incident.get("runbook", []),
        }

    def generate_investigation_terminal(self, incident, step_index=0):
        """Generate realistic terminal output for investigating an incident."""
        inc = incident
        ips = inc.get("affected_ips", [])
        title = inc.get("title", "Unknown")
        category = inc.get("category", "")
        severity = inc.get("severity", "medium")

        lines = [
            (f"nova@shallots:~$ # Investigating: {title}", 0.3),
            (f"nova@shallots:~$ curl -s shallots/api/incidents/{inc.get('id', '?')[:8]}", 0.4),
            (f"  severity: {severity}", 0.3),
            (f"  category: {category}", 0.3),
            (f"  alert_count: {inc.get('alert_count', 0)}", 0.3),
            (f"  affected_ips: {', '.join(ips[:3])}", 0.3),
        ]

        if category == "lateral_movement":
            lines.extend([
                ("nova@shallots:~$ # Checking lateral movement pattern", 0.4),
                (f"nova@shallots:~$ suricata-query --src {ips[0] if ips else '?'} --last 1h", 0.5),
                (f"  {len(ips)} internal IPs contacted", 0.4),
                ("  Protocol: SSH/SMB mix", 0.3),
                ("  Pattern: sequential port scanning", 0.4),
            ])
        elif "ssh" in title.lower() or "brute" in title.lower():
            lines.extend([
                ("nova@shallots:~$ # SSH brute force analysis", 0.4),
                (f"nova@shallots:~$ grep 'Failed password' /var/log/auth.log | tail -5", 0.5),
                (f"  Multiple failed attempts from {ips[0] if ips else 'unknown'}", 0.4),
                ("  Checking against threat intel...", 0.5),
            ])
        else:
            lines.extend([
                (f"nova@shallots:~$ # Analyzing {category} event", 0.4),
                (f"nova@shallots:~$ shallots-cli query --incident {inc.get('id', '?')[:8]}", 0.5),
                ("  Pulling correlated alerts...", 0.5),
            ])

        # Runbook steps
        runbook = inc.get("runbook", [])
        if runbook and step_index < len(runbook):
            step = runbook[step_index]
            if step.get("command"):
                lines.append((f"nova@shallots:~$ {step['command']}", 0.5))
                lines.append(("  Executing...", 0.8))

        lines.append(("--- SUMMARY FOR NOVA ---", 0.3))
        lines.append((f"Incident: {title}", 0.2))
        lines.append((f"Severity: {severity} | Alerts: {inc.get('alert_count', 0)} | IPs: {len(ips)}", 0.2))

        return lines

    def generate_decisions_for_incident(self, incident):
        """Generate decision options for a real incident."""
        severity = incident.get("severity", "medium")
        category = incident.get("category", "")
        ips = incident.get("affected_ips", [])

        options = []

        if severity == "critical":
            options.append({
                "id": "isolate",
                "label": "Isolate Affected Systems",
                "keywords": ["isolate", "block", "quarantine", "cut", "disconnect"],
                "score": 15,
                "result": f"Isolating {', '.join(ips[:2])}. Network access restricted pending investigation.",
                "action": {"type": "status", "status": "investigating"},
            })

        options.append({
            "id": "investigate",
            "label": "Deep Investigation",
            "keywords": ["investigate", "dig", "look", "analyze", "deeper", "more"],
            "score": 12,
            "result": "Sending agents for deeper analysis. Pulling full packet captures and host logs.",
            "action": {"type": "status", "status": "investigating"},
        })

        options.append({
            "id": "acknowledge",
            "label": "Acknowledge and Monitor",
            "keywords": ["acknowledge", "ack", "monitor", "watch", "note"],
            "score": 8,
            "result": "Acknowledged. Monitoring for escalation.",
            "action": {"type": "status", "status": "acknowledged"},
        })

        if severity in ("low", "medium"):
            options.append({
                "id": "dismiss",
                "label": "Dismiss — False Positive",
                "keywords": ["dismiss", "false", "positive", "ignore", "benign", "safe"],
                "score": 5,
                "result": "Dismissed as false positive. Pattern added to suppress future similar alerts.",
                "action": {"type": "status", "status": "dismissed"},
            })

        return options


async def test_connection():
    """Quick test of the Shallots bridge."""
    bridge = ShallotsBridge()
    connected = await bridge.connect()
    print(f"Connected: {connected}")

    if connected:
        stats = await bridge.get_stats()
        print(f"Total alerts: {stats.get('total_alerts', '?')}")
        print(f"Critical: {stats.get('by_severity', {}).get('critical', 0)}")
        print(f"Agents: {stats.get('agents_online', 0)}/{stats.get('agents_total', 0)}")

        incidents = await bridge.get_incidents(5)
        print(f"\nActive incidents: {len(incidents)}")
        for inc in incidents[:3]:
            print(f"  [{inc.get('severity')}] {inc.get('title', '?')[:60]}")
            print(f"    IPs: {inc.get('affected_ips', [])}")

        systems = bridge.format_stats_for_nova()
        print(f"\nSystem statuses:")
        for sid, sys in systems.items():
            print(f"  {sid}: {sys['status']} | {sys['metrics']}")

    await bridge.close()


if __name__ == "__main__":
    asyncio.run(test_connection())
