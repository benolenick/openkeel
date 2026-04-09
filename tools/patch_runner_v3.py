#!/usr/bin/env python3
"""Patch runner.py to add v3 strategy evaluation after step 8."""
import re

RUNNER_PATH = "/mnt/nvme/NCMS/ncms/runner.py"

# Read current runner.py
with open(RUNNER_PATH, "r") as f:
    content = f.read()

# Check if already patched
if "v3_strategy" in content:
    print("runner.py already has v3_strategy. Skipping.")
    exit(0)

# Add import
import_line = "from features.strategy_runner import run_all_strategies"
new_import = import_line + "\nfrom features.v3_strategy import evaluate_v3"
content = content.replace(import_line, new_import)

# Add v3 evaluation after step 8b (before the except _InsufficientData)
# Find the marker
marker = """        # =================================================================
        # STEP 8b: Execute trades for qualifying convergence events (live strategy only)
        # =================================================================
        if trading_cfg.get("enabled", False) and events_fired:
            log.info("--- STEP 8b: Trading (live strategy) ---")
            try:
                process_convergence_events(events_fired, trading_cfg)
            except Exception as exc:
                log.warning("Trade execution failed (non-fatal): %s", exc)"""

v3_block = marker + """

        # =================================================================
        # STEP 8c: V3 Strategy Evaluation (backtest-validated rules)
        # =================================================================
        log.info("--- STEP 8c: V3 Strategy (Narrative Analysis) ---")
        v3_signals = []
        try:
            # Load today's units for v3 analysis
            v3_conn = __import__('sqlite3').connect(str(DATA_DIR / "ncms.db"), timeout=30)
            v3_conn.row_factory = __import__('sqlite3').Row
            v3_rows = v3_conn.execute(
                "SELECT unit_id, episode_id, channel_id, parent_group, run_date, seq, text, tickers_json "
                "FROM units WHERE run_date = ?", (run_date,)
            ).fetchall()
            v3_conn.close()
            v3_units = [dict(r) for r in v3_rows]

            if v3_units:
                v3_signals = evaluate_v3(run_date, v3_units)
                for sig in v3_signals:
                    log.info(
                        "  V3 SIGNAL: %s %s conf=%.2f — %s",
                        sig["symbol"], sig["direction"], sig["confidence"],
                        ", ".join(sig.get("reasons", [])),
                    )

                # Store v3 signals in strategy_trades table
                if v3_signals:
                    v3_conn2 = __import__('sqlite3').connect(str(DATA_DIR / "ncms.db"), timeout=30)
                    for sig in v3_signals:
                        try:
                            v3_conn2.execute(
                                "INSERT OR REPLACE INTO strategy_trades "
                                "(run_date, strategy, target_id, direction, coordination_z, "
                                "effective_threshold, osint_boosted, boost_reason, "
                                "stance_mean, stance_agreement, is_live) "
                                "VALUES (?, 'v3_compound', ?, ?, ?, ?, 0, ?, 0, 0, 1)",
                                (run_date, sig["symbol"], sig["direction"],
                                 sig["confidence"], 0.5, ", ".join(sig.get("reasons", []))),
                            )
                        except Exception as e:
                            log.warning("Failed to store v3 signal: %s", e)
                    v3_conn2.commit()
                    v3_conn2.close()
            else:
                log.info("  No units available for v3 evaluation")
        except Exception as exc:
            log.warning("V3 strategy evaluation failed (non-fatal): %s", exc)"""

content = content.replace(marker, v3_block)

# Write back
with open(RUNNER_PATH, "w") as f:
    f.write(content)

print("runner.py patched successfully with v3 strategy.")
print("V3 signals will now be evaluated on every daily run.")
