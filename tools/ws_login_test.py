#!/usr/bin/env python3
"""Step-by-step WS login with screenshots at each step."""
import os, time, sys
import pyotp
from playwright.sync_api import sync_playwright

DIR = "/tmp/ws_screenshots"
os.makedirs(DIR, exist_ok=True)
step = 0

def snap(page, label):
    global step
    step += 1
    path = f"{DIR}/{step:02d}_{label}.png"
    page.screenshot(path=path)
    print(f"[{step}] {path}", flush=True)

print("Starting browser...", flush=True)
pw = sync_playwright().start()
browser = pw.chromium.launch(
    headless=False,
    args=["--disable-blink-features=AutomationControlled"]
)
ctx = browser.new_context(
    viewport={"width": 1280, "height": 800},
    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
page = ctx.new_page()

print("Going to login page...", flush=True)
page.goto("https://my.wealthsimple.com/app/login", wait_until="networkidle", timeout=30000)
time.sleep(3)
snap(page, "login_page")

print("Filling email...", flush=True)
page.fill('input[name="email"]', 'benjamin.olenick@gmail.com')
time.sleep(1)
snap(page, "email")

print("Filling password...", flush=True)
page.fill('input[name="password"], input[type="password"]', 'BenitoPrime123!!')
time.sleep(1)
snap(page, "password")

print("Clicking Log in...", flush=True)
page.click('button:has-text("Log in")')
time.sleep(5)
snap(page, "after_login")

print("Waiting for 2FA page...", flush=True)
time.sleep(3)
snap(page, "2fa_check")

# Generate and enter TOTP
code = pyotp.TOTP("R6M6H2AWY2MNRZ5P6BU2GCR66YDALCBPINOPBLY").now()
print(f"TOTP: {code}", flush=True)

# Take screenshot to see what 2FA page looks like
snap(page, "before_totp")

# Try to find and fill code inputs
try:
    inputs = page.query_selector_all('input')
    print(f"Found {len(inputs)} inputs", flush=True)
    for i, inp in enumerate(inputs):
        itype = inp.get_attribute("type") or "?"
        placeholder = inp.get_attribute("placeholder") or ""
        print(f"  input[{i}]: type={itype} placeholder={placeholder}", flush=True)
except Exception as e:
    print(f"Input scan error: {e}", flush=True)

snap(page, "input_scan")

print("Keeping browser open for 5 minutes...", flush=True)
time.sleep(300)
browser.close()
pw.stop()
