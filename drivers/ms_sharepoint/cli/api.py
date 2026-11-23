"""Microsoft Graph client for SharePoint/OneDrive files, using the access token the signed-in Teams web tab
already holds (audience graph.microsoft.com; Files.* / Sites.* scopes). The token is read from the tab's MSAL
cache via CDP by reusing the ms_teams driver's cli/auth.py. No app registration. Tokens are never printed.

Also holds the item addressing helpers: a *target* is one of
  https://... sharing/web URL   -> /shares/u!<b64>/driveItem
  f3                            -> short ref from the last list/search (cache file, see util.py)
  <driveId>:<itemId>            -> /drives/<driveId>/items/<itemId>
  /Some/Path or Some/Path       -> path inside the OneDrive, or the --site / --drive library
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = "https://graph.microsoft.com/v1.0"
MAX_DOWNLOAD = 50 * 1024 * 1024
_TEAMS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ms_teams", "cli"))
_state = {"token": None, "exp": 0}
_SECRET = re.compile(r"(?i)(token|password|secret|authorization|bearer)([=:\s]+)(?:Bearer\s+)?\S+")


class GraphError(RuntimeError):
    pass


def redact(s):
    return _SECRET.sub(r"\1\2***", str(s))


def _teams_auth():
    if _TEAMS_DIR not in sys.path:
        sys.path.append(_TEAMS_DIR)
    import auth  # drivers/ms_teams/cli/auth.py
    return auth


def configure(cdp_url=None, account=None):
    _teams_auth().configure(cdp_url or os.environ.get("SHAREPOINT_CDP_URL"),
                            account or os.environ.get("SHAREPOINT_ACCOUNT"))
    _state.update(token=None, exp=0)


def token():
    """Unexpired Graph token with Files scopes (falls back to any Graph token); renewed via the Teams tab."""
    if not _state["token"] or _state["exp"] < time.time() + 60:
        a = _teams_auth()
        try:
            try:
                t = a.tab_token(a.RESOURCE_GRAPH, "Files")
            except RuntimeError:
                t = a.tab_token(a.RESOURCE_GRAPH)
        except RuntimeError as e:
            raise GraphError(redact(e))
        c = a.decode_jwt(t)
        _state.update(token=t, exp=c.get("exp", 0), upn=c.get("upn") or c.get("preferred_username") or "",
                      tid=c.get("tid") or "", scp=c.get("scp") or "")
    return _state["token"]


def claims():
    token()
    return {"upn": _state.get("upn", ""), "tid": _state.get("tid", ""), "scp": _state.get("scp", "")}


def request(path, method="GET", body=None, data=None, headers=None, absolute=False):
    """One Graph call -> parsed JSON (None for empty). `body` is JSON-encoded; `data` raw bytes."""
    url = path if absolute or path.startswith("http") else BASE + path
    if url.startswith("http") and not url.startswith("https://graph.microsoft.com/"):
        raise GraphError("refusing to send the Graph token to a non-Graph URL")  # nextLink/deltaLink must stay on Graph
    hdrs = {"Authorization": "Bearer " + token(), "Accept": "application/json"}
    payload = data
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=payload, headers=hdrs, method=method.upper())
    try:
        raw = urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        detail = redact(e.read()[:300].decode("utf-8", "replace"))
        raise GraphError(f"HTTP {e.code} {method.upper()} {url.split('?')[0].replace(BASE, '')}: {detail}")
    except urllib.error.URLError as e:
        raise GraphError(f"network error: {e.reason}")
    return json.loads(raw) if raw else None


def get_all(path, limit):
    """GET with @odata.nextLink paging until `limit` items."""
    items, nxt = [], path
    while nxt and len(items) < limit:
        d = request(nxt) or {}
        items += d.get("value", [])
        nxt = d.get("@odata.nextLink")
    return items[:limit]


def _fetch_url(url, headers=None, method="GET", data=None, limit=None):
    """Pre-authenticated URL (download/upload URL from Graph): never sends the bearer token."""
    if not url.startswith("https://"):
        raise GraphError("refusing non-https download/upload URL")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return resp.read(limit + 1) if limit else resp.read()
    except urllib.error.HTTPError as e:
        raise GraphError(f"HTTP {e.code} on pre-authenticated URL")
    except urllib.error.URLError as e:
        raise GraphError(f"network error: {e.reason}")


def download(item_path, limit=MAX_DOWNLOAD):
    """File bytes via the item's short-lived downloadUrl; refuses files bigger than `limit`."""
    meta = request(item_path + "?$select=name,size,file,folder,@microsoft.graph.downloadUrl")
    if "folder" in meta:
        raise GraphError(f"{meta.get('name')} is a folder")
    if int(meta.get("size") or 0) > limit:
        raise GraphError(f"{meta.get('name')} is {meta.get('size')} bytes, over the {limit} byte limit")
    url = meta.get("@microsoft.graph.downloadUrl")
    if not url:
        raise GraphError("no download URL for this item (no read permission?)")
    data = _fetch_url(url, limit=limit)
    if len(data) > limit:
        raise GraphError(f"download exceeds {limit} bytes")
    return meta.get("name") or "file", data


# ---------- addressing ----------

SITE_RE = re.compile(r"^(?:https://)?(?P<host>[A-Za-z0-9\-]+\.sharepoint\.com)(?P<path>/(?:sites|teams)/[A-Za-z0-9._\-]+)?/?$")
SITE_ID_RE = re.compile(r"^[A-Za-z0-9\-]+\.sharepoint\.com,[0-9a-fA-F\-]{36},[0-9a-fA-F\-]{36}$")
DRIVE_RE = re.compile(r"^b![A-Za-z0-9_\-]+$")
ITEM_RE = re.compile(r"^(b![A-Za-z0-9_\-]+):([A-Za-z0-9!_\-]+)$")


def site_path(site):
    """'--site' value -> Graph site resource. Accepts a site URL, host[:/sites/Name] or a full site id."""
    s = site.strip()
    if SITE_ID_RE.match(s):
        return "/sites/" + s
    m = SITE_RE.match(s) or SITE_RE.match(s.split("/Shared%20Documents")[0].split("/Forms")[0])
    if not m:
        raise GraphError(f"bad --site {site!r}: use https://<tenant>.sharepoint.com/sites/<Name> or a site id")
    return f"/sites/{m['host']}:{m['path']}:" if m["path"] else f"/sites/{m['host']}"


def drive_base(site=None, drive=None):
    """Resource prefix of the library to work in: --drive id > --site default library > own OneDrive."""
    if drive:
        if not DRIVE_RE.match(drive):
            raise GraphError(f"bad --drive {drive!r} (expected an id like b!abc...)")
        return "/drives/" + drive
    if site:
        sp = site_path(site)
        return sp + "/drive"
    return "/me/drive"


def encode_share_url(url):
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def item_path(target, base="/me/drive", refs=None):
    """Graph resource path of the driveItem a target points to (see module docstring)."""
    t = target.strip()
    if t.startswith("https://"):
        return f"/shares/{encode_share_url(t)}/driveItem"
    if refs is not None and re.fullmatch(r"f\d{1,3}", t):
        if t not in refs:
            raise GraphError(f"unknown ref {t}: run ls/search first")
        t = refs[t]
    m = ITEM_RE.match(t)
    if m:
        return f"/drives/{m[1]}/items/{m[2]}"
    if t in ("", "/", "root"):
        return base + "/root"
    p = quote("/" + t.strip("/"), safe="/")
    return f"{base}/root:{p}:"


def children_path(item):
    return item + "/children"
