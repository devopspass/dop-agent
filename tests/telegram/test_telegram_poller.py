"""Poller + inbound routing against the strict fake (offline, no sleeps)."""
import message_store
import telegram_store
from telegram_api import TelegramError, TelegramNetworkError
from fake_telegram import png
from telegram_rig import FORUM, USER, Rig

OTHER = 777


async def test_start_clears_webhook_and_self_checks(rig):
    rig.server.webhook_url = "https://old.example/hook"
    assert await rig.poller.start()
    assert rig.server.webhook_url is None


async def test_bad_token_is_fatal_and_poller_stops(rig):
    rig.server.fail_next("getMe", TelegramError(401, "Unauthorized"))
    assert await rig.poller.start() is False and rig.poller.fatal


async def test_offset_is_persisted_before_dispatch_and_survives_restart(rig):
    seen_offsets = []

    async def spy(msg):
        seen_offsets.append(telegram_store.get_offset())

    rig.poller.handle_incoming = spy
    upd = rig.server.push_message(USER, USER, "hello")
    await rig.pump()
    assert seen_offsets == [upd["update_id"] + 1]
    rig2 = Rig()
    rig2.server = rig.server
    assert telegram_store.get_offset() == upd["update_id"] + 1


async def test_dm_message_reaches_core_with_ref_and_key(dm_rig):
    dm_rig.server.push_message(USER, USER, "hello")
    await dm_rig.pump()
    m = dm_rig.incoming[0]
    assert (m.platform, m.user_id, m.conversation_key, m.text) == ("telegram", str(USER), str(USER), "hello")
    assert m.ref.startswith(f"{USER}:")


async def test_unauthorized_user_is_silently_ignored_with_no_api_call(rig):
    rig.server.push_message(OTHER, OTHER, "let me in", chat_type="private")
    before = len(rig.server.calls)
    await rig.pump()
    names = [c[0] for c in rig.server.calls[before:]]
    assert rig.incoming == [] and names == ["getUpdates"]


async def test_foreign_chat_is_ignored_even_from_allowed_user(rig):
    rig.server.push_message(-100999, USER, "hi", chat_type="supergroup")
    await rig.pump()
    assert rig.incoming == []


async def test_stale_backlog_message_is_dropped(dm_rig):
    dm_rig.server.push_message(USER, USER, "old", date=dm_rig.server.clock() - 3600)
    await dm_rig.pump()
    assert dm_rig.incoming == []


async def test_edited_messages_are_ignored(dm_rig):
    dm_rig.server.push_edit(USER, USER, 1, "edited")
    await dm_rig.pump()
    assert dm_rig.incoming == []


async def test_commands_are_not_forwarded_to_the_model(dm_rig):
    dm_rig.server.push_message(USER, USER, "/help")
    dm_rig.server.push_message(USER, USER, "/new")
    await dm_rig.pump()
    assert dm_rig.incoming == [] and len(dm_rig.server.sent(USER)) == 2


async def test_dm_new_starts_a_new_slot_used_by_next_message(dm_rig):
    dm_rig.server.push_message(USER, USER, "/new")
    dm_rig.server.push_message(USER, USER, "first in slot 1")
    await dm_rig.pump()
    assert dm_rig.incoming[0].conversation_key == f"{USER}:1"


async def test_dm_reply_routes_to_the_session_that_sent_the_message(dm_rig):
    tk = {"chat_id": USER, "topic_id": None, "slot": 3}
    res = await dm_rig.adapter.send(tk, "answer from slot 3")
    dm_rig.server.push_message(USER, USER, "follow-up", reply_to=res["message_id"])
    await dm_rig.pump()
    assert dm_rig.incoming[0].conversation_key == f"{USER}:3"


async def test_message_in_topic_routes_to_that_topic_session(rig):
    tk = await rig.adapter.create_topic("t")
    rig.server.push_message(FORUM, USER, "in topic", thread_id=tk["topic_id"])
    await rig.pump()
    assert rig.incoming[0].conversation_key == f"{FORUM}:{tk['topic_id']}"


