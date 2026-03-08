#!/bin/bash
# ARCHIVE: This script was written for macOS + MoltenVK. It uses
# VK_ICD_FILENAMES pointing to MoltenVK_icd.json and references
# /Volumes/Vault paths. On Ubuntu, servers are managed via systemd
# (gemma0.service, gemma1.service) and the NAS is at /mnt/vault/.
# See SERVERS.md for current operations.

# Multi-GPU Indexer Startup Script
# Launches 2 independent Gemma 3 12B instances for parallel media indexing
# Each GPU runs its own llama-server with the full model
#
# GPU0 (RX 580)     → port 8090
# GPU1 (RX 580)     → port 8091
# GPU2 (Pro 580X)   → kept free for interactive LLM use (start-llm-server.sh)
#
# Usage:
#   ./start-indexer-gpus.sh          # Start both GPU servers
#   ./start-indexer-gpus.sh stop     # Stop both GPU servers
#   ./start-indexer-gpus.sh status   # Check status of both

set -e

# Configuration
MODEL_DIR="$HOME/models"
LLAMA_DIR="$HOME/llama.cpp"
MODEL="$MODEL_DIR/gemma-3-12b-it-Q3_K_S.gguf"
MMPROJ="$MODEL_DIR/mmproj-gemma-3-12b-it-f16.gguf"
LOG_DIR="$HOME/media-index"
HOST="0.0.0.0"

# Vulkan setup (MoltenVK on macOS)
export VK_ICD_FILENAMES="/usr/local/etc/vulkan/icd.d/MoltenVK_icd.json"

# GPU → Port mapping
GPUS=("Vulkan0" "Vulkan1")
PORTS=("8090" "8091")

case "${1:-start}" in
    stop)
        for i in 0 1; do
            PID=$(lsof -ti:${PORTS[$i]} 2>/dev/null || true)
            if [ -n "$PID" ]; then
                kill $PID 2>/dev/null
                echo "Stopped GPU$i server on port ${PORTS[$i]} (PID $PID)"
            else
                echo "GPU$i server on port ${PORTS[$i]} was not running"
            fi
        done
        exit 0
        ;;
    status)
        for i in 0 1; do
            PORT=${PORTS[$i]}
            PID=$(lsof -ti:$PORT 2>/dev/null || true)
            if [ -n "$PID" ]; then
                HEALTH=$(curl -s "http://localhost:$PORT/health" 2>/dev/null || echo '{"status":"unreachable"}')
                echo "GPU$i (${GPUS[$i]}) port $PORT: RUNNING (PID $PID) — $HEALTH"
            else
                echo "GPU$i (${GPUS[$i]}) port $PORT: NOT RUNNING"
            fi
        done
        exit 0
        ;;
    start)
        ;;
    *)
        echo "Usage: $0 [start|stop|status]"
        exit 1
        ;;
esac

# Verify files exist
if [ ! -f "$MODEL" ]; then
    echo "ERROR: Model not found: $MODEL"
    echo ""
    echo "Download with:"
    echo "  cd ~/models"
    echo "  curl -L -O 'https://huggingface.co/bartowski/google_gemma-3-12b-it-GGUF/resolve/main/gemma-3-12b-it-Q3_K_S.gguf'"
    exit 1
fi

if [ ! -f "$MMPROJ" ]; then
    echo "ERROR: Vision encoder not found: $MMPROJ"
    exit 1
fi

# Check if any ports are already in use
for i in 0 1; do
    PID=$(lsof -ti:${PORTS[$i]} 2>/dev/null || true)
    if [ -n "$PID" ]; then
        echo "ERROR: Port ${PORTS[$i]} already in use (PID $PID). Run '$0 stop' first."
        exit 1
    fi
done

echo ""
echo "========================================="
echo "  Multi-GPU Indexer (2x Gemma 3 12B)"
echo "========================================="
echo "  Model:  $(basename "$MODEL")"
echo "  Vision: $(basename "$MMPROJ")"
echo ""

mkdir -p "$LOG_DIR"

# IMPORTANT: Launch GPUs one at a time and wait for each to finish loading.
# Launching all 3 simultaneously overwhelms the AMD GPU driver and causes
# a WindowServer crash (GPU driver deadlock → watchdog kill → kernel panic).

TIMEOUT=90
ALL_READY=true

for i in 0 1; do
    GPU=${GPUS[$i]}
    PORT=${PORTS[$i]}
    LOG="$LOG_DIR/gpu${i}-server.log"

    echo "  Starting GPU$i ($GPU) on port $PORT..."

    nohup "$LLAMA_DIR/build/bin/llama-server" \
        --host "$HOST" \
        --port "$PORT" \
        -m "$MODEL" \
        --mmproj "$MMPROJ" \
        --device "$GPU" \
        -ngl 99 \
        --ctx-size 2048 \
        --parallel 1 \
        > "$LOG" 2>&1 &

    # Wait for THIS server to be ready before starting the next one
    ELAPSED=0
    READY=false
    while [ $ELAPSED -lt $TIMEOUT ]; do
        if curl -s "http://localhost:$PORT/health" 2>/dev/null | grep -q '"status"'; then
            echo "  GPU$i (port $PORT): Ready"
            READY=true
            break
        fi
        sleep 2
        ELAPSED=$((ELAPSED + 2))
    done
    if [ "$READY" = false ]; then
        echo "  GPU$i (port $PORT): TIMEOUT after ${TIMEOUT}s — check $LOG_DIR/gpu${i}-server.log"
        ALL_READY=false
    fi
done

echo ""
if [ "$ALL_READY" = true ]; then
    echo "Both GPUs ready. Start indexing with:"
    echo "  python3 ~/media-indexer.py index '/Volumes/Vault/Videos Vault/2024'"
else
    echo "WARNING: Not all servers started. Check logs in $LOG_DIR/"
fi
