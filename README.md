# session-watcher

A daemon that monitors Claude Code session token usage and automatically rotates sessions before hitting context limits.

## What it does

1. Watches the active Claude Code session's token count (every 30s by default)
2. When tokens exceed a threshold (default 250k), triggers rotation:
   - Splits the conversation — old messages get summarized via an LLM API, recent messages are kept verbatim
   - Forges a new session JSONL with the summary + recent messages
   - Kills the old Claude process and restarts with `--resume` pointing to the new session
3. The new session picks up seamlessly with full context preserved

## Configuration

All config is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `WATCHER_PROJECT_DIR` | `cwd` | Project directory Claude runs in |
| `WATCHER_SESSIONS_DIR` | auto | Claude sessions directory (derived from project dir) |
| `WATCHER_TOKEN_THRESHOLD` | `250000` | Rotate when tokens exceed this |
| `WATCHER_KEEP_TOKEN_THRESHOLD` | `200000` | Split point — messages after this are kept verbatim |
| `WATCHER_CHECK_INTERVAL` | `30` | Seconds between checks |
| `WATCHER_TMUX_SESSION` | `cc` | tmux session name for Claude |
| `WATCHER_CLAUDE_FLAGS` | `--dangerously-skip-permissions` | Flags passed to `claude` on restart |
| `WATCHER_CLAUDE_ENV` | _(empty)_ | Env var prefix for the claude command |
| `WATCHER_SUMMARY_API_KEY` | _(required)_ | API key for the summarization LLM |
| `WATCHER_SUMMARY_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible API base URL |
| `WATCHER_SUMMARY_MODEL` | `deepseek-chat` | Model to use for summarization |
| `WATCHER_INJECT_USER` | `[Session Rotated]\n\n{summary}` | First user message injected into the new session (`{summary}` is replaced) |
| `WATCHER_INJECT_ASSISTANT` | `Understood. I have the context...` | Assistant's reply to the inject |

### Tuning the token threshold

The default thresholds (rotate at 250k, split at 200k) are designed for **1M-context models** (e.g. `claude-opus-4-6[1m]`). If you're on a smaller context window, you **must** adjust these:

| Context window | Suggested `TOKEN_THRESHOLD` | Suggested `KEEP_TOKEN_THRESHOLD` |
|---|---|---|
| 1M | 250,000 | 200,000 |
| 200k | 150,000 | 100,000 |
| 128k | 90,000 | 60,000 |

The general idea: rotate well before the hard limit, and keep enough recent context (~50k–75k tokens) so the new session doesn't lose thread.

### Recommended: disable adaptive thinking

Claude Code has adaptive thinking that may skip extended thinking on some turns. For long-running sessions where you want consistent reasoning, disable it via `WATCHER_CLAUDE_ENV`:

```bash
export WATCHER_CLAUDE_ENV="CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1"
```

### Injecting custom context on rotation

If you have a persistent memory/knowledge system (e.g. markdown files, a knowledge base, or anything you want Claude to "wake up" with), you can inject it into every new session by customizing the inject message.

**Simple approach** — set `WATCHER_INJECT_USER` to prepend your context before the summary:

```bash
export WATCHER_INJECT_USER="[System Context]

$(cat /path/to/your/context.md)

---

[Previous conversation summary]

{summary}"
```

**Programmatic approach** — for dynamic context (multiple files, API calls, etc.), modify `forge_session()` in the source. The injection point is the first user+assistant message pair in the new session. Here's the pattern:

```python
# In forge_session(), before building inject_content, load your context:
def load_custom_context():
    """Load context files you want injected on every rotation."""
    parts = []
    context_dir = "/path/to/your/context/"
    for fname in sorted(os.listdir(context_dir)):
        path = os.path.join(context_dir, fname)
        if os.path.isfile(path) and path.endswith(".md"):
            with open(path, "r") as f:
                parts.append(f.read().strip())
    return "\n\n---\n\n".join(parts)

# Then in forge_session():
custom_context = load_custom_context()
inject_content = f"[Session Rotated]\n\n{custom_context}"
if summary_text:
    inject_content += f"\n\n---\n\n[Summary]\n\n{summary_text}"
```

This is useful for things like persona prompts, project-specific instructions, or any "always-on" context that should survive across rotations.

## Usage

```bash
# Set your API key
export WATCHER_SUMMARY_API_KEY="sk-..."
export WATCHER_PROJECT_DIR="/path/to/your/project"

# Start the watcher daemon (runs in a tmux session)
./start_watcher.sh

# Or run directly
python3 session_watcher.py

# CLI: prepare a rotated session from the current active one
python3 session_watcher.py prepare
```

## Requirements

- Python 3.10+
- `httpx` (`pip install httpx`)
- `tmux`
- Claude Code CLI

## How rotation works

```
Active session hits 250k tokens
  → Split at ~200k boundary (backs up to nearest user message)
  → Old half → LLM summarization
  → New session JSONL = [summary inject] + [recent ~50k messages]
  → Kill old Claude, restart with --resume <new_session_id>
  → Old session marked as .rotated_* (never picked up again)
```
