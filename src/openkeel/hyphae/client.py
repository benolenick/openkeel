"""Hyphae memory client — long-term memory for OpenKeel.

Queries Hyphae for relevant project context before gathering,
and remembers key findings after reasoning.
"""

import json
import os
import urllib.request
import urllib.error


def _get_endpoint():
    """Get Hyphae endpoint from settings or env."""
    url = os.environ.get("OPENKEEL_HYPHAE_URL") or os.environ.get("BUBBLE_HYPHAE_URL")
    if url:
        return url
    # Try reading from settings file
    settings_path = os.path.join(os.path.expanduser("~"), ".openkeel2", "settings.json")
    try:
        with open(settings_path) as f:
            s = json.load(f)
            return s.get("hyphae_url", "http://127.0.0.1:8100")
    except Exception:
        return "http://127.0.0.1:8100"


HYPHAE_ENDPOINT = _get_endpoint()
HYPHAE_TIMEOUT = 5


def _post(path, data):
    """POST JSON to Hyphae. Returns parsed response or None."""
    try:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{HYPHAE_ENDPOINT}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HYPHAE_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _get(path):
    """GET from Hyphae. Returns parsed response or None."""
    try:
        req = urllib.request.Request(f"{HYPHAE_ENDPOINT}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=HYPHAE_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def is_available():
    """Check if Hyphae is running."""
    data = _get("/health")
    return data is not None


def recall(query, top_k=5):
    """Recall relevant memories for a task. Returns list of fact strings."""
    data = _post("/recall", {"query": query, "top_k": top_k})
    if not data:
        return []
    results = data.get("results", [])
    return [r.get("text", "") for r in results if r.get("text")]


def get_briefing(project=None):
    """Get session briefing for the current project."""
    if not project:
        project = "general"
    data = _get(f"/briefing/{project}")
    if not data or data.get("is_fallback"):
        return ""
    return data.get("briefing", "")


def remember(fact_text, source="bubble"):
    """Save a finding to Hyphae memory."""
    _post("/remember", {"text": fact_text, "source": source})


def get_context_for_task(task, project=None):
    """Get relevant Hyphae context for a task.

    Returns a formatted string with memories + briefing, or empty string.
    """
    parts = []

    # Recall memories relevant to the task
    memories = recall(task, top_k=5)
    if memories:
        parts.append("## Relevant Memories (from Hyphae)\n")
        for i, mem in enumerate(memories, 1):
            parts.append(f"{i}. {mem[:300]}")
        parts.append("")

    # Get project briefing (recent activity)
    briefing = get_briefing(project)
    if briefing:
        parts.append("## Recent Project Context\n")
        parts.append(briefing[:2000])
        parts.append("")

    return "\n".join(parts)
