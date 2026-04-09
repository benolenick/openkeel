#!/usr/bin/env python3
import json
import sys
import csv
import io
from datetime import datetime, timedelta
from pathlib import Path

DATA_FILE = Path.home() / ".tt.json"


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"sessions": [], "active": None}


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2))


def fmt_duration(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def start(label=""):
    data = load_data()
    if data["active"]:
        task = data["active"].get("label") or "(unlabeled)"
        print(f"Already tracking: {task}. Stop it first.")
        sys.exit(1)
    data["active"] = {
        "label": label,
        "start": datetime.now().isoformat(),
    }
    save_data(data)
    tag = f" [{label}]" if label else ""
    print(f"Started{tag} at {datetime.now().strftime('%H:%M:%S')}")


def stop():
    data = load_data()
    if not data["active"]:
        print("No active session.")
        sys.exit(1)
    active = data["active"]
    started = datetime.fromisoformat(active["start"])
    ended = datetime.now()
    duration = (ended - started).total_seconds()
    session = {
        "label": active.get("label", ""),
        "start": active["start"],
        "end": ended.isoformat(),
        "duration": duration,
    }
    data["sessions"].append(session)
    data["active"] = None
    save_data(data)
    tag = f" [{session['label']}]" if session["label"] else ""
    print(f"Stopped{tag} — {fmt_duration(duration)}")


def log():
    data = load_data()
    today = datetime.now().date()
    sessions = [
        s for s in data["sessions"]
        if datetime.fromisoformat(s["start"]).date() == today
    ]
    if data["active"]:
        active = data["active"]
        started = datetime.fromisoformat(active["start"])
        elapsed = (datetime.now() - started).total_seconds()
        tag = f" [{active['label']}]" if active.get("label") else ""
        print(f"  ACTIVE{tag}  started {started.strftime('%H:%M')}  ({fmt_duration(elapsed)} so far)")
    if not sessions and not data["active"]:
        print("No sessions today.")
        return
    total = 0
    for s in sessions:
        started = datetime.fromisoformat(s["start"])
        ended = datetime.fromisoformat(s["end"])
        label = f"  [{s['label']}]" if s["label"] else ""
        print(f"  {started.strftime('%H:%M')}–{ended.strftime('%H:%M')}{label}  {fmt_duration(s['duration'])}")
        total += s["duration"]
    if sessions:
        print(f"  ─────────────────────────")
        print(f"  Total: {fmt_duration(total)}")


def week():
    data = load_data()
    today = datetime.now().date()
    days = {}
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        days[d] = []
    for s in data["sessions"]:
        d = datetime.fromisoformat(s["start"]).date()
        if d in days:
            days[d].append(s["duration"])
    print(f"  {'Day':<12} {'Sessions':>8}  {'Total':>10}")
    print(f"  {'─'*12} {'─'*8}  {'─'*10}")
    week_total = 0
    for d, durations in days.items():
        total = sum(durations)
        week_total += total
        marker = " ◀ today" if d == today else ""
        print(f"  {d.strftime('%a %b %d'):<12} {len(durations):>8}  {fmt_duration(total):>10}{marker}")
    print(f"  {'─'*12} {'─'*8}  {'─'*10}")
    print(f"  {'Week total':<12} {sum(len(v) for v in days.values()):>8}  {fmt_duration(week_total):>10}")


def export():
    data = load_data()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["label", "start", "end", "duration_seconds", "duration"])
    for s in data["sessions"]:
        writer.writerow([
            s.get("label", ""),
            s["start"],
            s["end"],
            round(s["duration"]),
            fmt_duration(s["duration"]),
        ])
    print(out.getvalue(), end="")


def status():
    data = load_data()
    if not data["active"]:
        print("No active session.")
        return
    active = data["active"]
    started = datetime.fromisoformat(active["start"])
    elapsed = (datetime.now() - started).total_seconds()
    tag = f" [{active['label']}]" if active.get("label") else ""
    print(f"Tracking{tag} for {fmt_duration(elapsed)} (since {started.strftime('%H:%M:%S')})")


USAGE = """tt — minimal time tracker

Commands:
  tt start [label]   Start a session (optional label)
  tt stop            Stop current session
  tt status          Show active session
  tt log             Today's sessions
  tt week            7-day summary
  tt export          CSV to stdout
"""


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(0)
    cmd = args[0]
    if cmd == "start":
        start(" ".join(args[1:]))
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    elif cmd == "log":
        log()
    elif cmd == "week":
        week()
    elif cmd == "export":
        export()
    else:
        print(f"Unknown command: {cmd}\n{USAGE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
