"""
Markdown table parsing + PNG rendering.

Self-contained: does its own optional-dependency handling (WeasyPrint /
PyMuPDF / Pillow may not be installed — image rendering falls back to
TABLE_RENDER_MODE=ascii elsewhere) so this module has no import-time
dependency on dop_agent.py's config and can't create a circular import.

Pipeline (render_table_png): build a small HTML/CSS table -> WeasyPrint
renders it to a PDF -> PyMuPDF (fitz) rasterizes the PDF page to a
high-DPI pixmap -> Pillow autocrops the surrounding whitespace down to the
table's bounding box.
"""

from __future__ import annotations

import html
import io
import logging
import re

log = logging.getLogger("dop-agent.render_tables")

try:
    import fitz  # PyMuPDF
except ModuleNotFoundError:
    fitz = None

try:
    from PIL import Image, ImageChops
except ModuleNotFoundError:
    Image = None
    ImageChops = None

# Our own WeasyPrint table renders can exceed Pillow's default decompression-
# bomb pixel-count safety limit (we use an oversized @page to avoid clipping
# wide tables, see _TABLE_CSS). This is trusted, locally-generated content —
# not an untrusted upload — so raising the limit here is safe.
if Image is not None:
    Image.MAX_IMAGE_PIXELS = None

try:
    from weasyprint import HTML as WeasyHTML
    from weasyprint.urls import URLFetcher
except ModuleNotFoundError:
    WeasyHTML = None
    URLFetcher = None

# Only data: URIs may be fetched. file:// and http(s) resources (<img>, CSS
# url()/@import, @font-face) fail with a warning instead of reading local files
# or making network requests. fail_on_errors=False keeps a blocked fetch from
# aborting the render.
_NO_EXTERNAL_FETCH = (URLFetcher(allowed_protocols={"data"}, fail_on_errors=False)
                      if URLFetcher is not None else None)


def missing_image_render_deps() -> list[str]:
    """Return missing optional packages needed for table PNG rendering."""
    missing: list[str] = []
    if fitz is None:
        missing.append("PyMuPDF (fitz)")
    if Image is None or ImageChops is None:
        missing.append("Pillow")
    if WeasyHTML is None:
        missing.append("WeasyPrint")
    return missing


MISSING_IMAGE_DEPS = missing_image_render_deps()


def _split_table_row_cells(line: str) -> list[str]:
    """Split a single Markdown table row on unescaped `|` delimiters.

    A naive `re.split(r"\\|", ...)` breaks on cells that contain inline
    code with pipes in it, e.g. `` `bot_ctl.sh {start|stop|restart}` `` —
    each `|` inside the backticks gets mistaken for a column separator,
    shredding one cell into several and misaligning the whole row. This
    walks the line character-by-character so `|` inside a backtick code
    span, or escaped as `\\|`, is treated as literal text, not a delimiter.
    """
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
            current.append(ch)
            i += 1
            continue
        if ch == "|" and not in_code:
            cells.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    cells.append("".join(current))
    return cells


def _parse_md_table_rows(table_text: str) -> list[list[str]] | None:
    """Parse a Markdown table block into a list of rows (first row = header).

    Returns None if the text doesn't look like a real table (no separator
    row, e.g. `|---|---|`) — used to avoid mis-detecting plain lines that
    happen to contain a pipe character.
    """
    lines = [l.rstrip() for l in table_text.strip().splitlines()]
    if not any(re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line) for line in lines):
        return None
    rows: list[list[str]] = []
    for line in lines:
        if re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line):
            continue  # skip separator row
        raw_cells = _split_table_row_cells(line.strip())
        # A well-formed row is `| a | b |`, which produces an empty cell
        # before the first "|" and after the last one — drop only those,
        # so a genuinely empty middle cell (`| a | | c |`) is preserved.
        if raw_cells and raw_cells[0].strip() == "":
            raw_cells = raw_cells[1:]
        if raw_cells and raw_cells[-1].strip() == "":
            raw_cells = raw_cells[:-1]
        cells = [c.strip() for c in raw_cells]
        if cells:
            rows.append(cells)
    return rows or None


_TABLE_CSS = """
@page { size: 5000px 5000px; margin: 0; }
body { font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
       margin: 0; padding: 0; }
table { border-collapse: collapse; background: #ffffff; table-layout: auto; }
th, td { padding: 7px 16px; border: 1px solid #d0d5da; text-align: left;
         font-size: 15px; color: #1b1f23; background: #ffffff;
         max-width: 320px; white-space: normal; word-wrap: break-word;
         overflow-wrap: anywhere; }
th { background: #24292f; color: #ffffff; font-weight: 600; }
tr:nth-child(even) td { background: #f6f8fa; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 13px;
       background: rgba(175, 184, 193, 0.2); padding: 1px 5px; border-radius: 4px;
       word-break: break-all; }
a { color: #0969da; text-decoration: none; }
.emoji { vertical-align: -0.35em; }
"""

