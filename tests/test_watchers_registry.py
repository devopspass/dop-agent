"""Watchers driven by the integrations registry (not hard-coded GitLab/Jira/Teams knowledge).

Guarantees: package drivers (gitlab, jira) contribute no native watch types, so only the builtin
types are accepted; a watch of a type no enabled integration provides becomes `broken` with a clear
reason instead of crashing the poller, and is retried on every watcher tick.
"""
from __future__ import annotations

import asyncio

import pytest

import watchers
from integrations import runtime
from integrations.registry import build_registry

GITLAB = {"driver": "gitlab", "base_url": "https://gitlab.test", "auth": {"type": "private_token", "token": "t"}}
JIRA = {"driver": "jira", "base_url": "https://jira.test", "auth": {"type": "token_file"},
        "options": {"email": "u@example.com", "token_file": "/tmp/jira-test-token"}}


def _install(monkeypatch, cfg):
    reg = build_registry(cfg)
    monkeypatch.setattr(runtime, "_registry", reg)
    return reg


def _job_watch(**kw):
    w = {"id": 5, "type": "job", "key": "g/p#7", "label": "L", "conversation_id": "c", "status": "active",
         "meta": "{}", "last_state": "{}", "created_at": None}
    w.update(kw)
    return w


@pytest.fixture
def recorded(monkeypatch):
    out = {"injected": [], "broken": [], "notices": [], "states": []}

    async def inject(cid, text):
        out["injected"].append(text)

    async def notice(cid, text):
        out["notices"].append(text)

    monkeypatch.setattr(watchers, "_inject_watch_message", inject)
    monkeypatch.setattr(watchers, "_deliver_system_notice", notice)
    monkeypatch.setattr(watchers, "mark_broken", lambda wid, reason: out["broken"].append((wid, reason)))
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, st: out["states"].append((wid, st)))
    return out


def test_package_drivers_add_no_watch_types_beyond_the_builtins(monkeypatch):
    _install(monkeypatch, {"jira": JIRA})
    types = watchers.valid_types()
    assert "ticket" not in types and {"time", "session_turn", "script", "self_restart"} <= set(types)

    _install(monkeypatch, {"jira": JIRA, "gitlab": GITLAB})
    assert set(watchers.valid_types()) == set(types)
    assert not {"mr", "pipeline", "job"} & set(types)


def test_validate_key_rejects_types_no_integration_provides(monkeypatch):
    _install(monkeypatch, {"gitlab": GITLAB})
    with pytest.raises(ValueError, match="Unknown watch type 'mr'"):
        watchers.validate_key("mr", "group/project#42")


def test_add_watch_for_type_of_disabled_integration_is_rejected_with_hint(monkeypatch):
    _install(monkeypatch, {"gitlab": {**GITLAB, "enabled": False}, "jira": JIRA})
    with pytest.raises(ValueError, match="Unknown watch type 'mr'.*enabled integration"):
        watchers.add_watch("mr", "conv", key="g/p#1")
    with pytest.raises(ValueError, match="no enabled integration|Unknown watch type"):
        watchers.validate_key("mr", "g/p#1")


def test_poll_of_watch_whose_integration_is_disabled_marks_it_broken_without_crashing(monkeypatch, recorded):
    _install(monkeypatch, {"gitlab": {**GITLAB, "enabled": False}})
    asyncio.run(watchers._poll_integration_watch(_job_watch()))
    assert len(recorded["broken"]) == 1 and recorded["broken"][0][0] == 5
    assert "no enabled integration provides watch type 'job'" in recorded["broken"][0][1]
    assert "no active integration" in recorded["notices"][0] and "re-enable" in recorded["notices"][0]
    assert recorded["injected"] == []


def test_describe_types_reports_builtins_only_with_emoji_and_no_integration(monkeypatch):
    _install(monkeypatch, {"gitlab": GITLAB, "jira": JIRA})
    d = watchers.describe_types()
    assert set(d) == set(watchers.valid_types())
    assert "mr" not in d and "ticket" not in d
    assert d["time"]["builtin"] is True and d["time"]["integration"] is None and d["time"]["emoji"]
    assert d["script"]["builtin"] is True
    doc = watchers.watch_types_doc()
    assert "script: key = schedule" in doc and "mr:" not in doc


def test_watcher_loop_polls_unresolvable_watches_on_every_tick(monkeypatch):
    _install(monkeypatch, {"gitlab": GITLAB})
    polled = []

    async def poll(w):
        polled.append(w["type"])

    async def noop(*a, **k):
        return None

    class Stop(BaseException):
        pass

    watches = [_job_watch(id=1, type="job"), _job_watch(id=2, type="mr", key="g/p#1")]
    monkeypatch.setattr(watchers, "list_watches", lambda *a, **k: watches)
    monkeypatch.setattr(watchers, "_announce_new_watches", noop)
    monkeypatch.setattr(watchers, "_write_watchers_md", noop)
    monkeypatch.setattr(watchers, "_poll_integration_watch", poll)
    n = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(_):
        n["n"] += 1
        await real_sleep(0.01)
        if n["n"] > 3:
            raise Stop()

    monkeypatch.setattr(watchers.asyncio, "sleep", fake_sleep)

    async def run():
        with pytest.raises(Stop):
            await watchers.watcher_loop()

    asyncio.run(run())
    assert polled.count("job") >= 2 and polled.count("mr") >= 2
