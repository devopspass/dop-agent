"""Markdown -> Telegram HTML, fence-aware splitting and fence extraction."""
from fake_telegram import check_html, u16
from telegram_format import extract_fences, md_to_telegram_html, split_markdown


def test_html_is_always_valid_and_escaped():
    out = md_to_telegram_html("# T\n**b** <script> & _i_ ~~s~~ `c<d>`\n- a\n- b\n> q")
    check_html(out)
    assert "<script>" not in out and "<b>b</b>" in out


def test_snake_case_is_not_italicised():
    out = md_to_telegram_html("see my_var_name and __init__")
    assert "<i>" not in out


def test_shortcode_maps_to_unicode():
    assert ":white_check_mark:" not in md_to_telegram_html(":white_check_mark: done")


def test_unsafe_link_scheme_is_dropped():
    assert "href" not in md_to_telegram_html("[x](javascript:alert(1))")


def test_table_becomes_pre():
    assert "<pre>" in md_to_telegram_html("| a | b |\n|---|---|\n| 1 | 2 |")


def test_split_never_exceeds_limit_and_keeps_fences_balanced():
    text = "intro\n```python\n" + "\n".join(f"line {i} 😀" for i in range(900)) + "\n```\nend"
    parts = split_markdown(text, 4000)
    assert len(parts) > 1
    for p in parts:
        assert u16(p) <= 4000 and p.count("```") % 2 == 0
        check_html(md_to_telegram_html(p))


def test_actions_fence_becomes_buttons_and_is_removed():
    text = "go?\n```actions\n- * Proceed | proceed\n- Skip | skip\n```"
    res = extract_fences(text)
    assert "```actions" not in res.text
    assert [b.label for row in res.rows for b in row] == ["Proceed", "Skip"]


def test_ideas_fence_is_rendered_as_text_list():
    res = extract_fences('```ideas title="x" id=a\n- [1] One :: desc\n```')
    assert "One" in res.text and not res.rows
