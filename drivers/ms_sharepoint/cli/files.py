"""SharePoint/OneDrive CLI commands. Reads are free; writes (upload, cell-update, link-create) are draft-first:
without --yes they only print what WOULD happen. Output is compact (TSV or --json lines); file content is bounded.
"""
import json
import os
import re
import sys
import tempfile
from urllib.parse import quote

import api
import extract
import util

SELECT = "id,name,size,webUrl,lastModifiedDateTime,lastModifiedBy,parentReference,folder,file"
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
UPLOAD_MAX = 250 * 1024 * 1024
CHUNK = 320 * 1024 * 10
MAX_CELLS = 1000


def _base(a):
    return api.drive_base(getattr(a, "site", None), getattr(a, "drive", None))


def _item(a, target):
    return api.item_path(target, _base(a), util.load_refs())


def _sel(path):
    return path + ("&" if "?" in path else "?") + "$select=" + SELECT


def _meta(a, target):
    return api.request(_sel(_item(a, target)))


def cmd_whoami(a):
    me = api.request("/me?$select=displayName,mail,userPrincipalName")
    c = api.claims()
    scopes = [s for s in c["scp"].split() if re.match(r"(Files|Sites)\.", s)]
    print(f"as={me.get('userPrincipalName')} name={me.get('displayName')} tenant={c['tid']} file_scopes={','.join(scopes) or 'none-seen'}")


def cmd_sites(a):
    if a.query:
        items = api.get_all(f"/sites?search={quote(a.query)}&$top={min(a.top, 50)}", a.top)
    else:
        items = api.get_all("/me/followedSites", a.top)
    rows = [{"name": s.get("displayName") or s.get("name"), "url": s.get("webUrl"), "id": s.get("id")} for s in items]
    util.table(rows, ["name", "url", "id"], a.json)


def cmd_drives(a):
    src = api.site_path(a.site) + "/drives" if a.site else "/me/drives"
    rows = [{"name": d.get("name"), "type": d.get("driveType"), "url": d.get("webUrl"), "id": d.get("id")}
            for d in api.get_all(src, a.top)]
    util.table(rows, ["name", "type", "url", "id"], a.json)


def cmd_ls(a):
    parent = _item(a, a.target)
    items = api.get_all(_sel(parent + "/children") + f"&$top={min(a.top, 200)}", a.top)
    util.print_items(items, a.json)


def cmd_search(a):
    qs = a.query.replace("'", "''")
    path = f"{_base(a)}/root/search(q='{quote(qs, safe='')}')?$top={min(a.top, 100)}&$select={SELECT}"
    util.print_items(api.get_all(path, a.top), a.json, ("ref", "type", "name", "path", "modified", "by"))


def cmd_recent(a):
    util.print_items(api.get_all(f"/me/drive/recent?$top={min(a.top, 100)}", a.top), a.json,
                     ("ref", "type", "name", "modified", "by"))


def cmd_shared(a):
    items = []
    for it in api.get_all(f"/me/drive/sharedWithMe?$top={min(a.top, 100)}", a.top):
        rem = it.get("remoteItem") or it
        rem.setdefault("name", it.get("name"))
        items.append(rem)
    util.print_items(items, a.json, ("ref", "type", "name", "modified", "by"))


def cmd_info(a):
    m = _meta(a, a.target)
    r = util.item_row(m)
    r.update(created_by=util.person(m.get("createdBy")), etag=m.get("eTag"), mime=(m.get("file") or {}).get("mimeType", ""))
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
        return
    for k in ("type", "name", "path", "size", "modified", "by", "created_by", "mime", "id", "url"):
        print(f"{k}: {r.get(k, '')}")


def cmd_versions(a):
    vs = api.get_all(_item(a, a.target) + "/versions", a.top)
    rows = [{"version": v.get("id"), "modified": (v.get("lastModifiedDateTime") or "")[:16].replace("T", " "),
             "by": util.person(v.get("lastModifiedBy")), "size": util.human(v.get("size"))} for v in vs]
    util.table(rows, ["version", "modified", "by", "size"], a.json)


def cmd_read(a):
    name, data = api.download(_item(a, a.target), limit=a.max_bytes)
    try:
        if extract.kind_of(name) == "xlsx":
            if a.sheets:
                util.table([{**s} for s in extract.xlsx_sheets(data)], ["name", "state", "used", "cells"], a.json)
                return
            res = extract.xlsx_range(data, a.sheet, a.range, a.max_rows, a.max_cols)
            if a.json:
                print(json.dumps(res, ensure_ascii=False))
            else:
                print(util.trunc(extract.render_table(res), a.chars if not a.full else 10 ** 9))
            return
        text = extract.to_text(name, data, a.timestamps)
    except extract.ExtractError as e:
        util.die(str(e))
    print(util.trunc(text, 10 ** 9 if a.full else a.chars))


