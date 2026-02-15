#!/bin/bash
# LLM Server Startup Script
# Runs Qwen3-VL-32B on the Intel Mac Pro with all 3 GPUs
#
# Usage:
#   ./start-llm-server.sh          # Default: Q4_K_M (fits in VRAM, ~6 tok/s)
#   ./start-llm-server.sh q8       # Q8_0 (best accuracy, spills to CPU, ~0.14 tok/s)

set -e

# Configuration
MODEL_DIR="$HOME/models"
LLAMA_DIR="$HOME/llama.cpp"
HOST="0.0.0.0"
PORT="8080"

# Vulkan setup (MoltenVK on macOS)
export VK_ICD_FILENAMES="/usr/local/etc/vulkan/icd.d/MoltenVK_icd.json"

# Model selection
QUALITY="${1:-q4}"

case "$QUALITY" in
    q8|Q8)
        MODEL="$MODEL_DIR/Qwen3VL-32B-Instruct-Q8_0.gguf"
        echo "Starting with Q8_0 model (best accuracy, ~35GB, spills to CPU RAM)"
        ;;
    q4|Q4)
        MODEL="$MODEL_DIR/Qwen3VL-32B-Instruct-Q4_K_M.gguf"
        echo "Starting with Q4_K_M model (fits in VRAM, faster, ~20GB)"
        ;;
    *)
        echo "Unknown quality: $QUALITY"
        echo "Usage: $0 [q8|q4]"
        exit 1
        ;;
esac

MMPROJ="$MODEL_DIR/mmproj-Qwen3VL-32B-Instruct-F16.gguf"

# Verify files exist
if [ ! -f "$MODEL" ]; then
    echo "ERROR: Model not found: $MODEL"
    exit 1
fi

if [ ! -f "$MMPROJ" ]; then
    echo "ERROR: Vision encoder not found: $MMPROJ"
    exit 1
fi

echo ""
echo "==================================="
echo "  Qwen3-VL-32B LLM Server"
echo "==================================="
echo "  Model:   $(basename "$MODEL")"
echo "  Vision:  $(basename "$MMPROJ")"
echo "  API:     http://$HOST:$PORT"
echo "  GPUs:    Vulkan0, Vulkan1, Vulkan2"
echo "==================================="
echo ""
echo "Connect any OpenAI-compatible tool to: http://$(hostname -I 2>/dev/null || echo "10.10.11.173" | tr -d ' '):$PORT"
echo ""

exec "$LLAMA_DIR/build/bin/llama-server" \
    --host "$HOST" \
    --port "$PORT" \
    -m "$MODEL" \
    --mmproj "$MMPROJ" \
    --device Vulkan0,Vulkan1,Vulkan2 \
    -ngl 99 \
    --ctx-size 8192
