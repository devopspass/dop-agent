"""
Full Markdown document -> PNG rendering (e.g. SOP drafts, skill files
stored as .md, ticket notes) — one or more page images instead of a wall
of Slack mrkdwn text.

Self-contained like render_tables.py: does its own optional-dependency
handling (WeasyPrint / PyMuPDF / Pillow may not be installed) so this
module has no import-time dependency on dop_agent.py's config and can't
create a circular import. Reuses `EMOJI_RE` from render_tables.py (a
shared, exported constant) rather than duplicating the emoji regex.
"""

from __future__ import annotations

import io
import logging
import re

from render_tables import EMOJI_RE, _NO_EXTERNAL_FETCH

log = logging.getLogger("dop-agent.render_markdown")

try:
    import fitz  # PyMuPDF
except ModuleNotFoundError:
    fitz = None

try:
    from PIL import Image, ImageChops
except ModuleNotFoundError:
    Image = None
    ImageChops = None

if Image is not None:
    Image.MAX_IMAGE_PIXELS = None

try:
    from weasyprint import HTML as WeasyHTML
except ModuleNotFoundError:
    WeasyHTML = None

try:
    import nh3
except ModuleNotFoundError:
    nh3 = None


def missing_image_render_deps() -> list[str]:
    """Return the list of missing packages needed for Markdown-doc image
    rendering, or an empty list if everything is available.
    """
    missing = []
    if Image is None or ImageChops is None:
        missing.append("pillow")
    if WeasyHTML is None:
        missing.append("weasyprint")
    if fitz is None:
        missing.append("pymupdf")
    if nh3 is None:
        missing.append("nh3")
    return missing


MISSING_IMAGE_DEPS = missing_image_render_deps()


_DOC_CSS = """
@page { size: 1100px 30000px; margin: 0; background: #ffffff; }
html { background: #ffffff; }
body { font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
       margin: 0; padding: 32px 40px; max-width: 1020px; color: #1b1f23;
       font-size: 16px; line-height: 1.55; background: #ffffff; }
h1, h2, h3, h4, h5, h6 { font-weight: 600; line-height: 1.25; margin: 24px 0 12px; }
h1 { font-size: 30px; border-bottom: 1px solid #d0d5da; padding-bottom: 8px; }
h2 { font-size: 24px; border-bottom: 1px solid #eaecef; padding-bottom: 6px; }
h3 { font-size: 20px; }
h4 { font-size: 17px; }
p, ul, ol, blockquote, pre, table { margin: 0 0 14px; }
ul, ol { padding-left: 28px; }
li { margin: 3px 0; }
li > ul, li > ol { margin: 3px 0 3px; }
blockquote { border-left: 4px solid #d0d5da; margin-left: 0; padding: 2px 16px;
             color: #57606a; background: #ffffff; }
hr { border: none; border-top: 1px solid #d0d5da; margin: 24px 0; }
a { color: #0969da; text-decoration: none; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 14px;
       background: rgba(175, 184, 193, 0.2); padding: 1px 6px; border-radius: 4px;
       overflow-wrap: anywhere; }
pre { background: #f6f8fa; border: 1px solid #d0d5da; border-radius: 6px;
      padding: 14px 16px; overflow-wrap: anywhere; white-space: pre-wrap; }
pre code { background: none; padding: 0; border-radius: 0; }
table { border-collapse: collapse; width: 100%; table-layout: auto; background: #ffffff; }
th, td { padding: 7px 14px; border: 1px solid #d0d5da; text-align: left;
         font-size: 15px; max-width: 320px; white-space: normal;
         word-wrap: break-word; overflow-wrap: anywhere; background: #ffffff; }
th { background: #24292f; color: #ffffff; font-weight: 600; }
tr:nth-child(even) td { background: #f6f8fa; }
img { max-width: 100%; }
.emoji { vertical-align: -0.35em; }
"""


_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote",
    "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "a", "b", "i", "em",
    "strong", "s", "del", "span", "div", "sup", "sub", "dl", "dt", "dd",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title", "id"}, "span": {"class"}, "div": {"class", "id"},
    "code": {"class"}, "pre": {"class"}, "sup": {"id"}, "li": {"id"},
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"}, "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
    "th": {"align", "style"}, "td": {"align", "style"},
}


def _keep_text_align_only(tag, attr, value):
    """Allow only `text-align` in style attributes (no url(), no other CSS)."""
    if attr != "style":
        return value
    name, _, val = value.strip().rstrip(";").partition(":")
    if name.strip() == "text-align" and val.strip() in {"left", "center", "right"}:
        return f"text-align: {val.strip()}"
    return None


def _sanitize_html(html_text: str) -> str:
    """Strip raw HTML from the Markdown source: no img/link/style/script, no url()."""
    return nh3.clean(html_text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
                     url_schemes={"http", "https", "mailto"},
                     attribute_filter=_keep_text_align_only)