def cmd_download(a):
    name, data = api.download(_item(a, a.target), limit=a.max_bytes)
    out = os.path.abspath(a.out or os.path.join(tempfile.gettempdir(), "sharepoint-dl"))
    os.makedirs(out, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", os.path.basename(name)) or "file"
    dest = os.path.join(out, safe)
    fd, tmp = tempfile.mkstemp(dir=out)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    print(f"saved {dest} ({util.human(len(data))})")


def cmd_link(a):
    item = _item(a, a.target)
    m = api.request(_sel(item))
    print(f"url: {m.get('webUrl')}")
    try:
        perms = api.get_all(item + "/permissions", 50)
    except api.GraphError as e:
        print(f"(sharing links not readable: {str(e)[:100]})")
        return
    rows = []
    for p in perms:
        ln = p.get("link") or {}
        if ln:
            rows.append({"type": ln.get("type"), "scope": ln.get("scope"), "roles": ",".join(p.get("roles") or []),
                         "url": ln.get("webUrl")})
    util.table(rows, ["type", "scope", "roles", "url"], a.json)


def cmd_link_create(a):
    m = _meta(a, a.target)
    plan = f"create a '{a.type}' sharing link with scope '{a.scope}' for {util.item_row(m)['path']} ({m.get('webUrl')})"
    if a.scope == "anonymous" and not a.allow_anonymous:
        util.die("anonymous links need --allow-anonymous plus --yes after explicit user approval")
    if not a.yes:
        print("DRAFT (nothing created): would " + plan + ". Re-run with --yes to create.")
        return
    res = api.request(_item(a, a.target) + "/createLink", "POST", {"type": a.type, "scope": a.scope})
    print("created: " + (res.get("link") or {}).get("webUrl", "?"))


def _remote(a, to, local):
    p = "/" + to.strip("/")
    if to.endswith("/") or to in ("", "/"):
        p = p.rstrip("/") + "/" + os.path.basename(local)
    return p


def cmd_upload(a):
    if not os.path.isfile(a.local):
        util.die(f"local file not found: {a.local}")
    size = os.path.getsize(a.local)
    if size > UPLOAD_MAX:
        util.die(f"file is {util.human(size)}, over the {util.human(UPLOAD_MAX)} limit")
    rp = _remote(a, a.to, a.local)
    target = f"{_base(a)}/root:{quote(rp, safe='/')}:"
    try:
        old = api.request(_sel(target))
    except api.GraphError as e:
        if "HTTP 404" not in str(e):
            raise
        old = None
    mode = "replace" if a.replace else "fail"
    state = (f"EXISTS (modified {util.item_row(old)['modified']} by {util.item_row(old)['by']}; "
             + ("will be replaced, a new version is kept" if a.replace else "upload will fail without --replace") + ")") \
        if old else "new file"
    print(f"{'UPLOAD' if a.yes else 'DRAFT (nothing uploaded)'}: {a.local} ({util.human(size)}) -> {rp} [{state}]")
    if not a.yes:
        print("Re-run with --yes to upload.")
        return
    if old and not a.replace:
        util.die("target exists; pass --replace to overwrite (SharePoint keeps the old version)")
    with open(a.local, "rb") as f:
        data = f.read()
    if size <= SIMPLE_UPLOAD_MAX:
        res = api.request(f"{target}/content?@microsoft.graph.conflictBehavior={mode}", "PUT", data=data,
                          headers={"Content-Type": "application/octet-stream"})
    else:
        sess = api.request(f"{target}/createUploadSession", "POST", {"item": {"@microsoft.graph.conflictBehavior": mode}})
        res = None
        for off in range(0, size, CHUNK):
            chunk = data[off:off + CHUNK]
            raw = api._fetch_url(sess["uploadUrl"], {"Content-Range": f"bytes {off}-{off + len(chunk) - 1}/{size}",
                                                     "Content-Length": str(len(chunk))}, "PUT", chunk)
            if raw and raw[:1] == b"{":
                res = json.loads(raw)
    print("uploaded: " + ((res or {}).get("webUrl") or rp))


def _dims(rng):
    c1, r1, c2, r2 = extract.parse_range(rng)
    return r2 - r1 + 1, c2 - c1 + 1


def cmd_cell_update(a):
    if not re.fullmatch(r"[^\\/?*:\[\]'\"]{1,31}", a.sheet):
        util.die("bad --sheet name")
    c1, r1, c2, r2 = extract.parse_range(a.range)
    rng = f"{extract.col_name(c1)}{r1}:{extract.col_name(c2)}{r2}"
    raw = open(a.values_file).read() if a.values_file else a.values
    try:
        values = json.loads(raw or "")
    except ValueError:
        util.die("--values must be a JSON 2D array, e.g. '[[\"a\",1],[\"b\",2]]'")
    rows, cols = _dims(rng)
    if not (isinstance(values, list) and len(values) == rows and all(isinstance(r, list) and len(r) == cols for r in values)):
        util.die(f"--values must be a {rows}x{cols} array for range {rng}")
    if rows * cols > MAX_CELLS:
        util.die(f"at most {MAX_CELLS} cells per update")
    url = f"{_item(a, a.target)}/workbook/worksheets/{quote(a.sheet, safe='')}/range(address='{rng}')"
    cur = api.request(url + "?$select=values")
    print(f"{'UPDATE' if a.yes else 'DRAFT (nothing written)'}: {a.sheet}!{rng} in {a.target}")
    for i, (old, new) in enumerate(zip(cur.get("values", []), values)):
        print(f"  row {r1 + i}: {json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}")
    if not a.yes:
        print("Re-run with --yes to write (the workbook keeps its version history).")
        return
    api.request(url, "PATCH", {"values": values})
    print("updated")
