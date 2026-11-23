#!/usr/bin/env python3
"""Artifactory CLI (REST, no jf CLI, no browser) for one dop-agent `artifactory` integration instance.

Global: --instance NAME (config.yaml integrations.<name>; optional when exactly one is enabled), --json.
Reads (anonymous when the server allows it, else with the instance token):
  repos [--type local|remote|virtual]     ls <repo/path> [-l]       info <repo/path>
  search <name-glob> [--repo R]           aql '<items.find(...)>'   get <repo/path> [-o FILE] [--force]
  verify <local-file> <repo/path>         raw GET <api/path>
Writes (print the plan; need --yes):
  upload <file> <repo/path/> [--name N] [--force] [--timeout S] --yes
  raw POST|PUT|DELETE <api/path> [--data S] --yes
Tokens:  token status | authorize | revoke <token_id>   (authorize: auth.type sso_token only)
dop-agent:  test | fetch --kind artifact --ident '{"path": "repo/dir/file"}'
See README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import afconfig  # noqa: E402
import uploader  # noqa: E402
from client import ApiError, Client, clean_path, sha256_of  # noqa: E402

MAX_RAW_CHARS = 20000


def out(obj) -> None:
    print(json.dumps(obj, indent=1) if not isinstance(obj, str) else obj)


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n or "")
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def info_of(client: Client, path: str) -> dict:
    r = client.storage(path)
    if r.status_code != 200:
        raise ApiError(f"HTTP {r.status_code} for {clean_path(path)}: {r.text[:200]}")
    return r.json()


def cmd_repos(c, a):
    r = c.get_json("api/repositories" + (f"?type={a.type}" if a.type else ""))
    for x in r:
        print(f"{x['key']:50} {x['type']:8} {x.get('packageType', '')}")


def cmd_ls(c, a):
    p = clean_path(a.path)
    d = c.get_json(f"api/storage/{p}?list&deep=0&listFolders=1&mdTimestamps=0")
    for f in d.get("files", []):
        name = f["uri"].lstrip("/")
        if f.get("folder"):
            print(f"{'DIR':>10}  {'':19}  {name}/")
        elif a.long:
            print(f"{human_size(f.get('size')):>10}  {f.get('lastModified', '')[:19]:19}  {name}")
        else:
            print(name)


def cmd_info(c, a):
    d = info_of(c, a.path)
    keys = ("repo", "path", "size", "created", "createdBy", "lastModified", "modifiedBy", "checksums", "downloadUri")
    out({k: d.get(k) for k in keys if k in d} if "children" not in d else
        {"repo": d.get("repo"), "path": d.get("path"), "folder": True, "children": len(d["children"]),
         "created": d.get("created"), "lastModified": d.get("lastModified")})


def cmd_search(c, a):
    q = "api/search/artifact?name=" + quote(a.name, safe="*?") + (f"&repos={quote(a.repo, safe=',')}" if a.repo else "")
    for x in c.get_json(q).get("results", [])[: a.limit]:
        print(x["uri"].split("/api/storage/", 1)[-1])


def cmd_aql(c, a):
    r = c.request("POST", "api/search/aql", data=a.query, headers={"Content-Type": "text/plain"})
    print(r.text[:MAX_RAW_CHARS] if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:300]}"
          + ("  (AQL needs a token: use auth.type access_token or sso_token)" if r.status_code in (401, 403) else ""))


def cmd_get(c, a):
    out_path = Path(a.output or Path(a.path).name)
    if out_path.exists() and not a.force:
        raise ApiError(f"{out_path} exists; use -o other-name or --force")
    r = c.request("GET", clean_path(a.path), stream=True, timeout=600)
    if r.status_code != 200:
        raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    print(out_path, out_path.stat().st_size, "bytes")


def cmd_verify(c, a):
    local = sha256_of(Path(a.file).expanduser())
    remote = info_of(c, a.path).get("checksums", {}).get("sha256")
    print("local ", local, "\nremote", remote)
    return 0 if local == remote else 1


def cmd_raw(c, a):
    method = a.method.upper()
    if method not in ("GET", "HEAD") and not a.yes:
        print(f"DRY RUN: {method} {a.path} not sent; re-run with --yes to really send it")
        return 0
    if a.path.startswith(("http://", "https://")):
        raise ApiError("pass an API path relative to /artifactory (e.g. api/repositories), not a URL")
    r = c.request(method, a.path, data=a.data)
    print(r.status_code)
    print(r.text[:MAX_RAW_CHARS])


def cmd_upload(c, a, inst):
    mode = inst.options.get("upload_mode") or "rest"
    if mode == "none":
        raise ApiError("uploads are disabled for this instance (option upload_mode=none)")
    p = uploader.plan(c, Path(a.file).expanduser(), a.target, a.name, a.force)
    print(f"plan: {mode} upload {p['size']} bytes -> {inst.base_url}/artifactory/{p['dest']}"
          + (" (OVERWRITES existing)" if p["existing"] else ""))
    if not a.yes:
        print("DRY RUN: nothing uploaded; re-run with --yes after the user confirmed target and name")
        return 0
    src = Path(a.file).expanduser()
    res = uploader.upload_rest(c, src, p)
    out(res)
    return 0 if res.get("sha256_match") else 1


def cmd_token(c, a, inst):
    opts = inst.options
    if a.action == "authorize":
        if inst.auth_type != "sso_token":
            raise ApiError("token authorize only works for auth.type sso_token (access_token is managed in config)")
        import sso_token
        try:
            res = sso_token.authorize(inst, opts.get("cdp_url") or "http://localhost:9222")
        except sso_token.SsoError as exc:
            raise ApiError(str(exc)) from None
        exp = res.get("expiry")
        print("token stored;", f"expires {time.strftime('%Y-%m-%d', time.localtime(exp))};" if exp else "",
              "previous token revoked" if res["revoked_previous"] else "no previous token revoked")
    elif a.action == "revoke":
        if not a.token_id:
            raise ApiError("token revoke needs <token_id>")
        import sso_token
        print("revoke HTTP", sso_token.revoke(inst, a.token_id))
    else:
        info = c.token_info(afconfig.load_state(inst.name).get("token_id") if inst.auth_type == "sso_token" else None)
        if info is None:
            print(f"no token (auth.type {inst.auth_type}); anonymous reads only")
            return
        exp = info.get("expiry")
        print("valid:", info.get("valid"), "| expires:",
              time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "unknown",
              "| days left:", int((exp - time.time()) / 86400) if exp else "?")


def fetch_card(c: Client, kind: str, ident: dict) -> dict:
    from urllib.parse import unquote
    if kind not in ("artifact", "artifact_ui"):
        raise ApiError(f"unknown kind {kind!r}")
    path = clean_path(unquote(ident.get("path", "")))
    d = info_of(c, path)
    url = f"{c.inst.api}/{path}"
    if "children" in d:
        return {"title": path, "url": url, "fields": {"repo": d.get("repo", ""), "type": "folder",
                                                       "items": str(len(d["children"])), "modified": d.get("lastModified", "")}}
    return {"title": path.rsplit("/", 1)[-1], "url": url,
            "fields": {"repo": d.get("repo", ""), "path": path, "size": human_size(d.get("size")),
                       "modified": d.get("lastModified", ""), "modifiedBy": d.get("modifiedBy", ""),
                       "sha256": (d.get("checksums") or {}).get("sha256", "")}}


def run_test(name: str | None) -> dict:
    steps = []

    def step(n, ok, detail=""):
        steps.append({"step": n, "ok": ok, "detail": detail})
        return ok

    def done(msg):
        return {"ok": all(s["ok"] for s in steps), "message": msg, "steps": steps}

    try:
        inst = afconfig.load_instance(name)
        step("config", True, f"instance {inst.name} -> {inst.base_url} (auth {inst.auth_type})")
    except afconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done(str(exc))
    if inst.auth_type == "sso_token" and not inst.token:
        step("token", False, "no token yet: run `token authorize` (browser SSO)")
        return done("sso_token instance has no token yet: run `token authorize`")
    c = Client(inst)
    try:
        r = c.ping()
    except ApiError as exc:
        step("reach", False, str(exc))
        return done(str(exc))
    if not step("reach", r.status_code == 200 and r.text.strip().upper() == "OK", f"ping HTTP {r.status_code}"):
        return done(f"{inst.base_url} did not answer the Artifactory ping (check base_url / VPN / proxy)")
    try:
        repos = c.get_json("api/repositories")
        step("read", True, f"{len(repos)} repositories visible")
    except ApiError as exc:
        step("read", False, str(exc)[:200])
        return done("repository listing failed (anonymous access disabled? use auth.type access_token)")
    if inst.token:
        info = c.token_info()
        exp = (info or {}).get("expiry")
        left = f", {int((exp - time.time()) / 86400)} days left" if exp else ""
        if not step("auth", bool(info and info.get("valid")), "token accepted" + left if info and info.get("valid")
                    else f"token rejected (HTTP {(info or {}).get('http', '?')})"):
            return done("token rejected by the server: check/renew it")
    mode = inst.options.get("upload_mode") or "rest"
    step("upload_config", True, f"upload_mode={mode}")
    return done(f"{len(repos)} repositories visible" + (" with token" if inst.token else " anonymously"))


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="artifactory integration instance (env ARTIFACTORY_INSTANCE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(n, **kw):
        return sub.add_parser(n, **kw)

    add("test")
    s = add("fetch"); s.add_argument("--kind", required=True); s.add_argument("--ident", required=True)
    s = add("repos"); s.add_argument("--type", choices=["local", "remote", "virtual"])
    s = add("ls"); s.add_argument("path"); s.add_argument("-l", "--long", action="store_true")
    s = add("info"); s.add_argument("path")
    s = add("search"); s.add_argument("name"); s.add_argument("--repo"); s.add_argument("--limit", type=int, default=100)
    s = add("aql"); s.add_argument("query")
    s = add("get"); s.add_argument("path"); s.add_argument("-o", "--output"); s.add_argument("--force", action="store_true")
    s = add("verify"); s.add_argument("file"); s.add_argument("path")
    s = add("raw"); s.add_argument("method"); s.add_argument("path"); s.add_argument("--data"); s.add_argument("--yes", action="store_true")
    s = add("upload"); s.add_argument("file"); s.add_argument("target"); s.add_argument("--name")
    s.add_argument("--force", action="store_true"); s.add_argument("--yes", action="store_true")
    s = add("token"); s.add_argument("action", choices=["status", "authorize", "revoke"]); s.add_argument("token_id", nargs="?")
    return p


def main(argv=None) -> int:
    a = build().parse_args(argv)
    try:
        if a.cmd == "test":
            print(json.dumps(run_test(a.instance)))
            return 0
        inst = afconfig.load_instance(a.instance)
        c = Client(inst)
        if a.cmd == "fetch":
            print(json.dumps(fetch_card(c, a.kind, json.loads(a.ident))))
            return 0
        fn = {"repos": cmd_repos, "ls": cmd_ls, "info": cmd_info, "search": cmd_search, "aql": cmd_aql,
              "get": cmd_get, "verify": cmd_verify, "raw": cmd_raw}.get(a.cmd)
        if fn:
            return fn(c, a) or 0
        return (cmd_upload if a.cmd == "upload" else cmd_token)(c, a, inst) or 0
    except (ApiError, afconfig.InstanceError, ValueError) as exc:
        print(f"ERROR: {str(exc)[:400]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
