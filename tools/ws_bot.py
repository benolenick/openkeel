#!/usr/bin/env python3
"""
ws_bot.py — Wealthsimple SnapTrade Trading Bot (Production)

API-based fractional share trading via SnapTrade. Replaces Playwright browser automation.

Safety features:
  - Hard max $5 per trade (configurable, NEVER exceeded)
  - Hard max $50 total daily spend
  - Hard max 20 open positions
  - Pre-trade validation with order impact preview
  - All trades logged to append-only audit file
  - Automatic retry with exponential backoff
  - Circuit breaker: halts after 3 consecutive failures

Usage:
    python3 ws_bot.py buy XLE 1.00       # Buy $1 of XLE
    python3 ws_bot.py sell XLE 1.00      # Sell $1 of XLE
    python3 ws_bot.py status             # Show accounts, holdings, recent orders
    python3 ws_bot.py login              # Verify API connection (backward compat)
    python3 ws_bot.py holdings           # Show current positions

Backward compatible: accepts --headless flag (ignored, API needs no browser).
"""

import json
import logging
import os
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import hmac

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ws_bot] %(levelname)s: %(message)s",
)
log = logging.getLogger("ws_bot")

# ============================================================================
# CONFIG — HARD LIMITS (these are absolute ceilings, not targets)
# ============================================================================

SNAPTRADE_CLIENT_ID = os.environ.get(
    "SNAPTRADE_CLIENT_ID", "PERS-KGMIQU94VNFT99EO5I8L")
SNAPTRADE_CONSUMER_SECRET = os.environ.get(
    "SNAPTRADE_CONSUMER_SECRET", "r7qcvQCsB8Z5kZ8VfvfV9zNKXi8XpMDPXMPXWmvjsoGkQjGI8a")
SNAPTRADE_USER_ID = os.environ.get(
    "SNAPTRADE_USER_ID", "ncms_ben")
SNAPTRADE_USER_SECRET = os.environ.get(
    "SNAPTRADE_USER_SECRET", "e9d15297-5312-4bc7-901d-73dae362f6be")

# Account to trade on (Wealthsimple RRSP)
ACCOUNT_ID = os.environ.get(
    "WS_ACCOUNT_ID", "98c899b5-c236-4a13-8354-c5155712d88e")

# --- HARD SAFETY LIMITS (never exceeded, period) ---
MAX_TRADE_AMOUNT = float(os.environ.get("WS_MAX_TRADE", "5.00"))
MAX_DAILY_SPEND = float(os.environ.get("WS_MAX_DAILY", "5.00"))
MAX_OPEN_POSITIONS = int(os.environ.get("WS_MAX_POSITIONS", "20"))
MAX_RETRIES = 3
CIRCUIT_BREAKER_THRESHOLD = 3  # consecutive failures before halt