async def test_general_message_creates_topic_once_even_if_redelivered(rig):
    upd = rig.server.push_message(FORUM, USER, "please do X")
    await rig.pump()
    rig.server.updates.append(dict(upd, update_id=upd["update_id"] + 50))
    await rig.pump()
    assert len(rig.server.calls_to("createForumTopic")) == 1
    assert rig.incoming[0].conversation_key.startswith(f"{FORUM}:")


async def test_conflict_409_backs_off_then_recovers(rig):
    rig.server.conflicts = 1
    rig.server.push_message(FORUM, USER, "x", thread_id=(await rig.adapter.create_topic("t"))["topic_id"])
    await rig.pump()
    assert rig.sleeps and rig.incoming == []
    await rig.pump()
    assert len(rig.incoming) == 1


async def test_network_error_backs_off_and_continues(dm_rig):
    dm_rig.server.fail_next("getUpdates", TelegramNetworkError("down"))
    await dm_rig.poller.poll_once()
    assert dm_rig.sleeps


async def test_photo_is_downloaded_and_passed_as_text_with_path(dm_rig):
    dm_rig.server.add_file("f1", "photos/a.png", png(20, 20))
    dm_rig.server.push_message(USER, USER, None, photo=[{"file_id": "f1", "file_unique_id": "u", "width": 20, "height": 20}],
                               caption="look")
    await dm_rig.pump()
    assert "look" in dm_rig.incoming[0].text and "a" in dm_rig.incoming[0].text


async def test_voice_is_transcribed(dm_rig):
    dm_rig.server.add_file("v1", "voice/v.ogg", b"OggS")
    dm_rig.server.push_message(USER, USER, None, voice={"file_id": "v1", "file_unique_id": "u", "duration": 3})
    await dm_rig.pump()
    assert "spoken words" in dm_rig.incoming[0].text


async def test_oversized_file_gets_a_clear_warning_not_a_crash(dm_rig):
    dm_rig.server.push_message(USER, USER, "see file", document={"file_id": "big", "file_unique_id": "u",
                                                                  "file_name": "x.bin", "file_size": 30 * 1024 * 1024})
    await dm_rig.pump()
    assert any("20 MB" in m["text"] for m in dm_rig.server.sent(USER))


# -- callbacks ---------------------------------------------------------------------
async def test_stop_button_interrupts_and_always_answers(dm_rig):
    cid = message_store.get_or_create_bound_conversation("telegram", str(USER))
    dm_rig.server.push_callback(USER, USER, f"st:{cid}", 5)
    await dm_rig.pump()
    assert dm_rig.interrupted == [cid] and len(dm_rig.server.answered) == 1


async def test_stop_button_for_conversation_bound_to_another_chat_is_ignored(dm_rig):
    """Security: a st:<conversation_id> tap must only stop a conversation bound to the tapped chat."""
    cid = message_store.get_or_create_bound_conversation("telegram", "777001")
    dm_rig.server.push_callback(USER, USER, f"st:{cid}", 5)
    await dm_rig.pump()
    assert dm_rig.interrupted == [] and len(dm_rig.server.answered) == 1


async def test_unauthorized_tap_is_answered_but_does_nothing(dm_rig):
    dm_rig.server.push_callback(OTHER, OTHER, "st:conv-9", 5)
    await dm_rig.pump()
    assert dm_rig.interrupted == [] and len(dm_rig.server.answered) == 1


async def test_choice_button_sends_text_once_and_strips_keyboard(dm_rig):
    res = await dm_rig.adapter.send({"chat_id": USER, "topic_id": None, "slot": None},
                                    "go?\n```actions\n- * Proceed | proceed now\n```")
    mid = res["message_id"]
    data = dm_rig.server.keyboard(USER, mid)[0][0]["callback_data"]
    dm_rig.server.push_callback(USER, USER, data, mid)
    dm_rig.server.push_callback(USER, USER, data, mid)
    await dm_rig.pump()
    assert [m.text for m in dm_rig.incoming] == ["proceed now"]
    assert dm_rig.server.keyboard(USER, mid) is None
    assert len(dm_rig.server.answered) == 2


async def test_expired_choice_token_is_answered(dm_rig):
    dm_rig.server.push_callback(USER, USER, "ab:deadbeef", 5)
    await dm_rig.pump()
    assert dm_rig.incoming == [] and dm_rig.server.answered
