"""Jira package driver: watcher script semantics, skill rendering, option validation, no site-specific defaults.

Feature: `watch/jira_watch.py jql` records a silent baseline, then exits 200 only for new/updated issues
(`--new-only` ignores updates); transient HTTP errors stay silent. The skill renders per instance with absolute
paths and no leftover placeholders; the manifest rejects unsafe/missing options.
"""
import importlib.util
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills

ROOT = Path(__file__).resolve().parents[1] / "drivers" / "jira"
INST = {"driver": "jira", "base_url": "https://jira.example.net", "auth": {"type": "token_file"},
        "options": {"email": "me@example.com", "token_file": "/tmp/jira-token-x", "title": "Main Jira"}}


def _watch():
    spec = importlib.util.spec_from_file_location("jira_watch_t", ROOT / "watch" / "jira_watch.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeCli:
    CONF = {"base_url": "https://jira.example.net"}
    API = "/rest/api/3"

    def __init__(self, issues=None, err=None):
        self.issues, self.err = issues or [], err

    def configure(self):
        pass

    def short(self, s, n):
        return (s or "")[:n]

    def call(self, method, path, params=None):
        if self.err:
            raise SystemExit(self.err)
        return {"issues": self.issues}


def _issue(key, updated, summary="S"):
    return {"key": key, "fields": {"summary": summary, "updated": updated, "status": {"name": "To Do"}}}


def _run(w, cli, monkeypatch, tmp_path, *extra):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(w, "load_cli", lambda: cli)
    monkeypatch.setattr("sys.argv", ["jira_watch.py", "jql", "project = PRJ", *extra])
    with pytest.raises(SystemExit) as e:
        w.main()
    return e.value.code


def test_baseline_silent_then_new_and_updated_reported(monkeypatch, tmp_path, capsys):
    w = _watch()
    assert _run(w, FakeCli([_issue("PRJ-1", "t1")]), monkeypatch, tmp_path) == 0
    assert _run(w, FakeCli([_issue("PRJ-1", "t1")]), monkeypatch, tmp_path) == 0
    code = _run(w, FakeCli([_issue("PRJ-1", "t2"), _issue("PRJ-2", "t1")]), monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert code == 200 and "PRJ-1 [updated]" in out and "PRJ-2 [new]" in out
    assert "https://jira.example.net/browse/PRJ-2" in out


def test_new_only_ignores_updates(monkeypatch, tmp_path):
    w = _watch()
    _run(w, FakeCli([_issue("PRJ-1", "t1")]), monkeypatch, tmp_path, "--new-only")
    assert _run(w, FakeCli([_issue("PRJ-1", "t2")]), monkeypatch, tmp_path, "--new-only") == 0


def test_transient_error_is_silent_but_auth_error_fails(monkeypatch, tmp_path):
    w = _watch()
    assert _run(w, FakeCli(err="HTTP 503: x"), monkeypatch, tmp_path) == 0
    assert _run(w, FakeCli(err="HTTP 401: nope"), monkeypatch, tmp_path) == 1


def test_skill_renders_generic_with_absolute_paths(tmp_path):
    reg = build_registry({"jira-main": INST})
    assert ensure_skills(reg, tmp_path) == ["jira-jira-main"]
    text = (tmp_path / "jira-jira-main" / "SKILL.md").read_text()
    assert "${{" not in text and "https://jira.example.net" in text
    assert str(ROOT / "cli" / "cli.py") in text and "me@example.com" in text


@pytest.mark.parametrize("opts", [{"token_file": "/tmp/x"}, {"email": "a b@x", "token_file": "/tmp/x"},
                                  {"email": "a@x.com", "token_file": "/tmp/x`id`"}])
def test_invalid_or_missing_options_are_rejected(opts):
    reg = build_registry({"j": {**INST, "options": opts}})
    assert reg.get("j").errors


class TicketCli(FakeCli):
    adf_to_text = staticmethod(lambda b: b or "")

    def __init__(self, status="To Do", updated="u1", comments=()):
        super().__init__()
        self.t = (status, updated, list(comments))

    def call(self, method, path, params=None):
        if path.endswith("/comment"):
            return {"comments": self.t[2]}
        return {"fields": {"summary": "S", "status": {"name": self.t[0]}, "updated": self.t[1]}}


def _tick(w, cli, monkeypatch, tmp_path, *extra):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(w, "load_cli", lambda: cli)
    monkeypatch.setattr("sys.argv", ["jira_watch.py", "ticket", "PRJ-1", *extra])
    with pytest.raises(SystemExit) as e:
        w.main()
    return e.value.code


def test_ticket_mode_reports_comments_status_and_field_changes_with_filters(monkeypatch, tmp_path, capsys):
    """`ticket` mode: baseline silent, then reports comment/status/field changes, honoring filters."""
    w = _watch()
    c1 = {"id": "1", "author": {"displayName": "Ann"}, "body": "old"}
    assert _tick(w, TicketCli(comments=[c1]), monkeypatch, tmp_path, "--match", "deploy") == 0
    c2 = {"id": "2", "author": {"displayName": "Bob"}, "body": "please deploy"}
    c3 = {"id": "3", "author": {"displayName": "Bob"}, "body": "chatter"}
    assert _tick(w, TicketCli("To Do", "u2", [c1, c2, c3]), monkeypatch, tmp_path, "--match", "deploy") == 200
    out = capsys.readouterr().out
    assert "Bob: please deploy" in out and "chatter" not in out
    assert _tick(w, TicketCli("Done", "u3", [c1, c2, c3]), monkeypatch, tmp_path, "--match", "deploy") == 200
    assert "To Do -> Done" in capsys.readouterr().out
    assert _tick(w, TicketCli("Done", "u4", [c1, c2, c3]), monkeypatch, tmp_path, "--match", "deploy") == 200
    assert "fields changed" in capsys.readouterr().out
    assert _tick(w, TicketCli("Done", "u4", [c1, c2, c3]), monkeypatch, tmp_path, "--match", "deploy") == 0