# Unicode ranges covering the common emoji blocks (emoticons, symbols &
# pictographs, transport, dingbats, variation selectors, flags, etc). Emoji
# glyphs have a much taller natural ascent than the surrounding text font, so
# left un-wrapped they render baseline-aligned but visually "floating" above
# regular text. Wrapping matches in a span lets CSS pull them back down
# (see .emoji vertical-align above). Exported (no leading underscore) since
# render_markdown.py reuses this same regex for full-document rendering.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols & pictographs, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols, dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U00002190-\U000021FF"  # arrows (e.g. some emoji-ish arrows)
    "\U00002700-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\uFE0F"                  # variation selector-16 (emoji presentation)
    "]+"
)


def _md_inline_to_html(text: str) -> str:
    """Render inline Markdown (bold, italic, code, links, strikethrough)
    inside a single table cell to safe HTML.
    """
    escaped = html.escape(text, quote=False)
    # Wrap emoji so CSS can correct their oversized ascent vs. regular text
    # (see .emoji rule in _TABLE_CSS) — must run before other rules so it
    # doesn't interfere with code/link/bold matching.
    escaped = EMOJI_RE.sub(lambda m: f'<span class="emoji">{m.group(0)}</span>', escaped)

    # Inline code `code` (do first so its contents aren't touched by other rules)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    # Links [text](url)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    # Bold **text** or __text__. The __ variant requires word boundaries
    # (like CommonMark) so dunder identifiers such as `__init__` aren't
    # mistaken for bold markers.
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\w)__(?!_)([^_\n]+?)(?<!_)__(?!\w)", r"<b>\1</b>", escaped)
    # Italic *text* or _text_ (single markers, after bold is handled).
    # The _text_ variant also requires word boundaries around both
    # delimiters — without this, snake_case identifiers/filenames like
    # `get_domain_config` get their middle segment ("_domain_") rendered
    # as italic, which is wrong: those underscores are part of the
    # identifier, not emphasis markers.
    escaped = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!\w)_(?!_)([^_\n]+?)(?<!_)_(?!\w)", r"<i>\1</i>", escaped)
    # Strikethrough ~~text~~
    escaped = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", escaped)

    return escaped


def render_table_png(rows: list[list[str]], scale: float = 3.0) -> bytes:
    """Render a parsed Markdown table (rows[0] = header) to a cropped PNG.

    Pipeline: build a small HTML/CSS table -> WeasyPrint renders it to a PDF
    -> PyMuPDF (fitz) rasterizes the PDF page to a high-DPI pixmap -> Pillow
    autocrops the surrounding whitespace down to the table's bounding box.
    Cell content supports inline Markdown (bold/italic/code/links/strike).
    """
    if MISSING_IMAGE_DEPS:
        raise RuntimeError(
            "Missing dependencies for table image rendering: "
            + ", ".join(MISSING_IMAGE_DEPS)
        )

    header, *body_rows = rows
    thead = "".join(f"<th>{_md_inline_to_html(c)}</th>" for c in header)
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_md_inline_to_html(c)}</td>" for c in row) + "</tr>"
        for row in body_rows
    )
    doc_html = (
        f"<html><head><style>{_TABLE_CSS}</style></head>"
        f"<body><table><tr>{thead}</tr>{tbody}</table></body></html>"
    )
    pdf_bytes = WeasyHTML(string=doc_html, url_fetcher=_NO_EXTERNAL_FETCH).write_pdf()
    pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = pdf[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 0))
    bbox = ImageChops.difference(img, bg).getbbox()
    if bbox:
        # Small padding around the tight crop so borders aren't clipped
        pad = 4
        bbox = (max(bbox[0] - pad, 0), max(bbox[1] - pad, 0),
                 min(bbox[2] + pad, img.width), min(bbox[3] + pad, img.height))
        img = img.crop(bbox)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_TABLE_BLOCK_RE = re.compile(r"(?:^\|?.+\|.+\n?)+", re.MULTILINE)


def extract_and_render_tables(text: str) -> list[tuple[str, bytes | None]]:
    """Split `text` around each Markdown table block into an ordered list of
    segments: (text_chunk, None) for prose, or ("", png_bytes) for a
    rendered table image. Consecutive text segments are NOT merged here —
    callers post each segment as its own Slack message, in order, so the
    layout is: text before the table, the table image, text after it (and
    so on for multiple tables).
    """
    segments: list[tuple[str, bytes | None]] = []
    pos = 0
    for m in _TABLE_BLOCK_RE.finditer(text):
        block = m.group(0)
        if "|" not in block:
            continue
        rows = _parse_md_table_rows(block)
        if not rows:
            continue
        try:
            png = render_table_png(rows)
        except Exception as exc:
            log.warning("Table image render failed, keeping raw Markdown: %s", exc)
            continue

        before = text[pos:m.start()]
        if before.strip():
            segments.append((before, None))
        segments.append(("", png))
        pos = m.end()

    remaining = text[pos:]
    if remaining.strip() or not segments:
        segments.append((remaining, None))
    return segments
