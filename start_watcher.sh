#!/bin/bash
# Kill existing watcher if running
pkill -f 'session_watcher.py' 2>/dev/null

# Start in tmux session
tmux kill-session -t watcher 2>/dev/null
tmux new-session -d -s watcher "cd $(dirname "$0") && python3 session_watcher.py 2>&1 | tee /tmp/watcher.log"
echo "[watcher] started. Use: tmux attach -t watcher"
