#!/bin/bash
# Check LLM server status (supports both single and multi-GPU modes)

FOUND=0

# Check single-server mode (port 8080)
if lsof -ti:8080 > /dev/null 2>&1; then
    echo "=== Single-Server Mode (port 8080) ==="
    curl -s http://localhost:8080/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(could not reach health endpoint)"
    echo ""
    echo "Process info:"
    ps aux | grep "llama-server" | grep "8080" | grep -v grep
    FOUND=1
fi

# Check multi-GPU mode (ports 8090-8092)
MULTI_COUNT=0
for i in 0 1 2; do
    PORT=$((8090 + i))
    PID=$(lsof -ti:$PORT 2>/dev/null || true)
    if [ -n "$PID" ]; then
        if [ $MULTI_COUNT -eq 0 ]; then
            echo "=== Multi-GPU Indexer Mode ==="
        fi
        HEALTH=$(curl -s "http://localhost:$PORT/health" 2>/dev/null || echo "unreachable")
        echo "  GPU$i (port $PORT): $HEALTH"
        MULTI_COUNT=$((MULTI_COUNT + 1))
        FOUND=1
    fi
done

if [ $MULTI_COUNT -gt 0 ]; then
    echo "  $MULTI_COUNT / 3 GPU servers running"
fi

if [ $FOUND -eq 0 ]; then
    echo "No LLM servers running."
    echo ""
    echo "Start options:"
    echo "  ~/start-llm-server.sh          # Single server (Qwen, etc.)"
    echo "  ~/start-indexer-gpus.sh         # 3x GPU indexer (Gemma Q3_K_S)"
fi
