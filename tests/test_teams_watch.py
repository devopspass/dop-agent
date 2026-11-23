"""Teams watcher script (scripts/watch/teams_watch.py) behaviour.

Feature: token-free Teams polling for `script` watches. Guarantees tested here:
the first run records a baseline silently, a new message/meeting is reported once
(never duplicated on the next poll), sender/regex/mention filters work, unchanged
chats are not re-read, transcript readiness is reported once, calendar modes
(started/soon/created/changed) fire on the right transitions, a missing token/CDP tab
is an error exit (1) while a 429 is a silent skip, and bodies are truncated.
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "drivers" / "ms_teams" / "watch" / "teams_watch.py"
spec = importlib.util.spec_from_file_location("teams_watch", SRC)
tw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tw)

CHAT = "19:abc@thread.v2"


def msg(i, text, who="Alice", mtype="RichText/Html", **extra):
    return {"id": str(i), "imdisplayname": who, "from": f"8:orgid:{who}", "messagetype": mtype,
            "composetime": "2026-11-20T10:00:00Z", "content": text, **extra}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    box = {"msgs": [], "convs": []}
    monkeypatch.setattr(tw, "fetch_messages", lambda cid, limit=20: list(box["msgs"]))
    monkeypatch.setattr(tw, "fetch_conversations", lambda: list(box["convs"]))
    monkeypatch.setattr(tw, "my_oid", lambda: "ME")
    monkeypatch.setattr(tw, "now_utc", lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc))
    box["now_ms"] = int(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return box


def run(capsys, *argv):
    rc = tw.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_new_chat_message_reported_once_after_silent_baseline(env, capsys):
    base = env["now_ms"]
    env["msgs"] = [msg(base - 10, "old")]
    assert run(capsys, "messages", "--chat", CHAT)[0] == 0
    env["msgs"].append(msg(base + 5, "<p>hello <b>team</b></p>"))
    rc, out, _ = run(capsys, "messages", "--chat", CHAT)
    assert rc == 200 and "hello team" in out and "<b>" not in out
    assert run(capsys, "messages", "--chat", CHAT)[0] == 0


def test_sender_and_regex_filters_still_advance_cursor(env, capsys):
    base = env["now_ms"]
    run(capsys, "messages", "--chat", CHAT, "--from", "bob", "--match", r"deploy\s+\d+")
    env["msgs"] = [msg(base + 1, "deploy 42", "Alice"), msg(base + 2, "nothing", "Bob"),
                   msg(base + 3, "Deploy 7", "Bob")]
    rc, out, _ = run(capsys, "messages", "--chat", CHAT, "--from", "bob", "--match", r"deploy\s+\d+")
    assert rc == 200 and "Deploy 7" in out and "deploy 42" not in out and "nothing" not in out
    assert run(capsys, "messages", "--chat", CHAT, "--from", "bob", "--match", r"deploy\s+\d+")[0] == 0


def test_mention_of_me_and_system_messages_ignored(env, capsys):
    base = env["now_ms"]
    run(capsys, "messages", "--chat", CHAT, "--mentions-me")
    env["msgs"] = [msg(base + 1, "hi all"),
                   msg(base + 2, "ping", properties={"mentions": '[{"mentioned":{"id":"8:orgid:ME"}}]'}),
                   msg(base + 3, "ME joined", mtype="ThreadActivity/AddMember")]
    rc, out, _ = run(capsys, "messages", "--chat", CHAT, "--mentions-me")
    assert rc == 200 and "ping" in out and "hi all" not in out and "joined" not in out


def test_any_chat_only_reads_chats_whose_last_message_changed(env, capsys, monkeypatch):
    base = env["now_ms"]
    env["convs"] = [{"id": "c1", "threadProperties": {"topic": "Ops"}, "lastMessage": {"id": "1"}},
                    {"id": "c2", "lastMessage": {"id": "2"}}]
    run(capsys, "messages")
    reads = []
    monkeypatch.setattr(tw, "fetch_messages", lambda cid, limit=20: reads.append(cid) or [msg(base + 9, "new in ops")])
    env["convs"][0]["lastMessage"]["id"] = str(base + 9)
    rc, out, _ = run(capsys, "messages")
    assert rc == 200 and reads == ["c1"] and "[Ops]" in out


def test_long_bodies_are_truncated(env, capsys):
    base = env["now_ms"]
    run(capsys, "messages", "--chat", CHAT)
    env["msgs"] = [msg(base + 1, "x" * 1000)]
    _, out, _ = run(capsys, "messages", "--chat", CHAT, "--max-chars", "50")
    assert "x" * 60 not in out and "…" in out


def test_transcript_ready_reported_once(env, capsys, monkeypatch):
    found = []
    fake_chatsvc = type("C", (), {"find_call_transcripts": staticmethod(lambda m: list(found))})
    monkeypatch.setattr(tw, "_teams_modules", lambda: (None, fake_chatsvc, None))
    assert run(capsys, "transcript", "--chat", CHAT)[0] == 0
    found.append({"title": "Standup", "time": "2026-11-20T11:00:00Z", "url": "https://ams/t1"})
    rc, out, _ = run(capsys, "transcript", "--chat", CHAT)
    assert rc == 200 and "Standup" in out
    assert run(capsys, "transcript", "--chat", CHAT)[0] == 0


def ev(i, start, minutes=30, **kw):
    s = datetime.fromisoformat(start)
    return {"id": i, "subject": f"M{i}", "isOnlineMeeting": True, "isCancelled": False,
            "start": {"dateTime": s.strftime("%Y-%m-%dT%H:%M:%S.0000000")},
            "end": {"dateTime": (s + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.0000000")}, **kw}


def test_meeting_started_fires_once_when_start_passes(env, capsys, monkeypatch):
    events = [ev("e1", "2026-11-20T12:30:00")]
    monkeypatch.setattr(tw, "fetch_events", lambda b, a: events)
    assert run(capsys, "meeting-started")[0] == 0
    events[0] = ev("e1", "2026-11-20T11:50:00")
    rc, out, _ = run(capsys, "meeting-started")
    assert rc == 200 and "STARTED Me1" in out
    assert run(capsys, "meeting-started")[0] == 0


def test_meeting_created_and_changed_detection(env, capsys, monkeypatch):
    events = [ev("e1", "2026-11-25T09:00:00")]
    monkeypatch.setattr(tw, "fetch_events", lambda b, a: list(events))
    assert run(capsys, "meeting-created")[0] == 0
    assert run(capsys, "meeting-changed")[0] == 0
    events.append(ev("e2", "2026-11-26T09:00:00"))
    rc, out, _ = run(capsys, "meeting-created")
    assert rc == 200 and "NEW Me2" in out and "Me1" not in out
    assert run(capsys, "meeting-changed")[0] == 0
    events[0] = ev("e1", "2026-11-25T10:00:00")
    events[1] = dict(events[1], isCancelled=True)
    rc, out, _ = run(capsys, "meeting-changed")
    assert rc == 200 and "RESCHEDULED Me1" in out and "CANCELLED Me2" in out


def test_meeting_soon_within_window(env, capsys, monkeypatch):
    events = [ev("e1", "2026-11-20T12:03:00"), ev("e2", "2026-11-20T13:00:00")]
    monkeypatch.setattr(tw, "fetch_events", lambda b, a: events)
    run(capsys, "meeting-soon", "--minutes", "5")  # baseline
    events.append(ev("e3", "2026-11-20T12:04:00"))
    rc, out, _ = run(capsys, "meeting-soon", "--minutes", "5")
    assert rc == 200 and "Me3" in out and "Me2" not in out


def test_missing_cdp_tab_is_error_but_rate_limit_is_silent(env, capsys, monkeypatch):
    def boom(cid, limit=20):
        raise RuntimeError("No Teams tab found on CDP port 9222")
    monkeypatch.setattr(tw, "fetch_messages", boom)
    run(capsys, "messages", "--chat", CHAT)
    rc, _, err = run(capsys, "messages", "--chat", CHAT)
    assert rc == 1 and "Teams tab" in err

    def limited(cid, limit=20):
        raise tw.Transient("429")
    monkeypatch.setattr(tw, "fetch_messages", limited)
    assert run(capsys, "messages", "--chat", CHAT)[0] == 0


def test_baseline_state_file_holds_no_message_bodies(env, capsys, tmp_path):
    env["msgs"] = [msg(env["now_ms"] + 1, "secret body")]
    run(capsys, "messages", "--chat", CHAT)
    run(capsys, "messages", "--chat", CHAT)
    assert "secret body" not in (tmp_path / "teams-messages.json").read_text()
    json.loads((tmp_path / "teams-messages.json").read_text())
