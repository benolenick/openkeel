#!/usr/bin/env python3
"""
ws_trader.py — Wealthsimple Playwright Trading Bot

Logs into Wealthsimple via browser, handles 2FA with TOTP,
and places fractional share trades.

Usage:
    python3 ws_trader.py login          # Test login only
    python3 ws_trader.py buy XLE 5.00   # Buy $5 of XLE
    python3 ws_trader.py sell XLE 5.00  # Sell $5 of XLE
    python3 ws_trader.py balance        # Check account balance
"""

import logging
import os
import sys
import time

import pyotp
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("ws_trader")

# Credentials
WS_EMAIL = "benjamin.olenick@gmail.com"
WS_PASSWORD = "BenitoPrime123!!"
WS_TOTP_SECRET = "R6M6H2AWY2MNRZ5P6BU2GCR66YDALCBPINOPBLY"

# Browser state persistence
STATE_DIR = os.path.expanduser("~/.ws_trader")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SCREENSHOT_DIR = os.path.join(STATE_DIR, "screenshots")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def _screenshot(page, name):
    """Save a screenshot for debugging."""
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path)
    log.info("Screenshot: %s", path)
    return path


def _wait_and_click(page, selector, timeout=10000):
    """Wait for element and click it."""
    page.wait_for_selector(selector, timeout=timeout)
    page.click(selector)


def _get_totp():
    """Generate current TOTP code."""
    return pyotp.TOTP(WS_TOTP_SECRET).now()


def login(page):
    """Log into Wealthsimple. Returns True if successful."""
    log.info("Navigating to Wealthsimple...")
    page.goto("https://my.wealthsimple.com/app/login", wait_until="networkidle", timeout=30000)
    time.sleep(2)
    _screenshot(page, "01_login_page")

    # Check if already logged in
    if "home" in page.url or "trade" in page.url:
        log.info("Already logged in!")
        return True

    # Enter email
    log.info("Entering email...")
    try:
        email_input = page.wait_for_selector('input[type="email"], input[name="email"], input[autocomplete="username"]', timeout=10000)
        email_input.fill(WS_EMAIL)
        time.sleep(0.5)
        _screenshot(page, "02_email_entered")

        # Click continue/next
        page.keyboard.press("Enter")
        time.sleep(2)
    except PwTimeout:
        log.warning("No email field found — might be a different login flow")
        _screenshot(page, "02_no_email_field")

    # Enter password
    log.info("Entering password...")
    try:
        pw_input = page.wait_for_selector('input[type="password"]', timeout=10000)
        pw_input.fill(WS_PASSWORD)
        time.sleep(0.5)
        _screenshot(page, "03_password_entered")
        page.keyboard.press("Enter")
        time.sleep(3)
    except PwTimeout:
        log.warning("No password field found")
        _screenshot(page, "03_no_password_field")

    _screenshot(page, "04_after_password")

    # Handle 2FA — might ask to choose method first
    log.info("Checking for 2FA...")
    time.sleep(2)
    page_text = page.content().lower()

    # If there's an authenticator app option, select it
    if "authenticator" in page_text:
        log.info("Selecting authenticator method...")
        try:
            # Try clicking the authenticator option
            auth_options = page.query_selector_all('text=Authenticator')
            if auth_options:
                auth_options[0].click()
                time.sleep(1)
            # Click continue if present
            continue_btns = page.query_selector_all('text=Continue')
            if continue_btns:
                continue_btns[0].click()
                time.sleep(2)
        except Exception as e:
            log.warning("Could not select authenticator: %s", e)

    _screenshot(page, "05_2fa_page")

    # Enter TOTP code
    totp_code = _get_totp()
    log.info("Entering TOTP code: %s", totp_code)

    try:
        # Look for code input fields (often 6 separate inputs or one field)
        code_inputs = page.query_selector_all('input[type="tel"], input[type="number"], input[inputmode="numeric"]')
        if len(code_inputs) >= 6:
            # 6 separate digit inputs
            for i, digit in enumerate(totp_code):
                code_inputs[i].fill(digit)
                time.sleep(0.1)
        elif len(code_inputs) >= 1:
            code_inputs[0].fill(totp_code)
        else:
            # Try a regular text input
            code_input = page.query_selector('input[type="text"], input[placeholder*="code"], input[placeholder*="Code"]')
            if code_input:
                code_input.fill(totp_code)
            else:
                # Last resort: type it
                log.info("No input found, typing code directly...")
                page.keyboard.type(totp_code)

        time.sleep(1)
        _screenshot(page, "06_totp_entered")

        # Submit
        page.keyboard.press("Enter")
        time.sleep(5)
    except Exception as e:
        log.error("TOTP entry failed: %s", e)
        _screenshot(page, "06_totp_error")

    _screenshot(page, "07_after_2fa")

    # Check if logged in
    time.sleep(3)
    current_url = page.url
    log.info("Current URL: %s", current_url)

    if "home" in current_url or "trade" in current_url or "app" in current_url:
        log.info("LOGIN SUCCESSFUL")
        _screenshot(page, "08_logged_in")
        return True
    else:
        log.error("LOGIN FAILED — unexpected URL: %s", current_url)
        _screenshot(page, "08_login_failed")
        return False


