#!/usr/bin/env python3
"""SharePoint / OneDrive watcher for dop-agent `script` watches (read-only, token-free for the model).

Modes (first argument):
  folder  --path /Recordings [--site URL|--drive ID] [--recursive] [--new-only] [--name REGEX] [--ext vtt,docx]
          new / changed files directly in a folder (or up to 3 levels with --recursive; scan capped at 500 items)
  file    --target <url|driveId:itemId|/path> [--site URL|--drive ID]      one file got a new version
  site    [--site URL|--drive ID] [--path-prefix /Folder] [--name REGEX]   anything changed in a whole library (Graph delta)
Output/limits: --limit N=10 lines.  Env: SHAREPOINT_CDP_URL, SHAREPOINT_ACCOUNT (set by the driver instance),
WATCH_STATE_DIR (ids/etags/cursors only, never file content).
Exit codes: 0 nothing / baseline recorded / transient error (429, 5xx), 200 changes (wake text on stdout), 1 error.
The first run only records a baseline, so existing files are not reported.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parent.parent / "cli"
SELECT = "id,name,size,webUrl,lastModifiedDateTime,createdDateTime,lastModifiedBy,eTag,parentReference,folder,file,deleted"
SCAN_CAP = 500
DELTA_PAGES = 5


class Transient(Exception):
    pass


class AuthError(Exception):
    pass


_api_mod = {}


def _api_paths(args=None):
    """The driver's cli/api.py, loaded by path under a private name (other drivers also ship an `api` module)."""
    if "m" not in _api_mod:
        spec = importlib.util.spec_from_file_location("sharepoint_cli_api", CLI_DIR / "api.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _api_mod["m"] = mod
    return _api_mod["m"]


def _api():
    m = _api_paths()
    if not _api_mod.get("configured"):
        m.configure(os.environ.get("SHAREPOINT_CDP_URL"), os.environ.get("SHAREPOINT_ACCOUNT"))
        _api_mod["configured"] = True
    return m


def graph_get(path):
    """One read-only GET through the driver's api module. The only network seam (tests patch it)."""
    try:
        d = _api().request(path)
    except Exception as exc:  # GraphError, CDP/token failures
        out = _api().redact(exc)
        if re.search(r"\b(429|500|502|503|504)\b", out):
            raise Transient(out[:200]) from exc
        if re.search(r"\b(401|403)\b|expired|Unauthorized|not signed in|No unexpired token|CDP endpoint", out, re.I):
            raise AuthError("SharePoint auth failed (Teams tab signed out or Files/Sites scope missing): " + out[:200]) from exc
        raise RuntimeError(out[:300]) from exc
    if not isinstance(d, dict):
        raise RuntimeError("unexpected Graph reply")
    return d


def state_path(mode):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent/watch-state/sharepoint-default")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"sharepoint-{mode}.json"


def load_state(mode):
    p = state_path(mode)
    return json.loads(p.read_text()) if p.exists() else None


def save_state(mode, st):
    p = state_path(mode)
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def library(args):
    return _api_paths(args).drive_base(args.site, args.drive)


def who(it):
    return ((it.get("lastModifiedBy") or {}).get("user") or {}).get("displayName") or "?"


def line(tag, it):
    size = it.get("size")
    path = (it.get("parentReference") or {}).get("path", "").split("root:", 1)[-1]
    return (f"{tag} {path}/{it.get('name')} ({int(size) // 1024 if size else 0} KB) "
            f"{(it.get('lastModifiedDateTime') or '')[:16]}Z by {who(it)} {it.get('webUrl', '')}")


def wanted(it, args):
    if args.name and not re.search(args.name, it.get("name", ""), re.I):
        return False
    if args.ext and it.get("name", "").rsplit(".", 1)[-1].lower() not in args.ext.lower().split(","):
        return False
    return True


def scan_folder(args):
    """Files below the folder (bounded): {id: item}."""
    api = _api_paths(args)
    root = api.item_path(args.path, library(args))
    found, queue = {}, [(root, 0)]
    while queue and len(found) < SCAN_CAP:
        parent, depth = queue.pop(0)
        nxt = f"{parent}/children?$top=200&$select={SELECT}"
        while nxt and len(found) < SCAN_CAP:
            d = graph_get(nxt)
            for it in d.get("value", []):
                if "folder" in it:
                    if args.recursive and depth < 2:
                        queue.append((f"/drives/{it['parentReference']['driveId']}/items/{it['id']}", depth + 1))
                elif wanted(it, args):
                    found[it["id"]] = it
            nxt = d.get("@odata.nextLink")
    return found


def mode_folder(args):
    cur = scan_folder(args)
    snap = {i: it.get("eTag") or it.get("lastModifiedDateTime") for i, it in cur.items()}
    old = load_state("folder")
    save_state("folder", {"files": snap})
    if old is None:
        print(f"baseline recorded: {len(snap)} file(s)", file=sys.stderr)
        return []
    out = []
    for i, it in cur.items():
        if i not in old["files"]:
            out.append(line("NEW", it))
        elif old["files"][i] != snap[i] and not args.new_only:
            out.append(line("CHANGED", it))
    return out


def mode_file(args):
    api = _api_paths(args)
    it = graph_get(f"{api.item_path(args.target, library(args))}?$select={SELECT}")
    tag = it.get("eTag") or it.get("lastModifiedDateTime")
    old = load_state("file")
    save_state("file", {"tag": tag})
    if old is None:
        print("baseline recorded", file=sys.stderr)
        return []
    return [line("CHANGED", it)] if old.get("tag") != tag else []


def _ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def mode_site(args):
    base = library(args)
    st = load_state("site")
    if st is None:
        d = graph_get(f"{base}/root/delta?token=latest")
        link = _final_link(d)
        save_state("site", {"delta": link})
        print("baseline recorded", file=sys.stderr)
        return []
    out, nxt, pages = [], st["delta"], 0
    link = st["delta"]
    while nxt and pages < DELTA_PAGES:
        d = graph_get(nxt)
        pages += 1
        for it in d.get("value", []):
            if "folder" in it or "root" in it:
                continue
            path = (it.get("parentReference") or {}).get("path", "").split("root:", 1)[-1]
            if args.path_prefix and not path.startswith(args.path_prefix.rstrip("/")):
                continue
            if "deleted" in it:
                out.append(f"DELETED {it.get('name', '?')}")
            elif wanted(it, args):
                c, m = _ts(it.get("createdDateTime")), _ts(it.get("lastModifiedDateTime"))
                new = c and m and abs((m - c).total_seconds()) < 90
                out.append(line("NEW" if new else "CHANGED", it))
        nxt = d.get("@odata.nextLink")
        link = d.get("@odata.deltaLink") or link
    if nxt:  # pages left: keep the cursor so the rest is delivered next run
        link = nxt
    save_state("site", {"delta": link})
    return out


def _final_link(d, pages=DELTA_PAGES):
    while d.get("@odata.nextLink") and pages:
        d = graph_get(d["@odata.nextLink"])
        pages -= 1
    return d.get("@odata.deltaLink") or d.get("@odata.nextLink")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["folder", "file", "site"])
    ap.add_argument("--site")
    ap.add_argument("--drive")
    ap.add_argument("--path", default="/")
    ap.add_argument("--target")
    ap.add_argument("--path-prefix")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--new-only", action="store_true")
    ap.add_argument("--name", help="regex on the file name")
    ap.add_argument("--ext", help="comma-separated extensions, e.g. vtt,docx")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)
    if args.mode == "file" and not args.target:
        print("error: file mode needs --target", file=sys.stderr)
        return 1
    try:
        if args.name:
            re.compile(args.name)
        out = {"folder": mode_folder, "file": mode_file, "site": mode_site}[args.mode](args)
    except Transient as e:
        print(f"transient: {e}", file=sys.stderr)
        return 0
    except (AuthError, RuntimeError, re.error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not out:
        return 0
    print(f"SharePoint {args.mode}: {len(out)} change(s)")
    print("\n".join(out[:args.limit]))
    if len(out) > args.limit:
        print(f"... +{len(out) - args.limit} more")
    return 200


if __name__ == "__main__":
    sys.exit(main())
