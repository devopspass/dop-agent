"""Power BI REST API (api.powerbi.com/v1.0/myorg) with the access token of a signed-in Power BI / Fabric (or Teams)
web tab, read-only from the tab's MSAL localStorage cache over CDP by reusing the ms_teams driver's cli/auth.py
(audience https://analysis.windows.net/powerbi/api). No app registration or admin consent. Never prints tokens.

Assumption (verified by the `test` command): the browser profile has a tab on app.powerbi.com / app.fabric.microsoft.com
(or a Teams tab whose cache already holds a Power BI token) signed in as the configured account.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.powerbi.com/v1.0/myorg"
RESOURCE = "analysis.windows.net/powerbi/api"
_TEAMS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ms_teams", "cli"))
_PBI_HOSTS = ("app.powerbi.com", "app.fabric.microsoft.com", "msit.powerbi.com")
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_state = {"token": None, "exp": 0}
_SECRET = re.compile(r"(?i)(token|password|secret|authorization|bearer)([=:\s]+)[A-Za-z0-9._\-~+/=]{8,}")


class PowerBIError(RuntimeError):
    pass


def redact(s):
    return _SECRET.sub(r"\1\2***", str(s))


def guid(value, what="id"):
    """Validate a GUID before it is placed in a URL path (blocks path/query injection)."""
    if not isinstance(value, str) or not GUID.match(value):
        raise PowerBIError(f"{what} must be a GUID, got {str(value)[:40]!r}")
    return value.lower()


def ws_prefix(workspace):
    """'/groups/<id>' for a workspace GUID, '' for 'me'/empty (My workspace)."""
    if not workspace or workspace.lower() == "me":
        return ""
    return "/groups/" + guid(workspace, "workspace")


def _auth():
    if _TEAMS_DIR not in sys.path:
        sys.path.append(_TEAMS_DIR)
    import auth  # drivers/ms_teams/cli/auth.py
    # Also treat Power BI / Fabric tabs as token sources (the helper only knows Teams tabs by default).
    if not getattr(auth, "_pbi_patched", False):
        orig = auth._is_teams

        def is_tab(tab):
            url = tab.get("url", "")
            return orig(tab) or (tab.get("type") == "page" and any(h in url for h in _PBI_HOSTS))

        auth._is_teams = is_tab
        auth._pbi_patched = True
    return auth


def configure(cdp_url=None, account=None):
    _auth().configure(cdp_url or os.environ.get("POWERBI_CDP_URL"), account or os.environ.get("POWERBI_ACCOUNT"))
    _state.update(token=None, exp=0)


def token():
    if not _state["token"] or _state["exp"] < time.time() + 60:
        a = _auth()
        try:
            t = a.tab_token(RESOURCE, "")
        except RuntimeError as e:
            raise PowerBIError(redact(e) + " (open https://app.powerbi.com in that browser, signed in as the account)")
        c = a.decode_jwt(t)
        _state.update(token=t, exp=c.get("exp", 0), upn=c.get("upn") or c.get("preferred_username") or "",
                      tid=(c.get("tid") or "").lower())
    return _state["token"]


def identity():
    token()
    return _state.get("upn", ""), _state.get("tid", "")


_ALLOWED_HOSTS = ("api.powerbi.com",)


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
        raise PowerBIError("refusing to send the access token to a non-official host; use a relative path or an https URL on: "
                 + ", ".join(_ALLOWED_HOSTS))
    return path


def request(path, method="get", body=None, timeout=60):
    """One API call; returns parsed JSON (or None for an empty body). Errors are redacted and short."""
    url = _checked_url(path)
    hdrs = {"Authorization": "Bearer " + token(), "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        raw = _OPENER.open(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        detail = redact(e.read()[:300].decode("utf-8", "replace"))
        raise PowerBIError(f"HTTP {e.code} {method.upper()} {url.split('?')[0].replace(BASE, '')}: {detail}")
    except urllib.error.URLError as e:
        raise PowerBIError(f"network error: {e.reason}")
    return json.loads(raw) if raw else None


def get_list(path, limit):
    """GET a collection (`value` list), cut to `limit` items."""
    d = request(path) or {}
    return (d.get("value") or [])[:limit]
