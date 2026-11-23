"""Daemon wiring for Telegram (kept out of ``dop_agent.py`` so Slack/Web never
depend on it). ``setup()`` is a no-op returning ``None`` unless a token is
configured, ``adapters.telegram.enabled`` is true and at least
one allowed user id is set (fail closed). It must be called BEFORE
``web_server.init`` and crash recovery (``_resume_interrupted_turns`` looks the
adapter up in the registry and drops entries whose platform is unregistered);
the poller task is started AFTER recovery via ``start_poller``. Every failure is
logged and swallowed - Telegram can never take Slack or the Web UI down.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import adapters
import config
import telegram_config as cfg
from telegram_adapter import TelegramAdapter
from telegram_api import HttpxTransport, TelegramApi, redact
from telegram_poller import TelegramPoller

log = logging.getLogger("tg.boot")


@dataclass
class TelegramRuntime:
    api: TelegramApi
    adapter: TelegramAdapter
    poller: TelegramPoller
    task: "asyncio.Task | None" = None


def setup(*, registry=None, transport=None) -> TelegramRuntime | None:
    registry = registry or adapters.REGISTRY
    if not config.value("adapters.telegram.enabled"):
        log.info("Telegram disabled (adapters.telegram.enabled is false)")
        return None
    try:
        token = cfg.bot_token()
    except PermissionError as exc:
        log.error("Telegram disabled: %s", exc)
        return None
    if not token and transport is None:
        return None
    allowed = cfg.allowed_user_ids()
    if not allowed:
        log.error("Telegram token configured but TELEGRAM_ALLOWED_USER_IDS is empty - refusing to start (fail closed)")
        return None
    try:
        import message_store

        message_store.init_db()  # seeds/revokes the telegram allow-list from the environment
        api = TelegramApi(transport or HttpxTransport(token), poll_timeout=cfg.poll_timeout())
        adapter = TelegramAdapter(api, forum_chat_id=cfg.forum_chat_id(), allowed_user_ids=allowed)
        adapter.attach_loop(asyncio.get_running_loop())
        poller = TelegramPoller(api, adapter, allowed_user_ids=allowed, forum_chat_id=cfg.forum_chat_id(),
                                max_update_age=cfg.max_update_age(), poll_timeout=cfg.poll_timeout())
        registry.register(adapter)
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram setup failed (continuing without it): %s", redact(exc))
        return None
    log.info("Telegram adapter registered (forum chat: %s, allowed users: %d)",
             "yes" if cfg.forum_chat_id() else "no (DM mode)", len(allowed))
    return TelegramRuntime(api, adapter, poller)


def start_poller(runtime: TelegramRuntime | None) -> None:
    """Fire-and-forget the long-poll loop (call after crash recovery)."""
    if runtime is None:
        return

    async def _run() -> None:
        try:
            await runtime.poller.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram poller crashed: %s", redact(exc))

    runtime.task = asyncio.create_task(_run())
