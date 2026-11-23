"""TARGET adapter interface properties (registry, capabilities, neutral messages, ...).

Each test describes one property the interface must have so that a new chat
platform (Telegram, ...) is a drop-in: write an adapter, register it, pass
the shared contract. Section markers (M1-M8, G5/G6) group the properties by
topic.
"""

from __future__ import annotations

import pytest

import message_store
from contract_harness import make_fake, make_slack, make_web
from fake_adapter import FAKE_PLATFORM, FakeAdapter

m1 = pytest.mark.xfail(strict=True, reason="M1: adapter registry + adapter-owned binding helpers")
m2 = pytest.mark.xfail(strict=True, reason="M2: capabilities object")


# ---------------------------------------------------------------- M1 ---------

def test_every_adapter_declares_its_platform_name():
    """The registry keys on ``adapter.platform`` — no external mapping."""
    assert make_slack().adapter.platform == message_store.SLACK_PLATFORM
    assert make_web().adapter.platform == message_store.WEB_PLATFORM
    assert make_fake().adapter.platform == FAKE_PLATFORM


def test_registry_resolves_adapters_by_platform_and_lists_platforms():
    from adapters import AdapterRegistry

    slack, web, fake = make_slack().adapter, make_web().adapter, make_fake().adapter
    reg = AdapterRegistry([slack, web, fake])

    assert reg.get(message_store.SLACK_PLATFORM) is slack
    assert reg.get(FAKE_PLATFORM) is fake
    assert reg.get("nope") is None
    assert set(reg.platforms()) == {"slack", "web", "fake"}


def test_adapters_translate_bindings_to_thread_keys_and_back():
    """Binding <-> thread_key translation lives on the adapter, so the core
    (broadcaster, watchers) never branches on platform names."""
    fake = make_fake().adapter
    key = fake.thread_key_from_binding("42:7")

    assert key == {"chat_id": 42, "topic_id": 7}
    assert fake.external_key(key) == "42:7"

    slack = make_slack().adapter
    assert slack.thread_key_from_binding("C1:100.1") == {"channel": "C1", "thread_ts": "100.1"}
    assert slack.external_key({"channel": "C1", "thread_ts": "100.1"}) == "C1:100.1"


async def test_broadcaster_mirrors_to_a_third_platform_without_core_changes():
    """Binding a conversation to the fake platform is enough for fan-out to
    reach it — no ``if platform == ...`` branch in the broadcaster."""
    from adapters import AdapterRegistry, ConversationBroadcaster

    web, fake = make_web(), make_fake()
    conv = web.conversation_id
    message_store.bind_conversation(conv, message_store.WEB_PLATFORM, conv)
    message_store.bind_conversation(conv, FAKE_PLATFORM, "42:7")
    broadcaster = ConversationBroadcaster(AdapterRegistry([web.adapter, fake.adapter]))

    deliveries = await broadcaster.fanout_send(conv, "hi", role="user")

    assert FAKE_PLATFORM in deliveries
    assert fake.adapter.sent[0]["thread_key"] == {"chat_id": 42, "topic_id": 7}


# ---------------------------------------------------------------- M2 ---------

def test_capabilities_describe_each_platform_instead_of_raising_not_implemented():
    slack, web, fake = make_slack().adapter, make_web().adapter, make_fake().adapter

    assert slack.capabilities.max_chars == 3900 and slack.capabilities.supports_reactions
    assert web.capabilities.max_chars is None and not web.capabilities.supports_uploads
    assert fake.capabilities.max_chars == 40 and not fake.capabilities.supports_delete
    assert slack.capabilities.supports_buttons and not fake.capabilities.supports_buttons
    assert fake.capabilities.edit_min_interval > 0


def test_format_markdown_respects_the_adapters_own_max_chars():
    fake = make_fake().adapter

    assert all(len(c) <= fake.capabilities.max_chars for c in fake.format_markdown("w " * 500))


# ---------------------------------------------------------------- M3 ---------

