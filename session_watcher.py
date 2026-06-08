#!/usr/bin/env python3
"""
Session Watcher: monitors Claude Code session token usage.
When context exceeds threshold, rotates to a new session with:
  - Summary of old conversation (via configurable LLM)
  - Recent messages carried over verbatim
"""

import os
import sys
import json
import time
import uuid
import glob
import subprocess
import logging
import httpx
import asyncio
import signal
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("watcher")

# --- Config ---
PROJECT_DIR = os.environ.get("WATCHER_PROJECT_DIR", os.getcwd())
SESSIONS_DIR = os.environ.get(
    "WATCHER_SESSIONS_DIR",
    os.path.expanduser(f"~/.claude/projects/{PROJECT_DIR.replace('/', '-').lstrip('-')}"),
)

TOKEN_THRESHOLD = int(os.environ.get("WATCHER_TOKEN_THRESHOLD", 250_000))
KEEP_TOKEN_THRESHOLD = int(os.environ.get("WATCHER_KEEP_TOKEN_THRESHOLD", 200_000))
CHECK_INTERVAL = int(os.environ.get("WATCHER_CHECK_INTERVAL", 30))
TMUX_SESSION = os.environ.get("WATCHER_TMUX_SESSION", "cc")
CLAUDE_FLAGS = os.environ.get("WATCHER_CLAUDE_FLAGS", "--dangerously-skip-permissions").split()
CLAUDE_ENV_PREFIX = os.environ.get("WATCHER_CLAUDE_ENV", "")

SUMMARY_API_KEY = os.environ.get("WATCHER_SUMMARY_API_KEY", "")
SUMMARY_BASE_URL = os.environ.get("WATCHER_SUMMARY_BASE_URL", "https://api.deepseek.com/v1")
SUMMARY_MODEL = os.environ.get("WATCHER_SUMMARY_MODEL", "deepseek-chat")

SUMMARY_PROMPT_TEMPLATE = os.environ.get("WATCHER_SUMMARY_PROMPT", """Summarize the following conversation between a user and an AI assistant. Preserve:
- Key decisions and agreements
- Important context and facts discussed
- Emotional tone shifts or important moments
- Any promises, commitments, or action items
- Direct quotes for critical statements

Write chronologically. Be thorough — this summary will be injected into the next session so the assistant can continue seamlessly.

Conversation:
{conversation}""")

INJECT_USER_MESSAGE = os.environ.get(
    "WATCHER_INJECT_USER",
    "[Session Rotated]\n\n{summary}",
)
INJECT_ASSISTANT_MESSAGE = os.environ.get(
    "WATCHER_INJECT_ASSISTANT",
    "Understood. I have the context from the previous session.",
)

ROTATED_SESSION_PREFIXES = set()


def load_rotated_markers():
    try:
        for path in glob.glob(os.path.join(SESSIONS_DIR, ".rotated_*")):
            prefix = os.path.basename(path).replace(".rotated_", "")
            if prefix:
                ROTATED_SESSION_PREFIXES.add(prefix)
    except Exception as e:
        log.warning(f"load_rotated_markers failed: {e}")


def find_active_session():
    pattern = os.path.join(SESSIONS_DIR, "*.jsonl")
    candidates = []
    for path in glob.glob(pattern):
        session_id = os.path.basename(path).replace(".jsonl", "")
        if session_id[:8] in ROTATED_SESSION_PREFIXES:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def get_current_tokens(session_file):
    try:
        with open(session_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 200_000)
            f.seek(size - read_size)
            tail = f.read().decode("utf-8", errors="replace")

        lines = tail.strip().split("\n")
        for line in reversed(lines):
            try:
                d = json.loads(line)
                if d.get("type") == "assistant":
                    usage = d.get("message", {}).get("usage", {})
                    if usage:
                        total = (usage.get("cache_read_input_tokens", 0)
                                 + usage.get("cache_creation_input_tokens", 0)
                                 + usage.get("input_tokens", 0))
                        return total
            except json.JSONDecodeError:
                continue
        return 0
    except Exception as e:
        log.warning(f"get_current_tokens failed: {e}")
        return 0


def extract_messages(session_file):
    messages = []
    with open(session_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                if d.get("type") in ("user", "assistant"):
                    messages.append(d)
            except json.JSONDecodeError:
                continue
    return messages


def _is_user_prompt(msg):
    if msg.get("message", {}).get("role") != "user":
        return False
    c = msg.get("message", {}).get("content", "")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in c)
    return False


