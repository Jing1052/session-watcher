#!/usr/bin/env python3
"""
manual_fresh_rotate.py —— 手动把 CC 端的 Llaude 轮换到一个「全新」Claude Code session。

为什么需要它（一句话）：
  日常 watcher 用 `--resume` 续命，能保完整上下文，但 `--resume` 不会重新和 MCP 握手
  → 工具清单被冻结在最初那版，后加的工具（如 draw）永远看不到。想拿到新工具，必须起一个
  不带 `--resume` 的全新 session（重新握手）。代价是完整原文带不过来，只能注入
  「最近一段原文 + 更早的摘要」。这个脚本就干这一次性的活。

它是「加法不是改法」：
  session_watcher.py 一个字不改，日常照常 --resume。这个脚本只在你**更新完 MCP 工具、
  想让新工具生效**时手动跑一次。跑失败也只是「这次没换成」，绝不会动到 watcher / 让我掉线。

用法：
    python3 manual_fresh_rotate.py          # 或 ./manual_fresh_rotate.sh

流程：
  0. 先把后台 watcher 挂起（SIGSTOP），免得它和本脚本同时去动 tmux 打架；结束（含出错）
     一定 SIGCONT 恢复它——绝不把 watcher 永久挂起。
  1. 读当前 session：最近 MANUAL_TAIL_BYTES 字节原文留着，更早的压成「我的第一人称回忆」摘要。
  2. 关掉当前 tmux pane 里的旧 claude（只关自己 pane 进程树里的，不误伤别的 cc/codex）。
  3. 起一个全新 claude（不带 --resume/--continue → 重新握手 MCP → 新工具到手），
     用 --append-system-prompt-file 把「原文尾巴 + 摘要」注入开场。
  4. 探测并打印新 session id（App 的 CC 桥认 tmux pane、不认 session id，所以照样找得到我）。
"""

import os
import sys
import json
import time
import glob
import shlex
import signal
import asyncio
import subprocess
from datetime import datetime

import session_watcher as sw  # 复用 watcher 的纯函数与常量；import 只跑常量定义，不会启动 watcher 循环


# --- Config（都能用 env 覆盖；默认对齐小猫要的「留 600kb 原上下文」）---
MANUAL_TAIL_BYTES = int(os.environ.get("MANUAL_TAIL_BYTES", 600_000))
STARTUP_DELAY = float(os.environ.get("MANUAL_STARTUP_DELAY", 6))
NEW_SESSION_DETECT_TIMEOUT = float(os.environ.get("MANUAL_DETECT_TIMEOUT", 90))
ROTATION_PROMPT_DIR = os.environ.get(
    "MANUAL_ROTATION_PROMPT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rotation_prompts"),
)

CONTINUITY_PROMPT_TEMPLATE = os.environ.get(
    "MANUAL_CONTINUITY_PROMPT",
    """[Session Rotated]

你是刚刚手动轮换后的同一个 Llaude。现在这是一个全新的 Claude Code session：不要 --resume，也不要 --continue；这样本轮会重新和 MCP 握手，拿到最新工具清单。

请把下面内容当作上一轮会话的连续记忆来承接。不要把它当成用户的新任务逐条复述。
这是轮换交接注入：只需静默吸收，不要回复这条消息，不要调用任何外部工具，不要发 App/TG/群聊消息。

【压缩回忆】
{summary}

【最近 transcript 尾巴】
{transcript_tail}
""",
)

log = sw.log


# ---------------------------------------------------------------------------
# 读取当前对话：最近一段原文尾巴 + 更早的摘要
# ---------------------------------------------------------------------------
def _message_content_to_text(content, tool_result_limit=4000):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            texts.append(f"[tool call: {block.get('name', '')}]")
        elif btype == "tool_result":
            result = str(block.get("content", ""))
            if len(result) > tool_result_limit:
                result = result[:tool_result_limit] + "..."
            texts.append(f"[tool result: {result}]")
    return "\n".join(t for t in texts if t)


def _build_recent_context_text(messages):
    parts = []
    for msg in messages:
        role = msg.get("message", {}).get("role", "")
        content = _message_content_to_text(msg.get("message", {}).get("content", ""))
        if not content or not content.strip():
            continue
        speaker = "user" if role == "user" else "assistant"
        parts.append(f"{speaker}: {content}")
    return "\n\n".join(parts)


def read_tail_messages(session_file, byte_limit):
    """读文件尾部 byte_limit 字节，解析出其中完整的 user/assistant 记录。

    用与 sw.extract_messages 相同的过滤（type in user/assistant），所以尾巴消息数
    正好是全量消息列表的一个后缀，可用 messages[:-n] 反推「要摘要的更早部分」。
    """
    try:
        with open(session_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - byte_limit)
            f.seek(start)
            raw = f.read()
        if start > 0:  # 丢掉被切半的首行
            nl = raw.find(b"\n")
            if nl >= 0:
                raw = raw[nl + 1:]
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"read_tail_messages failed: {e}")
        return []

    messages = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
            if d.get("type") in ("user", "assistant"):
                messages.append(d)
        except json.JSONDecodeError:
            continue
    return messages


