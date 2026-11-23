"""
Bot-wide configuration: environment variables, paths, constants, and
startup-time initialization (dotenv loading, logging setup, MCP server
config loading, message_store DB init).

This module is imported first (before web_server, which needs load_dotenv()
to have already run so its own WEB_UI_* env vars are picked up), and has
import-time side effects by design:
  1. load_dotenv() so all subsequent os.getenv() calls (here and in
     web_server.py) see .env values.
  2. message_store.init_db() so the bot_store.db schema exists before
     any other module tries to use it.
  3. logging.basicConfig(...) + the shared `log` logger used across the bot.
  Settings from config.py are read into module constants.
  4. MCP server config loading (mcp-config.json -> MCP_SERVERS).

Like render_tables.py / render_markdown.py, this module does its own
optional-dependency detection (WeasyPrint / PyMuPDF / Pillow may not be
installed) rather than importing MISSING_IMAGE_DEPS from those modules,
since this check must run before TABLE_RENDER_MODE's fallback-to-ascii
decision, independent of which render module ends up being used.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

try:
    import fitz  # PyMuPDF
except ModuleNotFoundError:
    fitz = None

try:
    from PIL import Image, ImageChops
except ModuleNotFoundError:
    Image = None
    ImageChops = None

if Image is not None:
    Image.MAX_IMAGE_PIXELS = None

try:
    from weasyprint import HTML as WeasyHTML
except ModuleNotFoundError:
    WeasyHTML = None

import config  # noqa: E402
import message_store  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")

SLACK_BOT_TOKEN = config.secret_value("adapters.slack.bot_token")
SLACK_APP_TOKEN = config.secret_value("adapters.slack.app_token")
ALLOWED_USER_ID = config.value("adapters.slack.allowed_user_id")
COPILOT_WORKDIR = config.workdir()
SESSION_TIMEOUT = config.value("agent.session_timeout_s")

# How often (seconds) the background watcher loop (watchers.watcher_loop())
# re-checks active watches. self_restart watches don't use this loop; they
# are resolved once at startup, before the loop starts (see main()).
WATCHER_POLL_INTERVAL = config.value("watchers.tick_s")

# Upper bound (seconds) for a single blocking integration client fetch made
# from a watch poll — a safety net on top of each HTTP client's own
# connect/read timeout, in case a broken DNS/VPN state hangs longer. Every
# such fetch goes through watchers._fetch_with_timeout().
#
# Applies only to the fetch, NOT the whole poll function: a poll also injects
# a watch message when something changed, which starts a full, open-ended
# Copilot agent turn that this ceiling must not cancel.
WATCH_POLL_TIMEOUT = config.value("watchers.fetch_timeout_s")

# Where the human-readable "all active watches" snapshot is (re)written on
# every watcher_loop tick — gitignored, purely a local convenience file (see
# _write_watchers_md() in watchers.py).
WATCHERS_MD_PATH = Path(__file__).parent / "WATCHERS.md"

# Sentinel returned by CopilotConversation.send() when the in-progress turn
# was aborted because a newer message arrived for the same conversation.
# Callers should treat this as "no response to show" rather than an error.
INTERRUPTED = object()

# Slack's Block Kit "section" text object caps at 3000 chars — smaller than
# SLACK_MAX_CHARS (which applies to the plain "text" fallback field), so the
# live "Working..." status message (which uses a section block + Stop
# button) needs its own, tighter truncation limit.
SLACK_BLOCK_MAX_CHARS = 2900

# Max number of recent status lines kept in the live "Working..." progress message
# (older lines are dropped as new ones arrive, so the message stays readable and
# under Slack's char limit while still showing more than just the latest action).
_STATUS_HISTORY_MAX = 12

# How often (seconds) to refresh the "Working..." status message even when no
# new tool-call events have arrived, purely to advance its elapsed/idle-time
# header. Without this heartbeat, a genuinely stuck turn would leave the last
# real event's timestamp frozen forever with no visual difference from a turn
# that is merely slow between steps — the heartbeat is what makes staleness
# actually visible (the idle counter stops climbing only once the whole bot
# process itself has died, at which point Slack simply stops receiving edits).
_STATUS_HEARTBEAT_SECS = 15

# Root directory of per-session state (session.db, events.jsonl, etc.),
# mirroring the same "~/.copilot/session-state/<session_id>/session.db"
# layout used by this very tool's own todos/todo_deps tables. Copilot's own
# agent sessions (the ones this bot drives) use the identical sqlite schema
# under their own session_id — read directly from there to show todo
# progress, no SDK/RPC support for it exists yet.
SESSION_STATE_DIR = Path.home() / ".copilot" / "session-state"

# Emoji shown per todo status in the checklist prepended to the live
# "Working..." status message (see CopilotConversation._read_session_todos in
# copilot_session.py).
_TODO_STATUS_ICON = {
    "done": ":white_check_mark:",
    "in_progress": ":arrows_counterclockwise:",
    "pending": ":white_large_square:",
    "blocked": ":no_entry_sign:",
}

# "image" (default) renders Markdown tables as PNG images via WeasyPrint
# (HTML/CSS -> PDF -> rasterized PNG, cropped to content). "ascii" renders
# box-drawing monospace code blocks instead.
TABLE_RENDER_MODE = config.value("agent.table_render_mode").lower()

# openai-whisper model name used to transcribe voice/audio messages. Weights
# are downloaded on first use and cached under ~/.cache/whisper.
WHISPER_MODEL = config.value("agent.transcription.whisper_model")

# Default behavior once a voice/audio message finishes transcribing:
# true (default) — forward the transcript to the Copilot session
# automatically; the placeholder offers ":eye: Preview before sending" to opt
# out for that one message (manual ":arrow_right: Send to agent" flow).
# false — wait for a manual "Send to agent" click; the placeholder offers
# ":zap: Auto-send when ready" to opt in for that message.
AUDIO_AUTO_SEND = config.value("agent.transcription.audio_auto_send")

# Bot-owned sqlite store (bot_store.db) keeps the conversation -> Copilot
# session mapping and the Web UI transcript/source-of-truth mirror.
message_store.init_db()

# The Copilot Python SDK does not automatically load the user's global
# ~/.copilot/mcp-config.json for programmatically-created sessions (unlike
# the interactive `copilot` CLI, which always loads it). We read it once at
# startup and pass its "mcpServers" entries explicitly to create_session()/
# resume_session() so integration MCP tools are available here too.
MCP_CONFIG_FILE = Path.home() / ".copilot" / "mcp-config.json"

# Same gap for skills: create_session()/resume_session() default to
# enable_skills=None (resolved to "off" by the SDK/CLI), whereas the
# interactive `copilot` CLI auto-discovers ".github/skills" from the cwd. We
# pass the skill directories explicitly so project skills are available to
# bot-driven sessions too.
SKILLS_DIR = Path(COPILOT_WORKDIR) / ".github" / "skills"
# Skills bundled with dop-agent (the *-format UI widgets): always available,
# regardless of which workdir the bot runs in.
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# Skills rendered per enabled package-driver instance (integrations/skills.py), refreshed before every session.
GENERATED_SKILLS_DIR = Path.home() / ".dop-agent" / "skills-gen"
SKILL_DIRECTORIES = list(dict.fromkeys(str(d) for d in (SKILLS_DIR, BUNDLED_SKILLS_DIR, GENERATED_SKILLS_DIR)))


def missing_image_render_deps() -> list[str]:
    """Return missing optional packages needed for PNG rendering features."""
    missing: list[str] = []
    if fitz is None:
        missing.append("PyMuPDF (fitz)")
    if Image is None or ImageChops is None:
        missing.append("Pillow")
    if WeasyHTML is None:
        missing.append("WeasyPrint")
    return missing


MISSING_IMAGE_DEPS = missing_image_render_deps()
# Underscore-prefixed alias of MISSING_IMAGE_DEPS.
_MISSING_IMAGE_DEPS = MISSING_IMAGE_DEPS


def _load_mcp_servers() -> dict | None:
    """Load MCP server definitions from ~/.copilot/mcp-config.json, converted
    to the SDK's MCPServerConfig wire format (drops the "type": "local" key,
    which the SDK config calls "local"/"stdio" -- both are accepted)."""
    logger = logging.getLogger("dop-agent")
    if not MCP_CONFIG_FILE.exists():
        return None
    try:
        raw = json.loads(MCP_CONFIG_FILE.read_text())
    except Exception:
        logger.warning("Could not parse %s — starting without MCP servers", MCP_CONFIG_FILE, exc_info=True)
        return None
    servers = raw.get("mcpServers") or {}
    if not servers:
        return None
    logger.info("Loaded MCP servers from %s: %s", MCP_CONFIG_FILE, ", ".join(servers))
    return servers


# Loopback URL/token for this bot's own in-process MCP server (mounted at
# /mcp by web_server.py, exposing watch_*/session_* — see mcp_tools.py).
# Duplicated here (rather than importing web_server.WEB_UI_PORT/
# MCP_AUTH_TOKEN) specifically so copilot_session.py — a low-level session
# lifecycle module — never has to import the FastAPI app module just to
# build its own mcp_servers dict; both computations read the same env vars
# and must be kept in sync if either changes.
_SELF_MCP_WEB_UI_PORT = config.value("adapters.web.port")
_SELF_MCP_AUTH_TOKEN = config.secret_value("mcp.auth_token")


def _self_mcp_server_entry() -> dict | None:
    """This bot's own in-process /mcp server, in the SDK's MCPHTTPServerConfig
    wire format — or None if no auth token is configured (MCP_AUTH_TOKEN
    unset), since every request would be refused by
    _McpAuthMiddleware anyway and there's no point wiring a server that can
    never authenticate."""
    if not _SELF_MCP_AUTH_TOKEN:
        return None
    return {
        "type": "http",
        "url": f"http://127.0.0.1:{_SELF_MCP_WEB_UI_PORT}/mcp",
        "headers": {"Authorization": f"Bearer {_SELF_MCP_AUTH_TOKEN}"},
        "tools": ["*"],
    }


def _build_session_mcp_servers(external: dict | None) -> dict:
    """Merge the externally-configured MCP servers (~/.copilot/mcp-config.json
    — Atlassian, etc.) with this bot's own self-hosted
    /mcp server, for handing to a bot-spawned Copilot CLI session's
    create_session/resume_session(mcp_servers=...). Deliberately NOT merged
    into MCP_SERVERS itself (that dict is also used elsewhere — e.g.
    probe_mcp_server_statuses() and /api/health — to describe only the
    externally-configured servers actually loaded from that JSON file, not
    this synthetic addition). Never mutates ``external`` in place."""
    merged = dict(external or {})
    self_entry = _self_mcp_server_entry()
    if self_entry is not None:
        merged["dop-agent"] = self_entry
    return merged


def probe_mcp_server_statuses() -> list[dict]:
    """Best-effort, secret-free status summary of every MCP server this bot
    knows about, for the Web UI's MCP health hover popover.

    Two distinct populations, both surfaced together since a user hovering
    the single "MCP" strip icon wants the full picture in one place:
      1. Every entry from ``~/.copilot/mcp-config.json`` (MCP_SERVERS) — the
         servers a bot-spawned Copilot CLI session gets access to (
         Atlassian Jira/Confluence, etc.). These are stdio subprocesses
         spawned fresh per Copilot session, not long-lived connections this
         process holds — so a real handshake/liveness probe would mean
         spawning a throwaway subprocess per health poll (expensive, and
         side-effect-prone for some servers). Instead this does a *static*
         sanity check only: does the configured command resolve on PATH (or
         exist as an absolute path with the exec bit set), and do any
         file-path-shaped args actually exist on disk. That's enough to
         catch the most common real failure mode (a moved/renamed script,
         a node/npx binary that no longer resolves) without the cost/risk
         of actually launching each one.
      2. This process's own in-process MCP server (mounted at ``/mcp`` by
         web_server.py) — status here IS a real, direct fact about this
         very process (whether MCP_AUTH_TOKEN is configured,
         i.e. whether the endpoint will accept ANY authenticated request at
         all), not a static file-path guess.

    Deliberately returns only {name, type, status, detail} — never command
    args or the `env` block, since mcp-config.json entries carry real API
    tokens (API tokens, etc.) that must never reach the
    browser, even over the authenticated Web UI.

    status is one of: "ok", "warning", "error", "unknown".
    """
    import shutil

    results: list[dict] = []

    for name, cfg in sorted((MCP_SERVERS or {}).items()):
        server_type = (cfg or {}).get("type", "local")
        if server_type not in ("local", "stdio"):
            # Remote/http transport — no cheap static check available.
            results.append({
                "name": name,
                "type": server_type,
                "status": "unknown",
                "detail": "remote transport — liveness not checked",
            })
            continue

        command = (cfg or {}).get("command", "")
        args = (cfg or {}).get("args") or []
        problems = []

        if not command:
            problems.append("no command configured")
        elif os.path.isabs(command):
            if not (os.path.isfile(command) and os.access(command, os.X_OK)):
                problems.append(f"command not found or not executable: {command}")
        elif shutil.which(command) is None:
            problems.append(f"command not found on PATH: {command}")

        for arg in args:
            if isinstance(arg, str) and os.path.isabs(arg) and arg.rsplit(".", 1)[-1] in ("js", "py", "mjs", "cjs"):
                if not os.path.isfile(arg):
                    problems.append(f"script not found: {arg}")

        if problems:
            results.append({
                "name": name,
                "type": server_type,
                "status": "error",
                "detail": "; ".join(problems),
            })
        else:
            results.append({
                "name": name,
                "type": server_type,
                "status": "ok",
                "detail": f"{command} {' '.join(str(a) for a in args)}".strip(),
            })

    # This process's own in-process MCP server (see web_server.py) — a
    # direct fact about this running process, not a guess.
    self_token_configured = bool(
        config.secret_value("mcp.auth_token")
    )
    results.append({
        "name": "dop-agent (this bot, self-hosted at /mcp)",
        "type": "http",
        "status": "ok" if self_token_configured else "error",
        "detail": (
            "watch_*/session_* tools, loopback-only"
            if self_token_configured
            else "MCP_AUTH_TOKEN not set — refusing all /mcp requests"
        ),
    })

    return results


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dop-agent")
logging.getLogger("weasyprint").setLevel(logging.WARNING)
logging.getLogger("fontTools").setLevel(logging.WARNING)

if TABLE_RENDER_MODE == "image" and MISSING_IMAGE_DEPS:
    log.warning(
        "TABLE_RENDER_MODE=image requested, but optional rendering dependencies are missing (%s). "
        "Falling back to TABLE_RENDER_MODE=ascii.",
        ", ".join(MISSING_IMAGE_DEPS),
    )
    TABLE_RENDER_MODE = "ascii"

MCP_SERVERS = _load_mcp_servers()

# What bot-spawned Copilot CLI sessions actually get handed as mcp_servers=
# (copilot_session.py) — every externally-configured server (MCP_SERVERS)
# plus this bot's own self-hosted /mcp server (watch_*/session_* tools), so
# those sessions can call them. Deliberately a separate dict from
# MCP_SERVERS: this session's own MCP client config (~/.copilot/
# mcp-config.json) intentionally does NOT include the bot's self-hosted
# server (it would then be reachable by every local CLI session, not just
# ones spawned by this bot), and MCP_SERVERS is also read as-is by
# probe_mcp_server_statuses()/the Web UI health popover to describe only the
# externally-configured servers.
SESSION_MCP_SERVERS = _build_session_mcp_servers(MCP_SERVERS)

# Copilot CLI's built-in MCP servers (GitHub MCP etc.) are never wanted in
# bot sessions: GitHub work goes through the github-* driver skills. Passed
# as disabled_mcp_servers= to create_session/resume_session.
DISABLED_BUILTIN_MCP_SERVERS = ["github-mcp-server", "githubiq"]
