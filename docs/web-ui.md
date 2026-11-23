# Web UI

## Web UI

A local, password-protected browser front-end (`web_server.py` + `webui/`) runs
**inside the same process** as `dop_agent.py` (started as an `asyncio` task
alongside the Slack Socket Mode handler in `main()`) — no second bot instance,
no IPC, and it shares the exact same in-memory `_convos` dict, so a session is
never duplicated between transports.

**Why**: Slack requires either the desktop app or a phone to send/read
messages. The Web UI lets you use the exact same Copilot sessions from any
browser on the same laptop (or any device that can reach `127.0.0.1`, if you
port-forward/tunnel it yourself — not exposed beyond loopback by default).

### Access

- URL: `http://127.0.0.1:10818` (host/port configurable via `WEB_UI_HOST`/`WEB_UI_PORT` in `.env`).
- If you normally browse the Web UI via a different hostname than
  `WEB_UI_HOST` (e.g. bind stays `127.0.0.1` for the loopback-only bind, but
  you actually open it as `http://localhost:10818`), also set
  `WEB_UI_PUBLIC_HOST` to that hostname (e.g. `localhost`). It's used only
  when the bot *generates* links back into the Web UI (`session_create`'s
  `web_url`, watch-fire "continue in session" links, the New Session link,
  etc.) — if left unset it defaults to `WEB_UI_HOST`. Getting this wrong
  doesn't break anything server-side, but the generated links resolve to a
  different browser origin than the tab you're logged in under, so
  `webui/app.js`'s `parseOwnSessionLink()` (deliberately origin-scoped)
  won't recognize them as "ours" — they fall back to opening in a new tab,
  which then bounces to `/login` since that origin has its own separate
  session cookie.
- Auth: single shared password (`WEB_UI_PASSWORD` in `.env`). `WEB_UI_SECRET_KEY` is generated
  once and persisted automatically to `.env` if left blank; set a fixed value yourself in
  `.env.example`'s style to manage it explicitly.
- **Login attempts are rate-limited per IP**: 5 wrong passwords within 15
  minutes locks that IP out (HTTP 429) for the rest of the window, even if a
  later attempt uses the correct password — protects against unlimited
  automated password guessing if this port is ever exposed beyond loopback.
- **Hardening**: strict CSP (`script-src 'self'`, no CDN — all JS/CSS vendored in `webui/vendor/`, see `VERSIONS.txt`);
  markdown is sanitized with DOMPurify; every mutating `/api/*` request needs `X-Requested-With: dop`
  (added by `webui/csrf.js`) and a same-origin `Origin`; the session cookie is `Secure` over https and logout
  revokes it server-side; `X-Forwarded-For/Proto` are honoured only from peers listed in `WEB_UI_TRUSTED_PROXIES`.
- **Bind is loopback-only by design.** Don't expose this port to a LAN or the
  internet without adding real hardening (this is a single shared password
  behind a signed cookie plus basic rate-limiting, nothing more).
- **`WEB_UI_FRONTEND_ENABLED=false`** disables the browser frontend (`/`,
  `/login`, `/static/*` all 404) while `/api/*`, the SSE streams, and `/mcp`
  keep running — for a headless deployment that only talks to the bot via
  the MCP tools or the API, never a browser.

### What it does

- **Sidebar**: lists every known conversation from `bot_store.db`, including
  pinned state, title/preview, last update time, and whether the bot is
  currently working in that thread.
- **Transcript**: full history for a conversation, served entirely from
  `bot_store.db` (mirrored by `dop_agent.py` as messages are posted/updated).
  Markdown tables render as real `<table>` HTML (copy/paste-able), unlike
  Slack which renders them as PNG images (no native Block Kit table support).
- **Sending a message**: goes through the exact same `_handle()` turn
  processor Slack uses — the `md`/`render` shortcut, watchers, everything
  behaves identically. The Web UI records the user turn once in the shared
  transcript, then `ConversationBroadcaster` mirrors it to any other bound
  transport(s): if the conversation already has a Slack
  thread binding, Slack gets a `🌐 (via Web UI)` mirror post; if it doesn't,
  the message stays Web-only until a Slack binding exists later.
- **New session from the Web UI**: if a known Slack DM channel is available,
  the first Web-origin message can still create a fresh top-level Slack thread
  anchor automatically. If Slack isn't available/known, the session remains a
  valid Web-only conversation and still uses the same Copilot session model.
- **Live refresh**: Server-Sent Events (SSE) push updates immediately for both
  the session list and the open transcript; no browser polling loop.
- **Instant session switching**: clicking a different session clears the old transcript and
  fetches the new session's history over a plain REST call right away, without waiting for
  its SSE connection to deliver a first push.
