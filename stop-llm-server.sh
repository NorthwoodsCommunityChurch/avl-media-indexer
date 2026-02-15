#!/bin/bash
# Stop the LLM server
pkill -f "llama-server" && echo "LLM server stopped." || echo "LLM server was not running."
