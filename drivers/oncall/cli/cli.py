#!/usr/bin/env python3
"""Compact PagerDuty / Opsgenie CLI for dop-agent (terse, LLM-friendly output). Part of the `oncall` package driver.

Connection: --instance NAME (or ONCALL_INSTANCE). base_url, options.backend (pagerduty|opsgenie) and auth.token are read from
config.yaml (integrations.<NAME>, driver oncall); auth.token is a ${env:X}/${file:P}/${keychain:S} ref, never printed.
One command set for both backends; output lines are normalized.

Read:
  incidents [--status open|all] [--service ID] [--team X] [--query Q] [--since-hours N] [--limit 30]
        PagerDuty: --service/--team are IDs. Opsgenie: --team is a team name, --query uses the Opsgenie search syntax; --service unsupported.
  incident ID [--notes 3]        timeline ID [--limit 30]
  schedules [--query TEXT] [--limit 30]
  oncall [--schedule ID | --name TEXT] [--next] [--date ISO] [--limit 30]
  escalations [--query TEXT] [--limit 30]      escalation ID
  services [--query TEXT] [--limit 30]         (PagerDuty services; Opsgenie has none -> its teams are listed)
  teams [--limit 30]
Write (confirm-first: without --confirm only a PREVIEW is printed and nothing is sent):
  ack ID | resolve ID | note ID "text" | override SCHEDULE --user X --start ISO --end ISO     [--confirm]
        (PagerDuty needs options.from_email; override --user is a PagerDuty user id / an Opsgenie username)
Other:
  raw METHOD PATH [-q k=v] [--data JSON|--data-file f] [--jq a.b[*].c] [--max 4000] [--confirm]   (non-GET needs --confirm)
  fetch --kind incident|alert --ident '{"id":"ID"}'   (card JSON, used by dop-agent)
  test                                                (connection check JSON, used by the Settings Test button)
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CONF = {"base_url": "", "backend": "", "from_email": "", "subdomain": ""}
_TOKEN = ""
ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,80}")
NAME_RE = re.compile(r"[^\"`$\\<>\n\r]{1,120}")
REDACT = re.compile(r"(?i)(token|password|secret|authorization|genie ?key|api[_-]?key)(=|:\s*)\S+")


def die(msg):
    sys.exit(redact(msg))


def redact(s):
    s = REDACT.sub(lambda m: m.group(1) + m.group(2) + "***", str(s))
    return s.replace(_TOKEN, "***") if _TOKEN else s


class Http(Exception):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {text}")
        self.code, self.text = code, text


def use(base_url, backend, token, from_email="", subdomain=""):
    """Set the connection directly (tests, embedding); the token stays in module state, never in CONF."""
    global _TOKEN
    none = lambda v: "" if (v or "").lower() == "none" else (v or "")
    CONF.update(base_url=base_url, backend=backend, from_email=none(from_email), subdomain=none(subdomain))
    _TOKEN = token


def configure(instance=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import oncallconfig
    try:
        _, base, token, opts = oncallconfig.load_instance(instance)
    except oncallconfig.InstanceError as e:
        die(str(e))
    use(base, str(opts.get("backend") or ""), token, str(opts.get("from_email") or ""), str(opts.get("subdomain") or ""))


_CONFIG_ERR = ""


def pd():
    return CONF["backend"] == "pagerduty"


def creds():
    if not (CONF["base_url"] and _TOKEN and CONF["backend"] in ("pagerduty", "opsgenie")):
        die("not configured: pass --instance <name> (integrations.<name> with driver oncall, base_url, options.backend, auth.token)")
    if not re.match(r"^https://", CONF["base_url"]):
        die("base URL must start with https://")
    if pd():
        return CONF["base_url"].rstrip("/"), {"Authorization": f"Token token={_TOKEN}", "Accept": "application/vnd.pagerduty+json;version=2"}
    return CONF["base_url"].rstrip("/"), {"Authorization": f"GenieKey {_TOKEN}", "Accept": "application/json"}


def call(method, path, query=None, data=None):
    """The single HTTP seam. query values may be lists (PagerDuty `x[]` params). Errors end the process with a redacted message."""
    base, headers = creds()
    if not path.startswith("/") or "://" in path or ".." in path:
        die("path must start with / and contain no scheme or ..")
    url = base + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
    h = {**headers, "Content-Type": "application/json"}
    if method != "GET" and pd():
        if not CONF["from_email"]:
            die("PagerDuty writes need the from_email option (email of a PagerDuty user) on this instance")
        h["From"] = CONF["from_email"]
    req = urllib.request.Request(url, method=method, headers=h, data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        die(f"HTTP {e.code} {method} {path.split('?')[0]}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        die(f"network error {method} {path.split('?')[0]}: {e.reason}")
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


# ---------- helpers ----------
def short(s, n):
    s = " ".join(str(s or "").split())
    return s if n <= 0 or len(s) <= n else s[:n] + "…"


def ts(v):
    """ISO string or epoch millis -> 'YYYY-MM-DDTHH:MM' (UTC for epoch)."""
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(v / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return str(v or "")[:16]


def emit(s, mx=0):
    s = s if isinstance(s, str) else json.dumps(s, indent=1, ensure_ascii=False)
    if mx and len(s) > mx:
        s = s[:mx] + f"\n…[+{len(s) - mx} chars, use --max]"
    print(s)


def project(obj, path):
    def one(o, parts):
        if not parts:
            return o
        p, rest = parts[0], parts[1:]
        if p.endswith("[*]"):
            o = o.get(p[:-3]) if isinstance(o, dict) else None
            return [one(x, rest) for x in o] if isinstance(o, list) else None
        if isinstance(o, dict):
            return one(o.get(p), rest)
        if isinstance(o, list) and p.isdigit() and int(p) < len(o):
            return one(o[int(p)], rest)
        return None
    paths = [p for p in path.split(",") if p]
    return one(obj, paths[0].split(".")) if len(paths) == 1 else {p: one(obj, p.split(".")) for p in paths}


def id_arg(v, what="id"):
    if not ID_RE.fullmatch(str(v or "")):
        die(f"bad {what} {v!r}")
    return str(v)


def name_arg(v, what="value"):
    if not NAME_RE.fullmatch(str(v or "")):
        die(f"bad {what} {v!r} (no quotes, backticks, $, <>, backslashes or newlines)")
    return str(v)


def lim(n, hi=100):
    return max(1, min(int(n or 30), hi))


def iso_utc(v, what):
    try:
        d = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        die(f"bad {what} {v!r} (ISO 8601, e.g. 2026-03-01T09:00:00+01:00)")
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pages(path, key, query, limit):
    """PagerDuty offset pagination, bounded by `limit` items."""
    out, off = [], 0
    while len(out) < limit:
        r = call("GET", path, {**(query or {}), "limit": min(100, limit - len(out)), "offset": off})
        vals = r.get(key) or []
        out += vals
        if not r.get("more") or not vals:
            break
        off += len(vals)
    return out[:limit]


# ---------- normalization ----------
def norm_pd_incident(i):
    asg = [(a.get("assignee") or {}).get("summary", "") for a in i.get("assignments") or []]
    return {"id": i.get("id", ""), "ref": f"#{i.get('incident_number', '')}", "title": i.get("title") or i.get("summary") or "",
            "status": i.get("status", ""), "priority": (i.get("priority") or {}).get("summary") or i.get("urgency", ""),
            "service": (i.get("service") or {}).get("summary", ""), "created": ts(i.get("created_at")),
            "assignee": ", ".join(a for a in asg if a), "url": i.get("html_url", "")}


def og_link(alert_id):
    if not CONF["subdomain"]:
        return ""
    eu = ".eu." in CONF["base_url"]
    return f"https://{CONF['subdomain']}.app{'.eu' if eu else ''}.opsgenie.com/alert/detail/{alert_id}/details"


def norm_og_alert(a):
    status = "resolved" if a.get("status") == "closed" else ("acknowledged" if a.get("acknowledged") else "triggered")
    return {"id": a.get("id", ""), "ref": f"#{a.get('tinyId', '')}", "title": a.get("message", ""), "status": status,
            "priority": a.get("priority", ""), "service": ",".join(t.get("name") or t.get("id", "") for t in a.get("teams") or []),
            "created": ts(a.get("createdAt")), "assignee": a.get("owner", ""), "url": og_link(a.get("id", ""))}


def line(n):
    return f"{n['id']} {n['ref']} [{n['status']}] {n['priority']} {short(n['title'], 90)} | {n['service']} | {n['created']}" + (f" | {n['assignee']}" if n["assignee"] else "")


# ---------- incidents / alerts ----------
def og_query(a):
    parts = [] if a.query else ["status: open" if a.status == "open" else ""]
    if a.query:
        parts.append(name_arg(a.query, "query"))
    if a.team:
        parts.append(f'teams: "{name_arg(a.team, "team")}"')
    return " AND ".join(p for p in parts if p)


def fetch_incidents(status="open", service=None, team=None, query=None, since_hours=0, limit=30):
    """Normalized incidents/alerts, newest first. Used by the CLI and the watcher."""
    limit = lim(limit)
    if pd():
        q = {"sort_by": "created_at:desc"}
        if status == "open":
            q["statuses[]"] = ["triggered", "acknowledged"]
        if service:
            q["service_ids[]"] = [id_arg(service, "service id")]
        if team:
            q["team_ids[]"] = [id_arg(team, "team id")]
        if since_hours:
            q["since"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if query:
            q["incident_key"] = id_arg(query, "incident key")
        return [norm_pd_incident(i) for i in pages("/incidents", "incidents", q, limit)]
    if service:
        die("--service is not supported on Opsgenie; use --team NAME or --query")
    ns = argparse.Namespace(query=query, status=status, team=team)
    out, off = [], 0
    while len(out) < limit:
        size = min(100, limit - len(out))
        vals = call("GET", "/v2/alerts", {"query": og_query(ns), "limit": size, "offset": off, "sort": "createdAt", "order": "desc"}).get("data") or []
        out += vals
        if len(vals) < size:
            break
        off += len(vals)
    res = [norm_og_alert(x) for x in out[:limit]]
    if since_hours:
        cut = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M")
        res = [x for x in res if x["created"] >= cut]
    return res


def cmd_incidents(a):
    rows = fetch_incidents(a.status, a.service, a.team, a.query, a.since_hours, a.limit)
    for n in rows:
        print(line(n))
    print(f"-- {len(rows)} {'incident' if pd() else 'alert'}(s)")


def get_incident(i):
    i = id_arg(i)
    if pd():
        return norm_pd_incident(call("GET", f"/incidents/{i}").get("incident") or {})
    q = {"identifierType": "tiny"} if i.isdigit() else None
    return norm_og_alert(call("GET", f"/v2/alerts/{i}", q).get("data") or {})


def notes_of(i, n):
    if pd():
        vals = call("GET", f"/incidents/{i}/notes").get("notes") or []
        return [(ts(x.get("created_at")), (x.get("user") or {}).get("summary", ""), x.get("content", "")) for x in vals][-n:]
    q = {"identifierType": "tiny"} if i.isdigit() else None
    vals = (call("GET", f"/v2/alerts/{i}/notes", q).get("data") or [])
    return [(ts(x.get("createdAt")), x.get("owner", ""), x.get("note", "")) for x in vals][-n:]


def cmd_incident(a):
    n = get_incident(a.id)
    print(line(n))
    if n["url"]:
        print("url:", n["url"])
    if a.notes:
        for t, who, text in notes_of(id_arg(a.id), a.notes):
            print(f"note {t} {who}: {short(text, 300)}")


def timeline_of(i, limit):
    if pd():
        vals = pages(f"/incidents/{i}/log_entries", "log_entries", {"is_overview": "true"}, limit)
        return [(ts(x.get("created_at")), str(x.get("type", "")).replace("_log_entry", ""), (x.get("agent") or {}).get("summary", ""), x.get("summary", "")) for x in vals]
    q = {"identifierType": "tiny", "limit": limit} if i.isdigit() else {"limit": limit}
    vals = call("GET", f"/v2/alerts/{i}/logs", q).get("data") or []
    return [(ts(x.get("createdAt")), x.get("type", ""), x.get("owner", ""), x.get("log", "")) for x in vals]


def cmd_timeline(a):
    rows = sorted(timeline_of(id_arg(a.id), lim(a.limit)))
    for t, kind, who, text in rows:
        print(f"{t} {kind} {who} {short(text, 160)}".rstrip())
    print(f"-- {len(rows)} event(s)")


# ---------- schedules / on-call ----------
def list_schedules(query=None, limit=30):
    limit = lim(limit)
    if pd():
        q = {"query": name_arg(query, "query")} if query else {}
        return [{"id": s["id"], "name": s.get("name") or s.get("summary", "")} for s in pages("/schedules", "schedules", q, limit)]
    vals = [{"id": s["id"], "name": s.get("name", "")} for s in call("GET", "/v2/schedules").get("data") or []]
    if query:
        vals = [s for s in vals if query.lower() in s["name"].lower()]
    return vals[:limit]


def cmd_schedules(a):
    rows = list_schedules(a.query, a.limit)
    for s in rows:
        print(f"{s['id']} {s['name']}")
    print(f"-- {len(rows)} schedule(s)")


def resolve_schedule(schedule=None, name=None):
    if schedule:
        sid = id_arg(schedule, "schedule id")
        return sid, next((s["name"] for s in list_schedules(None, 100) if s["id"] == sid), sid)
    hits = list_schedules(name_arg(name, "name"), 100)
    exact = [s for s in hits if s["name"].lower() == name.lower()]
    hits = exact or hits
    if len(hits) != 1:
        die(f"schedule name {name!r} matched {len(hits)} schedules: " + ", ".join(f"{s['id']}={s['name']}" for s in hits[:8]))
    return hits[0]["id"], hits[0]["name"]


def on_call(sid, nxt=False, date=None, schedule_name=""):
    """List of {who, start, end, schedule}; Opsgenie returns only names (no start/end)."""
    if pd():
        q = {"schedule_ids[]": [sid], "earliest": "true"}
        if nxt:
            now = datetime.datetime.now(datetime.timezone.utc)
            q.update(since=now.strftime("%Y-%m-%dT%H:%M:%SZ"), until=(now + datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        elif date:
            q.update(since=date, until=date)
        rows = pages("/oncalls", "oncalls", q, 50)
        now_s = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M")
        out = [{"who": (r.get("user") or {}).get("summary", ""), "start": ts(r.get("start")), "end": ts(r.get("end")),
                "schedule": schedule_name or (r.get("schedule") or {}).get("summary", "")} for r in rows]
        return [r for r in out if r["start"] > now_s] if nxt else out
    q = {"flat": "true"}
    if date:
        q["date"] = date
    r = call("GET", f"/v2/schedules/{sid}/{'next-on-calls' if nxt else 'on-calls'}", q).get("data") or {}
    names = r.get("nextOnCallRecipients" if nxt else "onCallRecipients") or []
    return [{"who": n, "start": "", "end": "", "schedule": schedule_name} for n in names]


def cmd_oncall(a):
    date = iso_utc(a.date, "date") if a.date else None
    if a.schedule or a.name:
        targets = [resolve_schedule(a.schedule, a.name)]
    else:
        targets = [(s["id"], s["name"]) for s in list_schedules(None, min(lim(a.limit), 20))]
    shown = 0
    for sid, name in targets:
        rows = on_call(sid, a.next, date, name)
        for r in rows:
            span = f" ({r['start']} -> {r['end']})" if r["start"] else ""
            print(f"{name}: {r['who']}{span}")
            shown += 1
        if not rows:
            print(f"{name}: nobody")
    print(f"-- {shown} {'next ' if a.next else ''}on-call entr(ies) in {len(targets)} schedule(s)")


# ---------- escalations / services / teams ----------
def cmd_escalations(a):
    if pd():
        q = {"query": name_arg(a.query, "query")} if a.query else {}
        rows = [(x["id"], x.get("name", ""), x.get("num_loops", 0)) for x in pages("/escalation_policies", "escalation_policies", q, lim(a.limit))]
    else:
        rows = [(x["id"], x.get("name", ""), "") for x in (call("GET", "/v2/escalations").get("data") or [])]
        if a.query:
            rows = [r for r in rows if a.query.lower() in r[1].lower()]
        rows = rows[:lim(a.limit)]
    for i, n, loops in rows:
        print(f"{i} {n}" + (f" (repeats {loops}x)" if loops else ""))
    print(f"-- {len(rows)} escalation polic(ies)")


def cmd_escalation(a):
    i = id_arg(a.id)
    if pd():
        p = call("GET", f"/escalation_policies/{i}").get("escalation_policy") or {}
        print(f"{p.get('id')} {p.get('name')} (repeats {p.get('num_loops', 0)}x)")
        for n, r in enumerate(p.get("escalation_rules") or [], 1):
            print(f"  level {n}: after {r.get('escalation_delay_in_minutes')} min -> " + ", ".join(f"{t.get('summary')} ({t.get('type', '').replace('_reference', '')})" for t in r.get("targets") or []))
        print("services:", ", ".join(s.get("summary", "") for s in p.get("services") or []) or "-")
        return
    p = call("GET", f"/v2/escalations/{i}").get("data") or {}
    print(f"{p.get('id')} {p.get('name')}")
    for n, r in enumerate(p.get("rules") or [], 1):
        rc = r.get("recipient") or {}
        print(f"  rule {n}: {r.get('condition')} {r.get('notifyType')} after {(r.get('delay') or {}).get('timeAmount', 0)} min -> {rc.get('type')} {rc.get('name') or rc.get('username') or rc.get('id', '')}")


def cmd_services(a):
    if not pd():
        print("(Opsgenie has no services; listing teams)")
        return cmd_teams(a)
    q = {"query": name_arg(a.query, "query")} if a.query else {}
    rows = pages("/services", "services", q, lim(a.limit))
    for s in rows:
        print(f"{s['id']} {s.get('name')} [{s.get('status')}] ep={(s.get('escalation_policy') or {}).get('summary', '')}")
    print(f"-- {len(rows)} service(s)")


def cmd_teams(a):
    rows = pages("/teams", "teams", None, lim(a.limit)) if pd() else (call("GET", "/v2/teams").get("data") or [])[:lim(a.limit)]
    for t in rows:
        print(f"{t['id']} {t.get('name')}")
    print(f"-- {len(rows)} team(s)")


# ---------- writes (confirm-first) ----------
def write(a, method, path, data, desc, query=None):
    if pd() and not CONF["from_email"]:
        die("PagerDuty writes need the from_email option (email of a PagerDuty user) on this instance")
    if not a.confirm:
        print(f"PREVIEW (nothing sent; re-run with --confirm after the user approved) {method} {path}: {desc}")
        if data is not None:
            print(json.dumps(data, ensure_ascii=False))
        return None
    r = call(method, path, query, data)
    print(f"OK {desc}")
    return r


def cmd_status_change(status_pd, verb_og, label):
    def run(a):
        i = id_arg(a.id)
        if pd() and getattr(a, "note", None):
            die("--note is Opsgenie-only; on PagerDuty add a note with the `note` command")
        if pd():
            write(a, "PUT", "/incidents", {"incidents": [{"id": i, "type": "incident_reference", "status": status_pd}]}, f"{label} incident {i}")
        else:
            body = {"note": a.note} if getattr(a, "note", None) else {}
            write(a, "POST", f"/v2/alerts/{i}/{verb_og}", body, f"{label} alert {i}", {"identifierType": "tiny"} if i.isdigit() else None)
    return run


def cmd_note(a):
    i, text = id_arg(a.id), a.text
    if not text or len(text) > 4000:
        die("note text is required (max 4000 chars)")
    if pd():
        write(a, "POST", f"/incidents/{i}/notes", {"note": {"content": text}}, f"note on incident {i}")
    else:
        write(a, "POST", f"/v2/alerts/{i}/notes", {"note": text}, f"note on alert {i}", {"identifierType": "tiny"} if i.isdigit() else None)


def cmd_override(a):
    sid, name = resolve_schedule(a.schedule, None)
    start, end = iso_utc(a.start, "start"), iso_utc(a.end, "end")
    if end <= start:
        die("end must be after start")
    user = name_arg(a.user, "user")
    if pd():
        body = {"overrides": [{"start": start, "end": end, "user": {"id": id_arg(a.user, "user id"), "type": "user_reference"}}]}
        write(a, "POST", f"/schedules/{sid}/overrides", body, f"override on '{name}': {user} {start} -> {end}")
    else:
        body = {"user": {"type": "user", "username": user}, "startDate": start, "endDate": end}
        write(a, "POST", f"/v2/schedules/{sid}/overrides", body, f"override on '{name}': {user} {start} -> {end}", {"scheduleIdentifierType": "id"})


# ---------- raw / fetch / test ----------
def cmd_raw(a):
    method = a.method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        die("method must be GET|POST|PUT|DELETE")
    data = json.loads(a.data) if a.data else (json.load(open(os.path.expanduser(a.data_file))) if a.data_file else None)
    q = dict(x.split("=", 1) for x in a.q) or None
    if method != "GET":
        write(a, method, a.path, data, f"raw {method} {a.path}", q)
        return
    r = call(method, a.path, q, data)
    emit(project(r, a.jq) if a.jq else r, a.max)


def cmd_fetch(a):
    if a.kind not in ("incident", "alert"):
        die(f"oncall has no kind {a.kind!r}")
    n = get_incident(json.loads(a.ident).get("id", ""))
    print(json.dumps({"title": n["title"], "status": n["status"], "url": n["url"],
                      "fields": {"id": n["id"], "ref": n["ref"], "priority": n["priority"], "service": n["service"], "assignee": n["assignee"]}}))


def cmd_test(a):
    steps, state = [], {}

    def step(name, fn):
        try:
            steps.append({"name": name, "ok": True, "message": fn()})
            return True
        except SystemExit as e:
            steps.append({"name": name, "ok": False, "message": redact(e.code)[:200]})
            return False

    def conf():
        if _CONFIG_ERR:
            die(_CONFIG_ERR)
        creds()
        note = "" if not pd() or CONF["from_email"] else "; writes disabled until from_email is set"
        return f"{CONF['backend']} at {CONF['base_url']}{note}"

    def auth():
        if pd():
            call("GET", "/abilities")
            return "API key accepted"
        acc = (call("GET", "/v2/account").get("data") or {})
        return f"account {acc.get('name', '?')}"

    def data():
        if pd():
            return f"{len(call('GET', '/services', {'limit': 1}).get('services') or [])}+ service(s) readable"
        return f"{len(call('GET', '/v2/teams').get('data') or [])} team(s) readable"
    ok = step("config", conf) and step("auth", auth) and step("data", data)
    msg = next((s["message"] for s in steps if not s["ok"]), "") if not ok else steps[1]["message"] + f" ({CONF['backend']})"
    if not ok and any("HTTP 401" in s["message"] or "HTTP 403" in s["message"] for s in steps):
        msg += " — check auth.token (valid, correct region base_url, read scope)"
    print(json.dumps({"ok": ok, "message": msg, "steps": steps}))


def build():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=os.environ.get("ONCALL_INSTANCE"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, confirm=False):
        p = sp.add_parser(name)
        for x in args:
            p.add_argument(x)
        if confirm:
            p.add_argument("--confirm", action="store_true")
        p.set_defaults(fn=fn)
        return p
    p = add("incidents", cmd_incidents)
    p.add_argument("--status", default="open", choices=["open", "all"])
    for x in ("service", "team", "query"):
        p.add_argument("--" + x)
    p.add_argument("--since-hours", type=int, default=0)
    p.add_argument("--limit", type=int, default=30)
    p = add("incident", cmd_incident, "id")
    p.add_argument("--notes", type=int, default=0)
    p = add("timeline", cmd_timeline, "id")
    p.add_argument("--limit", type=int, default=30)
    p = add("schedules", cmd_schedules)
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=30)
    p = add("oncall", cmd_oncall)
    p.add_argument("--schedule")
    p.add_argument("--name")
    p.add_argument("--date")
    p.add_argument("--next", action="store_true")
    p.add_argument("--limit", type=int, default=30)
    p = add("escalations", cmd_escalations)
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=30)
    add("escalation", cmd_escalation, "id")
    p = add("services", cmd_services)
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=30)
    p = add("teams", cmd_teams)
    p.add_argument("--limit", type=int, default=30)
    p = add("ack", cmd_status_change("acknowledged", "acknowledge", "acknowledge"), "id", confirm=True)
    p.add_argument("--note")
    p = add("resolve", cmd_status_change("resolved", "close", "resolve"), "id", confirm=True)
    p.add_argument("--note")
    add("note", cmd_note, "id", "text", confirm=True)
    p = add("override", cmd_override, "schedule", confirm=True)
    for x in ("user", "start", "end"):
        p.add_argument("--" + x, required=True)
    p = add("raw", cmd_raw, "method", "path", confirm=True)
    p.add_argument("-q", action="append", default=[])
    p.add_argument("--data")
    p.add_argument("--data-file")
    p.add_argument("--jq")
    p.add_argument("--max", type=int, default=4000)
    p = add("fetch", cmd_fetch)
    p.add_argument("--kind", required=True)
    p.add_argument("--ident", required=True)
    add("test", cmd_test)
    return ap


def main(argv=None):
    a = build().parse_args(argv)
    global _CONFIG_ERR
    try:
        configure(a.instance)
    except SystemExit as e:
        if a.cmd != "test":
            raise
        _CONFIG_ERR = str(e.code)  # `test` reports it as a failed config step instead of dying
    try:
        a.fn(a)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
