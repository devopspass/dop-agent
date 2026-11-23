"""Pure Telegram text helpers (no I/O).

Pipeline contract: ``format_markdown`` (the adapter
method the core calls) returns *plain markdown* chunks split fence-aware; the
adapter converts each chunk to Telegram HTML at send time with
``md_to_telegram_html``. HTML is never split.

- Parse mode is HTML (only ``& < >`` need escaping). Supported tags:
  b i u s a code pre blockquote tg-spoiler. ``<pre>`` never holds other tags.
- Slack shortcodes used by the core's own status text (``:thinking_face:``) are
  mapped to unicode; ``_italic_`` uses word-boundary rules so
  ``stop_working_turn`` is never italicised; unclosed markers (status text may
  be truncated mid-token) are tolerated; invalid output falls back to escaped
  plain text via ``validate_html``.
- Limits are measured in UTF-16 code units (Telegram counts entities-stripped
  text that way); ``TEXT_BUDGET`` leaves headroom under the 4096 hard cap.
- ``actions`` / ``ask`` fences become inline keyboards (tokens persisted by the
  caller, since callback_data is capped at 64 bytes); ``ideas`` degrades to a
  numbered list; ``chart`` / ``mermaid`` stay readable code blocks.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

HARD_LIMIT = 4096
TEXT_BUDGET = 4000
CAPTION_LIMIT = 1024
SAFE_SCHEMES = ("http://", "https://", "tg://", "mailto:")

SHORTCODES = {
    "thinking_face": "🤔", "black_square_for_stop": "⏹", "thread": "🧵", "white_check_mark": "✅",
    "x": "❌", "warning": "⚠️", "no_entry": "⛔", "globe_with_meridians": "🌐", "hourglass_flowing_sand": "⏳",
    "eyes": "👀", "headphones": "🎧", "zap": "⚡", "frame_with_picture": "🖼", "compass": "🧭",
    "rocket": "🚀", "bulb": "💡", "memo": "📝", "tada": "🎉", "arrows_counterclockwise": "🔄",
    "repeat": "🔁", "mag": "🔍", "wrench": "🔧", "fire": "🔥", "heavy_check_mark": "✔️", "stop_sign": "🛑",
    "robot_face": "🤖", "link": "🔗", "pushpin": "📌", "clock1": "🕐", "bell": "🔔",
}
_SHORTCODE_RE = re.compile(r":([a-z0-9_+-]+):")


def u16(text: str) -> int:
    """Length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def replace_shortcodes(text: str) -> str:
    return _SHORTCODE_RE.sub(lambda m: SHORTCODES.get(m.group(1), m.group(0)), text)


# -- fence-aware splitting ---------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^\s*```([\w+-]*)\s*$")


def _hard_split(line: str, limit: int) -> list[str]:
    parts, cur = [], ""
    for ch in line:
        if u16(cur) + u16(ch) > limit:
            parts.append(cur)
            cur = ""
        cur += ch
    if cur:
        parts.append(cur)
    return parts


def split_markdown(text: str, limit: int = TEXT_BUDGET) -> list[str]:
    """Split markdown into chunks of at most ``limit`` UTF-16 units, preferring
    paragraph then line boundaries. A split inside a fenced block closes the
    fence in the chunk and reopens it (same language) in the next one."""
    if u16(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence_lang: str | None = None  # currently open fence language ('' = no lang) or None
    reserve = u16("\n```")

    def flush() -> None:
        nonlocal cur, size
        if not cur:
            return
        body = "\n".join(cur)
        if fence_lang is not None:
            body += "\n```"
        chunks.append(body)
        cur = []
        size = 0
        if fence_lang is not None:
            cur.append(f"```{fence_lang}")
            size = u16(cur[0])

    for raw_line in text.split("\n"):
        m = _FENCE_OPEN_RE.match(raw_line)
        pieces = [raw_line]
        in_fence_cost = reserve if fence_lang is not None or (m and fence_lang is None) else 0
        if u16(raw_line) + 1 > limit - in_fence_cost - 20:
            pieces = _hard_split(raw_line, max(limit - in_fence_cost - 40, 50))
        for piece in pieces:
            add = u16(piece) + 1
            extra = reserve if fence_lang is not None else 0
            if cur and size + add + extra > limit:
                flush()
            cur.append(piece)
            size += add
        if m:
            fence_lang = None if fence_lang is not None else m.group(1)
    if cur and any(line.strip() for line in cur):
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()] or [text[:limit]]


