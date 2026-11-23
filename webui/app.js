let sessions = [];
let groups = [];
let currentConversationId = null;
let lastTurns = [];
let sessionListStream = null;
let conversationStream = null;
// Configured per-turn timeout (seconds), learned from GET /api/health -
// used by the elapsed-time ticker to warn as a running turn approaches it.
let sessionTimeoutSeconds = null;
// Copilot working directory (fixed/global, from COPILOT_WORKDIR - see
// bot_config.py), learned from GET /api/health. Shown in the session info
// popover alongside the per-session copilot_session_id.
let botWorkdir = null;

// Deep-link URL to a specific message (#msg=<conversationId>:<messageId>).
// Parsed from location.hash; takes priority over "reopen last session".
// Once the transcript has rendered we scroll to and highlight the message,
// then clear it so later session switches don't re-trigger the scroll.
//
// A second, simpler hash form (#open=<conversationId>) just opens a
// conversation with no specific message target - used by session_create's
// returned web_url (an agent-created session has no particular message to
// scroll to, it just needs a working link into the Web UI).
//
// A further optional "&draft=<url-encoded text>" suffix on the #open= form
// (never on #msg=, which already has a specific message target) pre-fills
// the compose box with that text on open - e.g. a bot-generated "click here
// to continue with this context" link. See applyDraftPrefill() below for
// the merge/dedup semantics.
let pendingDeepLinkMessageId = null;

// Shared by parseDeepLinkHash() (reads location.hash) and
// parseOwnSessionLink() (reads an arbitrary href's hash) so the two forms
// can never drift out of sync with each other.
function parseSessionHash(hash) {
  hash = hash || '';
  let match = /^#msg=([^:]+):(.+)$/.exec(hash);
  if (match) {
    return {
      conversationId: decodeURIComponent(match[1]),
      messageId: decodeURIComponent(match[2]),
      draftText: null,
      isNew: false,
    };
  }
  match = /^#open=([^&]+)(?:&draft=(.*))?$/.exec(hash);
  if (match) {
    return {
      conversationId: decodeURIComponent(match[1]),
      messageId: null,
      draftText: match[2] ? decodeURIComponent(match[2]) : null,
      isNew: false,
    };
  }
  // "#new[&draft=<text>]" - equivalent to clicking the New Session button
  // (see startNewSession()), optionally pre-filling the compose box the
  // same way "#open=<id>&draft=<text>" does for an existing session. Has
  // no conversationId of its own - the not-yet-created "new session" slot
  // shares the NEW_SESSION_DRAFT_KEY draft-cache entry (getDraft(null)/
  // setDraft(null, ...)), same as the button always has.
  match = /^#new(?:&draft=(.*))?$/.exec(hash);
  if (match) {
    return {
      conversationId: null,
      messageId: null,
      draftText: match[1] ? decodeURIComponent(match[1]) : null,
      isNew: true,
    };
  }
  return null;
}

// "#settings[=<tab>][&q=<search text>]" opens the settings overlay on a tab
// and/or with the search box prefilled. Not a session link, so it is parsed
// separately from parseSessionHash().
function parseSettingsHash(hash) {
  const m = /^#settings(?:=([^&]*))?(?:&q=(.*))?$/.exec(hash || '');
  if (!m) return null;
  return { tab: m[1] ? decodeURIComponent(m[1]) : null, q: m[2] ? decodeURIComponent(m[2]) : '' };
}

function settingsLinkUrlFor(tab, q) {
  let h = `${window.location.origin}${window.location.pathname}#settings`;
  if (tab) h += `=${encodeURIComponent(tab)}`;
  if (q) h += `&q=${encodeURIComponent(q)}`;
  return h;
}

function applySettingsHash(hash) {
  const link = parseSettingsHash(hash);
  if (!link) return false;
  if (settingsOverlay.style.display !== 'flex') openSettingsOverlay();
  if (window.DopSettings) window.DopSettings.openTo(link.tab, link.q);
  return true;
}

function parseDeepLinkHash() {
  return parseSessionHash(window.location.hash);
}

function deepLinkUrlFor(conversationId, messageId) {
  return `${window.location.origin}${window.location.pathname}#msg=${encodeURIComponent(conversationId)}:${encodeURIComponent(messageId)}`;
}

// Builds a "#open=<id>[&draft=<text>]" link - the counterpart to
// deepLinkUrlFor() above for when there's no specific message to scroll to,
// just (optionally) some context text to pre-fill the compose box with.
function sessionLinkUrlFor(conversationId, draftText) {
  const base = `${window.location.origin}${window.location.pathname}#open=${encodeURIComponent(conversationId)}`;
  return draftText ? `${base}&draft=${encodeURIComponent(draftText)}` : base;
}

// Builds a "#new[&draft=<text>]" link for starting a brand-new session
// (counterpart of sessionLinkUrlFor(), which needs an existing conversationId).
function newSessionLinkUrlFor(draftText) {
  const base = `${window.location.origin}${window.location.pathname}#new`;
  return draftText ? `${base}&draft=${encodeURIComponent(draftText)}` : base;
}

// Recognizes a link (absolute or relative) as pointing at one of THIS
// bot's own sessions - i.e. it would resolve to a #open=<id> or
// #msg=<id>:<msgId> hash on the current origin. These show up whenever a
// bot-generated link gets pasted/rendered back into a bubble: session_
// create's returned web_url, a "continue in new session" link, a bookmark
// shared between threads, etc. Deliberately origin-scoped (not matched
// against some other known bot hostname) - this Web UI can be reached via
// several different hostnames/tunnels (localhost, a reverse proxy, a
// tailnet name...) and the only reliable signal that a link is "one of
// ours" without a hardcoded host allowlist is that it was rendered by,
// and thus resolves against, the page currently viewing it.
function parseOwnSessionLink(href) {
  let url;
  try {
    url = new URL(href, window.location.href);
  } catch (e) {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  return parseSessionHash(url.hash || '');
}

// Same-origin "#settings..." link (absolute or relative) -> {tab, q} | null.
function parseOwnSettingsLink(href) {
  let url;
  try { url = new URL(href, window.location.href); } catch (e) { return null; }
  if (url.origin !== window.location.origin) return null;
  return parseSettingsHash(url.hash || '');
}

// Merges a link-supplied "context" text into a session's draft (the unsent
// compose-box text, backed by the same per-conversation cache normal typing
// uses - see getDraft()/setDraft() above) WITHOUT ever clobbering whatever
// the user already has typed there. Appended below any existing draft with
// a blank line, never replacing it.
//
// Idempotent by design: re-opening/re-clicking the exact same link a second
// time (e.g. the 'already on this hash' branch in the click handler below,
// where 'hashchange' never fires again for an identical hash) must NOT
// duplicate the same block of text every time. A plain substring check
// against the current draft is enough here - false positives (the user
// happening to have already typed this exact text themselves) just mean we
// skip a redundant append, which is harmless.
function applyDraftPrefill(conversationId, text) {
  if (!text) return false;
  const current = getDraft(conversationId);
  if (current.includes(text)) return false;
  setDraft(conversationId, current ? `${current}\n\n${text}` : text);
  return true;
}

// Focuses the compose box with the caret at the end (after a draft-prefill link).
function focusComposeTextAtEnd() {
  composeText.focus();
  const end = composeText.value.length;
  composeText.setSelectionRange(end, end);
}

// Per-session draft (unsent compose text) cache, keyed by conversation_id.
// The "new session" (no conversation_id yet) slot uses this sentinel key.
const NEW_SESSION_DRAFT_KEY = '__new__';
const DRAFTS_STORAGE_KEY = 'dop_agent_drafts_v1';
// Remembers which session was open so a page reload reopens it (and its draft).
const LAST_SESSION_STORAGE_KEY = 'dop_agent_last_session_v1';

function loadDrafts() {
  try {
    return JSON.parse(localStorage.getItem(DRAFTS_STORAGE_KEY) || '{}');
  } catch (e) {
    return {};
  }
}

function saveDrafts(drafts) {
  try {
    localStorage.setItem(DRAFTS_STORAGE_KEY, JSON.stringify(drafts));
  } catch (e) {
    console.error('saving drafts failed', e);
  }
}

let drafts = loadDrafts();

function draftKeyFor(conversationId) {
  return conversationId || NEW_SESSION_DRAFT_KEY;
}

function setDraft(conversationId, text) {
  const key = draftKeyFor(conversationId);
  if (text) {
    drafts[key] = text;
  } else {
    delete drafts[key];
  }
  saveDrafts(drafts);
}

function getDraft(conversationId) {
  return drafts[draftKeyFor(conversationId)] || '';
}

// Drafts still live only in localStorage (unlike read/unread state below,
// they're intentionally per-browser scratch state, not synced server-
// side). The native "storage" event fires in every *other* same-origin
// tab/window whenever localStorage changes (never in the tab that made
// the change) - this keeps this tab's in-memory `drafts` cache fresh so
// switching sessions (or opening a fresh tab) here picks up the latest
// draft from another tab, without clobbering whatever's actively being
// typed in this tab's own compose box.
window.addEventListener('storage', (event) => {
  if (event.key === DRAFTS_STORAGE_KEY) {
    drafts = loadDrafts();
  }
});

function saveLastSession(conversationId) {
  try {
    if (conversationId) {
      localStorage.setItem(LAST_SESSION_STORAGE_KEY, conversationId);
    } else {
      localStorage.removeItem(LAST_SESSION_STORAGE_KEY);
    }
  } catch (e) {
    console.error('saving last session failed', e);
  }
}

function loadLastSession() {
  try {
    return localStorage.getItem(LAST_SESSION_STORAGE_KEY) || null;
  } catch (e) {
    return null;
  }
}

// --- Read/unread tracking (server-side) ---------------------------------
// Backed by the conversations.last_read_at DB column
// (see message_store.mark_read()/get_last_read()) - the sessions[] array
// carries last_read_at from GET /api/sessions and the sessions SSE stream,
// so every tab/device sees the same read state. markSessionRead() only sends a
// PUT when the local view is actually stale, to avoid hammering the API
// on every SSE-triggered re-render.
const markedReadUpTo = {};

async function markSessionRead(conversationId, updatedAt) {
  if (!conversationId || !updatedAt) return;
  if (markedReadUpTo[conversationId] === updatedAt) return;
  // Optimistic: update immediately so isSessionUnread() and the sidebar
  // badge react without waiting on the round trip, then confirm server-side.
  markedReadUpTo[conversationId] = updatedAt;
  const session = sessions.find((s) => s.conversation_id === conversationId);
  if (session) session.last_read_at = updatedAt;
  try {
    await fetch(`/api/sessions/${encodeURIComponent(conversationId)}/read`, { method: 'PUT' });
  } catch (e) {
    console.error('marking session read failed', e);
  }
}

// Sessions with a very high turn count can behave sluggishly (large SSE
// payloads, long transcripts) - flag them visually so it's obvious before
// opening rather than after noticing lag.
const LONG_SESSION_TURN_THRESHOLD = 100;

function isLongSession(session) {
  return !!session && typeof session.turn_count === 'number' && session.turn_count > LONG_SESSION_TURN_THRESHOLD;
}

function isSessionUnread(session) {
  if (!session || !session.updated_at) return false;
  if (session.conversation_id === currentConversationId) return false;
  if (!session.last_read_at) return true;
  return parseIso(session.updated_at).getTime() > parseIso(session.last_read_at).getTime();
}

function parseIso(iso) {
  // Timestamps from the backend can be naive (no tz suffix, assume UTC),
  // `Z`-suffixed, or an explicit numeric offset like `+00:00` (e.g.
  // Python's `datetime.now(timezone.utc).isoformat()`, used for watch
  // timestamps). Appending "Z" to a string already ending in `+00:00`
  // yields an Invalid Date, so only append it when there is no timezone info.
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasTz ? iso : `${iso}Z`);
}

// Jump-to-first-unread (🔵 sidebar button). "First" follows the sidebar's
// rendered order (pinned-then-recency), i.e. the unread session nearest the top.
function jumpToFirstUnread() {
  const target = sessions.find((s) => isSessionUnread(s));
  if (!target) return false;
  openSession(target.conversation_id);
  return true;
}

const sessionListEl = document.getElementById('session-list');
const emptyStateEl = document.getElementById('empty-state');
const threadViewEl = document.getElementById('thread-view');
const threadTitleEl = document.getElementById('thread-title');
const addToGroupBtn = document.getElementById('add-to-group-btn');
const stopTurnBtn = document.getElementById('stop-turn-btn');
const continueSessionBtn = document.getElementById('continue-session-btn');
const messagesEl = document.getElementById('messages');
const queuedBadgeEl = document.getElementById('queued-badge');
const composeForm = document.getElementById('compose-form');
const composeText = document.getElementById('compose-text');
const micBtn = document.getElementById('mic-btn');
const micStatusEl = document.getElementById('mic-status');
const newSessionBtn = document.getElementById('new-session-btn');
const searchInputEl = document.getElementById('search-input');
const searchResultsEl = document.getElementById('search-results');
const searchIncludeMessagesEl = document.getElementById('search-include-messages');
const sessionInfoBtn = document.getElementById('session-info-btn');
const sessionInfoPopover = document.getElementById('session-info-popover');
const sessionInfoIdEl = document.getElementById('session-info-id');
const sessionInfoWorkdirEl = document.getElementById('session-info-workdir');
const noteBtn = document.getElementById('note-btn');
const notePopover = document.getElementById('note-popover');
const noteTextarea = document.getElementById('note-textarea');
const noteSaveStatusEl = document.getElementById('note-save-status');
const bookmarksBtn = document.getElementById('bookmarks-btn');
const bookmarksPopover = document.getElementById('bookmarks-popover');
const bookmarksListEl = document.getElementById('bookmarks-list');
const printBtn = document.getElementById('print-btn');
const threadMetaEl = document.getElementById('thread-meta');
const threadOverflowBtn = document.getElementById('thread-overflow-btn');
const threadOverflowMenu = document.getElementById('thread-overflow-menu');
const sessionFiltersEl = document.getElementById('session-filters');

marked.setOptions({ gfm: true, breaks: true });

const emojiConverter = new EmojiConvertor();
emojiConverter.replace_mode = 'unified';
emojiConverter.allow_native = true;

function convertEmoji(text) {
  if (!text) return text;
  try {
    return emojiConverter.replace_colons(text);
  } catch (e) {
    return text;
  }
}

function sanitizeDanglingInlineCode(text) {
  // Each line of streamed status/step text (see copilot_session.py's
  // status-updater) is meant to render as its own row. If a line contains
  // an odd number of backtick characters - e.g. a tool-call argument that
  // itself happens to contain a stray backtick - marked's inline-code
  // lexer treats that lone backtick as an *unterminated* code span and
  // keeps scanning forward past the newline for a closing backtick,
  // swallowing one or more otherwise-unrelated following lines into a
  // single <code> block (they visually "merge" into one row). Neutralize
  // any line with an unbalanced backtick count by escaping its backticks,
  // so a bad line can never eat the lines after it. Lines inside a fenced
  // ``` code block are left untouched (backtick counting doesn't apply
  // there - the fence itself defines the boundaries).
  if (!text) return text;
  let inFence = false;
  return text
    .split('\n')
    .map((line) => {
      if (/^\s*```/.test(line)) {
        inFence = !inFence;
        return line;
      }
      if (inFence) return line;
      const backtickCount = (line.match(/`/g) || []).length;
      if (backtickCount % 2 !== 0) {
        return line.replace(/`/g, '\\`');
      }
      return line;
    })
    .join('\n');
}

function slackLinksToMarkdown(text) {
  if (!text) return text;
  let out = text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  // Slack's angle-bracket link syntax isn't limited to http(s) - it also
  // covers tel:, mailto:, and other URI schemes (e.g. <tel:+123|+123>),
  // so match any "scheme:" prefix rather than hardcoding https?.
  out = out.replace(/<([a-zA-Z][a-zA-Z0-9+.-]*:[^|>]+)\|([^>]+)>/g, (_, url, label) => `[${label}](${url})`);
  out = out.replace(/<([a-zA-Z][a-zA-Z0-9+.-]*:[^>]+)>/g, (_, url) => `[${url}](${url})`);
  out = out.replace(/(https?:\/\/[^\s<>|]+)\|([^\s<>|]+)/g, (_, url, label) => `[${label}](${url})`);
  out = out.replace(/\b(tel:\+?[0-9][0-9-]*)\|([+0-9][0-9-]*)\b/g, (_, url, label) => `[${label}](${url})`);
  return out;
}

function repairUnbalancedMarkdownLinks(text) {
  // Assistant-generated links whose URL embeds parentheses (e.g. Wiz URLs
  // with "#~(entity~(...))") sometimes miss the closing ")", most often in
  // table cells. marked's tokenizer then falls back to its autolinker and
  // emits literal "[label](" plus a link with the truncated raw URL.
  //
  // Scan for "](" and manually paren-depth-count the URL that follows,
  // starting from depth=1 for the link's own not-yet-closed opening paren.
  // If depth never returns to 0 before a "|" (table cell boundary), a
  // newline, or the end of the string, the link is malformed - close it
  // right there by appending the missing ")" characters, which reconstructs
  // exactly the well-formed, fully-balanced link marked already renders
  // correctly. Deliberately bounded to a single line/cell so one malformed
  // link can never swallow unrelated content (e.g. a following link in the
  // next table cell) past its own boundary.
  if (!text || text.indexOf('](') === -1) return text;
  let out = '';
  let i = 0;
  while (i < text.length) {
    const idx = text.indexOf('](', i);
    if (idx === -1) {
      out += text.slice(i);
      break;
    }
    out += text.slice(i, idx + 2);
    let j = idx + 2;
    let depth = 1;
    while (j < text.length && depth > 0) {
      const ch = text[j];
      if (ch === '(') {
        depth++;
      } else if (ch === ')') {
        depth--;
      } else if (ch === '\n' || ch === '|') {
        break;
      }
      if (depth > 0) j++;
    }
    if (depth > 0) {
      out += text.slice(idx + 2, j) + ')'.repeat(depth);
      i = j;
    } else {
      out += text.slice(idx + 2, j + 1);
      i = j + 1;
    }
  }
  return out;
}

// All markdown (agent/user/third-party text) is sanitized with vendored DOMPurify
// before reaching innerHTML; every sink goes through renderMarkdown().
if (window.DOMPurify) {
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A' && node.getAttribute('target') === '_blank') node.setAttribute('rel', 'noopener noreferrer');
  });
}

function renderMarkdown(text) {
  const raw = marked.parse(
    convertEmoji(repairUnbalancedMarkdownLinks(slackLinksToMarkdown(sanitizeDanglingInlineCode(text || ''))))
  );
  return window.DOMPurify ? DOMPurify.sanitize(raw, { ADD_ATTR: ['target'] }) : escapeHtml(raw);
}

// Syntax-highlighted code blocks with a copy button. marked.js
// already emits standard <pre><code class="language-xxx"> blocks for
// fenced code - this runs after that HTML is inserted into a bubble to
// (a) apply highlight.js coloring and (b) inject a floating 📋 button per
// block that copies the block's raw text (not the highlighted markup) to
// the clipboard.
function enhanceCodeBlocks(container) {
  if (typeof hljs === 'undefined') return;
  container.querySelectorAll('pre > code').forEach((codeEl) => {
    if (codeEl.parentElement.dataset.mermaidHandled) return;
    if (codeEl.parentElement.dataset.chartHandled) return;
    try {
      hljs.highlightElement(codeEl);
    } catch (e) {
      console.error('highlight.js failed on a code block', e);
    }
    const preEl = codeEl.parentElement;
    if (!preEl || preEl.querySelector('.code-copy-btn')) return;
    preEl.classList.add('code-block-wrapper');
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'code-copy-btn';
    copyBtn.title = 'Copy code';
    copyBtn.textContent = '📋';
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(codeEl.textContent);
        const original = copyBtn.textContent;
        copyBtn.textContent = '✅';
        setTimeout(() => {
          copyBtn.textContent = original;
        }, 1000);
      } catch (e) {
        console.error('copy code block failed', e);
      }
    });
    preEl.appendChild(copyBtn);
  });
}

// Auto-unfurl integration links into preview cards. Recognizes the
// URL shapes declared by the configured integrations (see furls.js) among a
// bubble's rendered <a> links, fetches a live summary for each, and shows a small floating card
// (with an app icon + status emoji) on hover - a lookup failure
// (unrecognized URL, 404, network) just leaves the plain link alone with
// no card, rather than showing an error.
// Recognised-link rules, URL templates, status emoji and card fields come from the
// integration registry projection (furls.js, GET /api/integrations/ui).
Furls.ready = Furls.load((u) => fetch(u));

