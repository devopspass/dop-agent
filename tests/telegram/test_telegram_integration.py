"""Config, allow-list, boot wiring and cross-component behaviour."""
import asyncio

import adapters
import handlers
import message_store
import telegram_boot
import telegram_config as cfg
import web_server
from telegram_rig import FORUM, USER, Rig


def test_disabled_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_FILE", raising=False)
    assert cfg.enabled() is False
    assert "telegram" not in adapters.REGISTRY._adapters


async def test_setup_is_noop_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_FILE", raising=False)
    assert telegram_boot.setup() is None


async def test_setup_fails_closed_without_allowed_users(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:fake")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    assert telegram_boot.setup() is None


async def test_disabled_in_config_skips_setup_even_with_token(monkeypatch):
    import config

    real = config.value
    monkeypatch.setattr(config, "value", lambda p: False if p == "adapters.telegram.enabled" else real(p))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    assert telegram_boot.setup() is None


async def test_setup_registers_adapter_before_anything_else(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "4242")
    rig = Rig()
    rt = telegram_boot.setup(transport=rig.server)
    assert rt is not None and adapters.REGISTRY.get("telegram") is rt.adapter
    assert message_store.is_allowed_identity("telegram", "4242")


def test_allow_list_is_revocable_via_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2")
    message_store.init_db()
    assert message_store.is_allowed_identity("telegram", "1")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "2")
    message_store.init_db()
    assert not message_store.is_allowed_identity("telegram", "1")
    assert message_store.is_allowed_identity("telegram", "2")


def test_unset_env_leaves_existing_telegram_rows_alone(monkeypatch):
    message_store.add_allowed_identity("telegram", "9")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    message_store.init_db()
    assert message_store.is_allowed_identity("telegram", "9")


def test_unbind_conversation_removes_only_that_binding():
    cid = message_store.get_or_create_bound_conversation("telegram", "-100:5")
    assert message_store.unbind_conversation(cid, "telegram", "-100:5")
    assert message_store.resolve_conversation_binding("telegram", "-100:5") is None
    assert message_store.get_or_create_bound_conversation("telegram", "-100:5") != cid


async def test_session_context_describes_telegram_topic():
    ctx = await handlers._session_context(None, "", None, "1", "conv-1", platform="telegram",
                                          thread_key={"chat_id": FORUM, "topic_id": 5})
    assert "Telegram" in ctx and "conv-1" in ctx and "Web UI-only" not in ctx


async def test_web_origin_new_session_mirrors_into_a_fresh_topic(monkeypatch):
    rig = Rig()
    adapters.REGISTRY.register(rig.adapter)
    monkeypatch.setenv("NEW_SESSION_PLATFORM", "telegram")
    assert web_server._async_new_session_adapter(adapters.REGISTRY) is rig.adapter
    cid = message_store.get_or_create_conversation(None)
    _, tk = await rig.adapter.ensure_new_conversation_target(cid, "From web")
    assert message_store.resolve_conversation_binding("telegram", rig.adapter.external_key(tk)) == cid


def test_new_session_default_does_not_use_telegram(monkeypatch):
    monkeypatch.delenv("NEW_SESSION_PLATFORM", raising=False)
    assert web_server._async_new_session_adapter(adapters.REGISTRY) is None


async def test_archive_and_rename_sync_to_topic(monkeypatch):
    rig = Rig()
    rig.adapter.attach_loop(asyncio.get_running_loop())
    cid = message_store.get_or_create_conversation(None)
    tk = await rig.adapter.ensure_topic(cid, "Old")
    message_store.set_title(cid, "Brand new title")
    message_store.set_archived(cid, True)
    await asyncio.sleep(0.1)
    for t in list(rig.adapter._tasks):
        await t
    assert rig.server.calls_to("editForumTopic") and rig.server.calls_to("closeForumTopic")
    assert tk["topic_id"]


async def test_watch_fire_is_delivered_into_the_telegram_topic():
    rig = Rig()
    adapters.REGISTRY.register(rig.adapter)
    cid = message_store.get_or_create_bound_conversation("telegram", f"{FORUM}:12")
    adapter, tk = adapters.REGISTRY.delivery_for(cid)
    assert adapter is rig.adapter and tk["topic_id"] == 12
    rig.server.topics[(FORUM, 12)] = {"name": "t", "closed": False}
    await adapter.send(tk, "watch fired")
    assert any("watch fired" in m["text"] for m in rig.server.sent(FORUM))


async def test_recovery_needs_the_adapter_registered_first(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "4242")
    assert adapters.REGISTRY.get("telegram") is None
    rt = telegram_boot.setup(transport=Rig().server)
    assert adapters.REGISTRY.get("telegram") is rt.adapter
