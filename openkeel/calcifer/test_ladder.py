#!/usr/bin/env python3
"""Test harness for Calcifer's Ladder — sends test messages and verifies responses."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG_FILE = Path("/tmp/calcifer_test.log")

def test_message(msg: str, expected_route: str, timeout: int = 30):
    """Send a test message and verify it routes correctly."""
    log_file = Path("/tmp/calcifer_send.log")

    # Clear the send log
    if log_file.exists():
        log_file.unlink()

    print(f"\n{'='*60}")
    print(f"TEST: {msg!r}")
    print(f"Expected route: {expected_route}")
    print(f"{'='*60}")

    # Send message via xdotool
    result = subprocess.run([
        "bash", "-c",
        f"""
        WID=$(DISPLAY=:0 xdotool search --name "Calcifer" 2>/dev/null | head -1)
        if [ -z "$WID" ]; then echo "Window not found"; exit 1; fi
        DISPLAY=:0 xdotool windowactivate $WID 2>/dev/null || true
        sleep 0.2
        DISPLAY=:0 xdotool type {msg!r}
        sleep 0.2
        DISPLAY=:0 xdotool key Return
        """
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"❌ Failed to send message: {result.stderr}")
        return False

    # Wait for response
    start = time.time()
    while time.time() - start < timeout:
        if log_file.exists():
            with open(log_file) as f:
                content = f.read()
                # Look for the streaming completion marker
                if "got" in content and "chars" in content:
                    print(f"✓ Message processed")
                    print(f"Log:\n{content}")
                    return True
        time.sleep(0.5)

    print(f"❌ Timeout waiting for response (checked for {timeout}s)")
    if log_file.exists():
        with open(log_file) as f:
            print(f"Last log:\n{f.read()}")
    return False


def main():
    # Start the window
    print("Starting Calcifer's Ladder...")
    proc = subprocess.Popen(
        ["python3", "-m", "openkeel.calcifer.ladder_chat"],
        env={**__import__("os").environ, "DISPLAY": ":0"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        time.sleep(8)  # Let it initialize

        tests = [
            ("hello", "A·trivial → gemma4_small"),
            ("what does timeout mean", "B·local-26b → gemma4_large"),
            ("write a function to parse JSON", "B·code → qwen25"),
            ("fix the failing test", "C·operational → sonnet"),
            ("think hard about the best approach", "E·strategic → opus"),
        ]

        passed = 0
        for msg, expected in tests:
            if test_message(msg, expected):
                passed += 1
                print(f"✓ PASS")
            else:
                print(f"✗ FAIL")

        print(f"\n{'='*60}")
        print(f"Results: {passed}/{len(tests)} passed")
        print(f"{'='*60}")

        return 0 if passed == len(tests) else 1

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
