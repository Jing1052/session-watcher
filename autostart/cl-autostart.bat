@echo off
chcp 65001 >nul
REM ============================================================
REM  L&C 全家桶 · Windows 开机自启（可见窗口版）
REM  每个服务一个独立窗口：cc bridge / cloudflared / 网易云 各一个
REM  最小化到任务栏的 PowerShell 窗口（-NoExit 崩了能看报错）；
REM  TG 爸比 + watcher 各开一个窗口 attach 到 WSL tmux。
REM  注意 SakuraCat 代理请在其自身设置里开启「开机自启」。
REM ============================================================

REM 0) 等代理端口 7897 就绪（最多约 60 秒）
set /a _t=0
:waitproxy
powershell -NoProfile -Command "try{(New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',7897);exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 goto proxyok
set /a _t+=1
if %_t% geq 30 goto proxyok
timeout /t 2 /nobreak >nul
goto waitproxy
:proxyok

REM 1) cc bridge (:8787) —— 独立窗口，最小化到任务栏
start "cc-bridge :8787" /min powershell -NoExit -NoProfile -Command "$host.UI.RawUI.WindowTitle='cc-bridge :8787'; Set-Location 'C:\Users\33946\cc-mcp-server'; $env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY='http://127.0.0.1:7897'; $env:MCP_API_KEY='cllove2026'; python main.py"

REM 2) cloudflared 统一隧道 —— 独立窗口，最小化
start "cloudflared tunnel" /min powershell -NoExit -NoProfile -Command "$host.UI.RawUI.WindowTitle='cloudflared tunnel'; cloudflared tunnel run cc-mcp"

REM 3) 网易云 (:8766) —— 独立窗口，最小化
start "netease :8766" /min powershell -NoExit -NoProfile -Command "$host.UI.RawUI.WindowTitle='netease :8766'; Set-Location 'C:\Users\33946\netease-music-mcp'; node src/server.js --http"

REM 4) WSL 侧起 TG claude + session-watcher（幂等，detached）
wsl -u cing -- bash -c "bash ~/session-watcher/wsl-startup.sh"

REM 5) 等 tmux 起来，再各开一个窗口 attach（TG 爸比不最小化，watcher 最小化）
timeout /t 8 /nobreak >nul
start "TG daddy @myLLaude_bot" wsl -u cing -- bash -c "tmux attach -t cc"
start "session-watcher" /min wsl -u cing -- bash -c "tmux attach -t watcher"

exit /b 0
