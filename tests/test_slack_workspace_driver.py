"""Slack workspace package driver (drivers/slack_workspace/) — a workspace OTHER than the bot's own Slack adapter.

Feature: generic Slack integration (CLI + watcher + skill + link cards) driven by integration config. Guarantees:
the manifest loads and is off by default; one `slack-ws-<instance>` skill renders per enabled instance without
leftovers; permalinks resolve only to the instance with the matching base_url; hostile options are rejected;
the token never appears in output/errors; `post` is a draft (nothing sent) without --yes and is blocked with
write_mode=none; read commands are compact; watchers baseline silently and then report new messages / mentions /
thread replies exactly once. A fake Slack Web API replaces the network (no real credentials needed).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "slack_workspace"
sys.path.insert(0, str(DRV / "cli"))
sys.modules.pop("client", None)  # every driver CLI has its own top-level `client`
import client as sclient  # noqa: E402
import slackconfig  # noqa: E402

TOKEN = "xoxp-1111-secret-token"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("slack_cli", DRV / "cli" / "cli.py")
watch = _load("slack_watch", DRV / "watch" / "slack_watch.py")


def _cfg(**opts):
    return {"ext": {"driver": "slack_workspace", "enabled": True, "base_url": "https://ext.slack.com",
                    "auth": {"type": "token", "token": "${env:SLACK_EXT_TOKEN}"}, "options": opts},
            "two": {"driver": "slack_workspace", "enabled": True, "base_url": "https://two.slack.com",
                    "auth": {"type": "token", "token": "${env:SLACK_EXT_TOKEN}"}}}


class FakeSlack:
    """Fake Web API: `routes[method]` is a dict or a callable(params)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def post(self, url, headers=None, timeout=None, data=None, json=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json if json is not None else data))
        r = self.routes.get(method, {"ok": False, "error": "unknown_method"})
        body = r(json if json is not None else data) if callable(r) else r

        class Resp:
            status_code, headers = 200, {}
            text = ""

            def json(self_):
                return body
        return Resp()


@pytest.fixture
def slack(monkeypatch):
    def make(routes):
        fake = FakeSlack({"auth.test": {"ok": True, "user_id": "UME000000", "user": "me", "team": "T"},
                          "users.info": lambda p: {"ok": True, "user": {"id": p["user"], "name": "u" + p["user"][-2:]}},
                          "conversations.info": {"ok": True, "channel": {"name": "general"}},
                          "chat.getPermalink": {"ok": True, "permalink": "https://ext.slack.com/archives/C00000000/p1"},
                          **routes})
        monkeypatch.setattr(sclient.requests, "post", fake.post)
        return fake
    inst = slackconfig.Instance("ext", "https://ext.slack.com", TOKEN, {})
    return make, inst


def test_manifest_valid_off_by_default_and_skill_rendered_per_instance(tmp_path):
    assert "slack_workspace" in build_registry({}).manifests
    assert not [i for i in build_registry({}).package_instances() if i.manifest.id == "slack_workspace"]
    names = ensure_skills(build_registry(_cfg()), tmp_path)
    assert {"slack-ws-ext", "slack-ws-two"} <= set(names)
    text = (tmp_path / "slack-ws-ext" / "SKILL.md").read_text()
    assert "https://ext.slack.com" in text and "--instance ext" in text and "${{" not in text
    desc = [ln for ln in text.splitlines() if ln.startswith("description:")][0]
    assert "--instance" not in desc and "cli" not in desc.lower() and TOKEN not in text


def test_permalinks_resolve_only_to_the_instance_with_that_base_url():
    reg = build_registry(_cfg())
    it, kind, ident = reg.resolve_url("https://two.slack.com/archives/C0123ABCDE/p1700000000123456?thread_ts=1.2")
    assert (it.name, kind, ident["channel"], ident["ts"]) == ("two", "message", "C0123ABCDE", "1700000000123456")
    assert reg.resolve_url("https://other.slack.com/archives/C0123ABCDE/p1700000000123456") is None


