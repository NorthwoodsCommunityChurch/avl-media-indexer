#!/bin/bash
# Kill any existing session for THIS project only
tmux kill-session -t claude-llm 2>/dev/null

# Open iTerm2 in the project folder
open -a iTerm "/Users/aaronlarson/Library/CloudStorage/OneDrive-NorthwoodsCommunityChurch/VS Code/LLM Server"

# Wait for iTerm2 to open, then start tmux -CC and launch Claude
sleep 3
osascript -e 'tell application "iTerm2" to tell current window to tell current session to write text "tmux -CC new -s claude-llm \"/Users/aaronlarson/Library/CloudStorage/OneDrive-NorthwoodsCommunityChurch/VS\\ Code/LLM\\ Server/start-claude-team.sh\""'
