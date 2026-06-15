#!/bin/bash
# Flip the session-watcher rotation threshold between two presets, live (no restart).
#   low  = 日常档（默认）
#   high = 大上下文档（1M context）
# Usage:
#   ./mode.sh           # 看当前档位
#   ./mode.sh low       # 切到低档（频繁轮换、省上下文）
#   ./mode.sh high      # 切到高档（吃满大窗口再轮换）
F="$(dirname "$0")/.threshold_mode"
case "$1" in
  low|high)
    echo "$1" > "$F"
    echo "[mode] -> $1  (watcher 下一轮巡检 ≤30s 内生效)"
    ;;
  "")
    echo "current mode: $(cat "$F" 2>/dev/null || echo 'low (default)')"
    echo "usage: $0 low|high"
    ;;
  *)
    echo "unknown mode '$1' — only 'low' or 'high'"
    exit 1
    ;;
esac
