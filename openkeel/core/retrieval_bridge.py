"""Retrieval Bridge — connects Cartographer/Pilgrim to Pathfinder v2 + Memoria.

Wired into ObserverOrchestrator (Option A: same process).
Three integration points:

1. enrich_new_nodes:  Called after Cartographer processes a log entry.
   Queries Memoria for each new node -> adds latent nodes the operator hasn't found.

2. fill_pilgrim_gaps: Called during Pilgrim walk.
   Takes blind spots + known nodes -> calls Pathfinder /analyze -> returns enriched findings.

3. proactive_retrieve: Called on every log entry regardless of success/failure.
   Extracts technical terms -> queries Memoria -> returns nudges if relevant hits found.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("openkeel.retrieval_bridge")

PATHFINDER_URL = "http://127.0.0.1:8005"
MEMORIA_URL = "http://127.0.0.1:8000"
MAX_LATENT_NODES_PER_ENTRY = 3
PATHFINDER_TIMEOUT = 30
MEMORIA_TIMEOUT = 10

NOISE_MARKERS = [
    "ietf-", "value_string", "CONFIG_", "static const",
    "return offset", '{"draft":', "ietf-ac-svc", "[Wireshark]",
]

TRIGGER_PATTERNS = [
    "nginx", "apache", "proxy", "traversal", "ssrf", "lfi", "rfi",
    "ldap", "kerberos", "ntlm", "smb", "winrm", "rdp",
    "gitea", "gitlab", "jenkins", "pwm", "moodle", "wordpress",
    "cve-", "exploit", "vulnerability", "misconfiguration",
    "certificate", "adcs", "esc1", "esc8", "certipy",
    "password", "credential", "hash", "ntds", "dcsync",
    "reverse shell", "foothold", "privilege escalation", "lateral movement",
]


def _query_memoria(query: str, top_k: int = 5) -> list[dict]:
    try:
        payload = json.dumps({"query": query, "top_k": top_k}).encode()
        req = urllib.request.Request(
            f"{MEMORIA_URL}/search", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=MEMORIA_TIMEOUT) as resp:
            return json.loads(resp.read()).get("results", [])
    except Exception as e:
        logger.debug(f"Memoria query failed: {e}")
        return []


def _query_pathfinder_analyze(observations: list[str], objective: str) -> dict:
    try:
        payload = json.dumps({
            "observations": observations, "objective": objective, "use_memoria": True,
        }).encode()
        req = urllib.request.Request(
            f"{PATHFINDER_URL}/analyze", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=PATHFINDER_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug(f"Pathfinder analyze failed: {e}")
        return {}


def _is_noise(text: str) -> bool:
    return any(m in text for m in NOISE_MARKERS)


def enrich_new_nodes(new_node_texts: list[str], existing_node_texts: set[str]) -> list[dict]:
    """Query Memoria for each new node text. Return latent nodes to add to the graph."""
    latent_nodes = []
    seen = set(existing_node_texts)
    for node_text in new_node_texts[:5]:
        results = _query_memoria(node_text[:200], top_k=3)
        for r in results:
            text = r.get("text", "").strip()[:300]
            if not text or len(text) < 20 or text in seen or _is_noise(text):
                continue
            seen.add(text)
            latent_nodes.append({
                "text": text, "node_type": "fact", "source": "memoria",
                "confidence": 0.5, "discovered": False,
            })
            if len(latent_nodes) >= MAX_LATENT_NODES_PER_ENTRY:
                break
        if len(latent_nodes) >= MAX_LATENT_NODES_PER_ENTRY:
            break
    if latent_nodes:
        logger.info(f"Enrichment: {len(latent_nodes)} latent nodes from {len(new_node_texts)} new nodes")
    return latent_nodes


def fill_pilgrim_gaps(blind_spots: list[dict], known_observations: list[str], objective: str = "") -> list[dict]:
    """For each Pilgrim blind spot, query Pathfinder v2 for knowledge that fills the gap."""
    if not blind_spots:
        return []
    enriched = []
    obs = known_observations[-10:]
    for spot in blind_spots[:5]:
        desc = spot.get("description", "")
        if not desc:
            continue
        result = {
            "blind_spot": desc, "category": spot.get("category", "unknown"),
            "severity": spot.get("severity", 0), "memoria_hits": [], "pathfinder_hits": [],
        }
        memoria_results = _query_memoria(desc[:200], top_k=5)
        for r in memoria_results:
            text = r.get("text", "").strip()[:300]
            if text and len(text) > 20 and not _is_noise(text):
                result["memoria_hits"].append(text)
        if objective:
            pf_data = _query_pathfinder_analyze(observations=obs + [desc], objective=objective)
            for r in pf_data.get("memoria_results", [])[:5]:
                text = r.get("text", "").strip()[:300]
                if text and text not in result["memoria_hits"] and not _is_noise(text):
                    result["pathfinder_hits"].append(text)
        if result["memoria_hits"] or result["pathfinder_hits"]:
            enriched.append(result)
            logger.info(f"Filled gap '{desc[:50]}': {len(result['memoria_hits'])} memoria, {len(result['pathfinder_hits'])} pathfinder")
    return enriched


def proactive_retrieve(log_text: str) -> list[str]:
    """Extract technical terms from log output, query Memoria, return nudges."""
    if not log_text or len(log_text) < 30:
        return []
    text_lower = log_text.lower()
    matched = [p for p in TRIGGER_PATTERNS if p in text_lower]
    if not matched:
        return []
    results = _query_memoria(log_text[:200], top_k=5)
    if not results:
        return []
    nudges = []
    for r in results[:3]:
        text = r.get("text", "").strip()
        if not text or len(text) < 30 or _is_noise(text) or text[:50] in log_text:
            continue
        nudges.append(f"[RETRIEVAL] {text[:200]}")
    if nudges:
        logger.info(f"Proactive retrieval: {len(nudges)} nudges from '{log_text[:40]}...'")
    return nudges


def format_enrichment_nudge(enriched_findings: list[dict]) -> str:
    """Format enriched Pilgrim findings for injection."""
    if not enriched_findings:
        return ""
    parts = ["[RETRIEVAL BRIDGE] Knowledge found for blind spots:"]
    for f in enriched_findings[:3]:
        parts.append(f"\n  BLIND SPOT: {f['blind_spot'][:100]}")
        parts.append(f"  Severity: {f['severity']}, Category: {f['category']}")
        for hit in (f.get("memoria_hits", []) + f.get("pathfinder_hits", []))[:3]:
            parts.append(f"    -> {hit[:150]}")
    return "\n".join(parts)