async function enhanceLinkPreviews(container) {
  await Furls.ready;
  const links = Array.from(container.querySelectorAll('a')).filter((a) => {
    const href = a.getAttribute('href') || '';
    return Furls.isLink(href) || parseOwnSessionLink(href) !== null || parseOwnSettingsLink(href) !== null;
  });
  for (const a of links) {
    if (a.dataset.unfurlWired) continue;
    a.dataset.unfurlWired = '1';
    a.classList.add('unfurl-link');
    // Deliberately NOT fetching /api/unfurl here - see wireLazyUnfurl().
    // Fetching eagerly for every recognized link the moment a bubble
    // renders meant every SSE push / heartbeat / session switch fired a
    // burst of parallel API calls for links the user may never even look
    // at ("chasing" behavior). Instead, defer the fetch to first hover.
    wireLazyUnfurl(a);
  }
}

function renderUnfurlCard(data) {
  const card = document.createElement('div');
  card.className = `unfurl-card unfurl-${data.type}`;
  // Every card follows the same two-row layout: row 1 is the app icon +
  // short key/number + status emoji(s) (quick
  // glance, no need to read the title); row 2 is the actual
  // title/summary text, which can be long and wrap independently.
  if (data.type === 'own_settings') {
    const what = [data.tab ? `${data.tab} tab` : null, data.q ? `search “${data.q}”` : null].filter(Boolean).join(' · ') || 'Settings';
    card.innerHTML = `
      <div class="unfurl-row1">
        <span class="unfurl-icon" aria-hidden="true">⚙️</span>
        <a href="${escapeHtml(data.url)}">Settings</a>
        <span class="unfurl-status">${escapeHtml(data.tab || 'all')}</span>
      </div>
      <div class="unfurl-row2">${escapeHtml(what)}</div>
    `;
  } else if (data.type === 'own_session') {
    const statusEmoji = data.running ? '🔄' : '⚪';
    const statusLabel = data.running ? 'running' : 'idle';
    const activityPart = data.updated_at ? ` · updated ${escapeHtml(timeAgo(data.updated_at))}` : '';
    const shortId = (data.conversationId || '').slice(0, 8) || 'session';
    // Deliberately no target="_blank" here (unlike the integration cards
    // above) - this points at one of THIS bot's own sessions, so clicking
    // it should navigate in-app (same tab, no new-tab/window) exactly like
    // any other in-app deep link. See the click handling wired onto the
    // anchor in wireLazyUnfurl()/renderMessages() for the actual
    // navigation - this href is just a plain #open=/#msg= hash so the
    // link still "just works" if right-clicked/opened manually.
    // Row layout matches every other card type: row1 is icon + short
    // key + status (quick glance), row2 is the actual title (can be
    // long, wraps independently).
    card.innerHTML = `
      <div class="unfurl-row1">
        <span class="unfurl-icon" aria-hidden="true">🤖</span>
        <a href="${escapeHtml(data.url)}">Session ${escapeHtml(shortId)}</a>
        <span class="unfurl-status">${statusEmoji} ${statusLabel}${activityPart}</span>
      </div>
      <div class="unfurl-row2">${escapeHtml(data.title)}</div>
    `;
  } else {
    const html = Furls.cardHtml(data);
    if (!html) return null;
    card.innerHTML = html;
  }
  return card;
}

// Reveal an unfurl card as a floating tooltip that follows the mouse
// pointer while hovering its trigger link, and keep it in place (not
// following) once the mouse moves into the card itself, so you can click
// through to the linked ticket/MR without it chasing the cursor. A short
// hide delay absorbs the small gap between the link and the card while the
// mouse travels between them.
const UNFURL_HOVER_HIDE_DELAY_MS = 200;
const UNFURL_CARD_CURSOR_OFFSET_X = 16;
const UNFURL_CARD_CURSOR_OFFSET_Y = 20;

function positionUnfurlCardNearCursor(card, clientX, clientY) {
  const margin = 8;
  const cardWidth = card.offsetWidth || 320;
  const cardHeight = card.offsetHeight || 60;
  let left = clientX + UNFURL_CARD_CURSOR_OFFSET_X;
  let top = clientY + UNFURL_CARD_CURSOR_OFFSET_Y;
  if (left + cardWidth > window.innerWidth - margin) {
    left = clientX - cardWidth - UNFURL_CARD_CURSOR_OFFSET_X;
  }
  if (top + cardHeight > window.innerHeight - margin) {
    top = clientY - cardHeight - UNFURL_CARD_CURSOR_OFFSET_Y;
  }
  card.style.left = `${Math.max(margin, left)}px`;
  card.style.top = `${Math.max(margin, top)}px`;
}

function createUnfurlSpinnerCard() {
  const card = document.createElement('div');
  card.className = 'unfurl-card unfurl-card-spinner';
  card.innerHTML = '<span class="unfurl-spinner" aria-label="Loading preview…"></span>';
  document.body.appendChild(card);
  return card;
}

