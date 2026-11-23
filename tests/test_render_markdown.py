"""
Unit tests for render_markdown.py — Markdown document -> PNG rendering.

`_wrap_emoji_in_html` and `_find_page_breaks` are pure-logic and always
tested. `render_markdown_doc_png`'s happy path additionally exercises the
real WeasyPrint/PyMuPDF/Pillow pipeline (skipped automatically if those
optional dependencies aren't installed in this environment).
"""

from __future__ import annotations

import pytest

import render_markdown

Image = render_markdown.Image  # None when the render extra (Pillow) is absent

needs_image_deps = pytest.mark.skipif(
    bool(render_markdown.MISSING_IMAGE_DEPS),
    reason=f"missing image render deps: {render_markdown.MISSING_IMAGE_DEPS}",
)


def test_wrap_emoji_in_html_wraps_emoji_outside_tags():
    html_in = "<p>hello 🚀 world</p>"
    html_out = render_markdown._wrap_emoji_in_html(html_in)
    assert '<span class="emoji">🚀</span>' in html_out
    # Tags themselves must be left untouched.
    assert html_out.startswith("<p>")
    assert html_out.endswith("</p>")


def test_wrap_emoji_in_html_no_emoji_is_noop():
    html_in = "<p>plain text, no emoji here</p>"
    assert render_markdown._wrap_emoji_in_html(html_in) == html_in


needs_pillow = pytest.mark.skipif(Image is None, reason="Pillow not installed")


@needs_pillow
def test_find_page_breaks_short_image_no_breaks():
    img = Image.new("RGB", (100, 50), (255, 255, 255))
    assert render_markdown._find_page_breaks(img, max_page_height=200) == []


@needs_pillow
def test_find_page_breaks_splits_tall_blank_image():
    # An all-white image taller than max_page_height must produce at least
    # one break, and every row chosen must land on a blank row (trivially
    # true here since the whole image is blank).
    img = Image.new("RGB", (50, 500), (255, 255, 255))
    breaks = render_markdown._find_page_breaks(img, max_page_height=200)
    assert breaks
    assert all(0 < b < 500 for b in breaks)


@needs_image_deps
def test_render_markdown_doc_png_returns_single_page_png_bytes():
    md_text = "# Title\n\nSome **bold** text and a [link](https://example.com)."
    pages = render_markdown.render_markdown_doc_png(md_text)
    assert isinstance(pages, list)
    assert len(pages) == 1
    assert pages[0].startswith(b"\x89PNG\r\n\x1a\n")  # PNG magic bytes


@needs_image_deps
def test_render_markdown_doc_png_splits_long_document_into_pages():
    # A long document with many headings should exceed the default
    # max_page_height and be split into more than one page image.
    md_text = "\n\n".join(f"## Section {i}\n\nSome body text for section {i}." for i in range(80))
    pages = render_markdown.render_markdown_doc_png(md_text, max_page_height=300)
    assert len(pages) > 1
    for page in pages:
        assert page.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.skipif(render_markdown.nh3 is None, reason="nh3 not installed")
def test_raw_html_in_markdown_loses_img_script_and_css_url():
    """Bug: /md passed raw HTML from a .md file straight to WeasyPrint, so an
    <img src="file://..."> or style url() could read local files or make
    requests. The sanitizer must drop those, keep text and text-align.
    """
    dirty = ('<img src="file:///etc/hosts"><script>x()</script>'
             '<div style="background:url(file:///etc/passwd)">box</div>'
             '<th style="text-align:center;background:url(x)">t</th>')
    clean = render_markdown._sanitize_html(dirty)
    assert "<img" not in clean and "<script" not in clean
    assert "url(" not in clean and "background" not in clean
    assert "box" in clean
    assert 'text-align: center' in render_markdown._sanitize_html(
        '<table><tr><th style="text-align:center">t</th></tr></table>')


def test_weasyprint_fetcher_allows_only_data_uris():
    """Bug: WeasyPrint's default fetcher loaded file:// and http(s) resources
    referenced from rendered content. The shared fetcher must refuse both
    and still allow inline data: URIs.
    """
    from render_tables import _NO_EXTERNAL_FETCH
    if _NO_EXTERNAL_FETCH is None:
        pytest.skip("WeasyPrint not installed")
    for url in ("file:///etc/hosts", "http://127.0.0.1:9/x.png", "https://example.com/x.png"):
        with pytest.raises(ValueError):
            _NO_EXTERNAL_FETCH(url)
    assert _NO_EXTERNAL_FETCH("data:text/plain,hello") is not None
