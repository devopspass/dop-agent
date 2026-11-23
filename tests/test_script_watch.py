"""`script` watch type: exit-code contract + full cron for time/script schedules.

Feature: a generic watch that runs an allow-listed read-only script on a cron/in:/at:
schedule and lets the EXIT CODE decide - 0 noop (silent, no model), 200 changed (wake with
output head + prompt), anything else error (wake and stop the watch after `retries`
consecutive failures). Raw output goes to a capped file whose path is in the wake text; the
script keeps its own state in $WATCH_STATE_DIR. Time watches gain full 5-field cron keys.
Plugin-dir scripts need a successful dry run before a watch may use them.
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

import mcp_tools
import watch_scripts as ws
import watchers


def run(c):
    return asyncio.run(c)


def test_full_cron_keys_parse_and_schedule_next_fire():
    p = watchers.parse_time_key("cron:*/10 * * * *")
    assert p == {"kind": "cron", "expr": "*/10 * * * *"}
    now = datetime(2026, 10, 19, 12, 3, 30)
    assert watchers.compute_next_fire(p, now) == datetime(2026, 10, 19, 12, 10)
    assert watchers.is_recurring_time_kind("cron")
    # downtime: fast-forwards to the next future slot instead of replaying missed ones
    assert watchers.advance_recurring(p, datetime(2026, 10, 19, 12, 0), datetime(2026, 10, 19, 12, 33)) == datetime(2026, 10, 19, 12, 40)
    # named daily/weekly forms take precedence over 5-field cron parsing
    assert watchers.parse_time_key("cron:daily 09:30")["kind"] == "cron_daily"
    assert watchers.parse_time_key("cron:mon 09:30")["kind"] == "cron_weekly"
    with pytest.raises(ValueError, match="invalid 5-field cron"):
        watchers.parse_time_key("cron:61 * * * *")


def R(rc, out="", err="", to=False):
    return {"rc": rc, "stdout": out, "stderr": err, "timed_out": to}


def test_exit_code_contract_noop_changed_error_and_retries(tmp_path):
    m = {"script": "s", "retries": 1}
    o, t, st = ws.decide_contract(m, R(0, "ignored"), {})
    assert (o, t, st["runs"], st["last_rc"]) == ("noop", "", 1, 0)
    o, t, st = ws.decide_contract(m, R(200, "NEW alert"), st, tmp_path / "x.log")
    assert o == "changed" and "NEW alert" in t and "x.log" in t
    o, _, st = ws.decide_contract(m, R(1, "", "boom"), st)
    assert o == "retry" and st["fails"] == 1
    o, t, st = ws.decide_contract(m, R(1, "", "boom"), st)
    assert o == "error" and "boom" in t
    o, _, st = ws.decide_contract(m, R(0), st)
    assert o == "noop" and st["fails"] == 0
    assert ws.decide_contract({"script": "s"}, R(-9, to=True), {})[0] == "error"
    assert ws.decide_contract({"script": "s"}, R(3, "x"), {})[0] == "error"  # only 200 means changed


def test_long_output_is_truncated_inline_and_full_text_kept_in_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "OUT_ROOT", tmp_path)
    big = "line\n" * 5000
    p = ws.write_output(7, R(200, big))
    assert p.read_text() == big and oct(p.parent.stat().st_mode)[-3:] == "700"
    _, text, _ = ws.decide_contract({"script": "s"}, R(200, big), {}, p)
    assert len(text) < ws.STDOUT_CAP + 300 and "(truncated)" in text and str(p) in text


def test_output_files_are_never_pruned_or_overwritten(tmp_path, monkeypatch):
    """Owner decision: every raw watch output is kept forever; same-second runs get distinct files."""
    monkeypatch.setattr(ws, "OUT_ROOT", tmp_path)
    d = tmp_path / "3"
    d.mkdir()
    for i in range(8):
        (d / f"2026010{i}-000000-rc0.log").write_text("x")
    p1 = ws.write_output(3, R(0, "new"))
    p2 = ws.write_output(3, R(0, "new2"))
    assert len(list(d.glob("*.log"))) == 10 and p1.read_text() == "new" and p1 != p2


def test_meta_validation_rejects_wake_if_and_bad_numbers(tmp_path):
    (tmp_path / "s.sh").write_text("#!/bin/sh\n")
    (tmp_path / "s.sh").chmod(0o755)
    with pytest.raises(ValueError, match="wake_if"):
        ws.validate_script_watch_meta({"script": "s.sh", "wake_if": "changed"}, tmp_path)
    with pytest.raises(ValueError, match="retries"):
        ws.validate_script_watch_meta({"script": "s.sh", "retries": -1}, tmp_path)
    ws.validate_script_watch_meta({"script": "s.sh", "retries": 2, "max_runs": 3}, tmp_path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "w.db")
    watchers.init_db()
    plug = tmp_path / "plugins"
    plug.mkdir()
    monkeypatch.setattr(ws, "SCRIPT_DIR", tmp_path / "shipped")
    monkeypatch.setattr(ws, "PLUGIN_DIR", plug)
    monkeypatch.setattr(ws, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(ws, "OUT_ROOT", tmp_path / "out")
    woke, notes = [], []

    async def inj(cid, text): woke.append(text)
    async def note(cid, text): notes.append(text)
    monkeypatch.setattr(watchers, "_inject_watch_message", inj)
    monkeypatch.setattr(watchers, "_deliver_system_notice", note)
    monkeypatch.setattr(watchers.message_store, "get_conversation", lambda cid: {"conversation_id": cid})
    monkeypatch.setattr(watchers, "_post_watch_announcement", lambda *a, **k: True)
    return plug, woke, notes


def mk(plug, body):
    p = plug / "t.sh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def add(**meta):
    return watchers.add_watch("script", "conv", key="cron:*/10 * * * *", label="triage it",
                              meta={"script": "t.sh", **meta})


def tick(wid):
    run(watchers._poll_script_watch(watchers.get_watch(wid)))
    return watchers.get_watch(wid)


def make_due(wid):
    w = watchers.get_watch(wid)
    meta = json.loads(w["meta"])
    meta["next_fire_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
    watchers.set_meta(wid, meta)


def test_script_watch_stays_silent_on_noop_wakes_on_200_and_keeps_own_state(env):
    plug, woke, notes = env
    mk(plug, 'if [ -f "$WATCH_STATE_DIR/seen" ]; then echo "changed!"; exit 200; fi; touch "$WATCH_STATE_DIR/seen"; exit 0')
    wid = add()
    w = tick(wid)  # run_immediately: baseline run
    assert woke == [] and w["status"] == "active"
    assert json.loads(w["meta"])["next_fire_at"] > datetime.now().isoformat()
    make_due(wid)
    w = tick(wid)
    assert len(woke) == 1 and "triage it" in woke[0] and "changed!" in woke[0] and "full output:" in woke[0]
    assert w["status"] == "active" and json.loads(w["script_state"])["runs"] == 2


def test_script_error_wakes_once_and_stops_the_watch_after_retries(env):
    plug, woke, _ = env
    mk(plug, "echo 'token expired' >&2; exit 1")
    wid = add(retries=1)
    assert tick(wid)["status"] == "active" and woke == []  # first failure tolerated
    make_due(wid)
    w = tick(wid)
    assert w["status"] == "done" and len(woke) == 1 and "token expired" in woke[0]
    assert json.loads(w["meta"])["finished_reason"] == "script_error"


def test_until_changed_and_max_runs_finish_the_watch(env):
    plug, woke, notes = env
    mk(plug, "exit 200")
    assert tick(add(until_changed=True))["status"] == "done" and len(woke) == 1
    mk(plug, "exit 0")
    assert tick(add(max_runs=1))["status"] == "done" and any("max_runs" in n for n in notes)


def test_failed_delivery_is_retried_without_rerunning_script_first(env, monkeypatch):
    plug, woke, _ = env
    mk(plug, 'if [ -f "$WATCH_STATE_DIR/s" ]; then exit 0; fi; touch "$WATCH_STATE_DIR/s"; echo hit; exit 200')
    calls = {"n": 0}
    good = watchers._inject_watch_message

    async def flaky(cid, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("slack down")
        await good(cid, text)
    monkeypatch.setattr(watchers, "_inject_watch_message", flaky)
    wid = add()
    w = tick(wid)
    assert "pending" in json.loads(w["script_state"]) and woke == []
    tick(wid)
    assert len(woke) == 1 and "hit" in woke[0]


def test_plugin_dir_script_requires_a_dry_run_of_its_current_content(env):
    plug, _, _ = env
    mk(plug, "exit 0")
    with pytest.raises(ValueError, match="dry_run=True"):
        mcp_tools.watch_script("conv", "t.sh", "cron:*/10 * * * *", "p")
    res = mcp_tools.watch_script("conv", "t.sh", "cron:*/10 * * * *", "p", dry_run=True)
    assert res["outcome"] == "noop"
    assert "watch_id" in mcp_tools.watch_script("conv", "t.sh", "cron:*/10 * * * *", "p")
    mk(plug, "exit 0 # edited")  # content changed -> gate re-arms
    with pytest.raises(ValueError, match="dry_run=True"):
        mcp_tools.watch_script("conv", "t.sh", "cron:*/10 * * * *", "p")


def test_dry_run_reports_error_outcome_and_does_not_unlock(env):
    plug, _, _ = env
    mk(plug, "echo bad >&2; exit 1")
    res = mcp_tools.watch_script("conv", "t.sh", "cron:*/10 * * * *", "p", dry_run=True)
    assert res["outcome"] == "error" and "bad" in res["stderr"]
    with pytest.raises(ValueError, match="dry_run=True"):
        mcp_tools.watch_script("conv", "t.sh", "cron:*/10 * * * *", "p")


def test_shipped_jsm_watch_script_is_valid_and_exempt_from_dry_run_gate():
    p = ws.resolve_script("jsm_watch.py")
    assert p.name == "jsm_watch.py"
    ws.require_dry_run("jsm_watch.py")
