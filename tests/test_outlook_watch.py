"""Outlook watcher script (drivers/ms_outlook/watch/outlook_watch.py) behaviour.

Feature: token-free Outlook polling for `script` watches. Guarantees tested here: the first run
records a baseline silently, a new mail is reported exactly once, sender/domain/subject/regex/
attachment/importance/flag filters work, replies in a thread ignore my own messages, meeting
invite/update/cancel/response mails are classified, auto-reply and bounce detection, calendar
modes (soon/changed/invite responses), unread-threshold crossing fires once and re-arms, digest,
flagged-due, wake text is truncated (privacy), auth failures exit 1 while 429 is a silent skip.
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "drivers" / "ms_outlook" / "watch" / "outlook_watch.py"
spec = importlib.util.spec_from_file_location("outlook_watch", SRC)
ow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ow)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def mail(i, subject="Hello", who="alice@x.com", name="Alice", received="2026-11-20T10:00:00Z", **extra):
    return {"id": str(i), "subject": subject, "receivedDateTime": received, "bodyPreview": "preview " + subject,
            "from": {"emailAddress": {"name": name, "address": who}}, "importance": "normal",
            "hasAttachments": False, "flag": {"flagStatus": "notFlagged"}, **extra}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    b = {"mail": [], "events": [], "unread": 0, "calls": [], "raise": None}

    def fake(url, account=None):
        b["calls"].append((url, account))
        if b["raise"]:
            raise b["raise"]
        if url.rstrip("/").endswith("/me"):
            return {"mail": "me@x.com", "userPrincipalName": "me@x.com"}
        if "calendarView" in url:
            return {"value": b["events"]}
        if url.split("?")[0].endswith("mailFolders/inbox"):
            return {"unreadItemCount": b["unread"]}
        if "$select=conversationId" in url:
            return {"conversationId": "CONV1"}
        return {"value": list(b["mail"])}

    monkeypatch.setattr(ow, "graph_get", fake)
    monkeypatch.setattr(ow, "now_utc", lambda: NOW)
    return b


def run(*argv):
    return ow.main(list(argv))


def test_baseline_is_silent_and_new_mail_reported_once(box, capsys):
    box["mail"] = [mail(1)]
    assert run("mail") == 0
    assert run("mail") == 0
    box["mail"] = [mail(2, "Invoice", received="2026-11-20T11:00:00Z"), mail(1)]
    assert run("mail") == 200
    out = capsys.readouterr().out
    assert "Invoice" in out and "Hello" not in out
    assert run("mail") == 0


def test_sender_domain_subject_attachment_importance_flag_filters(box, capsys):
    run("mail")
    box["mail"] = [mail(2, "A", who="bob@corp.com", name="Bob"), mail(3, "Report Q3", who="c@other.com", hasAttachments=True),
                   mail(4, "Urgent", importance="high"), mail(5, "Todo", flag={"flagStatus": "flagged"})]
    assert run("mail", "--from", "@corp.com") == 200
    assert "Bob" in capsys.readouterr().out
    for args, key in ((["--attachment"], "Report Q3"), (["--importance", "high"], "Urgent"),
                      (["--flagged"], "Todo"), (["--subject", "^report"], "Report Q3"), (["--match", "preview urg"], "Urgent")):
        (Path(ow.state_path("mail"))).unlink()
        box["mail"] = []
        run("mail")
        box["mail"] = [mail(2, "A", who="bob@corp.com", name="Bob"), mail(3, "Report Q3", who="c@other.com", hasAttachments=True),
                       mail(4, "Urgent", importance="high"), mail(5, "Todo", flag={"flagStatus": "flagged"})]
        assert run("mail", *args) == 200
        out = capsys.readouterr().out
        assert key in out and out.count("\n- ") == 1


def test_multiple_from_flags_act_as_user_supplied_sender_list(box, capsys):
    run("mail")
    box["mail"] = [mail(2, "no", who="rand@y.com"), mail(3, "yes1", who="a@customer.com"), mail(4, "yes2", who="boss@x.com")]
    assert run("mail", "--from", "@customer.com", "--from", "boss@x.com") == 200
    out = capsys.readouterr().out
    assert "yes1" in out and "yes2" in out and "no |" not in out


def test_thread_reply_ignores_my_own_messages(box, capsys):
    box["mail"] = [mail(1, "Q?", who="me@x.com")]
    run("thread", "--reply-to", "SENT1")
    box["mail"] += [mail(2, "RE: Q?", who="me@x.com"), mail(3, "RE: Q?", who="bob@x.com", name="Bob")]
    assert run("thread", "--reply-to", "SENT1") == 200
    out = capsys.readouterr().out
    assert "Bob" in out and out.count("\n- ") == 1
    assert any("conversationId eq 'CONV1'" in u.replace("%27", "'").replace("%20", " ") for u, _ in box["calls"])


def test_meeting_mail_classification(box, capsys):
    run("invite")
    req = {"@odata.type": "#microsoft.graph.eventMessageRequest", "meetingMessageType": "meetingRequest"}
    box["mail"] = [mail(2, "Sync", **req), mail(3, "Updated: Sync", **req, previousStartDateTime={"dateTime": "x"}),
                   mail(4, "Canceled: Sync", **{"@odata.type": "#microsoft.graph.eventMessage", "meetingMessageType": "meetingCancelled"}),
                   mail(5, "Declined: Sync", **{"@odata.type": "#microsoft.graph.eventMessageResponse", "meetingMessageType": "meetingDeclined"})]
    assert run("invite") == 200
    assert "Sync" in capsys.readouterr().out
    for mode, subj in (("meeting-updated", "Updated: Sync"), ("meeting-cancelled", "Canceled: Sync"), ("invite-response", "Declined: Sync")):
        box["mail"] = []
        run(mode)
        box["mail"] = [mail(12, "Sync", **req), mail(13, "Updated: Sync", **req, previousStartDateTime={"dateTime": "x"}),
                       mail(14, "Canceled: Sync", **{"@odata.type": "#microsoft.graph.eventMessage", "meetingMessageType": "meetingCancelled"}),
                       mail(15, "Declined: Sync", **{"@odata.type": "#microsoft.graph.eventMessageResponse", "meetingMessageType": "meetingDeclined"})]
        assert run(mode) == 200
        out = capsys.readouterr().out
        assert subj in out and out.count("\n- ") == 1
    (Path(ow.state_path("invite-response"))).unlink()
    box["mail"] = []
    run("invite-response", "--response", "accepted")
    box["mail"] = [mail(25, "Declined: Sync", **{"@odata.type": "#microsoft.graph.eventMessageResponse", "meetingMessageType": "meetingDeclined"})]
    assert run("invite-response", "--response", "accepted") == 0


def test_autoreply_and_bounce_detection(box, capsys):
    run("autoreply"), run("bounce")
    box["mail"] = [mail(2, "Automatic reply: Trip", who="x@y.com"), mail(3, "Undeliverable: Hi", who="postmaster@y.com"),
                   mail(4, "Normal")]
    assert run("autoreply") == 200 and "Automatic reply" in capsys.readouterr().out
    assert run("bounce") == 200 and "Undeliverable" in capsys.readouterr().out


def test_wake_text_is_truncated_and_snippet_can_be_dropped(box, capsys):
    run("mail")
    box["mail"] = [mail(2, "S" * 300, bodyPreview="B" * 500)]
    run("mail", "--max-chars", "50")
    out = capsys.readouterr().out
    assert "S" * 81 not in out and "B" * 51 not in out
    (Path(ow.state_path("mail"))).unlink()
    box["mail"] = []
    run("mail")
    box["mail"] = [mail(3, "T", bodyPreview="SECRETBODY")]
    run("mail", "--no-snippet")
    assert "SECRETBODY" not in capsys.readouterr().out


def test_output_capped_with_more_counter(box, capsys):
    run("mail")
    box["mail"] = [mail(i, f"m{i}", received=f"2026-11-20T11:{i:02d}:00Z") for i in range(2, 9)]
    run("mail", "--limit", "3")
    assert "+4 more" in capsys.readouterr().out


def test_unread_threshold_fires_once_and_rearms(box):
    box["unread"] = 5
    assert run("unread", "--threshold", "10") == 0
    box["unread"] = 12
    assert run("unread", "--threshold", "10") == 200
    box["unread"] = 15
    assert run("unread", "--threshold", "10") == 0
    box["unread"] = 3
    assert run("unread", "--threshold", "10") == 0
    box["unread"] = 11
    assert run("unread", "--threshold", "10") == 200


def test_digest_lists_unread_and_is_silent_when_empty(box, capsys):
    assert run("digest") == 0
    box["unread"], box["mail"] = 2, [mail(1, "Pending")]
    assert run("digest") == 200
    assert "Pending" in capsys.readouterr().out


def test_event_soon_fires_once_per_event(box, capsys):
    box["events"] = [{"id": "E1", "subject": "Standup", "start": {"dateTime": "2026-11-20T12:05:00.0000000"}}]
    assert run("event-soon", "--minutes", "10") == 200
    assert "Standup" in capsys.readouterr().out
    assert run("event-soon", "--minutes", "10") == 0


def test_event_changed_detects_move_and_cancel(box, capsys):
    ev = lambda s, c=False: {"id": "E1", "subject": "Plan", "start": {"dateTime": s}, "end": {"dateTime": s}, "isCancelled": c}
    box["events"] = [ev("2026-11-21T10:00:00")]
    assert run("event-changed") == 0
    assert run("event-changed") == 0
    box["events"] = [ev("2026-11-21T11:00:00")]
    assert run("event-changed") == 200 and "moved" in capsys.readouterr().out
    box["events"] = [ev("2026-11-21T11:00:00", True)]
    assert run("event-changed") == 200 and "CANCELLED" in capsys.readouterr().out


def test_my_invite_response_change_reported(box, capsys):
    def e(resp):
        return {"id": "E1", "subject": "Kickoff", "isOrganizer": True, "start": {"dateTime": "2026-11-21T10:00:00"},
                "attendees": [{"emailAddress": {"name": "Bob", "address": "b@x.com"}, "status": {"response": resp}}]}
    box["events"] = [e("none")]
    run("my-invite-responses")
    box["events"] = [e("declined")]
    assert run("my-invite-responses", "--response", "declined") == 200
    assert "Bob" in capsys.readouterr().out
    assert run("my-invite-responses") == 0


def test_task_due_fires_once_for_flagged_mail_due_soon(box, capsys):
    run("task-due")
    due = mail(2, "Pay", flag={"flagStatus": "flagged", "dueDateTime": {"dateTime": "2026-11-20T18:00:00.0000000"}})
    far = mail(3, "Later", flag={"flagStatus": "flagged", "dueDateTime": {"dateTime": "2026-12-20T18:00:00.0000000"}})
    box["mail"] = [due, far]
    assert run("task-due", "--hours", "24") == 200
    out = capsys.readouterr().out
    assert "Pay" in out and "Later" not in out
    assert run("task-due", "--hours", "24") == 0


def test_auth_failure_is_error_and_throttle_is_silent_skip(box, capsys):
    box["raise"] = ow.AuthError("login expired")
    assert run("mail") == 1
    assert "login expired" in capsys.readouterr().err
    box["raise"] = ow.Transient("429")
    assert run("mail") == 0
    assert ow.state_path("mail").exists() is False


def test_account_is_forwarded_to_backend(box):
    run("mail", "--account", "work")
    assert all(a == "work" for _, a in box["calls"])


def test_outlook_rest_pascal_case_meeting_values_are_classified():
    """Tab backend (Outlook REST) returns 'MeetingRequest'/'MeetingCancelled' and '#Microsoft.OutlookServices.EventMessageRequest';
    the classifier must treat them like the Graph camelCase values, or invites/cancels are silently missed."""
    req = {"@odata.type": "#Microsoft.OutlookServices.EventMessageRequest", "meetingMessageType": "MeetingRequest"}
    assert ow.meeting_kind(req) == "invite"
    assert ow.meeting_kind({"@odata.type": "#Microsoft.OutlookServices.EventMessage", "meetingMessageType": "MeetingCancelled"}) == "cancelled"
    assert ow.meeting_kind({"meetingMessageType": "MeetingDeclined"}) == "declined"


def test_attachments_mode_lists_only_mail_with_attachments(box, capsys, monkeypatch):
    """`attachments` mode reports only new mail that has attachments, never downloading anything."""
    run("attachments")
    box["mail"] = [mail(2, "Plain"), mail(3, "Report", hasAttachments=True)]
    monkeypatch.setattr(ow, "graph_get", lambda url, account=None: (
        {"value": [{"name": "q3.xlsx", "size": 2048}]} if "/attachments" in url else {"value": list(box["mail"])}))
    assert run("attachments") == 200
    out = capsys.readouterr().out
    assert "Report" in out and "q3.xlsx" in out and "Plain" not in out


def test_unread_filter_skips_read_mail(box, capsys):
    """`--unread` ignores messages already marked read."""
    run("mail", "--unread")
    box["mail"] = [mail(2, "Seen", isRead=True), mail(3, "Fresh", isRead=False)]
    assert run("mail", "--unread") == 200
    out = capsys.readouterr().out
    assert "Fresh" in out and "Seen" not in out
