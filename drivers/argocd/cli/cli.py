#!/usr/bin/env python3
"""Compact, LLM-friendly Argo CD CLI (wraps the native `argocd` binary; auth = your `argocd login` session).

Global: --instance NAME (argocd integration in config.yaml; optional when exactly one is enabled).
Checks: test   (config, binary, server, login session, app visibility; JSON {ok, message, steps})

Reads (<app> = name or namespace/name):
  apps [--project P] [--selector S] [--out-of-sync] [--unhealthy] [--limit 100]
  app <app>                  health, sync, revision, destination, operation, conditions
  tree <app> [--unhealthy] [--limit 200]       resources with health/sync status
  diff <app> [--revision R] [--max-chars 6000]  live vs desired (exit code 1 of argocd = differences)
  history <app> [--limit 10]
  problems <app>             conditions, last operation message, failed/unhealthy/out-of-sync resources
  logs <app> [--kind K --resource-name N --namespace NS --container C] [--tail 100] [--grep RE] [--previous]
  appsets | appset <name>    projects | project <name>
  clusters | repos           metadata only (whitelisted fields; credentials are never shown)
Writes (confirm-first: without --yes only a preview is printed and nothing is executed):
  refresh <app> [--hard] [--yes]
  sync <app> [--revision R] [--prune] [--dry-run] [--yes]
  rollback <app> <history_id> [--prune] [--yes]
Instance options `project` / `app_selector` scope lists and block access to apps of other projects.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import argocdconfig  # noqa: E402
from argocdclient import (ArgoClient, ArgoError, redact, valid_name, valid_ns, valid_rev, valid_selector)  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B")
HEALTH_EMOJI = {"Healthy": "💚", "Degraded": "💔", "Progressing": "🔄", "Missing": "❓", "Suspended": "⏸️", "Unknown": "❔"}
CONFIRM = "NOT EXECUTED — show this preview to the user and re-run with --yes only after they confirm."


def split_ref(ref):
    """'name' or 'namespace/name' -> (namespace|None, name); both validated (never start with '-')."""
    ns, sep, name = ref.rpartition("/")
    return (valid_ns(ns) if sep else None), valid_name(name, "application name")


def app_arg(ref):
    ns, name = split_ref(ref)
    return f"{ns}/{name}" if ns else name


def get_app(c, ref):
    app = c.json(["app", "get", app_arg(ref)])
    if not isinstance(app, dict):
        raise ArgoError(f"app {ref!r}: empty answer from argocd")
    proj = (app.get("spec") or {}).get("project") or ""
    if c.project and proj != c.project:
        raise ArgoError(f"app {ref!r} is in project {proj!r}, outside this instance's scope (project={c.project!r})")
    return app


def summ(app):
    md, spec, st = app.get("metadata") or {}, app.get("spec") or {}, app.get("status") or {}
    dest = spec.get("destination") or {}
    src = spec.get("source") or (spec.get("sources") or [{}])[0]
    return {"name": md.get("name", ""), "namespace": md.get("namespace", ""), "project": spec.get("project", ""),
            "health": (st.get("health") or {}).get("status", "Unknown"), "sync": (st.get("sync") or {}).get("status", "Unknown"),
            "revision": ((st.get("sync") or {}).get("revision") or "")[:10],
            "cluster": dest.get("name") or dest.get("server") or "", "dest_ns": dest.get("namespace", ""),
            "repo": src.get("repoURL", ""), "target": src.get("targetRevision", ""),
            "op": ((st.get("operationState") or {}).get("phase") or "")}


def line(s):
    return f"{s['name']} [{s['project']}] {s['health']}/{s['sync']} rev={s['revision'] or '-'} -> {s['cluster']}/{s['dest_ns']}" + (f" op={s['op']}" if s["op"] else "")


def clip(text, n):
    return text if len(text) <= n else text[:n] + f"\n... (cut at {n} chars)"


def cmd_apps(c, a):
    proj = a.project or c.project
    if c.project and a.project and a.project != c.project:
        raise ArgoError(f"project {a.project!r} is outside this instance's scope (project={c.project!r})")
    args = ["app", "list"]
    if proj:
        args += ["-p", valid_name(proj, "project")]
    sel = a.selector or c.selector
    if a.selector and c.selector:
        sel = f"{c.selector},{a.selector}"
    if sel:
        args += ["-l", valid_selector(sel)]
    rows = [summ(x) for x in (c.json(args) or [])]
    if a.out_of_sync:
        rows = [r for r in rows if r["sync"] != "Synced"]
    if a.unhealthy:
        rows = [r for r in rows if r["health"] != "Healthy"]
    out = [f"{len(rows)} app(s)"] + [line(r) for r in rows[:a.limit]]
    if len(rows) > a.limit:
        out.append(f"... {len(rows) - a.limit} more (raise --limit or filter)")
    return "\n".join(out)


def problems(app):
    st = app.get("status") or {}
    out = [f"condition {x.get('type')}: {' '.join(str(x.get('message', '')).split())[:300]}" for x in st.get("conditions") or []]
    op = st.get("operationState") or {}
    if op.get("phase") not in (None, "Succeeded"):
        out.append(f"operation {op.get('phase')}: {' '.join(str(op.get('message', '')).split())[:300]}")
    for r in (op.get("syncResult") or {}).get("resources") or []:
        if r.get("status") not in ("Synced", "Pruned", None):
            out.append(f"sync {r.get('kind')}/{r.get('name')} {r.get('status')}: {' '.join(str(r.get('message', '')).split())[:200]}")
    for r in st.get("resources") or []:
        h = (r.get("health") or {}).get("status")
        if h not in (None, "Healthy") or r.get("status") == "OutOfSync":
            out.append(f"resource {r.get('kind')}/{r.get('name')} ns={r.get('namespace', '')} health={h or '-'} sync={r.get('status', '-')}"
                       + (f" | {(r.get('health') or {}).get('message', '')[:150]}" if (r.get('health') or {}).get('message') else ""))
    return out or ["no conditions, failed operation or unhealthy/out-of-sync resources"]


def cmd_app(c, a):
    app = get_app(c, a.app)
    s = summ(app)
    head = [line(s), f"repo={s['repo']} target={s['target']} namespace={s['namespace']}"]
    return "\n".join(head + problems(app)[:15])


def cmd_problems(c, a):
    return "\n".join(problems(get_app(c, a.app)))


def cmd_tree(c, a):
    rows = (get_app(c, a.app).get("status") or {}).get("resources") or []
    out = []
    for r in rows:
        h = (r.get("health") or {}).get("status") or "-"
        if a.unhealthy and h in ("Healthy", "-") and r.get("status") == "Synced":
            continue
        out.append(f"{r.get('kind')}/{r.get('name')} ns={r.get('namespace', '')} health={h} sync={r.get('status', '-')}")
    more = [f"... {len(out) - a.limit} more"] if len(out) > a.limit else []
    return "\n".join([f"{len(out)} resource(s)"] + out[:a.limit] + more)


def cmd_history(c, a):
    hist = (get_app(c, a.app).get("status") or {}).get("history") or []
    rows = [f"id={h.get('id')} rev={(h.get('revision') or '')[:10]} deployed={h.get('deployedAt', '')} repo={(h.get('source') or {}).get('repoURL', '')}"
            for h in hist[-a.limit:]]
    return "\n".join(rows or ["no history"])


def cmd_diff(c, a):
    get_app(c, a.app)
    args = ["app", "diff", app_arg(a.app)]
    if a.revision:
        args += ["--revision", valid_rev(a.revision)]
    rc, out, err = c.run(args)
    if rc == 0:
        return "no differences (live matches desired)"
    if rc == 1:
        return clip(ANSI.sub("", out), a.max_chars)
    raise ArgoError(f"argocd app diff failed (rc={rc}): {(err or out).strip()[-300:]}")


def clean_logs(text, tail, grep):
    lines = [ANSI.sub("", ln)[:300] for ln in text.splitlines() if ln.strip()]
    if grep:
        rx = re.compile(grep, re.I)
        lines = [ln for ln in lines if rx.search(ln)]
    return lines[-tail:]


def cmd_logs(c, a):
    get_app(c, a.app)
    args = ["app", "logs", app_arg(a.app), "--tail", str(max(1, min(a.tail, 2000)))]
    for flag, val, chk in (("--kind", a.kind, valid_name), ("--name", a.resource_name, valid_name),
                           ("--namespace", a.namespace, valid_ns), ("--container", a.container, valid_name)):
        if val:
            args += [flag, chk(val)]
    if a.previous:
        args.append("--previous")
    rc, out, err = c.run(args, timeout=60)
    if rc != 0:
        raise ArgoError(f"argocd app logs failed (rc={rc}): {(err or out).strip()[-300:]}")
    lines = clean_logs(out, a.tail, a.grep)
    return clip("\n".join(lines) or "(no log lines)", a.max_chars)


def cmd_appsets(c, a):
    rows = c.json(["appset", "list"]) or []
    out = []
    for x in rows:
        md, spec = x.get("metadata") or {}, x.get("spec") or {}
        gens = ",".join(sorted({k for g in spec.get("generators") or [] for k in g}))
        out.append(f"{md.get('name')} project={((spec.get('template') or {}).get('spec') or {}).get('project', '')} generators={gens}")
    return "\n".join([f"{len(out)} applicationset(s)"] + out)


def cmd_appset(c, a):
    x = c.json(["appset", "get", valid_name(a.name)])
    md, spec, st = x.get("metadata") or {}, x.get("spec") or {}, x.get("status") or {}
    gens = ",".join(sorted({k for g in spec.get("generators") or [] for k in g}))
    out = [f"{md.get('name')} generators={gens} apps={len(st.get('applicationStatus') or [])}"]
    out += [f"condition {k.get('type')}={k.get('status')}: {str(k.get('message', ''))[:200]}" for k in st.get("conditions") or []]
    out += [f"app {s.get('application')} {s.get('status')}" for s in (st.get("applicationStatus") or [])[:50]]
    return "\n".join(out)


def cmd_projects(c, a):
    rows = c.json(["proj", "list"]) or []
    if c.project:
        rows = [x for x in rows if (x.get("metadata") or {}).get("name") == c.project]
    return "\n".join([f"{len(rows)} project(s)"] + [
        f"{(x.get('metadata') or {}).get('name')} dest={len((x.get('spec') or {}).get('destinations') or [])} "
        f"sources={len((x.get('spec') or {}).get('sourceRepos') or [])} | {((x.get('spec') or {}).get('description') or '')[:80]}" for x in rows])


def cmd_project(c, a):
    name = valid_name(a.name)
    if c.project and name != c.project:
        raise ArgoError(f"project {name!r} is outside this instance's scope (project={c.project!r})")
    spec = (c.json(["proj", "get", name]) or {}).get("spec") or {}
    out = [f"{name}: {spec.get('description', '')}"]
    out += [f"dest {d.get('name') or d.get('server')}/{d.get('namespace')}" for d in spec.get("destinations") or []]
    out += [f"source {s}" for s in spec.get("sourceRepos") or []]
    return "\n".join(out)


def cmd_clusters(c, a):
    rows = c.json(["cluster", "list"]) or []
    return "\n".join([f"{len(rows)} cluster(s)"] + [
        f"{x.get('name')} {x.get('server')} k8s={x.get('serverVersion', '-')} {((x.get('connectionState') or {}).get('status')) or '-'}" for x in rows])


def cmd_repos(c, a):
    rows = c.json(["repo", "list"]) or []
    return "\n".join([f"{len(rows)} repo(s)"] + [
        f"{x.get('repo')} type={x.get('type', 'git')} project={x.get('project', '-')} {((x.get('connectionState') or {}).get('status')) or '-'}" for x in rows])


def preview(c, a, action, extra):
    s = summ(get_app(c, a.app))
    return "\n".join([f"PREVIEW {action} on {c.base_url}", line(s), *extra, CONFIRM])


def cmd_refresh(c, a):
    if not a.yes:
        return preview(c, a, "refresh" + (" --hard" if a.hard else ""), [])
    get_app(c, a.app)
    app = c.json(["app", "get", app_arg(a.app), "--hard-refresh" if a.hard else "--refresh"])
    return "refreshed: " + line(summ(app))


def cmd_sync(c, a):
    get_app(c, a.app)
    args = ["app", "sync", app_arg(a.app), "--timeout", "120"]
    if a.revision:
        args += ["--revision", valid_rev(a.revision)]
    if a.prune:
        args.append("--prune")
    if a.dry_run:
        rc, out, err = c.run(args + ["--dry-run"], timeout=150)
        return f"dry-run rc={rc}\n" + clip(ANSI.sub("", out or err), 3000)
    if not a.yes:
        return preview(c, a, "sync", [f"revision={a.revision or 'target revision of the app'} prune={a.prune}",
                                      "tip: run `diff` and `sync --dry-run` first"])
    rc, out, err = c.run(args, timeout=180)
    s = summ(get_app(c, a.app))
    return f"sync rc={rc}: {line(s)}" + ("" if rc == 0 else "\n" + clip(ANSI.sub("", err or out), 1500))


def cmd_rollback(c, a):
    app = get_app(c, a.app)
    hid = int(a.id)
    hist = {h.get("id"): h for h in (app.get("status") or {}).get("history") or []}
    if hid not in hist:
        raise ArgoError(f"history id {hid} not found; available: {sorted(i for i in hist if i is not None)}")
    h = hist[hid]
    if not a.yes:
        return preview(c, a, f"rollback to id {hid}", [f"target rev={(h.get('revision') or '')[:10]} deployed={h.get('deployedAt', '')} prune={a.prune}",
                                                       "note: rollback needs auto-sync disabled on the app"])
    args = ["app", "rollback", app_arg(a.app), str(hid)] + (["--prune"] if a.prune else [])
    rc, out, err = c.run(args, timeout=180)
    return f"rollback rc={rc}: {line(summ(get_app(c, a.app)))}" + ("" if rc == 0 else "\n" + clip(ANSI.sub("", err or out), 1500))


def cmd_fetch(c, a):
    """Link card for the web UI: ONE JSON {title, status, status_emoji, url, fields}."""
    ident = json.loads(a.ident)
    name, ns = valid_name(ident.get("name", "")), ident.get("ns") or None
    if a.kind == "application":
        ref = f"{valid_ns(ns)}/{name}" if ns else name
        s = summ(get_app(c, ref))
        url = f"{c.base_url}/applications/{s['namespace']}/{s['name']}" if s["namespace"] else f"{c.base_url}/applications/{s['name']}"
        return json.dumps({"title": s["name"], "url": url, "status": f"{s['health']}/{s['sync']}",
                           "status_emoji": HEALTH_EMOJI.get(s["health"], ""),
                           "fields": {"project": s["project"], "health": s["health"], "sync": s["sync"],
                                      "revision": s["revision"], "cluster": s["cluster"]}})
    if a.kind == "applicationset":
        x = c.json(["appset", "get", f"{valid_ns(ns)}/{name}" if ns else name])
        spec, st = x.get("spec") or {}, x.get("status") or {}
        bad = [k for k in st.get("conditions") or [] if k.get("status") == "False" and k.get("type") in ("ErrorOccurred", "ResourcesUpToDate")]
        return json.dumps({"title": name, "url": f"{c.base_url}/applicationsets/{name}", "status": "error" if bad else "ok",
                           "status_emoji": "💔" if bad else "💚",
                           "fields": {"project": ((spec.get("template") or {}).get("spec") or {}).get("project", ""), "status": "error" if bad else "ok",
                                      "generators": ",".join(sorted({k for g in spec.get("generators") or [] for k in g})),
                                      "applications": len(st.get("applicationStatus") or [])}})
    if a.kind == "project":
        if c.project and name != c.project:
            raise ArgoError("project outside this instance's scope")
        spec = (c.json(["proj", "get", name]) or {}).get("spec") or {}
        return json.dumps({"title": name, "url": f"{c.base_url}/settings/projects/{name}", "status": "",
                           "fields": {"description": spec.get("description", ""), "destinations": len(spec.get("destinations") or []),
                                      "sources": len(spec.get("sourceRepos") or [])}})
    raise ArgoError(f"unknown kind {a.kind!r}")


def cmd_test(_c, a):
    """Per-step connection test for Settings -> Test; never raises, never prints credentials."""
    steps = []
    out = {"ok": False, "message": "", "steps": steps}

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    def fail(msg):
        out["message"] = redact(msg)
        return json.dumps(out)

    try:
        name, base, opts = argocdconfig.load_instance(a.instance)
        step("config", True, f"instance {name} -> {base}")
    except argocdconfig.InstanceError as exc:
        step("config", False, str(exc))
        return fail(str(exc))
    c = ArgoClient(base, opts)
    try:
        v = c.json(["version", "--client"], timeout=20)
        step("binary", True, f"argocd {(v or {}).get('client', {}).get('Version', '?') if isinstance(v, dict) else '?'}")
    except ArgoError as exc:
        step("binary", False, str(exc))
        return fail(f"argocd CLI problem: {exc}")
    try:
        v = c.json(["version"], timeout=30)
        step("server", True, f"server {((v or {}).get('server') or {}).get('Version', '?')}")
    except ArgoError as exc:
        step("server", False, str(exc))
        return fail(f"{base} unreachable via argocd CLI: check base_url / VPN / option insecure")
    try:
        u = c.json(["account", "get-user-info"], timeout=30) or {}
        if not u.get("loggedIn"):
            raise ArgoError("not logged in")
        step("session", True, f"user {u.get('username', '?')}")
    except ArgoError as exc:
        step("session", False, str(exc))
        return fail(f"no valid argocd login for {c.host}: run `argocd login {c.host} --sso` (or --username) in a terminal")
    try:
        args = ["app", "list"] + (["-p", c.project] if c.project else []) + (["-l", c.selector] if c.selector else [])
        n = len(c.json(args, timeout=60) or [])
        step("apps", True, f"{n} app(s) visible" + (f" in project {c.project}" if c.project else ""))
    except ArgoError as exc:
        step("apps", False, str(exc))
        return fail(f"logged in but cannot list apps: {exc}")
    out.update(ok=True, message=f"logged in as {u.get('username', '?')}, {n} app(s) visible")
    return json.dumps(out)


CMDS = {"apps": cmd_apps, "app": cmd_app, "tree": cmd_tree, "diff": cmd_diff, "history": cmd_history,
        "problems": cmd_problems, "logs": cmd_logs, "appsets": cmd_appsets, "appset": cmd_appset,
        "projects": cmd_projects, "project": cmd_project, "clusters": cmd_clusters, "repos": cmd_repos,
        "refresh": cmd_refresh, "sync": cmd_sync, "rollback": cmd_rollback, "fetch": cmd_fetch}


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--instance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test")
    p = sub.add_parser("apps")
    p.add_argument("--project"); p.add_argument("--selector"); p.add_argument("--limit", type=int, default=100)
    p.add_argument("--out-of-sync", action="store_true"); p.add_argument("--unhealthy", action="store_true")
    for n in ("app", "problems"):
        sub.add_parser(n).add_argument("app")
    p = sub.add_parser("tree")
    p.add_argument("app"); p.add_argument("--unhealthy", action="store_true"); p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("diff")
    p.add_argument("app"); p.add_argument("--revision"); p.add_argument("--max-chars", type=int, default=6000)
    p = sub.add_parser("history")
    p.add_argument("app"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("logs")
    p.add_argument("app"); p.add_argument("--kind"); p.add_argument("--resource-name"); p.add_argument("--namespace")
    p.add_argument("--container"); p.add_argument("--tail", type=int, default=100); p.add_argument("--grep")
    p.add_argument("--previous", action="store_true"); p.add_argument("--max-chars", type=int, default=6000)
    for n in ("appsets", "projects", "clusters", "repos"):
        sub.add_parser(n)
    for n in ("appset", "project"):
        sub.add_parser(n).add_argument("name")
    p = sub.add_parser("refresh")
    p.add_argument("app"); p.add_argument("--hard", action="store_true"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("sync")
    p.add_argument("app"); p.add_argument("--revision"); p.add_argument("--prune", action="store_true")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("rollback")
    p.add_argument("app"); p.add_argument("id", type=int); p.add_argument("--prune", action="store_true"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("fetch")
    p.add_argument("--kind", required=True); p.add_argument("--ident", required=True)
    return ap


def main(argv=None, client=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(cmd_test(None, a))
            return 0
        if client is None:
            _name, base, opts = argocdconfig.load_instance(a.instance)
            client = ArgoClient(base, opts)
        print(CMDS[a.cmd](client, a))
    except (ArgoError, argocdconfig.InstanceError, ValueError, re.error) as e:
        print(f"ERROR: {redact(str(e))[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