- **Full-text search across sessions & messages**: a search box at the top
  of the sidebar (`#search-input`) defaults to matching **session titles
  only** — a fast, client-side filter over the already-loaded `sessions`
  list (`searchSessionTitles()`), no backend round trip. An "Also search
  messages" checkbox (`#search-include-messages`) opts into additionally
  querying `GET /api/search?q=...` (`message_store.search_messages()` — a
  plain case-insensitive `LIKE` scan over every message's content, not a
  separate FTS index; the store is small enough this stays fast) and
  merges those message-content matches in below the title matches (deduped
  by conversation id), each shown with a short snippet centered on the
  match plus the owning session's title. Toggling the checkbox re-runs the
  current query immediately. Clicking a result opens that session directly
  via the same `openSession()` used by the sidebar list, so you can jump
  straight to a remembered term without clicking through every session by
  hand.
- **Archive/hide sessions**: each sidebar item has an archive button
  (🗄️/📤, next to the pin button) that toggles an `archived` flag via
  `PUT /api/sessions/{id}/archive` (`message_store.set_archived()`).
  Archived sessions drop out of the default sidebar view — they still
  exist in the DB with full history, they're just tucked away — and only
  reappear (visually dimmed) via the "Archived" filter chip (see below).
  The session you currently have open stays visible even if you archive
  it (so it doesn't vanish out from under you mid-conversation); it
  disappears from the default view the next time you switch away from it.
- **Sidebar quick filters**: a row of chips above the session list (All /
  📌 Pinned / ⏳ Running / 🔵 Unread / 🗄️ Archived) narrows the list down
  to just that category client-side (no extra request — it filters the
  already-loaded `sessions` array), so you can answer "what's still
  running?" or "what haven't I read yet?" at a glance instead of scanning
  a long list by eye. Selecting a filter with no matches shows a plain
  "No … sessions" message instead of an empty-looking list.
- **Remembers your last-open session**: reloading the page (or reopening the
  browser tab) reopens whichever session you had open, including its unsent
  compose-box draft (drafts are stored in `localStorage`).
- **Stop button parity with Slack**: while a turn is running, a "⏹ Stop"
  button appears (`POST /api/sessions/{id}/stop`), wired to the exact same
  `CopilotConversation.interrupt()` used by Slack's "⏹️ Stop" button on the
  "Working..." status message — aborting a turn from either surface behaves
  identically. The button lives at the bottom of the live "Working..."
  status bubble inside the transcript itself (re-parented there by
  `renderMessages()` in `app.js`; it is a static DOM node outside
  `#messages`, so it survives the transcript's `innerHTML` wipes).
- **Transcript pane never overlaps the compose box**: the header health
  strip's height is accounted for in `#thread-view`'s layout so the compose
  box always stays fully visible.
- **Scroll position preserved on live updates while reading history**: if
  you've scrolled up in the transcript to read earlier messages, a passive
  live update (e.g. a watch-fired notice, or another turn streaming in)
  does not yank the view back down to the bottom — `renderMessages()`
  only re-pins to the bottom if you were already near it before the
  re-render. Sending a message yourself still always jumps to the bottom.
- **Unread indicator per session (server-side)**: a small dot appears next to
  a session's title in the sidebar whenever it has new activity you haven't
  seen yet. Read/unread state is a `last_read_at` column on `conversations`
  in `bot_store.db` (see `message_store.get_last_read()`/`mark_read()`),
  set via `PUT /api/sessions/{id}/read` and included in every
  `GET /api/sessions`/SSE session-list payload. `isSessionUnread()` in
  `app.js` just compares a session's `updated_at` to its `last_read_at`
  field. Opening a session — or simply having it be the currently-open one
  while new turns stream in — calls `markSessionRead()`, which optimistically
  updates the local session object for instant UI feedback, dedupes via an
  in-memory `markedReadUpTo` cache (so a render-triggered call doesn't spam
  the endpoint), and fires the `PUT` in the background. Read state is
  consistent across every tab, browser, and device hitting the same backend.
- **Jump to first unread session**: a 🔵 button in the sidebar header opens
  the first session (in the sidebar's own rendered order) that currently has
  an unread dot, so you don't have to scroll/hunt for it manually
  (`jumpToFirstUnread()` in `app.js`). If nothing is unread, the button
  briefly shakes instead of navigating anywhere.
- **Keyboard shortcuts**: `j`/`k` move focus down/up through the currently
  visible sidebar session list (respects the active filter/search); `/`
  jumps focus straight to the sidebar search box; `Cmd/Ctrl+Enter` sends
  the compose message (in addition to the existing plain-`Enter`-to-send
  behavior — useful right after a multi-line message where Shift+Enter was
  used for newlines). All shortcuts are ignored while focus is inside any
  input/textarea/contenteditable field, so normal typing (including typing
  the letters `j`/`k` or a `/` character into a message) is never hijacked
  (see `isEditableTarget()`/`navigateSessionList()` in `app.js`). Press `?`
  anywhere outside an editable field to open a read-only cheat-sheet
  overlay listing every shortcut (`Esc`, the ✕ button, or clicking outside
  the panel all close it).
- **Resizable/collapsible sidebar**: drag the thin handle on the sidebar's
  right edge to resize it (clamped between 180px and 600px); click the
  ‹/› circular button at the top of that edge to collapse/expand the
  sidebar entirely. Both the chosen width and collapsed state persist
  across reloads via `localStorage` (`dop_agent_sidebar_width_v1` /
  `dop_agent_sidebar_collapsed_v1` in `app.js`).
- **Session info popover**: click the ℹ️ button next to the thread title to
  see the current session's Copilot `session_id` (already tracked
  per-conversation - see `message_store.py`'s `copilot_session_id` column)
  and the bot's working directory (the fixed `COPILOT_WORKDIR` constant,
  exposed via `GET /api/health`'s `workdir` field), each with a 📋 button to
  copy the value to the clipboard. Handy for pairing with
  `copilot session logs <id>` or opening a terminal in the same workdir.
- **Thread header turn count / idle time**: the open session's title shows
  a compact `"N turns · idle Xm"` readout (`updateThreadMeta()` in
  `app.js`), hovering shows the exact last-activity timestamp. Refreshes on
  every sidebar update plus a 30s ticker, so it stays current even while
  you're just reading an old transcript.
- **Auto-growing compose textarea**: the compose box grows in height as you
  type multi-line messages (`autoGrowComposeText()` in `app.js`, capped at
  `max-height: 240px` in CSS, beyond which it scrolls internally as
  before). Re-applied any time text is set programmatically too — draft
  restore on session switch, clearing after send, retry-with-edit — not
  just on live typing.
- **Themes (dark / light / system)**: `webui/theme.js` (blocking `<head>` script, no flash) sets
  `<html data-theme>`; the preference lives in `localStorage['dop-theme']` (`dark` default for
  existing users, `light`, or `system` = follow `prefers-color-scheme` live). Switch via the health-strip
  button (cycles dark → light → system) or Settings → Browser → Appearance; synced across tabs via the
  `storage` event. `light.css` holds the palette overrides; mermaid/Chart.js re-render on the
  `dop-theme-change` window event. Tests: `ui_tests/test_light_theme.py`.
- **Syntax-highlighted code blocks with copy button**: fenced code blocks in
  assistant/user messages are highlighted via highlight.js (loaded from
  cdnjs, no build step — same CDN-`<script>` convention as `marked`/
  `emoji-js`) and get a floating 📋 button that copies the block's raw code
  (not the highlighted HTML) to the clipboard. Wired via `enhanceCodeBlocks()`
  in `app.js`, called on every rendered message bubble right after
  `renderMarkdown()`'s HTML is inserted — `marked.js` already produces the
  `<pre><code class="language-xxx">` structure highlight.js expects, so no
  markdown-parsing changes were needed.
- **Syntax-highlighted diff view**: fenced ` ```diff ` blocks get diff-aware
  coloring (added/removed line highlighting) for free — highlight.js's
  bundled "Diff" grammar is already part of the CDN build loaded for the
  code-block-highlighting feature above, and `enhanceCodeBlocks()` already
  runs on every code block regardless of language, so no additional
  app.js/CSS changes were needed for this one.
- **Auto-unfurl (registry-driven, since the integration registry): link regexes, url templates, status emoji, card fields and icons come from `GET /api/integrations/ui` (`furls.js`); `/api/unfurl` resolves URLs of ANY configured instance via `furl_api.py` (`integrations.runtime.get_registry()`; TTL = `unfurl.cache_ttl_s`).**
- **Auto-unfurl integration links into preview cards**: a link recognized by a configured integration (kinds declared in its `driver.yaml`; card data comes from the driver's `fetch`) shows a live summary. Cards use a two-row layout: row 1 is the app icon plus the short identifier and status emoji(s); row 2 is the title. The `/api/unfurl` lookup
  is **lazy**: `enhanceLinkPreviews()` only marks recognized links
  (`.unfurl-link`) when a message renders, it does *not* fetch anything up
  front — the request only fires the first time that specific link is
  hovered (`wireLazyUnfurl()`), showing a small spinner card
  (`createUnfurlSpinnerCard()`) floating near the cursor while the request
  is in flight, then swapping the *same* card element's content in place
  once the response resolves (so its position/hover-state stay valid). The
  result is cached per-link for the rest of the page's lifetime — repeat
  hovers never refetch — and a failed/unrecognized lookup removes the
  placeholder entirely with no retry. Cards float and follow the mouse
  pointer while hovering (positioned/clamped to stay on-screen via
  `positionUnfurlCardNearCursor()`), and stop following once the mouse
  moves into the card itself so you can click through to the linked
  ticket/MR/pipeline/job, with a short hide delay to absorb the gap between
  the link and the card while the mouse travels between them. Cards are
  appended to `<body>` (not inline in the message flow) so they can float
  freely; `renderMessages()` removes any leftover floating cards from a
  previous render before rebuilding message bubbles, since they'd otherwise
  be orphaned by `#messages`' `innerHTML` wipe. Backend: `GET
  /api/unfurl?url=...` in `web_server.py`, reusing the same
  the driver `fetch` (see `furl_api.py`), with a 60s in-memory
  cache per URL and a graceful `{"type": null}` fallback (no error
  surfaced) for unrecognized/unreachable links. Frontend:
  `enhanceLinkPreviews()` / `renderUnfurlCard()` in `app.js`, called on
  every rendered message bubble right after `enhanceCodeBlocks()`.
- **Long-session highlight**: sessions with more than
  `LONG_SESSION_TURN_THRESHOLD` (100) turns get a subtle orange left-border
  accent and a highlighted turn count in the sidebar (`isLongSession()` in
  `app.js`), flagging sessions whose large transcript/SSE payload may load
  or scroll more slowly before you click into them.
- **Private note-to-self (📝)**: a per-session free-text scratchpad, never
  sent to the agent and never rendered into the transcript — just a
  personal jotting space tied to that conversation (e.g. "waiting on
  Arunas' approval before merging"). Click 📝 in the thread header to open
  a small popover with a textarea; typing autosaves after a short debounce
  (and immediately on blur), with a "Saving…"/"Saved" status indicator.
  Backend: a new nullable `note` column on `conversations`
  (`message_store.get_note()`/`set_note()` — a pure metadata change, doesn't
  touch `updated_at`, same as pin/archive) and `GET`/`PUT
  /api/sessions/{conversation_id}/note`. Frontend: `#note-btn`/
  `#note-popover` in `index.html`, `openNotePopover()`/
  `loadNoteForCurrentSession()`/`saveNoteForCurrentSession()` in `app.js` —
  guarded against a load/edit race (if you start typing before the initial
  load of an existing note finishes, the fresh edit wins, not the stale
  fetch) and cleared immediately when switching sessions so one thread's
  note can never flash into view under a different one.
- **Thread header (two rows + overflow menu)**: `.thread-header-top` holds the title
  (truncated with an ellipsis) and the meta (turn count/idle time) pinned to the right edge.
  `.thread-header-toolbar` holds ℹ️ Info / 📝 Note / ⭐ Bookmarks / 👁 Watches as
  `.header-chip` buttons, the turn-elapsed text, a "⋯" `#thread-overflow-btn` and
  `#thread-overflow-menu` with 🖨️ Print / ↪ Continue in new session / the Slack ↗ link
  (`openThreadOverflowMenu()`/`closeThreadOverflowMenu()` in `app.js`, outside-click-to-close).
  `#thread-watches-btn` stays inline so a broken watch on the current thread is visible at a
  glance. The ℹ️/📝/⭐ popovers are children of `.thread-header` so `top: 100%` anchors
  against the header; the popover tests assert their `getBoundingClientRect()` lands near the
  trigger button.
- **Deep link to a specific message (hover timestamp)**: hover any message
  bubble to reveal its timestamp, which doubles as a "copy link" button —
  clicking it copies a URL of the form
  `#msg=<conversationId>:<messageId>` to the clipboard and briefly flashes
  "✅ Copied" in place of the time — the message id is the same stable row
  id `_transcript_payload()` already returns, so no backend changes were
  needed. Opening that URL (fresh tab, or
  pasting it into the already-open app) switches to the right session,
  scrolls the target message into view, and briefly flashes a highlight
  (`.msg-row-deep-link-highlight`, a 2s CSS box-shadow animation) so it's
  easy to spot in a long transcript. Handled in `app.js` via
  `parseDeepLinkHash()`/`deepLinkUrlFor()`/`pendingDeepLinkMessageId`: a
  fresh page load parses `location.hash` in `init()` (taking priority over
  the normal "reopen last session" behavior if the target conversation
  exists); a same-document hash change (e.g. pasting a new deep link while
  the tab is already open, which browsers do NOT reload the page for) is
  handled by a separate `hashchange` listener so the feature works in both
  cases.
- **Bookmark/star a message (☆/⭐)**: hover a message bubble to reveal a
  ☆ button (stays visible and turns ⭐ once starred, unlike the hover-only
  timestamp/deep-link control) to star it for quick recall later. A ⭐ button in the
  thread header opens a popover listing every starred message in the
  current session with a short text preview — clicking an entry jumps to
  and highlights that message, reusing the same `pendingDeepLinkMessageId`
  scroll/highlight machinery the deep-link feature already established.
  Backend: a new `message_bookmarks(conversation_id, message_id,
  created_at)` table (`message_store.get_bookmarked_message_ids()`/
  `set_message_bookmarked()` — a separate table rather than a column on
  `messages`) and
  `PUT /api/sessions/{conversation_id}/messages/{message_id}/bookmark` +
  `GET /api/sessions/{conversation_id}/bookmarks`. `_transcript_payload()`
  includes a `bookmarked` flag per turn. Like pin/archive/note,
  bookmarking is a metadata change and does not touch the conversation's
  `updated_at`.
- **Print-friendly transcript (🖨️)**: a print button in the thread header
  (next to the bookmarks ⭐ button) triggers `window.print()`. A new
  `@media print` stylesheet block hides all interactive chrome (sidebar,
  health strip, thread header, note/bookmarks/session-info popovers, stop
  button, queued badge, retry-last-message row, compose form, the
  per-message hover buttons, avatars) and switches the transcript from the
  dark Slack palette to plain black-on-white for paper/PDF output, with
  each message row set to `page-break-inside: avoid` so bubbles don't split
  across pages. While printing, `beforeprint`/`afterprint` listeners
  temporarily retitle the document to `Copilot transcript - <session
  title>` (restored afterward) so a saved PDF's default filename is
  meaningful.
- **Warn before closing tab with an unsent draft**: a `beforeunload`
  listener calls `preventDefault()`/sets `event.returnValue` whenever the
  compose box has non-whitespace text in it, triggering the browser's
  native "leave site?" confirmation on tab close/reload/navigation. Drafts
  are already persisted per-session to `localStorage` (see the
  `setDraft`/`getDraft` cache), so nothing is actually lost on an
  accidental close — this is purely a safety prompt to avoid forgetting an
  in-progress message or sending it from the wrong tab after a reload. No
  warning fires once the draft is empty, whitespace-only, or has been sent.
- **Settings screen (⚙️) — quiet-hours toggle for notifications**: a new
  Settings overlay, opened via a minimal "Settings" text link in the top
  health strip (`#settings-link`, moved out of the sidebar header to
  declutter it — icon-free, minimal, right-aligned; see the "Global
  watchers view" bullet below for the sibling "Watchers" health-strip
  item), opened/closed the same way as the
  Watches/Shortcuts overlays (backdrop click or Escape to close). First
  setting: a quiet-hours toggle plus a start/end local time-of-day range
  (default 22:00 → 08:00) that suppresses the P1 "turn finished" browser
  notification while active — the window correctly wraps past midnight.
  Settings are stored in `localStorage` (`loadSettings`/`saveSettings`, key
  `dop_agent_settings_v1`) rather than the backend, since they're a
  per-browser UI preference, not something that needs to sync across
  devices or survive on the server. This screen is intentionally the first
  item in what will likely grow into a broader Settings panel (timeout
  config, theme, etc.) over time.
- **Errored-session flag**: sessions whose last turn ended in a known
  failure/timeout wording (the same text `handlers.py` produces — see the
  "Retry last message" bullet above) get a red left-border accent and a
  ⚠️ badge next to the title in the sidebar, without needing to open the
  session first. Backed by `message_store.list_conversations()`'s
  `last_role` field plus `web_server._session_payload()`'s `errored`
  computation (only true while the session is currently idle — a session
  that errored once but has since started a new turn or succeeded is not
  flagged).
- **Distinct message styling per role**: user, assistant, and
  system/watch-injected messages (watch-fired notices, interrupt notices,
  "continue in new session" backlinks, broken-watch warnings, etc. — all
  recorded server-side with `role="system"`) each get a visually distinct
  transcript style, not just a different avatar — user/assistant bubbles
  differ in background/alignment, while system bubbles use a dashed
  border, italic text, smaller font, and muted color so they read
  unambiguously as "injected by the system", not part of the actual
  human↔agent conversation.
- **Live SSE connection indicator**: a "Live" item in the header health
  strip reflects whether the browser's EventSource connections
  (`/api/stream/sessions` for the sidebar, plus the per-conversation
  stream while a thread is open) are currently connected — green "Live"
  normally, red "Reconnecting…" if either stream's `onerror` fires (the
  browser's native EventSource retry then reconnects automatically;
  `onopen`/each received event flips the indicator back to green).
- **Multi-tab consistency**: draft text and "last session opened" are kept in
  `localStorage`, and each tab only loads them into an in-memory cache once at
  startup — without extra wiring, one tab's changes wouldn't show up in
  another already-open tab until it reloaded. A
  `window.addEventListener('storage', ...)` listener (fires natively in every
  *other* same-origin tab whenever `localStorage` changes) refreshes the
  drafts cache immediately when another tab edits a draft. Per-session
  read/unread state, however, is server-side (see the unread-indicator bullet
  above) and reaches every open tab — including ones in a different browser
  or on a different device — via the existing `/api/stream/sessions` SSE
  feed, not the `storage` event, so marking a session read in one tab clears
  its unread dot everywhere else with no reload needed.
- **Turn-finish browser notification**: when a running turn transitions to
  finished while the browser tab isn't focused/visible, a Notification
  API popup ("Turn finished" / session title) fires so you don't have to
  keep tabbing back to check on a long-running turn. Permission is
  requested lazily the first time a turn is actually seen running, not on
  page load, to avoid an unsolicited prompt just from opening the UI.
- **"Continue in new session"**: a "↪ Continue in new session" button in
  the thread header (`POST /api/sessions/{id}/continue`) creates a
  brand-new, standalone conversation seeded with a system message linking
  back to the session you came from (`message_store.get_display_title()`
  for the label, an in-app `#session:<id>` markdown link that the Web UI
  intercepts to jump straight back instead of following it as a dead
  browser anchor). Useful for escaping an overly-long/slow thread (see the
  long-session highlight above) without losing your way back to the
  original context.
- **Global watchers view**: a **"Watchers" health-strip item**
  (`#health-watches`, right after the "MCP" item, with its own status dot — see the
  "Header health/status strip" bullet below) opens a wider panel (`.watches-panel-wide`)
  listing every registered watch across *all* threads (`GET /api/watches`,
  backed directly by `watchers.list_watches()` — no separate storage),
  carrying a live count badge next to the label (`#health-watches-count`,
  turns red if any watch is broken). The panel was reworked (2026-10 UX
  pass, after the original read-only version was felt to be missing too
  much) into:
  - **Grouped by type** (MR/pipeline/job/ticket/teams/time/self-restart),
    each group collapsible with state persisted in `localStorage`
    (`watchesCollapsedGroups`), sticky group headers, and a per-type
    left-border color accent on every row matching the legend.
  - **A summary bar** (`#watches-summary`) with active/broken/paused/done
    count chips, plus a "Showing X of Y" note whenever search or
    "Broken only" is filtering the list.
  - **A legend** (`#watches-legend`) spelling out the emoji→type and
    dot-color→status vocabulary, so the row accents aren't tribal
    knowledge.
  - **Search** (`#watches-search`, 150ms debounce) filtering by label or
    key substring, and a **"Broken only"** checkbox
    (`#watches-broken-only`) — both re-filter the already-fetched list
    client-side rather than re-querying the API.
  - **Per-row links (no background checks)**: `ticket`
    rows get a plain link built purely from the watch key (`watchLinkUrl()`
    in `app.js`) — no network call when the panel renders; the existing lazy
    hover unfurl card (`wireLazyUnfurl`) fetches only on first hover.
    `project@ref` pipeline watches and time/Teams/self-restart watches get
    no link.
  - **Inline row actions** — ⟳ re-check now (`POST .../recheck`, disabled
    once a watch isn't `active` or is a `self_restart`), ⏸/▶ pause/resume
    (`POST .../pause` / `.../resume`), and 🗑 remove (`DELETE
    /api/watches/{id}`, with a `confirm()` prompt) — all backed by new
    `web_server.py` endpoints that call the exact same `mcp_tools.py`
    functions the `watch_*` MCP tools use, so Web-UI actions and
    agent/MCP actions can never diverge in behavior. The panel is **no
    longer read-only**.
  - **Broken-reason and next-fire-time surfaced inline**: a `broken`
    watch shows its `meta.broken_reason` under the row; an active `time`
    watch shows its next scheduled fire time and whether it's recurring.
  - **Monospace watch key**, **relative "added"/"last checked" timestamps**
    (reusing the existing `timeAgo()` helper), and a **session link**
    (reusing `sessionLinkUrlFor()`) back to the watch's own
    `conversation_id` on every row.
  - **Loading skeleton** while the initial fetch is pending, and a
    **"Retry" button** if it fails, instead of a bare "Loading…"/"Failed"
    string.
  - **Auto-refreshes while open**: piggybacks on the existing 30s
    `HEALTH_POLL_INTERVAL_MS` health-strip poll to silently re-fetch
    (`loadWatches(true)`: no skeleton, no re-render if unchanged, scroll position kept) whenever the panel is currently visible, rather than
    requiring a manual reopen to see new state — deliberately *not* wired
    into the `/api/stream/...` SSE broadcaster (that's shared, higher-risk
    plumbing used for session streaming); 30s "fresh enough" staleness was
    judged an acceptable trade-off for the lower-risk implementation.

  By default, watches with status `done` are hidden; a "Show done/inactive"
  checkbox toggles `include_inactive=true` on the API call to reveal them
  too (note: `paused`/`broken` watches are always shown regardless of this
  toggle — only `done` is filtered by default).
- **Per-session watchers view**: alongside the global "Watchers" health-strip
  item, each open session's thread header has a 👁 Watches chip
  (`#thread-watches-btn`, also carrying its own count badge,
  `#thread-watches-count`) that opens the same Watches panel pre-filtered to
  *that session's own `conversation_id`* (`GET /api/watches?conversation_id=...`,
  reusing `watchers.list_watches()`'s existing `conversation_id` filter — no
  new backend logic was needed, just exposing it through the API/UI). Watches
  are keyed by `conversation_id`, not by Slack thread, so this works
  identically for Slack-bound sessions and for sessions created purely from
  the Web UI (no underlying Slack thread) — both can register and see their
  own watches; there is no "no watches possible" restriction. The panel
  title changes to "Watches — this session" and a "Show all sessions"
  checkbox lets you flip back to the global list without closing the panel.
  The chip also carries its own status dot (`#thread-watches-dot`,
  `refreshThreadWatchesHealth()`) scoped to just that session's watches —
  hidden entirely for a session with none registered, green once one exists
  and none are broken, red the moment any of *that session's* watches gets
  marked broken — independent from the global health-strip dot, which
  reflects every session's watches combined. Refreshed on the same 30s
  cadence as the rest of the health strip, and immediately on switching
  sessions (`updateThreadHeader()`), so it never shows a stale color left
  over from whichever session was open before.
- **Session timeout (12h)**: the signed login cookie (`_COOKIE_MAX_AGE` in
  `web_server.py`) expires after 12 hours — after
  that, any request is treated as unauthenticated and redirected to
  `/login`. This is a deliberate trade-off (more frequent re-logins) for a
  shorter-lived session on a password-only auth surface; re-authenticating
  is a single password entry, no other state is lost (drafts/last-open
  session are stored separately in `localStorage`, unaffected by cookie
  expiry).
- **Header health/status strip**: a thin bar above the sidebar/thread panes,
  always visible, polling `GET /api/health` every 30s (also fetched once at
  page load). Shows five signals:
  - **Slack** dot — green/red based on a **live** `auth_test()` call against
    the Slack Web API made by the backend on each `/api/health` request (not
    just "is the process alive").
  - **MCP** dot — green/red based on whether any MCP servers were **loaded
    from `~/.copilot/mcp-config.json` at bot startup**
    (`bot_config.MCP_SERVERS`). This is a config-presence check only, **not**
    a live per-server connectivity ping — hovering the dot lists the
    configured server names.
  - **Watchers** dot (`#health-watches`, right after MCP) — green/red based
    on a separate `GET /api/watches?include_inactive=false` poll (same
    30s cadence, own `refreshWatchesHealth()` function): green while no
    registered watch has status `broken`, red the moment any watch has been
    flipped to `broken` (see `watchers.mark_broken()` — a confirmed-permanent
    failure like a 404'd MR/pipeline/job, not a transient poll error), so a
    silently-broken watch is visible at a glance instead of only
    discoverable by opening the panel. This item is also the click target
    that opens the global watches modal (see "Global watchers view" above)
    — unlike the other four signals, it's underlined on hover
    (`.health-item-clickable`) to read as clickable, not just a status
    readout.
  - **Uptime** — wall-clock time since `web_server.py` was imported (i.e.
    process start), formatted as `Xd Yh` / `Xh Ym` / `Xm`.
  - **Version** — short git commit hash (`git rev-parse --short HEAD`,
    computed once at import time), falling back to `unknown` if not running
    from a git checkout.
  - Also hosts a "Settings" text link (see above), right-aligned via a
    `.health-strip-spacer` flex spacer — icon-free, minimal, just clickable
    text.
- **App logo/favicon**: the sidebar header and login page both show a
  small logo (`webui/icons/logo.png`), also wired as the browser tab
  favicon (`<link rel="icon">` in `index.html`/`login.html`) — the project logo. `.sidebar-logo`/`.login-logo` carry a small `margin-right` so the
  icon isn't flush against the adjacent "Sessions"/"DOP Agent" text.
- **Message timestamp shown on row hover (doubles as copy-link button)**:
  the full timestamp for a message renders as a small
  `.msg-hover-time` element inside the same hover-reveal row as the ⭐
  bookmark button, fading in together with it on `.msg-row:hover`. The
  timestamp itself is clickable — see the "Deep link to a specific
  message" bullet above; there is no separate 🔗 button any more. Clicking
  it appends `✅ Copied` after the existing time text (e.g. `10:32 AM ✅
  Copied`) rather than replacing it, so the actual timestamp stays visible
  throughout the ~1s confirmation window.
- **Cache-busted static assets**: `GET /` does not serve `index.html`
  as-is — `index_page()` reads it and appends
  `?v=<mtime>` to the `/static/app.js` and `/static/style.css` URLs based
  on each file's on-disk modification time, so a *fresh* page load always
  fetches the latest JS/CSS after any change to these files.
- **Auto-reload an already-open tab on bot restart**: cache-busting does not help a tab
  opened before a restart. `refreshHealth()` (the 30s `/api/health` poll behind the header
  health strip) remembers the git-commit `bot_version` seen on the tab's first poll and calls
  `location.reload()` when a later poll reports a different one. Covered by
  `ui_tests/test_auto_reload_on_version_change.py`, which intercepts `/api/health` to
  simulate a version change.
- **Live "Working... Ns" elapsed counter**: while a turn is running, a
  `⏱ Working... Ns` label appears next to the Stop button and ticks up
  every second client-side (`setInterval` in `app.js`), independent of the
  slower ~15s server-side status heartbeat that refreshes the actual
  message text. Backed by `CopilotConversation.turn_started_at` (an epoch
  timestamp set at the start of `send()` and cleared in its `finally`
  block), exposed via `turn_started_at` on both `GET /api/sessions` and
  `GET /api/sessions/{id}` — the same fields used for `running`. Hidden
  whenever no turn is in progress for the open session.
  - **Timeout warning**: the counter also turns amber ("warning") once the
    remaining time before the configured `SESSION_TIMEOUT` drops to
    whichever is larger of 20% of the total timeout or 30s, and red
    ("critical") in the final 10s — giving visible advance notice that a
    turn may be about to hit its per-turn timeout (see the "Live progress
    status" section above for what actually happens server-side once it
    does). `SESSION_TIMEOUT` itself is fetched via the health strip's
    `GET /api/health` (`session_timeout_seconds` field, refreshed every
    30s) so the frontend always reflects the real configured value.
- **One-click "Retry last message"**: if the last turn in the open session
  is an assistant reply matching a known failure/timeout wording (the same
  `:warning: DOP Agent request timed out...` / `:x: Error: ...` text
  `handlers.py` produces when `convo.send()` raises), a small
  "🔄 Retry last message" button appears below it. Clicking it resends the
  immediately preceding user message's exact text as a brand-new
  `POST /api/send` — there's no concept of "resuming" the exact failed
  turn, so a fresh send (which reuses the same Copilot session/history like
  any other message) is the simplest reliable recovery. The button is
  purely client-side detection (`isErrorResponseText()` in `app.js`); no
  new backend state was added for this.
- **"Queued" badge on interrupting sends**: `CopilotConversation.send()`
  aborts any in-progress turn immediately rather than literally queuing
  behind it (see `copilot_session.py`) — but that interrupt+restart still
  takes a moment to be reflected back over SSE. If you submit a new
  message while the current session shows a running turn (Stop button
  visible), a small amber "⏳ Interrupting current turn — your message is
  queued…" badge appears above the compose box immediately on click, and
  clears as soon as the next state update for that conversation arrives.
  Purely a client-side UX cue (`queuedBadgeEl`/`lastRunning` in `app.js`);
  no backend change.
- **Settings deep links**: `#settings[=<tab>][&q=<text>]` opens the Settings
  overlay on a tab (`integrations`, `browser`, `agent`, `transcription`,
  `adapters`, `mcp`, `watchers`, `unfurl`, `advanced`) and/or prefills the
  search box, e.g. `http://localhost:<port>/#settings=watchers` or
  `#settings&q=timeout`. Works on fresh load, on pasted hashes and on clicked
  in-page links. Settings links in messages also get a hover unfurl card
  (`own_settings`: ⚙️ Settings + tab, with the tab/search in row 2).
- **Session link with context prefill** (`#open=<id>&draft=<text>`): the
  existing `#open=<conversationId>` link (no specific message target — see
  the deep-link-to-a-message feature above) optionally takes an
  `&draft=<url-encoded text>` suffix. Opening such a link switches to
  the referenced session like a plain `#open=` link, but also
  merges that text into the compose box — appended below any existing
  unsent draft with a blank line, never replacing it (`applyDraftPrefill()`
  in `app.js`, sharing the same per-conversation draft cache normal typing
  uses), then focuses the box with the caret at the end so you can start
  editing/sending immediately. Re-opening/re-clicking the exact same link a
  second time is a no-op rather than duplicating the text (a plain
  substring check against the current draft — see `applyDraftPrefill()`'s
  docstring for why this is sufficient). Use this for a "click here to
  continue with this context" link, e.g. a suggested follow-up prompt in a
  watch-fire reminder — `web_server.py`'s `_web_ui_url_for()` takes an
  optional `draft_text` parameter to build these server-side.
  `parseDeepLinkHash()`/`parseOwnSessionLink()` share one
  `parseSessionHash()` parser so the `#msg=`/`#open=` hash forms can't drift
  out of sync with each other. `ui_tests/test_session_link_with_prefill.py`
  covers the no-draft-param no-op case, fill-when-empty, append-below-
  existing-draft, the re-apply-is-idempotent case, and focus.
- **New-session link with context prefill** (`#new[&draft=<text>]`): the
  same context-prefill mechanism as above, but for starting a brand-new
  session instead of opening an existing one — a `#new` hash link is
  equivalent to clicking the "New Session" button, and `#new&draft=<url-
  encoded text>` pre-fills that new session's compose box the same way
  (append-not-replace, idempotent re-apply, auto-focus — reusing
  `applyDraftPrefill()`/`focusComposeTextAtEnd()` unchanged, since the
  "new session" slot already shared the same draft-cache key
  (`NEW_SESSION_DRAFT_KEY`)). The button and the hash path share `startNewSession()`;
  the current compose text is saved back into the draft cache only when switching away from a
  real session. `web_server.py`'s `_web_ui_new_session_url()` builds these links server-side,
  mirroring `_web_ui_url_for()`'s `draft_text` parameter. `ui_tests/test_new_session_link_with_prefill.py`
  covers the plain-`#new`-switches-away-from-a-real-session case,
  fill-when-empty, append-below-an-existing-unsent-new-session-draft, the
  re-apply-is-idempotent case, focus, and that an existing session's own
  unsent draft survives switching to `#new` and back.

### Limitations

- File/image uploads still render as textual markers in the Web UI rather than
  inline attachments.
- Voice/audio message recording is supported from the Web UI (see
  [Voice Transcription](#voice-transcription)), but only within an
  already-open session — there's no lazy-create-a-session-from-voice flow
  (arbitrary file *upload*, as opposed to mic recording, is Slack- and Telegram-only).
- No per-session/global Slack on/off toggle: once a conversation has a Slack
  binding, Web-origin messages always mirror there.
