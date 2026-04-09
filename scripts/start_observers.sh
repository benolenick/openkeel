#!/bin/bash
# start_observers.sh — Launch the full observer stack on jagg
#
# Usage:
#   ./start_observers.sh <mission_name>
#   ./start_observers.sh pirate-htb
#   ./start_observers.sh pirate-htb --no-oracle
#
# Starts:
#   1. Ollama instance on port 11444 (GPU 1) for Cartographer (qwen3.5)
#   2. Ollama instance on port 11445 (GPU 0) for Pilgrim (qwen3:14b)
#   3. Ollama instance on port 11446 (CPU only) for Oracle (qwen3.5:latest)
#   4. The observer_daemon.py process
#
# GPU allocation:
#   GPU 0 — Hyphae embeddings (~500MB) + Pilgrim qwen3:14b (~9GB) = ~9.5GB / 24GB
#   GPU 1 — Cartographer qwen3.5 (~6GB) = 6GB / 24GB
#   CPU   — Oracle qwen3.5:latest (~4GB RAM)

set -euo pipefail

MISSION="${1:?Usage: $0 <mission_name> [--no-oracle]}"
shift
EXTRA_ARGS="$@"

OPENKEEL_DIR="$HOME/openkeel"
LOG_DIR="/tmp/observers"
mkdir -p "$LOG_DIR"

echo "=== Observer Stack Launcher ==="
echo "Mission: $MISSION"
echo "Logs: $LOG_DIR/"

# --- Kill any existing observer instances ---
echo "[1/5] Cleaning up old instances..."
for port in 11444 11445 11446; do
    pid=$(lsof -ti :$port 2>/dev/null || true)
    if [ -n "$pid" ]; then
        echo "  Killing process on port $port (PID $pid)"
        kill $pid 2>/dev/null || true
        sleep 1
    fi
done

# Kill old observer daemon
if [ -f "$HOME/.openkeel/observer_daemon.pid" ]; then
    old_pid=$(cat "$HOME/.openkeel/observer_daemon.pid")
    if kill -0 "$old_pid" 2>/dev/null; then
        echo "  Killing old observer daemon (PID $old_pid)"
        kill "$old_pid"
        sleep 1
    fi
fi

# --- Start Ollama instances ---
echo "[2/5] Starting Cartographer Ollama (GPU 1, port 11444)..."
CUDA_VISIBLE_DEVICES=1 OLLAMA_HOST=0.0.0.0:11444 \
    nohup ollama serve > "$LOG_DIR/ollama_cart.log" 2>&1 &
CART_PID=$!
echo "  PID: $CART_PID"

echo "[3/5] Starting Pilgrim Ollama (GPU 0, port 11445)..."
CUDA_VISIBLE_DEVICES=0 OLLAMA_HOST=0.0.0.0:11445 \
    nohup ollama serve > "$LOG_DIR/ollama_pilgrim.log" 2>&1 &
PILGRIM_PID=$!
echo "  PID: $PILGRIM_PID"

if [[ ! " $EXTRA_ARGS " =~ " --no-oracle " ]]; then
    echo "[4/5] Starting Oracle Ollama (CPU only, port 11446)..."
    CUDA_VISIBLE_DEVICES="" OLLAMA_HOST=0.0.0.0:11446 \
        nohup ollama serve > "$LOG_DIR/ollama_oracle.log" 2>&1 &
    ORACLE_PID=$!
    echo "  PID: $ORACLE_PID"
else
    echo "[4/5] Oracle disabled (--no-oracle)"
fi

# Wait for Ollama instances to be ready
echo "  Waiting for Ollama instances..."
sleep 3

for port in 11444 11445; do
    for i in $(seq 1 10); do
        if curl -s "http://127.0.0.1:$port/api/tags" > /dev/null 2>&1; then
            echo "  Port $port: ready"
            break
        fi
        sleep 1
    done
done

# Pre-load models (if not already loaded)
echo "  Pre-loading Cartographer model (qwen3.5)..."
OLLAMA_HOST=http://127.0.0.1:11444 ollama run qwen3.5:latest "" --nowordwrap 2>/dev/null &
echo "  Pre-loading Pilgrim model (qwen3:14b)..."
OLLAMA_HOST=http://127.0.0.1:11445 ollama run qwen3:14b "" --nowordwrap 2>/dev/null &
wait

# --- Start Observer Daemon ---
echo "[5/5] Starting Observer Daemon..."
cd "$OPENKEEL_DIR"
PYTHONPATH="$OPENKEEL_DIR" nohup python3 -m openkeel.observer_daemon "$MISSION" $EXTRA_ARGS \
    > "$LOG_DIR/observer_daemon.log" 2>&1 &
DAEMON_PID=$!
echo "  PID: $DAEMON_PID"

sleep 2

# --- Health check ---
echo ""
echo "=== Status ==="
echo "Cartographer (11444): $(curl -s http://127.0.0.1:11444/api/tags 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(", ".join(m["name"] for m in d.get("models",[])) or "no models")' 2>/dev/null || echo 'DOWN')"
echo "Pilgrim (11445): $(curl -s http://127.0.0.1:11445/api/tags 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(", ".join(m["name"] for m in d.get("models",[])) or "no models")' 2>/dev/null || echo 'DOWN')"
if [[ ! " $EXTRA_ARGS " =~ " --no-oracle " ]]; then
    echo "Oracle (11446): $(curl -s http://127.0.0.1:11446/api/tags 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(", ".join(m["name"] for m in d.get("models",[])) or "no models")' 2>/dev/null || echo 'DOWN')"
fi
echo "Observer Daemon: PID $DAEMON_PID (log: $LOG_DIR/observer_daemon.log)"
echo ""
echo "Monitor: tail -f $LOG_DIR/observer_daemon.log"
echo "Stop: kill $DAEMON_PID && kill $CART_PID $PILGRIM_PID ${ORACLE_PID:-}"
