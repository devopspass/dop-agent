"""
Local, password-protected web UI for the Copilot Slack bot.

Runs as an asyncio task inside the SAME process as dop_agent.py (started
from its main()), sharing the in-memory `_convos` dict and the Slack
`AsyncApp` client — no IPC, no second Copilot session manager.

Data sources:
  - dop-agent/bot_store.db         -> bot-owned conversation/message store
  - dop-agent/session_titles.json  -> optional custom title overrides

Live updates use Server-Sent Events: every durable, user-visible message is
recorded in bot_store.db, and message_store notifies this module whenever a
conversation changes so open browsers refresh the session list and the
active thread immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import config
import secrets
import subprocess
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import bot_config
import copilot_session
import audio_store
import auto_title
import handlers
import mcp_tools
import groups as session_groups
import message_kinds
import message_store
import model_routing
import transcription
import usage_index
import adapters
from adapters import BroadcastOrigin, ConversationBroadcaster, WebAdapter
import watchers

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger("web_server")

WEBUI_DIR = Path(__file__).parent / "webui"

WEB_UI_HOST = config.value("adapters.web.host")
WEB_UI_PORT = config.value("adapters.web.port")
# Hostname used when generating links back into the Web UI (session_create's
# web_url, watch-fire links, etc.); separate from WEB_UI_HOST (the bind
# address). A different host is a different origin with its own cookie jar,
# and webui/app.js's parseOwnSessionLink() is origin-scoped, so a link whose
# host differs from the one the browser uses opens a new tab at /login.
# Defaults to WEB_UI_HOST; set it when browsing via another hostname (e.g.
# "localhost" while bound to "127.0.0.1").
WEB_UI_PUBLIC_HOST = config.value("adapters.web.public_host") or WEB_UI_HOST
WEB_UI_PASSWORD = config.secret_value("adapters.web.password")
# When false, the browser frontend (/, /login, /static/*) is disabled (404)
# while /api/*, the SSE streams, and /mcp stay live — e.g. for a headless
# deployment that only talks to the bot via the MCP tools or the API.
WEB_UI_FRONTEND_ENABLED = config.value("adapters.web.frontend_enabled")
# Bearer token gating the in-process MCP server at /mcp (see below).
# Deliberately NO fallback to WEB_UI_PASSWORD: /mcp must use its own strong secret.
MCP_AUTH_TOKEN = config.secret_value("mcp.auth_token")


def _build_allowed_hosts() -> frozenset[str]:
    """Host header values accepted by every route (DNS-rebinding defence).
    Loopback names on WEB_UI_PORT plus optional extras from WEB_UI_ALLOWED_HOSTS
    (comma-separated, exact host[:port] strings)."""
    hosts = {f"{h}:{WEB_UI_PORT}" for h in ("127.0.0.1", "localhost", "[::1]")}
    hosts.update(h.strip().lower() for h in config.value("adapters.web.allowed_hosts") if h.strip())
    return frozenset(hosts)


ALLOWED_HOSTS = _build_allowed_hosts()

_ENV_FILE = Path(__file__).parent / ".env"


def _ensure_persisted_secret_key() -> str:
    """Return WEB_UI_SECRET_KEY from the environment, generating AND
    persisting a new one to .env on first run if it's unset, so login
    sessions survive bot restarts.
    """
    existing = config.secret_value("adapters.web.secret_key")
    if existing:
        return existing

    new_key = secrets.token_hex(32)
    try:
        lines = _ENV_FILE.read_text().splitlines() if _ENV_FILE.exists() else []
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("WEB_UI_SECRET_KEY="):
                lines[i] = f"WEB_UI_SECRET_KEY={new_key}"
                found = True
                break
        if not found:
            lines.append(f"WEB_UI_SECRET_KEY={new_key}")
        _ENV_FILE.write_text("\n".join(lines) + "\n")
        log.info("Generated and persisted a new WEB_UI_SECRET_KEY to .env")
    except Exception:
        log.exception(
            "Could not persist a generated WEB_UI_SECRET_KEY to .env — "
            "sessions will still be invalidated on every restart until "
            "WEB_UI_SECRET_KEY is set manually"
        )
    os.environ["WEB_UI_SECRET_KEY"] = new_key
    return new_key


WEB_UI_SECRET_KEY = _ensure_persisted_secret_key()

# Captured at module import (close to process start); drives the /api/health uptime.
_START_TIME = time.time()


def _bot_version() -> str:
    """Best-effort short git commit hash of the working tree, used as a
    lightweight "version" identifier for the health strip since this repo
    has no formal release/version scheme. Falls back to "unknown" if git
    isn't available or this isn't a git checkout (e.g. some test setups)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


_BOT_VERSION = _bot_version()

_COOKIE_NAME = "copilot_webui_session"
# Signed-cookie lifetime (12h), enforced via URLSafeTimedSerializer's max_age in _current_user().
_COOKIE_MAX_AGE = 60 * 60 * 12
_serializer = URLSafeTimedSerializer(WEB_UI_SECRET_KEY, salt="copilot-webui-auth")
_SSE_KEEPALIVE_SECS = 15

# Login rate-limiting: in-memory per-IP failure counter (per-process, not persisted);
# only meant to prevent unlimited password guessing if bound beyond localhost.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECS = 15 * 60  # 15 minutes
_login_failures: dict[str, list[float]] = defaultdict(list)


# Comma-separated peer IPs whose X-Forwarded-For / X-Forwarded-Proto are honoured.
# Empty (default): forwarded headers are ignored, throttle buckets are per socket peer.
_TRUSTED_PROXIES = frozenset(
    p.strip() for p in os.environ.get("WEB_UI_TRUSTED_PROXIES", "").split(",") if p.strip()
)


def _from_trusted_proxy(request: Request) -> bool:
    return bool(request.client and request.client.host in _TRUSTED_PROXIES)


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _from_trusted_proxy(request):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip() or peer  # rightmost = added by our proxy
    return peer


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return _from_trusted_proxy(request) and request.headers.get("x-forwarded-proto", "").lower() == "https"


def _prune_login_failures(ip: str) -> list[float]:
    """Drop failure timestamps older than the rate-limit window and return
    the remaining (still-counting) ones for `ip`."""
    cutoff = time.time() - _LOGIN_WINDOW_SECS
    attempts = [t for t in _login_failures.get(ip, []) if t >= cutoff]
    _login_failures[ip] = attempts
    return attempts


_slack_client = None
_handle_fn: Callable[..., Awaitable[None]] | None = None
_convos: dict | None = None
_is_authorized: Callable[[str], bool] | None = None
_allowed_user_id: str = ""
_initialized = False

_session_subscribers: set[asyncio.Queue[bool]] = set()
_conversation_subscribers: dict[str, set[asyncio.Queue[bool]]] = defaultdict(set)
_webui_adapter: WebAdapter | None = None
_broadcaster: ConversationBroadcaster | None = None

# --------------------------------------------------------------------------
# Link unfurling (P3 feature) - GET /api/unfurl?url=...
#
# Recognizes URLs of configured integrations (see furl_api / the registry) pasted
# or rendered in a message, and returns a small JSON summary (title, status, etc.)
# for the frontend to render as a preview card.
# --------------------------------------------------------------------------

import furl_api

# Tiny TTL cache (TTL: config unfurl.cache_ttl_s) so re-rendering a transcript doesn't re-hit
# the integration for a link looked up seconds ago. Keyed by raw URL (or `watch:<id>:<key>`).
_unfurl_cache: dict[str, tuple[float, dict]] = {}


def _fetch_watch_preview_sync(watch: dict) -> dict:
    """Live title/url/status for one watch's resource via the integration registry
    ({"type": None} = nothing to show: no resource, '@ref' keys, any failure)."""
    return furl_api.watch_preview(watch)


class LoginBody(BaseModel):
    password: str


class SendBody(BaseModel):
    conversation_id: str | None = None
    text: str


class PinBody(BaseModel):
    pinned: bool


class ArchiveBody(BaseModel):
    archived: bool


class GroupBody(BaseModel):
    title: str | None = None
    color: str | None = None
    emoji: str | None = None
    description: str | None = None


class AssignGroupBody(BaseModel):
    group_id: str | None = None


class GroupMasterBody(BaseModel):
    conversation_id: str | None = None


class SuggestGroupBody(BaseModel):
    prompt: str


class PinOrderBody(BaseModel):
    conversation_ids: list[str]


class GroupOrderBody(BaseModel):
    conversation_ids: list[str]


class TitleBody(BaseModel):
    title: str | None = None


class NoteBody(BaseModel):
    note: str = ""


class BookmarkBody(BaseModel):
    bookmarked: bool = True


class TranscriptActionBody(BaseModel):
    action_id: str


# ---------------------------------------------------------------------------
# In-process MCP server — mounted at /mcp below, in the same FastAPI app/
# process (no separate process or extra port). Tool families:
#   - watch_*: thin wrappers around mcp_tools.py's pure functions, managing
#     watchers.py watches.
#   - session_search/session_get/session_list: wrappers over mcp_tools.py,
#     read-only over past Copilot session history (global
#     ~/.copilot/session-store.db and bot_store.db).
#   - session_create/session_send: implemented here, not in mcp_tools.py,
#     because they spawn/continue a live Copilot turn (send_message(),
#     _handle_fn, _broadcaster, _slack_client runtime state).
# Intended consumer: Copilot sessions run by this bot, never an external
# client — see _McpAuthMiddleware below.
# ---------------------------------------------------------------------------

class _DynamicFastMCP(FastMCP):
    """Re-renders integration-dependent tool descriptions on every tools/list so
    enabling/disabling an integration in settings is reflected without a restart."""

    async def list_tools(self):
        tool = self._tool_manager.get_tool("watch_add")
        if tool is not None:
            tool.description = _watch_add_description()
        return await super().list_tools()


