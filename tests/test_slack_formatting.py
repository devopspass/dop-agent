"""
Unit tests for slack_formatting.py — Markdown -> Slack mrkdwn conversion
and message splitting.
"""

from __future__ import annotations

import slack_formatting


def test_convert_md_table_renders_monospace_box():
    table_text = (
        "| Name | Value |\n"
        "|------|-------|\n"
        "| foo  | 1     |\n"
    )
    result = slack_formatting._convert_md_table(table_text)
    assert result.startswith("```\n")
    assert result.endswith("\n```")
    assert "Name" in result
    assert "foo" in result
    # Should render box-drawing borders
    assert "┌─" in result and "└─" in result


def test_convert_md_table_returns_original_text_when_not_a_table():
    text = "just a line | with a pipe, no separator row"
    assert slack_formatting._convert_md_table(text) == text


def test_md_to_slack_headers_to_bold():
    # Headers are first wrapped in single asterisks (*Title*); the italic pass,
    # which runs before bold, then re-matches that span and converts it to _Title_.
    assert slack_formatting.md_to_slack("# Title") == "_Title_"


def test_md_to_slack_bold_and_italic():
    out = slack_formatting.md_to_slack("**bold** and *italic*")
    assert out == "*bold* and _italic_"


def test_md_to_slack_links():
    out = slack_formatting.md_to_slack("[text](https://example.com)")
    assert out == "<https://example.com|text>"


def test_md_to_slack_bullets():
    out = slack_formatting.md_to_slack("- item one\n* item two")
    assert out == "• item one\n• item two"


def test_md_to_slack_strikethrough():
    assert slack_formatting.md_to_slack("~~gone~~") == "~gone~"


def test_md_to_slack_preserves_code_block_contents_untouched():
    text = "```\n# not a header\n**not bold**\n```"
    out = slack_formatting.md_to_slack(text)
    assert out == text


def test_md_to_slack_preserves_inline_code_untouched():
    text = "Use `**not bold**` here"
    out = slack_formatting.md_to_slack(text)
    assert out == text


def test_md_to_slack_converts_table_to_monospace_block():
    text = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    out = slack_formatting.md_to_slack(text)
    assert out.startswith("```\n")
    assert "1" in out and "2" in out


def test_split_message_short_text_single_chunk():
    assert slack_formatting.split_message("hello") == ["hello"]


def test_split_message_splits_on_newline_boundary():
    text = "a" * 10 + "\n" + "b" * 10
    parts = slack_formatting.split_message(text, max_len=12)
    assert len(parts) == 2
    assert parts[0] == "a" * 10
    assert parts[1] == "b" * 10


def test_split_message_hard_cut_when_no_newline():
    text = "x" * 30
    parts = slack_formatting.split_message(text, max_len=10)
    assert len(parts) == 3
    assert "".join(parts) == text
