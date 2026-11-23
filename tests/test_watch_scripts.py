"""Script-only time watches (Phase-2 token savings).

Feature: a `time` watch with meta.script runs an allow-listed read-only
script and wakes the model only when meta.wake_if says so (or on failure),
instead of burning a full ~150k-token turn to run one command and say
"nothing yet". Guarantees: allow-list/traversal protection, timeout kills
the process group, 'changed' state, failure wake with stderr tail and
failure dedup (outage must not wake the model every tick), label-prompt
fallback when no script meta, migration of the script_state column.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import time

import pytest

import watch_scripts as ws
import watchers


def run(c):
    return asyncio.run(c)


@pytest.fixture
def sdir(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    return d


def mk(d, name, body, mode=0o755):
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(mode)
    return p


def test_only_allowlisted_basenames_resolve(sdir, tmp_path):
    mk(sdir, "ok.sh", "echo hi")
    assert ws.resolve_script("ok.sh", sdir).name == "ok.sh"
    outside = mk(tmp_path, "evil.sh", "echo x")
    for bad in ("../evil.sh", "/bin/ls", "a/b.sh", "", None, "_lib.py", "missing.sh", "ok.sh; rm -rf /"):
        with pytest.raises(ValueError):
            ws.resolve_script(bad, sdir)
    (sdir / "link.sh").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        ws.resolve_script("link.sh", sdir)
    mk(sdir, "ww.sh", "echo x", 0o777)
    with pytest.raises(ValueError, match="writable"):
        ws.resolve_script("ww.sh", sdir)
    mk(sdir, "nx.sh", "echo x", 0o644)
    with pytest.raises(ValueError, match="executable"):
        ws.resolve_script("nx.sh", sdir)


def test_meta_validation_hints(sdir):
    mk(sdir, "ok.sh", "echo")
    ws.validate_meta({"script": "ok.sh", "args": ["a"], "wake_if": "changed"}, sdir)
    for bad in ({"script": "ok.sh", "args": "x"}, {"script": "ok.sh", "wake_if": "sometimes"},
                {"script": "ok.sh", "args": [1]}, {"script": "ok.sh", "args": ["a"] * 11}):
        with pytest.raises(ValueError):
            ws.validate_meta(bad, sdir)


def test_runner_captures_output_uses_minimal_env_and_passes_args(sdir, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN_X", "leak")
    mk(sdir, "e.sh", 'echo "args=$1 $2"; echo "secret=${SECRET_TOKEN_X:-none}"; echo oops >&2; exit 0')
    r = run(ws.run_script({"script": "e.sh", "args": ["a b", "c"]}, base=sdir))
    assert r["rc"] == 0 and "args=a b c" in r["stdout"] and "secret=none" in r["stdout"] and "oops" in r["stderr"]


def test_timeout_kills_the_whole_process_group(sdir):
    marker = sdir / "child_alive"
    mk(sdir, "slow.sh", f"(sleep 3; touch {marker}) &\nsleep 30")
    t = time.time()
    r = run(ws.run_script({"script": "slow.sh"}, timeout=0.5, base=sdir))
    assert r["timed_out"] and time.time() - t < 5
    time.sleep(3.5)
    assert not marker.exists(), "background child survived the timeout kill"


def test_wake_modes_for_successful_runs():
    ok = lambda out: {"rc": 0, "stdout": out, "stderr": "", "timed_out": False}
    st = {"h": "x"}
    assert ws.decide({"script": "s"}, ok("hello"), st)[:2] == ("notice", "hello")
    assert ws.decide({"script": "s"}, ok(""), st)[0] == "silent"
    assert ws.decide({"script": "s", "wake_if": "nonempty"}, ok("x"), st)[0] == "wake"
    assert ws.decide({"script": "s", "wake_if": "nonempty"}, ok(""), st)[0] == "silent"
    assert ws.decide({"script": "s", "wake_if": "exit!=0"}, ok("x"), st)[0] == "notice"


def test_changed_mode_wakes_only_when_output_changes():
    m = {"script": "s", "wake_if": "changed"}
    ok = lambda out: {"rc": 0, "stdout": out, "stderr": "", "timed_out": False}
    a1, _, s1 = ws.decide(m, ok("A"), {"h": "x"})
    a2, _, s2 = ws.decide(m, ok("A"), s1)
    a3, _, s3 = ws.decide(m, ok("B"), s2)
    assert (a1, a2, a3) == ("wake", "silent", "wake")


def test_failure_wakes_with_redacted_stderr_tail_then_dedups_until_cooldown():
    m = {"script": "s"}
    bad = {"rc": 3, "stdout": "", "stderr": "VPN down\ntoken=glpat-abcdefghij1234567890", "timed_out": False}
    a, text, st = ws.decide(m, bad, {"h": "x"}, now=1000)
    assert a == "wake" and "exit 3" in text and "VPN down" in text and "glpat-" not in text
    assert ws.decide(m, bad, st, now=1000 + 60)[0] == "silent"
    assert ws.decide(m, bad, st, now=1000 + ws.FAIL_COOLDOWN_SECS + 1)[0] == "wake"
    other = dict(bad, stderr="different problem")
    assert ws.decide(m, other, st, now=1100)[0] == "wake"
    assert ws.decide(m, {"rc": -9, "stdout": "", "stderr": "timed out", "timed_out": True}, {"h": "x"})[1].count("timed out")


def test_state_resets_when_script_or_args_change():
    a = {"script": "s", "args": ["1"]}
    b = {"script": "s", "args": ["2"]}
    st = json.dumps({"h": ws.script_hash(a), "out": "zzz"})
    assert ws.parse_state(st, a)["out"] == "zzz"
    assert "out" not in ws.parse_state(st, b)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "w.db")
    watchers.init_db()
    return tmp_path


def _insert(meta, label="LABEL"):
    with watchers._conn() as c:
        cur = c.execute("INSERT INTO watches (type,key,label,channel,thread_ts,conversation_id,status,meta,created_at,updated_at)"
                        " VALUES ('time','in:1m',?,'','','conv','active',?, 'n','n')", (label, json.dumps(meta)))
        return cur.lastrowid


def _poll(monkeypatch, wid, sdir, script_body):
    mk(sdir, "t.sh", script_body)
    monkeypatch.setattr(ws, "SCRIPT_DIR", sdir)
    orig = ws.run_script
    monkeypatch.setattr(ws, "run_script", lambda meta, **k: orig(meta, base=sdir))
    woke, notices = [], []

    async def inj(cid, text): woke.append(text)
    async def note(cid, text): notices.append(text)
    monkeypatch.setattr(watchers, "_inject_watch_message", inj)
    monkeypatch.setattr(watchers, "_deliver_system_notice", note)
    run(watchers._poll_time_watch(watchers.get_watch(wid)))
    return woke, notices


def _due(extra):
    return {"next_fire_at": "2000-01-01T00:00:00", **extra}


def test_script_watch_posts_notice_without_model_turn(db, sdir, monkeypatch):
    wid = _insert(_due({"script": "t.sh"}))
    woke, notices = _poll(monkeypatch, wid, sdir, "echo slot free")
    assert woke == [] and notices and "slot free" in notices[0]
    assert json.loads(watchers.get_watch(wid)["script_state"])["out"]


def test_script_watch_failure_wakes_model_with_stderr(db, sdir, monkeypatch):
    wid = _insert(_due({"script": "t.sh"}))
    woke, notices = _poll(monkeypatch, wid, sdir, "echo 'login expired' >&2; exit 2")
    assert notices == [] and woke and "login expired" in woke[0] and "LABEL" in woke[0]


def test_time_watch_without_script_meta_keeps_label_prompt_fallback(db, monkeypatch):
    wid = _insert(_due({}), label="do the thing")
    woke = []

    async def inj(cid, text): woke.append(text)
    monkeypatch.setattr(watchers, "_inject_watch_message", inj)
    run(watchers._poll_time_watch(watchers.get_watch(wid)))
    assert woke and "do the thing" in woke[0]


def test_add_watch_rejects_script_meta_outside_allowlist_and_on_other_types(db, monkeypatch):
    monkeypatch.setattr(watchers.message_store, "get_conversation", lambda cid: {"conversation_id": cid})
    monkeypatch.setattr(watchers, "check_watch_resource_exists", lambda t, k: None)
    with pytest.raises(ValueError, match="invalid script name"):
        watchers.add_watch("time", "conv", key="in:5m", label="x", meta={"script": "../../bin/ls"})
    with pytest.raises(ValueError, match="only supported on `time`"):
        watchers.add_watch("session_turn", "conv", key="other", meta={"script": "any.sh"})


def test_init_db_adds_script_state_column_to_an_old_database(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, key TEXT, label TEXT, channel TEXT NOT NULL,"
              " thread_ts TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', last_state TEXT, meta TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    c.commit(); c.close()
    monkeypatch.setattr(watchers, "DB_PATH", p)
    watchers.init_db()
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(watches)")}
    assert "script_state" in cols


def test_no_scripts_ship_by_default_and_dir_is_allowlist_root():
    """    No built-in scripts ship: the allow-list dir holds only its README, so any
    script name is rejected with a hint."""
    assert ws.SCRIPT_DIR.name == "watch" and ws.SCRIPT_DIR.parent.name == "scripts"
    with pytest.raises(ValueError, match="not found"):
        ws.resolve_script("job_status.py")
