#!/bin/bash
# Start the full token saver stack: tunnel + daemon.
#
# Usage:  ./scripts/token_saver_start.sh
# Stop:   ./scripts/token_saver_stop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPENKEEL_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Token Saver — Starting ==="

# 1. Start SSH tunnel to jagg
echo "[1/2] Starting SSH tunnel to jagg..."
bash "$SCRIPT_DIR/token_saver_tunnel.sh" --daemon 2>/dev/null || true

# Wait for tunnel to establish
sleep 1

# 2. Start the daemon
echo "[2/2] Starting token saver daemon..."
DAEMON_PID_FILE="$HOME/.openkeel/token_saver_daemon.pid"

# Check if daemon already running
if [ -f "$DAEMON_PID_FILE" ]; then
    OLD_PID=$(cat "$DAEMON_PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  Daemon already running (PID $OLD_PID)"
        echo "=== Token Saver — Ready ==="
        exit 0
    fi
fi

cd "$OPENKEEL_DIR"
python3 -m openkeel.token_saver.daemon &
DAEMON_PID=$!
echo "$DAEMON_PID" > "$DAEMON_PID_FILE"

# Wait a moment and check health
sleep 1
if curl -s http://127.0.0.1:11450/health | python3 -m json.tool 2>/dev/null; then
    echo ""
    echo "=== Token Saver — Ready ==="
else
    echo "  Warning: daemon started but health check failed"
    echo "  Ollama may not be reachable — running in cache-only mode"
    echo "=== Token Saver — Ready (degraded) ==="
fi
