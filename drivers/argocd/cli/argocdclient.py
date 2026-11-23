"""Thin, injection-safe wrapper around the `argocd` binary (argv list only, never a shell).

    ArgoClient(base_url, options).json(["app", "list"])   -> parsed `-o json` output
    ArgoClient(...).run(["app", "diff", name])            -> (rc, stdout, stderr), secrets redacted
Every user-supplied name/revision is validated first (`valid_name`, `valid_rev`, ...): they cannot start with `-`,
so they can never be parsed as an option. The server is derived from the instance base_url; auth is whatever login
session the `argocd` CLI already has for that server (this module never touches tokens or the session file).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse

TIMEOUT_S = 120
SECRET = re.compile(r"(token|password|secret|authorization)([=:]\s*)(?:(?:bearer|basic)\s+)?\S+|(bearer\s+)\S+", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
NS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
REV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@~^+-]{0,200}$")
SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/=!,() -]{0,200}$")


class ArgoError(RuntimeError):
    pass


def redact(text: str) -> str:
    return SECRET.sub(lambda m: (m.group(1) + m.group(2) if m.group(1) else m.group(3)) + "***", text or "")


def _check(rx, value, what):
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise ArgoError(f"invalid {what}: {str(value)[:60]!r}")
    return value


def valid_name(v, what="name"):
    return _check(NAME_RE, v, what)


def valid_ns(v):
    return _check(NS_RE, v, "namespace")


def valid_rev(v):
    return _check(REV_RE, v, "revision")


def valid_selector(v):
    return _check(SELECTOR_RE, v, "selector")


def find_binary(name):
    """The bot (launchd) has a short PATH: also look in the usual install dirs; fall back to the bare name."""
    if "/" in name:
        return name
    extra = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", str(os.path.expanduser("~/.local/bin"))])
    return shutil.which(name, path=os.environ.get("PATH", "") + os.pathsep + extra) or name


class ArgoClient:
    def __init__(self, base_url, options=None, runner=None):
        o = options or {}
        u = urllib.parse.urlparse(base_url)
        self.base_url = base_url.rstrip("/")
        self.host = u.netloc
        self.plaintext = u.scheme == "http"
        self.insecure = bool(o.get("insecure"))
        self.binary = find_binary(str(o.get("argocd_bin") or "argocd"))
        self.project = str(o.get("project") or "")
        self.selector = str(o.get("app_selector") or "")
        self._runner = runner or subprocess.run

    def _argv(self, args):
        argv = [self.binary, *args, "--server", self.host, "--grpc-web"]
        if self.plaintext:
            argv.append("--plaintext")
        if self.insecure:
            argv.append("--insecure")
        return argv

    def run(self, args, timeout=TIMEOUT_S):
        try:
            p = self._runner(self._argv(args), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise ArgoError(f"argocd binary {self.binary!r} not found on PATH (install argocd CLI or set option argocd_bin)") from None
        except subprocess.TimeoutExpired:
            raise ArgoError(f"argocd {' '.join(args[:2])} timed out after {timeout}s") from None
        return p.returncode, redact(p.stdout or ""), redact(p.stderr or "")

    def ok(self, args, timeout=TIMEOUT_S):
        rc, out, err = self.run(args, timeout)
        if rc != 0:
            msg = (err or out).strip().splitlines()
            hint = " (run `argocd login <host> --sso` again)" if re.search(r"unauthenticated|token is expired|login|permission denied", err or "", re.I) else ""
            raise ArgoError(f"argocd {' '.join(args[:2])} failed (rc={rc}): {(msg[-1] if msg else '')[:300]}{hint}")
        return out

    def json(self, args, timeout=TIMEOUT_S):
        out = self.ok([*args, "-o", "json"], timeout)
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            raise ArgoError(f"argocd {' '.join(args[:2])}: output is not JSON") from None