def buy_stock(page, symbol: str, amount: float):
    """Buy a dollar amount of a stock (fractional shares)."""
    log.info("Buying $%.2f of %s", amount, symbol)

    # Navigate to the stock page
    # Wealthsimple Trade URL format
    page.goto(f"https://my.wealthsimple.com/app/trade/stock/{symbol}", wait_until="networkidle", timeout=20000)
    time.sleep(3)
    _screenshot(page, f"buy_01_{symbol}_page")

    # Click Buy button
    try:
        buy_btn = page.wait_for_selector('button:has-text("Buy")', timeout=10000)
        buy_btn.click()
        time.sleep(2)
        _screenshot(page, f"buy_02_{symbol}_buy_clicked")
    except PwTimeout:
        log.error("No Buy button found")
        _screenshot(page, f"buy_02_{symbol}_no_buy_btn")
        return False

    # Switch to dollar amount if needed (might default to shares)
    try:
        dollar_toggle = page.query_selector('text=Dollars, text=$, button:has-text("$")')
        if dollar_toggle:
            dollar_toggle.click()
            time.sleep(1)
    except Exception:
        pass

    # Enter the amount
    try:
        amount_input = page.wait_for_selector('input[type="number"], input[inputmode="decimal"], input[placeholder*="0"]', timeout=5000)
        amount_input.fill(str(amount))
        time.sleep(1)
        _screenshot(page, f"buy_03_{symbol}_amount_entered")
    except PwTimeout:
        # Try typing into whatever is focused
        log.info("No amount input found, trying keyboard...")
        page.keyboard.type(str(amount))
        time.sleep(1)
        _screenshot(page, f"buy_03_{symbol}_typed_amount")

    # Click Review/Preview order
    try:
        review_btn = page.query_selector('button:has-text("Review"), button:has-text("Preview"), button:has-text("Continue")')
        if review_btn:
            review_btn.click()
            time.sleep(2)
            _screenshot(page, f"buy_04_{symbol}_review")
    except Exception as e:
        log.warning("No review button: %s", e)

    # Confirm the order
    try:
        confirm_btn = page.wait_for_selector('button:has-text("Confirm"), button:has-text("Place order"), button:has-text("Submit")', timeout=10000)
        _screenshot(page, f"buy_05_{symbol}_confirm_page")
        confirm_btn.click()
        time.sleep(3)
        _screenshot(page, f"buy_06_{symbol}_order_placed")
        log.info("ORDER PLACED: BUY $%.2f of %s", amount, symbol)
        return True
    except PwTimeout:
        log.error("No confirm button found")
        _screenshot(page, f"buy_05_{symbol}_no_confirm")
        return False


