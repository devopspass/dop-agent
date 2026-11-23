"""Output helpers and short refs (f1, f2 ... from the last list/search) for the SharePoint/OneDrive CLI."""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.join(os.path.expanduser("~"), ".sharepoint-cli")
_account = {"name": None}


def set_account(a):
    _account["name"] = a


def _cache_path():
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._@\-]", "_", _account["name"] or "default")
    return os.path.join(ROOT, f"refs-{safe}.json")


def load_refs():
    try:
        with open(_cache_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_refs(refs):
    """Replace the cache with {f1: 'driveId:itemId'}; atomic, 0600."""
    p = _cache_path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
    with os.fdopen(fd, "w") as f:
        json.dump(refs, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def human(n):
    n = int(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def person(by):
    u = (by or {}).get("user") or {}
    return u.get("displayName") or u.get("email") or ""


def item_row(it):
    """Compact dict for one driveItem."""
    pr = it.get("parentReference") or {}
    path = (pr.get("path") or "").split("root:", 1)[-1]
    full = f"{path}/{it.get('name', '')}" if path or pr.get("path") else it.get("name", "")
    did = pr.get("driveId") or ""
    return {"type": "folder" if "folder" in it else "file", "name": it.get("name", ""), "path": full,
            "size": human(it.get("size")) if "folder" not in it else f"{(it.get('folder') or {}).get('childCount', 0)} items",
            "modified": (it.get("lastModifiedDateTime") or "")[:16].replace("T", " "),
            "by": person(it.get("lastModifiedBy")), "id": f"{did}:{it.get('id', '')}" if did else it.get("id", ""),
            "url": it.get("webUrl", "")}


def print_items(items, as_json=False, cols=("ref", "type", "name", "size", "modified", "by")):
    """Rows with refs f1..fN (saved for later commands)."""
    rows, refs = [], {}
    for i, it in enumerate(items, 1):
        r = item_row(it)
        r["ref"] = f"f{i}"
        refs[r["ref"]] = r["id"]
        rows.append(r)
    if refs:
        save_refs(refs)
    table(rows, list(cols) + (["path", "id", "url"] if as_json else []), as_json)
    return rows


def table(rows, cols, as_json=False):
    if as_json:
        for r in rows:
            print(json.dumps({c: r.get(c) for c in cols if c in r}, ensure_ascii=False))
        return
    if not rows:
        print("(none)")
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r.get(c) is None else str(r[c]).replace("\n", " ").replace("\t", " ") for c in cols))


def trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n... [+{len(s) - n} chars, use --full or --chars]"


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)
