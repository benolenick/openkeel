#!/usr/bin/env python3
"""
ncms_full_backtest.py — Full historical backtest for NCMS.

Three phases:
  1. Reprocess historical transcript files into units (segment + reduce + ticker enrich)
  2. Backfill market prices for all tradeable symbols (yfinance)
  3. Run the backtester across the full date range

Usage (on jagg):
    python3 ncms_full_backtest.py

This script is self-contained — it reads transcripts from disk, writes units
to the DB, fetches prices, then calls the existing backtester.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ncms.full_backtest")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")
TRANSCRIPTS_DIR = Path("/mnt/nvme/NCMS/ncms/data/transcripts_backfill")

# Channel → parent_group mapping (from episodes table)
CHANNEL_GROUP = {
    "andrei_jikh": "finfluencer",
    "barrons": "news_corp",
    "benjamin_cowen": "crypto_media",
    "bloomberg_tv": "bloomberg",
    "cnbc": "versant_media",
    "cnbc_intl": "versant_media",
    "cnbc_tv": "versant_media",
    "coin_bureau": "crypto_media",
    "dw_news": "euro_media",
    "economist": "economist_group",
    "euronews": "euro_media",
    "everything_money": "finfluencer_pro",
    "financial_times": "nikkei",
    "fox_business": "fox_corp",
    "fox_news": "fox_corp",
    "graham_stephan": "finfluencer",
    "investing_com": "independent",
    "joseph_carlson": "finfluencer",
    "marketwatch": "news_corp",
    "meet_kevin": "finfluencer",
    "motley_fool": "independent",
    "msnow": "versant_media",
    "patrick_boyle": "finfluencer_pro",
    "real_vision": "real_vision",
    "reuters": "thomson_reuters",
    "td_ameritrade": "schwab",
    "wealthion": "wealthion",
    "wsj": "news_corp",
    "yahoo_finance": "apollo",
}

TRADEABLE_SYMBOLS = ["SPY", "QQQ", "TLT", "XLE", "GLD", "SMH", "XLF"]

# Also track these for OSINT proxy mapping
PROXY_MAP = {"RECESSION": "SPY", "FED_POLICY": "TLT", "BTC": "BTC-USD"}

MIN_TRANSCRIPT_CHARS = 500
MIN_UNIT_CHARS = 180
MAX_UNIT_CHARS = 520

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Phase 1: Reprocess historical transcripts → units
# ---------------------------------------------------------------------------

_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
_FIN_KWS = {
    "buy", "sell", "long", "short", "bullish", "bearish", "overweight", "underweight",
    "rate", "rates", "fed", "cpi", "inflation", "jobs", "payrolls", "earnings", "guidance",
    "yield", "yields", "treasury", "oil", "wti", "brent", "gold", "dollar", "usd", "eur",
    "recession", "soft landing", "hard landing", "semiconductor", "tariff", "tariffs",
    "rally", "selloff", "correction", "crash", "bubble", "valuation", "pe ratio",
    "gdp", "pmi", "ism", "fomc", "powell", "dovish", "hawkish",
}

KNOWN_TICKERS = {
    "SPY", "QQQ", "TLT", "XLE", "GLD", "SMH", "XLF", "AAPL", "MSFT", "NVDA",
    "TSLA", "AMZN", "GOOG", "META", "BTC", "ETH", "AMD", "INTC", "BA", "JPM",
    "GS", "WFC", "BAC", "XOM", "CVX", "COP", "DIS", "NFLX", "COST", "WMT",
    "HD", "UNH", "JNJ", "PFE", "MRNA", "LLY", "SQQQ", "SPXS", "VIX", "USO",
}


def reduce_transcript_simple(text: str, max_sents: int = 35) -> tuple[str, dict]:
    """Extractive reduction — simplified version that doesn't need spaCy."""
    sents = re.split(r"(?<=[\.\!\?])\s+", text.strip())
    sents = [s.strip() for s in sents if s.strip()]

    tickers = Counter()
    scored = []
    for idx, s in enumerate(sents):
        low = s.lower()
        kw_hits = sum(1 for w in _FIN_KWS if w in low)
        t_found = _TICKER_RE.findall(s)
        t_real = [t for t in t_found if t in KNOWN_TICKERS]
        tickers.update(t_real)
        score = (kw_hits * 3) + (len(t_real) * 4)
        if score > 0:
            scored.append((score, idx, s))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:max_sents]
    top_idx = sorted(i for _, i, _ in top)
    reduced = "\n".join(sents[i] for i in top_idx) if top_idx else "\n".join(sents[:max_sents])

    return reduced, {"tickers": [t for t, _ in tickers.most_common(15)]}


