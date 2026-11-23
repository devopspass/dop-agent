"""Transport/API layer: retries, error mapping, and the token never leaking."""
import logging

import httpx
import pytest

from telegram_api import HttpxTransport, TelegramApi, TelegramError, TelegramNetworkError, redact
from telegram_rig import FORUM, Rig

TOKEN = "123456:SECRET-token-VALUE"


async def test_429_sleeps_retry_after_once_then_succeeds():
    r = Rig()
    r.server.fail_next("sendMessage", TelegramError(429, "Too Many Requests: retry after 3", retry_after=3))
    await r.api.send_message(FORUM, "hi")
    assert 3 in r.sleeps and len(r.server.sent()) == 1


async def test_429_with_raise_policy_raises_immediately():
    r = Rig()
    r.server.fail_next("sendMessage", TelegramError(429, "Too Many Requests", retry_after=3))
    with pytest.raises(TelegramError):
        await r.api.send_message(FORUM, "hi", on_429="raise")
    assert r.sleeps == []


async def test_edit_with_identical_text_is_success():
    r = Rig()
    m = await r.api.send_message(FORUM, "same")
    await r.api.edit_message_text(FORUM, m["message_id"], "same")


async def test_get_updates_always_requests_explicit_allowed_updates():
    r = Rig()
    await r.api.get_updates(offset=None)
    assert "callback_query" in r.server.calls_to("getUpdates")[0]["allowed_updates"]


async def test_token_never_appears_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        raise httpx.ConnectError("boom " + str(request.url))

    api = TelegramApi(HttpxTransport(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    with pytest.raises(TelegramNetworkError) as ei:
        await api.get_me()
    assert TOKEN not in str(ei.value) and TOKEN not in caplog.text
    assert TOKEN not in redact(f"https://api.telegram.org/bot{TOKEN}/getMe")


async def test_http_error_maps_to_telegram_error_with_code():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    api = TelegramApi(HttpxTransport(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    with pytest.raises(TelegramError) as ei:
        await api.get_me()
    assert ei.value.code == 401 and TOKEN not in str(ei.value)
