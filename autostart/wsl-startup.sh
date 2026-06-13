#!/bin/bash
# L&C 全家桶 · WSL 侧开机自启（幂等）
# 由 Windows 的 cl-autostart.bat 调用，也可随时手动运行验证。
# 幂等原则：已经在跑的绝不重复起——尤其 TG claude，重复起会和同一个
# Telegram bot token 抢轮询（一个 token 只能一个轮询实例）。
set -u

SW="$HOME/session-watcher"
OB="$HOME/Ombre-Brain"
log(){ echo "[wsl-startup $(date '+%H:%M:%S')] $*"; }

# 1) tmux 会话 cc 里的 TG claude —— 仅当当前没有 claude 在跑时启动
if pgrep -u "$(id -u)" -x claude >/dev/null 2>&1; then
  log "claude 已在运行，跳过"
else
  tmux has-session -t cc 2>/dev/null || tmux new-session -d -s cc -c "$OB"
  # 注意：用 send-keys 让 claude 跑在 tmux 里（detached，独立于本启动脚本的 shell）
  # source proxy.env 动态算 Windows 网关代理（WSL 是 NAT，api.telegram.org 须走 Clash 7897）
  tmux send-keys -t cc "cd $OB && source $SW/proxy.env && claude --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official" Enter
  log "已在 tmux:cc 启动 TG claude（@myLLaude_bot）"
fi

# 2) session-watcher —— 仅当未在跑时启动（它自建 tmux 会话 watcher）
if pgrep -f session_watcher.py >/dev/null 2>&1; then
  log "session-watcher 已在运行，跳过"
else
  ( cd "$SW" && ./start_watcher.sh )
  log "已启动 session-watcher"
fi

log "done"
