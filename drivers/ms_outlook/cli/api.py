"""Outlook REST API (outlook.office.com/api/v2.0) with the access token the signed-in Teams web tab already
holds (audience https://outlook.office.com/, scopes Mail.ReadWrite, Mail.Send, Calendars.ReadWrite,
Contacts.ReadWrite). Tokens are read read-only from the tab's MSAL cache via CDP by reusing the ms_teams driver's
cli/auth.py. Needs no app registration or admin consent. Never prints tokens.

v2.0 returns PascalCase JSON; responses are converted to Graph-style camelCase so mail.py/cal.py use Graph names.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://outlook.office.com/api/v2.0"
ROOT = os.path.join(os.path.expanduser("~"), ".outlook-cli")  # short-ref cache (m1, e1, ...)
_TEAMS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ms_teams", "cli"))
_state = {"token": None, "exp": 0}


class OutlookError(RuntimeError):
    pass


def _teams_auth():
    if _TEAMS_DIR not in sys.path:
        sys.path.append(_TEAMS_DIR)
    import auth  # drivers/ms_teams/cli/auth.py
    return auth


def configure(cdp_url=None, account=None):
    _teams_auth().configure(cdp_url or os.environ.get("OUTLOOK_CDP_URL"), account or os.environ.get("OUTLOOK_ACCOUNT"))
    _state.update(token=None, exp=0)


def token():
    """Unexpired outlook.office.com token; the Teams auth helper reloads/opens a Teams tab if it expired."""
    if not _state["token"] or _state["exp"] < time.time() + 60:
        a = _teams_auth()
        try:
            t = a.tab_token("https://outlook.office.com/", "Mail.ReadWrite")
        except RuntimeError as e:
            raise OutlookError(str(e))
        c = a.decode_jwt(t)
        _state.update(token=t, exp=c.get("exp", 0), upn=c.get("upn") or c.get("preferred_username") or "")
    return _state["token"]


def identity():
    token()
    return _state.get("upn", "")


def identities():
    a = _teams_auth()
    out = set()
    for tab in [t for t in a._cdp_json("/json") if a._is_teams(t)]:
        data = a._read_cache(tab["webSocketDebuggerUrl"])
        for e in data["tokens"]:
            out |= a._identities(e, data["accounts"])
        out |= {u.lower() for u in data["accounts"].values()}
    return sorted(out)


def _camel(o):
    if isinstance(o, dict):
        return {(k if k.startswith("@") or not k else k[0].lower() + k[1:]): _camel(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_camel(x) for x in o]
    return o


def _pascal(o):
    if isinstance(o, dict):
        return {k[:1].upper() + k[1:]: _pascal(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_pascal(x) for x in o]
    return o


_ALLOWED_HOSTS = ("outlook.office.com", "graph.microsoft.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the bearer token must not be forwarded to another host."""
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _checked_url(path):
    """Relative paths go to BASE; absolute URLs must be https on an official host (token is attached)."""
    if not path.startswith(("http://", "https://")):
        return BASE + path
    parts = urllib.parse.urlsplit(path)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise OutlookError("refusing to send the access token to a non-official host; use a relative path or an https URL on: "
                 + ", ".join(_ALLOWED_HOSTS))
    return path


def request(path, method="get", body=None, file_path=None):
    url = _checked_url(path)
    value_suffix = file_path is not None and url.endswith("/$value")
    if value_suffix:
        url = url[:-len("/$value")]
    hdrs = {"Authorization": "Bearer " + token(), "Accept": "application/json",
            "Prefer": 'outlook.timezone="UTC", outlook.body-content-type="text"'}
    data = None
    if body is not None:
        data = json.dumps(_pascal(body)).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        raw = _OPENER.open(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise OutlookError(f"HTTP {e.code} {method.upper()} {url.split('?')[0].replace(BASE, '')}: {detail}")
    except urllib.error.URLError as e:
        raise OutlookError(f"network error: {e.reason}")
    d = _camel(json.loads(raw)) if raw else None
    if value_suffix:
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(d["contentBytes"]))
        return None
    return d


def get_all(path, limit):
    """GET with @odata.nextLink paging until `limit` items."""
    items, nxt = [], path
    while nxt and len(items) < limit:
        d = request(nxt) or {}
        items += d.get("value", [])
        nxt = d.get("@odata.nextLink")
    return items[:limit]
