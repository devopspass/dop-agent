"""Mint an Artifactory identity token by driving a signed-in browser (CDP) through the JFrog UI.

Only for instances with `auth.type: sso_token` (SAML/OIDC-only servers where no token can be created via REST).
Flow: open /ui/login/ (SSO is silent when the browser has a session) -> user profile -> "Generate an Identity
Token" -> read the token from the dialog -> store it (chmod 600, state dir, see afconfig) -> revoke the previous
token of this instance. The token is never printed. Needs `websocket-client` and a browser started with
remote debugging (option `cdp_url`). Identity tokens normally have a fixed lifetime and cannot be refreshed.
"""
from __future__ import annotations

import json
import time
import urllib.parse

import requests

import afconfig
from client import Client

DESCRIPTION = "dop-agent"
_TOKEN_JS = ("(()=>{const a=[...document.querySelectorAll('input,textarea,code,pre,[class*=token]')]"
             ".map(e=>(e.value||e.innerText||'').trim()).filter(x=>/^[A-Za-z0-9_\\-.=]{40,}$/.test(x));"
             "return a.length?JSON.stringify(a):null})()")


class SsoError(RuntimeError):
    pass


def _ws():
    try:
        import websocket
        return websocket
    except ImportError:
        raise SsoError("websocket-client is missing for the CLI's Python: pip install websocket-client") from None


class Tab:
    def __init__(self, cdp_url: str, url: str):
        self.cdp = cdp_url.rstrip("/")
        info = requests.put(f"{self.cdp}/json/new?{urllib.parse.quote(url, safe='')}", timeout=10).json()
        self.id, self.ws = info["id"], info["webSocketDebuggerUrl"]

    def ev(self, js: str):
        ws = _ws().create_connection(self.ws, timeout=30)
        try:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == 1:
                    return r["result"].get("result", {}).get("value")
        finally:
            ws.close()

    def wait(self, js: str, timeout: int = 40):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = self.ev(js)
                if v:
                    return v
            except Exception:
                pass
            time.sleep(1.5)
        raise SsoError(f"timed out waiting for the browser UI ({js[:60]}...)")

    def close(self):
        try:
            requests.get(f"{self.cdp}/json/close/{self.id}", timeout=5)
        except requests.RequestException:
            pass


def revoke(inst: afconfig.Instance, token_id: str) -> int:
    r = requests.delete(f"{inst.base_url}/access/api/v1/tokens/{urllib.parse.quote(token_id, safe='')}",
                        headers=Client(inst).headers(), timeout=20)
    return r.status_code


def authorize(inst: afconfig.Instance, cdp_url: str) -> dict:
    """Mint + store a token; returns {token_id, expiry, revoked_previous}. Raises SsoError."""
    base = inst.base_url
    tab = Tab(cdp_url, f"{base}/ui/login/")
    try:
        tab.wait("(!location.pathname.startsWith('/ui/login') && document.body.innerText.length>50)"
                 " || /SAML|SSO/.test(document.body.innerText)")
        if tab.ev("location.pathname.startsWith('/ui/login')"):
            tab.ev("[...document.querySelectorAll('button,a')].find(b=>/SAML|SSO/.test(b.innerText))?.click()")
            tab.wait("location.pathname.startsWith('/ui/') && !location.pathname.startsWith('/ui/login')", 90)
        tab.ev(f"location.href={json.dumps(base + '/ui/user_profile')}")
        btn = "[...document.querySelectorAll('button')]"
        tab.wait(f"{btn}.some(b=>b.innerText.trim()=='Generate an Identity Token')")
        tab.ev(f"{btn}.find(b=>b.innerText.trim()=='Generate an Identity Token').click()")
        time.sleep(2)
        tab.ev("""(()=>{const i=[...document.querySelectorAll('input,textarea')].filter(i=>i.offsetParent).pop();
          Object.getOwnPropertyDescriptor(i.__proto__,'value').set.call(i,%s);
          i.dispatchEvent(new Event('input',{bubbles:true}));return 1})()""" % json.dumps(DESCRIPTION))
        time.sleep(1)
        tab.ev(f"{btn}.filter(b=>b.offsetParent).find(b=>b.innerText.trim()=='Next').click()")
        toks = json.loads(tab.wait(_TOKEN_JS, 20))
    finally:
        tab.close()
    toks.sort(key=lambda t: not t.startswith("cmVm"))
    old = afconfig.load_state(inst.name)
    new = afconfig.Instance(inst.name, base, "sso_token", toks[0], inst.options)
    info = Client(new).token_info()
    if not info or not info.get("valid"):
        raise SsoError("the minted token was rejected by the server; not stored")
    afconfig.save_state(inst.name, {"token": toks[0], "token_id": info.get("token_id", ""),
                                    "expiry": info.get("expiry"), "created": time.strftime("%Y-%m-%d")})
    revoked = False
    if old.get("token_id") and old["token_id"] != info.get("token_id") and old.get("token"):
        revoked = revoke(afconfig.Instance(inst.name, base, "sso_token", old["token"], inst.options),
                         old["token_id"]) in (200, 204)
    return {"token_id": info.get("token_id", ""), "expiry": info.get("expiry"), "revoked_previous": revoked}
