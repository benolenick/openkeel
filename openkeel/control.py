#!/usr/bin/env python3
"""OpenKeel Control — management interface for the Claude Code agent.

This is how the agent manages treadstone, observers, and the attack tree
without requiring the user to type commands.

Usage:
    python3 -m openkeel.control status
    python3 -m openkeel.control advance
    python3 -m openkeel.control add-stone "Initial Recon" "Enumerate attack surface"
    python3 -m openkeel.control add-hyp "SQL injection in login" "Login form echoes input"
    python3 -m openkeel.control record <hyp-id> success|fail|partial "notes"
    python3 -m openkeel.control phase recon|research|run|review
    python3 -m openkeel.control observers start|stop|status
    python3 -m openkeel.control nudge "text"
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


OPENKEEL_DIR = Path.home() / ".openkeel"
GOALS_DIR = OPENKEEL_DIR / "goals"


def _get_project() -> str:
    active_file = OPENKEEL_DIR / "active_mission.txt"
    try:
        return active_file.read_text(encoding="utf-8").strip() or "default"
    except OSError:
        return "default"


def _mission_dir() -> Path:
    d = GOALS_DIR / _get_project()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_tree():
    try:
        from openkeel.core.treadstone import load_tree
        return load_tree(_mission_dir())
    except Exception:
        return None


def _save_tree(tree):
    from openkeel.core.treadstone import save_tree
    save_tree(_mission_dir(), tree)


def cmd_status(args):
    """Full system status."""
    project = _get_project()
    mdir = _mission_dir()
    print(f"Mission: {project}")
    print(f"Dir: {mdir}")
    print()

    # Treadstone tree
    tree = _load_tree()
    if tree:
        from openkeel.core.treadstone import tree_status_line, get_active_stone
        print(f"[TREADSTONE] {tree_status_line(tree)}")
        stone = get_active_stone(tree)
        if stone:
            print(f"  Active: {stone.label} (phase: {stone.phase})")
            print(f"  Objective: {stone.objective}")
            for h in stone.hypotheses:
                if h.status == "active":
                    bar = "#" * int(h.confidence * 10) + "-" * (10 - int(h.confidence * 10))
                    print(f"    H[{h.id[:8]}] {h.label}: [{bar}] {h.confidence:.0%} ({h.attempt_count}/{tree.circuit_breaker.max_attempts_per_hypothesis} attempts)")
                elif h.status == "succeeded":
                    print(f"    H[{h.id[:8]}] {h.label}: SUCCEEDED")
                elif h.status == "abandoned":
                    print(f"    H[{h.id[:8]}] {h.label}: ABANDONED")
        total = len(tree.stones)
        done = sum(1 for s in tree.stones if s.status == "done")
        print(f"  Stones: {done}/{total} done")
    else:
        print("[TREADSTONE] No tree. Use: add-stone to create one.")

    print()

    # Observer daemon
    pid_file = OPENKEEL_DIR / "observer_daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"[OBSERVERS] Running (PID {pid})")
        except (ValueError, OSError):
            print("[OBSERVERS] Stale PID (not running)")
    else:
        print("[OBSERVERS] Not running")

    # Nudge file
    nudge_path = mdir / "observer_nudges.jsonl"
    if nudge_path.exists():
        content = nudge_path.read_text().strip()
        count = len([l for l in content.split("\n") if l.strip()]) if content else 0
        if count:
            print(f"  Pending nudges: {count}")

    # Distilled log
    log_path = mdir / "distilled_log.jsonl"
    if log_path.exists():
        count = sum(1 for _ in open(log_path, encoding="utf-8"))
        print(f"  Log entries: {count}")

    # Problem map
    map_path = mdir / "problem_map.yaml"
    if map_path.exists():
        try:
            import yaml
            data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
            nodes = len(data.get("nodes", {}))
            edges = len(data.get("edges", []))
            print(f"  Problem map: {nodes} nodes, {edges} edges")
        except Exception:
            pass


def cmd_advance(args):
    """Advance to next stone."""
    from openkeel.core.treadstone import get_active_stone, advance_to_stone

    tree = _load_tree()
    if not tree:
        print("No tree exists.")
        return

    next_stone = None
    for s in tree.stones:
        if s.status == "pending":
            next_stone = s
            break

    if not next_stone:
        print("No pending stones to advance to.")
        return

    active = get_active_stone(tree)
    advance_to_stone(tree, next_stone.id)
    _save_tree(tree)
    prev_label = active.label if active else "none"
    print(f"Advanced: {prev_label} -> {next_stone.label}")
    print(f"Objective: {next_stone.objective}")


def cmd_add_stone(args):
    """Add a new stepping stone."""
    from openkeel.core.treadstone import create_tree, add_stone

    tree = _load_tree()
    if not tree:
        tree = create_tree(_get_project())

    stone = add_stone(tree, label=args.label, objective=args.objective or "")
    _save_tree(tree)
    print(f"Stone [{stone.id[:8]}] {stone.label}" + (" (active)" if stone.status == "active" else ""))


def cmd_add_hyp(args):
    """Add hypothesis to active stone."""
    from openkeel.core.treadstone import get_active_stone, add_hypothesis

    tree = _load_tree()
    if not tree:
        print("No tree. Use add-stone first.")
        return

    stone = get_active_stone(tree)
    if not stone:
        print("No active stone.")
        return

    h = add_hypothesis(stone, label=args.label, rationale=args.rationale or "")
    _save_tree(tree)
    print(f"Hypothesis [{h.id[:8]}] {h.label} ({h.confidence:.0%})")


def cmd_record(args):
    """Record attempt result for a hypothesis."""
    from openkeel.core.treadstone import (
        get_active_stone, record_attempt, check_circuit_breaker,
    )

    tree = _load_tree()
    if not tree:
        print("No tree.")
        return

    stone = get_active_stone(tree)
    if not stone:
        print("No active stone.")
        return

    hyp = None
    for h in stone.hypotheses:
        if h.id.startswith(args.hyp_id):
            hyp = h
            break

    if not hyp:
        print(f"Hypothesis '{args.hyp_id}' not found. Active:")
        for h in stone.hypotheses:
            if h.status == "active":
                print(f"  [{h.id[:8]}] {h.label}")
        return

    record_attempt(
        hyp, command="manual", expected="",
        actual=args.notes or "", result=args.result,
        notes=args.notes or "",
    )
    alerts = check_circuit_breaker(stone, hyp, tree.circuit_breaker)
    _save_tree(tree)
    print(f"{args.result}: {hyp.label} ({hyp.confidence:.0%})")
    for alert in alerts:
        print(f"  !! {alert}")


def cmd_phase(args):
    """Set phase of active stone."""
    from openkeel.core.treadstone import get_active_stone

    tree = _load_tree()
    if not tree:
        print("No tree.")
        return

    stone = get_active_stone(tree)
    if not stone:
        print("No active stone.")
        return

    stone.phase = args.phase
    _save_tree(tree)
    print(f"Phase: {args.phase}")


def cmd_observers(args):
    """Manage observer daemon."""
    action = args.action

    if action == "status":
        pid_file = OPENKEEL_DIR / "observer_daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                print(f"Running (PID {pid})")
            except (ValueError, OSError):
                print("Not running (stale PID)")
        else:
            print("Not running")

    elif action == "start":
        mission = _get_project()
        cmd = [
            sys.executable, "-m", "openkeel.observer_daemon",
            mission, "--no-oracle",
        ]
        log_dir = Path("/tmp/observers")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "observer_daemon.log"
        subprocess.Popen(
            cmd,
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
        )
        print(f"Starting observer daemon for '{mission}'")
        print(f"Log: {log_file}")

    elif action == "stop":
        pid_file = OPENKEEL_DIR / "observer_daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                print(f"Stopped (PID {pid})")
            except (ValueError, OSError) as e:
                print(f"Stop failed: {e}")
        else:
            print("Not running")


def cmd_nudge(args):
    """Manually inject a nudge."""
    mdir = _mission_dir()
    nudge_path = mdir / "observer_nudges.jsonl"
    entry = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "level": "nudge",
        "text": args.text,
    }, separators=(",", ":"))
    with open(nudge_path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    print(f"Nudge injected: {args.text}")


def main():
    parser = argparse.ArgumentParser(description="OpenKeel Control")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Full system status")
    sub.add_parser("advance", help="Advance to next stone")

    p = sub.add_parser("add-stone", help="Add stepping stone")
    p.add_argument("label")
    p.add_argument("objective", nargs="?", default="")

    p = sub.add_parser("add-hyp", help="Add hypothesis to active stone")
    p.add_argument("label")
    p.add_argument("rationale", nargs="?", default="")

    p = sub.add_parser("record", help="Record attempt result")
    p.add_argument("hyp_id")
    p.add_argument("result", choices=["success", "fail", "partial"])
    p.add_argument("notes", nargs="?", default="")

    p = sub.add_parser("phase", help="Set stone phase")
    p.add_argument("phase", choices=["recon", "research", "run", "review"])

    p = sub.add_parser("observers", help="Manage observer daemon")
    p.add_argument("action", choices=["start", "stop", "status"])

    p = sub.add_parser("nudge", help="Inject a manual nudge")
    p.add_argument("text")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "status": cmd_status,
        "advance": cmd_advance,
        "add-stone": cmd_add_stone,
        "add-hyp": cmd_add_hyp,
        "record": cmd_record,
        "phase": cmd_phase,
        "observers": cmd_observers,
        "nudge": cmd_nudge,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