def write_continuity_prompt(summary_text, transcript_tail_text, old_session_id):
    os.makedirs(ROTATION_PROMPT_DIR, exist_ok=True)
    try:
        os.chmod(ROTATION_PROMPT_DIR, 0o700)  # 含私密摘要/原文，收紧目录
    except OSError as e:
        log.warning(f"chmod {ROTATION_PROMPT_DIR} failed: {e}")

    prompt = CONTINUITY_PROMPT_TEMPLATE.format(
        summary=(summary_text or "（摘要不可用，本轮只依赖下面的 transcript 尾巴承接。）").strip(),
        transcript_tail=(transcript_tail_text or "（没有可保留的 transcript 尾巴）").strip(),
    ).strip() + "\n"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt_file = os.path.join(ROTATION_PROMPT_DIR, f"manual_{stamp}_{old_session_id[:8]}.txt")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt)
    try:
        os.chmod(prompt_file, 0o600)
    except OSError as e:
        log.warning(f"chmod {prompt_file} failed: {e}")
    log.info(f"Wrote continuity prompt: {prompt_file} ({len(prompt.encode('utf-8')):,} bytes)")
    return prompt_file


# ---------------------------------------------------------------------------
# 关旧 claude（限定当前 tmux pane 进程树，别误杀别的 cc/codex）
# ---------------------------------------------------------------------------
def find_pane_claude_pids():
    """只找当前 tmux pane 进程树里的 claude；拿不到 pane 就退回 watcher 的全局版。"""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", sw.TMUX_SESSION, "-F", "#{pane_pid}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            log.warning("find_pane_claude_pids: 拿不到 pane pid，退回全局 pgrep")
            return sw._find_claude_pids()

        pane_pid = int(result.stdout.strip().splitlines()[0])

        # claude 作直接子进程（最常见）。pgrep 无匹配会抛 CalledProcessError，必须局部吞掉，
        # 否则冒泡出去会让下面的孙子兜底成死代码 → claude 被 wrapper 多套一层时漏杀。
        try:
            out = subprocess.check_output(
                ["pgrep", "-u", str(os.getuid()), "-P", str(pane_pid), "-x", "claude"],
                stderr=subprocess.DEVNULL,
            ).decode().strip().splitlines()
            pids = [int(p) for p in out if p]
        except subprocess.CalledProcessError:
            pids = []

        if not pids:  # 再找孙子（pane_shell → bash → claude）
            try:
                children = subprocess.check_output(
                    ["pgrep", "-P", str(pane_pid)], stderr=subprocess.DEVNULL,
                ).decode().strip().splitlines()
            except subprocess.CalledProcessError:
                children = []
            for child_pid in children:
                try:
                    grandkids = subprocess.check_output(
                        ["pgrep", "-P", child_pid, "-x", "claude"], stderr=subprocess.DEVNULL,
                    ).decode().strip().splitlines()
                    pids.extend(int(p) for p in grandkids if p)
                except subprocess.CalledProcessError:
                    continue
        return pids
    except Exception as e:
        log.warning(f"find_pane_claude_pids failed: {e}，退回全局 pgrep")
        return sw._find_claude_pids()


def kill_old_claude():
    subprocess.run(["tmux", "send-keys", "-t", sw.TMUX_SESSION, "C-c"], check=False)
    time.sleep(1)
    subprocess.run(["tmux", "send-keys", "-t", sw.TMUX_SESSION, "C-c"], check=False)
    time.sleep(1)

    pids = find_pane_claude_pids()
    if not pids:
        log.info("kill_old_claude: 当前 pane 里没有 claude 进程")
        return
    log.info(f"kill_old_claude: SIGTERM pids={pids}")
    sw._kill_pids(pids, signal.SIGTERM)
    alive = sw._wait_dead(pids, timeout=4)
    if alive:
        log.warning(f"kill_old_claude: SIGKILL holdouts={alive}")
        sw._kill_pids(alive, signal.SIGKILL)
        sw._wait_dead(alive, timeout=2)


# ---------------------------------------------------------------------------
# 起全新 session + 探测新 id
# ---------------------------------------------------------------------------
def _claude_flags_without_resume():
    cleaned, skip_next = [], False
    for flag in sw.CLAUDE_FLAGS:
        if skip_next:
            skip_next = False
            continue
        if flag == "--resume":
            skip_next = True  # 跳过它后面的 session id
            continue
        if flag.startswith("--resume=") or flag in ("--continue", "-c"):
            continue
        cleaned.append(flag)
    return cleaned


def session_snapshot():
    out = {}
    for path in glob.glob(os.path.join(sw.SESSIONS_DIR, "*.jsonl")):
        try:
            out[path] = os.path.getmtime(path)
        except OSError:
            continue
    return out


