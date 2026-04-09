"""
features/v3_strategy.py — NCMS v3 Strategy: Backtest-Validated Rules

Encodes the 11 strong findings from the 2026-03-30 backtest analysis.
Designed to be called from runner.py alongside existing v1/v2 strategies.

FINDINGS ENCODED:
  #1  XLF structural short — flip bearish on convergence
  #3  Gold caution ratio >40% = rug pull risk
  #5  Require Bloomberg or Reuters presence (institutional confirmation)
  #6  Skip fundamentals-dominated convergence
  #7  Require commodity participation (gold OR energy groups >= 5)
  #9  Inst cautious + retail bullish = best signal; both bullish = exit
  #10 Fed-default regime detection — only trade non-Fed narrative breakouts
  #11 XLE+GLD co-signal = highest confidence

USAGE:
    from features.v3_strategy import evaluate_v3
    signals = evaluate_v3(run_date, units, db_path)
"""

import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("ncms.v3_strategy")

DB_PATH = Path("/mnt/nvme/NCMS/ncms/data/ncms.db")

TRADEABLE_SYMBOLS = ["SPY", "QQQ", "TLT", "XLE", "GLD", "SMH", "XLF"]

# ---------------------------------------------------------------------------
# Keyword sets for narrative classification
# ---------------------------------------------------------------------------

GOLD_KW = {"gold", "precious metal", "bullion", "safe haven", "gld"}
ENERGY_KW = {"oil", "crude", "energy", "opec", "drill", "barrel", "refin", "xle", "pipeline", "natural gas"}
FED_KW = {"fed ", "rate cut", "interest rate", "inflation", "cpi", "fomc", "powell", "dovish", "hawkish", "monetary policy"}
GEO_KW = {"tariff", "trump", "trade war", "china", "war ", "iran", "sanction", "geopolit", "military", "conflict", "venezuela"}
FUND_KW = {"earnings", "dividend", "buyback", "cash flow", "profit", "revenue", "valuation", "pe ratio", "guidance"}
TECH_KW = {"ai ", "nvidia", "artificial intelligence", "semiconductor", "chip", "data center"}

GOLD_CAUTIONARY_KW = {"correction", "overvalued", "bubble", "crash", "too high", "too late", "pullback", "come down", "careful", "overextend", "top out"}
GOLD_EUPHORIC_KW = {"record", "all-time", "unstoppable", "keep going", "bullish", "higher", "rally", "flock", "soar", "10000", "safe haven", "shining"}
HAWKISH_KW = {"inflation", "hawkish", "higher for longer", "no cut", "rate hike", "higher rate"}
DOVISH_KW = {"rate cut", "dovish", "easing", "money printer", "lower rate"}

INSTITUTIONAL_GROUPS = {"bloomberg", "thomson_reuters", "versant_media"}
RETAIL_GROUPS = {"wealthion", "finfluencer", "finfluencer_pro", "crypto_media"}


def _text_matches(text: str, keywords: set) -> int:
    """Count how many keywords appear in text (case-insensitive)."""
    lower = text.lower()
    return sum(1 for kw in keywords if kw in lower)


