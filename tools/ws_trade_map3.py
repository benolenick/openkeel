#!/usr/bin/env python3
"""Map WS trade flow part 3 — login, go to XLE, switch to Market buy, enter $1."""
import os, time, pyotp
from playwright.sync_api import sync_playwright

DIR = "/tmp/ws_screenshots"
STATE = os.path.expanduser("~/.ws_trader/state.json")
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

pw = sync_playwright().start()
browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
ctx = browser.new_context(
    storage_state=STATE if os.path.exists(STATE) else None,
    viewport={"width": 1280, "height": 800},
    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
)
page = ctx.new_page()

# Go home — check if session still valid
print("=> Home", flush=True)
page.goto("https://my.wealthsimple.com/app/home", wait_until="domcontentloaded", timeout=30000)
time.sleep(5)
print(f"   URL: {page.url}", flush=True)

if "login" in page.url:
    print("=> Session expired, logging in...", flush=True)
    snap(page, "login_page")

    # Email
    email_input = page.wait_for_selector('input[name="email"]', timeout=10000)
    email_input.click(force=True)
    time.sleep(0.3)
    page.keyboard.type("benjamin.olenick@gmail.com", delay=30)
    time.sleep(0.5)

    # Password
    page.keyboard.press("Tab")
    time.sleep(0.3)
    page.keyboard.type("BenitoPrime123!!", delay=30)
    time.sleep(0.5)

    # Login
    page.click('button:has-text("Log in")')
    time.sleep(6)
    snap(page, "after_login")

    # 2FA
    time.sleep(2)
    code = pyotp.TOTP("R6M6H2AWY2MNRZ5P6BU2GCR66YDALCBPINOPBLY").now()
    print(f"   TOTP: {code}", flush=True)
    totp_input = page.query_selector('input[type="text"]')
    if totp_input:
        totp_input.click()
        time.sleep(0.3)
        page.keyboard.type(code, delay=100)
        time.sleep(2)
        try:
            page.click('button:has-text("Submit")', timeout=5000)
        except Exception:
            page.keyboard.press("Enter")
        time.sleep(8)

    snap(page, "logged_in")
    ctx.storage_state(path=STATE)
    print(f"   URL: {page.url}", flush=True)

snap(page, "home")

# Go directly to XLE using the security URL we discovered
print("=> XLE stock page", flush=True)
page.goto("https://my.wealthsimple.com/app/security-details/sec-s-d66fe2b7074145c181130a3c0761bcc8",
          wait_until="domcontentloaded", timeout=30000)
time.sleep(5)
snap(page, "xle_page")

# The buy form should be on the right. Click "Limit buy" dropdown to switch to Market
print("=> Switch order type to Market buy", flush=True)
try:
    page.click('button:has-text("Limit buy")', timeout=5000)
    time.sleep(2)
    snap(page, "order_type_dropdown")

    # Dump what appeared in the dropdown
    for i, el in enumerate(page.query_selector_all('[role="option"], [role="menuitem"], li, div[class*="option"]')):
        txt = el.inner_text().strip().replace("\n", " ")[:60]
        if txt:
            print(f"   option[{i}]: {txt}", flush=True)

    # Click Market buy
    try:
        page.click('text=Market buy', timeout=3000)
        time.sleep(2)
        snap(page, "market_buy_selected")
    except Exception:
        # Try other selectors
        market_options = page.query_selector_all('text=Market')
        for opt in market_options:
            txt = opt.inner_text().strip()
            if "market" in txt.lower() and "buy" in txt.lower():
                opt.click()
                time.sleep(2)
                break
        snap(page, "market_buy_attempt")

except Exception as e:
    print(f"   Order type err: {e}", flush=True)
    snap(page, "order_type_error")

# Now scan the form again — should show dollar amount input
print("=> Scan market buy form", flush=True)
snap(page, "market_form")

for i, inp in enumerate(page.query_selector_all("input")):
    attrs = {a: inp.get_attribute(a) for a in ["type","name","placeholder","inputmode","aria-label","value","id"] if inp.get_attribute(a)}
    print(f"   inp[{i}]: {attrs}", flush=True)

for i, b in enumerate(page.query_selector_all("button")):
    txt = b.inner_text().strip().replace("\n", " ")[:60]
    disabled = b.get_attribute("disabled")
    if txt:
        print(f"   btn[{i}]: '{txt}' disabled={disabled}", flush=True)

# Also check for any labels/text that says "Amount" or "Dollars" or "Shares"
labels = page.query_selector_all('label, span, div')
for l in labels:
    txt = l.inner_text().strip()
    if txt in ("Amount", "Dollars", "Shares", "Estimated cost", "Available cash", "USD", "CAD"):
        print(f"   label: '{txt}'", flush=True)

# Try entering $1 in whatever amount field exists
print("=> Enter $1", flush=True)
amt_inputs = page.query_selector_all('input[inputmode="decimal"], input[inputmode="numeric"]')
print(f"   Found {len(amt_inputs)} decimal inputs", flush=True)
for i, inp in enumerate(amt_inputs):
    val = inp.get_attribute("value") or ""
    placeholder = inp.get_attribute("placeholder") or ""
    iid = inp.get_attribute("id") or ""
    print(f"   amt[{i}]: value='{val}' placeholder='{placeholder}' id='{iid}'", flush=True)

if amt_inputs:
    # Click the first empty one
    for inp in amt_inputs:
        val = inp.get_attribute("value") or ""
        if not val or val == "0":
            inp.click(force=True)
            time.sleep(0.3)
            page.keyboard.press("Control+a")
            page.keyboard.type("1", delay=100)
            time.sleep(2)
            snap(page, "dollar_entered")
            break

# Check if Next is now enabled
for b in page.query_selector_all("button"):
    txt = b.inner_text().strip()
    disabled = b.get_attribute("disabled")
    if "next" in txt.lower() or "review" in txt.lower() or "confirm" in txt.lower():
        print(f"   ACTION: '{txt}' disabled={disabled}", flush=True)

snap(page, "ready_state")

# Save session
ctx.storage_state(path=STATE)
print(f"\nFinal URL: {page.url}", flush=True)
print("DONE — open 10 min", flush=True)
time.sleep(600)
browser.close(); pw.stop()
