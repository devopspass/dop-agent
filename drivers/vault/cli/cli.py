#!/usr/bin/env python3
"""HashiCorp Vault CLI — METADATA ONLY (never reads secret values) for one dop-agent `vault` integration instance.

Global: --instance NAME (config.yaml integrations.<name>; optional when exactly one is enabled).
  health | seal-status            server state (active/standby/sealed/...), version, cluster
  auth-methods | mounts           enabled auth methods / secret engines (type, KV version, accessor)
  policies [NAME]                 list ACL policies, or print one policy's text (rules, not secrets)
  ls <mount/path>                 list secret PATH NAMES (keys only; KV v2 via metadata)
  meta <mount/path>               KV v2 metadata: versions, created/updated, deleted/destroyed, custom_metadata
  whoami                          own token metadata (policies, ttl, expire_time) — token value is never shown
  caps <path> [<path>...]         capabilities of own token on paths (checks permissions, reads nothing)
  leases [prefix] [--within H] [--limit N]   lease ids/ttl/expiry below a prefix (needs sys/leases policy)
  audit                           audit devices
  raw GET|LIST <path>             guarded read of an allow-listed metadata endpoint
dop-agent:  test | fetch --kind secret_meta --ident '{"path": "secret/app/db"}'
Hard guard: endpoints that return secret data (/data/, /creds/, /decrypt/, /sign/, /read, unwrap, sys/raw, ...)
are refused before any request is sent. No writes. See README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vaultconfig  # noqa: E402
from client import HEALTH_STATES, ApiError, Client, clean_path, lease_rows, redact, token_info  # noqa: E402

MAX_RAW_CHARS = 20000


def out(obj) -> None:
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=1))


def _mounts(c) -> dict:
    d = c.get("sys/mounts")
    return {k.rstrip("/"): v for k, v in (d.get("data") or d).items() if isinstance(v, dict)}


def split_mount(c, path: str):
    """(mount, rest, mount_info) using the longest mount prefix of path; mount_info may be {}."""
    p = clean_path(path).strip("/")
    best = ""
    mounts = _mounts(c)
    for m in mounts:
        if (p == m or p.startswith(m + "/")) and len(m) > len(best):
            best = m
    return best, p[len(best):].strip("/"), mounts.get(best, {})


def kv_version(info: dict) -> str:
    return str((info.get("options") or {}).get("version") or "1") if info.get("type") in ("kv", "generic") else ""


def cmd_health(c, a):
    status, d = c.health()
    out({"state": HEALTH_STATES.get(status, f"http-{status}"), "initialized": d.get("initialized"),
         "sealed": d.get("sealed"), "version": d.get("version"), "cluster": d.get("cluster_name"),
         "replication_dr": d.get("replication_dr_mode"), "http": status})


def cmd_seal(c, a):
    d = c.get("sys/seal-status")
    keep = ("type", "initialized", "sealed", "t", "n", "progress", "version", "cluster_name", "storage_type", "ha_enabled")
    out({k: d.get(k) for k in keep if k in d})


def cmd_auth(c, a):
    d = c.get("sys/auth")
    for k, v in sorted((d.get("data") or d).items()):
        if isinstance(v, dict):
            print(f"{k:30} {v.get('type', ''):12} {v.get('description', '')[:60]}")


def cmd_mounts(c, a):
    for k, v in sorted(_mounts(c).items()):
        ver = kv_version(v)
        print(f"{k + '/':30} {v.get('type', ''):12} {('v' + ver) if ver else '':3} {v.get('description', '')[:50]}")


def cmd_policies(c, a):
    if a.name:
        d = c.get(f"sys/policies/acl/{clean_path(a.name)}")
        print((d.get("data") or d).get("policy", ""))
        return
    for n in sorted(c.list("sys/policies/acl")):
        print(n)


def cmd_ls(c, a):
    mount, rest, info = split_mount(c, a.path)
    if not mount:
        raise ApiError(f"no secret mount found for {a.path!r}; see `mounts`")
    target = f"{mount}/metadata/{rest}" if kv_version(info) == "2" else f"{mount}/{rest}"
    keys = c.list(target.rstrip("/") + "/")
    for k in keys[: a.limit]:
        print(k)
    if len(keys) > a.limit:
        print(f"... {len(keys) - a.limit} more (--limit)")


def meta_of(c, path: str) -> dict:
    mount, rest, info = split_mount(c, path)
    if not mount or not rest:
        raise ApiError("meta needs <mount>/<secret path> of a KV v2 mount")
    if kv_version(info) != "2":
        raise ApiError(f"mount {mount!r} is not KV v2: no metadata endpoint (use `ls`)")
    d = c.get(f"{mount}/metadata/{rest}").get("data") or {}
    vers = d.get("versions") or {}
    keep = sorted(vers, key=int)[-10:]
    return {"path": f"{mount}/{rest}", "current_version": d.get("current_version"),
            "oldest_version": d.get("oldest_version"), "max_versions": d.get("max_versions"),
            "created_time": d.get("created_time"), "updated_time": d.get("updated_time"),
            "cas_required": d.get("cas_required"), "delete_version_after": d.get("delete_version_after"),
            "custom_metadata": d.get("custom_metadata"),
            "versions": {k: {"created_time": vers[k].get("created_time"), "deletion_time": vers[k].get("deletion_time") or None,
                             "destroyed": vers[k].get("destroyed")} for k in keep}}


def cmd_meta(c, a):
    out(meta_of(c, a.path))


def cmd_whoami(c, a):
    out(token_info(c))


def cmd_caps(c, a):
    paths = [clean_path(p) for p in a.paths]
    d = c.request("POST", "sys/capabilities-self", {"paths": paths})[1]
    for p in paths:
        print(f"{p:50} {','.join(d.get(p) or (d.get('data') or {}).get(p) or ['?'])}")


def cmd_leases(c, a):
    prefix = clean_path(a.prefix) if a.prefix else ""
    if not a.prefix:
        out(c.get("sys/leases/count") if a.count else {"hint": "pass a lease prefix (e.g. a mount path) or --count"})
        return
    for lid, ttl, exp, ren in sorted(lease_rows(c, prefix, a.limit, a.within), key=lambda r: str(r[2])):
        print(f"{lid:70} ttl={ttl} expires={exp} renewable={ren}")


def cmd_audit(c, a):
    d = c.get("sys/audit")
    for k, v in sorted((d.get("data") or d).items()):
        if isinstance(v, dict):
            print(f"{k:25} {v.get('type', ''):10} {v.get('description', '')[:60]}")


def cmd_raw(c, a):
    method = a.method.upper()
    if method not in ("GET", "LIST"):
        raise ApiError("raw supports only GET|LIST (read-only, metadata-only)")
    text = json.dumps(c.request(method, a.path)[1], indent=1)
    print(text[:MAX_RAW_CHARS] + ("\n... truncated" if len(text) > MAX_RAW_CHARS else ""))


def fetch_card(c, kind: str, ident: dict) -> dict:
    if kind != "secret_meta":
        raise ValueError(f"unknown kind {kind!r}")
    m = meta_of(c, str(ident.get("path") or ""))
    return {"title": m["path"], "status": f"v{m['current_version']}", "status_emoji": "🔐",
            "fields": {"updated": m["updated_time"], "created": m["created_time"], "max_versions": m["max_versions"]}}


def run_test(name):
    steps, set_options = [], {}

    def step(n, ok, msg):
        steps.append({"name": n, "ok": ok, "message": redact(msg)})
        return ok

    def done(ok, msg):
        return {"ok": ok, "message": msg, "steps": steps, "set_options": set_options}

    try:
        inst = vaultconfig.load_instance(name)
    except vaultconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done(False, str(exc))
    step("config", True, f"{inst.name} -> {inst.base_url}" + (f" ns={inst.namespace}" if inst.namespace else ""))
    c = Client(inst)
    try:
        status, d = c.health()
        state = HEALTH_STATES.get(status, f"http-{status}")
        step("health", status in (200, 429, 472, 473), f"{state}, version {d.get('version', '?')}")
        if status not in (200, 429, 472, 473):
            return done(False, f"Vault is {state}")
    except ApiError as exc:
        step("health", False, str(exc))
        return done(False, str(exc))
    try:
        t = token_info(c)
        step("auth", True, f"token ok, policies={t.get('policies')}, ttl={t.get('ttl')}s")
    except ApiError as exc:
        step("auth", False, f"{exc}; check auth.token validity")
        return done(False, str(exc))
    try:
        n = len(_mounts(c))
        step("mounts", True, f"{n} secret mounts visible")
    except ApiError as exc:
        step("mounts", False, f"{exc} (token needs read on sys/mounts)")
    ok = all(s["ok"] for s in steps)
    return done(ok, "Vault reachable, token valid (metadata-only)" if ok else "Vault reachable, some metadata not permitted")


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="vault integration instance (env VAULT_INSTANCE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(n):
        return sub.add_parser(n)

    add("test")
    s = add("fetch"); s.add_argument("--kind", required=True); s.add_argument("--ident", required=True)
    add("health"); add("seal-status"); add("auth-methods"); add("mounts"); add("whoami"); add("audit")
    s = add("policies"); s.add_argument("name", nargs="?")
    s = add("ls"); s.add_argument("path"); s.add_argument("--limit", type=int, default=200)
    s = add("meta"); s.add_argument("path")
    s = add("caps"); s.add_argument("paths", nargs="+")
    s = add("leases"); s.add_argument("prefix", nargs="?"); s.add_argument("--within", type=float, help="only leases expiring within H hours")
    s.add_argument("--limit", type=int, default=100); s.add_argument("--count", action="store_true")
    s = add("raw"); s.add_argument("method"); s.add_argument("path")
    return p


COMMANDS = {"health": cmd_health, "seal-status": cmd_seal, "auth-methods": cmd_auth, "mounts": cmd_mounts,
            "policies": cmd_policies, "ls": cmd_ls, "meta": cmd_meta, "whoami": cmd_whoami, "caps": cmd_caps,
            "leases": cmd_leases, "audit": cmd_audit, "raw": cmd_raw}


def main(argv=None) -> int:
    a = build().parse_args(argv)
    try:
        if a.cmd == "test":
            print(json.dumps(run_test(a.instance)))
            return 0
        c = Client(vaultconfig.load_instance(a.instance))
        if a.cmd == "fetch":
            print(json.dumps(fetch_card(c, a.kind, json.loads(a.ident))))
            return 0
        return COMMANDS[a.cmd](c, a) or 0
    except (ApiError, vaultconfig.InstanceError, ValueError) as exc:
        print(f"ERROR: {redact(str(exc))[:400]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
