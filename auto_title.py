"""Auto-generate a short session title from the first user message.

Until a title is set, the Web UI shows the first message text. For new
sessions we ask the cheap model for a 3-7 word title in the background and
store it as a normal custom title. Best effort: any failure keeps the
first-message fallback. A title set by the user (or session_create) while the
model call is in flight is never overwritten.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

import config
import message_store
import model_routing

log = logging.getLogger("dop-agent.auto_title")

MAX_TITLE_LEN = 60
TIMEOUT_S = 20.0
_MAX_INPUT = 2000

_SYSTEM = "You write short chat-session titles. Never use tools. Reply with the title only."

_PROMPT = (
    "Write a concise title (3-7 words) for a chat session that starts with the request below. "
    "Use the same language as the request. Reply with ONLY the title: no quotes, no trailing "
    "punctuation, no prefix.\n\nRequest:\n{text}"
)


def enabled() -> bool:
    return bool(config.value("agent.auto_title"))


def clean_title(raw: str | None) -> str:
    """Normalise a model reply into a single-line title (``''`` if unusable)."""
    lines = [ln.strip() for ln in (raw or "").strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    t = re.sub(r"^(title\s*:\s*)", "", lines[0], flags=re.IGNORECASE)
    t = t.strip().strip("\"'`*#").strip().rstrip(".:;,")
    t = " ".join(t.split())
    if len(t) > MAX_TITLE_LEN:
        t = t[: MAX_TITLE_LEN - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return t


def _scratch_dir() -> str:
    """Empty cwd so the title session sees no repo, AGENTS.md or instructions."""
    d = os.path.join(tempfile.gettempdir(), "dop-agent-title")
    os.makedirs(d, exist_ok=True)
    return d


def _session_kwargs(model: str) -> dict:
    """Minimal, tool-less, instruction-free session options (title only)."""
    from copilot.generated.rpc import PermissionDecisionReject

    def _deny_all(request, invocation):  # noqa: ARG001
        return PermissionDecisionReject()

    return dict(
        on_permission_request=_deny_all,
        working_directory=_scratch_dir(),
        model=model,
        available_tools=[],
        system_message={"mode": "replace", "content": _SYSTEM},
        mcp_servers={},
        disabled_mcp_servers=["github-mcp-server", "githubiq"],
        enable_skills=False,
        skip_custom_instructions=True,
        enable_config_discovery=False,
        enable_on_demand_instruction_discovery=False,
        enable_session_store=False,
        enable_file_hooks=False,
        infinite_sessions={"enabled": False},
    )


async def _ask_model(prompt: str) -> str:
    from copilot import CopilotClient  # lazy: SDK only needed at runtime
    model = config.value("agent.auto_title_model") or model_routing.cheap_model()
    client = CopilotClient(working_directory=_scratch_dir(), github_token=config.copilot_github_token() or None)
    await client.__aenter__()
    session = None
    session_id = None
    try:
        session = await client.create_session(**_session_kwargs(model))
        session_id = session.session_id
        event = await session.send_and_wait(prompt, timeout=TIMEOUT_S)
        return (getattr(getattr(event, "data", None), "content", "") or "").strip()
    finally:
        try:
            if session is not None:
                await session.disconnect()
        finally:
            try:  # don't leave a throwaway session dir in ~/.copilot/session-state
                if session_id:
                    await client.delete_session(session_id)
            except Exception as exc:  # noqa: BLE001
                log.debug("title session cleanup failed: %s", exc)
            await client.__aexit__(None, None, None)


async def auto_title(conversation_id: str, text: str, ask=None) -> str | None:
    """Generate and store a title; returns it, or None if skipped/failed."""
    if not enabled():
        return None
    body = message_store._extract_title(text)[:_MAX_INPUT]
    if not body or body == "(new session)":
        return None
    try:
        raw = await asyncio.wait_for((ask or _ask_model)(_PROMPT.format(text=body)), TIMEOUT_S + 5)
    except Exception as exc:  # noqa: BLE001 - best effort, fallback title stays
        log.warning("[%s] auto-title failed: %s", conversation_id, exc)
        return None
    title = clean_title(raw)
    if not title:
        return None
    try:
        if message_store.get_title(conversation_id):  # user/session_create set one meanwhile
            return None
        message_store.set_title(conversation_id, title)
    except KeyError:
        return None
    log.info("[%s] auto-title: %r", conversation_id, title)
    return title
