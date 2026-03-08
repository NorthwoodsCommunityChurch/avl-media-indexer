#!/bin/bash
# Stop all LLM server instances (single or multi-GPU mode)

KILLED=0

if pgrep -f "llama-server" > /dev/null; then
    pkill -f "llama-server"
    KILLED=1
fi

if [ $KILLED -eq 1 ]; then
    echo "LLM server(s) stopped."
    sleep 1
    # Report any ports still in use
    for PORT in 8080 8090 8091 8092; do
        if lsof -ti:$PORT > /dev/null 2>&1; then
            echo "  WARNING: Port $PORT still in use"
        fi
    done
else
    echo "No LLM servers were running."
fi
