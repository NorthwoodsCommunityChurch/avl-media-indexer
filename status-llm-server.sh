#!/bin/bash
# Check LLM server status
if pgrep -f "llama-server" > /dev/null; then
    echo "LLM server is RUNNING"
    echo ""
    # Try to hit the health endpoint
    curl -s http://localhost:8080/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(could not reach health endpoint)"
    echo ""
    echo "Process info:"
    ps aux | grep "llama-server" | grep -v grep
else
    echo "LLM server is NOT running."
    echo "Start it with: ~/start-llm-server.sh"
fi