def split_into_units(text: str) -> list[str]:
    """Split reduced transcript into narrative units."""
    t = re.sub(r"\r\n", "\n", (text or "").strip())
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    paras = [p.strip() for p in t.split("\n\n") if p.strip()]
    units = []
    buf = ""

    def flush():
        nonlocal buf
        b = buf.strip()
        if len(b) >= MIN_UNIT_CHARS:
            units.append(b)
        buf = ""

    for p in paras:
        parts = re.split(r"(?<=[\.\!\?])\s+", p) if len(p) > MAX_UNIT_CHARS * 2 else [p]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if not buf:
                buf = part
            elif len(buf) + 1 + len(part) <= MAX_UNIT_CHARS:
                buf = buf + " " + part
            else:
                flush()
                buf = part
    flush()

    return [re.sub(r"\s+", " ", u).strip() for u in units if len(re.sub(r"\s+", " ", u).strip()) >= MIN_UNIT_CHARS]


def enrich_tickers(unit_text: str) -> list[str]:
    """Extract ticker mentions from a unit."""
    found = _TICKER_RE.findall(unit_text)
    return [t for t in found if t in KNOWN_TICKERS]


def phase1_reprocess_transcripts():
    """Read transcript files, reduce, segment into units, and insert into DB."""
    log.info("=" * 60)
    log.info("PHASE 1: Reprocessing historical transcripts into units")
    log.info("=" * 60)

    conn = get_conn()

    # Check how many units already exist
    existing = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    existing_dates = set()
    if existing > 0:
        rows = conn.execute("SELECT DISTINCT run_date FROM units").fetchall()
        existing_dates = {r[0] for r in rows}
        log.info("Found %d existing units across %d dates", existing, len(existing_dates))

    # Walk transcript directories
    if not TRANSCRIPTS_DIR.exists():
        log.error("Transcripts directory not found: %s", TRANSCRIPTS_DIR)
        return

    date_dirs = sorted(d for d in TRANSCRIPTS_DIR.iterdir() if d.is_dir())
    log.info("Found %d date directories", len(date_dirs))

    total_units = 0
    total_files = 0
    skipped_dates = 0

    for date_dir in date_dirs:
        date_str = date_dir.name  # YYYY-MM-DD format
        # Convert to run_date format
        run_date = date_str

        if run_date in existing_dates:
            skipped_dates += 1
            continue

        day_units = []

        # Each subdir is a channel
        for channel_dir in sorted(date_dir.iterdir()):
            if not channel_dir.is_dir():
                continue

            channel_id = channel_dir.name
            parent_group = CHANNEL_GROUP.get(channel_id, "unknown")

            for txt_file in sorted(channel_dir.glob("*.txt")):
                try:
                    text = txt_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                if len(text) < MIN_TRANSCRIPT_CHARS:
                    continue

                total_files += 1
                episode_id = txt_file.stem  # YouTube video ID

                # Reduce
                reduced, meta = reduce_transcript_simple(text)

                # Segment into units
                raw_units = split_into_units(reduced)

                for seq, unit_text in enumerate(raw_units):
                    tickers = enrich_tickers(unit_text)
                    unit_id = f"{episode_id}:{seq:03d}"
                    day_units.append((
                        unit_id, episode_id, channel_id, parent_group,
                        run_date, seq, unit_text,
                        json.dumps(tickers) if tickers else None,
                    ))

        if day_units:
            conn.executemany(
                """INSERT OR IGNORE INTO units
                   (unit_id, episode_id, channel_id, parent_group,
                    run_date, seq, text, tickers_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                day_units,
            )
            conn.commit()
            total_units += len(day_units)

        if total_files % 200 == 0 and total_files > 0:
            log.info("  Processed %d files → %d units so far...", total_files, total_units)

    log.info("Phase 1 complete: %d files → %d new units (%d dates skipped)",
             total_files, total_units, skipped_dates)

    # Verify
    final_count = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(run_date), MAX(run_date) FROM units"
    ).fetchone()
    log.info("Total units in DB: %d (range: %s to %s)",
             final_count, date_range[0], date_range[1])
    conn.close()


# ---------------------------------------------------------------------------
# Phase 2: Backfill market prices
# ---------------------------------------------------------------------------

def phase2_backfill_prices():
    """Fetch historical daily prices for all tradeable symbols."""
    log.info("=" * 60)
    log.info("PHASE 2: Backfilling market prices")
    log.info("=" * 60)

    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed — run: pip install yfinance")
        return

    conn = get_conn()

    # Get date range from units
    row = conn.execute("SELECT MIN(run_date), MAX(run_date) FROM units").fetchone()
    if not row or not row[0]:
        log.error("No units in DB — run phase 1 first")
        return

    start_date = row[0]
    # End date + buffer for forward returns
    end_dt = datetime.strptime(row[1], "%Y-%m-%d") + timedelta(days=15)
    end_date = end_dt.strftime("%Y-%m-%d")

    symbols = TRADEABLE_SYMBOLS + ["BTC-USD", "^VIX"]

    # Check existing prices
    existing = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    log.info("Existing price rows: %d", existing)

    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start_date, end=end_date)
            if hist.empty:
                log.warning("No data for %s", symbol)
                continue

            rows_added = 0
            for idx, row_data in hist.iterrows():
                price_date = idx.strftime("%Y-%m-%d")
                # Normalize symbol name for DB (^VIX → VIX, BTC-USD → BTC)
                db_symbol = symbol.replace("^", "").replace("-USD", "")
                conn.execute(
                    """INSERT OR REPLACE INTO daily_prices
                       (price_date, symbol, close_price, volume)
                       VALUES (?, ?, ?, ?)""",
                    (price_date, db_symbol, float(row_data["Close"]),
                     float(row_data.get("Volume", 0))),
                )
                rows_added += 1

            conn.commit()
            log.info("  %s: %d price rows (%s to %s)",
                     symbol, rows_added,
                     hist.index[0].strftime("%Y-%m-%d"),
                     hist.index[-1].strftime("%Y-%m-%d"))

        except Exception as e:
            log.warning("Failed to fetch %s: %s", symbol, e)

    final = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    symbols_in_db = conn.execute(
        "SELECT symbol, COUNT(*), MIN(price_date), MAX(price_date) "
        "FROM daily_prices GROUP BY symbol"
    ).fetchall()
    log.info("Total price rows: %d", final)
    for r in symbols_in_db:
        log.info("  %s: %d days (%s → %s)", r[0], r[1], r[2], r[3])

    conn.close()


# ---------------------------------------------------------------------------
# Phase 3: Run backtest
# ---------------------------------------------------------------------------

def phase3_run_backtest():
    """Run the full backtester over all available data."""
    log.info("=" * 60)
    log.info("PHASE 3: Running backtest")
    log.info("=" * 60)

    conn = get_conn()
    row = conn.execute("SELECT MIN(run_date), MAX(run_date) FROM units").fetchone()
    if not row or not row[0]:
        log.error("No units in DB")
        return
    start_date, end_date = row[0], row[1]
    conn.close()

    # Add NCMS project to sys.path so we can import modules
    ncms_root = Path("/mnt/nvme/NCMS/ncms")
    if str(ncms_root) not in sys.path:
        sys.path.insert(0, str(ncms_root))

    try:
        from features.backtester import run_backtest
        results = run_backtest(
            start_date=start_date,
            end_date=end_date,
            holding_days=5,
            trade_amount_usd=5000.0,
            symbols=TRADEABLE_SYMBOLS,
        )
        return results
    except Exception as e:
        log.error("Backtest failed: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Phase 4: Summary report
# ---------------------------------------------------------------------------

def phase4_report(results: dict):
    """Print a detailed summary with trade-by-trade breakdown."""
    if not results:
        log.warning("No results to report")
        return

    print("\n" + "=" * 70)
    print("  NCMS FULL BACKTEST REPORT")
    print(f"  Period: {results['start_date']} to {results['end_date']}")
    print(f"  Backtest ID: {results['backtest_id']}")
    print("=" * 70)

    for strat_name, data in sorted(results["strategies"].items()):
        m = data["metrics"]
        trades = data["trades"]

        print(f"\n{'─' * 60}")
        print(f"  Strategy: {strat_name}")
        print(f"{'─' * 60}")
        print(f"  Trades:      {m['n_trades']}")
        print(f"  Total P&L:   ${m['total_return']:,.2f}")
        print(f"  Sharpe:      {m['sharpe_ratio']:.2f}")
        print(f"  Max DD:      ${m['max_drawdown']:,.2f}")
        print(f"  Win Rate:    {m['win_rate']:.1%}")
        print(f"  Avg Win:     ${m['avg_win']:,.2f}")
        print(f"  Avg Loss:    ${m['avg_loss']:,.2f}")

        if trades:
            print(f"\n  {'Date':<12} {'Symbol':<6} {'Dir':<8} {'Entry':>8} {'Exit':>8} {'P&L':>10} {'Trigger'}")
            print(f"  {'─'*12} {'─'*6} {'─'*8} {'─'*8} {'─'*8} {'─'*10} {'─'*20}")
            for t in trades:
                pnl_str = f"${t['pnl']:,.2f}"
                pnl_color = "+" if t["pnl"] > 0 else ""
                print(f"  {t['trade_date']:<12} {t['symbol']:<6} {t['direction']:<8} "
                      f"${t['entry_price']:>7.2f} ${t['exit_price']:>7.2f} "
                      f"{pnl_color}{pnl_str:>9} {t['trigger_type']}")

    # Overall best
    print(f"\n{'=' * 70}")
    best = max(results["strategies"].items(),
               key=lambda x: x[1]["metrics"]["sharpe_ratio"])
    print(f"  BEST STRATEGY: {best[0]}")
    print(f"  Sharpe: {best[1]['metrics']['sharpe_ratio']:.2f}, "
          f"Return: ${best[1]['metrics']['total_return']:,.2f}, "
          f"Win Rate: {best[1]['metrics']['win_rate']:.1%}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start_time = time.time()

    log.info("NCMS Full Historical Backtest")
    log.info("DB: %s", DB_PATH)
    log.info("Transcripts: %s", TRANSCRIPTS_DIR)

    # Phase 1: Reprocess transcripts
    phase1_reprocess_transcripts()

    # Phase 2: Backfill prices
    phase2_backfill_prices()

    # Phase 3: Run backtest
    results = phase3_run_backtest()

    # Phase 4: Report
    phase4_report(results)

    elapsed = time.time() - start_time
    log.info("Total runtime: %.1f minutes", elapsed / 60)


if __name__ == "__main__":
    main()
