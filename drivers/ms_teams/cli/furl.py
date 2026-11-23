"""`fetch` (furl card) and `test` (connection check) subcommands used by dop-agent's package driver.

Both print ONE JSON object to stdout and exit 0 for expected outcomes (a failed `test` is data, not a crash).
Tokens are never printed.

fetch --kind chat|message|meeting --ident '<json from the link regex>'
    -> {title, status?, url, fields:{...}}
test
    -> {ok, message, steps:[{step, ok, detail}], set_options:{tenant_id}}   (CDP reachable -> Teams tab ->
       token (+account match) -> Graph /me answers)
"""
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import auth
import chatsvc
import graph
import http_util

CHAT_URL = "https://teams.microsoft.com/l/chat/{}/0"


def _text(content: str, limit: int = 160) -> str:
    plain = re.sub(r"<[^>]+>", " ", content or "")
    plain = re.sub(r"\s+", " ", html.unescape(plain)).strip()
    return plain[:limit] + ("…" if len(plain) > limit else "")


def _chat_card(chat: str, msg_id: str | None = None) -> dict:
    topic = None
    for c in chatsvc.list_conversations():
        if c.get("id") == chat:
            topic = (c.get("threadProperties") or {}).get("topic")
            break
    msgs = chatsvc.get_messages(chat, 50 if msg_id else 1)
    picked = next((m for m in msgs if str(m.get("id")) == str(msg_id)), None) if msg_id else None
    m = picked or (msgs[0] if msgs else None)
    fields = {}
    if m:
        fields["from"] = m.get("imdisplayname") or ""
        fields["last_message"] = _text(m.get("content", ""))
        fields["time"] = m.get("composetime") or ""
    title = topic or (f"Chat with {fields['from']}" if fields.get("from") else "Teams chat")
    if picked:
        title = f"{fields['from'] or 'Message'}: {fields['last_message']}"
    return {"title": title, "url": CHAT_URL.format(chat), "fields": fields}


def _meeting_card(join: str) -> dict:
    join_key = unquote(join).split("?")[0].rstrip("/")
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=14)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=60)).isoformat().replace("+00:00", "Z")
    for e in graph.calendar_view(start, end, top=200):
        join_url = (e.get("onlineMeeting") or {}).get("joinUrl") or ""
        if join_url and join_key in unquote(join_url):
            return {"title": e.get("subject") or "Teams meeting", "url": join_url,
                    "fields": {"start": (e.get("start") or {}).get("dateTime", ""),
                               "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("name", "")}}
    raise RuntimeError("meeting not found in your calendar (-14d..+60d)")


def fetch(kind: str, ident: dict) -> dict:
    if kind == "chat":
        return _chat_card(unquote(ident["chat"]))
    if kind == "message":
        return _chat_card(unquote(ident["chat"]), ident.get("msg"))
    if kind == "meeting":
        return _meeting_card(ident["join"])
    raise RuntimeError(f"unknown kind {kind!r}")


def run_test() -> dict:
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
        auth._cdp_json("/json/version")
        step("cdp", True, f"CDP endpoint {auth._cdp_url} reachable")
    except Exception as exc:
        step("cdp", False, str(exc))
        return done(f"CDP not reachable: {exc}")
    try:
        tabs = [t for t in auth._cdp_json("/json") if auth._is_teams(t)]
    except Exception as exc:
        step("teams_tab", False, str(exc))
        return done(f"cannot list tabs: {exc}")
    if not step("teams_tab", bool(tabs), f"{len(tabs)} Teams tab(s)" if tabs else "no Teams tab open"):
        return done("No Teams tab found - open https://teams.cloud.microsoft in that browser and sign in")
    try:
        token = auth.tab_token(auth.RESOURCE_GRAPH)
        claims = auth.decode_jwt(token)
        who = (claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name") or "").lower()
        tid = (claims.get("tid") or "").lower()
        step("token", True, f"token for {who or '?'}" + (f", tenant {tid}" if tid else ""))
    except Exception as exc:
        step("token", False, str(exc))
        return done(f"No usable token: {exc}")
    status, data = http_util.call(token, "GET", f"{graph.BASE}/me?$select=displayName,userPrincipalName")
    if not step("graph_me", status == 200, "Graph /me answered" if status == 200 else f"Graph /me HTTP {status}"):
        return done(f"Token present but Graph /me returned HTTP {status}")
    name = data.get("displayName") or who
    return done(f"Signed in as {name} ({data.get('userPrincipalName') or who}), tenant {tid or '?'}",
                set_options={"tenant_id": tid} if tid else {}, tenant_id=tid)
