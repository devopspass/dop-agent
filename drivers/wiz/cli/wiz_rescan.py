"""On-demand "Rescan resource" and scan-activity status for Wiz cloud resources (CDP transport).

There is no public API for this. The Wiz UI's "Rescan resource" button fires an internal mutation:

    mutation RequestResourceScan($input: RequestConnectorEntityScanInput!) {
      requestConnectorEntityScan(input: $input) { success reason scan { id } } }
    variables: {"input": {"id": "<resource-uuid>", "type": "VIRTUAL_MACHINE"}}

GOTCHA - session-cookie auth: unlike the read-only queries (wizcli bearer token, wiz_api.py) this mutation, and the
"Running Activities" query used for status, are accepted with the browser's own session cookies. So the calls are
made with `fetch()` from INSIDE an already-open, logged-in app.wiz.io tab driven over Chrome DevTools Protocol
(browser started with --remote-debugging-port; `pip install websocket-client`). The browser is never launched and
login is never handled here.

- A `success` reply only means the scan was QUEUED. Wiz chains several async activities per resource (e.g. "VM
  metadata scan" then "VM disk scan", sharing a `groupId` == the returned scan id); use `rescan-status` until all are
  DONE before trusting re-queried findings.
- Wiz enforces a ~10 min per-resource cooldown ("allow at least 10 minutes between requests"): reported as
  status `cooldown`, not an error. Space several triggers yourself (time watch).
- Per-resource `"Unauthorized"` is a real, observed answer (Wiz project/RBAC scoping: the user may scan some
  resources but not others although read-only status works): status `error`; do not retry it like a cooldown.
- A long-backgrounded tab can stall `fetch()` forever (Chromium throttling): `Page.bringToFront` is sent first.
- The resource UUID is Wiz's cloud-resource id, not the AWS instance id: `findings --format assets` lists it.
"""
import json
import time
import urllib.request

import wiz_auth as auth

cdp_base = "http://localhost:9222"
PORTAL = "https://app.wiz.io/home"

MUTATION = ("mutation RequestResourceScan($input: RequestConnectorEntityScanInput!) {"
            " requestConnectorEntityScan(input: $input) { success reason scan { id __typename } __typename } }")
ACTIVITY_QUERY = (
    "query CheckResourceScanActivities($filterBy: SystemActivityFilters, $first: Int) {"
    " systemActivities(filterBy: $filterBy, first: $first) { nodes { id name status statusInfo startedAt createdAt groupId"
    " context { ... on SystemActivityScanContext { type scannedEntity { id name __typename } __typename } } __typename }"
    " __typename } }")
ACTIVITY_TYPES = ["SCAN", "RESOURCE_FORENSICS_COPY", "REMEDIATION_ACTION_RUN", "REMEDIATION_ACTION_RUN_REVERT",
                  "UPDATE_REMEDIATION_CATALOG", "REPORT_RUN", "SECURITY_FRAMEWORK_FROM_PDF_CREATE",
                  "PENETRATION_TEST_FINDINGS_IMPORT", "PULL_REQUEST_CREATE", "ISSUE_AI_REMEDIATION_ANALYSIS_RUN",
                  "POSTURE_ISSUE_AI_REMEDIATION_ANALYSIS_RUN", "SENSOR_COMMAND", "THREAT_CENTER_PROJECT_SCOPE_RUN"]
UNFINISHED = {"PENDING", "IN_PROGRESS", "QUEUED", "RUNNING"}


class RescanError(RuntimeError):
    pass


def configure(cdp_url=None):
    global cdp_base
    if cdp_url:
        cdp_base = cdp_url.rstrip("/")