// Lazily fetches /api/unfurl only on the first hover of a recognized link
// (not eagerly for every link the moment a message bubble renders - see
// enhanceLinkPreviews()), showing a small spinner floating near the cursor
// while the request is in flight, then swapping it for the real card
// content once the response resolves. The result is cached on this
// closure (fetchState/card) so repeated hovers of the same link never
// refetch. If the lookup fails or the link type isn't recognized, the
// placeholder is removed and that link is treated as "known dead" for the
// rest of the page's lifetime (no card ever appears, no retry).
function wireLazyUnfurl(anchor) {
  const href = anchor.getAttribute('href');
  let card = null;
  let fetchState = 'idle'; // idle | loading | loaded | failed
  let hovering = false;
  let hideTimer = null;

  const cancelHide = () => {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  };
  const scheduleHide = () => {
    cancelHide();
    hideTimer = setTimeout(() => {
      if (card) card.classList.remove('unfurl-card-visible');
      hideTimer = null;
    }, UNFURL_HOVER_HIDE_DELAY_MS);
  };

  async function fetchAndRender(clientX, clientY) {
    fetchState = 'loading';
    try {
      const ownLink = parseOwnSessionLink(href);
      const settingsLink = parseOwnSettingsLink(href);
      let data;
      if (settingsLink) {
        data = { type: 'own_settings', url: href, tab: settingsLink.tab, q: settingsLink.q };
      } else if (ownLink) {
        // Resolved entirely client-side - the `sessions` array already
        // has everything needed (title, running state, last activity),
        // no need for an /api/unfurl round trip like the integration
        // cards below.
        const session = sessions.find((s) => s.conversation_id === ownLink.conversationId);
        if (!session) throw new Error('unknown session');
        data = {
          type: 'own_session',
          url: href,
          conversationId: ownLink.conversationId,
          title: markdownToPlainText(session.title) || '(untitled session)',
          running: !!session.running,
          updated_at: session.updated_at,
        };
      } else {
        const resp = await fetch(`/api/unfurl?url=${encodeURIComponent(href)}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        data = await resp.json();
        if (!data.type) throw new Error('unrecognized link');
      }
      const rendered = renderUnfurlCard(data);
      if (!rendered) throw new Error('no card for type');
      fetchState = 'loaded';
      if (card) {
        card.className = rendered.className;
        card.innerHTML = rendered.innerHTML;
        if (hovering) {
          card.classList.add('unfurl-card-visible');
          positionUnfurlCardNearCursor(card, clientX, clientY);
        }
      }
    } catch (e) {
      fetchState = 'failed';
      // No preview available for this link - drop the placeholder
      // entirely rather than leaving an empty/broken card behind.
      if (card) {
        card.remove();
        card = null;
      }
    }
  }

  anchor.addEventListener('mouseenter', (e) => {
    hovering = true;
    cancelHide();
    if (fetchState === 'failed') return; // already know there's nothing to show
    if (!card) {
      card = createUnfurlSpinnerCard();
      card.addEventListener('mouseenter', cancelHide);
      card.addEventListener('mouseleave', scheduleHide);
    }
    positionUnfurlCardNearCursor(card, e.clientX, e.clientY);
    card.classList.add('unfurl-card-visible');
    if (fetchState === 'idle') fetchAndRender(e.clientX, e.clientY);
  });
  anchor.addEventListener('mousemove', (e) => {
    if (card && card.classList.contains('unfurl-card-visible')) {
      positionUnfurlCardNearCursor(card, e.clientX, e.clientY);
    }
  });
  anchor.addEventListener('mouseleave', () => {
    hovering = false;
    scheduleHide();
  });
}


function markdownToPlainText(md) {
  if (!md) return '';
  const html = renderMarkdown(md);
  const div = document.createElement('div');
  div.innerHTML = html;
  return (div.textContent || '').trim();
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  // Only our own auth failure means "logged out"; a 401 from the /mcp catch-all mount
  // ({"error": "unauthorized"}, e.g. a route the running server doesn't know yet) must not.
  if (res.status === 401) {
    const body = await res.clone().json().catch(() => ({}));
    if (body.detail === 'Not authenticated') {
      window.location.href = '/login';
      throw new Error('unauthenticated');
    }
    throw new Error(body.error || 'HTTP 401 (unknown API route? restart the bot)');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function timeAgo(iso) {
  if (!iso) return '';
  const d = parseIso(iso);
  const diffMs = Date.now() - d.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function fullTimestamp(iso) {
  if (!iso) return '';
  const d = parseIso(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function currentSession() {
  return sessions.find((x) => x.conversation_id === currentConversationId) || null;
}

function updateThreadHeader() {
  if (!currentConversationId) {
    continueSessionBtn.style.display = 'none';
    closeSessionInfoPopover();
    threadMetaEl.textContent = '';
    return;
  }
  const session = currentSession();
  const fullTitle = markdownToPlainText(session ? session.title : currentConversationId) || '(new session)';
  threadTitleEl.textContent = fullTitle;
  threadTitleEl.title = fullTitle + (session ? ' (double-click to rename)' : '');
  // 'flex' (not 'inline-block') so it matches the other overflow-menu
  // item rows (icon + label laid out via display:flex in style.css) -
  // an inline style always wins over the stylesheet's own display value.
  continueSessionBtn.style.display = 'flex';
  updateThreadMeta(session);
  renderGroupBanner(session);
  refreshThreadWatchesHealth();
  if (sessionInfoPopover.style.display !== 'none') {
    renderSessionInfoPopover();
  }
}

// Turn count + idle time readout ("N turns · idle Xm") in the thread header,
// mirroring the sidebar's per-session line (see renderSessionList()).
function updateThreadMeta(session) {
  if (!session || typeof session.turn_count !== 'number') {
    threadMetaEl.textContent = '';
    threadMetaEl.title = '';
    return;
  }
  const turnLabel = `${session.turn_count} turn${session.turn_count === 1 ? '' : 's'}`;
  const idleLabel = session.updated_at ? `idle ${timeAgo(session.updated_at)}` : '';
  threadMetaEl.textContent = idleLabel ? `${turnLabel} · ${idleLabel}` : turnLabel;
  threadMetaEl.title = session.updated_at ? `Last activity: ${fullTimestamp(session.updated_at)}` : '';
  if (window.usageWidget) window.usageWidget.decorateThreadMeta(threadMetaEl, session);
}

// Session info popover (ℹ️ in the thread header): the session's Copilot
// session_id (from sessions[]) and the bot-wide working directory (botWorkdir,
// from GET /api/health). Each row has a 📋 button that copies its value.
function renderSessionInfoPopover() {
  const session = currentSession();
  sessionInfoIdEl.textContent = (session && session.session_id) || '(not yet started)';
  sessionInfoWorkdirEl.textContent = botWorkdir || '(unknown)';
}

function openSessionInfoPopover() {
  renderSessionInfoPopover();
  sessionInfoPopover.style.display = 'block';
}

function closeSessionInfoPopover() {
  sessionInfoPopover.style.display = 'none';
}

sessionInfoBtn.addEventListener('click', () => {
  if (sessionInfoPopover.style.display === 'none') {
    openSessionInfoPopover();
  } else {
    closeSessionInfoPopover();
  }
});

sessionInfoPopover.addEventListener('click', async (e) => {
  const btn = e.target.closest('.session-info-copy-btn');
  if (!btn) return;
  const targetEl = document.getElementById(btn.dataset.copyTarget);
  if (!targetEl) return;
  try {
    await navigator.clipboard.writeText(targetEl.textContent);
    const original = btn.textContent;
    btn.textContent = '✅';
    setTimeout(() => {
      btn.textContent = original;
    }, 1000);
  } catch (err) {
    console.error('copy to clipboard failed', err);
  }
});

document.addEventListener('click', (e) => {
  if (sessionInfoPopover.style.display === 'none') return;
  if (sessionInfoPopover.contains(e.target) || e.target === sessionInfoBtn) return;
  closeSessionInfoPopover();
});

// Private per-session note-to-self (📝 in the thread header), stored via
// message_store.get_note()/set_note(). Never sent to Copilot or shown in the
// transcript. Autosaves on a short debounce and on blur.
let noteSaveDebounceTimer = null;
let noteLoadedForConversationId = null;
let noteEditedSinceLoadRequested = false;

async function loadNoteForCurrentSession() {
  if (!currentConversationId) {
    noteTextarea.value = '';
    noteLoadedForConversationId = null;
    return;
  }
  const requestedForConversationId = currentConversationId;
  noteEditedSinceLoadRequested = false;
  try {
    const resp = await fetch(`/api/sessions/${requestedForConversationId}/note`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (requestedForConversationId !== currentConversationId) return;
    // Don't let a slow load clobber text typed while the fetch was in flight.
    if (!noteEditedSinceLoadRequested) {
      noteTextarea.value = data.note || '';
    }
    noteLoadedForConversationId = requestedForConversationId;
  } catch (e) {
    console.error('failed to load session note', e);
  }
}

async function saveNoteForCurrentSession() {
  if (!currentConversationId || noteLoadedForConversationId !== currentConversationId) return;
  noteSaveStatusEl.textContent = 'Saving…';
  try {
    const resp = await fetch(`/api/sessions/${currentConversationId}/note`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: noteTextarea.value }),
    });
    if (!resp.ok) throw new Error(`save failed: ${resp.status}`);
    noteSaveStatusEl.textContent = 'Saved';
    setTimeout(() => {
      if (noteSaveStatusEl.textContent === 'Saved') noteSaveStatusEl.textContent = '';
    }, 1500);
  } catch (e) {
    console.error('failed to save session note', e);
    noteSaveStatusEl.textContent = 'Save failed';
  }
}

function openNotePopover() {
  notePopover.style.display = 'block';
  loadNoteForCurrentSession();
}

function closeNotePopover() {
  notePopover.style.display = 'none';
}

noteBtn.addEventListener('click', () => {
  if (notePopover.style.display === 'none') {
    openNotePopover();
  } else {
    closeNotePopover();
  }
});

noteTextarea.addEventListener('input', () => {
  noteEditedSinceLoadRequested = true;
  noteSaveStatusEl.textContent = '';
  clearTimeout(noteSaveDebounceTimer);
  noteSaveDebounceTimer = setTimeout(saveNoteForCurrentSession, 800);
});

noteTextarea.addEventListener('blur', () => {
  clearTimeout(noteSaveDebounceTimer);
  saveNoteForCurrentSession();
});

document.addEventListener('click', (e) => {
  if (notePopover.style.display === 'none') return;
  if (notePopover.contains(e.target) || e.target === noteBtn) return;
  closeNotePopover();
});

// Message bookmarks. Each message's star toggle lives in renderMessages();
// this covers the header ⭐ popover listing the session's bookmarks, each
// jumping to its message via pendingDeepLinkMessageId.
async function toggleMessageBookmark(messageId, bookmarked) {
  if (!currentConversationId) return;
  try {
    const resp = await fetch(`/api/sessions/${currentConversationId}/messages/${messageId}/bookmark`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bookmarked }),
    });
    if (!resp.ok) throw new Error(`bookmark toggle failed: ${resp.status}`);
    const turn = lastTurns.find((t) => t.id === messageId);
    if (turn) turn.bookmarked = bookmarked;
    renderMessages(lastTurns, lastRunning);
    if (bookmarksPopover.style.display !== 'none') renderBookmarksList();
  } catch (e) {
    console.error('failed to toggle message bookmark', e);
  }
}

// Web UI equivalent of clicking one of Slack's voice-transcript buttons
// (Auto-send/Preview/Send to agent) - see handlers.py's
// _arm_transcript_mode()/web_transcript_action() for the shared backend
// core this calls into. Rendered inline on a message by renderMessages()
// below whenever that message's `actions` array (from message_store's
// `actions` column) is non-empty - real per-message button parity with
// Slack, not a single global auto-send setting.
async function triggerTranscriptAction(messageId, actionId) {
  if (!currentConversationId) return;
  try {
    const resp = await fetch(
      `/api/sessions/${currentConversationId}/messages/${messageId}/transcript-action`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: actionId }),
      },
    );
    if (!resp.ok) throw new Error(`transcript action failed: ${resp.status}`);
    // The backend update (adapter.update()) already persists the new
    // content/actions to message_store and fires an SSE change event -
    // the next pollSession()/SSE-triggered renderMessages() picks up the
    // new state. No optimistic local mutation needed here since (unlike
    // the bookmark toggle) the resulting text itself also changes.
    return true;
  } catch (e) {
    console.error('failed to trigger transcript action', e);
    return false;
  }
}

function renderBookmarksList() {
  bookmarksListEl.innerHTML = '';
  const bookmarked = lastTurns.filter((t) => t.bookmarked);
  if (bookmarked.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'bookmarks-empty';
    empty.textContent = 'No bookmarked messages yet — click ☆ on a message to star it.';
    bookmarksListEl.appendChild(empty);
    return;
  }
  for (const t of bookmarked) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'bookmarks-item';
    const preview = (t.text || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    item.textContent = preview || '(empty message)';
    item.addEventListener('click', () => {
      closeBookmarksPopover();
      pendingDeepLinkMessageId = t.id;
      renderMessages(lastTurns, lastRunning);
    });
    bookmarksListEl.appendChild(item);
  }
}

function openBookmarksPopover() {
  bookmarksPopover.style.display = 'block';
  renderBookmarksList();
}

function closeBookmarksPopover() {
  bookmarksPopover.style.display = 'none';
}

bookmarksBtn.addEventListener('click', () => {
  if (bookmarksPopover.style.display === 'none') {
    openBookmarksPopover();
  } else {
    closeBookmarksPopover();
  }
});

document.addEventListener('click', (e) => {
  if (bookmarksPopover.style.display === 'none') return;
  if (bookmarksPopover.contains(e.target) || e.target === bookmarksBtn) return;
  closeBookmarksPopover();
});

// Thread-header overflow menu ("⋯" button, row 2 of .thread-header-toolbar).
// Holds the lower-frequency actions (print, continue-in-new-session, add to
// group) so the toolbar row only shows the
// higher-frequency chips (Info/Note/Bookmarks/Watches) inline. Same
// open/toggle/close-on-outside-click pattern as the other header popovers
// (session-info/note/bookmarks) above.
function openThreadOverflowMenu() {
  threadOverflowMenu.style.display = 'block';
  threadOverflowBtn.setAttribute('aria-expanded', 'true');
}

function closeThreadOverflowMenu() {
  threadOverflowMenu.style.display = 'none';
  threadOverflowBtn.setAttribute('aria-expanded', 'false');
}

threadOverflowBtn.addEventListener('click', () => {
  if (threadOverflowMenu.style.display === 'none') {
    openThreadOverflowMenu();
  } else {
    closeThreadOverflowMenu();
  }
});

document.addEventListener('click', (e) => {
  if (threadOverflowMenu.style.display === 'none') return;
  if (threadOverflowMenu.contains(e.target) || e.target === threadOverflowBtn) return;
  closeThreadOverflowMenu();
});

// Same group picker as the sidebar right-click menu (groups.js), anchored
// under the overflow button.
addToGroupBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  closeThreadOverflowMenu();
  const s = currentSession();
  if (!s) return;
  const r = threadOverflowBtn.getBoundingClientRect();
  openSessionContextMenu(r.right - 240, r.bottom + 4, s);
});

// Print-friendly transcript. window.print() triggers the
// @media print rules in style.css (hide sidebar/health-strip/compose/
// buttons, switch to a plain light layout for paper/PDF output). We also
// set document.title for the duration of the print dialog so "Save as
// PDF" suggests a sensible filename derived from the session title,
// restoring the original tab title immediately after (both via the
// 'afterprint' event and a fallback timeout, since 'afterprint' support
// varies slightly across browsers/print-to-PDF flows).
printBtn.addEventListener('click', () => {
  closeThreadOverflowMenu();
  window.print();
});

(function setupPrintTitle() {
  let originalTitle = document.title;
  window.addEventListener('beforeprint', () => {
    originalTitle = document.title;
    const label = (threadTitleEl.textContent || 'session').trim() || 'session';
    document.title = `DOP Agent transcript - ${label}`;
  });
  window.addEventListener('afterprint', () => {
    document.title = originalTitle;
  });
})();


threadTitleEl.addEventListener('dblclick', () => {
  const session = currentSession();
  if (!session) return;
  startRenameThreadHeader(session);
});

function startRenameThreadHeader(session) {
  const currentText = markdownToPlainText(session.title);
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'thread-title-input';
  input.value = currentText;
  threadTitleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (save && newTitle !== currentText) {
      try {
        await api(`/api/sessions/${encodeURIComponent(session.conversation_id)}/title`, {
          method: 'PUT',
          body: JSON.stringify({ title: newTitle || null }),
        });
      } catch (e) {
        console.error('rename failed', e);
      }
    }
    input.replaceWith(threadTitleEl);
    await refreshSessionList();
    updateThreadHeader();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

// Active sidebar quick-filter (pinned/running/unread/archived). 'all' is the
// default (archived sessions hidden unless currently open); the other
// values narrow the list to that category.
const SESSION_FILTER_STORAGE_KEY = 'dop_agent_session_filter_v1';
let activeSessionFilter = (() => {
  try { return localStorage.getItem(SESSION_FILTER_STORAGE_KEY) || 'all'; } catch (_) { return 'all'; }
})();

function recentHours() {
  const n = Number(settings.recentHours);
  return n > 0 ? n : 8;
}

// Re-render once a minute while Recent is active, only if the matching set changed, so sessions age out.
// renderSessionList records what it actually drew (renderedSessionKey), so the tick compares against the screen, not its own last run.
let renderedSessionKey = '';
function recentTick() {
  if (activeSessionFilter !== 'recent') return;
  const key = sessions.filter(sessionMatchesFilter).map(s => s.conversation_id).join(',');
  if (key !== renderedSessionKey) renderSessionList();
}
setInterval(recentTick, 60000);


function sessionMatchesFilter(s) {
  if (!sessionMatchesGroupFilter(s)) return false;
  switch (activeSessionFilter) {
    case 'pinned':
      return !!s.pinned;
    case 'running':
      return !!s.running;
    case 'recent': {
      if (s.conversation_id === currentConversationId) return !s.archived;
      return !s.archived && s.updated_at && (Date.now() - new Date(s.updated_at).getTime()) <= recentHours() * 3600 * 1000;
    }
    case 'unread':
      // Keep the session you just opened (which stops being unread) until you switch to another one.
      return isSessionUnread(s) || s.conversation_id === currentConversationId;
    case 'archived':
      return !!s.archived;
    case 'all':
    default:
      // Default view hides archived sessions (they're "put away"), except
      // for whichever one you currently have open, so archiving your
      // current session doesn't yank it out from under you mid-conversation.
      return !s.archived || s.conversation_id === currentConversationId;
  }
}

// Tab indicator: while any session is running, the favicon logo pulses
// (blades grow from a dot to full size and back) and the tab title gets a "⏳" prefix.
const TabIndicator = (() => {
  const link = document.querySelector("link[rel='icon']");
  const baseHref = link ? link.getAttribute('href') : null;
  const baseTitle = document.title;
  let img = null, timer = null;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 64;
  function draw() {
    const c = canvas.getContext('2d');
    c.clearRect(0, 0, 64, 64);
    if (img && img.complete && img.naturalWidth) {
      // The logo's blades grow out of a dot to full size and shrink back
      // (eased, 2.8s cycle), turning slightly as they grow.
      const t = (performance.now() % 2800) / 2800;
      const k = 0.5 - 0.5 * Math.cos(t * Math.PI * 2);
      const scale = 0.12 + 0.88 * k;
      c.save();
      c.translate(32, 32);
      c.rotate((1 - k) * -Math.PI / 2);
      c.scale(scale, scale);
      c.drawImage(img, -32, -32, 64, 64);
      c.restore();
    }
    link.type = 'image/png';
    link.href = canvas.toDataURL('image/png');
  }
  function set(running) {
    if (!link) return;
    if (running && !timer) {
      img = new Image();
      img.src = baseHref;
      timer = setInterval(draw, 80);
      document.title = '⏳ ' + baseTitle;
    } else if (!running && timer) {
      clearInterval(timer); timer = null;
      link.href = baseHref;
      document.title = baseTitle;
    }
  }
  return { set };
})();
window.TabIndicator = TabIndicator;

function renderSessionList() {
  TabIndicator.set(sessions.some((x) => x.running && !x.archived));
  sessionListEl.innerHTML = '';
  const current = sessions.find((x) => x.conversation_id === currentConversationId);
  if (current) {
    // Keep last-read fresh for the session you're actively viewing, so
    // switching away afterwards doesn't immediately show it as unread.
    markSessionRead(current.conversation_id, current.updated_at);
  }
  const visibleSessions = sessions.filter(sessionMatchesFilter);
  renderedSessionKey = visibleSessions.map(s => s.conversation_id).join(',');
  renderGroupFilters();
  for (const s of orderSessionsForDisplay(visibleSessions)) {
    if (s.__groupHeader) { sessionListEl.appendChild(buildGroupHeader(s)); continue; }
    const item = document.createElement('div');
    item.className = 'session-item'
      + (s.conversation_id === currentConversationId ? ' active' : '')
      + (s.pinned ? ' pinned' : '')
      + (s.archived ? ' archived' : '')
      + (isLongSession(s) ? ' long-session' : '')
      + (s.errored ? ' errored-session' : '');
    item.dataset.conversationId = s.conversation_id;
    item.draggable = true;
    applyGroupDecoration(item, s);
    const title = escapeHtml(markdownToPlainText(s.title));
    const preview = escapeHtml(markdownToPlainText(s.preview));
    const spinner = s.running ? '<span class="session-running-spinner" title="Bot is working on this session right now"></span>' : '';
    const errored = s.errored ? '<span class="session-error-flag" title="Last turn failed or timed out">⚠️</span>' : '';
    const unread = isSessionUnread(s) ? '<span class="session-unread-dot" title="New activity since you last viewed this session"></span>' : '';
    const turnCountHtml = isLongSession(s)
      ? `<span class="session-turn-count-long" title="This session has more than ${LONG_SESSION_TURN_THRESHOLD} turns and may load/scroll slower than usual">${s.turn_count} turns</span>`
      : `${s.turn_count} turns`;
    item.innerHTML = `
      <div class="session-item-top">
        ${unread}
        <div class="session-title" title="${title}${s.custom_title ? '' : ' (double-click to rename)'}">${title}</div>
        ${spinner}
        ${errored}
        <button type="button" class="session-archive-btn" title="${s.archived ? 'Unarchive' : 'Archive'}">${s.archived ? '📤' : '🗄️'}</button>
        <button type="button" class="session-pin-btn" title="${s.pinned ? 'Unpin' : 'Pin to top'}">${s.pinned ? '📌' : '📍'}</button>
      </div>
      <div class="session-preview">${preview}</div>
      <div class="session-meta">${timeAgo(s.updated_at)} · ${turnCountHtml}${window.usageWidget ? window.usageWidget.aiuHtml(s) : ''}</div>
    `;
    item.addEventListener('click', (e) => {
      if (e.target.closest('.session-pin-btn') || e.target.closest('.session-archive-btn') || e.target.closest('.session-title-input')) return;
      openSession(s.conversation_id);
    });
    item.querySelector('.session-pin-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      await togglePin(s.conversation_id, !s.pinned);
    });
    item.querySelector('.session-archive-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      await toggleArchive(s.conversation_id, !s.archived);
    });
    item.querySelector('.session-title').addEventListener('dblclick', (e) => {
      e.stopPropagation();
      startRenameSession(item, s);
    });
    item.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      openSessionContextMenu(e.clientX, e.clientY, s);
    });
    item.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', s.conversation_id);
      e.dataTransfer.effectAllowed = 'move';
      item.classList.add('dragging');
    });
    item.addEventListener('dragend', () => item.classList.remove('dragging'));
    if (s.pinned) {
      item.addEventListener('dragover', (e) => {
        if (!item.classList.contains('pinned')) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        item.classList.add('drag-over');
      });
      item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
      item.addEventListener('drop', async (e) => {
        e.preventDefault();
        item.classList.remove('drag-over');
        const draggedId = e.dataTransfer.getData('text/plain');
        if (!draggedId || draggedId === s.conversation_id) return;
        await reorderPinned(draggedId, s.conversation_id);
      });
    }
    sessionListEl.appendChild(item);
  }
  if (visibleSessions.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'session-filter-empty';
    empty.textContent = activeSessionFilter === 'all' ? 'No sessions yet.'
      : activeSessionFilter === 'recent' ? `No sessions with activity in the last ${recentHours()}h`
      : `No ${activeSessionFilter} sessions.`;
    sessionListEl.appendChild(empty);
  }
}

// Keyboard shortcuts (j/k session navigation, / to focus
// search, Cmd/Ctrl+Enter to send). Shortcuts are ignored while focus is
// inside any editable field (input/textarea/contenteditable) so normal
// typing - including using the letters j/k or the / character in a
// message - is never hijacked; the compose textarea's own Enter handling
// stays the exception (handled separately below).
function isEditableTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable;
}

function visibleSessionIds() {
  return Array.from(sessionListEl.querySelectorAll('.session-item[data-conversation-id]'))
    .map((el) => el.dataset.conversationId);
}

function navigateSessionList(direction) {
  const ids = visibleSessionIds();
  if (ids.length === 0) return;
  const currentIdx = ids.indexOf(currentConversationId);
  let nextIdx;
  if (currentIdx === -1) {
    nextIdx = direction > 0 ? 0 : ids.length - 1;
  } else {
    nextIdx = Math.min(ids.length - 1, Math.max(0, currentIdx + direction));
  }
  openSession(ids[nextIdx]);
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && shortcutsOverlay.style.display !== 'none') {
    e.preventDefault();
    closeShortcutsOverlay();
    return;
  }
  if (e.key === 'Escape' && settingsOverlay.style.display !== 'none') {
    e.preventDefault();
    if (window.DopSettings && window.DopSettings.hasInnerDialog()) return; // settings.js closes it
    closeSettingsOverlay();
    return;
  }
  if (isEditableTarget(e.target)) return;
  if (window.Inbox && window.Inbox.active() && (e.key === 'j' || e.key === 'k')) return;
  if (e.key === 'j') {
    e.preventDefault();
    navigateSessionList(1);
  } else if (e.key === 'k') {
    e.preventDefault();
    navigateSessionList(-1);
  } else if (e.key === '/') {
    e.preventDefault();
    searchInputEl.focus();
    searchInputEl.select();
  } else if (e.key === '?') {
    e.preventDefault();
    openShortcutsOverlay();
  }
});


async function reorderPinned(draggedId, targetId) {
  const pinnedIds = sessions.filter((x) => x.pinned).map((x) => x.conversation_id);
  const fromIdx = pinnedIds.indexOf(draggedId);
  const toIdx = pinnedIds.indexOf(targetId);
  if (fromIdx === -1 || toIdx === -1) return;
  pinnedIds.splice(fromIdx, 1);
  pinnedIds.splice(toIdx, 0, draggedId);
  try {
    await api('/api/pins/order', {
      method: 'PUT',
      body: JSON.stringify({ conversation_ids: pinnedIds }),
    });
  } catch (e) {
    console.error('reorder pins failed', e);
  }
  await refreshSessionList();
}

function startRenameSession(item, s) {
  const titleEl = item.querySelector('.session-title');
  const currentText = markdownToPlainText(s.title);
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-title-input';
  input.value = currentText;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (save && newTitle !== currentText) {
      try {
        await api(`/api/sessions/${encodeURIComponent(s.conversation_id)}/title`, {
          method: 'PUT',
          body: JSON.stringify({ title: newTitle || null }),
        });
      } catch (e) {
        console.error('rename failed', e);
      }
    }
    await refreshSessionList();
    updateThreadHeader();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function refreshSessionList() {
  try {
    const data = await api('/api/sessions');
    sessions = data.sessions;
    groups = data.groups || [];
    renderSessionList();
    updateThreadHeader();
  } catch (e) {
    console.error('session list refresh failed', e);
  }
}

async function togglePin(conversationId, pinned) {
  try {
    await api(`/api/sessions/${encodeURIComponent(conversationId)}/pin`, {
      method: 'PUT',
      body: JSON.stringify({ pinned }),
    });
  } catch (e) {
    console.error('setting pin failed', e);
  }
}

async function toggleArchive(conversationId, archived) {
  try {
    await api(`/api/sessions/${encodeURIComponent(conversationId)}/archive`, {
      method: 'PUT',
      body: JSON.stringify({ archived }),
    });
  } catch (e) {
    console.error('setting archive failed', e);
  }
  // Archiving/unarchiving the currently-open session doesn't close it -
  // it just changes whether it shows in the default sidebar view.
  await refreshSessionList();
}

const AVATAR_GLYPH = { user: '🙂', assistant: '🤖', system: '⚙️', status: '🤖' };

// Prefixes handlers.py uses for a failed/timed-out final response (see
// handle_dm/handle_mention's convo.send() except blocks) - used to detect
// "last turn errored" for the retry button. Must match those literal strings.
const ERROR_RESPONSE_PREFIXES = [':warning: DOP Agent request timed out', ':warning: Copilot request timed out', ':x: Error:'];

function isErrorResponseText(text) {
  return ERROR_RESPONSE_PREFIXES.some((p) => (text || '').startsWith(p));
}

// Distance (px) from the bottom of the transcript within which we still
// consider the user "at the bottom" and therefore keep auto-scrolling as
// new content streams in. Wider than 0 so small sub-pixel rounding/layout
// jitter doesn't cause a stuck-at-bottom user to suddenly stop tracking.
const SCROLL_STICK_TO_BOTTOM_PX = 48;

// ---------------------------------------------------------------------------
// Watcher-noise views. Transcript turns arrive pre-classified from the server
// (message_kinds.classify_turns: kind / fire_type / fire_ok / reply_class /
// summary). Three views, persisted in localStorage:
//   raw   ("Today")        - every message exactly as stored
//   smart                  - routine watcher events folded into chips/groups,
//                            reports and failures stay as normal bubbles
//   msgs  ("Messages only")- only you, the agent's real messages, errors
// Smart/Messages views also get an inline "While you were away" digest at the
// first event after the chosen window. Visual rule ("calm & quiet"): grays
// only; blue only for links; red only for failure.
// ---------------------------------------------------------------------------
const VIEW_MODE_KEY = 'dopAgentViewMode';
let viewMode = ['raw', 'smart', 'msgs'].includes(localStorage.getItem(VIEW_MODE_KEY))
  ? localStorage.getItem(VIEW_MODE_KEY) : 'smart';
const digestSince = new Map();      // conversationId -> last_read_at (ISO) captured when the session was opened
const digestAutoScrolled = new Set();
const digestDismissed = new Set();
let digestWindow = 'visit';         // visit | morning | evening

const FIRE_TYPE_LABEL = {
  ticket: 'ticket', teams: 'Teams', time: 'timer', session_turn: 'session turn', other: 'watch',
};

function turnMs(t) {
  const d = parseIso(t.timestamp || t.updated_at);
  return d ? d.getTime() : 0;
}

function digestCutoffMs(convId, turns) {
  const now = new Date();
  if (digestWindow === 'morning') return new Date(now.getFullYear(), now.getMonth(), now.getDate(), 6).getTime();
  if (digestWindow === 'evening') return new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 18).getTime();
  const iso = digestSince.get(convId);
  const ms = iso ? parseIso(iso).getTime() : NaN;
  // Server last_read_at is ~now for a session that is being watched live, so
  // a cutoff within the last 2 minutes (or none) carries no information:
  // fall back to this morning so the digest is still reachable/selectable.
  if (!Number.isFinite(ms) || Date.now() - ms < 120000) {
    return new Date(now.getFullYear(), now.getMonth(), now.getDate(), 6).getTime();
  }
  return ms;
}

function isRoutineFire(fire, reply) {
  if (fire.fire_ok === false) return false;
  if (reply && reply.reply_class === 'report') return false;
  return true;
}

function buildDisplayItems(turns, mode) {
  if (mode === 'raw') return turns.map((t) => ({ type: 'turn', turn: t }));

  // Pair every fire with the reply that answers it.
  const replyByFire = new Map();      // fire id -> LAST reply (the one that matters)
  const allRepliesByFire = new Map(); // fire id -> every reply (a thin one + a re-prompted report)
  for (const t of turns) {
    if (t.kind === 'watch_reply' && t.reply_to != null) {
      replyByFire.set(t.reply_to, t);
      if (!allRepliesByFire.has(t.reply_to)) allRepliesByFire.set(t.reply_to, []);
      allRepliesByFire.get(t.reply_to).push(t);
    }
  }
  const hiddenReplyIds = new Set(); // replies folded under a chip
  const events = [];                // routine events in order, for digest counts
  const out = [];
  let run = [];

  const flushRun = () => {
    if (!run.length) return;
    out.push(run.length === 1 && run[0].kind !== 'registered'
      ? { type: 'chip', ev: run[0] }
      : { type: 'group', evs: run });
    run = [];
  };

  for (const t of turns) {
    if (t.kind === 'watch_registered') {
      const ev = { kind: 'registered', turn: t };
      events.push(ev);
      run.push(ev);
    } else if (t.kind === 'watch_fired') {
      const reply = replyByFire.get(t.id) || null;
      const routine = isRoutineFire(t, reply);
      const ev = { kind: 'fire', turn: t, reply, routine };
      events.push(ev);
      if (routine) {
        (allRepliesByFire.get(t.id) || []).forEach((r) => hiddenReplyIds.add(r.id));
        run.push(ev);
      } else {
        flushRun();
        out.push({ type: 'chip', ev });
        // non-routine: replies fall through to the watch_reply branch below and render as bubbles
      }
    } else if (t.kind === 'interrupt') {
      // the user pressed Stop: keep the original bubble visible in Smart view
      if (mode === 'smart') { flushRun(); out.push({ type: 'turn', turn: t }); }
    } else if (t.kind === 'watch_reply') {
      if (hiddenReplyIds.has(t.id)) continue;
      flushRun();
      out.push({ type: 'turn', turn: t });
    } else {
      if (mode === 'msgs' && !['user', 'agent', 'error', 'status'].includes(t.kind)) continue;
      flushRun();
      out.push({ type: 'turn', turn: t });
    }
  }
  flushRun();

  // Digest: inline before the first item newer than the cutoff.
  const convId = currentConversationId;
  const cutoff = digestCutoffMs(convId, turns);
  if (cutoff && !digestDismissed.has(convId)) {
    const idx = out.findIndex((i) => {
      const t = i.type === 'turn' ? i.turn
        : i.type === 'chip' ? i.ev.turn
          : i.evs[0].turn;
      return turnMs(t) > cutoff;
    });
    const after = turns.filter((t) => turnMs(t) > cutoff);
    const hasEvents = after.some((t) => t.kind === 'watch_fired' || t.kind === 'watch_reply');
    if (idx > 0 && hasEvents) {
      out.splice(idx, 0, { type: 'digest', cutoff, after });
    }
  }
  return mode === 'msgs' ? out.filter((i) => i.type === 'turn' || i.type === 'digest') : out;
}

function evTime(t) {
  const d = parseIso(t.timestamp || t.updated_at);
  return d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
}

function evChipLabel(ev) {
  if (ev.kind === 'registered') return { type: 'watch', text: `registered: ${ev.turn.fire_label || ''}` };
  if (ev.kind === 'interrupt') return { type: '', text: '↯ interrupted a running turn' };
  const t = ev.turn;
  const mark = t.fire_ok === true ? '✓ ' : (t.fire_ok === false ? '✗ ' : '');
  return { type: FIRE_TYPE_LABEL[t.fire_type] || 'watch', text: `${mark}${t.fire_label || ''}` };
}

function makeChip(ev) {
  const label = evChipLabel(ev);
  const chip = document.createElement('div');
  chip.className = `watch-chip${ev.turn.fire_ok === false ? ' watch-chip-fail' : ''}`;
  if (label.type) {
    const ty = document.createElement('span');
    ty.className = 'watch-chip-type';
    ty.textContent = label.type;
    chip.appendChild(ty);
  }
  const tx = document.createElement('span');
  tx.textContent = label.text;
  chip.appendChild(tx);
  const tm = document.createElement('span');
  tm.className = 'watch-chip-time';
  tm.textContent = evTime(ev.turn);
  chip.appendChild(tm);
  return chip;
}

// Chip click -> the full original message(s), same rendering as Today view:
// the raw watcher notice as a dashed system bubble, plus the agent's full
// reply (if any) as a normal bubble.
function makeFullPreview(ev) {
  const box = document.createElement('div');
  box.className = 'watch-preview';
  const add = (cls, text) => {
    const b = document.createElement('div');
    b.className = `bubble ${cls}`;
    b.innerHTML = renderMarkdown(text || '');
    b.querySelectorAll('a').forEach((a) => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    if (cls === 'assistant' && window.MermaidRender) window.MermaidRender.apply(b, `preview-${ev.reply ? ev.reply.id : ''}`, false);
    if (cls === 'assistant' && window.ChartRender) window.ChartRender.apply(b, `preview-${ev.reply ? ev.reply.id : ''}`, false);
    enhanceCodeBlocks(b);
    box.appendChild(b);
  };
  add('system', ev.turn.text);
  if (ev.reply) add('assistant', ev.reply.text);
  return box;
}

const openChipPreviews = new Set();
function makeClickableChip(ev) {
  const wrap = document.createElement('div');
  wrap.className = 'watch-chip-wrap';
  const chip = makeChip(ev);
  chip.classList.add('watch-chip-click');
  chip.title = 'Click to show / hide the full message';
  let preview = null;
  const key = ev.turn.id;
  const open = () => { preview = makeFullPreview(ev); chip.classList.add('watch-chip-open'); wrap.appendChild(preview); };
  chip.addEventListener('click', () => {
    if (preview) { preview.remove(); preview = null; chip.classList.remove('watch-chip-open'); openChipPreviews.delete(key); return; }
    openChipPreviews.add(key);
    open();
  });
  wrap.appendChild(chip);
  // Re-renders (SSE updates) rebuild the DOM - keep what the user expanded.
  if (openChipPreviews.has(key)) open();
  return wrap;
}

function makeEventDetail(ev) {
  const wrap = document.createElement('div');
  wrap.className = 'watch-group-item';
  wrap.appendChild(makeClickableChip(ev));
  if (ev.reply) {
    const r = document.createElement('div');
    r.className = 'watch-group-reply';
    r.textContent = ev.reply.reply_class === 'ack' ? `ACK ${ev.reply.summary || ''}`.trim() : (ev.reply.summary || '');
    wrap.appendChild(r);
  }
  return wrap;
}

function renderSpecialItem(item) {
  const row = document.createElement('div');
  row.className = 'watch-indent';
  if (item.type === 'chip') {
    const ev = item.ev;
    row.appendChild(makeClickableChip(ev));
    return row;
  }
  if (item.type === 'group') {
    const fires = item.evs.filter((e) => e.kind === 'fire').length;
    const regs = item.evs.filter((e) => e.kind === 'registered').length;
    const bits = [];
    if (fires) bits.push(`${fires} routine event${fires === 1 ? '' : 's'}`);
    if (regs) bits.push(`${regs} watcher${regs === 1 ? '' : 's'} registered`);
    const details = document.createElement('details');
    details.className = 'watch-group';
    const sum = document.createElement('summary');
    sum.className = 'watch-chip';
    sum.textContent = `${bits.join(' · ') || `${item.evs.length} events`} · ${evTime(item.evs[0].turn)}–${evTime(item.evs[item.evs.length - 1].turn)}`;
    details.appendChild(sum);
    const inner = document.createElement('div');
    inner.className = 'watch-group-inner';
    for (const ev of item.evs) inner.appendChild(makeEventDetail(ev));
    details.appendChild(inner);
    row.appendChild(details);
    return row;
  }
  return renderDigest(item);
}

function digestLines(after) {
  const changed = [];
  const needs = [];
  let routine = 0;
  const failed = [];
  for (const t of after) {
    if (t.kind === 'watch_reply' && t.reply_class === 'report') {
      changed.push({ id: t.id, text: t.summary || '' });
      const m = /(?:^|\n)\s*\**Next\**:?\**\s*(.+)/i.exec(t.text || '');
      if (m && !/^\W*nothing/i.test(m[1])) needs.push({ id: t.id, text: m[1].replace(/[*_`]/g, '').slice(0, 160) });
    } else if (t.kind === 'watch_fired') {
      if (t.fire_ok === false) failed.push({ id: t.id, text: `✗ ${t.fire_label}` });
      else routine += 1;
    } else if (t.kind === 'error') {
      failed.push({ id: t.id, text: `✗ ${t.summary || 'error'}` });
    }
  }
  return { changed: failed.concat(changed), needs: needs.slice(-3), routine };
}

