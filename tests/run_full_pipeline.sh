#!/bin/bash
# Full pipeline: wait for A/B test → run 3-round Delphi → harvest
set -e

REPO=~/Desktop/openkeel2
RESULTS=$REPO/tests/ab_results_v2.json
REPORT=$REPO/tests/ab_report_v2.md
DELPHI_OUT=$REPO/tests/delphi_v2_report.md

echo "=== Waiting for A/B test to complete ==="
while pgrep -f ab_full_battery > /dev/null 2>&1; do
    LAST=$(tail -1 /tmp/ab_full_battery.log 2>/dev/null || echo "waiting...")
    echo "  $(date +%H:%M:%S) — $LAST"
    sleep 30
done

echo ""
echo "=== A/B test complete ==="
echo ""

if [ ! -f "$RESULTS" ]; then
    echo "ERROR: $RESULTS not found"
    exit 1
fi

if [ ! -f "$REPORT" ]; then
    echo "ERROR: $REPORT not found"
    exit 1
fi

echo "Results: $RESULTS"
echo "Report: $REPORT"
echo ""

echo "=== Starting 3-round cross-pollinated Delphi ==="
cd $REPO
PYTHONPATH=src python3 tests/delphi_cross_pollinate.py \
    --results "$RESULTS" \
    --report "$REPORT" \
    --output "$DELPHI_OUT"

echo ""
echo "=== PIPELINE COMPLETE ==="
echo "A/B Report: $REPORT"
echo "Delphi Report: $DELPHI_OUT"
echo "Raw Delphi Data: ${DELPHI_OUT%.md}.json"
