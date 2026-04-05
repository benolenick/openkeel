#!/bin/bash
# Persistent SSH tunnel to jagg's Ollama for the token saver.
#
# Forwards local port 11447 → jagg:11434 (Ollama).
# Uses autossh for automatic reconnection.
#
# Usage:
#   ./scripts/token_saver_tunnel.sh          # foreground
#   ./scripts/token_saver_tunnel.sh --daemon  # background
#
# Stop:
#   kill $(cat ~/.openkeel/token_saver_tunnel.pid)

set -euo pipefail

JAGG_IP="${JAGG_IP:-192.168.0.224}"
LOCAL_PORT="${LOCAL_PORT:-11447}"
REMOTE_PORT="${REMOTE_PORT:-11434}"
PID_FILE="$HOME/.openkeel/token_saver_tunnel.pid"
JAGG_USER="${JAGG_USER:-om}"

mkdir -p "$(dirname "$PID_FILE")"

# Check if tunnel is already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Tunnel already running (PID $OLD_PID)"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# Check if local port is already in use
if ss -tlnp 2>/dev/null | grep -q ":${LOCAL_PORT} "; then
    echo "Port $LOCAL_PORT already in use — tunnel may already be running"
    exit 0
fi

# Check connectivity
if ! ping -c 1 -W 2 "$JAGG_IP" &>/dev/null; then
    echo "ERROR: jagg ($JAGG_IP) not reachable"
    exit 1
fi

SSH_OPTS="-o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=no"

if command -v autossh &>/dev/null; then
    TUNNEL_CMD="autossh -M 0 -N -L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT} ${JAGG_USER}@${JAGG_IP} ${SSH_OPTS}"
else
    echo "Note: autossh not found, using plain ssh (no auto-reconnect)"
    TUNNEL_CMD="ssh -N -L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT} ${JAGG_USER}@${JAGG_IP} ${SSH_OPTS}"
fi

if [ "${1:-}" = "--daemon" ]; then
    $TUNNEL_CMD &
    echo $! > "$PID_FILE"
    echo "Tunnel started in background (PID $!, local:${LOCAL_PORT} → jagg:${REMOTE_PORT})"
else
    echo "Starting tunnel: local:${LOCAL_PORT} → jagg:${REMOTE_PORT}"
    echo "Press Ctrl+C to stop"
    echo $$ > "$PID_FILE"
    trap 'rm -f "$PID_FILE"' EXIT
    exec $TUNNEL_CMD
fi