function renderDigest(item) {
  const card = document.createElement('div');
  card.className = 'digest-card watch-indent';
  const savedIso = readLastSeen(currentConversationId);
  const savedMs = savedIso ? parseIso(savedIso).getTime() : NaN;
  card.dataset.real = (digestWindow === 'visit' && Number.isFinite(savedMs) && Math.abs(item.cutoff - savedMs) < 1000) ? '1' : '0';
  const { changed, needs, routine } = digestLines(item.after);
  const away = Math.max(1, Math.round((Date.now() - item.cutoff) / 60000));
  const awayText = away >= 120 ? `${Math.round(away / 60)} h` : `${away} min`;

  const head = document.createElement('div');
  head.className = 'digest-head';
  head.innerHTML = '<b>While you were away</b>';
  const span = document.createElement('span');
  span.textContent = `· ${awayText}`;
  head.appendChild(span);
  const seg = document.createElement('span');
  seg.className = 'view-seg digest-seg';
  for (const [k, label] of [['visit', 'last visit'], ['morning', 'this morning'], ['evening', 'yesterday evening']]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    if (k === digestWindow) b.className = 'on';
    b.addEventListener('click', () => { digestWindow = k; renderMessages(lastTurns, lastRunning); });
    seg.appendChild(b);
  }
  head.appendChild(seg);
  card.appendChild(head);

  const body = document.createElement('div');
  body.className = 'digest-body';
  const addSection = (title, rows, jump) => {
    if (!rows.length) return;
    const h = document.createElement('div');
    h.className = 'digest-sec';
    h.textContent = title;
    body.appendChild(h);
    const ul = document.createElement('ul');
    for (const r of rows) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = '#';
      a.textContent = r.text;
      a.addEventListener('click', (e) => {
        e.preventDefault();
        const el = document.getElementById(`msg-${r.id}`);
        if (el) el.scrollIntoView({ block: 'center' });
        else { viewMode = 'raw'; localStorage.setItem(VIEW_MODE_KEY, viewMode); syncViewSeg(); renderMessages(lastTurns, lastRunning); setTimeout(() => document.getElementById(`msg-${r.id}`)?.scrollIntoView({ block: 'center' }), 0); }
      });
      li.appendChild(a);
      ul.appendChild(li);
    }
    body.appendChild(ul);
  };
  addSection('What changed', changed.slice(-8));
  addSection('Needs you', needs);
  if (routine) {
    const f = document.createElement('div');
    f.className = 'digest-folded';
    f.textContent = `${routine} routine event${routine === 1 ? '' : 's'} folded`;
    body.appendChild(f);
  }
  card.appendChild(body);

  const foot = document.createElement('div');
  foot.className = 'digest-foot';
  const act = (label, fn) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('click', fn);
    foot.appendChild(b);
  };
  act('Show all events', () => { viewMode = 'raw'; localStorage.setItem(VIEW_MODE_KEY, viewMode); syncViewSeg(); renderMessages(lastTurns, lastRunning); });
  act('Mark read', () => { const nowIso = new Date().toISOString(); writeLastSeen(currentConversationId, nowIso); digestSince.set(currentConversationId, nowIso); digestDismissed.add(currentConversationId); renderMessages(lastTurns, lastRunning); });
  act('Copy as note', async () => {
    const lines = [`While you were away (${awayText})`]
      .concat(changed.map((r) => `- ${r.text}`), needs.map((r) => `- Needs you: ${r.text}`));
    try { await navigator.clipboard.writeText(lines.join('\n')); } catch (e) { console.error(e); }
  });
  card.appendChild(foot);
  return card;
}

const viewSegEl = document.getElementById('view-seg');
function syncViewSeg() {
  if (!viewSegEl) return;
  viewSegEl.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === viewMode));
}
if (viewSegEl) {
  viewSegEl.querySelectorAll('button').forEach((b) => {
    b.addEventListener('click', () => {
      viewMode = b.dataset.mode;
      localStorage.setItem(VIEW_MODE_KEY, viewMode);
      syncViewSeg();
      renderMessages(lastTurns, lastRunning);
    });
  });
  syncViewSeg();
}

// Per-browser "when did I last look at this session" bookkeeping for the
// "While you were away" digest (see buildDisplayItems()).
const LAST_SEEN_KEY = 'dopAgentLastSeen';
function readLastSeen(convId) {
  try { return (JSON.parse(localStorage.getItem(LAST_SEEN_KEY) || '{}'))[convId] || null; } catch (e) { return null; }
}
function writeLastSeen(convId, iso) {
  if (!convId) return;
  try {
    const m = JSON.parse(localStorage.getItem(LAST_SEEN_KEY) || '{}');
    m[convId] = iso;
    localStorage.setItem(LAST_SEEN_KEY, JSON.stringify(m));
  } catch (e) { /* storage full/blocked - digest just falls back to last_read_at */ }
}
let hiddenAtIso = null;
function noteLeaving() {
  hiddenAtIso = new Date().toISOString();
  writeLastSeen(currentConversationId, hiddenAtIso);
}
window.addEventListener('pagehide', noteLeaving);
window.addEventListener('blur', () => { if (!hiddenAtIso) noteLeaving(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') { noteLeaving(); return; }
  focusReturn();
});
window.addEventListener('focus', focusReturn);
function focusReturn() {
  if (!hiddenAtIso || !currentConversationId) return;
  const awayMs = Date.now() - new Date(hiddenAtIso).getTime();
  const since = hiddenAtIso;
  hiddenAtIso = null;
  if (awayMs < 5 * 60 * 1000) return;
  digestSince.set(currentConversationId, since);
  digestAutoScrolled.add(currentConversationId); // never yank the view on tab return
  digestDismissed.delete(currentConversationId);
  digestWindow = 'visit';
  renderMessages(lastTurns, lastRunning);
}

// Slides view: long assistant messages with several headers are shown one
// section at a time (left rail = table of contents). View-only: the stored
// text and copy actions are untouched. State is keyed by message id because
// renderMessages() rebuilds every bubble on each refresh.
const SLIDES_MIN_HEADERS = 2;
const SLIDES_MIN_CHARS = 600;
const SLIDES_RAIL_KEY = 'dopAgentSlidesRailHidden';
const SLIDES_OFF_KEY = 'dopAgentSlidesOff';
const SLIDES_STATE_KEY = 'dopAgentSlidesState';
const slideState = (() => {
  try { return JSON.parse(localStorage.getItem(SLIDES_STATE_KEY) || '{}'); } catch (e) { return {}; }
})();
function saveSlideState() {
  const keys = Object.keys(slideState);
  if (keys.length > 300) keys.slice(0, keys.length - 300).forEach((k) => delete slideState[k]);
  try { localStorage.setItem(SLIDES_STATE_KEY, JSON.stringify(slideState)); } catch (e) { /* quota */ }
}
let slidesHoverId = null;
let slidesFocusId = null;
document.addEventListener('keydown', (e) => {
  if (!slidesHoverId || (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft')) return;
  if (e.target.closest && e.target.closest('input,textarea,.slides')) return;
  const w = document.querySelector(`.slides[data-msg-id="${slidesHoverId}"]`);
  if (w) w.dispatchEvent(new KeyboardEvent('keydown', { key: e.key, bubbles: false }));
});
function slidesEnabled() { return localStorage.getItem(SLIDES_OFF_KEY) !== '1'; }

function splitSections(container) {
  const kids = Array.from(container.children);
  // Agents often use a bold-only line ("**Section**") instead of a markdown header.
  const isBoldLine = (el) => el.tagName === 'P' && el.children.length === 1
    && /^(STRONG|B)$/.test(el.children[0].tagName)
    && el.textContent.trim() === el.children[0].textContent.trim()
    && el.textContent.trim().length <= 90;
  const isHead = (el) => /^H[1-3]$/.test(el.tagName) || isBoldLine(el);
  const headCount = kids.filter(isHead).length;
  if (headCount < SLIDES_MIN_HEADERS) return null;
  if (headCount < 3 && (container.textContent || '').length < SLIDES_MIN_CHARS) return null;
  const sections = [];
  let cur = null;
  for (const el of kids) {
    if (isHead(el)) {
      cur = { title: el.textContent.trim(), nodes: [el] };
      sections.push(cur);
    } else if (cur) {
      cur.nodes.push(el);
    } else {
      cur = { title: 'Intro', nodes: [el], intro: true };
      sections.push(cur);
    }
  }
  return sections;
}

function applySlides(bubble, msgId) {
  if (!slidesEnabled()) return;
  const sections = splitSections(bubble);
  if (!sections) return;
  const st = slideState[msgId] || { i: 0, all: false, seen: [0] };
  st.i = Math.min(st.i, sections.length - 1);
  const wrap = document.createElement('div');
  wrap.className = 'slides';
  const rail = document.createElement('ul');
  rail.className = 'slides-rail';
  const pane = document.createElement('div');
  pane.className = 'slides-pane';
  const body = document.createElement('div');
  body.className = 'slides-body';
  const nav = document.createElement('div');
  nav.className = 'slides-nav';
  const mkBtn = (txt, title) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'slides-btn'; b.textContent = txt; b.title = title;
    return b;
  };
  const prev = mkBtn('‹ Prev', 'Previous section (←)');
  const next = mkBtn('Next ›', 'Next section (→)');
  const count = document.createElement('span');
  count.className = 'slides-count';
  const allBtn = mkBtn('Show all', 'Show every section at once');
  allBtn.classList.add('slides-all');
  const spacer = document.createElement('span');
  spacer.style.flex = '1';
  const railBtn = mkBtn('☰', 'Hide / show the contents list');
  railBtn.classList.add('slides-railbtn');
  railBtn.addEventListener('click', () => {
    localStorage.setItem(SLIDES_RAIL_KEY, localStorage.getItem(SLIDES_RAIL_KEY) === '1' ? '0' : '1');
    document.querySelectorAll('.slides').forEach((w) => w.classList.toggle('slides-rail-hidden', localStorage.getItem(SLIDES_RAIL_KEY) === '1'));
  });
  nav.append(railBtn, allBtn, spacer, prev, count, next);
  const useRail = sections.length >= 2;
  const slideEls = sections.map((sec) => {
    const d = document.createElement('div');
    d.className = 'slides-slide';
    sec.nodes.forEach((n) => d.appendChild(n));
    body.appendChild(d);
    return d;
  });
  const railItems = sections.map((sec, i) => {
    const li = document.createElement('li');
    li.className = 'slides-rail-item';
    li.append(document.createTextNode(sec.title.replace(/^[#\s]+/, '')));
    li.addEventListener('click', () => go(i));
    rail.appendChild(li);
    return li;
  });
  function render() {
    wrap.classList.toggle('slides-all-mode', st.all);
    slideEls.forEach((d, i) => { d.hidden = !st.all && i !== st.i; });
    railItems.forEach((li, i) => {
      li.classList.toggle('cur', !st.all && i === st.i);
    });
    prev.disabled = st.all || st.i === 0;
    next.disabled = st.all || st.i === sections.length - 1;
    count.textContent = `${st.i + 1} / ${sections.length}`;
    allBtn.textContent = st.all ? 'Slides' : 'Show all';
    slideState[msgId] = st;
    saveSlideState();
  }
  function go(i) {
    st.i = Math.max(0, Math.min(sections.length - 1, i));
    st.all = false;
    if (!st.seen.includes(st.i)) st.seen.push(st.i);
    render();
  }
  prev.addEventListener('click', () => go(st.i - 1));
  next.addEventListener('click', () => go(st.i + 1));
  allBtn.addEventListener('click', () => { st.all = !st.all; render(); });
  wrap.tabIndex = 0;
  wrap.dataset.msgId = msgId;
  let hovered = false;
  wrap.addEventListener('mouseenter', () => { hovered = true; slidesHoverId = msgId; });
  wrap.addEventListener('mouseleave', () => { hovered = false; if (slidesHoverId === msgId) slidesHoverId = null; });
  wrap.addEventListener('keydown', (e) => {
    if (e.target.closest('input,textarea')) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); go(st.i + 1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(st.i - 1); }
  });
  pane.append(body, nav);
  if (useRail) wrap.classList.add('slides-with-rail');
  if (localStorage.getItem(SLIDES_RAIL_KEY) === '1') wrap.classList.add('slides-rail-hidden');
  if (!useRail) railBtn.hidden = true;
  if (useRail) wrap.appendChild(rail);
  wrap.appendChild(pane);
  bubble.appendChild(wrap);
  bubble.classList.add('bubble-slides');
  render();
  // Fix the pane height to the tallest slide so Next/Prev never shift the page.
  requestAnimationFrame(() => {
    if (st.all) return;
    let max = 0;
    slideEls.forEach((d) => { const was = d.hidden; d.hidden = false; max = Math.max(max, d.offsetHeight); d.hidden = was; });
    body.style.minHeight = `${Math.min(max, 480)}px`;
  });
}

(function wireSlidesToggle() {
  const btn = document.getElementById('slides-toggle-btn');
  if (!btn) return;
  const sync = () => { btn.classList.toggle('header-chip-off', !slidesEnabled()); btn.textContent = slidesEnabled() ? '🎞 Slides' : '🎞 Slides off'; };
  sync();
  btn.addEventListener('click', () => {
    localStorage.setItem(SLIDES_OFF_KEY, slidesEnabled() ? '1' : '0');
    sync();
    renderMessages(lastTurns, lastRunning);
  });
})();

// Mermaid/Chart.js bake theme colours into SVG/canvas, so rebuild bubbles when the theme flips.
window.addEventListener('dop-theme-change', () => {
  if (typeof currentConversationId !== 'undefined' && currentConversationId) renderMessages(lastTurns, lastRunning);
});

function renderMessages(turns, running) {
  // Capture scroll position before wiping the DOM: if the user has scrolled
  // up, a periodic re-render (status heartbeat, SSE push) must not yank them
  // back down. Re-pin to the bottom only if they were already there (or the
  // pane is empty/freshly opened, where scrollHeight is 0).
  const distanceFromBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
  // pinnedToBottom covers late layout growth that pushed us past the
  // threshold before this render ran.
  const shouldStickToBottom = distanceFromBottom <= SCROLL_STICK_TO_BOTTOM_PX || pinnedToBottom;
  const savedScrollTop = messagesEl.scrollTop;

  // Unfurl cards live outside #messages (appended to <body> so they can
  // float near the cursor - see enhanceLinkPreviews()/wireUnfurlHover()),
  // so wiping #messages does not remove them; the rebuilt bubbles re-create
  // their own cards, so remove the old ones here to avoid stale tooltips.
  document.querySelectorAll('.unfurl-card').forEach((el) => el.remove());

  if (window.IdeasWidget) window.IdeasWidget.captureFocus();
  if (window.AskWidget) window.AskWidget.captureFocus();
  slidesFocusId = document.activeElement?.closest?.('.slides')?.dataset.msgId || null;
  // Hold the old content height while the DOM is rebuilt so the browser
  // never clamps scrollTop.
  const heldHeight = messagesEl.scrollHeight;
  messagesEl.style.overflowAnchor = 'none';
  const holder = document.createElement('div');
  holder.style.cssText = `height:${heldHeight}px;flex:none;visibility:hidden`;
  messagesEl.innerHTML = '';
  messagesEl.appendChild(holder);
  if (messagesEl.scrollTop !== savedScrollTop) messagesEl.scrollTop = savedScrollTop;
  let lastStatusBubble = null;
  const displayItems = buildDisplayItems(turns, viewMode);
  for (const item of displayItems) {
    if (item.type !== 'turn') {
      messagesEl.appendChild(renderSpecialItem(item));
      continue;
    }
    const t = item.turn;
    const isStatus = t.role === 'status';
    const role = t.role === 'user' || t.role === 'system' || t.role === 'status' ? t.role : 'assistant';
    const row = document.createElement('div');
    row.className = `msg-row role-${isStatus ? 'assistant role-live' : role}`;
    if (t.id) row.id = `msg-${t.id}`;

    const avatar = document.createElement('div');
    avatar.className = `avatar avatar-${isStatus ? 'assistant' : role}`;
    avatar.textContent = AVATAR_GLYPH[role] || '🤖';

    const bubble = document.createElement('div');
    bubble.className = `bubble ${isStatus ? 'assistant' : role}${t.kind === 'error' ? ' bubble-fail' : ''}`;
    const html = renderMarkdown(t.text || '');
    // Transcription-in-progress ("🎧 Transcribing..."/"⏳ Queued...") is a
    // persisted status message, not the synthetic live-turn row, so the
    // `running` check below doesn't cover it; content-match the known
    // placeholder text (see handlers.py's _transcribe_one()/
    // transcribe_web_upload()) to give it the same spinner.
    const isTranscribingPlaceholder = isStatus
      && /_Transcribing\.\.\._|_Queued — waiting for another transcription/.test(t.text || '');
    const showSpinner = (isStatus && running) || isTranscribingPlaceholder;
    bubble.innerHTML = showSpinner ? `<span class="live-spinner"></span>${html}` : html;
    if (role === 'assistant' && !isStatus && t.kind !== 'error' && window.MermaidRender) window.MermaidRender.apply(bubble, t.id, running && turns[turns.length - 1] === t);
    if (role === 'assistant' && !isStatus && t.kind !== 'error' && window.ChartRender) window.ChartRender.apply(bubble, t.id, running && turns[turns.length - 1] === t);
    enhanceCodeBlocks(bubble);
    bubble.querySelectorAll('a').forEach((a) => {
      const href = a.getAttribute('href') || '';
      if (href.startsWith('#session:')) {
        // In-app backlink (e.g. "continue in new session"'s reference to
        // the session it came from) - jump straight to that session
        // instead of following it as a dead browser anchor.
        a.addEventListener('click', (e) => {
          e.preventDefault();
          openSession(href.slice('#session:'.length));
        });
        return;
      }
      if (parseOwnSessionLink(href) || parseOwnSettingsLink(href)) {
        // A link (absolute or relative) that resolves to one of THIS
        // bot's own sessions (session_create's returned web_url, a
        // "continue in new session" link, a pasted bookmark, ...) -
        // navigate in-app instead of opening a new tab/window. Actual
        // click handling is a single delegated document-level listener
        // (see below) so it also covers the unfurl-card's own link.
        return;
      }
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    });
    enhanceLinkPreviews(bubble);
    if (role === 'assistant' && !isStatus && t.kind !== 'error' && t.id && window.IdeasWidget) window.IdeasWidget.apply(bubble, t.id, t.text, currentConversationId, running && turns[turns.length - 1] === t, turns[turns.length - 1] === t && Date.now() - (parseIso(t.timestamp || t.updated_at) || new Date(0)).getTime() < 120000);
    if (role === 'assistant' && !isStatus && t.kind !== 'error' && t.id && window.AskWidget) window.AskWidget.apply(bubble, t.id, t.text, currentConversationId, running && turns[turns.length - 1] === t);
    if (role === 'assistant' && !isStatus && t.kind !== 'error' && t.id && window.ActionsWidget) window.ActionsWidget.apply(bubble, t.id, t.text, currentConversationId, running && turns[turns.length - 1] === t);
    if (role === 'assistant' && !isStatus && t.kind !== 'error' && t.id) applySlides(bubble, t.id);

    // Voice-transcript buttons (Auto-send/Preview before sending/Send to
    // Copilot) - real Web UI parity with Slack's block-kit buttons, driven
    // by this message's `actions` field (see message_store.py's `actions`
    // column + handlers.py's _arm_transcript_mode()/web_transcript_action()).
    // Only transcript-related placeholder/status messages ever carry a
    // non-empty `actions` array.
    if (Array.isArray(t.actions) && t.actions.length > 0 && t.id) {
      const actionsRow = document.createElement('div');
      actionsRow.className = 'msg-transcript-actions';
      for (const action of t.actions) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'msg-transcript-action-btn';
        btn.textContent = convertEmoji(action.label || action.id);
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          btn.disabled = true;
          const ok = await triggerTranscriptAction(t.id, action.id);
          // On failure no SSE update will re-render the button, so re-enable
          // it and flag the error to allow a retry.
          if (!ok) {
            btn.disabled = false;
            btn.title = 'Action failed - click to retry';
          }
        });
        actionsRow.appendChild(btn);
      }
      bubble.appendChild(actionsRow);
    }

    // Image attachments captured from Slack (see handlers.py's
    // maybe_capture_images / web_server.py's attachment-serving route).
    // Auth is cookie-based (see _require_auth in web_server.py), so a
    // plain <img src> works without any token plumbing here.
    if (Array.isArray(t.attachments) && t.attachments.length > 0) {
      const attachmentsRow = document.createElement('div');
      attachmentsRow.className = 'msg-attachments';
      for (const att of t.attachments) {
        if (att.kind !== 'image') continue; // generic file chips: deferred, see plan.md
        const link = document.createElement('a');
        link.href = att.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        const img = document.createElement('img');
        img.className = 'attachment-preview';
        img.src = att.url;
        img.loading = 'lazy';
        img.alt = att.filename || 'attachment';
        link.appendChild(img);
        attachmentsRow.appendChild(link);
      }
      if (attachmentsRow.children.length > 0) bubble.appendChild(attachmentsRow);
    }

    const ts = fullTimestamp(t.updated_at || t.timestamp);

    // Deep-link a specific message. Skipped on synthetic "live
    // status" bubbles (isStatus) - they're transient (replaced/removed the
    // moment the turn finishes) and have no stable id worth sharing.
    if (t.id && !isStatus) {
      // Message timestamp - shown only on row hover, and doubles as the
      // "copy deep link" control: clicking appends a "✅ Copied" confirmation
      // after the existing timestamp text (rather than replacing it), so the
      // actual time stays readable.
      if (ts) {
        const timeEl = document.createElement('button');
        timeEl.type = 'button';
        timeEl.className = 'msg-hover-time';
        timeEl.textContent = ts;
        timeEl.title = 'Click to copy a link directly to this message';
        timeEl.addEventListener('click', async (e) => {
          e.stopPropagation();
          const url = deepLinkUrlFor(currentConversationId, t.id);
          try {
            await navigator.clipboard.writeText(url);
            timeEl.textContent = `${ts} ✅ Copied`;
            setTimeout(() => { timeEl.textContent = ts; }, 1000);
          } catch (err) {
            console.error('copy deep link failed', err);
          }
        });
        row.appendChild(timeEl);
      }

      // Bookmark/star this message - unlike the deep-link
      // control (hover-reveal only), the star stays visible once bookmarked
      // so starred messages are obvious at a glance.
      const bookmarkBtn = document.createElement('button');
      bookmarkBtn.type = 'button';
      bookmarkBtn.className = `msg-bookmark-btn${t.bookmarked ? ' msg-bookmark-btn-active' : ''}`;
      bookmarkBtn.title = t.bookmarked ? 'Remove bookmark' : 'Bookmark this message';
      bookmarkBtn.textContent = t.bookmarked ? '⭐' : '☆';
      bookmarkBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await toggleMessageBookmark(t.id, !t.bookmarked);
      });
      row.appendChild(bookmarkBtn);
    }

    if (role === 'user') {
      row.appendChild(bubble);
      row.appendChild(avatar);
    } else {
      row.appendChild(avatar);
      row.appendChild(bubble);
    }
    messagesEl.appendChild(row);
    if (isStatus) lastStatusBubble = bubble;
  }

  // Re-parent (not clone) the Stop button to the bottom of the live
  // "Working..." status bubble, right where the user is already looking,
  // instead of leaving it in the header where it's easy to miss. Moving
  // the actual DOM node preserves its click listener/disabled state across
  // re-renders - only shown while a turn is genuinely running and there is
  // a status bubble to attach it to.
  if (running) {
    // Normally there's already a live "status" bubble to attach to, but a
    // turn can be `running` for a brief moment before the first status
    // heartbeat arrives - fall back to appending at the end of the
    // transcript so Stop is never silently unreachable.
    (lastStatusBubble || messagesEl).appendChild(stopTurnBtn);
    stopTurnBtn.style.display = 'block';
  } else {
    stopTurnBtn.style.display = 'none';
  }

  // One-click "retry last message": if the turn history isn't currently
  // running and the very last turn is an assistant reply matching one of
  // the known failure/timeout wordings, show a Retry button that resends
  // the preceding user message's text as a brand-new send - simpler and
  // more robust than trying to somehow "resume" the exact failed turn.
  if (!running && turns.length >= 2) {
    const last = turns[turns.length - 1];
    const prev = turns[turns.length - 2];
    if (last.role === 'assistant' && isErrorResponseText(last.text) && prev.role === 'user') {
      const retryRow = document.createElement('div');
      retryRow.className = 'retry-last-message-row';
      const retryBtn = document.createElement('button');
      retryBtn.type = 'button';
      retryBtn.className = 'retry-last-message-btn';
      retryBtn.textContent = '🔄 Retry last message';
      retryBtn.title = 'Resend your last message after this failure/timeout';
      retryBtn.addEventListener('click', () => retryLastMessage(prev.text));
      retryRow.appendChild(retryBtn);
      messagesEl.appendChild(retryRow);
    }
  }

  if (slidesFocusId) {
    messagesEl.querySelector(`.slides[data-msg-id="${slidesFocusId}"]`)?.focus({ preventScroll: true });
    slidesFocusId = null;
  }

  holder.remove();
  pinnedToBottom = shouldStickToBottom;
  observeRowGrowth();
  if (shouldStickToBottom) {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } else if (messagesEl.scrollTop !== savedScrollTop) {
    // Any layout flush while the pane is empty/short clamps scrollTop to 0;
    // put the old position back.
    messagesEl.scrollTop = savedScrollTop;
  }

  // "While you were away" digest: on the first render after opening a
  // session, bring the digest into view (once) instead of the very bottom.
  const digestEl = messagesEl.querySelector('.digest-card');
  if (digestEl && digestEl.dataset.real === '1' && !digestAutoScrolled.has(currentConversationId) && !pendingDeepLinkMessageId) {
    digestAutoScrolled.add(currentConversationId);
    digestEl.scrollIntoView({ block: 'start' });
  }

  // Deep-link URL to a specific message - if a #msg=... hash
  // targeting this conversation is pending (parsed once at page load, or
  // set right before navigating to another session's deep link), scroll
  // to it and briefly highlight it, overriding the scroll-to-bottom above.
  // Cleared after use so a later ordinary re-render doesn't keep
  // re-triggering the jump/highlight on every SSE refresh.
  if (pendingDeepLinkMessageId && currentConversationId) {
    const targetEl = document.getElementById(`msg-${pendingDeepLinkMessageId}`);
    if (targetEl) {
      pendingDeepLinkMessageId = null;
      targetEl.scrollIntoView({ block: 'center' });
      targetEl.classList.add('msg-row-deep-link-highlight');
      setTimeout(() => targetEl.classList.remove('msg-row-deep-link-highlight'), 2000);
    }
  }
}

