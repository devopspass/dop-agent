/*
 * Interactive "ideas" widget. An assistant message containing a fenced block
 *
 *   ```ideas title="20 ideas for X" id=slug
 *   - [1] Short title :: description | effort=S | prio=High
 *   ```
 *
 * is rendered as a triage widget (Card / Selected / All views). Decisions
 * (ok / nok / skip + optional comment) are written into the conversation's
 * compose draft as a feedback block. Convention: dop-agent/skills/ideas-format.
 *
 * Design notes:
 *  - renderMessages() rebuilds every bubble on each update, so all widget
 *    state lives in ideasState (persisted to localStorage, capped), never
 *    in the DOM. Focus/caret of the comment box is captured before the wipe
 *    (IdeasWidget.captureFocus) and restored after the rebuild.
 *  - Invalid / unclosed fences, or a turn that is still running, leave the
 *    plain code block untouched.
 *  - The feedback block in the draft starts with a header line
 *    `Feedback on "<title>" [<id>]` followed by consecutive `- [n] ...`
 *    lines. Only that range is replaced; other draft text is never touched.
 */
(function () {
  const STATE_KEY = 'dop_agent_ideas_v1';
  const MAX_STATES = 300;
  const FENCE_RE = /```ideas([^\n]*)\n([\s\S]*?)\n[ \t]*```/;
  const LABEL = { ok: 'ok', nok: 'nok', skip: 'skip' };
  const OUT = { ok: 'OK', nok: 'NOK' };
  const KEYS = { 1: 'ok', 2: 'nok', 3: 'skip' };

  let ideasState = {};
  try { ideasState = JSON.parse(localStorage.getItem(STATE_KEY) || '{}'); } catch (e) { ideasState = {}; }
  function saveState() {
    const keys = Object.keys(ideasState);
    if (keys.length > MAX_STATES) keys.slice(0, keys.length - MAX_STATES).forEach((k) => delete ideasState[k]);
    try { localStorage.setItem(STATE_KEY, JSON.stringify(ideasState)); } catch (e) { /* quota */ }
  }

  function parse(src) {
    const m = FENCE_RE.exec(src);
    if (!m) return null;
    const attrs = {};
    (m[1].match(/(\w+)=("[^"]*"|\S+)/g) || []).forEach((a) => {
      const i = a.indexOf('=');
      attrs[a.slice(0, i)] = a.slice(i + 1).replace(/^"|"$/g, '');
    });
    const items = [];
    const seen = new Set();
    for (const line of m[2].split('\n')) {
      if (!line.startsWith('- [')) continue;
      const x = /^- \[([^\]]+)\]\s+(.*?)(?:\s+::\s+(.*?))?((?:\s+\|\s+\w+=[^|]+)*)$/.exec(line);
      if (!x || seen.has(x[1])) return null;
      seen.add(x[1]);
      const meta = [];
      (x[4] || '').split('|').forEach((p) => {
        const q = p.trim().split('=');
        if (q[1]) meta.push(q.slice(1).join('=').trim());
      });
      items.push({ id: x[1], title: x[2], desc: x[3] || '', meta });
    }
    if (!items.length) return null;
    return { title: attrs.title || 'Ideas', id: attrs.id || '', items };
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  // Text with `inline code` spans, built with DOM nodes only (no innerHTML).
  function richText(parent, text) {
    text.split(/(`[^`]+`)/).forEach((part) => {
      if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) parent.appendChild(el('code', '', part.slice(1, -1)));
      else if (part) parent.appendChild(document.createTextNode(part));
    });
    return parent;
  }

  function headerLine(doc) { return `Feedback on "${doc.title}"${doc.id ? ` [${doc.id}]` : ''}`; }

  function feedbackBlock(doc, st) {
    const lines = doc.items
      .filter((i) => st.d[i.id] && (st.d[i.id].v === 'ok' || st.d[i.id].v === 'nok'))
      .map((i) => `- [${i.id}] ${OUT[st.d[i.id].v]} — ${i.title}${st.d[i.id].note ? `: ${st.d[i.id].note.replace(/\s*\n\s*/g, ' / ')}` : ''}`);
    return lines.length ? [headerLine(doc), ...lines].join('\n') : '';
  }

  // Replace this widget's block inside `draft` (header + following "- [" lines),
  // or append it; an empty block removes the old one.
  function mergeBlock(draft, header, block) {
    const lines = draft.split('\n');
    const idTag = /\[[^\]]+\]$/.exec(header);
    let at = lines.indexOf(header);
    if (at < 0 && idTag) at = lines.findIndex((l) => l.startsWith('Feedback on "') && l.endsWith(idTag[0]));
    if (at >= 0) {
      let end = at + 1;
      // bullets plus any continuation lines, up to a blank line
      while (end < lines.length && lines[end].trim() !== '') end++;
      lines.splice(at, end - at, ...(block ? block.split('\n') : []));
      return lines.join('\n').replace(/\n{3,}/g, '\n\n');
    }
    if (!block) return draft;
    return draft ? `${draft.replace(/\s+$/, '')}\n\n${block}\n\n` : `${block}\n\n`;
  }

  function syncDraft(doc, st) {
    const conv = st.conv;
    const active = typeof currentConversationId !== 'undefined' && currentConversationId === conv;
    const cur = active ? composeText.value : getDraft(conv);
    const next = mergeBlock(cur, headerLine(doc), feedbackBlock(doc, st));
    if (next === cur) return;
    setDraft(conv, next);
    if (active) {
      composeText.value = next;
      if (typeof autoGrowComposeText === 'function') autoGrowComposeText();
    }
  }

  let hoverKey = null;
  let focusKey = null;
  let rootFocusKey = null;
  const autoFocused = new Set();
  let lastAutoFocus = null; // {key, at}
  let focusInfo = null;
  const widgets = {};

  function build(key, doc, st) {
    const root = el('div', 'ideas-widget');
    root.dataset.ideasKey = key;
    root.tabIndex = 0;
    root.addEventListener('mouseenter', () => { hoverKey = key; });
    root.addEventListener('mouseleave', () => { if (hoverKey === key) hoverKey = null; });
    root.addEventListener('focusin', () => { focusKey = key; });
    root.addEventListener('focusout', (e) => { if (!root.contains(e.relatedTarget) && focusKey === key) focusKey = null; });
    widgets[key] = { doc, st, root };
    render(key);
    return root;
  }

  function render(key) {
    const { doc, st, root } = widgets[key];
    root.textContent = '';
    const n = doc.items.filter((i) => st.d[i.id]).length;

    const head = el('div', 'ideas-head');
    head.appendChild(el('b', 'ideas-title', doc.title));
    const bar = el('div', 'ideas-bar');
    const fill = el('i');
    fill.style.width = `${(100 * n) / doc.items.length}%`;
    bar.appendChild(fill);
    head.appendChild(bar);
    head.appendChild(el('span', 'ideas-count', `${n}/${doc.items.length}`));
    const seg = el('div', 'ideas-seg');
    [['card', 'Card'], ['selected', 'Selected'], ['all', 'All']].forEach(([v, label]) => {
      const b = el('button', st.view === v ? 'on' : '', label);
      b.type = 'button';
      b.addEventListener('click', () => {
        st.view = v;
        if (v === 'card') {
          const first = doc.items.findIndex((i) => !st.d[i.id]);
          st.cur = first < 0 ? doc.items.length : first;
        }
        saveState(); render(key);
      });
      seg.appendChild(b);
    });
    head.appendChild(seg);
    root.appendChild(head);
    root.appendChild(st.view === 'card' ? cardView(key) : tableView(key));
    restoreFocus(key);
  }

  function decideButtons(key, idx, withKeys) {
    const { doc, st } = widgets[key];
    const it = doc.items[idx];
    const row = el('div', 'ideas-acts');
    ['ok', 'nok', 'skip'].forEach((a, i) => {
      const b = el('button', st.d[it.id] && st.d[it.id].v === a ? 'on' : '', LABEL[a]);
      b.type = 'button';
      if (withKeys) b.appendChild(el('kbd', '', String(i + 1)));
      b.addEventListener('click', () => decide(key, idx, a));
      row.appendChild(b);
    });
    return row;
  }

  function commentValue(key) {
    const t = widgets[key].root.querySelector('textarea.ideas-note, input.ideas-note');
    return t ? t.value.trim() : null;
  }

  function decide(key, idx, a) {
    const { doc, st } = widgets[key];
    const it = doc.items[idx];
    const typed = commentValue(key);
    const note = typed != null ? typed : (st.d[it.id] ? st.d[it.id].note : '');
    if (st.view !== 'card' && st.d[it.id] && st.d[it.id].v === a) delete st.d[it.id];
    else st.d[it.id] = { v: a, note };
    if (st.view === 'card') {
      st.cur = Math.min(idx + 1, doc.items.length);
      if (st.cur >= doc.items.length) st.view = 'selected';
    }
    saveState(); syncDraft(doc, st); render(key);
  }

  function setNote(key, idx, text) {
    const { doc, st } = widgets[key];
    const it = doc.items[idx];
    if (!st.d[it.id]) st.d[it.id] = { v: 'skip', note: '' };
    st.d[it.id].note = text.trim();
    saveState(); syncDraft(doc, st);
  }

  function noteBox(key, idx, multiline) {
    const { doc, st } = widgets[key];
    const it = doc.items[idx];
    const t = multiline ? el('textarea', 'ideas-note') : el('input', 'ideas-note');
    if (multiline) t.rows = 4; else t.type = 'text';
    t.placeholder = 'Comment or edit (optional)';
    t.value = st.d[it.id] ? st.d[it.id].note : '';
    t.dataset.idx = String(idx);
    t.addEventListener('input', () => setNote(key, idx, t.value));
    t.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') t.blur();
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && multiline) {
        const d = st.d[it.id];
        decide(key, idx, d && d.v !== 'skip' ? d.v : 'ok');
      }
    });
    return t;
  }

  function cardView(key) {
    const { doc, st } = widgets[key];
    const wrap = el('div', 'ideas-body');
    const it = doc.items[st.cur];
    if (!it) {
      wrap.appendChild(el('b', '', `All ${doc.items.length} reviewed`));
      wrap.appendChild(el('div', 'ideas-muted', 'Switch to Selected or All to review; the feedback is in the compose box.'));
      return wrap;
    }
    const meta = el('div', 'ideas-meta');
    meta.appendChild(el('span', '', `${st.cur + 1} / ${doc.items.length}`));
    it.meta.forEach((m) => meta.appendChild(el('b', '', m)));
    wrap.appendChild(meta);
    wrap.appendChild(el('h4', 'ideas-card-title', it.title));
    wrap.appendChild(richText(el('div', 'ideas-desc'), it.desc));
    wrap.appendChild(noteBox(key, st.cur, true));
    const acts = decideButtons(key, st.cur, true);
    const back = el('button', 'ghost', '← back');
    back.type = 'button';
    back.addEventListener('click', () => { st.cur = Math.max(0, st.cur - 1); saveState(); render(key); });
    acts.appendChild(el('span', 'sp'));
    acts.appendChild(back);
    wrap.appendChild(acts);
    return wrap;
  }

  function tableView(key) {
    const { doc, st } = widgets[key];
    const wrap = el('div', 'ideas-body ideas-table-wrap');
    const rows = doc.items.map((it, k) => [it, k]).filter(([it, k]) => {
      const d = st.d[it.id];
      return st.view === 'all' || (d && (d.v === 'ok' || d.note)) || st.open === k;
    });
    if (!rows.length) {
      wrap.appendChild(el('div', 'ideas-muted', 'Nothing selected yet — mark ideas as ok or add a comment.'));
      return wrap;
    }
    const table = el('div', 'ideas-table');
    rows.forEach(([it, k]) => {
      const d = st.d[it.id];
      const row = el('div', `ideas-row${d ? ` s-${d.v}` : ''}`);
      row.appendChild(el('span', 'n', it.id));
      const t = el('span', 't');
      t.appendChild(document.createTextNode(it.title));
      if (d && d.note && st.open !== k) t.appendChild(el('span', 'ideas-muted', ` — ${d.note}`));
      t.addEventListener('click', () => { st.open = st.open === k ? null : k; saveState(); render(key); const i = widgets[key].root.querySelector('input.ideas-note'); if (i) i.focus(); });
      row.appendChild(t);
      table.appendChild(row);
      if (st.open === k) {
        const x = el('div', 'ideas-row-x');
        x.appendChild(richText(el('div', 'ideas-muted'), it.desc));
        x.appendChild(decideButtons(key, k, false));
        x.appendChild(noteBox(key, k, false));
        table.appendChild(x);
      }
    });
    wrap.appendChild(table);
    return wrap;
  }

  function captureFocus() {
    focusInfo = null;
    rootFocusKey = focusKey;
    const a = document.activeElement;
    const root = a && a.closest && a.closest('.ideas-widget');
    if (root && a.classList.contains('ideas-note')) {
      focusInfo = { key: root.dataset.ideasKey, idx: a.dataset.idx, start: a.selectionStart, end: a.selectionEnd };
    }
  }
  function restoreFocus(key) {
    if (!focusInfo || focusInfo.key !== key) return;
    const t = widgets[key].root.querySelector(`.ideas-note[data-idx="${focusInfo.idx}"]`);
    if (t) { t.focus(); try { t.setSelectionRange(focusInfo.start, focusInfo.end); } catch (e) { /* ignore */ } }
    focusInfo = null;
  }

  // `fresh` = the newest, just-arrived assistant message: pull keyboard focus
  // into the card (once per widget) so 1/2/3 work without touching the mouse.
  function apply(bubble, msgId, turnText, conversationId, running, fresh) {
    if (running || !turnText || !FENCE_RE.test(turnText)) return;
    const code = bubble.querySelector('pre > code.language-ideas');
    if (!code) return;
    const doc = parse(turnText);
    if (!doc) return;
    const key = `${msgId}|${doc.id || doc.title}`;
    const st = ideasState[key] || (ideasState[key] = { d: {}, view: 'card', cur: 0, open: null });
    st.conv = conversationId;
    code.parentElement.replaceWith(build(key, doc, st));
    const w = widgets[key];
    restoreFocus(key);
    const noDecisions = !Object.keys(st.d).length;
    const m = document.getElementById('messages');
    const atBottom = !m || m.scrollHeight - m.scrollTop - m.clientHeight <= 120;
    const ae = document.activeElement;
    const stolen = lastAutoFocus && lastAutoFocus.key === key && Date.now() - lastAutoFocus.at < 4000
      && (!ae || ae === document.body || ae.id === 'compose-text') && !composeText.value.trim().length;
    if (fresh && noDecisions && atBottom && (!autoFocused.has(key) || stolen)) {
      autoFocused.add(key);
      if (!stolen) lastAutoFocus = { key, at: Date.now() };
      // bubble isn't attached to the document yet during renderMessages()
      setTimeout(() => { if (w.root.isConnected) w.root.focus({ preventScroll: true }); }, 0);
    } else if (rootFocusKey === key && !focusInfo) {
      setTimeout(() => { if (w.root.isConnected) w.root.focus({ preventScroll: true }); }, 0);
    }
    if (rootFocusKey === key) rootFocusKey = null;
    return w;
  }

  document.addEventListener('keydown', (e) => {
    const key = focusKey || hoverKey;
    if (!key || !widgets[key] || e.metaKey || e.ctrlKey || e.altKey) return;
    if (isEditableTarget(e.target)) return;
    const { doc, st } = widgets[key];
    if (e.key === 'Escape' && focusKey === key) {
      e.preventDefault(); composeText.focus(); return;
    }
    if (st.view !== 'card') return;
    if (KEYS[e.key] && doc.items[st.cur]) {
      e.preventDefault(); decide(key, st.cur, KEYS[e.key]);
    } else if (e.key === 'Tab' && !e.shiftKey && doc.items[st.cur]) {
      const t = widgets[key].root.querySelector('textarea.ideas-note');
      if (t) { e.preventDefault(); t.focus(); }
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault(); st.cur = Math.max(0, st.cur - 1); saveState(); render(key);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault(); st.cur = Math.min(doc.items.length, st.cur + 1); saveState(); render(key);
    }
  });

  // The compose box re-grabs focus when a send finishes; if a widget just took
  // focus (fast replies finish before that), give it back to the widget.
  function reclaimFocus() {
    if (!lastAutoFocus || Date.now() - lastAutoFocus.at > 5000) return false;
    const w = widgets[lastAutoFocus.key];
    if (!w || !w.root.isConnected || Object.keys(w.st.d).length) return false;
    w.root.focus({ preventScroll: true });
    return true;
  }

  // ---- inline mount (Messages inbox): same widget, state and draft sync as in the thread ----
  function stateFor(msgId, doc, conv) {
    const key = `${msgId}|${doc.id || doc.title}`;
    const st = ideasState[key] || (ideasState[key] = { d: {}, view: 'card', cur: 0, open: null });
    st.conv = conv;
    return { key, st };
  }
  const inlineApi = {
    mount(msgId, doc, conv) {
      const { key, st } = stateFor(msgId, doc, conv);
      return build(key, doc, st);
    },
    stats(msgId, doc, conv) {
      const { st } = stateFor(msgId, doc, conv);
      const answered = doc.items.filter((i) => st.d[i.id]).length;
      return { total: doc.items.length, answered, skipped: doc.items.filter((i) => st.d[i.id] && st.d[i.id].v === 'skip').length, done: answered };
    },
    block(msgId, doc, conv) { return feedbackBlock(doc, stateFor(msgId, doc, conv).st); },
    clear(msgId, doc, conv) {
      const { key } = stateFor(msgId, doc, conv);
      const active = typeof currentConversationId !== 'undefined' && currentConversationId === conv;
      const cur = active ? composeText.value : getDraft(conv);
      const next = mergeBlock(cur, headerLine(doc), '');
      if (next !== cur) { setDraft(conv, next); if (active) composeText.value = next; }
      delete ideasState[key];
      delete widgets[key];
      saveState();
    },
    focus(msgId, doc) { const w = widgets[`${msgId}|${doc.id || doc.title}`]; if (w) w.root.focus({ preventScroll: true }); },
  };

  window.IdeasWidget = { ...inlineApi, reclaimFocus, apply, captureFocus, parse, mergeBlock, headerLine, state: ideasState };
})();
