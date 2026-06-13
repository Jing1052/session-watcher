@echo off
chcp 65001 >nul
REM ============================================================
REM  L&C 全家桶 · Windows 开机自启
REM  放在 shell:startup（%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup）
REM  启动顺序：等代理7897 -> cc bridge(8787) -> cloudflared 隧道 -> 网易云(8766) -> WSL(TG claude + watcher)
REM  ⚠ SakuraCat 代理请在其自身设置里开启「开机自启」；本脚本只负责等它就绪。
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

REM 1) cc bridge (FastAPI/FastMCP, :8787)
start "" powershell -NoProfile -WindowStyle Hidden -Command "Set-Location 'C:\Users\33946\cc-mcp-server'; $env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY='http://127.0.0.1:7897'; $env:MCP_API_KEY='cllove2026'; python main.py"

REM 2) cloudflared 统一隧道（cc.cllove.top:8787 + music.cllove.top:8766）
start "" powershell -NoProfile -WindowStyle Hidden -Command "cloudflared tunnel run cc-mcp"

REM 3) 网易云 MCP (--http, :8766)  ※ 工作区须停在带 --http 的分支
start "" powershell -NoProfile -WindowStyle Hidden -Command "Set-Location 'C:\Users\33946\netease-music-mcp'; node src/server.js --http"

REM 4) WSL 侧：TG claude + session-watcher（幂等脚本）
wsl -u cing -- bash -c "bash ~/session-watcher/wsl-startup.sh"

exit /b 0
