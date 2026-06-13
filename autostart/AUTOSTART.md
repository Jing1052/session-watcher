# 全家桶开机自启

游戏本重启后所有进程会丢失，这套脚本让它们随开机自动按序拉起，免得每次手动救。

## 启动链（顺序敏感）

```
SakuraCat 代理(7897)            ← 它自己开机自启（在 SakuraCat 设置里勾）
      ↓ 等 7897 就绪
cc bridge  python main.py :8787  ← cl-autostart.bat
cloudflared tunnel run cc-mcp    ← 统一隧道：cc.cllove.top:8787 + music.cllove.top:8766
网易云  node src/server.js --http :8766
      ↓
WSL  wsl-startup.sh → tmux:cc 里 TG claude + session-watcher
```

## 文件

- **`cl-autostart.bat`** — Windows 侧总启动脚本，放进 `shell:startup`。
- **`wsl-startup.sh`** — WSL 侧启动脚本，放在 `~/session-watcher/`，**幂等**（已在跑的跳过，防重复 claude 抢同一个 TG bot token）。

## 安装

1. **WSL 侧**：
   ```bash
   cd ~/session-watcher && git pull
   cp autostart/wsl-startup.sh ~/session-watcher/wsl-startup.sh   # 若仓库根已是它则免
   chmod +x ~/session-watcher/wsl-startup.sh
   bash ~/session-watcher/wsl-startup.sh      # 幂等，可立即测试：已在跑则全跳过
   ```

2. **Windows 侧**：把 `cl-autostart.bat` 复制到
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`
   （运行 `shell:startup` 可直接打开该文件夹）。

3. **SakuraCat**：在其自身设置里开启「开机自启」（本脚本只等它的 7897 就绪，不负责拉起它）。

## 注意事项 / 已知约束

- **网易云 8766 须停在带 `--http` 的分支**（`claude/frontend-overlap-auto-wake-puoz1y`，即 commit `ff15b89`）。
  bat 里直接 `node src/server.js --http`，依赖工作区持续停在该分支。长期更稳妥是把 `--http` 模式并进网易云 fork 的 main。
- **WSL 默认发行版**：bat 用 `wsl -u cing`（默认发行版）。若有多个发行版，改成 `wsl -d <发行版名> -u cing`。
- **代理端口 7897**：来自 SakuraCat/Clash。WSL 是 NAT 模式，`proxy.env` 用 `ip route show default` 动态算网关 IP，重启后 IP 变也没事。
- **MCP_API_KEY=cllove2026**、**NETEASE_MCP_TOKEN** 走 Windows User 环境变量，重启不丢。
- **无法在不重启的情况下端到端验证**：脚本按当前实际启动方式（记忆库「CC bridge 重启复活完整咒语」+ 本次恢复实测）编写，最终以一次真实重启验证为准。