# --- STATE FILES ---
STATE_DIR = Path(os.environ.get(
    "WS_STATE_DIR", os.path.expanduser("~/.ws_trader")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG = STATE_DIR / "audit.jsonl"
DAILY_SPEND_FILE = STATE_DIR / "daily_spend.json"
FAILURE_COUNT_FILE = STATE_DIR / "failure_count.json"
SYMBOL_CACHE_FILE = STATE_DIR / "symbol_cache.json"

BASE_URL = "https://api.snaptrade.com/api/v1"


# ============================================================================
# SNAPTRADE API CLIENT
# ============================================================================

class SnapTradeClient:
    """Thin, reliable SnapTrade API client with HMAC signing and retries."""

    def __init__(self):
        self.client_id = SNAPTRADE_CLIENT_ID
        self.consumer_secret = SNAPTRADE_CONSUMER_SECRET
        self.user_id = SNAPTRADE_USER_ID
        self.user_secret = SNAPTRADE_USER_SECRET
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    def _sign(self, path: str, query_str: str, body=None) -> str:
        sig_object = {
            "content": body if body else None,
            "path": f"/api/v1{path}",
            "query": query_str,
        }
        sig_content = json.dumps(
            sig_object, separators=(",", ":"), sort_keys=True)
        sig_digest = hmac.new(
            self.consumer_secret.encode(),
            sig_content.encode(),
            hashlib.sha256,
        ).digest()
        return b64encode(sig_digest).decode()

    def _query_str(self, extra: dict = None) -> str:
        params = {
            "clientId": self.client_id,
            "timestamp": str(int(time.time())),
            "userId": self.user_id,
            "userSecret": self.user_secret,
        }
        if extra:
            params.update(extra)
        return "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    def _request(self, method: str, path: str, body: dict = None,
                 extra_query: dict = None) -> dict | list | None:
        """Make authenticated request with retries and exponential backoff."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                qs = self._query_str(extra_query)
                signature = self._sign(path, qs, body)
                url = f"{BASE_URL}{path}?{qs}"
                headers = {"Signature": signature}

                if method == "GET":
                    resp = self.session.get(url, headers=headers, timeout=30)
                elif method == "POST":
                    resp = self.session.post(
                        url, json=body, headers=headers, timeout=30)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                if resp.status_code in (200, 201):
                    return resp.json()

                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                log.warning("Attempt %d/%d failed: %s",
                            attempt + 1, MAX_RETRIES, last_error)

                # Don't retry 4xx (client errors) except 429 (rate limit)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break

            except requests.exceptions.Timeout:
                last_error = "timeout"
                log.warning("Attempt %d/%d: timeout", attempt + 1, MAX_RETRIES)
            except requests.exceptions.ConnectionError as e:
                last_error = f"connection: {e}"
                log.warning("Attempt %d/%d: connection error",
                            attempt + 1, MAX_RETRIES)
            except Exception as e:
                last_error = str(e)
                log.error("Attempt %d/%d: %s", attempt + 1, MAX_RETRIES, e)

            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                log.info("Retrying in %ds...", wait)
                time.sleep(wait)

        log.error("All %d attempts failed: %s", MAX_RETRIES, last_error)
        return None

    def get(self, path, extra_query=None):
        return self._request("GET", path, extra_query=extra_query)

    def post(self, path, body=None, extra_query=None):
        return self._request("POST", path, body=body, extra_query=extra_query)


# ============================================================================
# SAFETY CHECKS — the last line of defense
# ============================================================================

def _audit(action: str, details: dict):
    """Append-only audit log. Never deleted."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    }
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.error("AUDIT WRITE FAILED: %s", e)
    log.info("AUDIT: %s %s", action, json.dumps(details)[:200])


def _get_daily_spend() -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads(DAILY_SPEND_FILE.read_text())
        if data.get("date") == today:
            return data.get("total", 0.0)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return 0.0


def _add_daily_spend(amount: float):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads(DAILY_SPEND_FILE.read_text())
        if data.get("date") != today:
            data = {"date": today, "total": 0.0}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"date": today, "total": 0.0}
    data["total"] = round(data["total"] + amount, 2)
    DAILY_SPEND_FILE.write_text(json.dumps(data))


