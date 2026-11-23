# Installation details and launchd daemon

## Installation

### Prerequisites

```bash
pip install github-copilot-sdk slack-bolt aiohttp python-dotenv pyyaml weasyprint pymupdf pillow openai-whisper markdown nh3 fastapi "uvicorn[standard]" itsdangerous "mcp<2"
```

`mcp<2` (the MCP Python SDK, pinned below the 2.x `FastMCP`→`MCPServer` rename)
backs the in-process MCP server mounted at `/mcp` — see `mcp_tools.py` and
the MCP server block in `web_server.py`.

WeasyPrint (used for table-image rendering) needs native system libraries — on macOS install once via Homebrew:

```bash
brew install cairo pango gdk-pixbuf libffi ffmpeg
```

`openai-whisper` (used for local voice-message transcription) requires the `ffmpeg` CLI (installed above) and works on any macOS (Apple Silicon or Intel). Its model weights are downloaded from OpenAI's official Azure CDN on first use (needs internet once, then fully offline) — deliberately not HuggingFace, since some corporate networks block it.

Node.js with Copilot CLI must be installed (used by the SDK internally):

```bash
# Check
copilot --version   # should print v1.x
```

### Configuration

```bash
cp dop-agent/.env.example dop-agent/.env
```

Edit `.env`:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ALLOWED_USER_ID=UXXXXXXXXXX      # your Slack user ID
COPILOT_GITHUB_TOKEN=github_pat_...
# Set this to the absolute path of your working directory the agent starts in (any folder you want it to operate on).
COPILOT_WORKDIR=/absolute/path/to/workdir
SESSION_TIMEOUT=120
```

> **Finding your Slack User ID**: leave `ALLOWED_USER_ID` empty on first run, send a DM to the bot, look at the log — it will print `Message from user_id=UXXXXXXX`.

### Run manually (for testing)

```bash
cd dop-agent
python3 -u dop_agent.py
```

The bot loads `dop-agent/.env` itself; logs go to stdout/stderr.


## launchd Daemon (auto-start on login)

The bot runs as a launchd user agent — starts on login, restarts automatically on crash.

### Install

```bash
cd dop-agent
./bot_ctl.sh install
./bot_ctl.sh start
```

`install` creates `~/Library/LaunchAgents/com.dop-agent.plist` using this
checkout's absolute path and the current user's home, Python 3.13, and `PATH`.
The checked-in plist is a template; do not copy it directly. Credentials are
loaded from `dop-agent/.env` or `~/.dop-agent/config.yaml` by the bot and are
not written into the installed plist. Before starting, install the Python
prerequisites above and configure one of those files.

### Manage

Use `dop-agent/bot_ctl.sh` instead of raw `launchctl`:

```bash
dop-agent/bot_ctl.sh status    # running? pid? + last 10 log lines
dop-agent/bot_ctl.sh restart   # atomic `launchctl kickstart -k` (safe to run from inside the bot), waits for a NEW pid, tails startup banner
dop-agent/bot_ctl.sh start     # bootstrap if stopped; verify startup
dop-agent/bot_ctl.sh stop      # unload from launchd and wait for stop
dop-agent/bot_ctl.sh logs      # tail -f the error log
```

Raw `launchctl` still works if needed directly:

```bash
launchctl list | grep dop-agent
launchctl bootout gui/$(id -u)/com.dop-agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dop-agent.plist
```

### Logs

```bash
dop-agent/bot_ctl.sh logs              # equivalent to the tail below
tail -f ~/.copilot/dop-agent-error.log   # main log (stdout+stderr)
tail -f ~/.copilot/dop-agent.log         # usually empty
```