def sell_stock(page, symbol: str, amount: float):
    """Sell a dollar amount of a stock."""
    log.info("Selling $%.2f of %s", amount, symbol)

    page.goto(f"https://my.wealthsimple.com/app/trade/stock/{symbol}", wait_until="networkidle", timeout=20000)
    time.sleep(3)
    _screenshot(page, f"sell_01_{symbol}_page")

    # Click Sell button
    try:
        sell_btn = page.wait_for_selector('button:has-text("Sell")', timeout=10000)
        sell_btn.click()
        time.sleep(2)
        _screenshot(page, f"sell_02_{symbol}_sell_clicked")
    except PwTimeout:
        log.error("No Sell button found")
        _screenshot(page, f"sell_02_{symbol}_no_sell_btn")
        return False

    # Enter amount
    try:
        amount_input = page.wait_for_selector('input[type="number"], input[inputmode="decimal"], input[placeholder*="0"]', timeout=5000)
        amount_input.fill(str(amount))
        time.sleep(1)
    except PwTimeout:
        page.keyboard.type(str(amount))
        time.sleep(1)

    _screenshot(page, f"sell_03_{symbol}_amount")

    # Review + Confirm
    try:
        review = page.query_selector('button:has-text("Review"), button:has-text("Preview"), button:has-text("Continue")')
        if review:
            review.click()
            time.sleep(2)
    except Exception:
        pass

    try:
        confirm = page.wait_for_selector('button:has-text("Confirm"), button:has-text("Place order"), button:has-text("Submit")', timeout=10000)
        _screenshot(page, f"sell_04_{symbol}_confirm")
        confirm.click()
        time.sleep(3)
        _screenshot(page, f"sell_05_{symbol}_done")
        log.info("ORDER PLACED: SELL $%.2f of %s", amount, symbol)
        return True
    except PwTimeout:
        log.error("No confirm button")
        _screenshot(page, f"sell_04_{symbol}_no_confirm")
        return False


def get_balance(page):
    """Get account balance."""
    page.goto("https://my.wealthsimple.com/app/home", wait_until="networkidle", timeout=20000)
    time.sleep(3)
    _screenshot(page, "balance_page")

    # Try to extract balance text
    text = page.inner_text("body")
    log.info("Page text preview: %s", text[:500])
    return text


def main():
    if len(sys.argv) < 2:
        print("Usage: ws_trader.py [login|buy|sell|balance] [symbol] [amount]")
        sys.exit(1)

    cmd = sys.argv[1]
    headed = "--headless" not in sys.argv

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Use persistent context to keep login state
        context = browser.new_context(
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        try:
            if cmd == "login":
                success = login(page)
                if success:
                    context.storage_state(path=STATE_FILE)
                    log.info("Session saved to %s", STATE_FILE)
                print("Login:", "SUCCESS" if success else "FAILED")

            elif cmd == "buy":
                symbol = sys.argv[2].upper()
                amount = float(sys.argv[3])
                if not login(page):
                    print("Login failed")
                    sys.exit(1)
                context.storage_state(path=STATE_FILE)
                success = buy_stock(page, symbol, amount)
                print("Buy:", "SUCCESS" if success else "FAILED")

            elif cmd == "sell":
                symbol = sys.argv[2].upper()
                amount = float(sys.argv[3])
                if not login(page):
                    print("Login failed")
                    sys.exit(1)
                context.storage_state(path=STATE_FILE)
                success = sell_stock(page, symbol, amount)
                print("Sell:", "SUCCESS" if success else "FAILED")

            elif cmd == "balance":
                if not login(page):
                    print("Login failed")
                    sys.exit(1)
                context.storage_state(path=STATE_FILE)
                text = get_balance(page)
                print(text[:1000])

            else:
                print(f"Unknown command: {cmd}")

        except Exception as e:
            log.error("Error: %s", e)
            _screenshot(page, "error")
            raise
        finally:
            # Keep browser open for headed mode so user can see
            if headed and cmd == "login":
                input("Press Enter to close browser...")
            browser.close()


if __name__ == "__main__":
    main()