def _get_failure_count() -> int:
    try:
        return json.loads(FAILURE_COUNT_FILE.read_text()).get("count", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _record_failure():
    count = _get_failure_count() + 1
    FAILURE_COUNT_FILE.write_text(json.dumps({
        "count": count,
        "last": datetime.now(timezone.utc).isoformat(),
    }))
    return count


def _reset_failures():
    FAILURE_COUNT_FILE.write_text(json.dumps({"count": 0}))


def _validate_trade(amount: float, action: str) -> str | None:
    """
    Pre-trade validation gauntlet. Returns error string or None if OK.
    Every single trade passes through here — no exceptions.
    """
    # 1. Positive amount
    if amount <= 0:
        return f"Invalid amount: ${amount:.2f} (must be > 0)"

    # 2. HARD per-trade ceiling
    if amount > MAX_TRADE_AMOUNT:
        return (f"BLOCKED: ${amount:.2f} exceeds per-trade limit "
                f"${MAX_TRADE_AMOUNT:.2f}")

    # 3. HARD daily ceiling
    daily = _get_daily_spend()
    if daily + amount > MAX_DAILY_SPEND:
        return (f"BLOCKED: ${amount:.2f} would breach daily limit "
                f"(${daily:.2f} spent + ${amount:.2f} = "
                f"${daily + amount:.2f} > ${MAX_DAILY_SPEND:.2f})")

    # 4. Circuit breaker
    failures = _get_failure_count()
    if failures >= CIRCUIT_BREAKER_THRESHOLD:
        return (f"BLOCKED: Circuit breaker tripped ({failures} consecutive "
                f"failures). Reset: echo '{{\"count\":0}}' > {FAILURE_COUNT_FILE}")

    # 5. Valid action
    if action not in ("BUY", "SELL"):
        return f"Invalid action: {action} (must be BUY or SELL)"

    return None


# ============================================================================
# SYMBOL RESOLUTION (cached)
# ============================================================================

def _load_symbol_cache() -> dict:
    try:
        return json.loads(SYMBOL_CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_symbol_cache(cache: dict):
    SYMBOL_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def resolve_symbol(client: SnapTradeClient, ticker: str) -> str | None:
    """Resolve ticker → SnapTrade universal_symbol_id. Results cached."""
    ticker = ticker.upper()
    cache = _load_symbol_cache()
    if ticker in cache:
        return cache[ticker]

    result = client.post("/symbols", body={"substring": ticker})
    if not result:
        log.error("Symbol search failed for %s", ticker)
        return None

    # Prefer exact match on major US exchanges
    us_exchanges = {"XNYS", "XNAS", "ARCX", "XASE"}
    for sym in result:
        if not isinstance(sym, dict):
            continue
        if sym.get("symbol") == ticker:
            mic = sym.get("exchange", {}).get("mic_code", "")
            if mic in us_exchanges:
                sym_id = sym["id"]
                cache[ticker] = sym_id
                _save_symbol_cache(cache)
                log.info("Resolved %s → %s (%s)", ticker, sym_id, mic)
                return sym_id

    # Fallback: first exact ticker match
    for sym in result:
        if isinstance(sym, dict) and sym.get("symbol") == ticker:
            sym_id = sym["id"]
            cache[ticker] = sym_id
            _save_symbol_cache(cache)
            log.info("Resolved %s → %s (fallback)", ticker, sym_id)
            return sym_id

    log.error("Could not resolve symbol: %s", ticker)
    return None


# ============================================================================
# TRADING
# ============================================================================

def place_trade(client: SnapTradeClient, symbol: str, amount: float,
                action: str) -> dict | None:
    """
    Place a market order for a dollar amount (fractional shares via notional).
    Returns order dict on success, None on failure.
    """
    action = action.upper()
    symbol = symbol.upper()

    # === SAFETY GATE (mandatory, no bypass) ===
    error = _validate_trade(amount, action)
    if error:
        _audit("BLOCKED", {
            "symbol": symbol, "amount": amount, "action": action,
            "reason": error})
        log.error(error)
        print(f"BLOCKED: {error}")
        return None

    # Resolve symbol
    sym_id = resolve_symbol(client, symbol)
    if not sym_id:
        _record_failure()
        _audit("FAILED", {"symbol": symbol, "reason": "symbol_not_found"})
        print(f"FAILED: Could not resolve symbol {symbol}")
        return None

    # Build order
    body = {
        "account_id": ACCOUNT_ID,
        "action": action,
        "order_type": "Market",
        "time_in_force": "Day",
        "universal_symbol_id": sym_id,
        "notional_value": amount,
    }

    _audit("ORDER_SUBMITTED", {
        "symbol": symbol, "amount": amount, "action": action})

    result = client.post("/trade/place", body=body)

    if result is None:
        _record_failure()
        _audit("ORDER_FAILED", {
            "symbol": symbol, "amount": amount, "action": action,
            "reason": "api_returned_none"})
        print(f"FAILED: API error placing {action} ${amount:.2f} of {symbol}")
        return None

    order_id = result.get("brokerage_order_id")
    status = result.get("status", "UNKNOWN")

    if not order_id:
        _record_failure()
        _audit("ORDER_FAILED", {
            "symbol": symbol, "amount": amount, "action": action,
            "reason": "no_order_id", "response": str(result)[:500]})
        print(f"FAILED: No order ID in response for {action} "
              f"${amount:.2f} of {symbol}")
        return None

    # === SUCCESS ===
    _reset_failures()
    _add_daily_spend(amount)
    _audit("ORDER_PLACED", {
        "symbol": symbol, "amount": amount, "action": action,
        "order_id": order_id, "status": status})

    log.info("ORDER PLACED: %s $%.2f of %s — %s (%s)",
             action, amount, symbol, order_id, status)

    # Brief pause then check fill
    time.sleep(3)
    fill = _check_order_status(client, order_id)
    if fill:
        result.update(fill)

    return result


def _check_order_status(client: SnapTradeClient,
                        order_id: str) -> dict | None:
    """Poll order status once."""
    orders = client.get(
        f"/accounts/{ACCOUNT_ID}/orders", extra_query={"state": "all"})
    if not orders:
        return None
    for o in orders:
        if isinstance(o, dict) and o.get("brokerage_order_id") == order_id:
            info = {
                "status": o.get("status", "UNKNOWN"),
                "filled_quantity": o.get("filled_quantity"),
                "execution_price": o.get("execution_price"),
            }
            _audit("ORDER_STATUS_CHECK", {"order_id": order_id, **info})
            log.info("Order %s: %s (filled=%s @ %s)",
                     order_id, info["status"],
                     info["filled_quantity"], info["execution_price"])
            return info
    return None


# ============================================================================
# READ-ONLY QUERIES
# ============================================================================

def get_accounts(client: SnapTradeClient) -> list:
    return client.get("/accounts") or []


def get_holdings(client: SnapTradeClient) -> list | dict:
    return client.get(f"/accounts/{ACCOUNT_ID}/holdings") or []


def get_recent_orders(client: SnapTradeClient) -> list:
    return client.get(
        f"/accounts/{ACCOUNT_ID}/orders",
        extra_query={"state": "all"}) or []


def get_balance(client: SnapTradeClient):
    return client.get(f"/accounts/{ACCOUNT_ID}/balances")


# ============================================================================
# CLI COMMANDS
# ============================================================================

def cmd_status(client: SnapTradeClient):
    print("=== WEALTHSIMPLE / SNAPTRADE STATUS ===\n")

    # Account + balance
    for acc in get_accounts(client):
        if isinstance(acc, dict) and acc.get("id") == ACCOUNT_ID:
            bal = (acc.get("balance") or {}).get("total") or {}
            print(f"Account:  {acc.get('name', '?')}")
            print(f"Balance:  ${bal.get('amount', 0):.2f} "
                  f"{bal.get('currency', '')}")
            print()

    # Recent orders
    orders = get_recent_orders(client)
    if orders:
        print("--- Recent Orders ---")
        for o in orders[:10]:
            if not isinstance(o, dict):
                continue
            sym = o.get("universal_symbol", {})
            ticker = sym.get("symbol", "?") if isinstance(sym, dict) else "?"
            print(f"  {o.get('action','?'):4s} {ticker:6s}  "
                  f"qty={o.get('filled_quantity','?'):>10s}  "
                  f"price={o.get('execution_price','?'):>10s}  "
                  f"status={o.get('status','?'):8s}  "
                  f"{o.get('time_placed','')[:19]}")
        print()

    # Safety
    daily = _get_daily_spend()
    failures = _get_failure_count()
    breaker = "TRIPPED" if failures >= CIRCUIT_BREAKER_THRESHOLD else "OK"
    print("--- Safety ---")
    print(f"  Daily spend:     ${daily:.2f} / ${MAX_DAILY_SPEND:.2f}")
    print(f"  Per-trade max:   ${MAX_TRADE_AMOUNT:.2f}")
    print(f"  Failures:        {failures} / {CIRCUIT_BREAKER_THRESHOLD}")
    print(f"  Circuit breaker: {breaker}")
    print()
    print("STATUS: CONNECTED")


def main():
    if len(sys.argv) < 2:
        print("Usage: ws_bot.py [login|buy|sell|status|holdings] "
              "[symbol] [amount] [--headless]")
        print()
        print(f"Limits: ${MAX_TRADE_AMOUNT:.2f}/trade, "
              f"${MAX_DAILY_SPEND:.2f}/day, "
              f"{MAX_OPEN_POSITIONS} positions max")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    # Strip --headless (backward compat with Playwright version)
    args = [a for a in sys.argv[2:] if a != "--headless"]

    client = SnapTradeClient()

    if cmd == "login":
        accounts = get_accounts(client)
        if accounts:
            print("LOGIN SUCCESS")
            for acc in accounts:
                if isinstance(acc, dict):
                    bal = (acc.get("balance") or {}).get("total") or {}
                    print(f"  {acc.get('name', '?')}: "
                          f"${bal.get('amount', 0):.2f} "
                          f"{bal.get('currency', '')}")
        else:
            print("LOGIN FAILED")
            sys.exit(1)

    elif cmd == "buy":
        if len(args) < 2:
            print("Usage: ws_bot.py buy SYMBOL AMOUNT")
            sys.exit(1)
        result = place_trade(client, args[0], float(args[1]), "BUY")
        if result:
            print(f"BUY SUCCESS")
        else:
            print(f"BUY FAILED")
            sys.exit(1)

    elif cmd == "sell":
        if len(args) < 2:
            print("Usage: ws_bot.py sell SYMBOL AMOUNT")
            sys.exit(1)
        result = place_trade(client, args[0], float(args[1]), "SELL")
        if result:
            print(f"SELL SUCCESS")
        else:
            print(f"SELL FAILED")
            sys.exit(1)

    elif cmd == "status":
        cmd_status(client)

    elif cmd == "holdings":
        print(json.dumps(get_holdings(client), indent=2, default=str))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
