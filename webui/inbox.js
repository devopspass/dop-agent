/*
 * Messages inbox. A sidebar mode (Sessions | Inbox) that lists every session
 * whose LAST message is an assistant message with unanswered ```ask / ```ideas
 * blocks. Selecting a row shows that session as slides in the main pane: one
 * slide per block, rendered by the real AskWidget / IdeasWidget (inline mount,
 * same state + compose-draft sync as the in-thread widgets), so an answer given
 * here shows up in the session and vice versa.
 *
 * Data: GET /api/inbox returns candidates (server pre-filter only); the badge
 * and list are built client-side from what the widget parsers accept. "Send to
 * session" posts only the answer blocks via /api/send and strips them from the
 * draft; once the session has a newer message it drops out of the inbox on its
 * own. "Not needed" dismisses (bound to the message id, undoable). Opening a
 * session switches the sidebar back to Sessions (Inbox.leave(), called from
 * openSession()). Answers/drafts are per-browser (localStorage), like the
 * widgets themselves.
 */
(function () {
  const MODE_KEY = 'dopAgentSidebarMode';
  const REFRESH_MS = 1500;
  const IDLE_REFRESH_MS = 6000;
  const $ = (id) => document.getElementById(id);
  const listEl = $('inbox-list');
  const viewEl = $('inbox-view');
  const badgeEl = $('inbox-badge');
  const modesEl = $('sidebar-modes');

  let mode = 'sessions';
  let entries = [];
  let cur = null; // conversation_id
  let ucur = null; // highlighted row in the Unread section (no slides for those)
  let slide = 0;
  let filter = 'all';
  let showAll = false;
  let ctxOpen = false;
  let curW = null; // ask widget on the visible slide
  let timer = null;
  let lastFetch = 0;
  let loaded = false;
  let toastTimer = null;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const whyOf = (text) => {
    const body = text.replace(/```[\s\S]*?```/g, '\n');
    const line = body.split('\n').map((l) => l.trim()).find((l) => l && !/^\|?[-: |]+\|?$/.test(l)) || '';
    const plain = line.replace(/^#+\s*/, '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*_`>]/g, '').trim();
    return plain.length > 140 ? `${plain.slice(0, 139)}…` : plain;
  };

  function buildEntry(c) {
    const blocks = [];
    if (window.AskWidget) window.AskWidget.parse(c.text).forEach((d) => blocks.push({ kind: 'ask', doc: d }));
    if (window.IdeasWidget) { const d = window.IdeasWidget.parse(c.text); if (d) blocks.push({ kind: 'ideas', doc: d }); }
    const actions = window.ActionsWidget ? window.ActionsWidget.parse(c.text) : [];
    if (!blocks.length && !actions.length) return null;
    const q = blocks.reduce((n, b) => n + (b.kind === 'ask' ? b.doc.items.filter((i) => i.kind === 'question').length : 0), 0);
    const d = blocks.reduce((n, b) => n + (b.kind === 'ask' ? b.doc.items.filter((i) => i.kind === 'decision').length : 0), 0);
    const i = blocks.reduce((n, b) => n + (b.kind === 'ideas' ? 1 : 0), 0);
    return { conv: c.conversation_id, msgId: c.message_id, title: c.title || '(untitled)', at: c.message_at, text: c.text, blocks, actions, why: whyOf(c.text), counts: { q, d, i, a: actions.length } };
  }
  const stats = (e) => e.blocks.map((b) => window[b.kind === 'ask' ? 'AskWidget' : 'IdeasWidget'].stats(e.msgId, b.doc, e.conv));
  const answeredCount = (e) => stats(e).reduce((n, s) => n + s.answered, 0);
  const isReady = (e) => { const st = stats(e); return st.every((s) => s.done >= s.total) && st.some((s) => s.answered > 0); };
  const matches = (e) => filter === 'all' || (filter === 'questions' && e.counts.q) || (filter === 'decisions' && e.counts.d) || (filter === 'ideas' && e.counts.i) || (filter === 'replies' && e.counts.a);
  const visible = () => entries.filter(matches);
  // Unread sessions without pending asks: same notion/rules as the Sessions list (archived hidden), client-side.
  const unreadList = () => {
    if (filter !== 'all' || typeof sessions === 'undefined' || typeof isSessionUnread !== 'function') return [];
    const asks = new Set(entries.map((e) => e.conv));
    return sessions.filter((s) => !s.archived && !asks.has(s.conversation_id) && isSessionUnread(s))
      .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at));
  };
  const curEntry = () => entries.find((e) => e.conv === cur) || null;

  function toast(msg, undo) {
    let t = $('inbox-toast');
    if (!t) { t = el('div'); t.id = 'inbox-toast'; document.body.appendChild(t); }
    t.textContent = msg;
    if (undo) { const b = el('button', 'lnk', 'Undo'); b.type = 'button'; b.addEventListener('click', () => { t.classList.remove('on'); undo(); }); t.appendChild(b); }
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('on'), 5000);
  }

  // A reply sent from the normal compose box makes the inbox entry stale; drop it
  // immediately instead of waiting for the next session-list push.
  function dropConversation(conv) {
    const e = entries.find((x) => x.conv === conv);
    if (!e) return;
    if (mode === 'inbox') { removeLocal(e); return; }
    entries = entries.filter((x) => x.conv !== conv);
    badgeEl.textContent = String(entries.length);
    badgeEl.style.display = entries.length ? '' : 'none';
    refreshSoon();
  }

  // ---- data ----
  async function load() {
    lastFetch = Date.now();
    let data;
    try { data = await api('/api/inbox'); } catch (e) { return; }
    const prev = curEntry();
    entries = (data.items || []).map(buildEntry).filter(Boolean);
    loaded = true;
    badgeEl.textContent = String(entries.length);
    badgeEl.style.display = entries.length ? '' : 'none';
    if (mode !== 'inbox') return;
    const now = curEntry();
    if (!now) { const v = visible()[0]; cur = v ? v.conv : null; slide = 0; renderList(); renderView(); return; }
    renderList();
    if (!prev || prev.msgId !== now.msgId) { slide = 0; renderView(); }
  }
  function refreshSoon() {
    if (!window.AskWidget) return;
    clearTimeout(timer);
    const wait = Math.max(mode === 'inbox' ? REFRESH_MS : IDLE_REFRESH_MS, lastFetch + (mode === 'inbox' ? REFRESH_MS : IDLE_REFRESH_MS) - Date.now());
    timer = setTimeout(load, wait);
  }

  // ---- mode switching ----
  function setTabs() { modesEl.querySelectorAll('.sidebar-mode').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode)); }
  function enter() {
    if (mode === 'inbox') return;
    mode = 'inbox';
    try { localStorage.setItem(MODE_KEY, 'inbox'); } catch (e) { /* ignore */ }
    document.body.classList.add('inbox-mode');
    setTabs();
    $('empty-state').style.display = 'none';
    $('thread-view').style.display = 'none';
    $('group-overview').style.display = 'none';
    viewEl.style.display = 'block';
    if (!curEntry()) { const v = visible()[0]; cur = v ? v.conv : null; slide = 0; }
    renderList(); renderView();
    load();
  }
  function leave() {
    if (mode !== 'inbox') return;
    mode = 'sessions';
    try { localStorage.setItem(MODE_KEY, 'sessions'); } catch (e) { /* ignore */ }
    document.body.classList.remove('inbox-mode');
    setTabs();
    viewEl.style.display = 'none';
    curW = null;
    if (typeof currentConversationId !== 'undefined' && currentConversationId) $('thread-view').style.display = 'flex';
    else $('empty-state').style.display = '';
  }
  modesEl.addEventListener('click', (e) => {
    const b = e.target.closest('.sidebar-mode');
    if (!b) return;
    if (b.dataset.mode === 'inbox') { enter(); if (location.hash !== '#inbox') history.replaceState(null, '', '#inbox'); }
    else { if (mode === 'inbox') { leave(); if (location.hash === '#inbox') history.replaceState(null, '', location.pathname); } }
  });

  // ---- sidebar list ----
  function renderList() {
    listEl.textContent = '';
    const chips = el('div', 'session-filters inbox-filters');
    [['all', 'All'], ['questions', 'Questions'], ['decisions', 'Decisions'], ['ideas', 'Ideas'], ['replies', 'Replies']].forEach(([k, label]) => {
      const b = el('button', `filter-chip${filter === k ? ' active' : ''}`, label);
      b.type = 'button';
      b.addEventListener('click', () => { filter = k; const v = visible(); if (!v.find((x) => x.conv === cur)) { cur = v[0] ? v[0].conv : null; slide = 0; } renderList(); renderView(); });
      chips.appendChild(b);
    });
    listEl.appendChild(chips);
    const vis = visible();
    if (!vis.length) listEl.appendChild(el('div', 'inbox-empty-side', loaded ? 'Nothing waiting for you' : 'Loading…'));
    vis.forEach((e) => {
      const s = (typeof sessions !== 'undefined' ? sessions : []).find((x) => x.conversation_id === e.conv);
      const item = el('div', `session-item${e.conv === cur ? ' active' : ''}`);
      item.dataset.conversationId = e.conv;
      if (s && typeof applyGroupDecoration === 'function') applyGroupDecoration(item, s);
      const top = el('div', 'session-item-top');
      top.appendChild(el('div', 'session-title', markdownToPlainText(e.title)));
      const ready = isReady(e);
      const n = answeredCount(e);
      const bd = el('span', `inbox-count${ready ? ' ready' : ''}`, ready ? '✓' : String(e.counts.q + e.counts.d + e.counts.i));
      bd.title = ready ? 'All answered — ready to send' : `${n} answered`;
      top.appendChild(bd);
      item.appendChild(top);
      const parts = [e.counts.q && `Q${e.counts.q}`, e.counts.d && `D${e.counts.d}`, e.counts.i && 'ideas', !e.counts.q && !e.counts.d && !e.counts.i && e.counts.a && 'reply'].filter(Boolean).join(' ');
      item.appendChild(el('div', 'session-meta', `${timeAgo(e.at)} · ${parts}`));
      item.addEventListener('click', () => { cur = e.conv; ucur = null; slide = 0; showAll = false; ctxOpen = false; renderList(); renderView(); });
      listEl.appendChild(item);
    });
    const un = unreadList();
    if (un.length) {
      listEl.appendChild(el('div', 'inbox-unread-head', `Unread · ${un.length}`));
      un.forEach((s) => {
        const item = el('div', `session-item inbox-unread-item${s.conversation_id === ucur ? ' active' : ''}`);
        item.dataset.conversationId = s.conversation_id;
        if (typeof applyGroupDecoration === 'function') applyGroupDecoration(item, s);
        const top = el('div', 'session-item-top');
        top.appendChild(el('span', 'session-unread-dot'));
        top.appendChild(el('div', 'session-title', markdownToPlainText(s.title || '(untitled)')));
        item.appendChild(top);
        item.appendChild(el('div', 'session-meta', timeAgo(s.updated_at)));
        item.addEventListener('click', () => openSession(s.conversation_id));
        listEl.appendChild(item);
      });
    }
  }

  // ---- main pane ----
  function renderView() {
    viewEl.textContent = '';
    curW = null;
    const e = curEntry();
    if (ucur && unreadList().some((s) => s.conversation_id === ucur)) {
      const s = sessions.find((x) => x.conversation_id === ucur);
      const box = el('div', 'inbox-empty');
      box.appendChild(el('b', '', markdownToPlainText(s.title || '(untitled)')));
      box.appendChild(el('div', 'ask-muted', 'Unread session — press Enter or click to open.'));
      const b = el('button', 'slides-btn', 'Open session'); b.type = 'button';
      b.addEventListener('click', () => openSession(ucur));
      box.appendChild(b);
      viewEl.appendChild(box);
      return;
    }
    ucur = null;
    if (!e || !matches(e)) {
      const box = el('div', 'inbox-empty');
      box.appendChild(el('div', 'inbox-empty-icon', '✓'));
      box.appendChild(el('b', '', 'Inbox zero'));
      box.appendChild(el('div', 'ask-muted', unreadList().length ? 'No pending questions — unread sessions are listed on the left.' : 'No agent is waiting for you.'));
      viewEl.appendChild(box);
      return;
    }
    slide = Math.max(0, Math.min(slide, Math.max(0, e.blocks.length - 1)));
    const wrap = el('div', 'inbox-card');
    const head = el('div', 'inbox-head');
    head.appendChild(el('b', 'inbox-title', markdownToPlainText(e.title)));
    head.appendChild(el('span', 'ask-muted', `· ${timeAgo(e.at)}`));
    head.appendChild(el('span', 'ask-sp'));
    const open = el('button', 'slides-btn', 'Open session');
    open.type = 'button';
    open.addEventListener('click', () => openSession(e.conv));
    head.appendChild(open);
    wrap.appendChild(head);
    if (e.why) wrap.appendChild(el('div', 'inbox-why', e.why));
    const ctx = el('div', 'inbox-ctx');
    ctx.id = 'inbox-ctx';
    ctx.hidden = !ctxOpen;
    if (ctxOpen) ctx.innerHTML = renderMarkdown(e.text.replace(/```(ask|ideas|actions)[\s\S]*?```/g, ''));
    wrap.appendChild(ctx);
    if (e.actions.length) window.ActionsWidget.render(wrap, e.actions, { conv: e.conv, onClick: (a) => sendText(e, a.text) });
    const chips = el('div', 'inbox-chips');
    chips.id = 'inbox-chips';
    wrap.appendChild(chips);
    const slides = el('div', 'slides');
    const pane = el('div', 'slides-pane');
    const body = el('div', 'slides-body');
    body.id = 'inbox-body';
    const nav = el('div', 'slides-nav');
    nav.id = 'inbox-nav';
    pane.append(body, nav);
    slides.appendChild(pane);
    wrap.appendChild(slides);
    viewEl.appendChild(wrap);
    e.blocks.forEach((b, k) => {
      if (!showAll && k !== slide) return;
      const host = el('div', 'inbox-slide');
      host.dataset.block = String(k);
      body.appendChild(host);
      if (b.kind === 'ask') {
        const w = window.AskWidget.mountInline(host, e.msgId, b.doc, e.conv, updateChrome);
        if (!showAll) curW = w;
      } else {
        host.appendChild(window.IdeasWidget.mount(e.msgId, b.doc, e.conv));
        if (!showAll) setTimeout(() => window.IdeasWidget.focus(e.msgId, b.doc), 0);
      }
      ['click', 'input', 'keyup'].forEach((ev) => host.addEventListener(ev, () => setTimeout(updateChrome, 0)));
    });
    updateChrome();
  }

  function updateChrome() {
    const e = curEntry();
    const nav = $('inbox-nav');
    const chips = $('inbox-chips');
    if (!e || !nav || !chips) return;
    const st = stats(e);
    chips.textContent = '';
    if (e.blocks.length > 1 && !showAll) {
      e.blocks.forEach((b, k) => {
        const done = st[k].done >= st[k].total;
        const c = el('span', `slides-rail-item${k === slide ? ' cur' : ''}${done ? ' done' : ''}`, `${b.kind === 'ideas' ? 'Ideas' : b.doc.title}${done ? ' ✓' : ''}`);
        c.addEventListener('click', () => { slide = k; renderView(); });
        chips.appendChild(c);
      });
    }
    nav.textContent = '';
    const btn = (txt, title, fn, cls) => { const b = el('button', cls || 'slides-btn', txt); b.type = 'button'; b.title = title; b.addEventListener('click', fn); return b; };
    nav.appendChild(btn(showAll ? 'Slides' : 'Show all', 'Show every block at once', () => { showAll = !showAll; renderView(); }));
    nav.appendChild(btn(ctxOpen ? 'Hide context' : 'Context', 'Full last message (c)', () => { ctxOpen = !ctxOpen; renderView(); }));
    nav.appendChild(el('span', 'ask-sp'));
    nav.appendChild(btn('Not needed ✕', 'Remove from inbox (d)', () => dismiss(e), 'slides-btn inbox-dismiss'));
    const prev = btn('‹ Prev', 'Previous block ([)', () => { slide--; renderView(); });
    const next = btn('Next ›', 'Next block (])', () => { slide++; renderView(); });
    prev.disabled = showAll || slide === 0;
    next.disabled = showAll || slide >= e.blocks.length - 1;
    if (e.blocks.length > 1) { nav.appendChild(prev); nav.appendChild(el('span', '', `${showAll ? e.blocks.length : slide + 1} / ${e.blocks.length}`)); nav.appendChild(next); }
    const n = answeredCount(e);
    const ready = isReady(e);
    if (!e.blocks.length) return;
    const send = btn(`Send to session${ready ? '' : n ? ` (${n} answered)` : ''}`, 'Shift+Enter', () => sendEntry(e), 'slides-btn inbox-send');
    send.disabled = n === 0;
    nav.appendChild(send);
    const all = entries.filter(isReady);
    if (all.length > 1) nav.appendChild(btn(`Send all answered (${all.length})`, 'Send every fully answered session', sendAll, 'slides-btn'));
    // keep the sidebar badge for this row current without rebuilding the list
    const row = listEl.querySelector(`.session-item[data-conversation-id="${e.conv}"] .inbox-count`);
    if (row) { row.className = `inbox-count${ready ? ' ready' : ''}`; row.textContent = ready ? '✓' : String(e.counts.q + e.counts.d + e.counts.i); }
  }

  // ---- actions ----
  async function sendEntry(e, quiet) {
    const parts = e.blocks.map((b) => window[b.kind === 'ask' ? 'AskWidget' : 'IdeasWidget'].block(e.msgId, b.doc, e.conv)).filter(Boolean);
    if (!parts.length) return false;
    const s = (typeof sessions !== 'undefined' ? sessions : []).find((x) => x.conversation_id === e.conv);
    if (s && s.running && !quiet && !confirm('This session is running a turn. Sending now will interrupt it. Send anyway?')) return false;
    try { await api('/api/send', { method: 'POST', body: JSON.stringify({ conversation_id: e.conv, text: parts.join('\n\n') }) }); }
    catch (err) { alert(`Send failed: ${err.message}`); return false; }
    e.blocks.forEach((b) => window[b.kind === 'ask' ? 'AskWidget' : 'IdeasWidget'].clear(e.msgId, b.doc, e.conv));
    removeLocal(e);
    if (!quiet) toast(`Sent to “${markdownToPlainText(e.title)}”`);
    return true;
  }
  async function sendText(e, text) {
    const s = (typeof sessions !== 'undefined' ? sessions : []).find((x) => x.conversation_id === e.conv);
    if (s && s.running && !confirm('This session is running a turn. Sending now will interrupt it. Send anyway?')) return;
    try { await api('/api/send', { method: 'POST', body: JSON.stringify({ conversation_id: e.conv, text }) }); }
    catch (err) { alert(`Send failed: ${err.message}`); return; }
    removeLocal(e);
    toast(`Sent “${text.length > 40 ? `${text.slice(0, 39)}…` : text}” to “${markdownToPlainText(e.title)}”`);
  }
  async function sendAll() {
    const ready = entries.filter(isReady);
    const running = ready.filter((e) => { const s = (typeof sessions !== 'undefined' ? sessions : []).find((x) => x.conversation_id === e.conv); return s && s.running; });
    if (running.length && !confirm(`${running.length} session(s) are running a turn; sending interrupts it. Send anyway?`)) return;
    let n = 0;
    for (const e of ready) if (await sendEntry(e, true)) n++;
    toast(`Sent to ${n} session${n === 1 ? '' : 's'}`);
  }
  function removeLocal(e) {
    const vis = visible();
    const ix = vis.findIndex((x) => x.conv === e.conv);
    entries = entries.filter((x) => x.conv !== e.conv);
    badgeEl.textContent = String(entries.length);
    badgeEl.style.display = entries.length ? '' : 'none';
    const nxt = visible()[Math.min(ix, visible().length - 1)];
    cur = nxt ? nxt.conv : null;
    slide = 0; showAll = false; ctxOpen = false;
    renderList(); renderView();
    refreshSoon();
  }
  async function dismiss(e) {
    try { await api(`/api/inbox/${encodeURIComponent(e.conv)}/dismiss?message_id=${encodeURIComponent(e.msgId)}`, { method: 'PUT' }); }
    catch (err) { alert(`Failed: ${err.message}`); return; }
    removeLocal(e);
    toast('Removed from inbox', async () => {
      try { await api(`/api/inbox/${encodeURIComponent(e.conv)}/dismiss?message_id=${encodeURIComponent(e.msgId)}`, { method: 'DELETE' }); } catch (err) { /* ignore */ }
      load();
    });
  }

  // ---- keyboard (only while the inbox is open and focus is not in an input) ----
  document.addEventListener('keydown', (ev) => {
    if (mode !== 'inbox' || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (typeof isEditableTarget === 'function' && isEditableTarget(ev.target)) return;
    const vis = visible();
    const rows = vis.map((x) => ({ conv: x.conv })).concat(unreadList().map((s) => ({ conv: s.conversation_id, unread: true })));
    const ix = rows.findIndex((r) => (ucur ? r.unread && r.conv === ucur : !r.unread && r.conv === cur));
    const go = (d) => {
      const n = rows[ix + d];
      if (!n) return;
      slide = 0; showAll = false; ctxOpen = false;
      if (n.unread) ucur = n.conv; else { cur = n.conv; ucur = null; }
      renderList(); renderView();
    };
    if (ucur && ev.key === 'Enter') { ev.preventDefault(); openSession(ucur); return; }
    const e = ucur ? null : curEntry();
    if (ev.key === 'j') { ev.preventDefault(); go(1); }
    else if (ev.key === 'k') { ev.preventDefault(); go(-1); }
    else if (!e) return;
    else if (ev.key === ']' && slide < e.blocks.length - 1) { slide++; renderView(); }
    else if (ev.key === '[' && slide > 0) { slide--; renderView(); }
    else if (ev.key === 'd') { ev.preventDefault(); dismiss(e); }
    else if (ev.key === 'c') { ctxOpen = !ctxOpen; renderView(); }
    else if (ev.key === 'o') { openSession(e.conv); }
    else if (ev.key === 'Enter' && ev.shiftKey) { ev.preventDefault(); sendEntry(e); }
    else if (curW && /^[1-9]$/.test(ev.key)) { ev.preventDefault(); window.AskWidget.pickIndex(curW, +ev.key - 1); }
    else if (curW && ev.key === 'ArrowRight') { ev.preventDefault(); window.AskWidget.nextItem(curW); }
    else if (curW && ev.key === 'ArrowLeft') { ev.preventDefault(); window.AskWidget.prevItem(curW); }
  });

  window.Inbox = { enter, leave, active: () => mode === 'inbox', refreshSoon, reload: load, dropConversation };

  // initial badge + restore last mode / deep link
  load().then(() => { if (location.hash === '#inbox' || (localStorage.getItem(MODE_KEY) === 'inbox' && !/^#(open|msg)=/.test(location.hash) && !/^#new/.test(location.hash))) enter(); });
})();