const turnElapsedEl = document.getElementById('turn-elapsed');
let turnElapsedInterval = null;
let currentTurnStartedAt = null; // epoch seconds, or null when not running

function stopTurnElapsedTicker() {
  if (turnElapsedInterval) {
    clearInterval(turnElapsedInterval);
    turnElapsedInterval = null;
  }
  currentTurnStartedAt = null;
  turnElapsedEl.style.display = 'none';
  turnElapsedEl.classList.remove('turn-elapsed-warning', 'turn-elapsed-critical');
}

function tickTurnElapsed() {
  if (!currentTurnStartedAt) return;
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - currentTurnStartedAt));
  turnElapsedEl.textContent = `⏱ Working... ${seconds}s`;

  // Warn as the turn approaches SESSION_TIMEOUT (the per-turn timeout the
  // backend actually enforces - see copilot_session.py's send_and_wait()
  // call) so a slow/stuck turn isn't a silent surprise when it eventually
  // times out. Thresholds: "warning" once remaining time drops to whichever
  // is larger of 20% of the total timeout or 30s; "critical" (more urgent
  // styling) in the last 10s.
  turnElapsedEl.classList.remove('turn-elapsed-warning', 'turn-elapsed-critical');
  if (typeof sessionTimeoutSeconds === 'number' && sessionTimeoutSeconds > 0) {
    const remaining = sessionTimeoutSeconds - seconds;
    const warnAt = Math.max(30, sessionTimeoutSeconds * 0.2);
    if (remaining <= 10) {
      turnElapsedEl.classList.add('turn-elapsed-critical');
      turnElapsedEl.title = `Approaching the ${sessionTimeoutSeconds}s turn timeout — may time out any moment`;
    } else if (remaining <= warnAt) {
      turnElapsedEl.classList.add('turn-elapsed-warning');
      turnElapsedEl.title = `Approaching the ${sessionTimeoutSeconds}s turn timeout (~${Math.max(0, Math.floor(remaining))}s left)`;
    } else {
      turnElapsedEl.title = '';
    }
  }
}

function startTurnElapsedTicker(startedAt) {
  currentTurnStartedAt = startedAt;
  turnElapsedEl.style.display = 'inline';
  tickTurnElapsed();
  if (!turnElapsedInterval) {
    turnElapsedInterval = setInterval(tickTurnElapsed, 1000);
  }
}

// Tracks whether the currently-open conversation's last known SSE/poll
// state was "running" - used by the compose-form submit handler to decide
// whether to show the "queued / interrupting current turn" badge, since
// CopilotConversation.send() (see copilot_session.py) aborts any
// in-progress turn immediately rather than literally queuing behind it;
// the badge is purely so the user isn't left wondering why nothing visibly
// happened between clicking Send and the interrupt actually landing.
let lastRunning = false;

// --- Turn-finish browser notification ---------------------------------
// Fires a Notification when a running turn transitions to finished while
// the tab is not focused/visible, so you don't have to keep tabbing back
// to check on a long-running turn. Permission is requested lazily (only
// once a turn is actually seen running) rather than on page load, so we
// don't throw an unsolicited permission prompt at someone just opening
// the UI to read a transcript.
let notificationPermissionRequested = false;

function ensureNotificationPermission() {
  if (typeof Notification === 'undefined') return;
  if (notificationPermissionRequested) return;
  notificationPermissionRequested = true;
  if (Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}

function notifyTurnFinished(conversationId) {
  if (typeof Notification === 'undefined') return;
  if (Notification.permission !== 'granted') return;
  if (document.hasFocus() && !document.hidden) return; // tab is already in front
  if (isWithinQuietHours()) return;
  const session = sessions.find((s) => s.conversation_id === conversationId);
  const title = (session && (session.title || session.preview)) || 'DOP Agent session';
  try {
    const notification = new Notification('Turn finished', {
      body: title,
      tag: `turn-finished-${conversationId}`,
    });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch (e) {
    console.error('failed to show turn-finished notification', e);
  }
}

function tryPatchStatusInPlace(data) {
  const prev = lastTurns;
  const next = data.turns || [];
  if (!data.running || !lastRunning || lastSnapshotConv !== currentConversationId) return false;
  if (!prev.length || prev.length !== next.length || messagesEl.childElementCount === 0) return false;
  const patches = [];
  for (let i = 0; i < next.length; i++) {
    const a = prev[i];
    const b = next[i];
    if (a.id !== b.id || a.role !== b.role) return false;
    if (JSON.stringify(a) === JSON.stringify(b)) continue;
    if (b.role !== 'status') return false;
    const bubble = document.getElementById(`msg-${b.id}`)?.querySelector('.bubble');
    if (!bubble) return false;
    patches.push([bubble, b]);
  }
  if (!patches.length) return false;
  // Capture before touching the DOM: emptying a bubble clamps scrollTop,
  // and that scroll event would otherwise flip pinnedToBottom to false.
  const wasPinned = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight <= SCROLL_STICK_TO_BOTTOM_PX;
  const savedTop = messagesEl.scrollTop;
  const keepBtn = stopTurnBtn.parentElement && patches.some(([bub]) => bub.contains(stopTurnBtn));
  for (const [bubble, t] of patches) {
    if (keepBtn && bubble.contains(stopTurnBtn)) bubble.removeChild(stopTurnBtn);
    bubble.innerHTML = `<span class="live-spinner"></span>${renderMarkdown(t.text || '')}`;
    if (keepBtn && !bubble.contains(stopTurnBtn)) bubble.appendChild(stopTurnBtn);
  }
  if (data.turn_started_at) startTurnElapsedTicker(data.turn_started_at);
  messagesEl.scrollTop = wasPinned ? messagesEl.scrollHeight : savedTop;
  pinnedToBottom = wasPinned;
  return true;
}

// Track "user is at the bottom" from real scroll events, and re-pin when
// the pane itself resizes (header/compose box/badges changing height) so a
// layout change elsewhere never leaves the view displaced from the bottom.
let pinnedToBottom = true;
// Only treat a scroll as "user left the bottom" if it follows real user
// input (wheel/touch/keys/scrollbar drag). Programmatic scrollTop changes
// during re-renders and clamps from shrinking/growing content must not.
let lastUserInputAt = 0;
const markUserInput = () => { lastUserInputAt = Date.now(); };
['wheel', 'touchstart', 'touchmove', 'mousedown', 'keydown'].forEach((evt) => messagesEl.addEventListener(evt, markUserInput, { passive: true, capture: true }));
messagesEl.addEventListener('scroll', () => {
  const distance = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
  if (distance <= SCROLL_STICK_TO_BOTTOM_PX) pinnedToBottom = true;
  else if (Date.now() - lastUserInputAt < 600) pinnedToBottom = false;
}, { passive: true });
// Rows can grow after render (images, diagrams, charts, link previews,
// highlighting): keep a pinned view glued to the bottom through that, or
// every later push sees "not at bottom" and the view stays stranded above
// the live status.
let rowResizeObserver = null;
function observeRowGrowth() {
  if (typeof ResizeObserver === 'undefined') return;
  if (rowResizeObserver) rowResizeObserver.disconnect();
  rowResizeObserver = new ResizeObserver(() => {
    if (pinnedToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  });
  for (const el of messagesEl.children) rowResizeObserver.observe(el);
}
if (typeof ResizeObserver !== 'undefined') {
  let lastPaneHeight = messagesEl.clientHeight;
  new ResizeObserver(() => {
    const h = messagesEl.clientHeight;
    if (h === lastPaneHeight) return;
    const wasPinned = pinnedToBottom;
    lastPaneHeight = h;
    if (wasPinned) { messagesEl.scrollTop = messagesEl.scrollHeight; pinnedToBottom = true; }
  }).observe(messagesEl);
}

let lastSnapshotKey = null;
let lastSnapshotConv = null;

function applyConversationData(data) {
  if (!data || data.conversation_id !== currentConversationId) return;
  lastTurns = data.turns || [];
  if (!digestSince.has(currentConversationId)) {
    // Prefer this browser's own "last left" time: the server's last_read_at is
    // bumped continuously while the session is open, so it is ~now and would
    // leave nothing to summarise.
    digestSince.set(currentConversationId, readLastSeen(currentConversationId) || data.last_read_at || null);
  }
  // Skip identical snapshots (keepalive-ish pushes, list churn): a rebuild
  // resets scrollTop, which kills momentum scrolling and made it
  // impossible to scroll down while a turn was live.
  const snapshotKey = JSON.stringify([data.turns, !!data.running, data.turn_started_at]);
  if (snapshotKey === lastSnapshotKey && lastSnapshotConv === currentConversationId && messagesEl.childElementCount > 0) return;
  // Live-status heartbeat ("Working… 19s elapsed" -> "23s"): when only the
  // status bubble text changed, patch it in place instead of rebuilding
  // the whole pane (the rebuild is what made the view jump).
  if (tryPatchStatusInPlace(data)) {
    lastSnapshotKey = snapshotKey;
    lastSnapshotConv = currentConversationId;
    lastTurns = data.turns || [];
    return;
  }
  lastSnapshotKey = snapshotKey;
  lastSnapshotConv = currentConversationId;
  const wasRunning = lastRunning;
  lastRunning = !!data.running;
  if (data.running) {
    ensureNotificationPermission();
  } else if (wasRunning) {
    notifyTurnFinished(data.conversation_id);
  }
  renderMessages(lastTurns, !!data.running);
  // NOTE: renderMessages() above already positions/shows/hides
  // stop-turn-btn (re-parented into the live status bubble) - do not
  // toggle its display here too, or a stale 'inline-block' from before the
  // button lived inside .bubble would silently override the CSS layout.
  // Any fresh state for this conversation means the interrupt (if we were
  // showing the queued badge) has already been reflected server-side -
  // safe to hide it now.
  queuedBadgeEl.style.display = 'none';
  // Live per-second "Working... Ns" counter, ticked client-side from
  // turn_started_at (an actual epoch timestamp from the backend) rather
  // than relying on the ~15s server-side status heartbeat, which only
  // refreshes the message body text periodically.
  if (data.running && data.turn_started_at) {
    startTurnElapsedTicker(data.turn_started_at);
  } else {
    stopTurnElapsedTicker();
  }
}

// SSE connection status, reflected in the header health strip's "Live"
// item. There are up to two live EventSource connections at once (the
// always-on session-list stream + the per-conversation stream while a
// thread is open); either one erroring means the browser has lost its
// live-update connection to the bot, so we track a single combined
// "connected" flag rather than one indicator per stream.
let sseConnected = null; // null = not yet known, true/false once observed

function setSseConnected(connected) {
  if (sseConnected === connected) return;
  sseConnected = connected;
  healthLiveEl.classList.toggle('health-ok', connected);
  healthLiveEl.classList.toggle('health-bad', !connected);
  healthLiveEl.title = connected ? 'Live updates: connected' : 'Live updates: reconnecting…';
  healthLiveEl.textContent = '';
  const dot = document.createElement('span');
  dot.className = 'health-dot';
  healthLiveEl.appendChild(dot);
  healthLiveEl.appendChild(document.createTextNode(connected ? 'Live' : 'Reconnecting…'));
}

function connectSessionListStream() {
  if (sessionListStream) {
    sessionListStream.close();
  }
  sessionListStream = new EventSource('/api/stream/sessions');
  sessionListStream.onopen = () => setSseConnected(true);
  sessionListStream.addEventListener('sessions', (event) => {
    setSseConnected(true);
    const data = JSON.parse(event.data);
    sessions = data.sessions || [];
    groups = data.groups || [];
    renderSessionList();
    updateThreadHeader();
    if (window.Inbox) window.Inbox.refreshSoon();
  });
  sessionListStream.onerror = () => {
    console.error('session list stream error');
    setSseConnected(false);
  };
}

function connectConversationStream(conversationId) {
  if (conversationStream) {
    conversationStream.close();
    conversationStream = null;
  }
  if (!conversationId) return;
  conversationStream = new EventSource(`/api/stream/sessions/${encodeURIComponent(conversationId)}`);
  conversationStream.onopen = () => setSseConnected(true);
  conversationStream.addEventListener('conversation', (event) => {
    setSseConnected(true);
    if (conversationId !== currentConversationId) return;
    sseDeliveredFor = conversationId;
    applyConversationData(JSON.parse(event.data));
  });
  conversationStream.onerror = () => {
    console.error('conversation stream error', conversationId);
    setSseConnected(false);
  };
}

let openSessionToken = 0;
let sseDeliveredFor = null;

function openSession(conversationId) {
  setDraft(currentConversationId, composeText.value);
  if (currentConversationId && currentConversationId !== conversationId) writeLastSeen(currentConversationId, new Date().toISOString());
  currentConversationId = conversationId;
  digestSince.delete(conversationId);
  digestAutoScrolled.delete(conversationId);
  digestDismissed.delete(conversationId);
  saveLastSession(conversationId);
  emptyStateEl.style.display = 'none';
  hideGroupOverview();
  if (window.Inbox) window.Inbox.leave();
  threadViewEl.style.display = 'flex';
  lastTurns = [];
  lastRunning = false;
  closeSessionInfoPopover();
  closeNotePopover();
  noteTextarea.value = '';
  noteLoadedForConversationId = null;
  closeBookmarksPopover();
  // Clear any previous session's transcript immediately rather than
  // leaving it visible until the new EventSource connection delivers its
  // first push (opening a fresh SSE connection has real-world latency -
  // TCP/TLS handshake, auth, browser per-host connection limits - that
  // was making the switch feel like it "hung" for a couple of seconds).
  messagesEl.innerHTML = '';
  queuedBadgeEl.style.display = 'none';
  stopTurnBtn.style.display = 'none';
  stopTurnElapsedTicker();
  const session = sessions.find((x) => x.conversation_id === conversationId);
  if (session) markSessionRead(conversationId, session.updated_at);
  renderSessionList();
  updateThreadHeader();
  // Fetch the current transcript over plain REST right away so the pane
  // renders instantly, instead of waiting on the SSE stream's first
  // event. The SSE connection (below) still takes over for live updates
  // from this point on.
  const openToken = ++openSessionToken;
  let delivered = false;
  const fetchInitial = (attempt) => api(`/api/sessions/${encodeURIComponent(conversationId)}`)
    .then((data) => {
      // Drop a stale REST reply if the user switched away, or if the SSE
      // stream already delivered a (newer) snapshot.
      if (openToken !== openSessionToken || delivered || sseDeliveredFor === conversationId) return;
      delivered = true;
      applyConversationData(data);
    })
    .catch((e) => {
      console.error('initial transcript fetch failed', e);
      if (attempt < 3 && openToken === openSessionToken && sseDeliveredFor !== conversationId) {
        setTimeout(() => fetchInitial(attempt + 1), 800 * (attempt + 1));
      }
    });
  fetchInitial(0);
  sseDeliveredFor = null;
  connectConversationStream(conversationId);
  // Watchdog: if neither REST nor SSE has painted this session shortly
  // after opening, refetch instead of waiting for the next live update.
  setTimeout(() => {
    if (openToken !== openSessionToken || delivered || sseDeliveredFor === conversationId) return;
    api(`/api/sessions/${encodeURIComponent(conversationId)}`)
      .then((data) => { if (openToken === openSessionToken && !delivered && sseDeliveredFor !== conversationId) { delivered = true; applyConversationData(data); } })
      .catch((e) => console.error('watchdog transcript fetch failed', e));
  }, 2500);
  composeText.value = getDraft(conversationId);
  autoGrowComposeText();
}

// --- Full-text search across sessions/messages ------------------------
//
// By default the search box only matches session titles - a fast,
// client-side filter over the already-loaded `sessions` array (no round
// trip needed, since the sidebar already has every session's title in
// memory). Ticking "Also search messages" additionally queries
// GET /api/search?q=... (message_store.search_messages(), a plain
// case-insensitive LIKE scan) for message-content matches, merged in
// below the title matches. Clicking a result opens that session via the
// existing openSession() and clears the search. Debounced so typing
// doesn't fire a request per keystroke (only matters for the
// message-content path - the title filter is synchronous/instant).

let searchDebounceTimer = null;
const SEARCH_DEBOUNCE_MS = 200;

function clearSearchResults() {
  searchResultsEl.style.display = 'none';
  searchResultsEl.innerHTML = '';
}

function searchSessionTitles(query) {
  const lowerQuery = query.toLowerCase();
  return sessions
    .filter((s) => markdownToPlainText(s.title || '').toLowerCase().includes(lowerQuery))
    .map((s) => ({
      conversation_id: s.conversation_id,
      title: s.title || '',
      snippet: '',
      isTitleMatch: true,
    }));
}

async function runSearch(query) {
  if (!query.trim()) {
    clearSearchResults();
    return;
  }
  const titleMatches = searchSessionTitles(query);
  if (!searchIncludeMessagesEl.checked) {
    renderSearchResults(titleMatches, query);
    return;
  }
  let data;
  try {
    data = await api(`/api/search?q=${encodeURIComponent(query)}`);
  } catch (e) {
    console.error('search failed', e);
    renderSearchResults(titleMatches, query);
    return;
  }
  const alreadyMatchedIds = new Set(titleMatches.map((r) => r.conversation_id));
  const messageMatches = (data.results || [])
    .filter((r) => !alreadyMatchedIds.has(r.conversation_id))
    .map((r) => ({ ...r, isTitleMatch: false }));
  renderSearchResults([...titleMatches, ...messageMatches], query);
}

function renderSearchResults(results, query) {
  if (!results.length) {
    searchResultsEl.innerHTML = '<div class="search-empty">No matches</div>';
    searchResultsEl.style.display = 'block';
    return;
  }
  searchResultsEl.innerHTML = results
    .map((r) => {
      const title = escapeHtml(markdownToPlainText(r.title));
      const snippet = r.snippet ? `<div class="search-result-snippet">${escapeHtml(r.snippet)}</div>` : '';
      return `
        <div class="search-result" data-conversation-id="${escapeHtml(r.conversation_id)}">
          <div class="search-result-title">${title}</div>
          ${snippet}
        </div>
      `;
    })
    .join('');
  searchResultsEl.style.display = 'block';
}

searchInputEl.addEventListener('input', () => {
  const query = searchInputEl.value;
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
});

// Re-running the search when the "also search messages" checkbox is
// toggled means you don't have to retype/nudge the query to see the
// broader result set take effect immediately.
searchIncludeMessagesEl.addEventListener('change', () => {
  if (searchInputEl.value.trim()) runSearch(searchInputEl.value);
});

searchResultsEl.addEventListener('click', (evt) => {
  const item = evt.target.closest('.search-result');
  if (!item) return;
  const conversationId = item.dataset.conversationId;
  searchInputEl.value = '';
  clearSearchResults();
  openSession(conversationId);
});

// Escape closes the results dropdown without needing to clear the text.
searchInputEl.addEventListener('keydown', (evt) => {
  if (evt.key === 'Escape') {
    clearSearchResults();
    searchInputEl.blur();
  }
});

async function sendMessageText(text, { restoreDraftOnFailure = true } = {}) {
  const sentForConversationId = currentConversationId;
  composeText.disabled = true;
  // Deliberately sending a message is a "jump to the latest" action, even
  // if the user had scrolled up to read earlier history - jump down now
  // so the sticky-bottom check in the next renderMessages() (triggered by
  // this send's own SSE push) naturally keeps tracking the bottom.
  messagesEl.scrollTop = messagesEl.scrollHeight;
  if (lastRunning) {
    // A turn is already in progress for this conversation - send() will
    // abort it immediately rather than literally queue behind it, but
    // that interrupt+restart happens server-side and takes a beat to be
    // reflected back over SSE. Show a transient badge so it's clear the
    // click registered instead of looking like nothing happened.
    queuedBadgeEl.style.display = 'block';
  }
  try {
    const result = await api('/api/send', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: sentForConversationId, text }),
    });
    if (window.Inbox && sentForConversationId) window.Inbox.dropConversation(sentForConversationId);
    // Only follow a redirect to a new conversation if the user is still
    // looking at the session the message was sent from; otherwise the
    // selection would jump back after they switched away.
    if (result.conversation_id && result.conversation_id !== sentForConversationId
        && currentConversationId === sentForConversationId) {
      openSession(result.conversation_id);
    }
  } catch (e) {
    alert(`Send failed: ${e.message}`);
    queuedBadgeEl.style.display = 'none';
    if (restoreDraftOnFailure) {
      // Restore the draft if sending failed, since the text was never delivered.
      if (currentConversationId === sentForConversationId) {
        composeText.value = text;
        autoGrowComposeText();
      }
      setDraft(sentForConversationId, text);
    }
  } finally {
    composeText.disabled = false;
    if (!(window.IdeasWidget && window.IdeasWidget.reclaimFocus())) composeText.focus();
  }
}

async function retryLastMessage(text) {
  // One-click retry for the "Retry last message" button rendered by
  // renderMessages() after a failed/timed-out turn - resends the same
  // text as a brand-new /api/send call (there's no "resume the exact
  // failed turn" concept; a fresh send reuses the same Copilot session
  // and history like any other message).
  await sendMessageText(text, { restoreDraftOnFailure: false });
}

// --- Voice messages (mic button) -------------------------------------------
//
// Web UI equivalent of Slack's "upload a voice message" flow: records a
// clip via MediaRecorder, then POSTs it to
// /api/sessions/{conversation_id}/transcribe, which runs it through the
// exact same platform-agnostic transcribe_web_upload() -> _transcribe_one()
// -> Whisper pipeline handlers.py already uses for Slack voice messages
// (adapter=WebAdapter) - the "Transcribing..." placeholder,
// Auto-send/Preview buttons, and final transcript all come back over the
// normal session SSE stream exactly like a Slack voice message would.
//
// Deliberately requires an existing currentConversationId (a session must
// already be open/started via text first) rather than also handling the
// "brand new, no conversation yet" case /api/send supports - lazy-creating
// a conversation specifically for a voice upload has its own race/UX
// questions (what shows in the sidebar while still transcribing? what if
// the recording is cancelled?) that are out of scope here; starting a
// session with a short text message first is a reasonable, clearly
// communicated limitation.
let mediaRecorder = null;
let recordedChunks = [];
// Chunked upload while recording (server saves audio to disk as it arrives, so a
// closed tab / bot restart keeps what was captured). null => one-shot upload on stop.
let chunkUpload = null;
const VOICE_TIMESLICE_MS = 15000;

async function startChunkUpload(conversationId, mimeType) {
  const ext = ((mimeType || 'audio/webm').split('/')[1] || 'webm').split(';')[0];
  try {
    const res = await fetch(`/api/sessions/${conversationId}/audio-upload?ext=${encodeURIComponent(ext)}`, { method: 'POST' });
    if (!res.ok) return null;
    const { upload_id } = await res.json();
    return { conversationId, id: upload_id, seq: 0, chain: Promise.resolve(), failed: false };
  } catch (e) {
    return null;
  }
}

function queueChunk(up, blob) {
  const seq = up.seq++;
  up.chain = up.chain.then(async () => {
    if (up.failed) return;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const r = await fetch(`/api/sessions/${up.conversationId}/audio-upload/${up.id}/chunk?seq=${seq}`, { method: 'PUT', body: blob });
        if (r.ok) return;
        if ([404, 409, 413].includes(r.status)) break;
      } catch (e) { /* retry */ }
      await new Promise((resolve) => setTimeout(resolve, 1000 * (attempt + 1)));
    }
    up.failed = true;
  });
}

async function finishChunkUpload(up) {
  await up.chain;
  if (up.failed) return false;
  try {
    const r = await fetch(`/api/sessions/${up.conversationId}/audio-upload/${up.id}/finish`, { method: 'POST' });
    return r.ok;
  } catch (e) {
    return false;
  }
}

async function abortChunkUpload(up) {
  // Retried so the partial file is not later promoted to a duplicate transcription job.
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const r = await fetch(`/api/sessions/${up.conversationId}/audio-upload/${up.id}`, { method: 'DELETE' });
      if (r.ok || r.status === 404) return;
    } catch (e) { /* retry */ }
    await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
  }
}

function pickVoiceMimeType() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/ogg', 'audio/mp4'];
  for (const type of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return '';
}

function setMicStatus(text, { animated = false } = {}) {
  if (!text) {
    micStatusEl.style.display = 'none';
    micStatusEl.textContent = '';
    micStatusEl.innerHTML = '';
    return;
  }
  micStatusEl.style.display = 'block';
  if (animated) {
    // Moving-dots indicator so "Recording…"/"Uploading & transcribing…"
    // visibly show progress instead of sitting as static, easy-to-miss
    // text - mirrors the .live-spinner convention used for in-progress
    // turns/transcription status messages elsewhere in the UI.
    micStatusEl.innerHTML =
      `${escapeHtml(text)}<span class="mic-status-dots"><span>.</span><span>.</span><span>.</span></span>`;
  } else {
    micStatusEl.textContent = text;
  }
}

function showVoiceSetupBanner(problems) {
  let banner = document.getElementById('voice-setup-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'voice-setup-banner';
    banner.className = 'voice-setup-banner';
    micBtn.closest('.compose-wrap').appendChild(banner);
  }
  banner.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'voice-setup-head';
  head.textContent = '⚠️ Voice transcription is unavailable';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'voice-setup-close';
  close.title = 'Dismiss';
  close.textContent = '×';
  close.addEventListener('click', () => banner.remove());
  head.appendChild(close);
  banner.appendChild(head);
  for (const p of problems) {
    const row = document.createElement('div');
    row.className = 'voice-setup-row';
    const msg = document.createElement('div');
    msg.textContent = p.message;
    row.appendChild(msg);
    if (p.command) {
      const code = document.createElement('code');
      code.textContent = p.command;
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'voice-setup-copy';
      copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(p.command); copy.textContent = 'Copied'; }
        catch (e) { copy.textContent = 'Copy failed'; }
        setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
      });
      const line = document.createElement('div');
      line.className = 'voice-setup-cmd';
      line.append(code, copy);
      row.appendChild(line);
    }
    banner.appendChild(row);
  }
  const foot = document.createElement('div');
  foot.className = 'voice-setup-foot';
  foot.textContent = 'Then restart the bot and try again.';
  banner.appendChild(foot);
}

async function startVoiceRecording() {
  if (!currentConversationId) {
    setMicStatus('Start a session with a text message first, then use the mic.');
    setTimeout(() => setMicStatus(''), 3000);
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    setMicStatus('Voice recording is not supported in this browser.');
    setTimeout(() => setMicStatus(''), 3000);
    return;
  }
  try {
    const vs = await (await fetch('/api/voice/status')).json();
    if (vs && vs.ok === false) {
      showVoiceSetupBanner(vs.problems || []);
      return;
    }
  } catch (e) { /* status check is best-effort */ }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    console.error('getUserMedia failed', e);
    setMicStatus('Microphone permission denied or unavailable.');
    setTimeout(() => setMicStatus(''), 3000);
    return;
  }
  const mimeType = pickVoiceMimeType();
  recordedChunks = [];
  try {
    mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  } catch (e) {
    console.error('MediaRecorder construction failed', e);
    setMicStatus('Could not start recording on this browser.');
    setTimeout(() => setMicStatus(''), 3000);
    stream.getTracks().forEach((t) => t.stop());
    return;
  }
  chunkUpload = await startChunkUpload(currentConversationId, mimeType);
  const activeUpload = chunkUpload;
  mediaRecorder.addEventListener('dataavailable', (e) => {
    if (e.data && e.data.size > 0) {
      recordedChunks.push(e.data);
      if (activeUpload) queueChunk(activeUpload, e.data);
    }
  });
  mediaRecorder.addEventListener('stop', async () => {
    stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || mimeType || 'audio/webm' });
    recordedChunks = [];
    mediaRecorder = null;
    micBtn.classList.remove('mic-btn-recording');
    if (blob.size === 0) {
      setMicStatus('');
      return;
    }
    chunkUpload = null;
    if (activeUpload) {
      setMicStatus('⬆️ Uploading & transcribing', { animated: true });
      if (await finishChunkUpload(activeUpload)) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
        setMicStatus('');
        return;
      }
      await abortChunkUpload(activeUpload);
    }
    await uploadVoiceMessage(blob);
  });
  if (activeUpload) mediaRecorder.start(VOICE_TIMESLICE_MS); else mediaRecorder.start();
  micBtn.classList.add('mic-btn-recording');
  setMicStatus('🔴 Recording… click the mic again to stop and send', { animated: true });
}

function stopVoiceRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
}

async function uploadVoiceMessage(blob) {
  const conversationId = currentConversationId;
  setMicStatus('⬆️ Uploading & transcribing', { animated: true });
  // Jump to the bottom like a normal send - the "Transcribing..." status
  // placeholder is about to arrive over SSE at the end of the transcript.
  messagesEl.scrollTop = messagesEl.scrollHeight;
  const ext = (blob.type.split('/')[1] || 'webm').split(';')[0];
  const formData = new FormData();
  formData.append('audio', blob, `voice-message.${ext}`);
  try {
    const res = await fetch(`/api/sessions/${conversationId}/transcribe`, {
      method: 'POST',
      body: formData,
    });
    if (res.status === 401) {
      window.location.href = '/login';
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    setMicStatus('');
  } catch (e) {
    console.error('voice upload failed', e);
    setMicStatus(`Voice upload failed: ${e.message}`);
    setTimeout(() => setMicStatus(''), 4000);
  }
}

micBtn.addEventListener('click', () => {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    stopVoiceRecording();
  } else {
    startVoiceRecording();
  }
});

composeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = composeText.value.trim();
  if (!text) return;
  const sentForConversationId = currentConversationId;
  composeText.value = '';
  autoGrowComposeText();
  setDraft(sentForConversationId, '');
  await sendMessageText(text);
});

composeText.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    composeForm.requestSubmit();
  } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    // Explicit Cmd/Ctrl+Enter send, even if Shift is also held (e.g. after
    // typing a multi-line message where plain Enter would insert a
    // newline instead of submitting).
    e.preventDefault();
    composeForm.requestSubmit();
  }
});

composeText.addEventListener('input', () => {
  setDraft(currentConversationId, composeText.value);
  autoGrowComposeText();
});

// Auto-growing compose textarea - grows with multi-line input (up to a CSS
// max-height, then scrolls). Must also be re-run whenever composeText.value
// is set programmatically (draft restore, clear after send, ...), since
// setting .value doesn't fire 'input'.
function autoGrowComposeText() {
  composeText.style.height = 'auto';
  composeText.style.height = `${composeText.scrollHeight}px`;
}

// Warn before closing/reloading the tab while there is unsent
// draft text in the compose box. Drafts are persisted to localStorage (see
// setDraft/getDraft), so this guards against abandoning an unfinished
// message rather than data loss.
window.addEventListener('beforeunload', (e) => {
  if (!composeText.value.trim()) return;
  e.preventDefault();
  // Chrome/Firefox ignore the custom string and show their own generic
  // wording, but setting returnValue is still required to trigger the
  // native prompt at all.
  e.returnValue = '';
});

stopTurnBtn.addEventListener('click', async () => {
  if (!currentConversationId) return;
  stopTurnBtn.disabled = true;
  try {
    await api(`/api/sessions/${encodeURIComponent(currentConversationId)}/stop`, {
      method: 'POST',
    });
  } catch (e) {
    console.error('stop turn failed', e);
  } finally {
    stopTurnBtn.disabled = false;
  }
});

