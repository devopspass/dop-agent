"""
Unit tests for message_kinds.py - the read-time classifier behind the Web UI's
Today / Smart / Messages-only views.

Feature: watcher registrations, fires and the agent's replies to them would
drown real conversation in the Web UI transcript. The UI folds routine
watcher events, which only works if every stored turn is reliably classified
(registered / fired / reply, ACK vs report, errors). These tests pin that
classification so a reworded notice or a changed reply shape can't silently
turn real reports into "folded noise" (or vice versa).
"""

from __future__ import annotations

import message_kinds as mk


def _t(i, role, text):
    return {"id": i, "role": role, "text": text, "timestamp": "2026-10-09T10:00:00"}


PIPE_OK = ":white_check_mark: *Ticket watch fired* — PRJ-5 done"
PIPE_FAIL = ":x: *Teams watch fired* — build chat\nstatus: `failed`"
NOTE = ":ticket: *Ticket watch fired* — PRJ-5\n\nNew comment(s):\n• alice: looks good"
REG = ":new: *New watcher registered* — PRJ-5"


def test_fire_kinds_types_and_final_flags():
    turns = mk.classify_turns([_t(1, "system", PIPE_OK), _t(2, "system", PIPE_FAIL), _t(3, "system", NOTE), _t(4, "system", REG)])
    assert [t["kind"] for t in turns] == ["watch_fired", "watch_fired", "watch_fired", "watch_registered"]
    assert turns[0]["fire_type"] == "ticket" and turns[0]["fire_final"] is False
    assert turns[1]["fire_type"] == "teams" and turns[1]["fire_ok"] is False
    assert turns[2]["fire_type"] == "ticket" and turns[2]["fire_ok"] is None


def test_replies_after_fire_are_linked_and_first_is_classified_ack():
    turns = mk.classify_turns([_t(1, "system", NOTE), _t(2, "assistant", "ACK - bot note"), _t(3, "assistant", "unrelated later message")])
    assert turns[1]["kind"] == "watch_reply" and turns[1]["reply_to"] == 1 and turns[1]["reply_class"] == "ack"
    assert turns[1]["summary"] == "bot note"
    # a fire's follow-up assistant messages (e.g. a re-prompted report) stay attached to it
    assert turns[2]["kind"] == "watch_reply" and turns[2]["reply_to"] == 1


def test_legacy_nothing_to_act_on_reply_counts_as_ack():
    turns = mk.classify_turns([_t(1, "system", NOTE), _t(2, "assistant", "That note only repeats my own push, nothing to act on.")])
    assert turns[1]["reply_class"] == "ack"


def test_report_with_table_and_link_is_report_not_ack():
    body = "Status update.\n\n| Item | State |\n|---|---|\n| [5](https://x/5) | ready |\n\nNext: review by the owner, then close it out. Please also double check the linked item before the end of the week."
    turns = mk.classify_turns([_t(1, "system", PIPE_OK), _t(2, "assistant", body)])
    assert turns[1]["reply_class"] == "report"


def test_reply_to_failure_is_always_report_even_if_short():
    turns = mk.classify_turns([_t(1, "system", PIPE_FAIL), _t(2, "assistant", "Looking at it.")])
    assert turns[1]["reply_class"] == "report"


def test_user_message_resets_fire_association_and_errors_are_flagged():
    turns = mk.classify_turns([
        _t(1, "system", NOTE), _t(2, "user", "stop"), _t(3, "assistant", "ok"),
        _t(4, "assistant", ":warning: DOP Agent request timed out (a tool call likely got stuck)."),
    ])
    assert turns[2]["kind"] == "agent"
    assert turns[3]["kind"] == "error"


def test_final_fire_and_thin_reply_helpers():
    assert not mk.is_final_fire_text(PIPE_OK) and not mk.is_final_fire_text(NOTE)
    assert mk.is_thin_reply("ok, pipeline is green now")
    assert mk.is_thin_reply("ACK")
    assert not mk.is_thin_reply("Done: see https://gl/mr/5 for the MR, pipeline is green and review is needed from Bob.")