# -- markdown -> Telegram HTML ----------------------------------------------

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")
_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "tg-spoiler"}


class _Validator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.error: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in _ALLOWED_TAGS:
            self.error = f"unsupported tag <{tag}>"
            return
        if self.stack and self.stack[-1] == "pre" and tag != "code":
            self.error = "entity inside <pre>"
        if tag == "pre" and "pre" in self.stack:
            self.error = "nested pre"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.error = f"mismatched </{tag}>"
            return
        self.stack.pop()


def validate_html(markup: str) -> str | None:
    """None when ``markup`` is valid Telegram HTML, else a reason string."""
    v = _Validator()
    try:
        v.feed(markup)
        v.close()
    except Exception as exc:  # noqa: BLE001
        return f"parse error: {exc}"
    return v.error or (f"unclosed <{v.stack[-1]}>" if v.stack else None)


def html_visible_text(markup: str) -> str:
    return html.unescape(_TAG_RE.sub("", markup))


def visible_len(markup: str) -> int:
    return u16(html_visible_text(markup))


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def safe_url(url: str) -> str | None:
    url = url.strip()
    return url if url.lower().startswith(SAFE_SCHEMES) else None


_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_PH = "\x00{}\x00"


def _convert_inline(text: str) -> str:
    holders: list[str] = []

    def hold(markup: str) -> str:
        holders.append(markup)
        return _PH.format(len(holders) - 1)

    text = _INLINE_CODE_RE.sub(lambda m: hold(f"<code>{esc(m.group(1))}</code>"), text)

    def link(m: re.Match) -> str:
        url = safe_url(m.group(2))
        label = esc(m.group(1))
        if url is None:
            return hold(f"{label} ({esc(m.group(2))})")
        return hold(f'<a href="{html.escape(url, quote=True)}">{label}</a>')

    text = _LINK_RE.sub(link, text)
    text = esc(text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDER_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    for i, markup in enumerate(holders):
        text = text.replace(_PH.format(i), markup)
    return text


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _is_table_row(line: str) -> bool:
    return "|" in line and line.strip().startswith("|")


def md_to_telegram_html(text: str) -> str:
    """Convert (slack-flavoured) markdown to Telegram HTML; invalid results fall
    back to escaped plain text so a send can never fail on markup."""
    markup = _md_to_html(text)
    return markup if validate_html(markup) is None else esc(replace_shortcodes(text))


def _md_to_html(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    prose: list[str] = []

    def flush_prose() -> None:
        if prose:
            out.append(_convert_inline(replace_shortcodes("\n".join(prose))))
            prose.clear()

    while i < len(lines):
        line = lines[i]
        m = _FENCE_OPEN_RE.match(line)
        if m:
            flush_prose()
            lang = m.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence (or EOF for an unclosed fence)
            code = esc("\n".join(body))
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{cls}>{code}</code></pre>" if lang else f"<pre>{code}</pre>")
            continue
        if _is_table_row(line):
            j = i
            while j < len(lines) and _is_table_row(lines[j]):
                j += 1
            block = lines[i:j]
            if len(block) >= 2 and _TABLE_SEP_RE.match(block[1]):
                flush_prose()
                out.append("<pre>" + esc("\n".join(block)) + "</pre>")
                i = j
                continue
        if line.startswith(">"):
            j = i
            quote: list[str] = []
            while j < len(lines) and lines[j].startswith(">"):
                quote.append(lines[j].lstrip(">").lstrip())
                j += 1
            flush_prose()
            out.append("<blockquote>" + _convert_inline(replace_shortcodes("\n".join(quote))) + "</blockquote>")
            i = j
            continue
        h = _HEADING_RE.match(line)
        if h:
            flush_prose()
            out.append(f"<b>{_convert_inline(replace_shortcodes(h.group(1)))}</b>")
            i += 1
            continue
        b = _BULLET_RE.match(line)
        if b and not re.match(r"^\s*(\*\*|__)", line):
            prose.append(f"{b.group(1)}• {b.group(2)}")
        elif re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            prose.append("──────────")
        else:
            prose.append(line)
        i += 1
    flush_prose()
    # blocks are separated by single newlines already represented in the source;
    # code/blockquote blocks supply their own line breaks.
    result: list[str] = []
    for piece in out:
        result.append(piece)
    return "\n".join(result)


def plain_text(text: str) -> str:
    """Fallback body for the plain-text retry (no parse_mode)."""
    return replace_shortcodes(text)


def truncate_visible(text: str, limit: int = HARD_LIMIT) -> str:
    if u16(text) <= limit:
        return text
    cut = _hard_split(text, limit - 1)[0]
    return cut + "…"


# -- fences -> keyboards ------------------------------------------------------

_FENCE_BLOCK_RE = re.compile(r"^```(actions|ask|ideas)[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_ACTION_RE = re.compile(r"^-\s*(\*\s+)?(.+?)(?:\s*\|\s*(.+))?$")
_ASK_HEAD_RE = re.compile(r"^([QD]\d+)(?:\s+(single|multi|text))?\s*:\s*(.*)$")
_ASK_OPT_RE = re.compile(r"^-\s*([\w.-]+?)(\*)?\s*:\s*(.*?)(?:\s*\|\s*(.*))?$")
_IDEA_RE = re.compile(r"^-\s*\[(\w+)\]\s*(.+?)(?:\s*::\s*(.*))?$")


@dataclass
class ChoiceButton:
    """A tappable choice: ``text`` is what is sent as the user's next message."""

    label: str
    text: str
    primary: bool = False


@dataclass
class FenceResult:
    text: str
    rows: list[list[ChoiceButton]] = field(default_factory=list)


def extract_fences(text: str) -> FenceResult:
    """Replace actions/ask/ideas fences with Telegram-friendly text and collect
    the choices as button rows. Other fences (chart, mermaid, code) are kept."""
    rows: list[list[ChoiceButton]] = []

    def repl(m: re.Match) -> str:
        kind, body = m.group(1), m.group(2)
        lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
        if kind == "actions":
            row: list[ChoiceButton] = []
            for ln in lines:
                am = _ACTION_RE.match(ln)
                if am:
                    label = am.group(2).strip()
                    row.append(ChoiceButton(label, (am.group(3) or label).strip(), bool(am.group(1))))
            rows.extend([[b] for b in row])
            return ""
        if kind == "ask":
            return _ask_to_text(lines, rows)
        return _ideas_to_text(lines)

    new = _FENCE_BLOCK_RE.sub(repl, text)
    return FenceResult(re.sub(r"\n{3,}", "\n\n", new).strip("\n") if new != text else text, rows)


def _ask_to_text(lines: list[str], rows: list[list[ChoiceButton]]) -> str:
    out: list[str] = []
    qid = mode = None
    for ln in lines:
        head = _ASK_HEAD_RE.match(ln)
        if head:
            qid, mode = head.group(1), head.group(2) or "single"
            suffix = "" if mode == "single" else f" (reply with text — {mode})"
            out.append(f"**{qid}: {head.group(3)}**{suffix}")
            continue
        if ln.startswith("?"):
            out.append(f"_{ln.lstrip('? ').strip()}_")
            continue
        opt = _ASK_OPT_RE.match(ln)
        if opt and qid:
            key, rec, label, note = opt.group(1), bool(opt.group(2)), opt.group(3), opt.group(4)
            out.append(f"- {'★ ' if rec else ''}{key}: {label}" + (f" — {note}" if note else ""))
            if mode == "single":
                rows.append([ChoiceButton(f"{qid} {label}"[:60], f"{qid}: {label}", rec)])
    return "\n".join(out)


def _ideas_to_text(lines: list[str]) -> str:
    out = []
    for ln in lines:
        im = _IDEA_RE.match(ln)
        if im:
            desc = f" — {im.group(3).split('|')[0].strip()}" if im.group(3) else ""
            out.append(f"{im.group(1)}. {im.group(2).strip()}{desc}")
    if not out:
        return ""
    return "\n".join(out) + "\n\n_Reply with e.g. `1 ok, 2 nok, 3 skip`._"


def split_caption(text: str) -> str:
    return text if u16(text) <= CAPTION_LIMIT else truncate_visible(text, CAPTION_LIMIT)
