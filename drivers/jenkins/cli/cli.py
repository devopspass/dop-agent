#!/usr/bin/env python3
"""Jenkins CLI for dop-agent (compact output, bounded scans, tokens never printed).

Run `cli.py --instance <name> <command>`; `<command> -h` shows options. Jobs are addressed by a slash path
(`folder/sub/job`, `%2F` for a slash inside a multibranch branch name) or a Jenkins URL; builds by number or
last|completed|success|failed|stable|unstable.

Reads:  test, fetch, jobs, job-info, builds, build-info, log-tail, stages, queue, nodes
Writes: trigger, abort, rebuild  -> confirm-first: without --yes only a DRY-RUN line is printed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jkconfig  # noqa: E402
from jkclient import (Client, JenkinsError, build_ref, is_folder, job_url_path, norm_path, redact,  # noqa: E402
                      walk_jobs)

COLOR_EMOJI = {"blue": "✅", "red": "❌", "yellow": "⚠️", "aborted": "🚫", "grey": "⚪", "disabled": "⏸️",
               "notbuilt": "⚪"}
RESULT_EMOJI = {"SUCCESS": "✅", "FAILURE": "❌", "UNSTABLE": "⚠️", "ABORTED": "🚫", "NOT_BUILT": "⚪", None: "🔄"}
SECRET_PARAM = re.compile(r"pass|secret|token|key|credential", re.I)


def fmt_dur(ms):
    if ms is None:
        return "-"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if h or m else "") + f"{s}s"


def fmt_ts(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else "-"


def state(b):
    return "RUNNING" if b.get("building") else (b.get("result") or "UNKNOWN")


def params_of(b):
    out = {}
    for act in b.get("actions") or []:
        for p in act.get("parameters") or []:
            name = str(p.get("name"))
            out[name] = "***" if SECRET_PARAM.search(name) else redact(str(p.get("value")))[:120]
    return out


def causes_of(b):
    return [c.get("shortDescription", "") for act in b.get("actions") or [] for c in act.get("causes") or []]


def changes_of(b):
    sets = list(b.get("changeSets") or []) + ([b["changeSet"]] if b.get("changeSet") else [])
    return [(i.get("commitId") or "")[:8] + " " + (i.get("author") or {}).get("fullName", "") + ": " + (i.get("msg") or "")[:100]
            for cs in sets for i in cs.get("items") or []]


BUILD_TREE = ("number,url,result,building,duration,estimatedDuration,timestamp,displayName,description,builtOn,"
              "actions[parameters[name,value],causes[shortDescription]],"
              "changeSets[items[commitId,msg,author[fullName]]],changeSet[items[commitId,msg,author[fullName]]]")


def cmd_jobs(c, a):
    jobs, trunc = walk_jobs(c, a.folder or "", max_jobs=500, max_requests=a.max_requests)
    q = a.search.lower() if a.search else None
    rows = []
    for j in jobs:
        if q and q not in j["fullName"].lower():
            continue
        color = (j.get("color") or "").replace("_anime", "")
        if a.failing and color not in ("red", "yellow"):
            continue
        lb = j.get("lastBuild") or {}
        rows.append(f"{COLOR_EMOJI.get(color, '⚪')} {j['fullName']}  #{lb.get('number', '-')} {state(lb) if lb else '-'}")
    shown = rows[:a.limit]
    tail = f"\n… {len(rows) - len(shown)} more (use --search/--limit)" if len(rows) > len(shown) else ""
    tail += "\n(scan truncated: narrow with a folder argument)" if trunc else ""
    return "\n".join(shown) + tail if shown else "no jobs" + tail


def cmd_job_info(c, a):
    p = job_url_path(a.job)
    j = c.json(p, tree="fullName,url,description,buildable,color,_class,healthReport[description,score],"
                       "property[parameterDefinitions[name,type,defaultParameterValue[value],choices,description]],"
                       "lastBuild[number,result,building,timestamp],lastSuccessfulBuild[number],lastFailedBuild[number],inQueue")
    if is_folder(j):
        return f"{j.get('fullName')} is a folder; use `jobs {norm_path(a.job)}`"
    lines = [f"{j.get('fullName')} {COLOR_EMOJI.get((j.get('color') or '').replace('_anime', ''), '⚪')} "
             f"buildable={j.get('buildable')} inQueue={j.get('inQueue')}", f"url: {j.get('url')}"]
    lb = j.get("lastBuild") or {}
    lines.append(f"last: #{lb.get('number', '-')} {state(lb) if lb else '-'} ({fmt_ts(lb.get('timestamp'))}) "
                 f"success=#{(j.get('lastSuccessfulBuild') or {}).get('number', '-')} "
                 f"failed=#{(j.get('lastFailedBuild') or {}).get('number', '-')}")
    for h in (j.get("healthReport") or [])[:2]:
        lines.append(f"health: {h.get('score')}% {h.get('description', '')[:100]}")
    for prop in j.get("property") or []:
        for d in prop.get("parameterDefinitions") or []:
            dv = (d.get("defaultParameterValue") or {}).get("value")
            dv = "***" if SECRET_PARAM.search(d.get("name", "")) or "Password" in (d.get("type") or "") else dv
            ch = f" choices={d['choices'][:8]}" if d.get("choices") else ""
            lines.append(f"param {d.get('name')} ({d.get('type', '').replace('ParameterDefinition', '')}) default={dv!r}{ch}")
    return "\n".join(lines)


def cmd_builds(c, a):
    p = job_url_path(a.job)
    j = c.json(p, tree=f"builds[number,result,building,duration,timestamp,displayName]{{0,{int(a.limit)}}}")
    rows = [f"{RESULT_EMOJI.get(b.get('result') if not b.get('building') else None, '❔')} #{b['number']} {state(b)} "
            f"{fmt_dur(b.get('duration'))} {fmt_ts(b.get('timestamp'))}" for b in j.get("builds") or []]
    return "\n".join(rows) or "no builds"


def cmd_build_info(c, a):
    b = c.json(f"{job_url_path(a.job)}/{build_ref(a.build)}", tree=BUILD_TREE)
    out = [f"#{b.get('number')} {RESULT_EMOJI.get(None if b.get('building') else b.get('result'), '❔')} {state(b)}  "
           f"{fmt_ts(b.get('timestamp'))}  duration={fmt_dur(b.get('duration'))}"
           + (f" (est {fmt_dur(b.get('estimatedDuration'))})" if b.get("building") else "")
           + (f"  node={b.get('builtOn')}" if b.get("builtOn") else ""), f"url: {b.get('url')}"]
    if b.get("description"):
        out.append("desc: " + redact(b["description"])[:200])
    out += [f"cause: {x[:120]}" for x in causes_of(b)[:3]]
    prm = params_of(b)
    if prm:
        out.append("params: " + ", ".join(f"{k}={v}" for k, v in list(prm.items())[:30]))
    ch = changes_of(b)
    out.append(f"changes: {len(ch)}" + "".join(f"\n  {x}" for x in ch[:15]))
    return "\n".join(out)


def cmd_log_tail(c, a):
    p = f"{job_url_path(a.job)}/{build_ref(a.build)}"
    lines, trunc = c.log_lines(p, tail=a.lines, grep=a.grep)
    head = f"# {norm_path(a.job)} {build_ref(a.build)}" + (" (log cut at 20 MB, tail is of the first 20 MB)" if trunc else "")
    return head + "\n" + ("\n".join(lines) if lines else "(no matching lines)")


def cmd_stages(c, a):
    r = c.get(f"{job_url_path(a.job)}/{build_ref(a.build)}/wfapi/describe").json()
    out = [f"#{r.get('name')} {r.get('status')} {fmt_dur(r.get('durationMillis'))}"]
    for s in r.get("stages") or []:
        out.append(f"{s.get('status', '?'):<12} {s.get('name')}  {fmt_dur(s.get('durationMillis'))}")
    return "\n".join(out) if len(out) > 1 else out[0] + "\n(no stages: not a Pipeline build or wfapi plugin missing)"


def cmd_queue(c, a):
    items = c.json("/queue", tree="items[id,why,inQueueSince,stuck,blocked,task[name,fullDisplayName,url]]").get("items") or []
    now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    rows = [f"#{i['id']} {(i.get('task') or {}).get('fullDisplayName') or (i.get('task') or {}).get('name')} "
            f"waiting {fmt_dur(now - i.get('inQueueSince', now))}{' STUCK' if i.get('stuck') else ''}: {(i.get('why') or '')[:140]}"
            for i in items[:a.limit]]
    return f"{len(items)} queued\n" + "\n".join(rows) if rows else "queue empty"


def cmd_nodes(c, a):
    cs = c.json("/computer", tree="computer[displayName,offline,temporarilyOffline,offlineCauseReason,numExecutors,idle,"
                                  "executors[idle],assignedLabels[name]]").get("computer") or []
    rows = []
    for n in cs:
        busy = sum(1 for e in n.get("executors") or [] if not e.get("idle"))
        st = "OFFLINE" if n.get("offline") else "online"
        if a.offline and not n.get("offline"):
            continue
        why = f" ({(n.get('offlineCauseReason') or '')[:80]})" if n.get("offline") else ""
        rows.append(f"{'🔴' if n.get('offline') else '🟢'} {n.get('displayName')} {st} {busy}/{n.get('numExecutors')} busy{why}")
    return "\n".join(rows[:a.limit]) or "no nodes"


def _confirm(a, what):
    if a.yes:
        return True
    print(f"DRY-RUN (nothing done): would {what}. Re-run with --yes after the user confirms.")
    return False


def cmd_trigger(c, a):
    prm = {}
    for kv in a.param or []:
        if "=" not in kv:
            raise JenkinsError(f"--param must be KEY=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        prm[k] = v
    shown = ", ".join(f"{k}={'***' if SECRET_PARAM.search(k) else v[:40]}" for k, v in prm.items()) or "no parameters"
    if not _confirm(a, f"trigger {norm_path(a.job)} with {shown}"):
        return ""
    r = c.post(job_url_path(a.job) + ("/buildWithParameters" if prm else "/build"), data=prm or None)
    return f"triggered {norm_path(a.job)} ({shown}); queue: {r.headers.get('Location', '-')}"


def cmd_abort(c, a):
    n = build_ref(a.build)
    if not _confirm(a, f"abort {norm_path(a.job)} {n}"):
        return ""
    c.post(f"{job_url_path(a.job)}/{n}/stop")
    return f"abort requested for {norm_path(a.job)} {n}"


def cmd_rebuild(c, a):
    n = build_ref(a.build)
    b = c.json(f"{job_url_path(a.job)}/{n}", tree="number,actions[parameters[name,value,_class]]")
    prm = {}
    for act in b.get("actions") or []:
        for p in act.get("parameters") or []:
            if p.get("value") is not None:  # password/file params have no readable value: Jenkins uses defaults
                prm[p["name"]] = str(p["value"])
    shown = ", ".join(f"{k}={'***' if SECRET_PARAM.search(k) else v[:40]}" for k, v in prm.items()) or "no parameters"
    if not _confirm(a, f"rebuild {norm_path(a.job)} #{b.get('number')} with {shown}"):
        return ""
    r = c.post(job_url_path(a.job) + ("/buildWithParameters" if prm else "/build"), data=prm or None)
    return f"rebuild of #{b.get('number')} triggered ({shown}); queue: {r.headers.get('Location', '-')}"


def cmd_fetch(c, a):
    ident = json.loads(a.ident)
    path = norm_path(ident.get("path", ""))
    if a.kind == "build":
        b = c.json(f"{job_url_path(path)}/{int(ident.get('id', 0))}", tree=BUILD_TREE)
        res = state(b)
        return json.dumps({"title": f"{path} #{b.get('number')}", "url": b.get("url") or "", "status": res,
                           "status_emoji": RESULT_EMOJI.get(None if b.get("building") else b.get("result"), ""),
                           "fields": {"job": path, "result": res, "duration": fmt_dur(b.get("duration"))}})
    if a.kind == "job":
        j = c.json(job_url_path(path), tree="fullName,url,color,lastBuild[number,result,building]")
        lb = j.get("lastBuild") or {}
        res = state(lb) if lb else "NO BUILDS"
        return json.dumps({"title": j.get("fullName") or path, "url": j.get("url") or "", "status": res,
                           "status_emoji": COLOR_EMOJI.get((j.get("color") or "").replace("_anime", ""), ""),
                           "fields": {"job": j.get("fullName") or path, "last_build": f"#{lb.get('number', '-')}", "result": res}})
    raise JenkinsError(f"unknown kind {a.kind!r}")


def cmd_test(_c, a):
    """Per-step connection test for Settings -> Test; never raises, never prints the token."""
    steps, out = [], {"ok": False, "message": "", "steps": []}
    out["steps"] = steps

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})

    try:
        name, base, user, token, _ = jkconfig.load_instance(a.instance)
        step("config", True, f"instance {name} -> {base} as {user}")
    except jkconfig.InstanceError as exc:
        step("config", False, str(exc))
        out["message"] = str(exc)
        return json.dumps(out)
    try:
        import requests
        r = requests.get(f"{base}/me/api/json", params={"tree": "id,fullName"}, auth=(user, token), timeout=20)
    except Exception as exc:
        step("reach", False, type(exc).__name__)
        out["message"] = f"{base} unreachable ({type(exc).__name__}): check base_url / VPN"
        return json.dumps(out)
    step("reach", True, f"HTTP {r.status_code}" + (f", Jenkins {r.headers['X-Jenkins']}" if r.headers.get("X-Jenkins") else ""))
    if not r.ok:
        step("auth", False, f"HTTP {r.status_code}")
        out["message"] = f"credentials rejected by {base} (HTTP {r.status_code}): check options.user and auth.token (API token, not password)"
        return json.dumps(out)
    who = (r.json() or {}).get("fullName") or user
    step("auth", True, f"user {who}")
    out.update(ok=True, message=f"authenticated as {who}")
    return json.dumps(out)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=None)
    s = ap.add_subparsers(dest="cmd", required=True)
    s.add_parser("test")
    x = s.add_parser("fetch"); x.add_argument("--kind", required=True, choices=["build", "job"]); x.add_argument("--ident", required=True)
    x = s.add_parser("jobs", help="list/search jobs under a folder (breadth-first, bounded)")
    x.add_argument("folder", nargs="?"); x.add_argument("--search"); x.add_argument("--failing", action="store_true")
    x.add_argument("--limit", type=int, default=40); x.add_argument("--max-requests", type=int, default=25)
    x = s.add_parser("job-info"); x.add_argument("job")
    x = s.add_parser("builds"); x.add_argument("job"); x.add_argument("--limit", type=int, default=10)
    x = s.add_parser("build-info"); x.add_argument("job"); x.add_argument("build", nargs="?", default="last")
    x = s.add_parser("log-tail"); x.add_argument("job"); x.add_argument("build", nargs="?", default="last")
    x.add_argument("--lines", type=int, default=60); x.add_argument("--grep")
    x = s.add_parser("stages"); x.add_argument("job"); x.add_argument("build", nargs="?", default="last")
    x = s.add_parser("queue"); x.add_argument("--limit", type=int, default=30)
    x = s.add_parser("nodes"); x.add_argument("--offline", action="store_true"); x.add_argument("--limit", type=int, default=60)
    x = s.add_parser("trigger", help="WRITE: start a build (dry-run unless --yes)")
    x.add_argument("job"); x.add_argument("--param", action="append", metavar="KEY=VALUE"); x.add_argument("--yes", action="store_true")
    x = s.add_parser("abort", help="WRITE: stop a running build (dry-run unless --yes)")
    x.add_argument("job"); x.add_argument("build"); x.add_argument("--yes", action="store_true")
    x = s.add_parser("rebuild", help="WRITE: new build with the parameters of an old one (dry-run unless --yes)")
    x.add_argument("job"); x.add_argument("build", nargs="?", default="last"); x.add_argument("--yes", action="store_true")
    return ap


CMDS = {"fetch": cmd_fetch, "jobs": cmd_jobs, "job-info": cmd_job_info, "builds": cmd_builds, "build-info": cmd_build_info,
        "log-tail": cmd_log_tail, "stages": cmd_stages, "queue": cmd_queue, "nodes": cmd_nodes, "trigger": cmd_trigger,
        "abort": cmd_abort, "rebuild": cmd_rebuild}


def make_client(instance):
    _, base, user, token, _ = jkconfig.load_instance(instance)
    return Client(base, user, token)


def main(argv=None, client=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(cmd_test(None, a))
            return 0
        res = CMDS[a.cmd](client or make_client(a.instance), a)
        if res:
            print(res)
    except (JenkinsError, jkconfig.InstanceError) as e:
        print(f"ERROR: {redact(e)[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