# Same emoji-detection regex used for table cells (see EMOJI_RE / .emoji in
# render_tables.py) — applied to the rendered HTML text nodes so
# headings/paragraphs get the same baseline correction as table cells.
def _wrap_emoji_in_html(html_text: str) -> str:
    """Wrap emoji runs in <span class="emoji"> without touching existing
    tags/attributes, by only operating on segments outside of `<...>`.
    """
    parts = re.split(r"(<[^>]+>)", html_text)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[i] = EMOJI_RE.sub(lambda m: f'<span class="emoji">{m.group(0)}</span>', part)
    return "".join(parts)


def _find_page_breaks(img: "Image.Image", max_page_height: int) -> list[int]:
    """Return a sorted list of y-coordinates at which to slice `img` into
    pages no taller than `max_page_height`, each break chosen at the
    nearest fully-blank (all-white) row so we never cut through a line of
    text, a table row, or a code block.
    """
    width, height = img.size
    if height <= max_page_height:
        return []

    # Precompute, for every row, whether it's "blank" (every pixel very
    # close to white) — cheap to do once via getextrema() per row strip.
    gray = img.convert("L")
    row_is_blank: list[bool] = []
    for y in range(height):
        extrema = gray.crop((0, y, width, y + 1)).getextrema()
        row_is_blank.append(extrema[0] >= 250)

    breaks: list[int] = []
    pos = 0
    while height - pos > max_page_height:
        target = pos + max_page_height
        # Search outward from the target line for the nearest blank row,
        # preferring a bit above the target over going way past it.
        search_span = max_page_height // 3
        chosen = None
        for delta in range(0, search_span):
            up = target - delta
            if up > pos and row_is_blank[up]:
                chosen = up
                break
            down = target + delta
            if down < height and row_is_blank[down]:
                chosen = down
                break
        if chosen is None:
            # No blank row nearby (e.g. inside a very long code block) —
            # fall back to a hard cut at the target line.
            chosen = target
        breaks.append(chosen)
        pos = chosen
    return breaks


def render_markdown_doc_png(
    md_text: str, scale: float = 2.0, max_page_height: int = 2200
) -> list[bytes]:
    """Render a full Markdown document (headings, lists, code blocks,
    blockquotes, tables, links, etc.) to one or more page PNGs.

    Used for sending whole .md files (SOP drafts, skill docs, ticket notes
    stored on disk) to Slack as readable images instead of a wall of
    Slack-mrkdwn text. Long documents are split into multiple
    `max_page_height`-tall pages (in on-screen pixels, i.e. before `scale`
    is applied) so each image is a comfortable, readable size in Slack
    instead of one extremely tall unreadable strip. Splits are made at the
    nearest fully-blank row to the target page height so a page break never
    lands mid-paragraph, mid-table-row, or mid-code-line.
    """
    if MISSING_IMAGE_DEPS:
        raise RuntimeError(
            "Missing dependencies for Markdown image rendering: "
            + ", ".join(MISSING_IMAGE_DEPS)
        )

    import markdown as _markdown

    body_html = _markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists", "tables", "fenced_code", "toc"],
    )
    body_html = _sanitize_html(_wrap_emoji_in_html(body_html))
    doc_html = f"<html><head><style>{_DOC_CSS}</style></head><body>{body_html}</body></html>"

    pdf_bytes = WeasyHTML(string=doc_html, url_fetcher=_NO_EXTERNAL_FETCH).write_pdf()
    pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = pdf[0]
    # Render fully opaque (alpha=False) — unlike table images, a full document
    # has large plain-text regions with no explicit background, so cropping
    # against transparency (as render_table_png does) would leave those areas
    # transparent and Slack's dark lightbox/zoom view renders them black.
    # Instead render opaque white and autocrop against a white background —
    # trims the oversized @page's unused space while keeping the whole
    # visible image solid white, so there is nothing left transparent to
    # turn black on zoom.
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bbox = ImageChops.difference(img, bg).getbbox()
    if bbox:
        pad = 8
        bbox = (max(bbox[0] - pad, 0), max(bbox[1] - pad, 0),
                min(bbox[2] + pad, img.width), min(bbox[3] + pad, img.height))
        img = img.crop(bbox)

    breaks = _find_page_breaks(img, int(max_page_height * scale))
    if not breaks:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return [buf.getvalue()]

    pages: list[bytes] = []
    y0 = 0
    for y1 in [*breaks, img.height]:
        slice_img = img.crop((0, y0, img.width, y1))
        buf = io.BytesIO()
        slice_img.save(buf, format="PNG")
        pages.append(buf.getvalue())
        y0 = y1
    return pages