async def test_buttons_are_neutral_and_each_adapter_renders_them_natively():
    from adapters import Button, deliver

    btn = [Button(label="Stop", action_id="stop_working_turn", value="conv-1")]
    slack, web, fake = make_slack(), make_web(), make_fake()

    await deliver(slack.adapter, slack.thread_key, "Working", conversation_id=slack.conversation_id, buttons=btn)
    await deliver(web.adapter, web.thread_key, "Working", conversation_id=web.conversation_id, buttons=btn)
    await deliver(fake.adapter, fake.thread_key, "Working", conversation_id=fake.conversation_id, buttons=btn)

    blocks = slack.extra["client"].posted[0]["blocks"]
    assert any(e.get("action_id") == "stop_working_turn" for b in blocks for e in b.get("elements", []))
    assert message_store.get_messages(web.conversation_id)[0]["actions"]
    assert fake.adapter.sent[0]  # fake ignores buttons gracefully


async def test_slack_adapter_adds_mrkdwn_itself_so_the_core_does_not_have_to():
    slack = make_slack()

    await slack.adapter.send(slack.thread_key, "x", conversation_id=slack.conversation_id)

    assert slack.extra["client"].posted[0]["mrkdwn"] is True


def test_core_modules_no_longer_build_slack_block_kit():
    """AST-based (not a text regex, which false-positives on prose): no call
    in the core passes ``blocks=``/``mrkdwn=``/``actions=`` keywords and no
    string literal equals ``"mrkdwn"``/``"blocks"`` - Slack's wire format is
    the SlackAdapter's business only."""
    import ast, pathlib

    bot = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for name in ("handlers.py", "watchers.py", "web_server.py", "dop_agent.py", "persistence.py"):
        tree = ast.parse((bot / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg in {"blocks", "mrkdwn", "actions"}:
                offenders.append(f"{name}:{node.value.lineno} keyword {node.arg}=")
            elif isinstance(node, ast.Constant) and node.value in {"mrkdwn", "blocks"}:
                offenders.append(f"{name}:{node.lineno} literal {node.value!r}")
    assert not offenders, offenders


# ---------------------------------------------------------------- M4 ---------

async def test_adapters_hand_out_message_refs_that_update_accepts_unchanged():
    """No per-platform shim (``ref_for``) needed to edit what you just sent."""
    from adapters import deliver, deliver_update
    for make in (make_slack, make_web, make_fake):
        h = make()
        # send() is pure transport; the pair comes from deliver()
        result, message_id = await deliver(h.adapter, h.thread_key, "a", conversation_id=h.conversation_id)
        ref = h.adapter.message_ref(h.thread_key, result, message_id)

        await deliver_update(h.adapter, h.thread_key, ref, "b", conversation_id=h.conversation_id,
                             message_id=message_id)

        assert "b" in h.delivered(), h.name


def test_message_ref_is_platform_neutral_and_has_a_stable_string_key():
    from adapters import MessageRef

    ref = MessageRef(platform="fake", conversation_key="42", id="1001")

    assert ref.key() == "fake:42:1001"
    assert MessageRef.parse(ref.key()) == ref


async def test_adapter_send_is_pure_transport_and_deliver_records_exactly_once(monkeypatch):
    """Spy on ``message_store.record_message``: adapters only move bytes
    (0 recordings during ``send``); ``adapters.deliver`` is the one place
    that records (exactly 1) - for every platform including Web, whose
    ``send`` is then a no-op transport (the UI reads the store)."""
    from adapters import deliver

    for make in (make_slack, make_web, make_fake):
        h = make()
        calls = []
        real = message_store.record_message
        monkeypatch.setattr(message_store, "record_message",
                            lambda *a, _real=real, **k: (calls.append(a), _real(*a, **k))[1])

        await h.adapter.send(h.thread_key, "transport only", conversation_id=h.conversation_id)
        assert calls == [], f"{h.name}: send() must not record"

        await deliver(h.adapter, h.thread_key, "recorded", conversation_id=h.conversation_id, role="assistant")
        assert len(calls) == 1, f"{h.name}: deliver() must record exactly once"
        monkeypatch.setattr(message_store, "record_message", real)


# ---------------------------------------------------------------- M5 ---------

def test_authorization_is_per_platform_via_allowed_identities():
    """A Telegram-style user id is allowed by adding it to
    ``allowed_identities``, no code change."""
    from handlers import is_authorized

    message_store.add_allowed_identity(FAKE_PLATFORM, "tg-user-1")

    assert is_authorized("tg-user-1", platform=FAKE_PLATFORM)
    assert not is_authorized("someone-else", platform=FAKE_PLATFORM)


def test_incoming_message_is_a_platform_neutral_value_object():
    from adapters import IncomingMessage

    msg = IncomingMessage(platform="fake", user_id="u", conversation_key="42", text="hi")

    assert msg.attachments == () and msg.ref is None


async def test_core_handles_an_incoming_message_from_any_adapter_end_to_end():
    """One shared inbound path: authorize -> resolve conversation by binding
    -> run turn -> reply through the SAME adapter. Fake stands in for a new
    platform; the turn engine is stubbed."""
    from adapters import AdapterRegistry, IncomingMessage
    from handlers import handle_incoming

    fake = make_fake().adapter
    message_store.add_allowed_identity(FAKE_PLATFORM, "tg-user-1")

    async def engine(conversation_id, text):
        return f"echo: {text}"

    await handle_incoming(
        IncomingMessage(platform=FAKE_PLATFORM, user_id="tg-user-1", conversation_key="42", text="ping"),
        registry=AdapterRegistry([fake]), run_turn=engine,
    )

    assert any("echo: ping" in s["text"] for s in fake.sent)


async def test_unauthorized_incoming_message_gets_no_turn_and_no_reply():
    from adapters import AdapterRegistry, IncomingMessage
    from handlers import handle_incoming

    fake = make_fake().adapter
    calls = []

    async def engine(conversation_id, text):
        calls.append(text)
        return "x"

    await handle_incoming(
        IncomingMessage(platform=FAKE_PLATFORM, user_id="intruder", conversation_key="42", text="rm -rf"),
        registry=AdapterRegistry([fake]), run_turn=engine,
    )

    assert calls == [] and fake.sent == []


# ---------------------------------------------------------------- M6 ---------

async def test_rapid_status_edits_are_coalesced_to_the_platform_edit_rate():
    """The interval comes from the adapter's own capabilities (not a
    caller-supplied number) and edits address a MessageRef, so the same
    editor works for any platform."""
    from adapters import MessageRef, ThrottledEditor

    now = [0.0]
    fake = make_fake()
    editor = ThrottledEditor(fake.adapter, fake.thread_key, clock=lambda: now[0])
    assert editor.min_interval == fake.adapter.capabilities.edit_min_interval > 0
    ref = MessageRef(platform=FAKE_PLATFORM, conversation_key="42", id="1")

    for i in range(10):  # ten edits inside one second
        now[0] += 0.1
        await editor.edit(ref, f"step {i}", conversation_id=fake.conversation_id)
    await editor.flush()

    assert 1 <= len(fake.adapter.edits) <= 3
    assert fake.adapter.edits[-1]["text"] == "step 9"  # the latest text always lands


# ---------------------------------------------------------------- M7 ---------

def test_session_payload_lists_every_bound_platform_with_a_link_per_platform():
    from web_server import platform_links_for

    conv = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conv, FAKE_PLATFORM, "42:7")
    message_store.bind_conversation(conv, message_store.SLACK_PLATFORM, "C1:100.1")

    links = {l["platform"]: l for l in platform_links_for(conv)}

    assert set(links) == {"fake", "slack"}
    assert links["slack"]["url"].startswith("slack://")
    assert links["fake"].get("url") is None  # unknown platform: badge only, no link


# ---------------------------------------------------------------- M8 ---------

def test_conversations_table_has_no_legacy_slack_key_column():
    import sqlite3

    message_store.init_db()
    with sqlite3.connect(message_store.DB_PATH) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]

    assert "slack_key" not in cols


