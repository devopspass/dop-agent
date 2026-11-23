# Message and document rendering

## Message Formatting

Copilot responses are Markdown; Slack uses its own `mrkdwn` dialect. The bot converts automatically:

| Markdown | Slack |
|----------|-------|
| `**bold**` | `*bold*` |
| `*italic*` | `_italic_` |
| `# Heading` | `*Heading*` |
| `` `code` `` | `` `code` `` |
| ` ```block``` ` | ` ```block``` ` |
| `[text](url)` | `<url\|text>` |
| `- item` | `• item` |
| `~~strike~~` | `~strike~` |
| Markdown table | Rendered as a PNG image (see below) |

---

## Table Rendering

Slack has no native Markdown-table support, and monospace box-drawing text looks poor with Slack's proportional font. Tables are rendered as **PNG images** instead:

1. Each Markdown table found in the response is parsed and converted to a small styled HTML `<table>`.
2. [WeasyPrint](https://weasyprint.org/) renders that HTML/CSS to a PDF. Cells have `max-width: 320px` with word-wrap enabled (`overflow-wrap: anywhere`), so long content — long titles, URLs, even unbroken strings — wraps onto multiple lines within the cell instead of stretching the column indefinitely.
3. [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) rasterizes the PDF page to a high-DPI (3x) pixmap.
4. [Pillow](https://python-pillow.org/) autocrops the surrounding whitespace down to the table's tight bounding box.
5. Inline Markdown inside cells (bold/italic/code/links/strikethrough) is rendered with word-boundary-aware rules — e.g. `_..._`/`__..__` only become italic/bold at actual word boundaries, so snake_case filenames and identifiers like `get_domain_config.py` are left untouched instead of having their middle segment mangled into italics.
6. The response text is split around each table into separate Slack messages, posted **in order**: text before the table, then the table image, then text after it (repeated for each table found) — instead of appending all images after a single combined text reply. After each image upload, the bot polls `files.info` briefly to confirm Slack has actually registered the file-share message in the channel before posting the next segment — `files.completeUploadExternal` can return success before the share message is fully processed, and posting the next text chunk too early could otherwise cause the image to visually land *after* it despite being sent first.

Controlled by the `TABLE_RENDER_MODE` env var:
- `image` (default) — render tables as PNG images (this section).
- `ascii` — fall back to a fixed-width box-drawing monospace code block.

**Dependencies**: `weasyprint`, `pymupdf` (`fitz`), `pillow` — see [Prerequisites](#prerequisites). WeasyPrint needs system libraries (Cairo, Pango, HarfBuzz, GDK-PixBuf); on macOS: `brew install cairo pango gdk-pixbuf libffi`.

**Slack scope**: uploading images requires the `files:write` OAuth scope (see [Required OAuth Scopes](#required-oauth-scopes-bot-token)) — reinstall the app to Workspace after adding it.

---

## Markdown File Rendering

Any `.md` file on disk (SOP drafts, skill docs, ticket notes stored under `outputs/`, etc.) can be
sent to Slack as rendered image(s) with:

```
md outputs/PRJ-123-SOP.md
render /absolute/path/to/notes.md
```

- Relative paths resolve against `COPILOT_WORKDIR`; absolute paths (or `~/...`) are used as-is.
- Raw HTML in the file is stripped (`nh3` allowlist), and WeasyPrint may only load `data:` URIs: remote
  and local `<img>`, CSS `url()`/`@import` and `@font-face` are not fetched. A `.md` that embeds
  images via absolute `file://`/`http(s)` URLs renders without them.
- **Only triggers when the argument resolves to a real, existing file.** If it doesn't (e.g. a
  natural-language message like "render this MR to PDF" or "render a summary of the incident"),
  the message is *not* intercepted and passes straight through to the Copilot agent as normal
  text — this guards against the command swallowing ordinary sentences that happen to start with
  "render"/"md" and rejecting them with a spurious "file not found" error.
- This is a direct, deterministic Slack command — it does **not** go through the Copilot SDK/agent
  turn, so it works instantly and even if there's no active Copilot session (when it does match a
  real file).
- Pipeline: the file is parsed with the [`markdown`](https://python-markdown.github.io/) library
  (`extra`, `tables`, `fenced_code`, `sane_lists`, `toc` extensions) into HTML, styled with a
  GitHub-flavored CSS sheet (headings, code blocks, blockquotes, tables reusing the same word-wrap
  rules as [Table Rendering](#table-rendering)), then rendered via the same
  WeasyPrint → PyMuPDF → Pillow autocrop pipeline used for tables.
- **Pagination**: long documents are automatically split into multiple ~2200px-tall page images
  instead of one extremely tall, unreadable strip. Each split point is chosen at the nearest fully
  blank row to the target height, so a page break never lands mid-line, mid-table-row, or
  mid-code-line — tables and code blocks stay intact on one page whenever they fit. Multiple pages
  are uploaded together as one grouped Slack message (titled `file.md (page N/M)`), not one message
  per page.
- **Opaque rendering**: pages are rasterized fully opaque (no alpha channel) and autocropped against
  a white background rather than a transparent one. Plain text regions have no explicit CSS
  background, so a transparent render would show as **black** in Slack's dark lightbox/zoom view
  even though the thumbnail looks fine over white.
- Emoji in headings/body text get the same baseline-alignment fix as table cells (see `.emoji` CSS
  rule) so they don't appear to float above the surrounding text.
- **Dependency**: `markdown` (`pip install markdown`) in addition to the table-rendering deps.
