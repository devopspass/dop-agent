"""
Unit tests for render_tables.py — pure parsing logic only (no WeasyPrint/
PyMuPDF rendering calls, which need native system libraries not guaranteed
to be present in every environment this suite runs in).
"""

from __future__ import annotations

import render_tables


def test_split_table_row_cells_simple():
    cells = render_tables._split_table_row_cells("a | b | c")
    assert cells == ["a ", " b ", " c"]


def test_split_table_row_cells_ignores_pipe_inside_backtick_code():
    line = "`bot_ctl.sh {start|stop|restart}` | description"
    cells = render_tables._split_table_row_cells(line)
    assert cells == ["`bot_ctl.sh {start|stop|restart}` ", " description"]


def test_split_table_row_cells_handles_escaped_pipe():
    cells = render_tables._split_table_row_cells(r"a\|b | c")
    assert cells == ["a|b ", " c"]


def test_parse_md_table_rows_basic():
    table_text = (
        "| Name | Value |\n"
        "|------|-------|\n"
        "| foo  | 1     |\n"
        "| bar  | 2     |\n"
    )
    rows = render_tables._parse_md_table_rows(table_text)
    assert rows == [
        ["Name", "Value"],
        ["foo", "1"],
        ["bar", "2"],
    ]


def test_parse_md_table_rows_returns_none_without_separator_row():
    # Plain text with a pipe character but no `|---|---|` separator row
    # must not be mistaken for a table.
    text = "some text | with a pipe\nanother line | also with pipe"
    assert render_tables._parse_md_table_rows(text) is None


def test_parse_md_table_rows_preserves_empty_middle_cell():
    table_text = (
        "| a | b | c |\n"
        "|---|---|---|\n"
        "| 1 |   | 3 |\n"
    )
    rows = render_tables._parse_md_table_rows(table_text)
    assert rows == [["a", "b", "c"], ["1", "", "3"]]


def test_md_inline_to_html_bold_italic_code_link():
    html_out = render_tables._md_inline_to_html(
        "**bold** _italic_ `code` [link](https://example.com)"
    )
    assert "<b>bold</b>" in html_out
    assert "<i>italic</i>" in html_out
    assert "<code>code</code>" in html_out
    assert '<a href="https://example.com">link</a>' in html_out


def test_md_inline_to_html_does_not_italicize_snake_case_identifier():
    html_out = render_tables._md_inline_to_html("get_domain_config")
    assert "<i>" not in html_out
    assert html_out == "get_domain_config"


def test_md_inline_to_html_bolds_full_dunder_word():
    # The word-boundary guard applies around the outer "__" pair only,
    # so "__init__" does get bolded to "<b>init</b>".
    html_out = render_tables._md_inline_to_html("__init__")
    assert html_out == "<b>init</b>"


def test_md_inline_to_html_wraps_emoji_span():
    html_out = render_tables._md_inline_to_html("hello 🚀 world")
    assert '<span class="emoji">🚀</span>' in html_out


def test_extract_and_render_tables_no_table_returns_single_text_segment():
    text = "just some plain prose, no tables here"
    segments = render_tables.extract_and_render_tables(text)
    assert segments == [(text, None)]


def test_extract_and_render_tables_skips_pipe_text_without_separator_row():
    # A line containing "|" but with no real table separator row should
    # not attempt (and fail) a PNG render — it must pass through as plain text.
    text = "a | b\nc | d"
    segments = render_tables.extract_and_render_tables(text)
    assert segments == [(text, None)]