# ----------------------------------------------- M1 (watch delivery) ---------

@pytest.fixture
def fake_in_registry():
    import adapters

    fake = make_fake()
    adapters.REGISTRY.register(fake.adapter)
    message_store.bind_conversation(fake.conversation_id, FAKE_PLATFORM, "42:7")
    yield fake
    adapters.REGISTRY.unregister(FAKE_PLATFORM)


async def test_system_notice_reaches_a_conversation_bound_only_to_a_third_platform(fake_in_registry):
    """Watch "registered/broken" notices pick their adapter from the
    conversation's bindings via the registry: a conversation bound to the
    fake platform gets the notice through the fake adapter, not Web/Slack."""
    import watchers

    await watchers._deliver_system_notice(fake_in_registry.conversation_id, "watch notice")

    assert [s["text"] for s in fake_in_registry.adapter.sent] == ["watch notice"]
    assert fake_in_registry.adapter.sent[0]["thread_key"] == {"chat_id": 42, "topic_id": 7}


async def test_watch_fire_turn_is_delivered_through_the_bound_platforms_adapter(fake_in_registry, monkeypatch):
    """``_inject_watch_message`` must hand the turn engine the adapter and
    thread key resolved from the binding (no Slack ``say``/``client`` shims),
    and its raw notice goes out through that same adapter."""
    import watchers

    seen = {}

    async def fake_handle(**kw):
        seen.update(kw)

    monkeypatch.setattr(watchers, "_handle_fn", fake_handle)

    await watchers._inject_watch_message_now(fake_in_registry.conversation_id, "fired", rules=False)

    assert seen["adapter"] is fake_in_registry.adapter
    assert seen["thread_key"] == {"chat_id": 42, "topic_id": 7}
    assert "say" not in seen and "client" not in seen and "channel" not in seen


