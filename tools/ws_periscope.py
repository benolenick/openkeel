#!/usr/bin/env python3
"""
ws_periscope.py — Screenshot-driven Wealthsimple browser session.
Opens browser, takes screenshots to /tmp/ws_screenshots/ for Claude to read.
"""
import os, sys, time
from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = "/tmp/ws_screenshots"
STATE_FILE = os.path.expanduser("~/.ws_trader/state.json")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

_step = 0

def snap(page, label=""):
    global _step
    _step += 1
    path = os.path.join(SCREENSHOT_DIR, f"{_step:02d}_{label}.png")
    page.screenshot(path=path)
    print(f"Screenshot {_step}: {path}")
    return path

def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://my.wealthsimple.com/app/login"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        print(f"Navigating to {url}...")
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(2)
        snap(page, "initial")

        # Keep browser open, take screenshots on command via stdin
        print("\nBrowser open. Commands:")
        print("  s          — screenshot")
        print("  g URL      — goto URL")
        print("  c SELECTOR — click selector")
        print("  t TEXT      — type text")
        print("  f SELECTOR TEXT — fill input")
        print("  k KEY      — press key (Enter, Tab, etc)")
        print("  save       — save session state")
        print("  q          — quit")

        while True:
            try:
                cmd = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not cmd:
                continue

            try:
                if cmd == "s":
                    snap(page, "manual")
                elif cmd == "q":
                    break
                elif cmd == "save":
                    context.storage_state(path=STATE_FILE)
                    print(f"Saved to {STATE_FILE}")
                elif cmd.startswith("g "):
                    page.goto(cmd[2:], wait_until="networkidle", timeout=20000)
                    time.sleep(2)
                    snap(page, "goto")
                elif cmd.startswith("c "):
                    page.click(cmd[2:])
                    time.sleep(1)
                    snap(page, "click")
                elif cmd.startswith("t "):
                    page.keyboard.type(cmd[2:])
                    time.sleep(0.5)
                    snap(page, "type")
                elif cmd.startswith("f "):
                    parts = cmd[2:].split(" ", 1)
                    page.fill(parts[0], parts[1])
                    time.sleep(0.5)
                    snap(page, "fill")
                elif cmd.startswith("k "):
                    page.keyboard.press(cmd[2:])
                    time.sleep(1)
                    snap(page, "keypress")
                else:
                    print(f"Unknown: {cmd}")
            except Exception as e:
                print(f"Error: {e}")
                snap(page, "error")

        browser.close()

if __name__ == "__main__":
    main()
