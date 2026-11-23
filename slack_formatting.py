"""
Markdown -> Slack mrkdwn conversion + message splitting.

Depends on render_tables._parse_md_table_rows for detecting/parsing
Markdown table blocks (reused rather than duplicated).
"""

from __future__ import annotations

import re

from render_tables import _parse_md_table_rows

# Slack's chat.postMessage plain "text" field is capped around 4000 chars;
# stay comfortably under that so messages never get silently truncated by
# the API. Used as the default for split_message() and directly by
# dop_agent.py wherever a single Slack text field needs pre-truncation
# (e.g. the "Working..." status line, before it even reaches split_message).
SLACK_MAX_CHARS = 3900


def _convert_md_table(table_text: str) -> str:
    """Convert a Markdown table to a fixed-width monospace code block."""
    rows = _parse_md_table_rows(table_text)
    if not rows:
        return table_text
    # Compute column widths
    col_count = max(len(r) for r in rows)
    widths = [0] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    # Render
    def fmt_row(row: list[str]) -> str:
        padded = [row[i].ljust(widths[i]) if i < len(row) else " " * widths[i]
                  for i in range(col_count)]
        return "│ " + " │ ".join(padded) + " │"
    sep = "├─" + "─┼─".join("─" * w for w in widths) + "─┤"
    top = "┌─" + "─┬─".join("─" * w for w in widths) + "─┐"
    bot = "└─" + "─┴─".join("─" * w for w in widths) + "─┘"
    result_lines = [top, fmt_row(rows[0]), sep]
    for row in rows[1:]:
        result_lines.append(fmt_row(row))
    result_lines.append(bot)
    return "```\n" + "\n".join(result_lines) + "\n```"


def md_to_slack(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format."""
    # Protect code blocks from conversion, restore them after
    code_blocks: list[str] = []
    def stash_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", stash_block, text)

    # Protect inline code
    inline_codes: list[str] = []
    def stash_inline(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`\n]+`", stash_inline, text)

    # Convert Markdown tables before other transformations
    text = re.sub(
        r"((?:^\|?.+\|.+\n)+)",
        lambda m: _convert_md_table(m.group(0)) if "|" in m.group(0) else m.group(0),
        text,
        flags=re.MULTILINE,
    )

    # Headers → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Italic *text* (single asterisk MD italic) → _text_ — must run BEFORE bold
    # so we don't accidentally re-process the * added by bold conversion
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"_\1_", text)
    # Bold **text** → *text* (Slack bold)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    # Links [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Unordered list bullets - or * → •
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
    # Strikethrough ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # Restore code blocks and inline code
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    return text


def split_message(text: str, max_len: int = SLACK_MAX_CHARS) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts
