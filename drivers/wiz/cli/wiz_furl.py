"""`fetch` (furl card) and `test` (connection check) subcommands used by dop-agent's package driver.

Both print ONE JSON object to stdout and exit 0 for expected outcomes (a failed `test` is data, not a crash).
Tokens are never printed.

fetch --kind issue|finding --ident '<json from the link regex>'
    -> {title, status, status_emoji, url, fields:{severity, asset, ...}}
test
    -> {ok, message, steps:[{step, ok, detail}]}   (python -> wizcli -> auth file -> token refresh -> GraphQL probe,
       plus an informational CDP check for the rescan commands, which never fails the test)

The issue/finding GraphQL shapes follow the public Wiz schema (not verified against a live tenant): a schema
mismatch surfaces as an error (no card, plain link), never as wrong data.
"""
import json
import shutil
import sys

import wiz_auth as auth
import wiz_rescan as rescan

SEV_EMOJI = {"CRITICAL": "🟣", "HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡", "INFORMATIONAL": "⚪", "NONE": "⚪"}
STATUS_EMOJI = {"OPEN": "🔓", "IN_PROGRESS": "🛠️", "RESOLVED": "✅", "REJECTED": "🚫"}

ISSUE_QUERY = ("query Issue($id: ID!) { issue(id: $id) { id severity status createdAt "
               "entitySnapshot { name type } sourceRule { __typename ... on Control { name } } } }")
FINDING_QUERY = ("query Finding($f: VulnerabilityFindingFilters) { vulnerabilityFindings(first: 1, filterBy: $f) "
                 "{ nodes { id name severity status detailedName fixedVersion portalUrl "
                 "vulnerableAsset { __typename ... on VulnerableAssetVirtualMachine { name } } } } }")


def _gql(query, variables):
    r = auth.graphql(query, variables)
    if r.get("errors"):
        raise RuntimeError("Wiz GraphQL error: " + json.dumps(r["errors"])[:200])
    return r["data"]


def _card(title, severity, status, url, **fields):
    return {"title": title, "status": status or "", "status_emoji": STATUS_EMOJI.get(status or "", ""),
            "url": url, "fields": {"severity": f"{SEV_EMOJI.get(severity, '')} {severity or ''}".strip(), **fields}}


def fetch(kind, ident, url=""):
    if kind == "issue":
        i = _gql(ISSUE_QUERY, {"id": ident["id"]}).get("issue")
        if not i:
            raise RuntimeError("issue not found (or not visible to this account)")
        rule = (i.get("sourceRule") or {}).get("name") or "Wiz issue"
        ent = i.get("entitySnapshot") or {}
        return _card(rule, i.get("severity"), i.get("status"), url, asset=ent.get("name") or "",
                     type=ent.get("type") or "", created=i.get("createdAt") or "")
    if kind == "finding":
        nodes = _gql(FINDING_QUERY, {"f": {"id": [ident["id"]]}})["vulnerabilityFindings"]["nodes"]
        if not nodes:
            raise RuntimeError("finding not found (or not visible to this account)")
        n = nodes[0]
        return _card(f"{n.get('name')} {n.get('detailedName') or ''}".strip(), n.get("severity"), n.get("status"),
                     n.get("portalUrl") or url, asset=(n.get("vulnerableAsset") or {}).get("name") or "",
                     fixed=n.get("fixedVersion") or "")
    raise RuntimeError(f"unknown kind {kind!r}")


def run_test():
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    def done(message):
        return {"ok": all(s["ok"] for s in steps), "message": message, "steps": steps}

    if sys.version_info < (3, 10):
        step("python", False, f"Python {sys.version_info[0]}.{sys.version_info[1]} is too old (3.10+ needed)")
        return done("CLI needs Python 3.10+")
    try:
        import websocket  # noqa: F401
        step("python", True, f"Python {sys.version_info[0]}.{sys.version_info[1]} with websocket-client (rescan available)")
    except ImportError:
        step("python", True, f"websocket-client missing for {sys.executable} - only rescan/rescan-status need it: "
                             "pip install websocket-client")
    if not step("wizcli", bool(shutil.which("wizcli")),
                "wizcli found" if shutil.which("wizcli") else "wizcli is not on PATH (macOS: brew install --cask wizcli)"):
        return done("wizcli is not installed")
    if not step("auth_file", auth.auth_path().exists(), str(auth.auth_path())):
        return done(f"Not logged in: run WIZ_DIR={auth.auth_path().parent} wizcli auth --use-device-code")
    st = auth.auth_status()
    if not step("token_and_graphql", st["ok"], st["message"] + (f" (data center {st['data_center']})" if st.get("data_center") else "")):
        return done(st["message"])
    try:
        rescan.cdp_get("/json/version")
        step("cdp", True, f"browser reachable at {rescan.cdp_base} (rescan commands need a logged-in app.wiz.io tab)")
    except Exception as exc:
        step("cdp", True, f"informational: browser CDP not reachable at {rescan.cdp_base} ({str(exc)[:80]}); "
                          "rescan/rescan-status unavailable")
    return done(f"Wiz API reachable (data center {st.get('data_center', '?')})")
