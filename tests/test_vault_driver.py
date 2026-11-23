"""HashiCorp Vault package driver (drivers/vault/) — metadata-only integration.

Feature: read-only Vault access (health/seal, auth methods, mounts, policies, KV metadata, token/lease info, audit)
that NEVER reads secret values. Guarantees tested here (against a fake HTTP backend, no real Vault):
secret-data endpoints (/data/, /creds/, /decrypt/, /sign/, /read, unwrap, sys/raw) and unknown endpoints are refused
before any request is sent, with an explanation; there is no write beyond the two lookup POSTs; the token is never
printed (lookup-self `id` scrubbed, errors redacted); KV metadata output has no secret data; the manifest is off by
default and renders a skill without leftovers; hostile option values are rejected; watchers baseline/fire once.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "vault"
sys.path.insert(0, str(DRV / "cli"))
sys.modules.pop("client", None)  # every driver CLI has its own top-level `client`
import client as vc  # noqa: E402
import vaultconfig  # noqa: E402

_s = importlib.util.spec_from_file_location("vault_cli", DRV / "cli" / "cli.py")
cli = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cli)
_w = importlib.util.spec_from_file_location("vault_watch", DRV / "watch" / "vault_watch.py")
watch = importlib.util.module_from_spec(_w)
_w.loader.exec_module(watch)

TOKEN = "hvs.SUPERSECRETTOKENVALUE1234567890"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeVault:
    """Records requests; answers canned metadata. Anything unexpected returns 404."""

    def __init__(self):
        self.calls, self.health_status = [], 200
        self.token_expire = _iso(datetime.now(timezone.utc) + timedelta(days=2))
        self.lease_expire = _iso(datetime.now(timezone.utc) + timedelta(hours=1))

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        self.calls.append((req.get_method(), url, req.get_header("X-vault-token"), req.get_header("X-vault-namespace")))
        path = url.split("/v1/", 1)[1].split("?")[0]
        body = json.loads(req.data) if req.data else None
        resp = {
            "sys/health": {"initialized": True, "sealed": self.health_status == 503, "version": "1.15.0"},
            "sys/mounts": {"data": {"secret/": {"type": "kv", "options": {"version": "2"}, "description": "kv2"},
                                    "aws/": {"type": "aws", "options": {}}}},
            "sys/auth": {"data": {"token/": {"type": "token", "description": "tok"}}},
            "auth/token/lookup-self": {"data": {"id": TOKEN, "accessor": "acc123456789", "policies": ["default"],
                                                "ttl": 100, "expire_time": self.token_expire, "renewable": True}},
            "secret/metadata/app/db": {"data": {"current_version": 2, "max_versions": 10, "created_time": "2024-01-01T00:00:00Z",
                                                "updated_time": "2024-02-01T00:00:00Z", "custom_metadata": {"owner": "x"},
                                                "versions": {"1": {"created_time": "2024-01-01T00:00:00Z", "deletion_time": "", "destroyed": False},
                                                             "2": {"created_time": "2024-02-01T00:00:00Z", "deletion_time": "", "destroyed": False}}}},
            "secret/metadata/app/": {"data": {"keys": ["db", "sub/"]}},
            "sys/leases/lookup/aws/": {"data": {"keys": ["creds/"]}},
            "sys/leases/lookup/aws/creds/": {"data": {"keys": ["role/"]}},
            "sys/leases/lookup/aws/creds/role/": {"data": {"keys": ["abc"]}},
            "sys/capabilities-self": {"secret/data/x": ["deny"], "capabilities": ["deny"]},
        }
        if path == "sys/leases/lookup":
            resp[path] = {"data": {"id": body["lease_id"], "ttl": 3600, "expire_time": self.lease_expire, "renewable": False}}
        if path not in resp:
            raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b'{"errors":[]}'))
        if path == "sys/health" and self.health_status != 200:
            raise urllib.error.HTTPError(url, self.health_status, "x", {}, io.BytesIO(json.dumps(resp[path]).encode()))
        return _Resp(json.dumps(resp[path]).encode())


@pytest.fixture
def vault(monkeypatch, tmp_path):
    fake = FakeVault()
    monkeypatch.setattr(vc.urllib.request, "urlopen", fake)
    monkeypatch.setenv("VAULT_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "st"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("integrations:\n  v1:\n    driver: vault\n    enabled: true\n    base_url: https://vault.example.com:8200\n"
                   "    auth: {type: token, token: '${env:VAULT_TEST_TOKEN}'}\n    options: {namespace: team-a}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("VAULT_INSTANCE", "v1")
    return fake


@pytest.mark.parametrize("path", [
    "secret/data/app/db", "aws/creds/role", "transit/decrypt/key", "transit/sign/key", "pki/sign/role",
    "database/creds/ro", "secret/foo/read", "sys/raw/secret", "cubbyhole/x", "sys/wrapping/unwrap",
    "/v1/secret/data/x",
])
def test_secret_data_endpoints_are_refused_before_any_request(vault, path):
    """Hard guard: endpoints that return secret data raise SecretEndpointError with a reason and send nothing."""
    c = vc.Client(vaultconfig.load_instance())
    for method in ("GET", "LIST", "POST"):
        with pytest.raises(vc.SecretEndpointError, match="secret data|allow-list"):
            c.request(method, path)
    assert vault.calls == []


def test_unknown_endpoints_and_writes_are_refused(vault):
    """Allow-list, not just deny-list: unknown paths and non-GET/LIST/lookup methods never reach the server."""
    c = vc.Client(vaultconfig.load_instance())
    for method, path in [("GET", "sys/config/state/sanitized"), ("POST", "secret/metadata/x"), ("DELETE", "secret/metadata/x"),
                         ("PUT", "sys/policies/acl/p"), ("GET", "sys/seal")]:
        with pytest.raises(vc.ApiError):
            c.request(method, path)
    for bad in ("secret/../sys/raw", "a//b", "secret/x?list=true", "", "sec ret/x"):
        with pytest.raises(vc.ApiError):
            vc.check_request("GET", bad)
    assert vault.calls == []


def test_metadata_path_named_data_is_allowed_after_metadata_segment():
    """A KV secret that happens to be called 'data' is still readable as metadata (guard looks before /metadata/)."""
    assert vc.check_request("GET", "secret/metadata/data/x") == "secret/metadata/data/x"


def test_cli_raw_refuses_data_endpoint_and_explains(vault, capsys):
    """CLI surface: `raw GET secret/data/x` exits 1 with an explanation and sends no request."""
    assert cli.main(["raw", "GET", "secret/data/app/db"]) == 1
    assert "metadata-only" in capsys.readouterr().err and vault.calls == []


def test_whoami_never_prints_the_token_and_sends_it_only_as_header(vault, capsys):
    """lookup-self returns the token as `id`; output must be scrubbed. Token only in X-Vault-Token header."""
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and "acc123456789" in out
    assert vault.calls[0][2] == TOKEN and vault.calls[0][3] == "team-a"


def test_errors_redact_token_like_values():
    """Error text never leaks token material."""
    assert TOKEN not in vc.redact(f"failed with token {TOKEN} and X-Vault-Token: {TOKEN}")


def test_meta_returns_versions_and_custom_metadata_without_data(vault, capsys):
    """KV v2 metadata: versions/timestamps/custom_metadata only; it calls .../metadata/, never .../data/."""
    assert cli.main(["meta", "secret/app/db"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["current_version"] == 2 and d["custom_metadata"] == {"owner": "x"} and set(d["versions"]) == {"1", "2"}
    assert all("/data/" not in u for _, u, _, _ in vault.calls)


def test_ls_uses_metadata_listing_for_kv2(vault, capsys):
    assert cli.main(["ls", "secret/app"]) == 0
    assert capsys.readouterr().out.split() == ["db", "sub/"]
    assert any("secret/metadata/app/" in u for _, u, _, _ in vault.calls)


def test_health_reports_sealed_state(vault, capsys):
    vault.health_status = 503
    assert cli.main(["health"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "sealed"


def test_test_command_reports_steps_and_set_options(vault):
    r = cli.run_test(None)
    assert r["ok"] and [s["name"] for s in r["steps"]] == ["config", "health", "auth", "mounts"]
    assert TOKEN not in json.dumps(r)


def test_test_command_fails_clearly_without_instance(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("integrations: {}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.delenv("VAULT_INSTANCE", raising=False)
    r = cli.run_test(None)
    assert not r["ok"] and r["steps"][0]["name"] == "config"


def test_empty_token_ref_raises_instance_error(monkeypatch, tmp_path):
    """A missing token ref must fail, never fall back to anything else."""
    monkeypatch.delenv("VAULT_TEST_TOKEN", raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("integrations:\n  v1: {driver: vault, base_url: 'https://v.example:8200', auth: {type: token, token: ''}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    with pytest.raises(vaultconfig.InstanceError):
        vaultconfig.load_instance("v1")


def test_caps_checks_permissions_without_reading(vault, capsys):
    assert cli.main(["caps", "secret/data/x"]) == 0
    assert "deny" in capsys.readouterr().out
    assert vault.calls[0][0] == "POST" and vault.calls[0][1].endswith("sys/capabilities-self")


def test_watch_health_baselines_silently_then_fires_on_seal_and_recovery(vault, capsys):
    """Healthy first run is silent; sealed fires once; staying sealed is silent; recovery fires."""
    assert watch.main(["health"]) == 0
    vault.health_status = 503
    assert watch.main(["health"]) == 200 and "sealed" in capsys.readouterr().out
    assert watch.main(["health"]) == 0
    vault.health_status = 200
    assert watch.main(["health"]) == 200 and "active" in capsys.readouterr().out


def test_watch_token_expiry_fires_once_per_token_and_ignores_far_expiry(vault, capsys):
    assert watch.main(["token-expiry", "--days", "1"]) == 0
    assert watch.main(["token-expiry", "--days", "7"]) == 200
    assert TOKEN not in capsys.readouterr().out
    assert watch.main(["token-expiry", "--days", "7"]) == 0


def test_watch_lease_expiry_reports_each_lease_once_and_requires_prefix(vault, capsys):
    assert watch.main(["lease-expiry"]) == 1
    assert watch.main(["lease-expiry", "--prefix", "aws", "--hours", "2"]) == 200
    assert "aws/creds/role/abc" in capsys.readouterr().out
    assert watch.main(["lease-expiry", "--prefix", "aws", "--hours", "2"]) == 0


def _cfg(extra=""):
    return {"prod": {"driver": "vault", "enabled": True, "base_url": "https://vault.one.example:8200",
                     "auth": {"type": "token", "token": "${env:X}"}, "options": {"title": "Prod"} if not extra else {"namespace": extra}}}


def test_manifest_off_by_default_and_skill_renders_without_leftovers(tmp_path):
    assert "vault" in build_registry({}).manifests and build_registry({}).package_instances() == []
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["vault-prod"]
    text = (tmp_path / "vault-prod" / "SKILL.md").read_text()
    assert "https://vault.one.example:8200" in text and "--instance prod" in text and "${{" not in text
    desc = text.split("description:")[1].split("\n")[0]
    assert "--instance" not in desc and "cli.py" not in desc and TOKEN not in text


def test_hostile_namespace_option_is_rejected():
    """Registry drops an instance whose namespace option fails the full-match pattern (no skill, no CLI args)."""
    assert build_registry(_cfg('x"; rm -rf /')).package_instances() == []


def test_token_with_whitespace_is_rejected_without_echoing_it(monkeypatch, tmp_path):
    """A malformed token (embedded newline) must fail at config load, not leak via an HTTP header error."""
    monkeypatch.setenv("VAULT_TEST_TOKEN", "hvs.abc\nINJECT: 1")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("integrations:\n  v1: {driver: vault, base_url: 'https://v.example:8200', auth: {type: token, token: '${env:VAULT_TEST_TOKEN}'}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    with pytest.raises(vaultconfig.InstanceError) as e:
        vaultconfig.load_instance("v1")
    assert "hvs.abc" not in str(e.value)
