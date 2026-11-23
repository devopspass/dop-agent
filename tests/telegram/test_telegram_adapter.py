"""TelegramAdapter against the strict fake: keyboards, uploads, reactions, topics, sync."""
import message_store
from fake_telegram import png
from adapters import Button
from telegram_api import TelegramError
from telegram_rig import FORUM, USER, Rig


def _conv(title="My session"):
    cid = message_store.create_conversation(title=title) if hasattr(message_store, "create_conversation") else None
    return cid


async def _topic(rig):
    return await rig.adapter.create_topic("T")


STOP = Button(label=":black_square_for_stop: Stop", action_id="stop_working_turn", value="conv-1", native_only=True)


async def test_long_markdown_is_split_and_every_part_valid(rig):
    tk = await _topic(rig)
    for part in rig.adapter.format_markdown("x" * 9000):
        await rig.adapter.send(tk, part)
    assert len(rig.server.sent(FORUM)) >= 3


async def test_html_parse_error_falls_back_to_plain_text(rig):
    tk = await _topic(rig)
    rig.server.fail_next("sendMessage", TelegramError(400, "Bad Request: can't parse entities: Unsupported start tag"))
    await rig.adapter.send(tk, "hello **x**")
    assert len(rig.server.sent(FORUM)) == 1


async def test_update_without_buttons_keeps_keyboard_and_empty_list_strips_it(rig):
    tk = await _topic(rig)
    res = await rig.adapter.send(tk, "working", buttons=[STOP])
    mid = res["message_id"]
    assert rig.server.keyboard(FORUM, mid)
    await rig.adapter.update(tk, mid, "still working")
    assert rig.server.keyboard(FORUM, mid)
    await rig.adapter.update(tk, mid, "done", buttons=[])
    assert rig.server.keyboard(FORUM, mid) is None


async def test_slack_only_kwargs_are_ignored(rig):
    tk = await _topic(rig)
    await rig.adapter.send(tk, "x", blocks=[{}], mrkdwn=True, thread_ts="1.2", unfurl_links=False)
    assert len(rig.server.sent(FORUM)) == 1


async def test_reactions_use_allowed_emoji_and_unknown_names_are_noops(rig):
    tk = await _topic(rig)
    res = await rig.adapter.send(tk, "x")
    ref = f"{FORUM}:{res['message_id']}"
    await rig.adapter.add_reaction(tk, "eyes", ref)
    await rig.adapter.add_reaction(tk, "definitely_not_an_emoji", ref)
    assert len(rig.server.calls_to("setMessageReaction")) == 1


async def test_photo_rejected_by_telegram_falls_back_to_document(rig):
    tk = await _topic(rig)
    rig.server.fail_next("sendPhoto", TelegramError(400, "Bad Request: PHOTO_INVALID_DIMENSIONS"))
    await rig.adapter.upload_file(tk, data=png(10, 10),
                                  filename="a.png")
    assert rig.server.calls_to("sendDocument")


async def test_deleted_topic_is_recreated_and_message_delivered(rig):
    cid = message_store.get_or_create_conversation(None)
    tk = await rig.adapter.ensure_topic(cid, "Doomed")
    rig.server.delete_topic(tk["chat_id"], tk["topic_id"])
    await rig.adapter.send(tk, "after delete")
    assert any("after delete" in m["text"] for m in rig.server.sent(FORUM))
    new_tk = await rig.adapter.ensure_topic(cid)
    assert new_tk["topic_id"] != tk["topic_id"]


async def test_closed_topic_is_reopened_to_send(rig):
    tk = await _topic(rig)
    await rig.api.close_forum_topic(FORUM, tk["topic_id"])
    await rig.adapter.send(tk, "hello")
    assert rig.server.calls_to("reopenForumTopic")


async def test_ensure_topic_binds_before_first_send_and_is_idempotent(rig):
    cid = message_store.get_or_create_conversation(None)
    a = await rig.adapter.ensure_topic(cid, "S")
    b = await rig.adapter.ensure_topic(cid, "S")
    assert a == b and len(rig.server.calls_to("createForumTopic")) == 1
    assert message_store.resolve_conversation_binding("telegram", rig.adapter.external_key(a)) == cid


async def test_dm_mode_new_target_is_a_fresh_slot(dm_rig):
    cid = message_store.get_or_create_conversation(None)
    _, tk = await dm_rig.adapter.ensure_new_conversation_target(cid, "D")
    assert tk["chat_id"] == USER and tk["slot"] == 1


def test_permalink_shape(rig):
    import asyncio
    link = asyncio.run(rig.adapter.permalink({"chat_id": FORUM, "topic_id": 5, "slot": None}))
    assert link == "https://t.me/c/1234567890/5"