continueSessionBtn.addEventListener('click', async () => {
  if (!currentConversationId) return;
  closeThreadOverflowMenu();
  continueSessionBtn.disabled = true;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(currentConversationId)}/continue`, {
      method: 'POST',
    });
    await refreshSessionList();
    openSession(data.conversation_id);
  } catch (e) {
    console.error('continue in new session failed', e);
  } finally {
    continueSessionBtn.disabled = false;
  }
});

// Resets the app into the "new session" (no conversationId yet) state.
// Shared by the button's click handler and the "#new" deep-link hash
// (see parseSessionHash()) so both do the same reset.
function startNewSession() {
  // Only persist the visible compose text into the draft cache when we're
  // actually switching away FROM a real session - if currentConversationId
  // is already null (a fresh page load, or a "#new" deep link re-applied
  // while already in the new-session view), composeText.value may be stale
  // relative to a cache update that just happened moments ago (e.g.
  // applyDraftPrefill() merging in link-supplied context text right before
  // this call) and blindly writing it back here would clobber that merge.
  if (currentConversationId) {
    setDraft(currentConversationId, composeText.value);
  }
  currentConversationId = null;
  saveLastSession(null);
  lastTurns = [];
  lastRunning = false;
  queuedBadgeEl.style.display = 'none';
  stopTurnBtn.style.display = 'none';
  stopTurnElapsedTicker();
  continueSessionBtn.style.display = 'none';
  if (conversationStream) {
    conversationStream.close();
    conversationStream = null;
  }
  emptyStateEl.style.display = 'none';
  hideGroupOverview();
  if (window.Inbox) window.Inbox.leave();
  threadViewEl.style.display = 'flex';
  document.getElementById('group-banner').style.display = 'none';
  threadTitleEl.textContent = '(new session)';
  threadTitleEl.title = '(new session)';
  threadMetaEl.textContent = '';
  threadMetaEl.title = '';
  messagesEl.innerHTML = '';
  renderSessionList();
  composeText.value = getDraft(null);
  autoGrowComposeText();
  composeText.focus();
}

newSessionBtn.addEventListener('click', startNewSession);

const sessionFilterSelect = document.getElementById('session-filter-select');
if (![...sessionFilterSelect.options].some((o) => o.value === activeSessionFilter)) activeSessionFilter = 'all';
sessionFilterSelect.value = activeSessionFilter;
sessionFilterSelect.addEventListener('change', () => {
  activeSessionFilter = sessionFilterSelect.value;
  try { localStorage.setItem(SESSION_FILTER_STORAGE_KEY, activeSessionFilter); } catch (_) {}
  renderSessionList();
});

// --- Global / per-thread watchers view ---------------------------------
// Lets you check every registered watch (integration/time/
// self_restart - see watchers.py / the watch-manage skill) across all
// Slack threads, or scoped to just the thread of the session currently
// open (via the 👁 button in the thread header), without leaving the Web
// UI. Supports inline pause/resume/remove/
// recheck row actions (see api_watch_pause/resume/delete/recheck in
// web_server.py) - grouped by type, searchable, filterable
// to broken-only, auto-refreshing while open, with live title/link
// previews resolved per row via GET /api/watches/{id}/preview.

const watchesBtn = document.getElementById('health-watches');
const watchesCountEl = document.getElementById('health-watches-count');
const threadWatchesBtn = document.getElementById('thread-watches-btn');
const threadWatchesDotEl = document.getElementById('thread-watches-dot');
const threadWatchesCountEl = document.getElementById('thread-watches-count');
const watchesOverlay = document.getElementById('watches-overlay');
const watchesListEl = document.getElementById('watches-list');
const watchesSummaryEl = document.getElementById('watches-summary');
const watchesLegendEl = document.getElementById('watches-legend');
const watchesSearchEl = document.getElementById('watches-search');
const watchesBrokenOnlyEl = document.getElementById('watches-broken-only');
const watchesCloseBtn = document.getElementById('watches-close-btn');
const watchesIncludeInactiveEl = document.getElementById('watches-include-inactive');
const watchesPanelTitleEl = document.getElementById('watches-panel-title');
const watchesScopeToggleWrapEl = document.getElementById('watches-scope-toggle-wrap');
const watchesShowAllThreadsEl = document.getElementById('watches-show-all-threads');

const WATCH_TYPE_EMOJI = {
  ticket: '🎫', self_restart: '🔁', time: '⏰',
};
const WATCH_TYPE_LABEL = {
  ticket: 'Tickets',
  self_restart: 'Self-restart', time: 'Time',
};
// Fixed display order for type groups - alphabetical-by-emoji would be
// meaningless, so this is a deliberate, stable ordering instead.
const WATCH_TYPE_ORDER = ['ticket', 'time', 'self_restart'];
// Registry-provided watch types (incl. ones added via YAML) override/extend the built-in fallbacks.
Furls.ready.then(() => {
  for (const { type, emoji } of Furls.watchTypes()) {
    if (emoji) WATCH_TYPE_EMOJI[type] = emoji;
    if (!WATCH_TYPE_LABEL[type]) WATCH_TYPE_LABEL[type] = type.charAt(0).toUpperCase() + type.slice(1);
    if (!WATCH_TYPE_ORDER.includes(type)) WATCH_TYPE_ORDER.splice(WATCH_TYPE_ORDER.indexOf('ticket') + 1, 0, type);
  }
  // Script watches created from a package-driver recipe (meta.watcher) get their own group/icon: key "script:<id>".
  for (const { id, emoji, title } of Furls.watcherRecipes()) {
    const k = 'script:' + id;
    if (emoji) WATCH_TYPE_EMOJI[k] = emoji;
    WATCH_TYPE_LABEL[k] = title || id;
    if (!WATCH_TYPE_ORDER.includes(k)) WATCH_TYPE_ORDER.splice(WATCH_TYPE_ORDER.indexOf('ticket') + 1, 0, k);
  }
});
// Group key of a watch: its type, or "script:<recipe>" for package-driver watcher recipes.
function _watchGroupKey(w) {
  if (w.type === 'script') {
    const m = _parseWatchMeta(w);
    if (m && m.watcher) return 'script:' + m.watcher;
  }
  return w.type;
}
const WATCH_STATUS_LABEL = { active: 'active', paused: 'paused', done: 'done', broken: 'broken' };

// null = global (all conversations); a conversation_id string = scoped to
// one conversation. Every conversation (Slack-bound or Web-UI-only) has a
// stable conversation_id, so any session can have watches.
// Set each time the overlay is opened, from whichever button triggered it.
let watchesScopeConversationId = null;
// Raw list from the last successful /api/watches fetch - filtering
// (search/broken-only) re-renders from this without a re-fetch.
let watchesLastLoaded = [];
// Collapsed-group state persists across panel opens/reloads (localStorage),
// keyed by type - group headers are per-type, not per-panel-instance.
const WATCHES_COLLAPSED_KEY = 'watchesCollapsedGroups';
function _loadCollapsedGroups() {
  try { return new Set(JSON.parse(localStorage.getItem(WATCHES_COLLAPSED_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function _saveCollapsedGroups(set) {
  try { localStorage.setItem(WATCHES_COLLAPSED_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}
let watchesCollapsedGroups = _loadCollapsedGroups();

function _parseWatchMeta(w) {
  try { return JSON.parse(w.meta || '{}'); } catch (e) { return {}; }
}

// `silent` = background auto-refresh (30s health poll): no skeleton flash,
// no re-render at all when nothing changed, and scroll position preserved
// when it did - otherwise the panel jumped to the top on every refresh.
async function loadWatches(silent = false) {
  if (silent !== true) {
    watchesListEl.innerHTML = `
      <div class="watches-skeleton">
        ${'<div class="watch-item-skeleton"></div>'.repeat(5)}
      </div>`;
    watchesSummaryEl.textContent = '';
  }
  try {
    const includeInactive = watchesIncludeInactiveEl.checked;
    const scoped = watchesScopeConversationId && !watchesShowAllThreadsEl.checked;
    const params = new URLSearchParams({ include_inactive: String(includeInactive) });
    if (scoped) params.set('conversation_id', watchesScopeConversationId);
    const data = await api(`/api/watches?${params.toString()}`);
    const fresh = data.watches || [];
    if (silent === true && JSON.stringify(fresh) === JSON.stringify(watchesLastLoaded)) return;
    watchesLastLoaded = fresh;
    renderWatchesLegend();
    const scrollEl = watchesListEl.closest('.watches-panel') || watchesListEl;
    const tops = [watchesListEl.scrollTop, scrollEl.scrollTop];
    renderWatchesSummary(watchesLastLoaded);
    renderWatches(applyWatchesFilters(watchesLastLoaded));
    if (silent === true) {
      watchesListEl.scrollTop = tops[0];
      scrollEl.scrollTop = tops[1];
    }
  } catch (e) {
    if (silent === true) { console.error('watches silent refresh failed', e); return; }
    console.error('loading watches failed', e);
    watchesListEl.innerHTML = `
      <div class="watches-empty">
        Failed to load watches.
        <button type="button" class="watches-retry-btn" id="watches-retry-btn">Retry</button>
      </div>`;
    document.getElementById('watches-retry-btn')?.addEventListener('click', () => loadWatches());
  }
}

function applyWatchesFilters(watches) {
  let result = watches;
  if (watchesBrokenOnlyEl.checked) {
    result = result.filter((w) => w.status === 'broken');
  }
  const q = (watchesSearchEl.value || '').trim().toLowerCase();
  if (q) {
    result = result.filter((w) =>
      (w.label || '').toLowerCase().includes(q) || (w.key || '').toLowerCase().includes(q));
  }
  return result;
}

function renderWatchesSummary(watches) {
  if (!watches.length) {
    watchesSummaryEl.textContent = '';
    return;
  }
  const counts = { active: 0, paused: 0, done: 0, broken: 0 };
  for (const w of watches) counts[w.status] = (counts[w.status] || 0) + 1;
  const shown = applyWatchesFilters(watches).length;
  const filtered = shown !== watches.length;
  const parts = [
    `<span class="watches-summary-chip watches-summary-chip-active">${counts.active} active</span>`,
    counts.broken ? `<span class="watches-summary-chip watches-summary-chip-broken">${counts.broken} broken</span>` : '',
    counts.paused ? `<span class="watches-summary-chip watches-summary-chip-paused">${counts.paused} paused</span>` : '',
    counts.done ? `<span class="watches-summary-chip watches-summary-chip-done">${counts.done} done</span>` : '',
  ].filter(Boolean).join('');
  watchesSummaryEl.innerHTML = parts + (filtered ? `<span class="watches-summary-filtered">Showing ${shown} of ${watches.length}</span>` : '');
}

function renderWatchesLegend() {
  watchesLegendEl.innerHTML = `
    <div class="watches-legend-statuses">
      <span class="watches-legend-item"><span class="watch-status-dot watch-status-dot-active"></span> active</span>
      <span class="watches-legend-item"><span class="watch-status-dot watch-status-dot-paused"></span> paused</span>
      <span class="watches-legend-item"><span class="watch-status-dot watch-status-dot-broken"></span> broken</span>
      <span class="watches-legend-item"><span class="watch-status-dot watch-status-dot-done"></span> done</span>
    </div>`;
}

function watchLinkHtml(w) {
  const url = watchLinkUrl(w);
  if (!url) return '';
  const text = (w.key || '').replace(/^.*\//, '');
  return `<div class="watch-preview"><a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="watch-preview-link">🔗 ${escapeHtml(text)}</a></div>`;
}

function _watchRowHtml(w) {
  const emoji = WATCH_TYPE_EMOJI[_watchGroupKey(w)] || WATCH_TYPE_EMOJI[w.type] || '👁';
  const rawLabel = w.label || w.key || `watch #${w.id}`;
  const label = escapeHtml(rawLabel);
  const key = escapeHtml(w.key || '');
  const meta = _parseWatchMeta(w);
  const added = timeAgo(w.created_at) || 'n/a';
  const lastChecked = timeAgo(w.updated_at) || 'n/a';
  const sessionHref = w.conversation_id ? sessionLinkUrlFor(w.conversation_id) : null;
  const sessionLinkHtml = sessionHref
    ? `<a href="${sessionHref}" class="watch-session-link" title="Open this watch's session">${w.channel && w.thread_ts ? 'Slack thread' : 'Web UI session'} ↗</a>`
    : 'unknown session';

  let brokenHtml = '';
  if (w.status === 'broken' && meta.broken_reason) {
    brokenHtml = `<div class="watch-broken-reason" title="${escapeHtml(meta.broken_reason)}">⚠️ ${escapeHtml(meta.broken_reason)}</div>`;
  }
  let nextFireHtml = '';
  if (w.type === 'time' && w.status === 'active' && meta.next_fire_at) {
    const recurring = meta.recurring ? ' (recurring)' : ' (one-shot)';
    nextFireHtml = `<div class="watch-next-fire">⏱ Next fire: ${escapeHtml(meta.next_fire_at)}${recurring}</div>`;
  }

  // For `time` watches, `label` IS the literal prompt text fired into the
  // thread (not just a human nickname like every other watch type — see
  // watchers.py's _poll_time_watch) - a bare title="..." tooltip is a poor
  // way to read a multi-sentence prompt (tooltips don't wrap reliably,
  // truncate again in some browsers, and aren't tap-friendly on mobile).
  // Any long label (not just time watches) gets the same expand control,
  // since a truncated MR/ticket label can be just as hard to read in
  // full via tooltip alone.
  const needsExpand = rawLabel.length > 60;
  const expandBtnHtml = needsExpand
    ? `<button type="button" class="watch-label-expand-btn" data-action="toggle-label" title="Show full text">⤢</button>`
    : '';
  const fullTextHtml = needsExpand
    ? `<div class="watch-full-text" data-full-text-for="${w.id}" style="display:none">${label}</div>`
    : '';

  const canRecheck = w.status === 'active' && w.type !== 'self_restart';
  const actionsHtml = `
    <div class="watch-actions">
      ${canRecheck ? `<button type="button" class="watch-action-btn" data-action="recheck" title="Re-check now">⟳</button>` : ''}
      ${w.status === 'active' ? `<button type="button" class="watch-action-btn" data-action="pause" title="Pause">⏸</button>` : ''}
      ${w.status === 'paused' ? `<button type="button" class="watch-action-btn" data-action="resume" title="Resume">▶</button>` : ''}
      <button type="button" class="watch-action-btn watch-action-btn-danger" data-action="remove" title="Remove">🗑</button>
    </div>`;

  return `
    <div class="watch-item watch-status-${w.status} watch-type-${w.type}" data-watch-id="${w.id}" data-watch-type="${w.type}" data-watch-key="${key}">
      <div class="watch-item-top">
        <span class="watch-type-emoji">${emoji}</span>
        <span class="watch-label" title="${label}">${label}</span>
        ${expandBtnHtml}
        <span class="watch-status-badge">${escapeHtml(WATCH_STATUS_LABEL[w.status] || w.status)}</span>
        ${actionsHtml}
      </div>
      <div class="watch-meta">
        #${w.id}${key ? ` · <code class="watch-key">${key}</code>` : ''} · ${sessionLinkHtml}
      </div>
      <div class="watch-meta watch-meta-secondary">
        added ${added} · last checked ${lastChecked}
      </div>
      ${fullTextHtml}
      ${watchLinkHtml(w)}
      ${brokenHtml}
      ${nextFireHtml}
    </div>`;
}

function renderWatches(watches) {
  if (!watches.length) {
    const scoped = watchesScopeConversationId && !watchesShowAllThreadsEl.checked;
    const filtering = watchesBrokenOnlyEl.checked || watchesSearchEl.value.trim();
    watchesListEl.innerHTML = `<div class="watches-empty">${
      filtering
        ? 'No watches match the current filter.'
        : `No watches registered${scoped ? ' for this session' : ''}.`
    }</div>`;
    return;
  }

  // Group by type (fixed order), each group collapsible/persisted.
  const byType = new Map();
  for (const w of watches) {
    const gk = _watchGroupKey(w);
    if (!byType.has(gk)) byType.set(gk, []);
    byType.get(gk).push(w);
  }
  const orderedTypes = WATCH_TYPE_ORDER.filter((t) => byType.has(t))
    .concat([...byType.keys()].filter((t) => !WATCH_TYPE_ORDER.includes(t)));

  watchesListEl.innerHTML = '';
  for (const type of orderedTypes) {
    const group = byType.get(type);
    const collapsed = watchesCollapsedGroups.has(type);
    const groupEl = document.createElement('div');
    groupEl.className = 'watch-group';
    groupEl.innerHTML = `
      <div class="watch-group-header" data-group-type="${type}">
        <span class="watch-group-toggle">${collapsed ? '▸' : '▾'}</span>
        <span>${WATCH_TYPE_EMOJI[type] || '👁'} ${WATCH_TYPE_LABEL[type] || type}</span>
        <span class="watch-group-count">${group.length}</span>
      </div>
      <div class="watch-group-body" style="${collapsed ? 'display:none' : ''}">
        ${group.map(_watchRowHtml).join('')}
      </div>`;
    watchesListEl.appendChild(groupEl);
  }

  document.querySelectorAll('.unfurl-card').forEach((el) => el.remove());
  wireWatchLinks();
}

// Pure string build from the watch key - no network. The hover card is the
// existing lazy unfurl (wireLazyUnfurl), fetched only on first hover.
function watchLinkUrl(w) {
  return Furls.watchLinkUrl(w.type, w.key);
}

function wireWatchLinks() {
  watchesListEl.querySelectorAll('a.watch-preview-link').forEach((a) => {
    if (a.dataset.unfurlWired) return;
    a.dataset.unfurlWired = '1';
    wireLazyUnfurl(a);
  });
}

async function performWatchAction(watchId, action) {
  if (action === 'remove' && !confirm('Remove this watch permanently?')) return;
  const methodByAction = { pause: 'POST', resume: 'POST', recheck: 'POST', remove: 'DELETE' };
  const path = action === 'remove' ? `/api/watches/${watchId}` : `/api/watches/${watchId}/${action}`;
  try {
    await api(path, { method: methodByAction[action] });
    await loadWatches();
  } catch (e) {
    console.error(`watch action ${action} failed`, e);
    alert(`Failed to ${action} watch #${watchId}: ${e.message || e}`);
  }
}

function openWatchesOverlay(conversationId) {
  watchesScopeConversationId = conversationId || null;
  watchesShowAllThreadsEl.checked = false;
  watchesSearchEl.value = '';
  watchesBrokenOnlyEl.checked = false;
  watchesScopeToggleWrapEl.style.display = watchesScopeConversationId ? 'flex' : 'none';
  watchesPanelTitleEl.textContent = watchesScopeConversationId ? 'Watches — this session' : 'Watches';
  renderWatchesLegend();
  watchesOverlay.style.display = 'flex';
  loadWatches();
}

watchesBtn.addEventListener('click', (e) => {
  e.preventDefault();
  openWatchesOverlay(null);
});

threadWatchesBtn.addEventListener('click', () => {
  const session = currentSession();
  openWatchesOverlay(session ? session.conversation_id : null);
});

watchesCloseBtn.addEventListener('click', () => {
  watchesOverlay.style.display = 'none';
});

watchesOverlay.addEventListener('click', (e) => {
  if (e.target === watchesOverlay) watchesOverlay.style.display = 'none';
});

// Event delegation for group-collapse toggles and per-row action buttons -
// the list is fully re-rendered (innerHTML) on every load/filter, so
// listeners are attached once on the stable parent container rather than
// per-row/per-group each render.
watchesListEl.addEventListener('click', (e) => {
  const groupHeader = e.target.closest('.watch-group-header');
  if (groupHeader) {
    const type = groupHeader.dataset.groupType;
    const body = groupHeader.nextElementSibling;
    const toggleEl = groupHeader.querySelector('.watch-group-toggle');
    const nowCollapsed = body.style.display !== 'none';
    body.style.display = nowCollapsed ? 'none' : '';
    toggleEl.textContent = nowCollapsed ? '▸' : '▾';
    if (nowCollapsed) watchesCollapsedGroups.add(type); else watchesCollapsedGroups.delete(type);
    _saveCollapsedGroups(watchesCollapsedGroups);
    return;
  }
  const expandBtn = e.target.closest('.watch-label-expand-btn');
  if (expandBtn) {
    const row = expandBtn.closest('.watch-item');
    const fullTextEl = row.querySelector(`[data-full-text-for="${row.dataset.watchId}"]`);
    if (fullTextEl) {
      const nowShown = fullTextEl.style.display !== 'none';
      fullTextEl.style.display = nowShown ? 'none' : '';
      expandBtn.textContent = nowShown ? '⤢' : '⤡';
      expandBtn.title = nowShown ? 'Show full text' : 'Hide full text';
    }
    return;
  }
  const actionBtn = e.target.closest('.watch-action-btn');
  if (actionBtn) {
    const row = actionBtn.closest('.watch-item');
    performWatchAction(row.dataset.watchId, actionBtn.dataset.action);
  }
});

