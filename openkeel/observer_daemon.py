#!/usr/bin/env python3
"""Observer Daemon — launches Cartographer, Pilgrim, and Oracle as a sidecar process.

Run alongside Claude Code on jagg. Reads the distilled log that the scribe hook writes,
builds the problem-space graph, walks it for blind spots, and injects findings.

Usage:
    python3 -m openkeel.observer_daemon <mission_name> [--no-oracle] [--dry-run]
    python3 -m openkeel.observer_daemon pirate-htb
    python3 -m openkeel.observer_daemon pirate-htb --no-oracle  # skip 122B model

The daemon writes:
    ~/.openkeel/goals/<mission>/problem_map.json   — Cartographer's graph
    ~/.openkeel/goals/<mission>/observer_nudges.jsonl — findings for inject hook
    ~/.openkeel/observer_daemon.pid  — PID file for monitoring
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

# Ensure openkeel is importable
_this = Path(__file__).resolve().parent.parent
if str(_this) not in sys.path:
    sys.path.insert(0, str(_this))

from openkeel.core.observers import ObserverOrchestrator, ObserverConfig
from openkeel.core.oracle import OracleConfig
from openkeel.integrations.local_llm import (
    LLMEndpoint,
    CARTOGRAPHER_ENDPOINT,
    PILGRIM_ENDPOINT,
    OVERWATCH_ENDPOINT,
    check_health,
)


OPENKEEL_DIR = Path.home() / ".openkeel"
GOALS_DIR = OPENKEEL_DIR / "goals"
PID_FILE = OPENKEEL_DIR / "observer_daemon.pid"
NUDGE_FILE_NAME = "observer_nudges.jsonl"


def _write_pid():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _load_mission_context(mission_dir: Path) -> dict:
    """Load mission YAML for context."""
    for candidate in [
        mission_dir / "mission.yaml",
        OPENKEEL_DIR / "missions" / f"{mission_dir.name}.yaml",
    ]:
        if candidate.exists():
            try:
                import yaml
                with open(candidate, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except ImportError:
                with open(candidate, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


def _write_nudge(mission_dir: Path, text: str, level: str = "nudge"):
    """Append a nudge/interrupt to the nudges file for the inject hook to read."""
    nudge_path = mission_dir / NUDGE_FILE_NAME
    entry = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "level": level,
        "text": text,
    }, separators=(",", ":"))
    with open(nudge_path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def _check_ollama_endpoints(config: ObserverConfig) -> dict:
    """Check which LLM endpoints are reachable."""
    results = {}
    for name, ep in [
        ("cartographer", config.cartographer_endpoint),
        ("pilgrim", config.pilgrim_endpoint),
    ]:
        results[name] = check_health(ep)
    if config.enable_oracle:
        results["oracle"] = check_health(config.oracle.endpoint)
    return results


def main():
    parser = argparse.ArgumentParser(description="Observer Daemon for HTB autopwn")
    parser.add_argument("mission", help="Mission name (maps to ~/.openkeel/goals/<name>/)")
    parser.add_argument("--no-oracle", action="store_true", help="Disable Oracle (skip 122B model)")
    parser.add_argument("--no-llm", action="store_true", help="Structural-only mode (no LLM calls)")
    parser.add_argument("--dry-run", action="store_true", help="Check endpoints and exit")
    parser.add_argument("--cart-port", type=int, default=11444, help="Cartographer Ollama port")
    parser.add_argument("--pilgrim-port", type=int, default=11445, help="Pilgrim Ollama port")
    parser.add_argument("--oracle-port", type=int, default=11446, help="Oracle Ollama port")
    parser.add_argument("--cart-interval", type=float, default=5.0, help="Cartographer poll seconds")
    parser.add_argument("--pilgrim-interval", type=float, default=120.0, help="Pilgrim walk seconds")
    parser.add_argument("--oracle-interval", type=float, default=300.0, help="Oracle cycle seconds")
    args = parser.parse_args()

    mission_dir = GOALS_DIR / args.mission
    mission_dir.mkdir(parents=True, exist_ok=True)

    print(f"[OBSERVER] Mission dir: {mission_dir}")
    print(f"[OBSERVER] Cartographer: port {args.cart_port}")
    print(f"[OBSERVER] Pilgrim: port {args.pilgrim_port}")
    if not args.no_oracle:
        print(f"[OBSERVER] Oracle: port {args.oracle_port}")

    # Build config with custom ports
    cart_ep = LLMEndpoint(
        name="Weary Cartographer",
        port=args.cart_port,
        model=CARTOGRAPHER_ENDPOINT.model,
        api_type="ollama",
    )
    pilgrim_ep = LLMEndpoint(
        name="Vigilant Pilgrim",
        port=args.pilgrim_port,
        model=PILGRIM_ENDPOINT.model,
        api_type="ollama",
    )
    oracle_cfg = OracleConfig(
        endpoint=LLMEndpoint(
            name="Overwatch Oracle",
            port=args.oracle_port,
            model=OVERWATCH_ENDPOINT.model,
            api_type="ollama",
            max_tokens=100,
            temperature=0.2,
            timeout=300,
        ),
        cycle_seconds=args.oracle_interval,
    )

    config = ObserverConfig(
        cartographer_endpoint=cart_ep,
        pilgrim_endpoint=pilgrim_ep,
        cartographer_poll_seconds=args.cart_interval,
        pilgrim_walk_seconds=args.pilgrim_interval,
        use_llm_cartographer=False,  # structural only
        use_llm_pilgrim=not args.no_llm,
        oracle=oracle_cfg,
        enable_oracle=not args.no_oracle,
    )

    # Check endpoints
    print("[OBSERVER] Checking LLM endpoints...")
    health = _check_ollama_endpoints(config)
    for name, status in health.items():
        state = status.get("status", "unknown")
        print(f"  {name}: {state}")

    if args.dry_run:
        print("[OBSERVER] Dry run — exiting.")
        return

    # Load mission context
    mission_data = _load_mission_context(mission_dir)
    objective = mission_data.get("objective", "")
    plan_steps = mission_data.get("plan", [])
    plan_text = "\n".join(
        f"  {s.get('id', '?')}. {s.get('step', '')}" for s in plan_steps
        if isinstance(s, dict)
    )
    credentials = mission_data.get("credentials", [])

    # Callbacks
    def on_nudge(text: str):
        ts = time.strftime("%H:%M:%S")
        print(f"[NUDGE {ts}] {text}")
        _write_nudge(mission_dir, text, "nudge")

    def on_interrupt(text: str):
        ts = time.strftime("%H:%M:%S")
        print(f"[INTERRUPT {ts}] {text}")
        _write_nudge(mission_dir, text, "interrupt")

    def on_status(text: str):
        ts = time.strftime("%H:%M:%S")
        print(f"[STATUS {ts}] {text}")

    # Create orchestrator
    orch = ObserverOrchestrator(
        mission_dir=mission_dir,
        config=config,
        on_nudge=on_nudge,
        on_interrupt=on_interrupt,
        on_status=on_status,
    )

    # Set mission context
    orch.set_mission_context(
        objective=objective,
        plan=plan_text,
        credentials=credentials if isinstance(credentials, list) else [],
    )

    # Signal handlers
    def _shutdown(signum, frame):
        print(f"\n[OBSERVER] Received signal {signum}, shutting down...")
        orch.stop()
        _remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start
    _write_pid()
    print(f"[OBSERVER] Starting observers (PID {os.getpid()})...")
    orch.start()

    # Health check after startup
    print("[OBSERVER] Initial health check:")
    h = orch.health_check()
    for k, v in h.items():
        print(f"  {k}: {v}")

    print("[OBSERVER] Running. Press Ctrl+C to stop.")

    # Main loop — just keep alive and periodically log status
    try:
        while True:
            time.sleep(30)
            h = orch.health_check()
            nodes = h.get("map_nodes", 0)
            edges = h.get("map_edges", 0)
            entries = h.get("log_entries", 0)
            contras = h.get("contradictions", 0)
            print(f"[HEARTBEAT] nodes={nodes} edges={edges} log={entries} contradictions={contras}")
    except KeyboardInterrupt:
        pass
    finally:
        orch.stop()
        _remove_pid()
        print("[OBSERVER] Stopped.")


if __name__ == "__main__":
    main()
