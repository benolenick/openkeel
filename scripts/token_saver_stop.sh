#!/bin/bash
# Stop the token saver stack: daemon + tunnel.

set -euo pipefail

echo "=== Token Saver — Stopping ==="

# Stop daemon
DAEMON_PID_FILE="$HOME/.openkeel/token_saver_daemon.pid"
if [ -f "$DAEMON_PID_FILE" ]; then
    PID=$(cat "$DAEMON_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "  Daemon stopped (PID $PID)"
    fi
    rm -f "$DAEMON_PID_FILE"
else
    echo "  Daemon not running"
fi

# Stop tunnel
TUNNEL_PID_FILE="$HOME/.openkeel/token_saver_tunnel.pid"
if [ -f "$TUNNEL_PID_FILE" ]; then
    PID=$(cat "$TUNNEL_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "  Tunnel stopped (PID $PID)"
    fi
    rm -f "$TUNNEL_PID_FILE"
else
    echo "  Tunnel not running"
fi

echo "=== Token Saver — Stopped ==="