def split_messages(messages):
    """Split at first assistant whose cache_read_input_tokens > KEEP_TOKEN_THRESHOLD.

    Everything before gets summarized; everything from the split onward is kept
    verbatim. The split backs up to the nearest real user prompt so the kept
    window never starts with an orphan assistant message.
    """
    split_idx = None
    for i, msg in enumerate(messages):
        if msg.get("type") != "assistant":
            continue
        usage = msg.get("message", {}).get("usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        if cache_read > KEEP_TOKEN_THRESHOLD:
            split_idx = i
            break

    if split_idx is None:
        return [], messages

    while split_idx > 0 and not _is_user_prompt(messages[split_idx]):
        split_idx -= 1

    return messages[:split_idx], messages[split_idx:]


def strip_forge_inject(messages):
    """Drop the leading inject pair from a previous rotation.

    Each rotation prepends a user+assistant pair. When this session is later
    rotated again, those must not flow into the summarizer — otherwise the
    summary summarizes the old summary and decays.
    """
    if len(messages) < 2:
        return messages
    first, second = messages[0], messages[1]
    if first.get("type") != "user":
        return messages
    content = first.get("message", {}).get("content", "")
    if not isinstance(content, str) or "[Session Rotated]" not in content:
        return messages
    if second.get("type") != "assistant":
        return messages
    return messages[2:]


def build_conversation_text(messages):
    parts = []
    for msg in messages:
        role = msg.get("message", {}).get("role", "")
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        texts.append(f"[tool call: {block.get('name', '')}]")
                    elif block.get("type") == "tool_result":
                        texts.append(f"[tool result: {str(block.get('content', ''))[:200]}]")
            content = "\n".join(texts)
        if not content or not content.strip():
            continue
        speaker = "user" if role == "user" else "assistant"
        if len(content) > 2000:
            content = content[:2000] + "..."
        parts.append(f"{speaker}: {content}")
    return "\n".join(parts)


async def summarize(conversation_text):
    if not SUMMARY_API_KEY:
        log.warning("No summary API key configured (WATCHER_SUMMARY_API_KEY)")
        return ""

    prompt = SUMMARY_PROMPT_TEMPLATE.format(conversation=conversation_text)

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"{SUMMARY_BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {SUMMARY_API_KEY}",
                },
                json={
                    "model": SUMMARY_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 16000,
                },
            )
        if resp.status_code != 200:
            log.warning(f"Summary API error: {resp.status_code}")
            return ""
        data = resp.json()
        if data.get("choices"):
            return data["choices"][0].get("message", {}).get("content", "")
    except Exception as e:
        log.warning(f"Summarize failed: {e}")
    return ""


