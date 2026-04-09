#!/usr/bin/env python3
"""Map WS trade flow part 2 — search XLE, click it, map buy form."""
import os, time
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

# Home
print("=> Home", flush=True)
page.goto("https://my.wealthsimple.com/app/home", wait_until="domcontentloaded", timeout=30000)
time.sleep(5)
snap(page, "home")

if "login" in page.url:
    print("Session expired!", flush=True)
    browser.close(); pw.stop(); exit(1)

# Search
print("=> Search XLE", flush=True)
search_btn = page.query_selector('button[aria-label*="earch"], a[aria-label*="earch"]')
if search_btn:
    search_btn.click()
else:
    # Click the magnifying glass icon (second icon in sidebar)
    icons = page.query_selector_all('nav button, aside button, aside a')
    for ic in icons:
        aria = ic.get_attribute("aria-label") or ""
        if "search" in aria.lower():
            ic.click()
            break
    else:
        # Fallback: click the search looking element
        page.click('svg >> nth=1')  # second svg icon
time.sleep(2)
snap(page, "search_open")

# Type XLE
search_input = page.query_selector('input[type="search"], input[placeholder*="Search"], input[placeholder*="search"]')
if search_input:
    search_input.click(force=True)
    time.sleep(0.3)
page.keyboard.type("XLE", delay=80)
time.sleep(3)
snap(page, "xle_results")

# Click first XLE result
print("=> Click XLE result", flush=True)
try:
    # Find the link/button with XLE text
    xle_result = page.query_selector('text=SSgA Energy Select Sector SPDR')
    if not xle_result:
        xle_result = page.query_selector('text=XLE')
    if xle_result:
        xle_result.click()
        time.sleep(5)
        print(f"   URL: {page.url}", flush=True)
        snap(page, "xle_stock_page")
    else:
        print("   Could not find XLE in results", flush=True)
        snap(page, "no_xle")
except Exception as e:
    print(f"   Click err: {e}", flush=True)
    snap(page, "click_err")

# Now on stock page — dump buttons
print("=> Stock page buttons", flush=True)
for i, b in enumerate(page.query_selector_all("button")):
    txt = b.inner_text().strip().replace("\n", " ")[:60]
    if txt:
        print(f"   btn[{i}]: {txt}", flush=True)
snap(page, "stock_buttons")

# Click Buy
print("=> Click Buy", flush=True)
try:
    page.click('button:has-text("Buy")', timeout=10000)
    time.sleep(4)
    snap(page, "buy_form")

    # Dump ALL form elements
    print("   --- INPUTS ---", flush=True)
    for i, inp in enumerate(page.query_selector_all("input, select, textarea")):
        attrs = {a: inp.get_attribute(a) for a in ["type","name","placeholder","inputmode","aria-label","value","id","role"] if inp.get_attribute(a)}
        print(f"   inp[{i}]: {attrs}", flush=True)

    print("   --- BUTTONS ---", flush=True)
    for i, b in enumerate(page.query_selector_all("button")):
        txt = b.inner_text().strip().replace("\n", " ")[:80]
        disabled = b.get_attribute("disabled")
        if txt:
            print(f"   btn[{i}]: '{txt}' disabled={disabled}", flush=True)

    print("   --- LINKS ---", flush=True)
    for i, a in enumerate(page.query_selector_all("a")):
        txt = a.inner_text().strip()[:60]
        if txt and ("dollar" in txt.lower() or "share" in txt.lower() or "amount" in txt.lower()):
            print(f"   link[{i}]: '{txt}'", flush=True)

    snap(page, "buy_scanned")

    # Try to switch to dollars mode if not already
    print("=> Switch to dollars", flush=True)
    try:
        dollar_btn = page.query_selector('button:has-text("$"), button:has-text("Dollars"), text=Dollars')
        if dollar_btn:
            dollar_btn.click()
            time.sleep(1)
            snap(page, "dollars_mode")
    except Exception:
        pass

    # Enter $1
    print("=> Enter $1", flush=True)
    page.keyboard.type("1")
    time.sleep(2)
    snap(page, "one_dollar")

    # Look for review/confirm
    print("   --- POST-AMOUNT BUTTONS ---", flush=True)
    for i, b in enumerate(page.query_selector_all("button")):
        txt = b.inner_text().strip().replace("\n", " ")[:80]
        disabled = b.get_attribute("disabled")
        if txt:
            print(f"   btn[{i}]: '{txt}' disabled={disabled}", flush=True)

    snap(page, "post_amount")

except Exception as e:
    print(f"   Buy error: {e}", flush=True)
    snap(page, "buy_error")

print(f"\nFinal URL: {page.url}", flush=True)
print("DONE — open 10 min", flush=True)
time.sleep(600)
browser.close(); pw.stop()
