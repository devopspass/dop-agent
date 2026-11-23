"""`fetch` (furl card) and `test` (connection check) subcommands used by dop-agent's package driver.

Both print ONE JSON object to stdout and exit 0 for expected outcomes (a failed `test` is data, not a crash).
Tokens are never printed.

fetch --kind file --ident '{"url": "<sharepoint url>"}'
    -> {title, url, fields:{type, modified, size}}
test
    -> {ok, message, steps:[{step, ok, detail}], set_options:{tenant_id}}   (Python deps -> CDP reachable ->
       Teams tab -> Graph token (+account match, file scopes) -> /me answers -> own OneDrive root readable)
"""
import sys

import api
import util


def fetch(kind, ident):
    if kind != "file":
        raise RuntimeError(f"unknown kind {kind!r}")
    m = api.request(f"{api.item_path(ident['url'])}?$select=name,size,webUrl,lastModifiedDateTime,folder,file")
    row = util.item_row(m)
    return {"title": m.get("name") or ident["url"], "url": m.get("webUrl") or ident["url"],
            "fields": {"type": row["type"], "modified": row["modified"], "size": row["size"]}}


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
        return done("SharePoint reuses the ms_teams driver's auth helper, which is missing")
    try:
        auth._cdp_json("/json/version")
        step("cdp", True, f"CDP endpoint {auth._cdp_url} reachable")
        tabs = [t for t in auth._cdp_json("/json") if auth._is_teams(t)]
    except Exception as exc:
        step("cdp", False, api.redact(exc))
        return done(f"CDP not reachable: {api.redact(exc)}")
    if not step("teams_tab", bool(tabs), f"{len(tabs)} Teams tab(s)" if tabs else "no Teams tab open"):
        return done("No Teams tab found - open https://teams.cloud.microsoft in that browser and sign in")
    try:
        api.token()
        c = api.claims()
        scopes = [s for s in c["scp"].split() if s.startswith(("Files.", "Sites."))]
        step("token", True, f"graph token for {c['upn'] or '?'}, tenant {c['tid'] or '?'}; file scopes: "
                            f"{', '.join(scopes) or 'none seen'}")
        tid = c["tid"].lower()
    except Exception as exc:
        step("token", False, api.redact(exc))
        return done(f"No usable token: {api.redact(exc)}")
    try:
        me = api.request("/me?$select=displayName,userPrincipalName")
        step("graph_me", True, "Graph /me answered")
    except api.GraphError as exc:
        step("graph_me", False, str(exc)[:200])
        return done(f"Token present but Graph failed: {str(exc)[:120]}")
    try:
        d = api.request("/me/drive?$select=id,driveType,webUrl")
        step("onedrive", True, f"{d.get('driveType')} drive {d.get('webUrl')}")
    except api.GraphError as exc:
        step("onedrive", False, str(exc)[:200] + " (token may lack Files.Read scope)")
        return done(f"Graph works but OneDrive is not readable: {str(exc)[:120]}")
    return done(f"Signed in as {me.get('displayName')} ({me.get('userPrincipalName')}), tenant {tid or '?'}",
                set_options={"tenant_id": tid} if tid else {}, tenant_id=tid)