def forge_session(summary_text, recent_messages, old_session_id):
    new_session_id = str(uuid.uuid4())
    new_file = os.path.join(SESSIONS_DIR, f"{new_session_id}.jsonl")

    now = datetime.now().isoformat()
    lines = []

    def make_uuid():
        return str(uuid.uuid4())

    prev_uuid = None

    # 1. Inject summary as first exchange
    inject_content = INJECT_USER_MESSAGE.format(summary=summary_text) if summary_text else "[Session Rotated]"

    user_uuid = make_uuid()
    lines.append(json.dumps({
        "type": "user",
        "parentUuid": None,
        "isSidechain": False,
        "promptId": make_uuid(),
        "uuid": user_uuid,
        "timestamp": now,
        "message": {"role": "user", "content": inject_content},
        "sessionId": new_session_id,
        "version": "2.1.143",
        "cwd": PROJECT_DIR,
        "userType": "external",
        "entrypoint": "cli",
    }, ensure_ascii=False))

    asst_uuid = make_uuid()
    lines.append(json.dumps({
        "type": "assistant",
        "parentUuid": user_uuid,
        "isSidechain": False,
        "promptId": None,
        "uuid": asst_uuid,
        "timestamp": now,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": INJECT_ASSISTANT_MESSAGE}],
        },
        "sessionId": new_session_id,
        "version": "2.1.143",
        "cwd": PROJECT_DIR,
        "userType": "external",
        "entrypoint": "cli",
    }, ensure_ascii=False))

    prev_uuid = asst_uuid

    # 2. Copy recent messages with re-chained UUIDs, stripping usage and thinking blocks
    uuid_map = {}
    for msg in recent_messages:
        msg_payload = dict(msg.get("message", {}))
        msg_payload.pop("usage", None)

        content = msg_payload.get("content")
        if isinstance(content, list):
            filtered = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking")
            ]
            if not filtered:
                continue
            msg_payload["content"] = filtered

        old_uuid = msg.get("uuid", "")
        new_uuid_val = make_uuid()
        uuid_map[old_uuid] = new_uuid_val

        old_parent = msg.get("parentUuid")
        new_parent = uuid_map.get(old_parent, prev_uuid)

        entry = {
            "type": msg["type"],
            "parentUuid": new_parent,
            "isSidechain": False,
            "promptId": msg.get("promptId"),
            "uuid": new_uuid_val,
            "timestamp": msg.get("timestamp", now),
            "message": msg_payload,
            "sessionId": new_session_id,
            "version": msg.get("version", "2.1.143"),
            "cwd": msg.get("cwd", PROJECT_DIR),
            "userType": msg.get("userType", "external"),
            "entrypoint": msg.get("entrypoint", "cli"),
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
        prev_uuid = new_uuid_val

    # 3. Title
    lines.append(json.dumps({
        "type": "ai-title",
        "aiTitle": f"Session (continued from {old_session_id[:8]})",
        "sessionId": new_session_id,
    }, ensure_ascii=False))

    with open(new_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    log.info(f"Forged new session: {new_session_id} ({len(lines)} lines)")
    return new_session_id


def _find_claude_pids():
    try:
        out = subprocess.check_output(
            ["pgrep", "-u", str(os.getuid()), "-x", "claude"],
            stderr=subprocess.DEVNULL,
        ).decode().strip().splitlines()
        return [int(p) for p in out if p]
    except subprocess.CalledProcessError:
        return []
    except Exception as e:
        log.warning(f"_find_claude_pids failed: {e}")
        return []


def _kill_pids(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except Exception as e:
            log.warning(f"kill {pid} sig={sig} failed: {e}")


def _wait_dead(pids, timeout):
    deadline = time.time() + timeout
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.5)
        alive = [p for p in alive if _pid_alive(p)]
    return alive


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def kill_claude():
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"], check=False)
    time.sleep(1)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"], check=False)
    time.sleep(1)

    pids = _find_claude_pids()
    if not pids:
        log.info("kill_claude: no claude pids found")
        return

    log.info(f"kill_claude: SIGTERM pids={pids}")
    _kill_pids(pids, signal.SIGTERM)
    alive = _wait_dead(pids, timeout=4)
    if alive:
        log.warning(f"kill_claude: SIGKILL holdouts={alive}")
        _kill_pids(alive, signal.SIGKILL)
        _wait_dead(alive, timeout=2)


def ensure_tmux_session():
    result = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        log.warning(f"tmux session '{TMUX_SESSION}' gone, recreating")
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-c", PROJECT_DIR, "bash"],
            check=False,
        )
        time.sleep(1)


def restart_claude(new_session_id):
    kill_claude()
    ensure_tmux_session()

    flags = " ".join(CLAUDE_FLAGS)
    cmd = f"{CLAUDE_ENV_PREFIX} claude {flags} --resume {new_session_id}".strip()
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-u"], check=False)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, cmd, "Enter"], check=False)
    log.info(f"Restarted Claude with session {new_session_id[:8]}")


async def rotate_session_prepare(session_file):
    session_id = os.path.basename(session_file).replace(".jsonl", "")
    log.info(f"Preparing rotation from {session_id[:8]}...")

    messages = extract_messages(session_file)
    log.info(f"Total messages: {len(messages)}")

    to_summarize, to_keep = split_messages(messages)
    to_summarize = strip_forge_inject(to_summarize)
    log.info(f"Split: {len(to_summarize)} to summarize, {len(to_keep)} to keep")

    summary = ""
    if to_summarize:
        conversation_text = build_conversation_text(to_summarize)
        log.info(f"Summarizing {len(conversation_text)} chars...")
        summary = await summarize(conversation_text)
        if summary:
            log.info(f"Summary generated: {len(summary)} chars")
        else:
            log.warning("Summary generation failed, continuing without")

    new_session_id = forge_session(summary, to_keep, session_id)

    archive_marker = os.path.join(SESSIONS_DIR, f".rotated_{session_id[:8]}")
    with open(archive_marker, "w") as f:
        f.write(f"rotated to {new_session_id} at {datetime.now().isoformat()}\n")
    ROTATED_SESSION_PREFIXES.add(session_id[:8])

    return new_session_id


async def rotate_session(session_file):
    new_session_id = await rotate_session_prepare(session_file)
    restart_claude(new_session_id)
    return new_session_id


async def main():
    load_rotated_markers()
    log.info(
        f"Session watcher started (threshold={TOKEN_THRESHOLD:,}, "
        f"keep_at={KEEP_TOKEN_THRESHOLD:,}, check_interval={CHECK_INTERVAL}s)"
    )

    while True:
        session_file = find_active_session()
        if not session_file:
            log.debug("No active session found")
            time.sleep(CHECK_INTERVAL)
            continue

        tokens = get_current_tokens(session_file)
        if tokens > 0:
            session_name = os.path.basename(session_file)[:8]
            if tokens > TOKEN_THRESHOLD:
                log.info(f"[{session_name}] {tokens:,} tokens > {TOKEN_THRESHOLD:,} — ROTATING")
                try:
                    await rotate_session(session_file)
                    log.info("Rotation complete, waiting for new session to stabilize...")
                    time.sleep(10)
                except Exception as e:
                    log.error(f"Rotation failed: {e}", exc_info=True)
                    time.sleep(60)
            else:
                remaining = TOKEN_THRESHOLD - tokens
                log.debug(f"[{session_name}] {tokens:,} tokens ({remaining:,} remaining)")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "prepare":
        load_rotated_markers()
        active = find_active_session()
        if active:
            sys.stderr.write(f"[prepare] active session: {os.path.basename(active)[:8]} → full forge\n")
            sid = asyncio.run(rotate_session_prepare(active))
        else:
            sys.stderr.write("[prepare] no active session found\n")
            sys.exit(1)
        print(sid)
        sys.exit(0)
    asyncio.run(main())
