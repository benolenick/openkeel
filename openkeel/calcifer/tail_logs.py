#!/usr/bin/env python3
"""Quick tail of Calcifer logs for debugging."""

import sys
from pathlib import Path
import subprocess

LOG_DIR = Path("/tmp/calcifer_logs")

def main():
    """Show the most recent log."""
    if not LOG_DIR.exists():
        print(f"No logs yet. Log dir: {LOG_DIR}")
        return

    # Find the most recent ladder log
    logs = sorted(LOG_DIR.glob("ladder_*.log"), reverse=True)
    if not logs:
        print("No ladder logs found")
        return

    latest = logs[0]
    print(f"Tailing {latest.name}...\n")

    # Tail the file
    subprocess.run(["tail", "-f", str(latest)])

if __name__ == "__main__":
    main()