watchesIncludeInactiveEl.addEventListener('change', () => loadWatches());
watchesShowAllThreadsEl.addEventListener('change', () => {
  watchesPanelTitleEl.textContent = (watchesScopeConversationId && !watchesShowAllThreadsEl.checked)
    ? 'Watches — this session' : 'Watches';
  loadWatches();
});
watchesSearchEl.addEventListener('input', () => {
  clearTimeout(watchesSearchEl._debounce);
  watchesSearchEl._debounce = setTimeout(() => {
    renderWatchesSummary(watchesLastLoaded);
    renderWatches(applyWatchesFilters(watchesLastLoaded));
  }, 150);
});
watchesBrokenOnlyEl.addEventListener('change', () => {
  renderWatchesSummary(watchesLastLoaded);
  renderWatches(applyWatchesFilters(watchesLastLoaded));
});

// Keyboard-shortcut cheat-sheet overlay, opened with the '?' key (no
// dedicated button); lists the j/k, /, Enter/Shift+Enter/Cmd+Enter shortcuts.
const shortcutsOverlay = document.getElementById('shortcuts-overlay');
const shortcutsCloseBtn = document.getElementById('shortcuts-close-btn');

function openShortcutsOverlay() {
  shortcutsOverlay.style.display = 'flex';
}

function closeShortcutsOverlay() {
  shortcutsOverlay.style.display = 'none';
}

shortcutsCloseBtn.addEventListener('click', closeShortcutsOverlay);
shortcutsOverlay.addEventListener('click', (e) => {
  if (e.target === shortcutsOverlay) closeShortcutsOverlay();
});

// --- Settings screen (P4) ---------------------------------------------
// First Settings feature: a quiet-hours toggle that suppresses the
// turn-finished browser notification (see notifyTurnFinished() above)
// during a configured local time-of-day window. Settings are stored in
// localStorage (like drafts/read-state) rather than the backend, since
// they're purely a per-browser UI preference, not something that needs to
// sync across devices or survive a bot restart on the server side.
const SETTINGS_STORAGE_KEY = 'dop_agent_settings_v1';
const DEFAULT_SETTINGS = {
  quietHoursEnabled: false,
  quietHoursStart: '22:00',
  quietHoursEnd: '08:00',
  recentHours: 8,
};

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_STORAGE_KEY);
    if (!raw) return { ...DEFAULT_SETTINGS };
    return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
  } catch (e) {
    console.error('loading settings failed', e);
    return { ...DEFAULT_SETTINGS };
  }
}

function saveSettings(settings) {
  try {
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(settings));
  } catch (e) {
    console.error('saving settings failed', e);
  }
}

let settings = loadSettings();

// Returns true if the current local time falls inside the configured
// quiet-hours window. Supports a window that wraps past midnight (e.g.
// 22:00 -> 08:00) as well as a same-day window (e.g. 09:00 -> 17:00).
function isWithinQuietHours() {
  if (!settings.quietHoursEnabled) return false;
  const [startH, startM] = (settings.quietHoursStart || '00:00').split(':').map(Number);
  const [endH, endM] = (settings.quietHoursEnd || '00:00').split(':').map(Number);
  if ([startH, startM, endH, endM].some((n) => Number.isNaN(n))) return false;
  const now = new Date();
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  const startMinutes = startH * 60 + startM;
  const endMinutes = endH * 60 + endM;
  if (startMinutes === endMinutes) return false; // zero-width window - never active
  if (startMinutes < endMinutes) {
    return nowMinutes >= startMinutes && nowMinutes < endMinutes;
  }
  // Window wraps past midnight, e.g. 22:00 -> 08:00.
  return nowMinutes >= startMinutes || nowMinutes < endMinutes;
}

const settingsBtn = document.getElementById('settings-link');
const settingsOverlay = document.getElementById('settings-overlay');
const settingsCloseBtn = document.getElementById('settings-close-btn');
const settingsQuietHoursEnabled = document.getElementById('settings-quiet-hours-enabled');
const settingsQuietHoursStart = document.getElementById('settings-quiet-hours-start');
const settingsQuietHoursEnd = document.getElementById('settings-quiet-hours-end');

const settingsRecentHours = document.getElementById('settings-recent-hours');
function updateRecentChipTitle() {
  const chip = document.getElementById('recent-filter-chip');
  if (chip) chip.title = `Sessions with activity in the last ${recentHours()} hours`;
}
settingsRecentHours.addEventListener('change', () => {
  const raw = parseInt(settingsRecentHours.value, 10);
  if (!(raw >= 1)) { settingsRecentHours.value = recentHours(); return; }
  const n = Math.min(720, raw);
  settingsRecentHours.value = n;
  settings = { ...settings, recentHours: n };
  saveSettings(settings);
  updateRecentChipTitle();
  renderSessionList();
});
updateRecentChipTitle();

function refreshSettingsForm() {
  settingsRecentHours.value = settings.recentHours;
  settingsQuietHoursEnabled.checked = !!settings.quietHoursEnabled;
  settingsQuietHoursStart.value = settings.quietHoursStart;
  settingsQuietHoursEnd.value = settings.quietHoursEnd;
}

function openSettingsOverlay() {
  refreshSettingsForm();
  settingsOverlay.style.display = 'flex';
  if (window.DopSettings) window.DopSettings.onOpen();
}

function closeSettingsOverlay() {
  if (window.DopSettings && !window.DopSettings.canClose()) return;
  settingsOverlay.style.display = 'none';
  if (window.DopSettings) window.DopSettings.onClose();
}

settingsBtn.addEventListener('click', (e) => {
  e.preventDefault();
  openSettingsOverlay();
});
settingsCloseBtn.addEventListener('click', closeSettingsOverlay);
settingsOverlay.addEventListener('click', (e) => {
  if (e.target === settingsOverlay) closeSettingsOverlay();
});

settingsQuietHoursEnabled.addEventListener('change', () => {
  settings = { ...settings, quietHoursEnabled: settingsQuietHoursEnabled.checked };
  saveSettings(settings);
});
settingsQuietHoursStart.addEventListener('change', () => {
  settings = { ...settings, quietHoursStart: settingsQuietHoursStart.value || DEFAULT_SETTINGS.quietHoursStart };
  saveSettings(settings);
});
settingsQuietHoursEnd.addEventListener('change', () => {
  settings = { ...settings, quietHoursEnd: settingsQuietHoursEnd.value || DEFAULT_SETTINGS.quietHoursEnd };
  saveSettings(settings);
});

// --- Header health/status strip --------------------------------------------
// Periodically polls GET /api/health and reflects bot uptime, live Slack
// connectivity, configured MCP servers, and a short git-commit "version"
// string in the always-visible strip above the sidebar/thread panes.

const HEALTH_POLL_INTERVAL_MS = 30000;

const healthAdaptersEl = document.getElementById('health-adapters');
const adaptersStatusPopoverEl = document.getElementById('adapters-status-popover');
const healthMcpEl = document.getElementById('health-mcp');
const mcpStatusPopoverEl = document.getElementById('mcp-status-popover');
const healthLiveEl = document.getElementById('health-live');
const healthUptimeEl = document.getElementById('health-uptime');
const healthVersionEl = document.getElementById('health-version');

// Renders the per-server breakdown inside the MCP hover popover from the
// `mcp_status` array returned by /api/health (see
// bot_config.probe_mcp_server_statuses()) — one row per configured server
// (name/type/status/detail), plus this bot's own self-hosted /mcp server.
// Text content only (never innerHTML) since `name`/`detail` ultimately
// trace back to a locally-edited JSON config file, not untrusted input,
// but there's no reason to risk it.
function renderMcpStatusPopover(mcpStatus, el = mcpStatusPopoverEl, emptyText = 'No MCP servers configured') {
  el.replaceChildren();
  if (!Array.isArray(mcpStatus) || mcpStatus.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'mcp-status-empty';
    empty.textContent = emptyText;
    el.appendChild(empty);
    return;
  }
  for (const entry of mcpStatus) {
    const row = document.createElement('div');
    row.className = 'mcp-status-row';

    const dot = document.createElement('span');
    const status = ['ok', 'warning', 'error', 'unknown'].includes(entry.status) ? entry.status : 'unknown';
    dot.className = `mcp-status-dot mcp-status-${status}`;
    row.appendChild(dot);

    const body = document.createElement('div');
    body.className = 'mcp-status-body';

    const name = document.createElement('div');
    name.className = 'mcp-status-name';
    name.textContent = `${entry.name || 'unknown'} (${entry.type || 'local'})`;
    body.appendChild(name);

    if (entry.detail) {
      const detail = document.createElement('div');
      detail.className = 'mcp-status-detail';
      detail.textContent = entry.detail;
      body.appendChild(detail);
    }

    row.appendChild(body);
    el.appendChild(row);
  }
}

// Auto-reload once the running bot process's version changes underneath an
// already-open tab. Cache-busted /static/app.js URLs (see web_server.py's
// index_page()) only help new page loads; an open tab keeps running the JS
// it originally loaded. Comparing the git-commit `bot_version` from each
// health poll against the value seen at first load lets the tab detect a
// restart and reload itself within one poll interval.
let initialBotVersion = null;

function formatUptime(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const mins = Math.floor((s % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

async function refreshHealth() {
  try {
    const data = await api('/api/health');
    if (data.bot_version && data.bot_version !== 'unknown') {
      if (initialBotVersion === null) {
        initialBotVersion = data.bot_version;
      } else if (data.bot_version !== initialBotVersion) {
        console.info(`Bot restarted with a new version (${initialBotVersion} -> ${data.bot_version}) - reloading to pick up the latest UI`);
        location.reload();
        return;
      }
    }
    const adapterList = Array.isArray(data.adapters) ? data.adapters : [];
    const adaptersOk = adapterList.every(a => a.status === 'ok');
    healthAdaptersEl.classList.toggle('health-ok', adaptersOk);
    healthAdaptersEl.classList.toggle('health-bad', !adaptersOk);
    renderMcpStatusPopover(adapterList.map(a => ({ ...a, type: 'adapter' })), adaptersStatusPopoverEl, 'No adapters enabled');

    // Colour from the per-server statuses shown in the popover (which also
    // include the self-hosted /mcp server), not from mcp_configured, which
    // only counts externally-configured servers.
    const mcpList = Array.isArray(data.mcp_status) ? data.mcp_status : [];
    const mcpHealthy = mcpList.length > 0 ? !mcpList.some(e => e.status === 'error') : !!data.mcp_configured;
    healthMcpEl.classList.toggle('health-ok', mcpHealthy);
    healthMcpEl.classList.toggle('health-bad', !mcpHealthy);
    renderMcpStatusPopover(data.mcp_status);

    healthUptimeEl.textContent = `Uptime: ${formatUptime(data.uptime_seconds)}`;
    healthVersionEl.textContent = `v${data.bot_version}`;
    if (typeof data.session_timeout_seconds === 'number') {
      sessionTimeoutSeconds = data.session_timeout_seconds;
    }
    if (typeof data.workdir === 'string') {
      botWorkdir = data.workdir;
    }
  } catch (e) {
    console.error('refreshHealth failed', e);
  }
}

// Watchers health dot — reuses the same #health-watches element the
// "open watches overlay" click handler is already wired to (see
// `watchesBtn` above). Polls the same read-only /api/watches endpoint the
// overlay itself uses (global scope, active+paused+broken, i.e. everything
// except 'done') on the health-strip's cadence, and colors the dot red the
// moment any watch has flipped to 'broken' (see mark_broken() in
// watchers.py — a confirmed-permanent failure, not a transient poll
// error) so a silently-broken watch is visible at a glance instead of only
// discoverable by opening the panel.
async function refreshWatchesHealth() {
  try {
    const data = await api('/api/watches?include_inactive=false');
    const watches = data.watches || [];
    const brokenCount = watches.filter((w) => w.status === 'broken').length;
    watchesBtn.classList.toggle('health-ok', brokenCount === 0);
    watchesBtn.classList.toggle('health-bad', brokenCount > 0);
    watchesBtn.title = brokenCount > 0
      ? `Watchers: ${brokenCount} broken (${watches.length} total) — click to view`
      : `Watchers: ${watches.length} active — click to view`;
    watchesCountEl.textContent = watches.length ? String(watches.length) : '';
    watchesCountEl.classList.toggle('health-item-count-bad', brokenCount > 0);
    // Keep the open panel live by piggybacking on this same 30s poll
    // instead of a dedicated SSE channel.
    if (watchesOverlay.style.display !== 'none' && !watchesScopeConversationId) {
      loadWatches(true);
    }
  } catch (e) {
    console.error('refreshWatchesHealth failed', e);
  }
}

// Per-conversation counterpart of refreshWatchesHealth() above, for the 👁
// Watches chip inside the *thread header* itself (as opposed to the
// global health-strip item), so a broken watch tied to the open session is
// visible without clicking in. Scoped via GET /api/watches?conversation_id=...,
// same dedup rule as the global dot (any 'broken' status -> red). The dot
// stays hidden entirely (not gray) for sessions with zero registered watches.
async function refreshThreadWatchesHealth() {
  const session = currentSession();
  if (!session || !session.conversation_id) {
    threadWatchesDotEl.classList.remove('chip-dot-visible', 'chip-dot-ok', 'chip-dot-bad');
    threadWatchesBtn.title = 'Watches for this session';
    threadWatchesCountEl.textContent = '';
    return;
  }
  try {
    const params = new URLSearchParams({ include_inactive: 'false', conversation_id: session.conversation_id });
    const data = await api(`/api/watches?${params.toString()}`);
    const watches = data.watches || [];
    if (!watches.length) {
      threadWatchesDotEl.classList.remove('chip-dot-visible', 'chip-dot-ok', 'chip-dot-bad');
      threadWatchesBtn.title = 'Watches for this session — none registered';
      threadWatchesCountEl.textContent = '';
      return;
    }
    const brokenCount = watches.filter((w) => w.status === 'broken').length;
    threadWatchesDotEl.classList.add('chip-dot-visible');
    threadWatchesDotEl.classList.toggle('chip-dot-ok', brokenCount === 0);
    threadWatchesDotEl.classList.toggle('chip-dot-bad', brokenCount > 0);
    threadWatchesBtn.title = brokenCount > 0
      ? `Watches for this session: ${brokenCount} broken (${watches.length} total)`
      : `Watches for this session: ${watches.length} active`;
    threadWatchesCountEl.textContent = String(watches.length);
    threadWatchesCountEl.classList.toggle('header-chip-count-bad', brokenCount > 0);
    if (watchesOverlay.style.display !== 'none' && watchesScopeConversationId === session.conversation_id) {
      loadWatches(true);
    }
  } catch (e) {
    console.error('refreshThreadWatchesHealth failed', e);
  }
}

refreshHealth();
refreshWatchesHealth();
setInterval(refreshHealth, HEALTH_POLL_INTERVAL_MS);
setInterval(refreshWatchesHealth, HEALTH_POLL_INTERVAL_MS);
setInterval(refreshThreadWatchesHealth, HEALTH_POLL_INTERVAL_MS);

// Keep the thread header's "idle Xm" readout ticking even between sidebar
// SSE updates (which only arrive on actual session activity) - otherwise
// it would silently go stale while you're just reading an old transcript.
setInterval(() => {
  if (currentConversationId) updateThreadMeta(currentSession());
}, 30000);

// Resizable/collapsible sidebar with persisted size. Drag
// #sidebar-resize-handle to resize (clamped to the CSS min/max-width so
// it can't be dragged unusably narrow or absurdly wide); the collapse
// button (‹/›) hides the sidebar entirely without losing the width you
// had set, so re-expanding restores exactly where you left it. Both the
// width and collapsed state persist across reloads via localStorage.
const SIDEBAR_WIDTH_STORAGE_KEY = 'dop_agent_sidebar_width_v1';
const SIDEBAR_COLLAPSED_STORAGE_KEY = 'dop_agent_sidebar_collapsed_v1';
const SIDEBAR_MIN_WIDTH = 180;
const SIDEBAR_MAX_WIDTH = 600;

const sidebarEl = document.getElementById('sidebar');
const sidebarResizeHandle = document.getElementById('sidebar-resize-handle');
const sidebarCollapseBtn = document.getElementById('sidebar-collapse-btn');

function loadSidebarWidth() {
  const stored = parseInt(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY) || '', 10);
  if (Number.isFinite(stored)) {
    return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, stored));
  }
  return 300;
}

function applySidebarWidth(width) {
  sidebarEl.style.width = `${width}px`;
}

function isSidebarCollapsed() {
  return localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === '1';
}

function setSidebarCollapsed(collapsed) {
  localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, collapsed ? '1' : '0');
  sidebarEl.classList.toggle('collapsed', collapsed);
  sidebarCollapseBtn.textContent = collapsed ? '›' : '‹';
  sidebarCollapseBtn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}

applySidebarWidth(loadSidebarWidth());
setSidebarCollapsed(isSidebarCollapsed());

sidebarCollapseBtn.addEventListener('click', () => {
  setSidebarCollapsed(!isSidebarCollapsed());
});

let sidebarResizing = false;
sidebarResizeHandle.addEventListener('mousedown', (e) => {
  if (isSidebarCollapsed()) return;
  e.preventDefault();
  sidebarResizing = true;
  sidebarResizeHandle.classList.add('dragging');
  sidebarEl.classList.add('resizing');
  document.body.style.userSelect = 'none';
});

document.addEventListener('mousemove', (e) => {
  if (!sidebarResizing) return;
  const width = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, e.clientX));
  applySidebarWidth(width);
});

document.addEventListener('mouseup', () => {
  if (!sidebarResizing) return;
  sidebarResizing = false;
  sidebarResizeHandle.classList.remove('dragging');
  sidebarEl.classList.remove('resizing');
  document.body.style.userSelect = '';
  const width = sidebarEl.getBoundingClientRect().width;
  localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(Math.round(width)));
});

// Deep links: a hash-only URL change on the same page fires only
// 'hashchange' and does not re-run this script. That covers pasting a
// deep link while the app tab is already open; a fresh tab/window load
// goes through init() below.
function applyDeepLinkFromHash() {
  if (applySettingsHash(window.location.hash)) return;
  const deepLink = parseDeepLinkHash();
  if (!deepLink) return;
  if (deepLink.isNew) {
    applyDraftPrefill(null, deepLink.draftText);
    startNewSession();
    if (deepLink.draftText) focusComposeTextAtEnd();
    return;
  }
  if (!sessions.some((s) => s.conversation_id === deepLink.conversationId)) return;
  pendingDeepLinkMessageId = deepLink.messageId;
  if (deepLink.conversationId === currentConversationId) {
    applyDraftPrefill(deepLink.conversationId, deepLink.draftText);
    if (deepLink.draftText) {
      composeText.value = getDraft(deepLink.conversationId);
      autoGrowComposeText();
      focusComposeTextAtEnd();
    }
    renderMessages(lastTurns, lastRunning);
  } else {
    applyDraftPrefill(deepLink.conversationId, deepLink.draftText);
    openSession(deepLink.conversationId);
    if (deepLink.draftText) focusComposeTextAtEnd();
  }
}

window.addEventListener('hashchange', applyDeepLinkFromHash);

// Delegated click handler for any link (anywhere in the document - bubble
// content, an unfurl card, wherever) that resolves to one of THIS bot's
// own sessions (#open=<id> / #msg=<id>:<msgId> on the current origin).
// Intercepts the click and navigates in-app via the hash instead of
// letting the browser follow it as a normal link (renderMessages() does not
// set target="_blank" on these links, so no new tab opens). Delegated at the
// document level so it covers both bubble content and the unfurl card's own
// link.
document.addEventListener('click', (e) => {
  const a = e.target.closest('a');
  if (!a) return;
  const href = a.getAttribute('href') || '';
  if (parseOwnSettingsLink(href)) {
    e.preventDefault();
    const h = new URL(href, window.location.href).hash;
    if (h === window.location.hash) applySettingsHash(h);
    else window.location.hash = h;
    return;
  }
  if (!parseOwnSessionLink(href)) return;
  e.preventDefault();
  const targetHash = new URL(href, window.location.href).hash;
  if (targetHash === window.location.hash) {
    // Already on this hash (e.g. clicking a link back to the currently
    // open session) - 'hashchange' won't fire on its own, so re-apply
    // directly instead of silently doing nothing.
    applyDeepLinkFromHash();
  } else {
    window.location.hash = targetHash;
  }
});

(async function init() {
  await refreshSessionList();
  connectSessionListStream();
  if (applySettingsHash(window.location.hash)) return;
  const deepLink = parseDeepLinkHash();
  if (deepLink && deepLink.isNew) {
    applyDraftPrefill(null, deepLink.draftText);
    startNewSession();
    if (deepLink.draftText) focusComposeTextAtEnd();
    return;
  }
  if (deepLink && sessions.some((s) => s.conversation_id === deepLink.conversationId)) {
    pendingDeepLinkMessageId = deepLink.messageId;
    applyDraftPrefill(deepLink.conversationId, deepLink.draftText);
    openSession(deepLink.conversationId);
    if (deepLink.draftText) focusComposeTextAtEnd();
    return;
  }
  const lastSessionId = loadLastSession();
  if (lastSessionId && sessions.some((s) => s.conversation_id === lastSessionId)) {
    openSession(lastSessionId);
  }
})();
