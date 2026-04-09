#!/usr/bin/env python3
"""Full WS periscope — login, 2FA, navigate, map buy flow."""
import os, time, pyotp
from playwright.sync_api import sync_playwright

DIR = "/tmp/ws_screenshots"
os.makedirs(DIR, exist_ok=True)
for f in os.listdir(DIR):
    if f.endswith(".png"):
        os.remove(os.path.join(DIR, f))

step = 0

def snap(page, label):
    global step; step += 1
    path = f"{DIR}/{step:02d}_{label}.png"
    page.screenshot(path=path)
    print(f"[{step}] {path}", flush=True)

print("Starting browser...", flush=True)
pw = sync_playwright().start()
browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
ctx = browser.new_context(
    viewport={"width": 1280, "height": 800},
    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
)
page = ctx.new_page()

# STEP 1: Login page
print("=> Login page", flush=True)
page.goto("https://my.wealthsimple.com/app/login", wait_until="domcontentloaded", timeout=30000)
time.sleep(5)
print(f"   URL: {page.url}", flush=True)
snap(page, "login")

# STEP 2: Email — click input directly then type
print("=> Email", flush=True)
email_input = page.wait_for_selector('input[name="email"]', timeout=10000)
email_input.click(force=True)
time.sleep(0.3)
page.keyboard.type("benjamin.olenick@gmail.com", delay=30)
time.sleep(1)
snap(page, "email")

# STEP 3: Password — tab to it and type
print("=> Password", flush=True)
page.keyboard.press("Tab")
time.sleep(0.3)
page.keyboard.type("BenitoPrime123!!", delay=30)
time.sleep(1)
snap(page, "password")

# STEP 4: Click login
print("=> Login click", flush=True)
page.click('button:has-text("Log in")')
time.sleep(6)
snap(page, "after_login")

# STEP 5: 2FA
print("=> 2FA", flush=True)
time.sleep(2)
snap(page, "2fa_page")

# Check remember box
try:
    cb = page.query_selector('input[type="checkbox"]')
    if cb and not cb.is_checked():
        cb.click(force=True)
        print("   Checked remember", flush=True)
except Exception as e:
    print(f"   Checkbox err: {e}", flush=True)

# TOTP
code = pyotp.TOTP("R6M6H2AWY2MNRZ5P6BU2GCR66YDALCBPINOPBLY").now()
print(f"   TOTP: {code}", flush=True)

# Click the input field first, clear it, then type char by char
totp_input = page.query_selector('input[type="text"]')
totp_input.click()
time.sleep(0.3)
page.keyboard.press("Control+a")
page.keyboard.press("Backspace")
time.sleep(0.2)
page.keyboard.type(code, delay=100)
time.sleep(2)
snap(page, "totp_typed")

# Submit — wait for button to be enabled
print("=> Submit 2FA", flush=True)
try:
    page.click('button:has-text("Submit")', timeout=5000)
except Exception:
    # If button is still disabled, try pressing Enter
    print("   Submit disabled, trying Enter...", flush=True)
    page.keyboard.press("Enter")
time.sleep(10)
snap(page, "after_2fa")
print(f"   URL: {page.url}", flush=True)

# Save state
STATE = os.path.expanduser("~/.ws_trader/state.json")
os.makedirs(os.path.dirname(STATE), exist_ok=True)
ctx.storage_state(path=STATE)
print(f"   Session saved: {STATE}", flush=True)

# STEP 6: Go to XLE stock page
print("=> XLE page", flush=True)
page.goto("https://my.wealthsimple.com/app/stock/XLE", wait_until="domcontentloaded", timeout=20000)
time.sleep(4)
snap(page, "xle_page")

# Dump buttons
for i, b in enumerate(page.query_selector_all("button")):
    txt = b.inner_text().strip().replace("\n", " ")[:60]
    if txt:
        print(f"   btn[{i}]: {txt}", flush=True)

# STEP 7: Click Buy
print("=> Buy click", flush=True)
try:
    page.click('button:has-text("Buy")', timeout=10000)
    time.sleep(4)
    snap(page, "buy_form")

    # Dump inputs
    for i, inp in enumerate(page.query_selector_all("input")):
        attrs = {a: inp.get_attribute(a) for a in ["type", "name", "placeholder", "inputmode", "aria-label", "value"] if inp.get_attribute(a)}
        print(f"   inp[{i}]: {attrs}", flush=True)

    # Dump buttons
    for i, b in enumerate(page.query_selector_all("button")):
        txt = b.inner_text().strip().replace("\n", " ")[:60]
        if txt:
            print(f"   btn[{i}]: {txt}", flush=True)

    snap(page, "buy_form_scanned")

    # Try entering $1
    print("=> Enter $1", flush=True)
    # Look for a dollar/amount input
    amt_inputs = page.query_selector_all('input[inputmode="decimal"], input[inputmode="numeric"], input[type="number"]')
    if amt_inputs:
        amt_inputs[0].fill("1")
        time.sleep(1)
        snap(page, "amount_entered")
    else:
        # Try typing into whatever is focused
        page.keyboard.type("1")
        time.sleep(1)
        snap(page, "amount_typed")

    # Look for Review/Continue button
    for i, b in enumerate(page.query_selector_all("button")):
        txt = b.inner_text().strip().replace("\n", " ")[:60]
        if txt:
            print(f"   btn[{i}]: {txt}", flush=True)

    snap(page, "ready_to_review")

except Exception as e:
    print(f"   Buy failed: {e}", flush=True)
    snap(page, "buy_error")

print("\nDONE — browser open for 10 min", flush=True)
time.sleep(600)
browser.close()
pw.stop()
