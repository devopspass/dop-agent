"""
Token acquisition for the Teams CLI.

Tokens come from the MSAL cache of a signed-in Teams web tab (Chrome/Edge
with remote debugging; endpoint via auth.configure / TEAMS_CDP_URL), read over CDP localStorage.

Flow: read the cache; if no unexpired token matches, reload the existing Teams
tab (or open a temporary background tab, then close it) so MSAL renews tokens,
and read again. Nothing here activates/focuses a tab or window.

Never print token values. Callers must keep them in variables only.
"""
import base64
import json
import os
import time
import urllib.request

RESOURCE_GRAPH = "graph.microsoft.com"
RESOURCE_IC3 = "ic3.teams.office.com"
RESOURCE_SKYPE = "api.spaces.skype.com"

_CDP_URL_DEFAULT = "http://localhost:9222"
_cdp_url = _CDP_URL_DEFAULT
_account = None
_tokens = {}
_TEAMS_URL = "https://teams.cloud.microsoft/"


def decode_jwt(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def configure(cdp_url=None, account=None):
    """Set the CDP endpoint and optional account (UPN/email) selector."""
    global _cdp_url, _account
    _cdp_url = (cdp_url or os.environ.get("TEAMS_CDP_URL") or _CDP_URL_DEFAULT).rstrip("/")
    if "://" not in _cdp_url:
        _cdp_url = "http://" + _cdp_url
    _account = (account or os.environ.get("TEAMS_ACCOUNT") or "").strip().lower() or None
    _tokens.clear()


def _cdp_json(path):
    try:
        return json.loads(urllib.request.urlopen(f"{_cdp_url}{path}", timeout=5).read())
    except Exception as exc:
        raise RuntimeError(f"CDP endpoint {_cdp_url} is not available ({exc}); start the browser with "
                           "--remote-debugging-port or pass --cdp-url / TEAMS_CDP_URL")


class _Cdp:
    """Minimal synchronous CDP client over one websocket."""

    def __init__(self, ws_url):
        import websocket  # lazy import, only needed for the CDP path
        self.ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error'].get('message')}")
                return msg.get("result", {})

    def eval(self, expression):
        return self.call("Runtime.evaluate", expression=expression, returnByValue=True) \
            .get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _is_teams(tab):
    url = tab.get("url", "")
    return tab.get("type") == "page" and ("teams.cloud.microsoft" in url or "teams.microsoft.com" in url)


_EXPR = """
  (() => {
    const tokens = [], accounts = {};
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      try {
        const v = JSON.parse(localStorage.getItem(k));
        if (v && v.credentialType === 'AccessToken') tokens.push(v);
        else if (v && v.username && v.homeAccountId) accounts[v.homeAccountId] = v.username;
      } catch (e) {}
    }
    return JSON.stringify({tokens, accounts});
  })()
"""



def _read_cache(ws_url):
    c = _Cdp(ws_url)
    try:
        raw = c.eval(_EXPR)
    finally:
        c.close()
    return json.loads(raw) if raw else {"tokens": [], "accounts": {}}


def _identities(entry, accounts):
    ids = set()
    u = accounts.get(entry.get("homeAccountId"))
    if u:
        ids.add(u.lower())
    try:
        c = decode_jwt(entry["secret"])
        for k in ("upn", "preferred_username", "unique_name", "email"):
            if c.get(k):
                ids.add(str(c[k]).lower())
    except Exception:
        pass
    return ids


def _pick(caches, resource, scope_hint):
    now = time.time()
    best, seen = None, set()
    for data in caches:
        for e in data["tokens"]:
            ids = _identities(e, data["accounts"])
            seen |= ids
            if _account and _account not in ids:
                continue
            if (resource in e.get("target", "") and scope_hint in e.get("target", "")
                    and int(e.get("expiresOn", 0)) > now
                    and (best is None or int(e["expiresOn"]) > int(best["expiresOn"]))):
                best = e
    return best, seen


def _wait_fresh(ws_url, resource, scope_hint, timeout=40):
    """Poll one tab's cache until a matching unexpired token shows up."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2)
        try:
            data = _read_cache(ws_url)
        except Exception:
            continue
        if _pick([data], resource, scope_hint)[0]:
            return True
    return False


def _refresh(tabs, resource, scope_hint):
    """Reload existing Teams tabs, or open+close a background tab if none exist."""
    if tabs:
        for tab in tabs:
            c = _Cdp(tab["webSocketDebuggerUrl"])
            try:
                c.call("Page.reload", ignoreCache=False)
            finally:
                c.close()
            if _wait_fresh(tab["webSocketDebuggerUrl"], resource, scope_hint):
                return
        return
    browser = _Cdp(_cdp_json("/json/version")["webSocketDebuggerUrl"])
    target = None
    try:
        # background=True: the tab is created without being activated, so the browser does not steal focus
        target = browser.call("Target.createTarget", url=_TEAMS_URL, background=True)["targetId"]
        ws_url = None
        for _ in range(10):
            for t in _cdp_json("/json"):
                if t.get("id") == target:
                    ws_url = t["webSocketDebuggerUrl"]
            if ws_url:
                break
            time.sleep(0.5)
        if ws_url:
            _wait_fresh(ws_url, resource, scope_hint, timeout=60)
    finally:
        if target:
            try:
                browser.call("Target.closeTarget", targetId=target)
            except Exception:
                pass
        browser.close()


def tab_token(resource: str, scope_hint: str = "") -> str:
    """Return an unexpired access token for `resource` (a host such as
    graph.microsoft.com) from the Teams MSAL localStorage cache via CDP.
    If the cached token is expired/missing, the Teams tab is reloaded (or a
    temporary background tab is opened and closed) to make MSAL renew it."""
    key = (resource, scope_hint, _cdp_url, _account)
    if key in _tokens and _tokens[key][1] > time.time() + 60:
        return _tokens[key][0]
    tabs = [t for t in _cdp_json("/json") if _is_teams(t)]  # raises if CDP is unavailable
    caches = [_read_cache(t["webSocketDebuggerUrl"]) for t in tabs]
    best, seen = _pick(caches, resource, scope_hint)
    known = set(seen)
    for d in caches:
        known |= {u.lower() for u in d["accounts"].values()}
    if _account and tabs and _account not in known:
        raise RuntimeError(f"Account {_account!r} not signed in at {_cdp_url}. Accounts seen: "
                           f"{sorted(known) or 'none'}. Teams tabs: "
                           + "; ".join(f"{t.get('title', '')!r}" for t in tabs))
    if not best:
        _refresh(tabs, resource, scope_hint)
        tabs = [t for t in _cdp_json("/json") if _is_teams(t)]
        caches = [_read_cache(t["webSocketDebuggerUrl"]) for t in tabs]
        best, seen = _pick(caches, resource, scope_hint)
    if best:
        _tokens[key] = (best["secret"], int(best["expiresOn"]))
        return best["secret"]
    avail = "; ".join(f"{t.get('title', '')!r} {t.get('url', '')[:60]}" for t in tabs) or "none"
    acct = f" for account {_account!r}" if _account else ""
    raise RuntimeError(f"No unexpired token for '{resource}' {scope_hint}{acct} at {_cdp_url} even after "
                       f"refreshing (user may need to sign in). Accounts seen: {sorted(seen) or 'none'}. "
                       f"Teams tabs: {avail}")
