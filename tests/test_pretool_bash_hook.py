"""rtk preToolUse hook (scripts/hooks/pretool_bash.py): allowlist-only, fail-open.

Guards the token-saving hook: only git status/log/branch and plain ls may be
rewritten to `rtk ...`; grep/find/cat/aws/heredocs/`c` wrappers must pass
through untouched (rtk drops grep matches and ls metadata), and any failure
(missing rtk, timeout, bad JSON, kill switch) must yield no output so the
tool call is never denied.
"""
import importlib.util
import json
import pathlib
import subprocess
import types

p = pathlib.Path(__file__).resolve().parents[1] / "scripts/hooks/pretool_bash.py"
spec = importlib.util.spec_from_file_location("pretool_bash", p)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def payload(cmd):
    return json.dumps({"toolName": "bash", "toolArgs": json.dumps({"command": cmd})})


def fake_run(cmd_out):
    def run(*a, **k):
        new = cmd_out(json.loads(k["input"]))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"modifiedArgs": {"command": new}}))
    return run


def test_allowlisted_commands_are_rewritten(monkeypatch):
    monkeypatch.delenv("RTK_HOOK_OFF", raising=False)
    monkeypatch.setattr(hook, "OFF_FILE", "/nonexistent")
    monkeypatch.setattr(hook, "log", lambda m: None)
    for c in ["git status", "git log -5", "git branch -a", "cd /x && git status | head", "ls", "ls /tmp foo", "git log -20 | tail -5"]:
        assert hook.decide(payload(c), run=fake_run(lambda pl: "rtk " + json.loads(pl["toolArgs"])["command"])), c


def test_everything_else_passes_through_untouched(monkeypatch):
    monkeypatch.delenv("RTK_HOOK_OFF", raising=False)
    monkeypatch.setattr(hook, "OFF_FILE", "/nonexistent")
    def boom(*a, **k):
        raise AssertionError("rtk must not be called")
    for c in ["grep -rn foo .", "find . -name x", "cat a.txt", "aws ec2 describe-instances", "curl -s x", "git diff", "git commit -m x",
              "git log --format=%H", "git log -p", "ls -la", "c prd myrepo1 -s 'git status'", "git status; rm -rf x", "git status && git push",
              "python3 - <<EOF\nprint(1)\nEOF", "git status\nls", "git status $(id)", "git status `id`", "git log > out.txt"]:
        assert hook.decide(payload(c), run=boom) == "", c


def test_non_bash_tool_and_bad_json_do_not_rewrite(monkeypatch):
    monkeypatch.setattr(hook, "OFF_FILE", "/nonexistent")
    assert hook.decide(json.dumps({"toolName": "view", "toolArgs": "{}"}), run=None) == ""


def test_kill_switch_env_and_file(monkeypatch, tmp_path):
    ok = fake_run(lambda pl: "rtk git status")
    monkeypatch.setattr(hook, "OFF_FILE", "/nonexistent")
    monkeypatch.setattr(hook, "log", lambda m: None)
    assert hook.decide(payload("git status"), run=ok)
    monkeypatch.setenv("RTK_HOOK_OFF", "1")
    assert hook.decide(payload("git status"), run=ok) == ""
    monkeypatch.delenv("RTK_HOOK_OFF")
    f = tmp_path / "off"; f.write_text("")
    monkeypatch.setattr(hook, "OFF_FILE", str(f))
    assert hook.decide(payload("git status"), run=ok) == ""


def test_main_is_fail_open_on_garbage_and_rtk_errors(monkeypatch, capsys):
    monkeypatch.setattr(hook, "log", lambda m: None)
    monkeypatch.setattr(hook, "OFF_FILE", "/nonexistent")
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(read=lambda: "not json"))
    assert hook.main() == 0 and capsys.readouterr().out == ""
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("rtk", 3)
    monkeypatch.setattr(hook.subprocess, "run", timeout)
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(read=lambda: payload("git status")))
    assert hook.main() == 0 and capsys.readouterr().out == ""
