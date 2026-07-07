#!/usr/bin/env bash
# 手动换装：把 CC 端 Llaude 轮换到一个全新 session（重新握手 MCP、拿到新工具）。
# 更新完 MCP 工具后跑一次即可；旧 watcher 一个字不改，跑失败也不影响日常续命。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 manual_fresh_rotate.py "$@"
