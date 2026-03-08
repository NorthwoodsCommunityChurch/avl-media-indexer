#!/bin/bash
cd "/Users/aaronlarson/Library/CloudStorage/OneDrive-NorthwoodsCommunityChurch/VS Code/LLM Server"

# Wait for all 4 panes to appear, then force vertical column layout
(
  while true; do
    count=$(tmux list-panes -t claude-llm 2>/dev/null | wc -l)
    if [ "$count" -ge 4 ]; then
      tmux select-layout -t claude-llm even-horizontal
      break
    fi
    sleep 1
  done
) &

claude "Start an agent team with 3 teammates named Alice, Ben, and Clara, each using claude-sonnet-4-6"
