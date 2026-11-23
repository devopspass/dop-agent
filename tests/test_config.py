"""Config core (config.py): precedence env > config.yaml > default, secret refs
(env/file with perms check), masking so secrets never leak into repr/dump,
atomic save round-trip preserving refs, zero-config back-compat, CLI check."""

from __future__ import annotations

import os
import stat

import pytest
import yaml

import config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(path))
    for s in config.SETTINGS:
        for n in s.env:
            monkeypatch.delenv(n, raising=False)
        monkeypatch.delenv(f"{s.env[0]}_FILE", raising=False) if s.env else None
    config.load()

    def write(doc, mode=0o600):
        path.write_text(yaml.safe_dump(doc))
        path.chmod(mode)
        config.reload()

    write.path = path
    yield write
    config._callbacks.clear()
    config._validators.clear()
    config.load()


def test_zero_config_defaults_match_legacy(cfg):
    assert not cfg.path.exists()
    assert config.value("agent.session_timeout_s") == 120.0
    assert config.value("adapters.web.port") == 10818
    assert config.value("adapters.web.host") == "127.0.0.1"
    assert config.value("agent.model_routing.mode") == "on"
    assert config.value("agent.usage_widget") is True
    assert config.source("agent.session_timeout_s") == "default"
    assert config.workdir() == os.path.expanduser("~")
    assert config.errors() == []
    assert config.integrations() == {}


def test_precedence_env_over_file_over_default(cfg, monkeypatch):
    cfg({"agent": {"session_timeout_s": 300}})
    assert (config.value("agent.session_timeout_s"), config.source("agent.session_timeout_s")) == (300.0, "file")
    monkeypatch.setenv("SESSION_TIMEOUT", "42")
    assert (config.value("agent.session_timeout_s"), config.source("agent.session_timeout_s")) == (42.0, "env:SESSION_TIMEOUT")


def test_bool_and_list_env_parsing_matches_legacy(cfg, monkeypatch):
    monkeypatch.setenv("WEB_UI_FRONTEND_ENABLED", "")
    assert config.value("adapters.web.frontend_enabled") is False
    monkeypatch.setenv("WEB_UI_ALLOWED_HOSTS", "a:1, B:2")
    assert config.value("adapters.web.allowed_hosts") == ["a:1", "B:2"]
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1;2,x")
    assert config.value("adapters.telegram.allowed_user_ids") == ["1", "2", "x"]


def test_secret_env_ref_and_masking(cfg, monkeypatch):
    monkeypatch.setenv("MY_TOK", "s3cr3t")
    cfg({"mcp": {"auth_token": "${env:MY_TOK}"}})
    v = config.value("mcp.auth_token")
    assert isinstance(v, config.Secret) and v.reveal() == "s3cr3t"
    assert "s3cr3t" not in repr(v) and "s3cr3t" not in str(v)
    assert config.secret_value("mcp.auth_token") == "s3cr3t"
    assert "s3cr3t" not in repr(config.dump(redact=True))
    assert config.dump(redact=True)["mcp"]["auth_token"] == config.MASK


def test_legacy_env_beats_secret_ref(cfg, monkeypatch):
    monkeypatch.setenv("MY_TOK", "from-ref")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "from-env")
    cfg({"mcp": {"auth_token": "${env:MY_TOK}"}})
    assert config.secret_value("mcp.auth_token") == "from-env"


def test_secret_file_ref_requires_private_perms(cfg, tmp_path):
    f = tmp_path / "tok"
    f.write_text("filetok\n")
    f.chmod(0o600)
    cfg({"mcp": {"auth_token": f"${{file:{f}}}"}})
    assert config.secret_value("mcp.auth_token") == "filetok"
    f.chmod(0o644)
    assert config.secret_value("mcp.auth_token") == ""
    with pytest.raises(config.SecretError):
        config.resolve_ref(f"${{file:{f}}}")


def test_legacy_telegram_token_file_env_refuses_world_readable(cfg, tmp_path, monkeypatch):
    f = tmp_path / "t"
    f.write_text("tgtok")
    f.chmod(0o640)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(f))
    assert config.secret_value("adapters.telegram.bot_token") == ""
    f.chmod(0o600)
    assert config.secret_value("adapters.telegram.bot_token") == "tgtok"


def test_inline_secret_is_rejected_and_file_ignored(cfg):
    cfg({"mcp": {"auth_token": "plaintext"}, "agent": {"session_timeout_s": 999}})
    assert any("never inline" in e for e in config.errors())
    assert config.value("agent.session_timeout_s") == 120.0  # invalid file -> defaults, no crash


