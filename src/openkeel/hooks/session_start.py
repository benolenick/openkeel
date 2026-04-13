#!/usr/bin/env python3
"""OpenKeel v3 — SessionStart hook.

Lightweight session injection:
  1. Hyphae health check + session scoping + usage instructions
  2. Token saver daemon startup

No missions, cortex, observers, or other bloat.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

HYPHAE_ENDPOINT = os.environ.get("HYPHAE_URL", "http://127.0.0.1:8100")


def _get_project_name() -> str:
    """Derive project name from cwd."""
    return os.path.basename(os.getcwd()) or "default"


def _check_hyphae():
    """Check Hyphae, set session scope, inject usage."""
    project = _get_project_name()

    try:
        # Health check
        req = urllib.request.Request(f"{HYPHAE_ENDPOINT}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        facts = data.get("facts", "?")
        clusters = data.get("clusters", "?")

        # Set session scope
        scope_payload = json.dumps({"scope": {"project": project}}).encode("utf-8")
        scope_req = urllib.request.Request(
            f"{HYPHAE_ENDPOINT}/session/set",
            data=scope_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(scope_req, timeout=5) as resp:
            scope_data = json.loads(resp.read().decode("utf-8"))
        warmed = scope_data.get("warmed", 0)

        # Fetch briefing
        briefing_text = ""
        try:
            briefing_req = urllib.request.Request(
                f"{HYPHAE_ENDPOINT}/briefing/{project}", method="GET"
            )
            with urllib.request.urlopen(briefing_req, timeout=5) as resp:
                briefing_data = json.loads(resp.read().decode("utf-8"))
            if briefing_data.get("briefing") and not briefing_data.get("is_fallback"):
                briefing_text = briefing_data["briefing"]
        except Exception:
            pass

        print(f"[OPENKEEL HYPHAE] Connected — {facts} facts, {clusters} clusters")
        print(f"[OPENKEEL HYPHAE] Session: {project} (warmed {warmed} facts)")

        if briefing_text:
            print()
            print("=" * 60)
            print("SESSION BRIEFING (auto-injected short-term memory)")
            print("=" * 60)
            print(f"Project: {project}")
            print()
            print(briefing_text)
            print("=" * 60)

        # Usage instructions
        print()
        print("=" * 60)
        print("HYPHAE MEMORY (auto-injected)")
        print("=" * 60)
        print(f"Hyphae is running at {HYPHAE_ENDPOINT} with {facts} facts.")
        print(f"Session scope: project={project}")
        print()
        print("USAGE:")
        print()
        print("Recall memories (scoped to current project by default):")
        print(f'  curl -s -X POST {HYPHAE_ENDPOINT}/recall -H "Content-Type: application/json" -d \'{{"query": "<search>", "top_k": 10}}\'')
        print()
        print("Recall across ALL projects (unscoped):")
        print(f'  curl -s -X POST {HYPHAE_ENDPOINT}/recall -H "Content-Type: application/json" -d \'{{"query": "<search>", "top_k": 10, "scope": {{}}}}\'')
        print()
        print("Remember a fact:")
        print(f'  curl -s -X POST {HYPHAE_ENDPOINT}/remember -H "Content-Type: application/json" -d \'{{"text": "<fact>", "source": "agent"}}\'')
        print("=" * 60)

    except Exception as e:
        print(f"[OPENKEEL HYPHAE] OFFLINE — not reachable at {HYPHAE_ENDPOINT}")
        print("[OPENKEEL HYPHAE] NOTE: Hyphae often takes 10-30s to start. Retry when needed.")


def main():
    _check_hyphae()


if __name__ == "__main__":
    main()
