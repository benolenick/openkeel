#!/usr/bin/env python3
"""Patch runner.py to add autopilot after v3 strategy."""

RUNNER_PATH = "/mnt/nvme/NCMS/ncms/runner.py"

with open(RUNNER_PATH, "r") as f:
    content = f.read()

if "autopilot" in content:
    print("runner.py already has autopilot. Skipping.")
    exit(0)

# Add import
old_import = "from features.v3_strategy import evaluate_v3"
new_import = old_import + "\nfrom features.autopilot import process_signals as autopilot_enter, check_exits as autopilot_exits, daily_summary as autopilot_summary"
content = content.replace(old_import, new_import)

# Add autopilot after v3 signals are stored (find the end of v3 block)
marker = """            else:
                log.info("  No units available for v3 evaluation")
        except Exception as exc:
            log.warning("V3 strategy evaluation failed (non-fatal): %s", exc)"""

autopilot_block = marker + """

        # =================================================================
        # STEP 8d: Autopilot — auto-enter paper trades + check exits
        # =================================================================
        log.info("--- STEP 8d: Autopilot (Paper Trading) ---")
        try:
            # First, check for exits on existing open trades
            closed = autopilot_exits(run_date)
            if closed:
                log.info("  Autopilot closed %d trade(s)", len(closed))

            # Then, enter new trades from v3 signals
            if v3_signals:
                entered = autopilot_enter(run_date, v3_signals)
                if entered:
                    log.info("  Autopilot entered %d new trade(s)", len(entered))

            # Daily summary
            autopilot_summary(run_date)
        except Exception as exc:
            log.warning("Autopilot failed (non-fatal): %s", exc)"""

content = content.replace(marker, autopilot_block)

with open(RUNNER_PATH, "w") as f:
    f.write(content)

print("runner.py patched with autopilot.")