def cdp_get(path, method="GET"):
    req = urllib.request.Request(f"{cdp_base}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def find_or_create_wiz_tab(tab_id=None):
    tabs = cdp_get("/json")
    if tab_id:
        for t in tabs:
            if t["id"] == tab_id:
                return t
        raise RescanError(f"--tab-id {tab_id} not found among open tabs")
    for t in tabs:
        if t.get("type") == "page" and "wiz.io" in t.get("url", ""):
            return t
    return cdp_get(f"/json/new?{PORTAL}", method="PUT")


class CDPSession:
    def __init__(self, ws_url):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self.ws.settimeout(0.3)
        self.msg_id = 1

    def send(self, method, params=None):
        self.ws.send(json.dumps({"id": self.msg_id, "method": method, "params": params or {}}))
        self.msg_id += 1
        return self.msg_id - 1

    def recv_until(self, target_id, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                continue
            if m.get("id") == target_id:
                return m
        return None

    def eval_js(self, expression, timeout=30):
        r = self.recv_until(self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                            "awaitPromise": True}), timeout=timeout)
        if not r:
            return None
        res = r.get("result", {}).get("result", {})
        return res["value"] if "value" in res else {"__exception__": r.get("result", {}).get("exceptionDetails")}

    def close(self):
        self.ws.close()


def open_session(tab_id=None):
    s = CDPSession(find_or_create_wiz_tab(tab_id)["webSocketDebuggerUrl"])
    s.recv_until(s.send("Page.enable"))
    s.recv_until(s.send("Runtime.enable"))
    s.recv_until(s.send("Page.bringToFront"), timeout=5)
    return s


def _page_graphql(session, operation, query, variables):
    """POST from the page context (session cookies). -> parsed body dict or {"error": ...}."""
    payload = json.dumps({"operationName": operation, "variables": variables, "query": query})
    expr = f"""(async () => {{ try {{
      const resp = await fetch({json.dumps(auth.current_graphql_url())}, {{method: "POST", credentials: "include",
        headers: {{"content-type": "application/json", "accept": "application/graphql-response+json,application/json;q=0.9"}},
        body: {json.dumps(payload)}}});
      return {{status: resp.status, body: await resp.text()}};
    }} catch (e) {{ return {{error: String(e)}}; }} }})();"""
    r = session.eval_js(expr)
    if not r:
        return {"error": "no_response"}
    if "__exception__" in r or "error" in r:
        return {"error": r}
    try:
        return json.loads(r.get("body", ""))
    except ValueError:
        return {"error": {"raw": str(r.get("body", ""))[:300], "status": r.get("status")}}


def request_scan(session, asset_id, asset_type="VIRTUAL_MACHINE"):
    """-> {"status": success|cooldown|error|rejected|no_response|js_error, "message": ...}"""
    body = _page_graphql(session, "RequestResourceScan", MUTATION, {"input": {"id": asset_id, "type": asset_type}})
    if "error" in body and "data" not in body:
        return {"status": "no_response" if body["error"] == "no_response" else "js_error", "message": body["error"]}
    if body.get("errors"):
        msg = body["errors"][0].get("message", "")
        return {"status": "cooldown" if "allow at least" in msg.lower() else "error", "message": msg}
    data = (body.get("data") or {}).get("requestConnectorEntityScan")
    if data and data.get("success"):
        return {"status": "success", "message": data.get("scan")}
    return {"status": "rejected", "message": data or body}


def scan_activities(session, asset_id, scan_id=None):
    """Scan activities of one resource (only those of `scan_id`'s group when given) summarized as
    {"done": bool|None, "activities": [{name,status,startedAt}], "note"?, "error"?}."""
    body = _page_graphql(session, "CheckResourceScanActivities", ACTIVITY_QUERY,
                         {"filterBy": {"resourceId": [asset_id], "activityType": ACTIVITY_TYPES}, "first": 40})
    if body.get("errors") or ("error" in body and "data" not in body):
        return {"done": None, "error": body.get("errors") or body["error"]}
    nodes = ((body.get("data") or {}).get("systemActivities") or {}).get("nodes", [])
    rel = [n for n in nodes if not scan_id or n.get("groupId") == scan_id]
    if not rel:
        return {"done": True, "activities": [], "note": "no matching activity records found"}
    return {"done": not any(n.get("status") in UNFINISHED for n in rel),
            "activities": [{"name": n.get("name"), "status": n.get("status"), "startedAt": n.get("startedAt")} for n in rel]}
