"""Plain-text extraction from downloaded file bytes, stdlib only (zipfile + xml): docx, pptx, xlsx, vtt, text-like.

xlsx is parsed locally (cached values, no Excel session): `xlsx_sheets` lists sheets with their used range,
`xlsx_range` returns a bounded rectangle as rows of strings. Dates in xlsx stay as Excel serial numbers.
"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
ZIP_LIMIT = 200 * 1024 * 1024  # uncompressed size guard against zip bombs
TEXT_EXT = {"txt", "md", "csv", "tsv", "json", "xml", "log", "yaml", "yml", "html", "htm", "ini", "sql"}


class ExtractError(RuntimeError):
    pass


def _zip(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ExtractError("not an Office Open XML file (zip)")
    if sum(i.file_size for i in z.infolist()) > ZIP_LIMIT:
        raise ExtractError("archive too large to parse safely")
    return z


def _xml(z, name):
    try:
        return ET.fromstring(z.read(name))
    except KeyError:
        raise ExtractError(f"{name} missing")


def kind_of(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("docx", "pptx", "xlsx", "vtt"):
        return ext
    if ext in ("xlsm",):
        return "xlsx"
    return "text" if ext in TEXT_EXT else ext or "unknown"


# ---------- docx / pptx ----------

def _para_text(p, ns):
    out = []
    for e in p.iter():
        if e.tag == ns + "t" and e.text:
            out.append(e.text)
        elif e.tag in (ns + "tab",):
            out.append("\t")
        elif e.tag in (ns + "br",):
            out.append("\n")
    return "".join(out)


def docx_text(data):
    z = _zip(data)
    body = _xml(z, "word/document.xml").find(W + "body")
    lines = []
    for ch in (body if body is not None else []):
        if ch.tag == W + "p":
            t = _para_text(ch, W).strip()
            style = ch.find(f"{W}pPr/{W}pStyle")
            if t and style is not None and (style.get(W + "val") or "").lower().startswith("heading"):
                t = "# " + t
            if t:
                lines.append(t)
        elif ch.tag == W + "tbl":
            for tr in ch.iter(W + "tr"):
                cells = [" ".join(_para_text(p, W).strip() for p in tc.iter(W + "p")).strip()
                         for tc in tr.findall(W + "tc")]
                lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def pptx_text(data):
    z = _zip(data)
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    out = []
    for i, n in enumerate(slides, 1):
        paras = [_para_text(p, A).strip() for p in _xml(z, n).iter(A + "p")]
        out.append(f"## Slide {i}\n" + "\n".join(p for p in paras if p))
        num = re.search(r"(\d+)\.xml$", n).group(1)
        notes = f"ppt/notesSlides/notesSlide{num}.xml"
        if notes in z.namelist():
            nt = [_para_text(p, A).strip() for p in _xml(z, notes).iter(A + "p")]
            nt = [t for t in nt if t and not t.isdigit()]
            if nt:
                out.append("Notes: " + " ".join(nt))
    return "\n\n".join(out)


# ---------- xlsx ----------

def col_num(letters):
    n = 0
    for c in letters.upper():
        n = n * 26 + ord(c) - 64
    return n


def col_name(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_cell(ref):
    m = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d{1,7})", ref.strip())
    if not m:
        raise ExtractError(f"bad cell reference {ref!r}")
    return col_num(m[1]), int(m[2])


def parse_range(rng):
    """'A1:D20' | 'B3' -> (c1, r1, c2, r2)."""
    a, _, b = rng.partition(":")
    c1, r1 = parse_cell(a)
    c2, r2 = parse_cell(b or a)
    if c2 < c1 or r2 < r1:
        raise ExtractError(f"range {rng!r} must go top-left to bottom-right")
    return c1, r1, c2, r2


def _shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    return ["".join(t.text or "" for t in si.iter(S + "t")) for si in _xml(z, "xl/sharedStrings.xml").iter(S + "si")]


def _sheet_files(z):
    wb = _xml(z, "xl/workbook.xml")
    rels = {r.get("Id"): r.get("Target") for r in _xml(z, "xl/_rels/workbook.xml.rels")}
    out = []
    for s in wb.iter(S + "sheet"):
        t = rels.get(s.get(R + "id"), "")
        t = t.lstrip("/")
        out.append((s.get("name"), t if t.startswith("xl/") else "xl/" + t, s.get("state") or "visible"))
    return out


def _cells(z, path, sst, bounds=None):
    """Yield (col, row, text) for non-empty cells, optionally clipped to bounds=(c1,r1,c2,r2)."""
    for _, el in ET.iterparse(io.BytesIO(z.read(path)), events=("end",)):
        if el.tag != S + "c":
            continue
        ref = el.get("r") or ""
        m = re.fullmatch(r"([A-Z]+)(\d+)", ref)
        if m:
            c, r = col_num(m[1]), int(m[2])
            if not bounds or (bounds[0] <= c <= bounds[2] and bounds[1] <= r <= bounds[3]):
                t, v = el.get("t"), el.find(S + "v")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in el.iter(S + "t"))
                elif v is None or v.text is None:
                    val = ""
                elif t == "s":
                    val = sst[int(v.text)] if int(v.text) < len(sst) else ""
                elif t == "b":
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text
                if val != "":
                    yield c, r, val
        el.clear()


def xlsx_sheets(data):
    """[{name, state, used}] with the used range computed from the cells present."""
    z = _zip(data)
    sst = _shared_strings(z)
    out = []
    for name, path, state in _sheet_files(z):
        c1 = r1 = 10 ** 9
        c2 = r2 = 0
        n = 0
        for c, r, _ in _cells(z, path, sst):
            c1, r1, c2, r2, n = min(c1, c), min(r1, r), max(c2, c), max(r2, r), n + 1
        out.append({"name": name, "state": state, "cells": n,
                    "used": f"{col_name(c1)}{r1}:{col_name(c2)}{r2}" if n else ""})
    return out


def xlsx_range(data, sheet=None, rng=None, max_rows=50, max_cols=20):
    """Rectangle of one sheet (default: first visible) -> {sheet, range, rows:[[str]], truncated}."""
    z = _zip(data)
    files = _sheet_files(z)
    if not files:
        raise ExtractError("workbook has no sheets")
    pick = next((f for f in files if sheet and f[0].lower() == sheet.lower()), None) if sheet else \
        next((f for f in files if f[2] == "visible"), files[0])
    if not pick:
        raise ExtractError(f"sheet {sheet!r} not found; sheets: {[f[0] for f in files]}")
    sst = _shared_strings(z)
    bounds = parse_range(rng) if rng else (1, 1, 10 ** 6, 10 ** 7)
    grid, mc, mr, c0, r0 = {}, 0, 0, 10 ** 9, 10 ** 9
    for c, r, v in _cells(z, pick[1], sst, bounds):
        grid[(c, r)] = v
        c0, r0, mc, mr = min(c0, c), min(r0, r), max(mc, c), max(mr, r)
    if not grid and not rng:
        return {"sheet": pick[0], "range": "", "rows": [], "truncated": False}
    c1, r1 = (bounds[0], bounds[1]) if rng else (c0, r0)
    c2, r2 = (bounds[2], bounds[3]) if rng else (mc, mr)
    trunc = (r2 - r1 + 1 > max_rows) or (c2 - c1 + 1 > max_cols)
    r2, c2 = min(r2, r1 + max_rows - 1), min(c2, c1 + max_cols - 1)
    rows = [[grid.get((c, r), "") for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)]
    return {"sheet": pick[0], "range": f"{col_name(c1)}{r1}:{col_name(c2)}{r2}", "rows": rows, "truncated": trunc,
            "first_row": r1, "first_col": c1}


def render_table(res):
    """Compact text table with Excel column letters / row numbers."""
    if not res["rows"]:
        return f"[{res['sheet']}] (empty)"
    c1, r1 = res["first_col"], res["first_row"]
    head = "row\t" + "\t".join(col_name(c1 + i) for i in range(len(res["rows"][0])))
    lines = [f"[{res['sheet']}!{res['range']}]", head]
    for i, row in enumerate(res["rows"]):
        lines.append(f"{r1 + i}\t" + "\t".join(v.replace("\t", " ").replace("\n", " ") for v in row))
    if res["truncated"]:
        lines.append("... truncated (raise --max-rows/--max-cols or narrow --range)")
    return "\n".join(lines)


# ---------- vtt ----------

_TS = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})\.\d{3}\s+-->\s+")
_VTAG = re.compile(r"<v\s+([^>]*)>")


def vtt_text(data, timestamps=False):
    """WEBVTT -> 'Speaker: text' lines; consecutive cues of one speaker are merged."""
    text = data.decode("utf-8-sig", "replace")
    out, cur_spk, cur_txt, cur_ts, ts = [], None, [], "", ""
    for line in text.splitlines():
        line = line.strip()
        m = _TS.match(line)
        if m:
            ts = f"{int(m[1] or 0):02d}:{m[2]}:{m[3]}"
            continue
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")) or line.isdigit() \
                or re.fullmatch(r"[0-9a-f\-]{36}(/\d+-\d+)?", line):
            continue
        v = _VTAG.search(line)
        spk = v.group(1).strip() if v else None
        body = re.sub(r"<[^>]+>", "", line).strip()
        if not body:
            continue
        if spk is None or spk == cur_spk:
            cur_txt.append(body)
        else:
            if cur_txt:
                out.append((cur_ts, cur_spk, " ".join(cur_txt)))
            cur_spk, cur_txt, cur_ts = spk, [body], ts
        if cur_spk is None and spk is None and len(cur_txt) == 1:
            cur_ts = ts
    if cur_txt:
        out.append((cur_ts, cur_spk, " ".join(cur_txt)))
    return "\n".join((f"[{t}] " if timestamps and t else "") + (f"{s}: " if s else "") + x for t, s, x in out)


def to_text(name, data, timestamps=False):
    k = kind_of(name)
    if k == "docx":
        return docx_text(data)
    if k == "pptx":
        return pptx_text(data)
    if k == "vtt":
        return vtt_text(data, timestamps)
    if k == "xlsx":
        return render_table(xlsx_range(data))
    if k == "text":
        return data.decode("utf-8-sig", "replace")
    raise ExtractError(f"no text extractor for .{k} files; use `download` instead")
