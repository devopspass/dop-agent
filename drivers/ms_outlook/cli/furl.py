"""`fetch` (furl card) and `test` (connection check) subcommands used by dop-agent's package driver.

Both print ONE JSON object to stdout and exit 0 for expected outcomes (a failed `test` is data, not a crash).
Tokens are never printed.

fetch --kind message|event --ident '<json from the link regex>'
    -> {title, url, fields:{...}}
test
    -> {ok, message, steps:[{step, ok, detail}], set_options:{tenant_id}}   (Python deps -> CDP reachable ->
       Teams tab -> outlook.office.com token (+account match) -> Outlook API /me answers)
"""
import re
import sys
from urllib.parse import unquote

import api
from util import q


def _text(s, limit=160):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def _sender(m):
    ea = (m.get("from") or {}).get("emailAddress") or {}
    return ea.get("name") or ea.get("address") or ""


def _message_card(mid):
    m = api.request(f"/me/messages/{q(mid)}?$select=subject,from,receivedDateTime,bodyPreview,webLink")
    return {"title": m.get("subject") or "(no subject)", "url": m.get("webLink") or "",
            "fields": {"from": _sender(m), "time": m.get("receivedDateTime") or "", "preview": _text(m.get("bodyPreview"))}}


def _event_card(eid):
    e = api.request(f"/me/events/{q(eid)}?$select=subject,start,organizer,location,webLink")
    org = (e.get("organizer") or {}).get("emailAddress") or {}
    return {"title": e.get("subject") or "(no subject)", "url": e.get("webLink") or "",
            "fields": {"start": (e.get("start") or {}).get("dateTime", ""), "organizer": org.get("name") or org.get("address") or "",
                       "location": (e.get("location") or {}).get("displayName", "")}}


def fetch(kind, ident):
    if kind == "message":
        return _message_card(unquote(ident["id"]))
    if kind == "event":
        return _event_card(unquote(ident["id"]))
    raise RuntimeError(f"unknown kind {kind!r}")


def run_test():
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    def done(message, **extra):
        return {"ok": all(s["ok"] for s in steps), "message": message, "steps": steps, **extra}

    if sys.version_info < (3, 10):
        step("python", False, f"Python {sys.version_info[0]}.{sys.version_info[1]} is too old (3.10+ needed)")
        return done("CLI needs Python 3.10+; run it with a newer python3")
    try:
        import websocket  # noqa: F401
        step("python", True, f"Python {sys.version_info[0]}.{sys.version_info[1]} with websocket-client")
    except ImportError:
        step("python", False, f"websocket-client is missing for {sys.executable}: pip install websocket-client")
        return done("websocket-client is not installed for the CLI's Python")
    try:
        auth = api._teams_auth()
    except ImportError as exc:
        step("teams_auth", False, f"ms_teams driver CLI not found at {api._TEAMS_DIR}: {exc}")
        return done("Outlook reuses the ms_teams driver's auth helper, which is missing")
    try:
        auth._cdp_json("/json/version")
        step("cdp", True, f"CDP endpoint {auth._cdp_url} reachable")
        tabs = [t for t in auth._cdp_json("/json") if auth._is_teams(t)]
    except Exception as exc:
        step("cdp", False, str(exc))
        return done(f"CDP not reachable: {exc}")
    if not step("teams_tab", bool(tabs), f"{len(tabs)} Teams tab(s)" if tabs else "no Teams tab open"):
        return done("No Teams tab found - open https://teams.cloud.microsoft in that browser and sign in")
    try:
        token = api.token()
        claims = auth.decode_jwt(token)
        who = (claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name") or "").lower()
        tid = (claims.get("tid") or "").lower()
        step("token", True, f"outlook.office.com token for {who or '?'}" + (f", tenant {tid}" if tid else ""))
    except Exception as exc:
        step("token", False, str(exc))
        return done(f"No usable token: {exc}")
    try:
        me = api.request("/me")
        step("outlook_me", True, "Outlook API /me answered")
    except api.OutlookError as exc:
        step("outlook_me", False, str(exc)[:200])
        return done(f"Token present but the Outlook API failed: {str(exc)[:120]}")
    return done(f"Signed in as {me.get('displayName') or who} ({me.get('emailAddress') or who}), tenant {tid or '?'}",
                set_options={"tenant_id": tid} if tid else {}, tenant_id=tid)
