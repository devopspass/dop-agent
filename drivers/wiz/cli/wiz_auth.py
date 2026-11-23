"""Wiz auth/GraphQL transport shared by the CLI, the watcher and furl/test.

Read-only GraphQL calls use the OAuth bearer token that `wizcli` maintains in `<wiz_dir>/auth.json`
(fields used: `access_token`, `data_center`, e.g. "eu4" -> API host `api.eu4.app.wiz.io`). This module does not
re-implement the OAuth flow: log in once with `WIZ_DIR=<wiz_dir> wizcli auth --use-device-code` (or a service
account: auth.type service_account, see wiz_config.py) and it shells out to `wizcli auth --print-token` to refresh when needed.

One instance = one `wiz_dir` (wizcli honours the WIZ_DIR env var), so several Wiz tenants can be logged in side by
side. Call `configure()` before anything else.

Concurrent refreshes (parallel watchers, threads) are serialized with an flock on `<wiz_dir>/.refresh.lock`:
overlapping `wizcli auth --print-token` runs were observed to corrupt auth.json. Tokens are never printed.

Note: a findings query reflects the last *completed* scan, which can lag reality by hours even after a rescan
reported success (see wiz_rescan.py).
"""
import fcntl
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

GRAPHQL_URL_TMPL = "https://api.{dc}.app.wiz.io/graphql"
DEFAULT_DATA_CENTER = "eu1"
AUTO = "auto"

_wiz_dir = Path(os.environ.get("WIZ_DIR") or "~/.wiz").expanduser()
_dc_override = None
_creds = None  # (client_id, client_secret) of a service-account instance; only ever passed to wizcli's env


class WizAuthError(RuntimeError):
    pass


def configure(wiz_dir=None, data_center=None):
    """Set the wizcli directory (default env WIZ_DIR, else ~/.wiz) and an optional data-center override."""
    global _wiz_dir, _dc_override, _creds
    _creds = None
    _wiz_dir = Path(wiz_dir or os.environ.get("WIZ_DIR") or "~/.wiz").expanduser()
    dc = (data_center or os.environ.get("WIZ_DATA_CENTER") or "").strip().lower()
    _dc_override = None if dc in ("", AUTO) else dc


def configure_instance(name=None):
    """Resolve service-account credentials from config.yaml for the instance (no-op for interactive wizcli login)."""
    global _creds
    import wiz_config
    try:
        _, _, cid, secret = wiz_config.load_credentials(name)
    except wiz_config.InstanceError as exc:
        raise WizAuthError(str(exc)) from None
    _creds = (cid, secret) if cid else None


def auth_path():
    return _wiz_dir / "auth.json"


def _read_auth_file():
    if not auth_path().exists():
        raise WizAuthError(f"{auth_path()} not found. Log in: WIZ_DIR={_wiz_dir} wizcli auth --use-device-code")
    try:
        return json.loads(auth_path().read_text())
    except ValueError as exc:
        raise WizAuthError(f"{auth_path()} is not valid JSON ({exc})") from exc


def refresh_token(timeout=30):
    """`wizcli auth --print-token` under the file lock. The token itself is re-read from auth.json afterwards."""
    _wiz_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WIZ_DIR": str(_wiz_dir)}
    if _creds:
        env["WIZ_CLIENT_ID"], env["WIZ_CLIENT_SECRET"] = _creds
    with open(_wiz_dir / ".refresh.lock", "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            r = subprocess.run(["wizcli", "auth", "--print-token"], capture_output=True, text=True,
                               timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise WizAuthError("wizcli is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise WizAuthError(f"wizcli auth --print-token timed out after {timeout}s") from exc
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
    if r.returncode != 0:
        raise WizAuthError(f"wizcli auth --print-token failed (exit {r.returncode}): "
                           f"{(r.stderr.strip() or r.stdout.strip())[:200]}")


def load_token(auto_refresh=True):
    """(access_token, data_center). With auto_refresh wizcli decides whether a real refresh is needed."""
    if auto_refresh:
        refresh_token()
    auth = _read_auth_file()
    token = auth.get("access_token")
    if not token:
        raise WizAuthError(f"no access_token in {auth_path()}")
    return token, _dc_override or auth.get("data_center") or DEFAULT_DATA_CENTER


def graphql_url(dc=None):
    return GRAPHQL_URL_TMPL.format(dc=dc or DEFAULT_DATA_CENTER)


def current_graphql_url():
    """GraphQL host of this instance (also used by the cookie/CDP transport in wiz_rescan.py)."""
    if _dc_override:
        return graphql_url(_dc_override)
    try:
        return graphql_url(_read_auth_file().get("data_center"))
    except WizAuthError:
        return graphql_url(None)


def graphql(query, variables, token=None, dc=None, retry_on_401=True):
    """POST a query; on 401 refresh once and retry. HTTP 400 schema errors come back as the {"errors": [...]} body
    exactly like a 200-with-errors reply, so callers only check `result.get("errors")`."""
    if token is None or dc is None:
        token, dc = load_token(auto_refresh=False)
    req = urllib.request.Request(
        graphql_url(dc), data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        if e.code == 401 and retry_on_401:
            refresh_token()
            token, dc = load_token(auto_refresh=False)
            return graphql(query, variables, token=token, dc=dc, retry_on_401=False)
        try:
            return json.loads(raw)
        except ValueError:
            raise WizAuthError(f"Wiz GraphQL HTTP {e.code}: {raw[:200]}") from e


def auth_status():
    """Auth health as a dict; never raises (it is the diagnostic entry point)."""
    st = {"auth_file": str(auth_path()), "auth_file_exists": auth_path().exists()}
    if not st["auth_file_exists"]:
        return {**st, "ok": False, "message": f"Not logged in. Run: WIZ_DIR={_wiz_dir} wizcli auth --use-device-code"}
    try:
        t0 = time.time()
        refresh_token()
        token, dc = load_token(auto_refresh=False)
        st["data_center"] = dc
        st["refresh_seconds"] = round(time.time() - t0, 2)
        probe = graphql("query { vulnerabilityFindings(first: 1) { totalCount } }", {}, token=token, dc=dc)
    except (WizAuthError, urllib.error.URLError, OSError) as exc:
        return {**st, "ok": False, "message": str(exc)[:300]}
    if probe.get("errors"):
        return {**st, "ok": False, "message": f"GraphQL probe failed: {json.dumps(probe['errors'])[:300]}"}
    return {**st, "ok": True, "message": "Token valid, GraphQL reachable."}