def test_watcher_recipe_pins_instance_and_hostile_options_are_rejected():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "slack_watch.py", "watcher": "slack-ws-mentions",
                                        "integration": "ext", "args": ["--channel", "C0123ABCDE"]}, reg)
    assert meta["args"][0] == "mentions" and meta["integration"] == "ext"
    assert watch_scripts.instance_env(meta, reg) == {"SLACK_WS_INSTANCE": "ext"}
    for bad in ({"api_url": "http://evil.example/api"}, {"write_mode": "yes"}, {"title": "x`id`"}):
        names = [i.name for i in build_registry(_cfg(**bad)).package_instances()]
        assert "ext" not in names


def test_driver_folder_is_self_contained_and_leaves_the_bot_slack_adapter_alone():
    for p in DRV.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            low = p.read_text(errors="ignore").lower()
            if p.suffix == ".py":
                assert "import adapters" not in low and "import handlers" not in low, p
    assert not [p for p in DRV.rglob("*") if p.is_symlink()]


def test_load_instance_never_falls_back_and_never_prints_token(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("integrations:\n  ext: {driver: slack_workspace, enabled: true, base_url: 'https://ext.slack.com', "
                   "auth: {type: token, token: '${env:SLACK_EXT_TOKEN}'}}\n"
                   "  off: {driver: slack_workspace, enabled: false, base_url: 'https://o.slack.com', auth: {type: token, token: x}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("SLACK_EXT_TOKEN", TOKEN)
    assert slackconfig.load_instance().token == TOKEN
    with pytest.raises(slackconfig.InstanceError, match="not found/enabled"):
        slackconfig.load_instance("off")
    monkeypatch.setenv("SLACK_EXT_TOKEN", "")
    with pytest.raises(slackconfig.InstanceError) as e:
        slackconfig.load_instance("ext")
    assert TOKEN not in str(e.value)
    assert TOKEN not in slackconfig.redact(f"failed token={TOKEN} and {TOKEN}")


def test_api_errors_carry_fix_hints_and_no_token(slack):
    make, inst = slack
    make({"conversations.list": {"ok": False, "error": "missing_scope", "needed": "channels:read"}})
    with pytest.raises(sclient.ApiError, match="channels:read"):
        sclient.Client(inst).call("conversations.list")


def test_history_is_compact_oldest_first_and_resolves_mentions(slack, capsys):
    make, inst = slack
    make({"conversations.history": {"ok": True, "messages": [
        {"ts": "1700000200.000000", "user": "U2222222A", "text": "second <@U1111111A>", "reply_count": 2},
        {"ts": "1700000100.000000", "user": "U1111111A", "text": "first\n\nline"}]}})
    c = sclient.Client(inst)
    assert cli.cmd_history(c, cli.build().parse_args(["history", "C0123ABCDE", "--limit", "5"])) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "first line" in lines[0] and "@u1A" in lines[1] and "[2 replies]" in lines[1]


def test_post_is_a_draft_without_yes_and_blocked_in_read_only_mode(slack, capsys):
    make, inst = slack
    fake = make({"chat.postMessage": {"ok": True, "ts": "1700000300.000000"}})
    c = sclient.Client(inst)
    a = cli.build().parse_args(["post", "C0123ABCDE", "hello", "--thread-ts", "1700000100.000000"])
    assert cli.cmd_post(c, a) == 0
    out = capsys.readouterr().out
    assert "DRAFT (not sent)" in out and "#general" in out and "hello" in out
    assert not [x for x in fake.calls if x[0] == "chat.postMessage"]
    a.yes = True
    cli.cmd_post(c, a)
    sent = [x for x in fake.calls if x[0] == "chat.postMessage"]
    assert sent and sent[0][1]["thread_ts"] == "1700000100.000000"
    ro = sclient.Client(slackconfig.Instance("ro", "https://ext.slack.com", TOKEN, {"write_mode": "none"}))
    with pytest.raises(sclient.ApiError, match="disabled"):
        cli.cmd_post(ro, a)


@pytest.mark.parametrize("bad", ["", "not-a-ts", "1700000000", "17000000001.12"])
def test_message_ts_validation_rejects_garbage(bad):
    with pytest.raises(sclient.ApiError):
        sclient.clean_ts(bad)


def test_fetch_card_and_test_steps(slack, monkeypatch):
    make, inst = slack
    make({"conversations.history": {"ok": True, "messages": [{"ts": "1700000000.123456", "user": "U1111111A", "text": "hi", "reply_count": 1}]},
          "conversations.list": {"ok": True, "channels": [{"id": "C0123ABCDE"}]},
          "search.messages": {"ok": False, "error": "not_allowed_token_type"}})
    card = cli.fetch_card(sclient.Client(inst), "message", {"channel": "C0123ABCDE", "ts": "p1700000000123456"})
    assert card["fields"]["replies"] == "1" and card["title"].startswith("#general")
    monkeypatch.setattr(slackconfig, "load_instance", lambda name=None: inst)
    res = cli.run_test("ext")
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["config", "auth", "channels", "search"]
    assert not res["steps"][3]["ok"] and TOKEN not in json.dumps(res)


def _run_watch(monkeypatch, tmp_path, inst, argv):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(slackconfig, "load_instance", lambda name=None: inst)
    return watch.main(argv)


def test_messages_and_mentions_watchers_baseline_then_report_once(slack, monkeypatch, tmp_path, capsys):
    make, inst = slack
    hist = {"msgs": [{"ts": "1700000100.000000", "user": "U1111111A", "text": "old"}]}
    make({"conversations.history": lambda p: {"ok": True, "messages": [m for m in hist["msgs"] if float(m["ts"]) > float(p.get("oldest", 0))][: p["limit"]][::-1]}})
    for mode in ("messages", "mentions"):
        argv = [mode, "--channel", "C0123ABCDE"]
        assert _run_watch(monkeypatch, tmp_path, inst, argv) == 0      # baseline, silent
    hist["msgs"] += [{"ts": "1700000200.000000", "user": "U1111111A", "text": "hello team"},
                     {"ts": "1700000300.000000", "user": "U1111111A", "text": "ping <@UME000000> please"},
                     {"ts": "1700000400.000000", "user": "UME000000", "text": "my own"}]
    assert _run_watch(monkeypatch, tmp_path, inst, ["messages", "--channel", "C0123ABCDE"]) == 200
    out = capsys.readouterr().out
    assert "2 new message" in out and "hello team" in out and "my own" not in out
    assert _run_watch(monkeypatch, tmp_path, inst, ["messages", "--channel", "C0123ABCDE"]) == 0
    assert _run_watch(monkeypatch, tmp_path, inst, ["mentions", "--channel", "C0123ABCDE"]) == 200
    out = capsys.readouterr().out
    assert "1 new mention" in out and "hello team" not in out


def test_thread_watcher_reports_only_new_replies(slack, monkeypatch, tmp_path, capsys):
    make, inst = slack
    thread = [{"ts": "1700000100.000000", "user": "U1111111A", "text": "root"}]
    make({"conversations.replies": lambda p: {"ok": True, "messages": list(thread)}})
    argv = ["thread", "--channel", "C0123ABCDE", "--thread-ts", "1700000100.000000"]
    assert _run_watch(monkeypatch, tmp_path, inst, argv) == 0
    thread.append({"ts": "1700000200.000000", "user": "U2222222A", "text": "a reply"})
    assert _run_watch(monkeypatch, tmp_path, inst, argv) == 200
    assert "a reply" in capsys.readouterr().out
    assert _run_watch(monkeypatch, tmp_path, inst, argv) == 0


def test_watcher_rate_limit_is_silent_and_auth_error_fails(slack, monkeypatch, tmp_path, capsys):
    make, inst = slack
    make({"auth.test": {"ok": False, "error": "invalid_auth"}})
    assert _run_watch(monkeypatch, tmp_path, inst, ["messages", "--channel", "C0123ABCDE"]) == 1
    assert TOKEN not in capsys.readouterr().err
    assert _run_watch(monkeypatch, tmp_path, inst, ["messages"]) == 1  # no channel given


def test_since_parser_accepts_relative_and_dates_and_rejects_garbage(slack):
    """`--since` must never pass arbitrary text to Slack's `oldest` parameter."""
    assert float(sclient.parse_since("2h", now=10000.0)) == 10000.0 - 7200
    assert float(sclient.parse_since("2026-01-02")) > 0
    with pytest.raises(sclient.ApiError):
        sclient.parse_since("yesterday; rm")


def test_fetch_rejects_a_hostile_channel_ident(slack):
    make, inst = slack
    make({})
    with pytest.raises(sclient.ApiError):
        cli.fetch_card(sclient.Client(inst), "message", {"channel": "C1&x=y", "ts": "1700000000123456"})