mcp_server = _DynamicFastMCP(
    "dop-agent",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(ALLOWED_HOSTS),
        allowed_origins=sorted(f"http://{h}" for h in ALLOWED_HOSTS),
    ),
)


def _watch_add_description() -> str:
    try:
        types = watchers.watch_types_doc()
    except Exception:
        types = "time, self_restart, session_turn, script"
    return (
        "Register a watch that wakes THIS session (pass your own conversation_id from the "
        "session context as session_id) on an external change. Valid types (from the enabled integrations + "
        f"builtins): {types}. meta.integration pins "
        "one integration instance when several accept the same key. session_turn 'self' fires when "
        "this turn ends (auto-resumes at most 10 times - meta max_chain=N to raise, recurring=True "
        "to re-arm). A time watch with meta={\"script\",\"args\",\"wake_if\"} polls a script from "
        "dop-agent/scripts/watch/ without waking the model (see watch_script)."
    )


@mcp_server.tool(description=_watch_add_description())
def watch_add(
    type: str,
    session_id: str,
    key: str | None = None,
    label: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Register a watch that wakes THIS session (pass your own
    conversation_id from the session context as session_id) on an external change.
    type/key: see the tool description for the types currently enabled.
    session_turn 'self' fires when this turn ends; auto-resumes at most
    10 times - meta max_chain=N to raise, recurring=True to re-arm.
    A time watch with meta={"script","args","wake_if"} polls a script from
    dop-agent/scripts/watch/ without waking the model (see watch_script)."""
    return mcp_tools.watch_add(type, session_id, key=key, label=label, meta=meta)


@mcp_server.tool()
def watch_script(
    session_id: str,
    script: str,
    schedule: str,
    prompt: str,
    args: list[str] | None = None,
    retries: int = 0,
    until_changed: bool = False,
    max_runs: int | None = None,
    expires_in: str | None = None,
    run_immediately: bool = True,
    dry_run: bool = False,
    watcher: str | None = None,
    integration: str | None = None,
) -> dict:
    """Token-free polling: run an allow-listed read-only script on a schedule and
    wake THIS conversation only when its exit code says so. Exit codes: 0 = nothing
    (silent), 200 = changed (wake with output + `prompt`), anything else = error
    (wake and stop the watch after `retries` consecutive failures; you decide what
    to do next). Output is truncated in the wake message; the full text is in the
    file path it names. The script keeps its own previous-state files in the dir
    given by env WATCH_STATE_DIR. schedule: 'cron:<5-field cron, local time>'
    (e.g. 'cron:*/10 * * * *'), 'in:30m' or 'at:2026-10-20T09:00'. Scripts live in
    dop-agent/scripts/watch/ or ~/.dop-agent/watch-plugins/ (chmod 755, read-only
    by convention, tokens read from files, never args). Plugin-dir scripts must pass
    dry_run=True first (show the user the output). Optional: until_changed (stop
    after the first change), max_runs, expires_in ('7d'), run_immediately (default
    True: first run now, e.g. to record a baseline). Integration packages (e.g. Teams) ship their scripts and
    recipes: pass watcher=<recipe id> (icon in the watchers list + the recipe's leading args) and
    integration=<instance name> (that instance's account/CDP env); see the integration's skill."""
    return mcp_tools.watch_script(session_id, script, schedule, prompt, args=args, retries=retries,
                                  until_changed=until_changed, max_runs=max_runs, expires_in=expires_in,
                                  run_immediately=run_immediately, dry_run=dry_run,
                                  watcher=watcher, integration=integration)


@mcp_server.tool()
def watch_list(
    session_id: str | None = None,
    include_inactive: bool = False,
    full: bool = False,
    fields: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """List registered watches (active/paused; include_inactive adds done).
    Compact rows by default (id,type,key,label[:80],status,session_id,
    next_fire_at,last_state[:60]); full=True = every column, fields=[..]
    picks columns, limit caps rows. Scope with session_id. Broken watches (key/meta no longer valid) carry a broken_error field."""
    return mcp_tools.watch_list(
        session_id=session_id, include_inactive=include_inactive,
        compact=not full, fields=fields, limit=limit,
    )


@mcp_server.tool()
def watch_get(watch_id: int) -> dict | None:
    """Get full detail (key, last-fired state, status) for one watch by
    id — for debugging 'why didn't this notify me' before touching the
    underlying target."""
    return mcp_tools.watch_get(watch_id)


@mcp_server.tool()
def watch_pause(ids: int | list[int], force: bool = False) -> dict:
    """Pause one watch id or a list of ids (e.g. 'stop watching this for
    now'). Skips cron:* / #251-255 unless force. Returns
    {done:[ids], skipped:[{id,reason}]}."""
    return mcp_tools.watch_pause(ids, force=force)


@mcp_server.tool()
def watch_resume(ids: int | list[int]) -> dict:
    """Resume one previously paused watch id or a list of ids. Returns
    {done:[ids], skipped:[{id,reason}]}."""
    return mcp_tools.watch_resume(ids)


@mcp_server.tool()
def watch_remove(ids: int | list[int], force: bool = False) -> dict:
    """Permanently delete one watch id or a list of ids once their purpose
    is done. Skips cron:* / #251-255 unless force. Returns
    {done:[ids], skipped:[{id,reason}]}."""
    return mcp_tools.watch_remove(ids, force=force)


@mcp_server.tool()
def watch_pause_session(session_id: str) -> dict:
    """Pause all active watches registered by one session."""
    return mcp_tools.watch_pause_session(session_id)


@mcp_server.tool()
def session_search(query: str, limit: int = 20) -> list[dict]:
    """Search past Copilot session content (CLI turns/checkpoints AND
    rendered bot Slack messages) by keyword/topic to answer 'find/search a
    session where I did X' — e.g. 'find the session where I ran the gather
    script for all envs' or 'search sessions for when I set Remote Edge as
    default browser'. Returns matching session ids with timestamp,
    repo/cwd, bot thread (if any), and a snippet."""
    return mcp_tools.session_search(query, limit=limit)


@mcp_server.tool()
def session_get(
    session_id: str,
    full: bool = False,
    turns: int = 3,
    sections: list[str] | None = None,
    max_chars: int = 1500,
) -> dict | None:
    """Detail for one session found via session_search. Compact by default:
    summary, last `turns` turns (text cut to max_chars), checkpoint titles,
    touched files, refs, last 6 bot messages; `truncated` shows what was
    cut and where the raw log lives on disk (~/.copilot/session-state/<id>/
    events.jsonl - grep it). full=True = everything; sections=[turns|
    checkpoints|files|refs|messages] limits what is returned."""
    return mcp_tools.session_get(session_id, compact=not full, last_turns=turns, sections=sections, max_chars=max_chars)


@mcp_server.tool()
def session_list(
    limit: int = 50, include_archived: bool = False, group_id: str | None = None, full: bool = False
) -> list[dict]:
    """List conversations (compact: id, title, updated_at, group, flags;
    full=True adds preview/thread/read state). group_id filters to one
    group ("none" = ungrouped)."""
    return mcp_tools.session_list(limit=limit, include_archived=include_archived, group_id=group_id, compact=not full)


@mcp_server.tool()
def group_list(include_archived: bool = False) -> list[dict]:
    """List session groups (workstreams) with member counts, master id and a
    status rollup (errored/running/unread/idle). A session is in at most one
    group; sessions in none are 'ungrouped'."""
    return session_groups.groups_payload(_session_payload(), include_archived=include_archived)


@mcp_server.tool()
def group_manage(
    action: str,
    group_id: str | None = None,
    session_id: str | None = None,
    title: str | None = None,
    color: str | None = None,
    emoji: str | None = None,
    description: str | None = None,
    make_master: bool = False,
    archived: bool = True,
) -> dict:
    """Mutate session groups. action: create(title,[color,emoji,description]);
    update(group_id + any of title/color/emoji/description); assign(session_id,
    group_id|None to ungroup,[make_master]); set_master(group_id,session_id|None;
    must be member); archive(group_id,[archived=False restores]; also archives
    its sessions); delete(group_id; sessions just ungrouped)."""
    return mcp_tools.group_manage(
        action, group_id=group_id, session_id=session_id, title=title, color=color, emoji=emoji,
        description=description, make_master=make_master, archived=archived,
    )


def _require_group(group_id: str) -> None:
    if message_store.get_group(group_id) is None:
        raise ValueError(f"group_id {group_id!r} does not exist - list groups with group_list() or create one with group_manage(action='create')")


@mcp_server.tool()
async def session_create(
    prompt: str,
    parent_session_id: str | None = None,
    mirror: bool = True,
    title: str | None = None,
    pinned: bool = False,
    inherit_group: bool | None = None,
    group_id: str | None = None,
    model: str | None = None,
    task_kind: str | None = None,
) -> dict:
    """Start a new Copilot session (Web UI, mirrored to Slack) with an initial
    prompt and run its first turn in the background. Use for isolated side
    tasks. parent_session_id leaves a backlink and the child joins the
    parent's group by default (inherit_group=False opts out; group_id
    overrides); title/pinned/mirror optional.
    task_kind picks the model: research/summary/log-triage/status-report/
    poll-review -> cheap model; code/design/review/prod-ops/terraform/change
    -> default; release-loop -> default and needs a handoff brief in the
    prompt. model=<id> overrides. Prod/CHG/terraform-apply/deploy wording
    never goes to a cheap model. Returns {conversation_id, web_url, model,
    model_reason, ...} immediately; poll with session_get."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be non-empty")
    if inherit_group and not parent_session_id:
        raise ValueError("inherit_group=True requires parent_session_id (the session whose group to inherit)")
    if group_id is not None:
        _require_group(group_id)
    if parent_session_id and message_store.get_conversation(parent_session_id) is None:
        raise ValueError(f"parent_session_id {parent_session_id!r} does not refer to an existing session")
    parent_title = message_store.get_display_title(parent_session_id) if parent_session_id else ""
    parent_model = (message_store.get_conversation_model(parent_session_id) if parent_session_id else None) or None
    routing = model_routing.resolve_model(
        model, task_kind, prompt=prompt, title=title or "", parent_title=parent_title or "",
        parent_model=parent_model,
        available=await model_routing.available_models() if (model or task_kind) else None,
    )
    log.info("session_create model routing: %s", routing.as_dict())
    conversation_id = await send_message(
        None,
        prompt,
        parent_conversation_id=parent_session_id,
        mirror=mirror,
        routing=routing,
    )
    if title and title.strip():
        set_title(conversation_id, title.strip())
    if pinned:
        message_store.set_pinned(conversation_id, True)
    if group_id is not None:
        message_store.assign_session_group(conversation_id, group_id)
    elif parent_session_id and inherit_group is not False:
        group_id = message_store.inherit_group_from_parent(conversation_id, parent_session_id)
    result = {"conversation_id": conversation_id, "web_url": _web_ui_url_for(conversation_id), "group_id": group_id}
    if routing is not None:
        result.update({"model": routing.model, "model_reason": routing.reason, "model_note": routing.note})
    return result


@mcp_server.tool()
async def session_send(
    conversation_id: str,
    text: str,
    interrupt: bool = True,
    mirror: bool = True,
) -> dict:
    """Send a message into an EXISTING session (same as typing in its Web UI
    or Slack thread). If its turn is running, interrupt=True (default)
    aborts it and tags the text as superseding the old task; interrupt=False
    raises instead. mirror=False keeps it Web-UI-only. Returns
    {conversation_id, web_url} immediately; verify with session_get."""
    if message_store.get_conversation(conversation_id) is None:
        raise ValueError(f"conversation_id {conversation_id!r} does not refer to an existing session")
    if not text or not text.strip():
        raise ValueError("text must be non-empty")

    convo = copilot_session.get_convo_if_exists(conversation_id)
    if convo is not None and convo.is_busy():
        last_user, _ = watchers._last_turn_snippets(conversation_id)
        if not interrupt:
            raise ValueError(
                f"session {conversation_id!r} is busy"
                + (f" with: \"{last_user}\"" if last_user else "")
                + " - pass interrupt=True to abort its current turn and send anyway, "
                "or retry later once it's idle."
            )
        if last_user:
            text = (
                f"{text}\n\n(This message supersedes your in-progress task: "
                f"\"{last_user}\" — address THIS message now; only resume "
                "that task afterward if it's still relevant. Don't drop it "
                "entirely, just don't let it block handling this.)"
            )

    await send_message(conversation_id, text, mirror=mirror)
    return {"conversation_id": conversation_id, "web_url": _web_ui_url_for(conversation_id)}


@mcp_server.tool()
def session_pin(session_id: str, pinned: bool = True) -> dict:
    """Pin (or unpin) an existing session so it stays at the top of the Web
    UI session list regardless of recent activity - the same effect as
    clicking the pin icon there. Raises ValueError if session_id doesn't
    refer to an existing session. Returns {conversation_id, pinned}."""
    if message_store.get_conversation(session_id) is None:
        raise ValueError(f"session_id {session_id!r} does not refer to an existing session")
    message_store.set_pinned(session_id, pinned)
    return {"conversation_id": session_id, "pinned": pinned}


class _McpAuthMiddleware(BaseHTTPMiddleware):
    """Gate every /mcp request behind a static bearer token
    (MCP_AUTH_TOKEN, no fallback). Distinct from the Web
    UI's cookie-based session auth: MCP clients (an mcp-config.json entry
    used by a Copilot CLI session) send a fixed bearer token per request,
    not a login-derived cookie. Refuses everything if no token is
    configured at all, rather than silently allowing unauthenticated
    access."""

    async def dispatch(self, request, call_next):
        if not MCP_AUTH_TOKEN:
            return JSONResponse(
                {"error": "MCP_AUTH_TOKEN not set — refusing all MCP requests"},
                status_code=500,
            )
        expected = f"Bearer {MCP_AUTH_TOKEN}"
        if not secrets.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


_mcp_asgi_app = mcp_server.streamable_http_app()
_mcp_asgi_app.add_middleware(_McpAuthMiddleware)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # Required for FastMCP's StreamableHTTP session manager to actually
    # start/stop — without this, /mcp requests hang or error since the
    # manager's background task never runs.
    async def _quota_loop():
        # Hourly background refresh so the quota never depends on someone having the page open.
        while True:
            try:
                await usage_index.fetch_quota(_find_live_client())
            except Exception:
                logging.getLogger("usage_index").exception("quota refresh loop")
            await asyncio.sleep(usage_index.quota_next_due())

    quota_task = asyncio.create_task(_quota_loop()) if usage_index.enabled() else None
    try:
        async with mcp_server.session_manager.run():
            yield
    finally:
        if quota_task:
            quota_task.cancel()


app = FastAPI(title="DOP Agent Web UI", docs_url=None, redoc_url=None, lifespan=_lifespan)


class _HostCheckMiddleware:
    """Pure-ASGI Host allow-list: rejects DNS-rebinding requests (403) on all
    routes, including /mcp, SSE and static files. Fails closed on a missing Host."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = ""
            for k, v in scope.get("headers", []):
                if k == b"host":
                    host = v.decode("latin-1").strip().lower()
                    break
            if host not in ALLOWED_HOSTS:
                if scope["type"] == "http":
                    resp = JSONResponse({"error": "forbidden host"}, status_code=403)
                    await resp(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; media-src 'self' blob:; font-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# /api/login has no session yet (nothing to ride); /api/settings* routes carry their own _guard_write.
_CSRF_EXEMPT = frozenset({"/api/login"})


class _SecurityMiddleware:
    """Pure-ASGI: CSRF guard for every mutating /api/* route (same rule as
    settings_api._guard_write: `X-Requested-With: dop` + same-origin Origin if
    sent) and a strict CSP / hardening headers on every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        if scope["method"] in _MUTATING and path.startswith("/api/") and path not in _CSRF_EXEMPT:
            err = _csrf_error(headers)
            if err:
                resp = JSONResponse({"detail": err}, status_code=403)
                return await resp(scope, receive, _with_security_headers(send))
        await self.app(scope, receive, _with_security_headers(send))


def _csrf_error(headers: dict[str, str]) -> str | None:
    if headers.get("x-requested-with", "").lower() != "dop":
        return "Missing X-Requested-With: dop header"
    origin = headers.get("origin")
    if origin:
        netloc = origin.split("://", 1)[-1].split("/", 1)[0].lower()
        if netloc != headers.get("host", "").lower() and netloc not in ALLOWED_HOSTS:
            return "Origin not allowed"
    return None


def _with_security_headers(send):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            hdrs = list(message.get("headers", []))
            have = {k.lower() for k, _ in hdrs}
            for k, v in (
                (b"content-security-policy", _CSP),
                (b"x-content-type-options", "nosniff"),
                (b"referrer-policy", "same-origin"),
            ):
                if k not in have:
                    hdrs.append((k, v.encode()))
            message = {**message, "headers": hdrs}
        await send(message)

    return wrapped


app.add_middleware(_SecurityMiddleware)
app.add_middleware(_HostCheckMiddleware)

import settings_api  # noqa: E402

app.include_router(settings_api.router)


def init(
    *,
    slack_client,
    handle_fn: Callable[..., Awaitable[None]],
    convos: dict,
    is_authorized: Callable[[str], bool],
    allowed_user_id: str,
) -> None:
    global _slack_client, _handle_fn, _convos, _is_authorized, _allowed_user_id
    global _initialized, _webui_adapter, _broadcaster
    _slack_client = slack_client
    _handle_fn = handle_fn
    _convos = convos
    _is_authorized = is_authorized
    _allowed_user_id = allowed_user_id
    message_store.init_db()
    if slack_client is not None:
        adapters.REGISTRY.register(adapters.REGISTRY.slack_for(slack_client))
    if not _initialized:
        _webui_adapter = WebAdapter()
        adapters.REGISTRY.register(_webui_adapter)
        _broadcaster = ConversationBroadcaster(adapters.REGISTRY)
        message_store.register_change_listener(_broadcast_change)
        _initialized = True


_revoked_sids: dict[str, float] = {}  # sid -> expiry (epoch); in-memory, cleared on restart


def _make_cookie_value() -> str:
    return _serializer.dumps({"ok": True, "t": time.time(), "sid": secrets.token_urlsafe(16)})


def _revoke_cookie(value: str | None) -> None:
    if not value:
        return
    try:
        data = _serializer.loads(value, max_age=_COOKIE_MAX_AGE)
    except Exception:
        return
    now = time.time()
    for k in [k for k, exp in _revoked_sids.items() if exp < now]:
        del _revoked_sids[k]
    sid = data.get("sid") if isinstance(data, dict) else None
    if sid:
        _revoked_sids[sid] = now + _COOKIE_MAX_AGE + 60


def _cookie_is_valid(value: str | None) -> bool:
    if not value:
        return False
    try:
        data = _serializer.loads(value, max_age=_COOKIE_MAX_AGE)
        if isinstance(data, dict) and data.get("sid") in _revoked_sids:
            return False
        return True
    except BadSignature:
        return False
    except Exception:
        return False


def _require_auth(request: Request) -> None:
    if not _cookie_is_valid(request.cookies.get(_COOKIE_NAME)):
        raise HTTPException(401, "Not authenticated")


def _is_running(conversation_id: str) -> bool:
    if not _convos:
        return False
    convo = _convos.get(conversation_id)
    return bool(convo is not None and convo._lock.locked())


def _running_since(conversation_id: str) -> float | None:
    """Epoch seconds the current turn started, or None if not running.

    Backs the Web UI's live per-second "Working... Ns" counter (see
    copilot_session.CopilotConversation.turn_started_at) — kept separate
    from _is_running() so callers that don't need the timestamp (e.g. the
    sidebar's simple spinner) don't pay for the extra lookup.
    """
    if not _convos:
        return None
    convo = _convos.get(conversation_id)
    if convo is None or not convo._lock.locked():
        return None
    return convo.turn_started_at


# Prefixes handlers.py's convo.send() except-blocks use for a failed or
# timed-out turn (see _handle()'s "except TimeoutError"/"except Exception"
# branches). Mirrored in webui/app.js's ERROR_RESPONSE_PREFIXES for the
# "Retry last message" button - kept in two places (Python for the sidebar
# error flag, JS for the transcript retry button) since each needs the
# check in a different runtime; update both if the wording ever changes.
_ERROR_RESPONSE_PREFIXES = (":warning: DOP Agent request timed out", ":warning: Copilot request timed out", ":x: Error:")


def platform_links_for(conversation_id: str, bindings: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """One entry per external platform this conversation is bound to:
    ``{"platform", "external_key", "url"}``. ``url`` is a deep link when the
    platform's URL scheme is known (the ONLY place the Web server names a
    chat platform) and None otherwise, so the UI shows a badge-only entry
    for platforms it cannot link to yet."""
    links: list[dict[str, Any]] = []
    if bindings is None:
        bindings = message_store.list_conversation_bindings(conversation_id)
    for binding in bindings:
        platform, key = binding["platform"], binding["external_key"]
        if platform == message_store.WEB_PLATFORM:
            continue
        url = None
        if platform == message_store.SLACK_PLATFORM and key:
            channel, _, thread_ts = key.partition(":")
            url = f"slack://channel?team=&id={channel}"
            if thread_ts:
                url += f"&message={thread_ts.replace('.', '')}"
        elif platform == message_store.TELEGRAM_PLATFORM and key:
            chat, _, thread = key.partition(":")
            if chat.startswith("-100") and thread:
                url = f"https://t.me/c/{chat[4:]}/{thread}"
        links.append({"platform": platform, "external_key": key, "url": url})
    return links


def _session_payload() -> list[dict[str, Any]]:
    sessions = message_store.list_conversations()
    all_bindings = message_store.list_all_bindings()
    for item in sessions:
        item["platform_links"] = platform_links_for(item["conversation_id"], all_bindings.get(item["conversation_id"], []))
        item["running"] = _is_running(item["conversation_id"])
        item["turn_started_at"] = _running_since(item["conversation_id"])
        item["errored"] = (
            not item["running"]
            and item.get("last_role") == "assistant"
            and str(item.get("preview") or "").startswith(_ERROR_RESPONSE_PREFIXES)
        )
    if usage_index.enabled():
        aiu_map = usage_index.aiu_by_session_cached()
        for item in sessions:
            aiu = aiu_map.get(item.get("session_id") or "")
            item["aiu"] = round(aiu, 1) if aiu is not None else None
    session_groups.decorate_sessions_with_groups(sessions, message_store.list_groups(include_archived=True))
    return sessions


def _transcript_payload(conversation_id: str) -> dict[str, Any]:
    convo = message_store.get_conversation(conversation_id)
    if convo is None:
        raise HTTPException(404, "Unknown conversation_id")
    bookmarked_ids = set(message_store.get_bookmarked_message_ids(conversation_id))
    # Bulk-fetched (one query, not one per message) so attachment previews
    # don't cost an N+1 round trip - see message_store.get_attachments_for_conversation.
    attachments_by_message = message_store.get_attachments_for_conversation(conversation_id)
    return {
        "conversation_id": conversation_id,
        "session_id": convo.get("copilot_session_id"),
        "last_read_at": message_store.get_last_read(conversation_id),
        "turns": message_kinds.classify_turns([
            {
                "id": msg["id"],
                "role": msg["role"],
                "text": msg["content"],
                "timestamp": msg["created_at"],
                "updated_at": msg["updated_at"],
                "bookmarked": msg["id"] in bookmarked_ids,
                "actions": msg.get("actions") or [],
                "attachments": [
                    {
                        "id": a["id"],
                        "kind": a["kind"],
                        "filename": a["filename"],
                        "mimetype": a["mimetype"],
                        "size": a["size"],
                        "url": f"/api/sessions/{conversation_id}/attachments/{a['id']}",
                    }
                    for a in attachments_by_message.get(msg["id"], [])
                ],
            }
            for msg in message_store.get_messages(conversation_id)
        ]),
        "running": _is_running(conversation_id),
        "turn_started_at": _running_since(conversation_id),
    }


def _broadcast_change(conversation_id: str) -> None:
    for queue in list(_session_subscribers):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(True)
        except asyncio.QueueFull:
            pass
    for queue in list(_conversation_subscribers.get(conversation_id, set())):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(True)
        except asyncio.QueueFull:
            pass


async def _sse_stream(request: Request, queue: asyncio.Queue[bool], builder: Callable[[], dict[str, Any]], *, event_name: str):
    yield _format_sse(event_name, builder())
    while True:
        if await request.is_disconnected():
            break
        try:
            await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECS)
            yield _format_sse(event_name, builder())
        except TimeoutError:
            yield ": keepalive\n\n"


def _format_sse(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"


def set_title(conversation_id: str, title: str | None) -> str | None:
    try:
        return message_store.set_title(conversation_id, title)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}") from exc


def _new_conversation_id(parent_conversation_id: str | None = None) -> str:
    """Create a brand-new, standalone conversation, optionally recording a
    '#session:<id>' backlink to the conversation it was spawned from.

    Shared by both the Web UI's "Continue in new session" button
    (api_continue_in_new_session) and the session_create MCP tool - both
    want the exact same parent-linkage/backlink behavior, just triggered
    from different callers (a human clicking a button vs. an agent
    proactively escaping an overly-long thread)."""
    conversation_id = message_store.get_or_create_conversation(None, parent_conversation_id=parent_conversation_id)
    if parent_conversation_id:
        old_title = message_store.get_display_title(parent_conversation_id)
        message_store.record_message(
            conversation_id,
            "system",
            f"\u21aa\ufe0f Continued from [{old_title}](#session:{parent_conversation_id})",
        )
    return conversation_id


def _web_ui_url_for(conversation_id: str, draft_text: str | None = None) -> str:
    """Deep link that opens this conversation directly in the Web UI (see
    webui/app.js's parseDeepLinkHash() - '#open=<id>' is parsed at page load
    the same way the existing '#msg=<id>:<msgId>' link is, just without a
    specific message to scroll to/highlight).

    If draft_text is given, appends '&draft=<url-encoded text>' - the Web UI
    pre-fills the compose box with this text on open (appended below any
    existing unsent draft, never replacing it - see applyDraftPrefill() in
    app.js). Use this to hand someone a "click here to continue with this
    context" link, e.g. from a watch-fire reminder or a suggested follow-up."""
    url = f"http://{WEB_UI_PUBLIC_HOST}:{WEB_UI_PORT}/#open={conversation_id}"
    if draft_text:
        url += f"&draft={quote(draft_text, safe='')}"
    return url


def _web_ui_new_session_url(draft_text: str | None = None) -> str:
    """Deep link that starts a brand-new session in the Web UI (same as
    clicking the "New Session" button) - the '#new'-hash counterpart to
    _web_ui_url_for() above for when there's no existing conversation_id to
    link to yet. If draft_text is given, pre-fills the (new session's)
    compose box the same way, via the shared NEW_SESSION_DRAFT_KEY draft-
    cache slot in app.js."""
    url = f"http://{WEB_UI_PUBLIC_HOST}:{WEB_UI_PORT}/#new"
    if draft_text:
        url += f"&draft={quote(draft_text, safe='')}"
    return url


def _async_new_session_adapter(registry):
    """The adapter that must create the new session's external thread itself
    (``NEW_SESSION_PLATFORM``, e.g. ``telegram``: a forum topic can't be offered
    synchronously), else None (Slack's known-DM behaviour is unchanged)."""
    name = config.value("agent.new_session_platform").lower()
    adapter = registry.get(name) if name else None
    return adapter if adapter is not None and hasattr(adapter, "ensure_new_conversation_target") else None


async def send_message(
    conversation_id: str | None,
    text: str,
    *,
    parent_conversation_id: str | None = None,
    mirror: bool = True,
    routing: "model_routing.Routing | None" = None,
) -> str:
    if _handle_fn is None:
        raise RuntimeError("web_server.init() not called yet")
    if not text.strip():
        raise HTTPException(400, "Empty message")

    registry = _broadcaster.registry if _broadcaster is not None else adapters.REGISTRY
    target: tuple | None = None  # (adapter, thread_key) of the external platform to mirror to
    wants_async_target = None

    if conversation_id:
        convo = message_store.get_conversation(conversation_id)
        if convo is None:
            raise HTTPException(404, "Unknown conversation_id")
        target = registry.external_target(conversation_id)
    elif mirror:
        # Only try to anchor brand-new conversations to an existing external
        # thread (e.g. a known Slack DM) on a best-effort basis. If none is
        # known, fall through to a Web-UI-only conversation below rather than
        # blocking the user - mirroring a *new* thread is a nice-to-have.
        wants_async_target = _async_new_session_adapter(registry)
        target = None if wants_async_target is not None else registry.new_conversation_target()

    is_new_conversation = conversation_id is None
    if conversation_id is None:
        # No external platform available (not yet known, mirror=False, or
        # disabled) - create a standalone, Web-UI-only conversation. It still
        # gets a normal UUID conversation_id and lives in bot_store.db like
        # any other; nothing is posted externally for it unless/until the
        # user later interacts with it from another platform.
        conversation_id = _new_conversation_id(parent_conversation_id)
        if routing is not None:
            # Persisted BEFORE the turn starts so _ensure_session sees it.
            message_store.set_conversation_model(conversation_id, routing.pass_model, routing.reason)
            if routing.pass_model or routing.note:
                message_store.record_message(
                    conversation_id, "system",
                    f":compass: Model: {routing.model} - {routing.reason}" + (f" ({routing.note})" if routing.note else ""))
    message_store.bind_conversation(conversation_id, message_store.WEB_PLATFORM, conversation_id)
    if wants_async_target is not None:
        # Needs the conversation to exist first: the adapter binds BEFORE the first send.
        try:
            target = await wants_async_target.ensure_new_conversation_target(
                conversation_id, text.strip().splitlines()[0][:60])
        except Exception as exc:  # noqa: BLE001 - mirroring a new session is best effort
            log.warning("Could not create a %s target for the new session: %s", wants_async_target.platform, exc)
            target = None

    if _webui_adapter is not None:
        _, message_id = await adapters.deliver(_webui_adapter,
            {"conversation_id": conversation_id},
            text,
            conversation_id=conversation_id,
            role="user",
        )
    else:
        message_id = message_store.record_message(
            conversation_id,
            "user",
            text,
        )

    msg_ts: str | None = None
    thread_ts: str | None = None
    turn_adapter = None
    turn_thread_key = None
    if target is not None:
        turn_adapter, turn_thread_key = target
        thread_ts = turn_thread_key.get("thread_ts")
    if mirror and target is not None and _broadcaster is not None:
        adapter, thread_key = target
        try:
            deliveries = await _broadcaster.fanout_send(
                conversation_id,
                f":globe_with_meridians: (via Web UI) {text}",
                role="user",
                origin=BroadcastOrigin(message_store.WEB_PLATFORM, conversation_id),
                store_text=text,
                record_by_platform={adapter.platform: False},
                explicit_thread_keys={adapter.platform: thread_key},
            )
        except Exception as exc:  # noqa: BLE001
            if adapter.platform != message_store.TELEGRAM_PLATFORM:
                raise
            # A Telegram outage must never block a Web UI send.
            log.warning("Telegram mirror of a Web UI message failed: %s", exc)
            deliveries = {}
        posted, _ = deliveries.get(adapter.platform, (None, None))
        ref = adapter.message_ref(thread_key, posted) if posted else None
        msg_ts = ref.id if ref else None
        if msg_ts:
            new_key = adapters.thread_key_after_post(adapter, thread_key, msg_ts)
            if adapter.external_key(new_key) != adapter.external_key(thread_key):
                message_store.bind_conversation(conversation_id, adapter.platform, adapter.external_key(new_key))
            turn_thread_key = new_key
            thread_ts = new_key.get("thread_ts")
            message_store.record_message(
                conversation_id,
                "user",
                text,
                message_id=message_id,
                external_ref=adapter.external_ref(ref),
            )

    # Run the actual turn in the background instead of awaiting it here.
    # Mirrors how Slack works: posting a message never blocks on the reply —
    # _handle()'s own busy-check (CopilotConversation.send()) aborts any
    # in-progress turn for this conversation the moment a new message comes
    # in, so the caller must be free to send that next message immediately.
    # If we awaited _handle_fn() here, this HTTP request (and therefore the
    # Web UI's compose box, which disables itself until the request resolves)
    # would stay blocked for the entire turn, making it impossible to send an
    # interrupting follow-up the way Slack allows.
    async def _run_turn() -> None:
        try:
            await _handle_fn(
                user_id=_allowed_user_id,
                text=text,
                ts=msg_ts or "",
                thread_ts=thread_ts,
                conversation_id=conversation_id,
                record_user_message=False,
                adapter=turn_adapter,
                thread_key=turn_thread_key if turn_adapter else None,
                authorized=True,
            )
        except Exception:
            log.exception("[%s] Background turn from Web UI failed", conversation_id)
            try:
                message_store.record_message(
                    conversation_id,
                    "system",
                    ":warning: Internal error while processing this message. Check the bot logs.",
                )
            except Exception:
                log.exception("[%s] Could not record error message for failed Web UI turn", conversation_id)

    asyncio.create_task(_run_turn())
    if is_new_conversation and auto_title.enabled():
        _spawn_auto_title(conversation_id, text)
    return conversation_id


_auto_title_tasks: set[asyncio.Task] = set()


def _spawn_auto_title(conversation_id: str, text: str) -> None:
    task = asyncio.create_task(auto_title.auto_title(conversation_id, text))
    _auto_title_tasks.add(task)  # strong ref so it isn't garbage-collected mid-flight
    task.add_done_callback(_auto_title_tasks.discard)


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not WEB_UI_PASSWORD:
        raise HTTPException(500, "WEB_UI_PASSWORD not set in .env — refusing all logins")

    ip = _client_ip(request)
    attempts = _prune_login_failures(ip)
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        retry_after = max(1, int(_LOGIN_WINDOW_SECS - (time.time() - attempts[0])))
        raise HTTPException(
            429,
            f"Too many failed login attempts. Try again in {retry_after}s.",
        )

    if not secrets.compare_digest(
        body.password.encode("utf-8", "surrogatepass"), WEB_UI_PASSWORD.encode("utf-8", "surrogatepass")
    ):
        attempts.append(time.time())
        _login_failures[ip] = attempts
        raise HTTPException(403, "Wrong password")

    # Successful login clears any prior failure history for this IP.
    _login_failures.pop(ip, None)
    response.set_cookie(
        _COOKIE_NAME,
        _make_cookie_value(),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    _revoke_cookie(request.cookies.get(_COOKIE_NAME))
    response.delete_cookie(_COOKIE_NAME, httponly=True, samesite="strict", secure=_is_https(request))
    return {"ok": True}


def _sessions_stream_payload() -> dict[str, Any]:
    sessions = _session_payload()
    return {"sessions": sessions, "groups": session_groups.groups_payload(sessions, include_archived=True)}


@app.get("/api/sessions")
async def api_list_sessions(request: Request):
    _require_auth(request)
    return _sessions_stream_payload()


def _group_or_404(group_id: str) -> dict[str, Any]:
    group = message_store.get_group(group_id)
    if group is None:
        raise HTTPException(404, "Unknown group_id")
    return group


@app.get("/api/groups")
async def api_list_groups(request: Request, include_archived: bool = False):
    _require_auth(request)
    return {"groups": session_groups.groups_payload(_session_payload(), include_archived=include_archived)}


@app.post("/api/groups")
async def api_create_group(body: GroupBody, request: Request):
    _require_auth(request)
    try:
        return message_store.create_group(body.title or "", body.color, body.emoji, body.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/groups/{group_id}")
async def api_update_group(group_id: str, body: GroupBody, request: Request):
    _require_auth(request)
    _group_or_404(group_id)
    try:
        return message_store.update_group(
            group_id, title=body.title, color=body.color, emoji=body.emoji, description=body.description
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/groups/{group_id}")
async def api_delete_group(group_id: str, request: Request):
    _require_auth(request)
    _group_or_404(group_id)
    return {"unassigned": message_store.delete_group(group_id)}


@app.put("/api/groups/{group_id}/archive")
async def api_archive_group(group_id: str, body: ArchiveBody, request: Request):
    _require_auth(request)
    _group_or_404(group_id)
    return {"archived": body.archived, "sessions": message_store.set_group_archived(group_id, body.archived)}


@app.put("/api/groups/{group_id}/read")
async def api_mark_group_read(group_id: str, request: Request):
    """Marks all unread sessions in the group read."""
    _require_auth(request)
    _group_or_404(group_id)
    return {"marked": message_store.mark_group_read(group_id)}


@app.put("/api/groups/{group_id}/master")
async def api_set_group_master(group_id: str, body: GroupMasterBody, request: Request):
    _require_auth(request)
    _group_or_404(group_id)
    try:
        message_store.set_group_master(group_id, body.conversation_id)
    except KeyError:
        raise HTTPException(404, "Unknown conversation_id")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"master_id": body.conversation_id}


@app.get("/api/groups/{group_id}/overview")
async def api_group_overview(group_id: str, request: Request):
    _require_auth(request)
    _group_or_404(group_id)
    return session_groups.overview(group_id, _session_payload(), watchers.list_watches())


@app.put("/api/sessions/{conversation_id}/group")
async def api_assign_group(conversation_id: str, body: AssignGroupBody, request: Request):
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, "Unknown conversation_id")
    if body.group_id is not None:
        _group_or_404(body.group_id)
    message_store.assign_session_group(conversation_id, body.group_id)
    return {"group_id": body.group_id}


@app.post("/api/groups/suggest")
async def api_suggest_group(body: SuggestGroupBody, request: Request):
    _require_auth(request)
    sessions = _session_payload()
    return {"suggestion": session_groups.suggest_group(body.prompt, message_store.list_groups(), sessions)}


@app.get("/api/search")
async def api_search_messages(request: Request, q: str = ""):
    _require_auth(request)
    return {"results": message_store.search_messages(q)}


@app.get("/api/sessions/{conversation_id}")
async def api_get_transcript(conversation_id: str, request: Request):
    _require_auth(request)
    return _transcript_payload(conversation_id)


@app.get("/api/sessions/{conversation_id}/attachments/{attachment_id}")
async def api_get_attachment(conversation_id: str, attachment_id: str, request: Request):
    """Serve a cached Slack image attachment (see handlers.py's
    maybe_capture_images). Scoped to conversation_id (via
    message_store.get_attachment) so one conversation's bearer token can't
    be used to enumerate/read another conversation's cached files."""
    _require_auth(request)
    attachment = message_store.get_attachment(attachment_id, conversation_id)
    if attachment is None:
        raise HTTPException(404, "Unknown attachment")
    local_path = Path(attachment["local_path"])
    if not local_path.is_file():
        raise HTTPException(404, "Attachment file no longer on disk")
    return FileResponse(local_path, media_type=attachment.get("mimetype") or "application/octet-stream")


@app.get("/api/stream/sessions")
async def api_stream_sessions(request: Request):
    _require_auth(request)
    queue: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
    _session_subscribers.add(queue)

    async def iterator():
        try:
            async for chunk in _sse_stream(request, queue, _sessions_stream_payload, event_name="sessions"):
                yield chunk
        finally:
            _session_subscribers.discard(queue)

    return StreamingResponse(iterator(), media_type="text/event-stream")


@app.get("/api/stream/sessions/{conversation_id}")
async def api_stream_conversation(conversation_id: str, request: Request):
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, "Unknown conversation_id")
    queue: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
    _conversation_subscribers[conversation_id].add(queue)

    async def iterator():
        try:
            async for chunk in _sse_stream(
                request,
                queue,
                lambda: _transcript_payload(conversation_id),
                event_name="conversation",
            ):
                yield chunk
        finally:
            subs = _conversation_subscribers.get(conversation_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    _conversation_subscribers.pop(conversation_id, None)

    return StreamingResponse(iterator(), media_type="text/event-stream")


@app.post("/api/send")
async def api_send(body: SendBody, request: Request):
    _require_auth(request)
    conversation_id = await send_message(body.conversation_id, body.text)
    return {"conversation_id": conversation_id}


@app.post("/api/sessions/{conversation_id}/transcribe")
async def api_transcribe_voice_upload(conversation_id: str, request: Request, audio: UploadFile = File(...)):
    """Web UI equivalent of sending a Slack voice message: the browser's
    MediaRecorder captures a clip (webm/ogg, whatever MediaRecorder.isTypeSupported()
    picked - see webui/app.js) and posts it here as multipart/form-data. Runs
    through the exact same platform-agnostic transcribe_web_upload() ->
    _transcribe_one() -> Whisper pipeline Slack voice messages use (adapter=
    WebAdapter), so the "Transcribing..." placeholder, Auto-send/Preview
    buttons, and (once done) the transcript/auto-forward-to-Copilot behavior
    are identical to Slack's, not a cut-down Web-only path.

    Runs transcription in the background (like /api/send does for turns) so
    this request returns immediately - the Web UI polls/streams the usual
    session SSE feed to see the "Transcribing..." placeholder appear and
    then get replaced with the final text."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    # Save-first: stream the upload to ~/.dop-agent/audio BEFORE any
    # processing, so a restart/crash mid-transcription can't lose it.
    try:
        path = await asyncio.to_thread(
            audio_store.copy_fileobj, conversation_id, audio.file, audio.filename,
            transcription.MAX_AUDIO_BYTES, platform="web", user_id=_allowed_user_id)
    except audio_store.AudioTooLarge:
        raise HTTPException(413, f"Audio file too large (max {transcription.MAX_AUDIO_BYTES} bytes)")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    handlers.start_audio_job(path, conversation_id=conversation_id, user_id=_allowed_user_id)
    return {"ok": True}


def _require_upload(conversation_id: str, upload_id: str):
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    path = audio_store.resolve_upload(conversation_id, upload_id)
    if path is None:
        raise HTTPException(404, f"Unknown upload_id: {upload_id!r}")
    return path


@app.post("/api/sessions/{conversation_id}/audio-retry")
async def api_audio_retry(conversation_id: str, request: Request):
    """Re-queue this conversation's ``failed`` recordings (audio is always kept
    on disk); the maintenance loop picks them up within a minute."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    return {"requeued": len(audio_store.retry_failed(conversation_id=conversation_id))}


@app.post("/api/sessions/{conversation_id}/audio-upload")
async def api_audio_upload_start(conversation_id: str, request: Request, ext: str = "webm"):
    """Start a chunked recording upload (browser sends MediaRecorder timeslice
    chunks while still recording, so a closed tab / crash keeps what was captured).
    Abandoned uploads are promoted to a normal transcription job by the
    audio-store maintenance sweep after ~90 s of silence."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    path = audio_store.begin_chunked(conversation_id, f"voice.{ext}", platform="web", user_id=_allowed_user_id)
    return {"upload_id": path.stem}


@app.put("/api/sessions/{conversation_id}/audio-upload/{upload_id}/chunk")
async def api_audio_upload_chunk(conversation_id: str, upload_id: str, seq: int, request: Request):
    _require_auth(request)
    path = _require_upload(conversation_id, upload_id)
    data = await request.body()
    try:
        nxt = await asyncio.to_thread(audio_store.append_chunk, path, seq, data, transcription.MAX_AUDIO_BYTES)
    except audio_store.AudioTooLarge:
        raise HTTPException(413, f"Audio file too large (max {transcription.MAX_AUDIO_BYTES} bytes)")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"next_seq": nxt}


@app.post("/api/sessions/{conversation_id}/audio-upload/{upload_id}/finish")
async def api_audio_upload_finish(conversation_id: str, upload_id: str, request: Request):
    _require_auth(request)
    path = _require_upload(conversation_id, upload_id)
    if not audio_store.finish_chunked(path):
        raise HTTPException(400, "Empty audio upload")
    handlers.start_audio_job(path, conversation_id=conversation_id, user_id=_allowed_user_id)
    return {"ok": True}


@app.delete("/api/sessions/{conversation_id}/audio-upload/{upload_id}")
async def api_audio_upload_abort(conversation_id: str, upload_id: str, request: Request):
    """Drop a partial chunked upload (browser falls back to a one-shot upload)."""
    _require_auth(request)
    path = _require_upload(conversation_id, upload_id)
    return {"aborted": audio_store.abort_chunked(path)}


@app.post("/api/sessions/{conversation_id}/stop")
async def api_stop(conversation_id: str, request: Request):
    """Interrupt an in-progress turn for this conversation, mirroring the
    ":black_square_for_stop: Stop" button in Slack (handlers.py's
    handle_stop_working_turn / CopilotConversation.interrupt()). Shares the
    same in-memory `_convos` registry as Slack, so it aborts the exact same
    running turn regardless of which surface (Slack or Web UI) started it."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, "Unknown conversation_id")
    convo = (_convos or {}).get(conversation_id)
    stopped = bool(convo is not None and await convo.interrupt())
    return {"stopped": stopped}


@app.post("/api/sessions/{conversation_id}/continue")
async def api_continue_in_new_session(conversation_id: str, request: Request):
    """Create a brand-new, standalone conversation seeded with a system
    message linking back to the session it was continued from - a cheap way
    to escape an overly-long/slow thread (see the long-session highlight)
    without losing the ability to find your way back to the original
    context. The backlink uses an in-app '#session:<id>' markdown link that
    the Web UI's renderMessages() intercepts to jump straight to the old
    session instead of opening a dead browser anchor. Shares its
    parent-linkage/backlink logic with the session_create MCP tool via
    _new_conversation_id()."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, "Unknown conversation_id")
    new_conversation_id = _new_conversation_id(conversation_id)
    return {"conversation_id": new_conversation_id}


@app.put("/api/sessions/{conversation_id}/pin")
async def api_set_pin(conversation_id: str, body: PinBody, request: Request):
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, "Unknown conversation_id")
    message_store.set_pinned(conversation_id, body.pinned)
    return {"pinned": body.pinned}


@app.put("/api/sessions/{conversation_id}/archive")
async def api_set_archive(conversation_id: str, body: ArchiveBody, request: Request):
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, "Unknown conversation_id")
    message_store.set_archived(conversation_id, body.archived)
    return {"archived": body.archived}


@app.put("/api/pins/order")
async def api_set_pin_order(body: PinOrderBody, request: Request):
    _require_auth(request)
    message_store.set_pin_order(body.conversation_ids)
    return {"ok": True}


class GroupsOrderBody(BaseModel):
    group_ids: list[str]


@app.put("/api/groups/order")
async def api_set_groups_order(body: GroupsOrderBody, request: Request):
    _require_auth(request)
    message_store.set_groups_order(body.group_ids)
    return {"ok": True}


@app.put("/api/groups/member-order")
async def api_set_group_order(body: GroupOrderBody, request: Request):
    _require_auth(request)
    message_store.set_group_order(body.conversation_ids)
    return {"ok": True}


@app.put("/api/sessions/{conversation_id}/title")
async def api_set_title(conversation_id: str, body: TitleBody, request: Request):
    _require_auth(request)
    return {"title": set_title(conversation_id, body.title)}


@app.get("/api/sessions/{conversation_id}/note")
async def api_get_note(conversation_id: str, request: Request):
    """Private per-session note-to-self - a free-text
    scratchpad tied to this conversation, never sent to the agent, never
    shown to anyone but the logged-in user of this Web UI."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    return {"note": message_store.get_note(conversation_id)}


@app.put("/api/sessions/{conversation_id}/note")
async def api_set_note(conversation_id: str, body: NoteBody, request: Request):
    _require_auth(request)
    try:
        return {"note": message_store.set_note(conversation_id, body.note)}
    except KeyError as exc:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}") from exc


@app.put("/api/sessions/{conversation_id}/messages/{message_id}/bookmark")
async def api_set_message_bookmark(conversation_id: str, message_id: str, body: BookmarkBody, request: Request):
    """Bookmark/star a specific message within a thread, so it
    can be found again quickly via the sidebar bookmark list without
    scrolling through a long transcript."""
    _require_auth(request)
    try:
        bookmarked = message_store.set_message_bookmarked(conversation_id, message_id, body.bookmarked)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}") from exc
    return {"bookmarked": bookmarked}


@app.post("/api/sessions/{conversation_id}/messages/{message_id}/transcript-action")
async def api_transcript_action(conversation_id: str, message_id: str, body: TranscriptActionBody, request: Request):
    """Web UI equivalent of clicking one of Slack's voice-transcript
    buttons (Auto-send/Preview/Send to agent) - see handlers.py's
    _arm_transcript_mode()/web_transcript_action() for the shared core this
    delegates to, and message_store.py's new `messages.actions` column for
    how the frontend knows which buttons to render on a given message. This
    is the backend half of real Web UI button parity, not a global
    AUDIO_AUTO_SEND-only fallback."""
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    handled = await handlers.web_transcript_action(
        body.action_id,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=_allowed_user_id,
    )
    if not handled:
        raise HTTPException(400, f"Unknown action_id: {body.action_id!r}")
    return {"ok": True}


@app.get("/api/sessions/{conversation_id}/bookmarks")
async def api_get_bookmarks(conversation_id: str, request: Request):
    _require_auth(request)
    if message_store.get_conversation(conversation_id) is None:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}")
    return {"message_ids": message_store.get_bookmarked_message_ids(conversation_id)}


@app.get("/api/inbox")
async def api_inbox(request: Request):
    """Messages inbox candidates: sessions whose last message is an assistant
    message with ask/ideas fences and has not been dismissed. The client
    parses the fences with the real widget parsers (invalid ones drop out)."""
    _require_auth(request)
    by_id = {s["conversation_id"]: s for s in _session_payload()}
    items = []
    for c in message_store.inbox_candidates():
        s = by_id.get(c["conversation_id"])
        if s is None or s.get("running"):
            continue
        items.append({**c, "title": s["title"], "group_id": s.get("group_id"), "running": False})
    return {"items": items}


@app.put("/api/inbox/{conversation_id}/dismiss")
async def api_inbox_dismiss(conversation_id: str, message_id: str, request: Request):
    _require_auth(request)
    message_store.dismiss_inbox(conversation_id, message_id)
    return {"ok": True}


@app.delete("/api/inbox/{conversation_id}/dismiss")
async def api_inbox_undismiss(conversation_id: str, request: Request, message_id: str | None = None):
    _require_auth(request)
    message_store.undismiss_inbox(conversation_id, message_id)
    return {"ok": True}


@app.put("/api/sessions/{conversation_id}/read")
async def api_mark_read(conversation_id: str, request: Request):
    """Server-side read/unread tracking (see message_store.mark_read()).
    No request body - always marks "read up to now" (the conversation's
    current updated_at at the moment this is called)."""
    _require_auth(request)
    try:
        return {"last_read_at": message_store.mark_read(conversation_id)}
    except KeyError as exc:
        raise HTTPException(404, f"Unknown conversation_id: {conversation_id!r}") from exc


@app.get("/api/watches")
async def api_list_watches(request: Request, include_inactive: bool = False, conversation_id: str | None = None):
    """Read-only view of registered watches (see watchers.py / the
    watch-manage skill). With no conversation_id, returns every watch across
    every conversation; passing conversation_id scopes the list to just
    that conversation (used by the session-header "watchers for this
    session" button so you can check what's pending for the session you're
    currently looking at, without hunting through the global list) —
    conversation_id-scoped rather than thread_ts-scoped so this also works
    correctly for Web-UI-only sessions with no Slack thread at all.

    This endpoint itself stays read-only (GET, no side effects) — pause/
    resume/remove/recheck now live as their own small mutating endpoints
    right below, added after the original "read-only, cancel/add explicitly
    not needed" ask was revisited in favor of inline row actions in the
    Watches panel."""
    _require_auth(request)
    return {"watches": watchers.list_watches(conversation_id=conversation_id, include_inactive=include_inactive)}


@app.get("/api/watches/{watch_id}/preview")
async def api_watch_preview(request: Request, watch_id: int):
    """Live title/url/status preview for one watch's underlying
    target (see _fetch_watch_preview_sync, above) — backs
    the Watches panel row showing a real title + clickable link instead of
    just the raw watch `key`. Uses the same TTL cache dict as api_unfurl()
    (prefixed `watch:` so the two namespaces can't collide), keyed by
    watch id + key so a changed key doesn't serve a stale cached title."""
    _require_auth(request)
    watch = watchers.get_watch(watch_id)
    if watch is None:
        raise HTTPException(404, f"Unknown watch_id: {watch_id}")
    cache_key = f"watch:{watch_id}:{watch.get('key')}"
    cached = _unfurl_cache.get(cache_key)
    if cached is not None and (time.time() - cached[0]) < furl_api.ttl():
        return cached[1]
    result = await asyncio.to_thread(_fetch_watch_preview_sync, watch)
    _unfurl_cache[cache_key] = (time.time(), result)
    return result


@app.post("/api/watches/{watch_id}/pause")
async def api_watch_pause(request: Request, watch_id: int):
    """Pause one watch — backs the Watches panel's inline row action.
    Thin wrapper around mcp_tools.watch_pause(), the same function the
    agent's own watch_pause MCP tool calls, so Web-UI and agent-driven
    pauses can never behave differently from each other."""
    _require_auth(request)
    if watchers.get_watch(watch_id) is None:
        raise HTTPException(404, f"Unknown watch_id: {watch_id}")
    mcp_tools.watch_pause(watch_id)
    return watchers.get_watch(watch_id)


@app.post("/api/watches/{watch_id}/resume")
async def api_watch_resume(request: Request, watch_id: int):
    """Resume one previously paused watch — backs the Watches panel's
    inline row action. Thin wrapper around mcp_tools.watch_resume()."""
    _require_auth(request)
    if watchers.get_watch(watch_id) is None:
        raise HTTPException(404, f"Unknown watch_id: {watch_id}")
    mcp_tools.watch_resume(watch_id)
    return watchers.get_watch(watch_id)


@app.delete("/api/watches/{watch_id}")
async def api_watch_delete(request: Request, watch_id: int):
    """Permanently remove one watch — backs the Watches panel's inline row
    action (behind a confirm in the UI). Thin wrapper around
    mcp_tools.watch_remove()."""
    _require_auth(request)
    if watchers.get_watch(watch_id) is None:
        raise HTTPException(404, f"Unknown watch_id: {watch_id}")
    mcp_tools.watch_remove(watch_id)
    return {"removed": True}


@app.post("/api/watches/{watch_id}/recheck")
async def api_watch_recheck(request: Request, watch_id: int):
    """Force an immediate out-of-band poll of one watch (the Watches
    panel's "re-check now" row action) instead of waiting for the next
    watcher_loop() tick. See watchers.poll_once() — this can itself
    trigger a real notification/agent turn if something changed, same as
    a normal scheduled poll would. 400s (not 500s) for the expected
    "can't be rechecked" cases (self_restart type, or a non-active
    status) so the UI can show a clear inline message."""
    _require_auth(request)
    watch = watchers.get_watch(watch_id)
    if watch is None:
        raise HTTPException(404, f"Unknown watch_id: {watch_id}")
    try:
        await watchers.poll_once(watch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return watchers.get_watch(watch_id)


@app.get("/api/integrations/ui")
async def api_integrations_ui(request: Request):
    """Secret-free registry projection (link regexes, url templates, status emoji, watch types)."""
    _require_auth(request)
    from integrations.runtime import get_registry
    return get_registry().ui_projection()


_ICON_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"}


@app.get("/api/drivers/{driver_id}/icon")
@app.get("/api/drivers/{driver_id}/icon/{filename}")
async def api_driver_icon(request: Request, driver_id: str, filename: str = ""):
    """Serve a driver-folder SVG icon. Only files the manifest declares (or icon.svg) are served,
    capped in size, with a locked-down CSP + nosniff because SVG can carry scripts."""
    _require_auth(request)
    from drivers.manifest import ICON_MAX_BYTES
    from integrations.runtime import get_registry
    m = get_registry().manifests.get(driver_id)
    if m is None:
        raise HTTPException(404, "Unknown driver")
    name = filename or (m.icon if m.icon.endswith(".svg") else "icon.svg")
    if name not in m.icon_files():
        raise HTTPException(404, "No such icon")
    f = Path(m.source_dir) / name
    if not f.is_file() or f.is_symlink() or f.stat().st_size > ICON_MAX_BYTES:
        raise HTTPException(404, "No such icon")
    return Response(f.read_bytes(), media_type="image/svg+xml", headers=_ICON_HEADERS)


@app.get("/api/unfurl")
async def api_unfurl(request: Request, url: str = ""):
    """Auto-unfurl an integration link rendered in a message into a small preview card. The
    frontend calls this once per recognized link after rendering a message
    bubble (see enhanceLinkPreviews() in app.js) - unrecognized URLs, or
    lookups that fail (404, auth, network), return {"type": null} rather
    than an HTTP error so a bad/expired link just quietly doesn't get a
    card instead of surfacing a scary error in the transcript."""
    _require_auth(request)
    cached = _unfurl_cache.get(url)
    if cached is not None and (time.time() - cached[0]) < furl_api.ttl():
        return cached[1]

    result = await asyncio.to_thread(furl_api.unfurl, url)
    _unfurl_cache[url] = (time.time(), result)
    return result


_usage_top_cache: dict[str, Any] = {"at": 0.0, "kinds": {}}


def _session_kind(conversation_id: str) -> str:
    """'watcher' when most non-status turns are watch fires/replies, else 'human' (cached by caller)."""
    turns = message_kinds.classify_turns([
        {"id": m["id"], "role": m["role"], "text": m["content"], "timestamp": m["created_at"], "updated_at": m["updated_at"]}
        for m in message_store.get_messages(conversation_id)
    ])
    real = [t for t in turns if t.get("kind") != "status"]
    watcher = [t for t in real if t.get("kind") in ("watch_fired", "watch_reply", "watch_registered")]
    return "watcher" if real and len(watcher) * 2 > len(real) else "human"


def _find_live_client():
    for convo in list((_convos or {}).values()):
        client = getattr(convo, "_client", None)
        if client is not None:
            return client
    return None


@app.get("/api/usage")
async def api_usage(request: Request, conversation_id: str | None = None, refresh: bool = False):
    """Header usage widget data: all-session totals/series/top, quota, and the selected conversation's
    AIU/context/cache (see usage_index.py). `enabled:false` when USAGE_WIDGET=off."""
    _require_auth(request)
    if not usage_index.enabled():
        return {"enabled": False}
    usage_index.ensure_background_scan()
    if usage_index.indexing():
        return {"enabled": True, "indexing": True}
    data = await asyncio.to_thread(usage_index.summary)
    by_sid = {s.get("session_id"): s for s in message_store.list_conversations()}
    now = time.monotonic()
    if now - _usage_top_cache["at"] > 300:
        _usage_top_cache["kinds"] = {}
        _usage_top_cache["at"] = now
    for rng in data["ranges"].values():
        for row in rng["top"]:
            convo = by_sid.get(row["session_id"])
            row["conversation_id"] = convo["conversation_id"] if convo else None
            row["title"] = (convo or {}).get("title") or f"(untitled) {row['session_id'][:8]}"
            if convo:
                kinds = _usage_top_cache["kinds"]
                if convo["conversation_id"] not in kinds:
                    kinds[convo["conversation_id"]] = await asyncio.to_thread(_session_kind, convo["conversation_id"])
                row["kind"] = kinds[convo["conversation_id"]]
            else:
                row["kind"] = "cli"
    data["enabled"] = True
    data["indexing"] = False
    data["quota"] = await usage_index.fetch_quota(_find_live_client(), force=refresh)
    if conversation_id:
        convo = message_store.get_conversation(conversation_id)
        data["session"] = usage_index.session_detail((convo or {}).get("copilot_session_id"), conversation_id)
    return data


@app.get("/api/voice/status")
async def api_voice_status():
    return transcription.whisper_status()


@app.get("/api/health")
async def api_health(request: Request):
    """Header health/status strip data: bot uptime, live Slack API
    connectivity (a real auth.test call, not just "the process is up"),
    a short git-commit "version" identifier, which MCP servers were
    loaded at startup (config-loaded, not individually live-pinged -
    verifying each MCP server's own connectivity would be much more
    expensive and isn't what was asked for), a per-server status summary
    (mcp_status — static sanity checks per configured server, plus this
    process's own in-process /mcp server's real auth-token status, see
    bot_config.probe_mcp_server_statuses()), and the configured
    per-turn SESSION_TIMEOUT (seconds) so the frontend can warn when an
    in-progress turn is approaching it."""
    _require_auth(request)
    slack_connected = False
    if _slack_client is not None:
        try:
            resp = await _slack_client.auth_test()
            slack_connected = bool(resp.get("ok"))
        except Exception:
            slack_connected = False
    adapter_status = []
    if _slack_client is not None:
        adapter_status.append({"name": "Slack", "status": "ok" if slack_connected else "error",
                               "detail": "connected (auth.test ok)" if slack_connected else "disconnected (auth.test failed)"})
    tg = adapters.REGISTRY.get("telegram")
    if tg is not None:
        adapter_status.append({"name": "Telegram", "status": "ok", "detail": "registered, long polling"})
    adapter_status.append({"name": "Web", "status": "ok", "detail": "serving this UI"})
    mcp_servers = sorted((bot_config.MCP_SERVERS or {}).keys())
    return {
        "adapters": adapter_status,
        "uptime_seconds": time.time() - _START_TIME,
        "bot_version": _BOT_VERSION,
        "slack_connected": slack_connected,
        "mcp_servers": mcp_servers,
        "mcp_configured": bool(mcp_servers),
        "mcp_status": bot_config.probe_mcp_server_statuses(),
        "session_timeout_seconds": bot_config.SESSION_TIMEOUT,
        "workdir": bot_config.COPILOT_WORKDIR,
    }


@app.get("/login")
async def login_page():
    if not WEB_UI_FRONTEND_ENABLED:
        raise HTTPException(404)
    return FileResponse(WEBUI_DIR / "login.html")


def _asset_version(filename: str) -> str:
    """mtime-derived cache-busting token for a static asset, appended as ?v=<mtime>
    to script/style tags so browsers fetch changed files without a hard refresh.
    """
    try:
        return str(int(os.path.getmtime(WEBUI_DIR / filename)))
    except OSError:
        return "0"


@app.get("/")
async def index_page(request: Request):
    if not WEB_UI_FRONTEND_ENABLED:
        raise HTTPException(404)
    if not _cookie_is_valid(request.cookies.get(_COOKIE_NAME)):
        return RedirectResponse("/login")
    html = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={_asset_version("style.css")}"')
    html = html.replace('href="/static/ideas.css"', f'href="/static/ideas.css?v={_asset_version("ideas.css")}"')
    html = html.replace('src="/static/ideas.js"', f'src="/static/ideas.js?v={_asset_version("ideas.js")}"')
    html = html.replace('href="/static/ask.css"', f'href="/static/ask.css?v={_asset_version("ask.css")}"')
    html = html.replace('src="/static/ask.js"', f'src="/static/ask.js?v={_asset_version("ask.js")}"')
    html = html.replace('href="/static/actions.css"', f'href="/static/actions.css?v={_asset_version("actions.css")}"')
    html = html.replace('src="/static/actions.js"', f'src="/static/actions.js?v={_asset_version("actions.js")}"')
    html = html.replace('href="/static/mermaid.css"', f'href="/static/mermaid.css?v={_asset_version("mermaid.css")}"')
    html = html.replace('src="/static/mermaid.js"', f'src="/static/mermaid.js?v={_asset_version("mermaid.js")}"')
    html = html.replace('href="/static/chart.css"', f'href="/static/chart.css?v={_asset_version("chart.css")}"')
    html = html.replace('src="/static/chart.js"', f'src="/static/chart.js?v={_asset_version("chart.js")}"')
    html = html.replace('href="/static/settings.css"', f'href="/static/settings.css?v={_asset_version("settings.css")}"')
    html = html.replace('src="/static/settings.js"', f'src="/static/settings.js?v={_asset_version("settings.js")}"')
    html = html.replace('href="/static/light.css"', f'href="/static/light.css?v={_asset_version("light.css")}"')
    html = html.replace('src="/static/theme.js"', f'src="/static/theme.js?v={_asset_version("theme.js")}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_asset_version("app.js")}"')
    return HTMLResponse(html)


if WEB_UI_FRONTEND_ENABLED:
    app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")
# Mounted with an empty prefix: the sub-app's only route is its own default
# "/mcp" path (set by FastMCP), so this doesn't shadow any other route
# registered above, and avoids the redirect FastAPI/Starlette would issue
# for a "/mcp" -> "/mcp/" mount-path mismatch.
app.mount("", _mcp_asgi_app)


async def run_in_background(ready_event: asyncio.Event | None = None) -> None:
    """Run the FastAPI app (Web UI + the self-hosted /mcp server exposing
    watch_*/session_* tools — see bot_config.SESSION_MCP_SERVERS) under
    uvicorn, as a background asyncio task alongside the rest of the bot.

    ``ready_event``, if given, is set the moment uvicorn's socket is
    actually bound and accepting connections (``server.started`` flips
    True at the end of ``Server.startup()``) — NOT merely once this
    coroutine/task has been scheduled. Callers that create/resume a Copilot
    session whose mcp_servers= includes this process's own /mcp endpoint
    (i.e. every bot-spawned session) MUST await this event first: a session
    created/resumed before the socket is actually listening gets a
    connection-refused when the Copilot CLI SDK tries to reach
    http://127.0.0.1:{WEB_UI_PORT}/mcp, and the SDK marks that MCP server
    unreachable for the rest of that session's lifetime — it does not
    retry later once the port comes up.
    """
    import uvicorn

    if not WEB_UI_PASSWORD:
        log.warning(
            "WEB_UI_PASSWORD is not set in .env — the web UI is installed but will refuse ALL logins until you set one."
        )

    config = uvicorn.Config(
        app,
        host=WEB_UI_HOST,
        port=WEB_UI_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def _signal_ready_once_listening() -> None:
        # Poll rather than assume a fixed delay: startup time depends on
        # real system load, and a fixed sleep would either race a slow
        # start or needlessly delay a fast one.
        while not server.started:
            await asyncio.sleep(0.01)
        log.info("Web UI listening on http://%s:%s", WEB_UI_HOST, WEB_UI_PORT)
        if ready_event is not None:
            ready_event.set()

    if ready_event is not None:
        asyncio.create_task(_signal_ready_once_listening())
    await server.serve()
