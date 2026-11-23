"""Lazy Telegram configuration (read on call, never at import, so tests can
monkeypatch the environment and Slack/Web never depend on Telegram vars).

Telegram is OFF unless a bot token is configured (``TELEGRAM_BOT_TOKEN`` or a
mode-600 ``TELEGRAM_BOT_TOKEN_FILE``) and ``adapters.telegram.enabled`` is true.
The token is only ever returned by ``bot_token()``; nothing here logs it.
"""

from __future__ import annotations

import os

import config

def bot_token() -> str:
    return config.secret_value("adapters.telegram.bot_token")


def allowed_user_ids() -> list[str]:
    return [p for p in config.value("adapters.telegram.allowed_user_ids") if p.lstrip("-").isdigit()]


def forum_chat_id() -> int | None:
    return config.value("adapters.telegram.forum_chat_id")


def max_update_age() -> float:
    return config.value("adapters.telegram.max_update_age_s")


def poll_timeout() -> int:
    return int(config.value("adapters.telegram.poll_timeout_s"))


def new_session_platform() -> str:
    return config.value("agent.new_session_platform").lower()


def enabled() -> bool:
    return bool(config.value("adapters.telegram.enabled")) and bool(bot_token())
