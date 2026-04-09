#!/usr/bin/env python3
"""Legacy entry point — forwards to the unified honest monitor.

The old scrolling-meter dashboard read the `savings` table, which stores
v3-era counterfactual "savings" that CLAUDE.md marks as inflated. It has
been replaced by the unified monitor in honest_dashboard.py, which pulls
real billed tokens from the `billed_tokens` table and includes the
scrolling meter (stacked by model) as one of its panels.

This shim exists so anything still launching `python -m openkeel.token_saver.dashboard`
gets the honest monitor instead.
"""
from openkeel.token_saver.honest_dashboard import main

if __name__ == "__main__":
    main()