def test_watchers_module_no_longer_imports_slack_sdk():
    """The immediate watch announcement goes through the adapter registry,
    not a synchronous ``slack_sdk.WebClient``."""
    import ast, pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "watchers.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith("slack_sdk") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("slack_sdk")


def test_watch_announcement_for_unreachable_platform_is_left_for_the_daemon_retry(monkeypatch):
    """A conversation bound to a platform with no live adapter in this
    process (CLI call, bot down) must NOT be silently diverted into the Web
    log: ``_post_watch_announcement`` returns False so the tick retries."""
    import watchers

    conv = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conv, "ghost", "1")

    assert watchers._post_watch_announcement(1, "time", "k", "l", conv, "", None, "2026-01-01T00:00:00") is False
    assert message_store.get_messages(conv) == []


# ----------------------------------------------- G5 / G6 -----------------------

async def test_web_server_has_no_slack_special_casing_and_mirrors_to_any_bound_platform():
    """Outside the single link-builder, ``web_server.py`` never names the
    Slack platform; mirroring a Web UI message loops over bindings, so a
    fake-bound conversation receives it."""
    import ast, pathlib
    from adapters import AdapterRegistry, ConversationBroadcaster

    src = (pathlib.Path(__file__).resolve().parents[2] / "web_server.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if fn.name == "platform_links_for":
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr == "SLACK_PLATFORM":
                offenders.append(f"{fn.name}:{node.lineno}")
    assert not offenders, offenders

    web, fake = make_web(), make_fake()
    message_store.bind_conversation(web.conversation_id, FAKE_PLATFORM, "42:7")
    deliveries = await ConversationBroadcaster(AdapterRegistry([web.adapter, fake.adapter])).fanout_send(
        web.conversation_id, "from web", role="user")
    assert FAKE_PLATFORM in deliveries


async def test_duplicate_incoming_message_with_same_ref_runs_the_turn_once():
    """Platforms redeliver updates after reconnects (Slack events, Telegram
    getUpdates). The shared inbound path dedups on ``msg.ref``."""
    from adapters import AdapterRegistry, IncomingMessage
    from handlers import handle_incoming

    fake = make_fake().adapter
    message_store.add_allowed_identity(FAKE_PLATFORM, "tg-user-1")
    runs = []

    async def engine(conversation_id, text):
        runs.append(text)
        return "ok"

    msg = IncomingMessage(platform=FAKE_PLATFORM, user_id="tg-user-1", conversation_key="42",
                          text="once", ref="fake:42:9001")
    registry = AdapterRegistry([fake])
    await handle_incoming(msg, registry=registry, run_turn=engine)
    await handle_incoming(msg, registry=registry, run_turn=engine)

    assert runs == ["once"]
