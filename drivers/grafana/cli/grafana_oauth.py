"""OAuth 2.0 (authorization code + PKCE, dynamic client registration) for Grafana behind an OAuth proxy.

Used only by instances with `auth.type: oauth_cdp`: the proxy at option `idp_url` mints its own tokens (a raw
Grafana session cookie / API key is not accepted there). Flow: discover endpoints (RFC 8414
`/.well-known/oauth-authorization-server`) -> register a fresh public client (RFC 7591) -> authorization code +
PKCE through a CDP-debuggable browser that is already signed in (the SSO leg is silent, the proxy's consent page
needs one "Authorize" click, done via injected JS) -> token exchange. Afterwards `refresh_token` renews the
access token silently (the refresh token rotates; always persist the newest one).

State per instance: `grafana_config.state_dir()/<instance>.json` (client_id, access/refresh token, expiry), chmod 600,
written atomically. Tokens are never printed.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse

import grafana_config as grconfig

CONSENT_TIMEOUT_S = 60
EXPIRY_MARGIN_S = 300
_BUTTON_JS = """(() => { const b = Array.from(document.querySelectorAll('button, input[type=submit]'))
  .find(x => /^(authorize|allow|accept)$/i.test((x.textContent || x.value || '').trim()));
  if (b) { b.click(); return true; } return false; })()"""


class OAuthError(RuntimeError):
    pass


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def state_path(instance: str):
    return grconfig.state_dir() / f"{_safe_name(instance)}.json"


def load_state(instance: str) -> dict:
    try:
        return json.loads(state_path(instance).read_text())
    except (OSError, ValueError):
        return {}


def save_state(instance: str, state: dict) -> None:
    path = state_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".st.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def discover(idp_url: str) -> dict:
    import requests
    idp = idp_url.rstrip("/")
    try:
        r = requests.get(f"{idp}/.well-known/oauth-authorization-server", timeout=10)
        r.raise_for_status()
        meta = r.json()
        return {k: meta[k] for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")}
    except Exception as exc:
        raise OAuthError(f"OAuth discovery failed at {idp} ({type(exc).__name__}): check option idp_url") from None


def _pkce():
    verifier = secrets.token_urlsafe(64)[:64]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Redirect(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        _Redirect.result = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authorization complete. You can close this tab.")

    def log_message(self, *_a):
        pass


def _drive_browser(cdp_url: str, auth_url: str, port: int) -> dict:
    import requests
    import websocket
    cdp = cdp_url.rstrip("/")
    server = http.server.HTTPServer(("127.0.0.1", port), _Redirect)
    server.timeout = CONSENT_TIMEOUT_S
    _Redirect.result = {}
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        tab = requests.put(f"{cdp}/json/new?{urllib.parse.quote(auth_url, safe='')}", timeout=10).json()
    except Exception as exc:
        server.server_close()
        raise OAuthError(f"cannot open a tab via CDP at {cdp} ({type(exc).__name__}): start the browser with remote debugging") from None
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        deadline, msg_id, clicked = time.time() + CONSENT_TIMEOUT_S, 1, False
        while time.time() < deadline and not _Redirect.result:
            if not clicked:
                ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {"expression": _BUTTON_JS}}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("id") == msg_id:
                        clicked = bool(m.get("result", {}).get("result", {}).get("value"))
                        break
                msg_id += 1
            time.sleep(0.5)
        t.join(timeout=3)
    finally:
        ws.close()
        try:
            requests.put(f"{cdp}/json/close/{tab['id']}", timeout=5)
        except Exception:
            pass
        server.server_close()
    if "code" not in _Redirect.result:
        raise OAuthError(f"no authorization code within {CONSENT_TIMEOUT_S}s: is the browser at {cdp} signed in to the SSO?")
    return _Redirect.result


def authorize(inst: grconfig.Instance) -> dict:
    import requests
    idp = inst.options["idp_url"].rstrip("/")
    cdp_url = inst.options.get("cdp_url") or "http://localhost:9222"
    ep = discover(idp)
    reg = requests.post(ep["registration_endpoint"], timeout=10, json={
        "client_name": "dop-agent-grafana", "redirect_uris": ["http://127.0.0.1:0/"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"]})
    reg.raise_for_status()
    client_id = reg.json()["client_id"]
    probe = http.server.HTTPServer(("127.0.0.1", 0), _Redirect)
    port = probe.server_address[1]
    probe.server_close()
    redirect = f"http://127.0.0.1:{port}/"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": state, "resource": idp + "/"}
    got = _drive_browser(cdp_url, f"{ep['authorization_endpoint']}?{urllib.parse.urlencode(params)}", port)
    if got.get("state") != state:
        raise OAuthError("OAuth state mismatch (another tab/flow interfered)")
    r = requests.post(ep["token_endpoint"], timeout=10, data={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier})
    r.raise_for_status()
    return _store(inst.name, {"client_id": client_id}, r.json())


def _store(instance: str, prev: dict, tokens: dict) -> dict:
    st = {"client_id": prev["client_id"], "access_token": tokens["access_token"],
          "refresh_token": tokens.get("refresh_token") or prev.get("refresh_token", ""),
          "expires_at": time.time() + float(tokens.get("expires_in", 0))}
    save_state(instance, st)
    return st


def refresh(inst: grconfig.Instance) -> dict:
    import requests
    st = load_state(inst.name)
    if not st.get("refresh_token") or not st.get("client_id"):
        raise OAuthError(f"no saved session for {inst.name!r}: run `auth authorize` first")
    ep = discover(inst.options["idp_url"])
    r = requests.post(ep["token_endpoint"], timeout=10, data={
        "grant_type": "refresh_token", "refresh_token": st["refresh_token"], "client_id": st["client_id"]})
    if r.status_code in (400, 401):
        raise OAuthError("refresh token rejected (revoked/expired): run `auth authorize` again")
    r.raise_for_status()
    return _store(inst.name, st, r.json())


def access_token(inst: grconfig.Instance) -> str:
    st = load_state(inst.name)
    if st.get("access_token") and st.get("expires_at", 0) - EXPIRY_MARGIN_S > time.time():
        return st["access_token"]
    return refresh(inst)["access_token"]