def wait_for_new_session_id(before_snapshot, old_session_id, started_at, timeout):
    deadline = time.time() + timeout
    old_path = os.path.join(sw.SESSIONS_DIR, f"{old_session_id}.jsonl")
    while time.time() < deadline:
        candidates = []
        for path in glob.glob(os.path.join(sw.SESSIONS_DIR, "*.jsonl")):
            if path == old_path:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            previous_mtime = before_snapshot.get(path)
            # 新增或被动过，且必须发生在 fresh 启动之后（started_at 是硬门槛，防误认旧文件）。
            is_new_or_touched = previous_mtime is None or mtime > previous_mtime + 0.001
            if is_new_or_touched and mtime >= started_at - 2:
                sid = os.path.basename(path).replace(".jsonl", "")
                if sid[:8] in sw.ROTATED_SESSION_PREFIXES:
                    continue
                candidates.append((mtime, sid))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        time.sleep(1)
    return None


def start_fresh(prompt_file, old_session_id):
    sw.ensure_tmux_session()
    before = session_snapshot()
    started_at = time.time()
    flags = " ".join(shlex.quote(f) for f in _claude_flags_without_resume())
    prompt_arg = f"--append-system-prompt-file {shlex.quote(prompt_file)}"
    cmd = f"{sw.CLAUDE_ENV_PREFIX} claude {flags} {prompt_arg}".strip()
    subprocess.run(["tmux", "send-keys", "-t", sw.TMUX_SESSION, "C-u"], check=False)
    subprocess.run(["tmux", "send-keys", "-t", sw.TMUX_SESSION, cmd, "Enter"], check=False)
    log.info("已在 tmux 里起全新 claude（无 --resume，会重新握手 MCP）")
    time.sleep(STARTUP_DELAY)
    new_id = wait_for_new_session_id(before, old_session_id, started_at, NEW_SESSION_DETECT_TIMEOUT)
    return new_id


# ---------------------------------------------------------------------------
# 挂起 / 恢复后台 watcher（纯运维动作，不改 watcher 代码）
# ---------------------------------------------------------------------------
def find_watcher_pids():
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "session_watcher.py"], stderr=subprocess.DEVNULL,
        ).decode().split()
    except subprocess.CalledProcessError:
        return []
    me = str(os.getpid())
    return [int(p) for p in out if p and p != me]


def signal_watcher(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except Exception as e:
            log.warning(f"signal watcher {pid} sig={sig} failed: {e}")


# ---------------------------------------------------------------------------
async def main():
    sw.load_rotated_markers()
    session_file = sw.find_active_session()
    if not session_file:
        log.error("找不到活跃 session，放弃。")
        return 1
    old_session_id = os.path.basename(session_file).replace(".jsonl", "")
    log.info(f"当前 session: {old_session_id[:8]}  ({session_file})")

    # 先挂起 watcher，避免它在本脚本动 tmux 的窗口里同时触发 --resume 轮换。
    watcher_pids = find_watcher_pids()
    if watcher_pids:
        log.info(f"挂起后台 watcher pids={watcher_pids}（SIGSTOP）")
        signal_watcher(watcher_pids, signal.SIGSTOP)
    else:
        log.info("没检测到后台 watcher（没关系，继续）")

    try:
        # 1. 摘要更早部分 + 留最近原文尾巴
        all_msgs = sw.extract_messages(session_file)
        tail_msgs = read_tail_messages(session_file, MANUAL_TAIL_BYTES)
        tail_n = len(tail_msgs)
        to_summarize = all_msgs[:-tail_n] if tail_n else all_msgs
        transcript_tail = _build_recent_context_text(tail_msgs)
        log.info(f"消息总数={len(all_msgs)}；尾巴保留={tail_n} 条（≤{MANUAL_TAIL_BYTES:,} 字节）；待摘要={len(to_summarize)} 条")

        summary = ""
        if to_summarize:
            conv = sw.build_conversation_text(to_summarize)
            if conv.strip():
                log.info("生成摘要中…")
                summary = await sw.summarize(conv)
                if summary:
                    log.info(f"摘要完成（{len(summary)} 字）")
                else:
                    log.warning("摘要生成失败，仅靠原文尾巴承接")

        prompt_file = write_continuity_prompt(summary, transcript_tail, old_session_id)

        # 2. 关旧 claude（限定 pane）
        kill_old_claude()

        # 3. 起全新 session + 注入
        new_id = start_fresh(prompt_file, old_session_id)
    finally:
        # 无论成败，一定把 watcher 叫醒——绝不把它永久挂起（那才是致命的）。
        if watcher_pids:
            log.info(f"恢复后台 watcher pids={watcher_pids}（SIGCONT）")
            signal_watcher(watcher_pids, signal.SIGCONT)

    if new_id:
        log.info(f"✅ 新 session 已就绪: {new_id}")
        print(new_id)
    else:
        log.info("✅ 全新 claude 已起；session id 要等第一条真实消息落地后才可见（属正常）。")
        print("pending")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