def _get_conn(db_path: Path = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _classify_units(units: list[dict]) -> dict:
    """Classify all units by topic, group, and sentiment. Returns rich context."""
    ctx = {
        "groups_active": set(),
        "gold_groups": set(),
        "energy_groups": set(),
        "fed_groups": set(),
        "geo_groups": set(),
        "fund_groups": set(),
        "tech_groups": set(),
        # Per-group gold sentiment
        "inst_gold_bullish": 0,
        "inst_gold_cautious": 0,
        "retail_gold_bullish": 0,
        "retail_gold_cautious": 0,
        # Topic unit counts
        "gold_units": 0,
        "energy_units": 0,
        "fed_units": 0,
        "geo_units": 0,
        "fund_units": 0,
        "tech_units": 0,
        "total_units": len(units),
        # Gold caution analysis
        "gold_cautionary_units": 0,
        "gold_euphoric_units": 0,
        "gold_total_units": 0,
        # Bloomberg/Reuters on gold specifically
        "bloomberg_talks_gold": False,
        "reuters_talks_gold": False,
        # Assets that fired in topic convergence
        "converging_assets": set(),
    }

    for u in units:
        text = u.get("text", "")
        group = u.get("parent_group", "unknown")
        ctx["groups_active"].add(group)

        is_gold = _text_matches(text, GOLD_KW) > 0
        is_energy = _text_matches(text, ENERGY_KW) > 0
        is_fed = _text_matches(text, FED_KW) > 0
        is_geo = _text_matches(text, GEO_KW) > 0
        is_fund = _text_matches(text, FUND_KW) > 0
        is_tech = _text_matches(text, TECH_KW) > 0

        if is_gold:
            ctx["gold_groups"].add(group)
            ctx["gold_units"] += 1
            ctx["gold_total_units"] += 1

            # Caution vs euphoria
            if _text_matches(text, GOLD_CAUTIONARY_KW) > 0:
                ctx["gold_cautionary_units"] += 1
            if _text_matches(text, GOLD_EUPHORIC_KW) > 0:
                ctx["gold_euphoric_units"] += 1

            # Institutional vs retail gold sentiment
            if group in INSTITUTIONAL_GROUPS:
                if _text_matches(text, GOLD_EUPHORIC_KW) > 0:
                    ctx["inst_gold_bullish"] += 1
                if _text_matches(text, GOLD_CAUTIONARY_KW) > 0:
                    ctx["inst_gold_cautious"] += 1
                if group == "bloomberg":
                    ctx["bloomberg_talks_gold"] = True
                if group == "thomson_reuters":
                    ctx["reuters_talks_gold"] = True
            elif group in RETAIL_GROUPS:
                if _text_matches(text, GOLD_EUPHORIC_KW) > 0:
                    ctx["retail_gold_bullish"] += 1
                if _text_matches(text, GOLD_CAUTIONARY_KW) > 0:
                    ctx["retail_gold_cautious"] += 1

        if is_energy:
            ctx["energy_groups"].add(group)
            ctx["energy_units"] += 1
        if is_fed:
            ctx["fed_groups"].add(group)
            ctx["fed_units"] += 1
        if is_geo:
            ctx["geo_groups"].add(group)
            ctx["geo_units"] += 1
        if is_fund:
            ctx["fund_groups"].add(group)
            ctx["fund_units"] += 1
        if is_tech:
            ctx["tech_groups"].add(group)
            ctx["tech_units"] += 1

    return ctx


def _check_fed_regime(run_date: str, db_path: Path = None) -> str:
    """
    Check the 3-day rolling dominant topic.
    Returns: 'fed_dominant', 'non_fed_dominant', or 'mixed'
    """
    conn = _get_conn(db_path)
    dt = datetime.strptime(run_date, "%Y-%m-%d")
    dates = [(dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    placeholders = ",".join("?" for _ in dates)

    rows = conn.execute(f"""
        SELECT
            SUM(CASE WHEN text LIKE '%Fed %' OR text LIKE '%rate cut%'
                     OR text LIKE '%inflation%' OR text LIKE '%interest rate%' THEN 1 ELSE 0 END) as fed,
            SUM(CASE WHEN text LIKE '%tariff%' OR text LIKE '%Trump%' OR text LIKE '%war %'
                     OR text LIKE '%China%' OR text LIKE '%geopolit%' OR text LIKE '%sanction%' THEN 1 ELSE 0 END) as geo,
            SUM(CASE WHEN text LIKE '%gold%' THEN 1 ELSE 0 END) as gold,
            SUM(CASE WHEN text LIKE '%oil%' OR text LIKE '%energy%' OR text LIKE '%crude%' THEN 1 ELSE 0 END) as energy
        FROM units WHERE run_date IN ({placeholders})
    """, dates).fetchone()
    conn.close()

    if not rows or not rows["fed"]:
        return "mixed"

    fed = rows["fed"] or 0
    non_fed = (rows["geo"] or 0) + (rows["gold"] or 0) + (rows["energy"] or 0)

    if fed > non_fed * 1.5:
        return "fed_dominant"
    elif non_fed > fed * 1.2:
        return "non_fed_dominant"
    return "mixed"


def evaluate_v3(
    run_date: str,
    units: list[dict],
    db_path: Path = None,
    trade_amount_usd: float = 5000.0,
) -> list[dict]:
    """
    Evaluate v3 strategy signals for a given day.

    Returns list of signal dicts:
        [{symbol, direction, confidence, trigger_type, reasons, skip_reason}]
    """
    if not units:
        return []

    ctx = _classify_units(units)
    signals = []
    skip_reasons = []

    n_groups = len(ctx["groups_active"])
    n_gold = len(ctx["gold_groups"])
    n_energy = len(ctx["energy_groups"])

    # =========================================================================
    # GATE 1: Institutional confirmation (Finding #5)
    # Require Bloomberg, Reuters, or Versant Media (CNBC) active today
    # CNBC included because it was tracked from day 1 (pre-expansion)
    # =========================================================================
    has_institutional = bool(
        ctx["groups_active"] & {"bloomberg", "thomson_reuters", "versant_media"}
    )
    # Boost confidence later if bloomberg/reuters specifically present
    has_top_institutional = bool(
        ctx["groups_active"] & {"bloomberg", "thomson_reuters"}
    )
    if not has_institutional:
        log.info("v3: No institutional media (Bloomberg/Reuters/CNBC). Skipping day.")
        return []

    # =========================================================================
    # GATE 2: Commodity participation (Finding #7)
    # Require gold OR energy groups >= 5
    # =========================================================================
    if n_gold < 5 and n_energy < 5:
        log.info("v3: Low commodity participation (gold=%d, energy=%d). Skipping.", n_gold, n_energy)
        return []

    # =========================================================================
    # GATE 3: Skip fundamentals-dominated convergence (Finding #6)
    # Only skip if fundamentals dominate AND geopolitical is low
    # (post-crash bounces can have high fundamentals but still be valid)
    # =========================================================================
    if (ctx["fund_units"] > ctx["geo_units"] * 1.5
            and ctx["fund_units"] > ctx["fed_units"]
            and ctx["geo_units"] < 10):
        log.info("v3: Fundamentals-dominated convergence (fund=%d, geo=%d). Skipping.",
                 ctx["fund_units"], ctx["geo_units"])
        return []

    # =========================================================================
    # GATE 4: Fed regime check (Finding #10)
    # Only trade when non-Fed narrative is dominant or ascending
    # =========================================================================
    regime = _check_fed_regime(run_date, db_path)
    if regime == "fed_dominant":
        log.info("v3: Fed-dominant regime (3-day rolling). Skipping.")
        return []

    # =========================================================================
    # Passed all gates. Now evaluate individual assets.
    # =========================================================================
    log.info(
        "v3 ACTIVE: groups=%d, gold_grp=%d, energy_grp=%d, regime=%s, "
        "inst=%s, geo=%d, fed=%d, fund=%d",
        n_groups, n_gold, n_energy, regime,
        "yes" if has_institutional else "no",
        ctx["geo_units"], ctx["fed_units"], ctx["fund_units"],
    )

    # Determine which assets to evaluate
    gld_eligible = n_gold >= 4
    xle_eligible = n_energy >= 4
    tlt_eligible = len(ctx["fed_groups"]) >= 4

    # =========================================================================
    # GLD evaluation (Findings #3, #9)
    # =========================================================================
    if gld_eligible:
        gld_confidence = 0.5
        gld_reasons = []
        gld_skip = None

        # Finding #3: Gold caution ratio
        caution_ratio = 0
        if ctx["gold_total_units"] > 0:
            caution_ratio = ctx["gold_cautionary_units"] / ctx["gold_total_units"]

        if caution_ratio > 0.40:
            gld_skip = f"Gold caution ratio {caution_ratio:.0%} > 40% — rug pull risk"
            log.info("v3: %s", gld_skip)
        else:
            if caution_ratio < 0.25:
                gld_confidence += 0.15
                gld_reasons.append(f"clean signal (caution {caution_ratio:.0%})")

            # Finding #9: Institutional vs retail sentiment divergence
            inst_tone = "bullish" if ctx["inst_gold_bullish"] > ctx["inst_gold_cautious"] else (
                "cautious" if ctx["inst_gold_cautious"] > ctx["inst_gold_bullish"] else "neutral")
            retail_tone = "bullish" if ctx["retail_gold_bullish"] > ctx["retail_gold_cautious"] else (
                "cautious" if ctx["retail_gold_cautious"] > ctx["retail_gold_bullish"] else "neutral")

            if inst_tone in ("cautious", "neutral") and retail_tone == "bullish":
                gld_confidence += 0.20
                gld_reasons.append(f"sentiment divergence (inst={inst_tone}, retail=bullish)")
            elif inst_tone == "bullish" and retail_tone == "bullish":
                gld_skip = "Both institutional and retail bullish — potential top"
                log.info("v3: %s", gld_skip)

        if not gld_skip:
            # Finding #11: XLE+GLD co-signal bonus
            if xle_eligible:
                gld_confidence += 0.10
                gld_reasons.append("XLE+GLD co-signal")

            signals.append({
                "symbol": "GLD",
                "direction": "bullish",
                "confidence": min(gld_confidence, 1.0),
                "trigger_type": "v3_compound",
                "reasons": gld_reasons,
                "trade_amount_usd": trade_amount_usd,
            })

    # =========================================================================
    # XLE evaluation (Finding #11)
    # =========================================================================
    if xle_eligible:
        xle_confidence = 0.6  # XLE starts higher — most reliable asset
        xle_reasons = []

        if gld_eligible:
            xle_confidence += 0.15
            xle_reasons.append("XLE+GLD co-signal (100% historical win rate)")

        if n_energy >= 6:
            xle_confidence += 0.10
            xle_reasons.append(f"strong energy convergence ({n_energy} groups)")

        signals.append({
            "symbol": "XLE",
            "direction": "bullish",
            "confidence": min(xle_confidence, 1.0),
            "trigger_type": "v3_compound",
            "reasons": xle_reasons,
            "trade_amount_usd": trade_amount_usd,
        })

    # =========================================================================
    # XLF evaluation (Finding #1) — ALWAYS BEARISH
    # =========================================================================
    if len(ctx["fed_groups"]) >= 5 or n_groups >= 8:
        signals.append({
            "symbol": "XLF",
            "direction": "bearish",
            "confidence": 0.55,
            "trigger_type": "v3_xlf_short",
            "reasons": ["XLF structurally short during convergence"],
            "trade_amount_usd": trade_amount_usd,
        })

    # =========================================================================
    # TLT evaluation (Finding #2) — only on extreme sentiment
    # =========================================================================
    if tlt_eligible:
        hawkish = sum(1 for u in units if _text_matches(u.get("text", ""), HAWKISH_KW) > 0)
        dovish = sum(1 for u in units if _text_matches(u.get("text", ""), DOVISH_KW) > 0)

        tlt_skip = None
        tlt_confidence = 0.45
        tlt_reasons = []

        if hawkish > 0 and dovish > 0:
            ratio = dovish / hawkish
            if hawkish > dovish * 2:
                # Extreme hawkish → contrarian bullish TLT
                tlt_confidence += 0.15
                tlt_reasons.append(f"contrarian: hawkish overwhelms ({hawkish} vs {dovish} dovish)")
            elif dovish >= hawkish:
                tlt_confidence += 0.10
                tlt_reasons.append(f"dovish consensus ({dovish} vs {hawkish} hawkish)")
            else:
                tlt_skip = f"Mixed sentiment (dovish={dovish}, hawkish={hawkish})"

        if not tlt_skip and tlt_reasons:
            signals.append({
                "symbol": "TLT",
                "direction": "bullish",
                "confidence": min(tlt_confidence, 1.0),
                "trigger_type": "v3_tlt_sentiment",
                "reasons": tlt_reasons,
                "trade_amount_usd": trade_amount_usd,
            })

    # =========================================================================
    # Finding #4: Signal density check
    # Prefer 2+ bullish signals. Allow single signal only if high confidence
    # and Bloomberg/Reuters specifically present (not just CNBC).
    # =========================================================================
    bullish_signals = [s for s in signals if s["direction"] == "bullish"]
    if len(bullish_signals) < 2:
        # Allow single high-confidence signal with top institutional backing
        high_conf_single = any(s["confidence"] >= 0.65 for s in bullish_signals)
        if not (high_conf_single and has_top_institutional):
            log.info("v3: Only %d bullish signals, no high-conf exception. Dropping.", len(bullish_signals))
            signals = [s for s in signals if s["symbol"] == "XLF"]
            return signals
        log.info("v3: Single signal but high confidence + institutional backing. Allowing.")

    log.info("v3: %d signals generated for %s", len(signals),
             ", ".join(f"{s['symbol']}({s['direction']},{s['confidence']:.2f})" for s in signals))

    return signals


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    date = sys.argv[1] if len(sys.argv) > 1 else "2026-01-15"

    conn = _get_conn()
    rows = conn.execute(
        "SELECT unit_id, episode_id, channel_id, parent_group, run_date, seq, text, tickers_json "
        "FROM units WHERE run_date = ?", (date,)
    ).fetchall()
    conn.close()

    units = [dict(r) for r in rows]
    print(f"Loaded {len(units)} units for {date}")

    signals = evaluate_v3(date, units)
    print(f"\n{'='*60}")
    print(f"V3 SIGNALS for {date}:")
    for s in signals:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']:.2f} — {', '.join(s['reasons'])}")
    if not signals:
        print("  (no signals)")
