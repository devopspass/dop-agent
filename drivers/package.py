"""Generic driver for `type: package` manifests: a CLI + watcher scripts + skill, no polling.

A package driver talks to its service through the package's own CLI (`cli.entry`), which is also what
the agent calls from the skill. dop-agent only needs three things from it:

  fetch(kind, ident)  -> runs `<cli> <global_args> fetch --kind K --ident '<json>'`; the CLI prints one JSON
                         object {title, status?, status_emoji?, url?, fields?} (the furl card).
  test_connection()   -> runs `<cli> <global_args> test`; prints {ok, message, steps?, set_options?}.
                         `set_options` (e.g. tenant_id learned from the token) is persisted by the settings API.
  poll()              -> unsupported: package watchers are `script` watches (see manifest `watchers:`).

Per-instance values come from `Integration.options` through `${{config.<option>}}` templates
(manifest `cli.global_args`, `env`, and the skill template). Secrets are never templated.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import Driver, Integration, Resource, UnsupportedOperation

CLI_TIMEOUT_S = 90


def template_context(it: Integration) -> dict:
    """Values available to `${{...}}` templates for one instance (no secrets)."""
    man = it.manifest
    d = Path(man.source_dir)
    cli = d / man.cli["entry"] if man.cli else None
    return {
        "config": {**it.options, "base_url": it.base_url,
                   "title": it.options.get("title") or it.options.get("account") or it.name},
        "instance": {"name": it.name},
        "driver": {"id": man.id, "dir": str(d), "cli": str(cli or ""), "python": sys.executable,
                   "cli_dir": str(cli.parent) if cli else "", "watch_dir": str(d / "watch")},
        "skill": {"name": f"{man.skill.get('name', man.id)}-{skill_suffix(it.name)}" if man.skill else ""},
    }


def skill_suffix(instance_name: str) -> str:
    """Instance name made safe for a skill directory name (`teams_aws` -> `teams-aws`)."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", instance_name.lower()).strip("-") or "instance"


class PackageDriver(Driver):
    def make_client(self):
        return None

    def _python(self) -> str:
        return sys.executable

    def cli_argv(self, *args: str) -> list[str]:
        from .templating import render
        man = self.integration.manifest
        if not man.cli:
            raise UnsupportedOperation(f"driver {man.id!r} has no CLI")
        ctx = template_context(self.integration)
        entry = Path(man.source_dir) / man.cli["entry"]
        return [self._python(), str(entry), *[render(a, ctx) for a in man.cli["global_args"]], *args]

    def run_cli(self, *args: str, timeout: float = CLI_TIMEOUT_S, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        from .templating import render
        ctx = template_context(self.integration)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "USER") if k in os.environ}
        env.update({k: render(v, ctx) for k, v in self.integration.manifest.env.items()})
        env.update(extra_env or {})
        return subprocess.run(self.cli_argv(*args), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)

    @staticmethod
    def _json_out(p: subprocess.CompletedProcess) -> dict:
        try:
            data = json.loads(p.stdout)
        except ValueError:
            data = None
        if p.returncode != 0 or not isinstance(data, dict):
            msg = (p.stderr or p.stdout or "").strip().splitlines()
            raise UnsupportedOperation((msg[-1] if msg else f"CLI exited with {p.returncode}")[:300])
        return data

    def fetch(self, kind: str, ident: dict) -> Resource:
        spec = self.integration.manifest.cli.get("fetch") or ["fetch"]
        p = self.run_cli(*spec, "--kind", kind, "--ident", json.dumps(ident))
        d = self._json_out(p)
        return self._resource(kind, ident, title=str(d.get("title") or ""), url=str(d.get("url") or ""),
                              status=str(d.get("status") or ""), status_emoji=str(d.get("status_emoji") or ""),
                              fields=d.get("fields") if isinstance(d.get("fields"), dict) else {})

    def poll(self, *a, **kw):
        raise UnsupportedOperation("package drivers have no poll(); use script watches (manifest `watchers:`)")

    @contextlib.contextmanager
    def _draft_config(self):
        """Settings -> Test of a not-yet-saved instance: CLIs that read `integrations.<name>` from config.yaml
        (via DOP_AGENT_CONFIG) would not find it, so hand them a private (0600, temp dir) one-instance config."""
        it = self.integration
        with tempfile.TemporaryDirectory(prefix="dop-agent-test-") as d:
            path = Path(d) / "config.yaml"
            entry = {"driver": it.manifest.id, "enabled": True, "base_url": it.base_url,
                     "auth": {k: (v.reveal() if hasattr(v, "reveal") else str(v)) for k, v in (it.auth or {}).items() if v is not None}, "options": dict(it.options)}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"integrations": {it.name: entry}}, f)  # JSON is valid YAML
            yield {"DOP_AGENT_CONFIG": str(path)}

    def test_connection(self) -> dict:
        try:
            with self._draft_config() as extra_env:
                p = self.run_cli(*(self.integration.manifest.cli.get("test") or ["test"]), timeout=60, extra_env=extra_env)
            d = self._json_out(p)
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "connection test timed out after 60s"}
        except UnsupportedOperation as exc:
            return {"ok": False, "message": str(exc)}
        d.setdefault("ok", False)
        d.setdefault("message", "ok" if d["ok"] else "failed")
        return d
