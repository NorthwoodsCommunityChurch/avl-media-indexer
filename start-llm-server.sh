#!/bin/bash
# ARCHIVE: This script was written for macOS + MoltenVK. It uses
# VK_ICD_FILENAMES pointing to MoltenVK_icd.json and Vulkan device names
# like "Vulkan2" that only work on macOS. On Ubuntu, servers are managed
# via systemd (gemma0.service, gemma1.service, whisper.service).
# See SERVERS.md for current operations.

# LLM Server Startup Script (Single Server Mode)
# Default: runs on GPU2 (Pro 580X, 8GB) only — GPU0+GPU1 reserved for indexer.
# For standalone use without indexer: change DEVICE to Vulkan0,Vulkan1,Vulkan2
#
# NOTE: With only GPU2 (8GB VRAM), only models ≤8GB fit fully on GPU.
#   - Gemma 3 12B Q3_K_S (~6.9 GB) ✓ recommended for interactive+indexer together
#   - Gemma 3 12B Q4_K_M (~8 GB)   ✓ tight but usually fits
#   - Qwen3-14B Q4_K_M (~8.4 GB)   ✗ slightly overflows, spills to CPU (slower)
#   - Qwen3-VL-32B                  ✗ way too big for single GPU
#
# Usage:
#   ./start-llm-server.sh          # Default: Gemma 3 12B Q3_K_S (fits in 8GB)
#   ./start-llm-server.sh gemma    # Gemma 3 12B Q4_K_M (vision, ~13 t/s on 3 GPUs)
#   ./start-llm-server.sh 14b      # Qwen3-14B Q4_K_M (~14 t/s on 3 GPUs)
#   ./start-llm-server.sh 32b      # Qwen3-VL-32B Q4_K_M (vision, ~7 t/s on 3 GPUs)
#   ./start-llm-server.sh q8       # Qwen3-VL-32B Q8_0 (slow, spills to CPU)

set -e

# Configuration
MODEL_DIR="$HOME/models"
LLAMA_DIR="$HOME/llama.cpp"
HOST="0.0.0.0"
PORT="8080"

# Vulkan setup (MoltenVK on macOS)
export VK_ICD_FILENAMES="/usr/local/etc/vulkan/icd.d/MoltenVK_icd.json"

# Model selection
CHOICE="${1:-gemma-q3}"
DEVICE="Vulkan2"  # GPU2 (Pro 580X) — change to Vulkan0,Vulkan1,Vulkan2 for standalone (no indexer)
MMPROJ=""
EXTRA_ARGS=""

case "$CHOICE" in
    gemma-q3)
        MODEL="$MODEL_DIR/gemma-3-12b-it-Q3_K_S.gguf"
        MMPROJ="$MODEL_DIR/mmproj-gemma-3-12b-it-f16.gguf"
        echo "Starting Gemma 3 12B Q3_K_S (vision, fits in 8GB, for use alongside indexer)"
        ;;
    14b)
        MODEL="$MODEL_DIR/Qwen3-14B-Q4_K_M.gguf"
        EXTRA_ARGS="--parallel 3"
        echo "Starting Qwen3-14B Q4_K_M (text only, ~14 t/s — best on all 3 GPUs)"
        ;;
    gemma)
        MODEL="$MODEL_DIR/gemma-3-12b-it-Q4_K_M.gguf"
        MMPROJ="$MODEL_DIR/mmproj-gemma-3-12b-it-f16.gguf"
        EXTRA_ARGS="--parallel 3"
        echo "Starting Gemma 3 12B Q4_K_M (vision, ~13 t/s — best on all 3 GPUs)"
        ;;
    32b|q4)
        MODEL="$MODEL_DIR/Qwen3VL-32B-Instruct-Q4_K_M.gguf"
        MMPROJ="$MODEL_DIR/mmproj-Qwen3VL-32B-Instruct-F16.gguf"
        echo "Starting Qwen3-VL-32B Q4_K_M (vision, ~7 t/s)"
        ;;
    q8)
        MODEL="$MODEL_DIR/Qwen3VL-32B-Instruct-Q8_0.gguf"
        MMPROJ="$MODEL_DIR/mmproj-Qwen3VL-32B-Instruct-F16.gguf"
        echo "Starting Qwen3-VL-32B Q8_0 (spills to CPU, ~0.14 t/s)"
        ;;
    *)
        echo "Unknown model: $CHOICE"
        echo "Usage: $0 [gemma-q3|14b|gemma|32b|q8]"
        echo ""
        echo "For parallel indexing (2 GPUs independent), use:"
        echo "  ~/start-indexer-gpus.sh"
        exit 1
        ;;
esac

# Verify model file exists
if [ ! -f "$MODEL" ]; then
    echo "ERROR: Model not found: $MODEL"
    exit 1
fi

# Build mmproj argument if needed
MMPROJ_ARG=""
if [ -n "$MMPROJ" ]; then
    if [ ! -f "$MMPROJ" ]; then
        echo "ERROR: Vision encoder not found: $MMPROJ"
        exit 1
    fi
    MMPROJ_ARG="--mmproj $MMPROJ"
fi

echo ""
echo "==================================="
echo "  LLM Server (Single Mode)"
echo "==================================="
echo "  Model:   $(basename "$MODEL")"
if [ -n "$MMPROJ" ]; then
echo "  Vision:  $(basename "$MMPROJ")"
fi
echo "  API:     http://$HOST:$PORT"
echo "  GPU:     $DEVICE"
echo "==================================="
echo ""
echo "Connect any OpenAI-compatible tool to: http://10.10.11.157:$PORT"
echo ""

exec "$LLAMA_DIR/build/bin/llama-server" \
    --host "$HOST" \
    --port "$PORT" \
    -m "$MODEL" \
    $MMPROJ_ARG \
    --device "$DEVICE" \
    -ngl 99 \
    --ctx-size 8192 \
    $EXTRA_ARGS