def test_unknown_key_and_bad_type_reported(cfg):
    cfg({"agent": {"nope": 1, "session_timeout_s": "abc"}, "bogus": {}})
    errs = " | ".join(config.errors())
    assert "agent.nope" in errs and "session_timeout_s" in errs and "bogus" in errs


def test_integrations_envelope_validation(cfg):
    cfg({"integrations": {
        "jira": {"driver": "jira", "base_url": "https://x", "auth": {"type": "basic", "token": "${env:T}"},
                 "kinds": {"ticket": {"enabled": True}}},
        "bad": {"enabled": True},
    }})
    assert any("integrations.bad.driver" in e for e in config.errors())
    cfg({"integrations": {"jira": {"driver": "jira", "auth": {"token": "${env:T}"}}}})
    i = config.get().integrations["jira"]
    assert i.driver == "jira" and i.enabled and i.auth["token"] == "${env:T}"


def test_integration_auth_inline_secret_rejected_and_resolved_auth_masked(cfg, monkeypatch):
    cfg({"integrations": {"j": {"driver": "jira", "auth": {"token": "inline"}}}})
    assert any("auth.token" in e for e in config.errors())
    monkeypatch.setenv("T", "abc")
    cfg({"integrations": {"j": {"driver": "jira", "auth": {"type": "basic", "token": "${env:T}"}}}})
    a = config.integrations()["j"].resolved_auth()
    assert a["type"] == "basic" and a["token"].reveal() == "abc" and "abc" not in repr(a)
    assert "abc" not in repr(config.dump())


def test_integration_validator_hook(cfg):
    config.register_integration_validator(lambda ints: [f"unknown driver {i.driver}" for i in ints.values()])
    cfg({"integrations": {"j": {"driver": "zzz"}}})
    assert "unknown driver zzz" in config.errors()


def test_save_round_trip_atomic_preserves_refs_and_perms(cfg, monkeypatch):
    monkeypatch.setenv("MY_TOK", "s3cr3t")
    cfg({"mcp": {"auth_token": "${env:MY_TOK}"}})
    fired = []
    config.on_change(lambda: fired.append(1))
    new = config.dump(redact=False)
    new["agent"] = {"session_timeout_s": 77}
    new["mcp"] = {"auth_token": config.MASK}  # UI sends the mask back untouched
    config.save(new)
    on_disk = yaml.safe_load(cfg.path.read_text())
    assert on_disk["mcp"]["auth_token"] == "${env:MY_TOK}"
    assert "s3cr3t" not in cfg.path.read_text()
    assert stat.S_IMODE(cfg.path.stat().st_mode) == 0o600
    assert config.value("agent.session_timeout_s") == 77.0 and fired
    assert [p.name for p in cfg.path.parent.iterdir()] == ["config.yaml"]


def test_save_rejects_invalid_without_touching_file(cfg):
    cfg({"agent": {"session_timeout_s": 5}})
    before = cfg.path.read_text()
    with pytest.raises(config.ConfigError):
        config.save({"mcp": {"auth_token": "inline"}})
    assert cfg.path.read_text() == before


def test_cli_check_exit_codes_and_masking(cfg, monkeypatch, capsys):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "topsecret")
    assert config._cli(["check"]) == 0
    out = capsys.readouterr().out
    assert "topsecret" not in out and "[env:MCP_AUTH_TOKEN]" in out
    cfg({"agent": {"bad": 1}})
    assert config._cli(["check"]) == 1


def test_yaml_bare_on_off_accepted_for_enum_settings(cfg):
    cfg({"agent": {"model_routing": {"mode": False}}})
    assert config.errors() == []
    assert config.value("agent.model_routing.mode") == "off"


def test_retired_settings_in_existing_config_files_are_ignored_not_errors():
    """Config files containing adapters.web.enabled, watchers.plugin_dirs or unfurl.hide_delay_ms
    still load (the keys are silently ignored) instead of failing with 'unknown setting'."""
    errs, _ = config.validate({"adapters": {"web": {"enabled": True}},
                               "watchers": {"plugin_dirs": ["x"]}, "unfurl": {"hide_delay_ms": 5}})
    assert errs == []
    assert config.validate({"unfurl": {"bogus": 1}})[0] == ["unfurl.bogus: unknown setting"]
