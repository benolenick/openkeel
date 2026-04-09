#!/usr/bin/env python3
"""Map the WS trade flow using saved session — search for stock instead of direct URL."""
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

# Go to home first
print("=> Home", flush=True)
page.goto("https://my.wealthsimple.com/app/home", wait_until="domcontentloaded", timeout=30000)
time.sleep(5)
print(f"   URL: {page.url}", flush=True)
snap(page, "home")

# Check if we need to login again
if "login" in page.url:
    print("   Session expired — need to login again", flush=True)
    snap(page, "need_login")
    browser.close(); pw.stop()
    exit(1)

# Look for search icon/button in the sidebar
print("=> Looking for search...", flush=True)
# The sidebar has icons — search is usually the magnifying glass
# Let's try clicking the search icon (usually first or second in sidebar)
try:
    # Try the search shortcut — many trading apps use Ctrl+K or /
    page.keyboard.press("Control+k")
    time.sleep(2)
    snap(page, "search_ctrlk")
except Exception:
    pass

# Check if a search overlay appeared
page_text = page.inner_text("body")[:200]
print(f"   Page text: {page_text[:100]}", flush=True)
snap(page, "after_search_attempt")

# Try clicking the search icon in sidebar
try:
    search_icon = page.query_selector('a[href*="search"], button[aria-label*="Search"], button[aria-label*="search"]')
    if search_icon:
        search_icon.click()
        time.sleep(2)
        snap(page, "search_clicked")
    else:
        # Try the second icon in the sidebar (first is home)
        sidebar_links = page.query_selector_all('nav a, aside a')
        print(f"   Found {len(sidebar_links)} nav links", flush=True)
        for i, link in enumerate(sidebar_links):
            href = link.get_attribute("href") or ""
            aria = link.get_attribute("aria-label") or ""
            print(f"   nav[{i}]: href={href} aria={aria}", flush=True)

        # Also try all links
        all_links = page.query_selector_all('a')
        for link in all_links:
            href = link.get_attribute("href") or ""
            if "search" in href.lower() or "explore" in href.lower() or "trade" in href.lower():
                print(f"   FOUND: {href}", flush=True)
                link.click()
                time.sleep(2)
                snap(page, "search_found")
                break
except Exception as e:
    print(f"   Search err: {e}", flush=True)

snap(page, "search_state")

# Try typing XLE in whatever search is open
print("=> Typing XLE", flush=True)
try:
    search_input = page.query_selector('input[type="search"], input[placeholder*="Search"], input[placeholder*="search"], input[aria-label*="Search"]')
    if search_input:
        search_input.click(force=True)
        time.sleep(0.3)
        page.keyboard.type("XLE", delay=100)
        time.sleep(3)
        snap(page, "xle_search_results")

        # Look for XLE in results
        results = page.query_selector_all('a, button, div[role="option"], li')
        for r in results:
            txt = r.inner_text().strip()[:80]
            if "XLE" in txt.upper():
                print(f"   RESULT: {txt}", flush=True)
    else:
        print("   No search input found", flush=True)
        # Just type and see
        page.keyboard.type("XLE", delay=100)
        time.sleep(3)
        snap(page, "xle_typed")
except Exception as e:
    print(f"   Type err: {e}", flush=True)

snap(page, "final")

# Dump full URL for reference
print(f"\nFinal URL: {page.url}", flush=True)

print("\nDONE — open 10 min", flush=True)
time.sleep(600)
browser.close()
pw.stop()
