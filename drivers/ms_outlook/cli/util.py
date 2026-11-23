"""Output helpers, short refs (m1, e1, c1 ...) and HTML-to-text."""
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import quote

import api


def q(s):
    return quote(str(s), safe="")


def _cache_path(account):
    d = os.path.join(api.ROOT, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, (account or "default") + ".json")


def save_refs(account, prefix, ids):
    p = _cache_path(account)
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    for i, v in enumerate(ids, 1):
        data[f"{prefix}{i}"] = v
    with open(p, "w") as f:
        json.dump(data, f)
    os.chmod(p, 0o600)


def resolve(account, ref):
    """m3 / e2 style short ref from the last list output, or a full Graph id."""
    if re.fullmatch(r"[med]\d{1,3}", ref):
        try:
            with open(_cache_path(account)) as f:
                return json.load(f)[ref]
        except (OSError, ValueError, KeyError):
            raise api.OutlookError(f"unknown ref {ref}: run the list/search command first")
    return ref


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(html):
    p = _Text()
    p.feed(html)
    t = "".join(p.out).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + f"... [+{len(s) - n} chars, use --full]"


def addr(r):
    e = (r or {}).get("emailAddress", {})
    n, a = e.get("name"), e.get("address")
    return f"{n} <{a}>" if n and a and n != a else (a or n or "")


def table(rows, cols, as_json=False):
    """rows: list of dicts. Compact TSV table, or JSON lines with --json."""
    if as_json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return
    if not rows:
        print("(none)")
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r.get(c) is None else str(r[c]).replace("\n", " ") for c in cols))


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)
