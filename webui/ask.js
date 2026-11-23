/*
 * Interactive Question/Decision ("ask") widget. An assistant message with
 * one or more fenced blocks
 *
 *   ```ask title="Review of MR !52" id=mr52
 *   D1 single: Where should the logic live?
 *   - a: Keep in base | recipe is self-contained
 *   - b*: New example-app-bind module | follows the naming rule
 *   Q1 text: Reviewer?
 *   ? e.g. Juraj
 *   ```
 *
 * renders each item as a clickable chip inside the message. A chip opens a
 * popover next to it (Card / Answered / All views) with the predefined
 * options and a note field. Answers are written into the conversation's
 * compose draft as
 *
 *   Answers on "<title>" [<id>]
 *   - [D1] <question> → b. <label> — <note>
 *
 * Convention: dop-agent/skills/ask-format. Design notes (same constraints as
 * ideas.js): renderMessages() rebuilds every bubble on each update, so all
 * widget state lives in askState (localStorage), never in the DOM; the popover
 * lives on <body> and is re-anchored (or closed) after each render; blocks
 * that are invalid or belong to a still-running turn stay plain code blocks.
 */
(function () {
  const STATE_KEY = 'dop_agent_ask_v1';
  const MAX_STATES = 300;
  const FENCE_RE = /```ask([^\n]*)\n([\s\S]*?)\n[ \t]*```/g;
  const HEAD_RE = /^([QD]\d+)(?:\s+\[?(single|multi|text)\]?)?\s*:\s*(.+)$/;
  const OPT_RE = /^-\s*([A-Za-z0-9_-]+)(\*)?\s*:\s*(.+?)(?:\s*\|\s*(.+))?$/;

  let askState = {};
  try { askState = JSON.parse(localStorage.getItem(STATE_KEY) || '{}'); } catch (e) { askState = {}; }
  function saveState() {
    const keys = Object.keys(askState);
    if (keys.length > MAX_STATES) keys.slice(0, keys.length - MAX_STATES).forEach((k) => delete askState[k]);
    try { localStorage.setItem(STATE_KEY, JSON.stringify(askState)); } catch (e) { /* quota */ }
  }

  function parseAttrs(s) {
    const attrs = {};
    (s.match(/(\w+)=("[^"]*"|\S+)/g) || []).forEach((a) => {
      const i = a.indexOf('=');
      attrs[a.slice(0, i)] = a.slice(i + 1).replace(/^"|"$/g, '');
    });
    return attrs;
  }

  // Parse one block body into items; null when invalid (empty, duplicate ids/keys, option before header).
  function parseBody(body) {
    const items = [];
    const ids = new Set();
    let cur = null;
    for (const raw of body.split('\n')) {
      const l = raw.trim();
      if (!l) continue;
      let m;
      if ((m = HEAD_RE.exec(l))) {
        if (ids.has(m[1])) return null;
        ids.add(m[1]);
        cur = { id: m[1], kind: m[1][0] === 'D' ? 'decision' : 'question', mode: m[2] || 'single', title: m[3], options: [], hint: null };
        items.push(cur);
      } else if (cur && (m = OPT_RE.exec(l))) {
        if (cur.options.some((o) => o.key === m[1])) return null;
        cur.options.push({ key: m[1], rec: !!m[2], label: m[3], note: m[4] || '' });
      } else if (cur && l[0] === '?') {
        cur.hint = l.slice(1).trim() || 'Add details…';
      } else {
        return null;
      }
    }
    if (!items.length) return null;
    items.forEach((i) => {
      if (!i.options.length) i.mode = 'text';
      if (i.mode === 'text' && i.hint == null) i.hint = 'Your answer…';
    });
    return items;
  }

  // Parse a whole message's text into [{attrs, items, body}] (invalid blocks omitted).
  function parse(text) {
    const out = [];
    FENCE_RE.lastIndex = 0;
    let m;
    let n = 0;
    while ((m = FENCE_RE.exec(text))) {
      const attrs = parseAttrs(m[1]);
      const items = parseBody(m[2]);
      if (items) out.push({ title: attrs.title || 'Questions', id: attrs.id || String(n), items, body: m[2].trim() });
      n++;
    }
    return out;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function short(t, n) { return t.length > n ? `${t.slice(0, n - 1)}…` : t; }

  const itemSt = (st, it) => st.d[it.id] || (st.d[it.id] = { sel: [], note: '', skip: false });
  const answered = (st, it) => { const s = st.d[it.id]; return !!s && (s.sel.length > 0 || s.note.trim().length > 0); };
  const skipped = (st, it) => !answered(st, it) && !!(st.d[it.id] && st.d[it.id].skip);
  const status = (st, it) => (answered(st, it) ? 'ok' : skipped(st, it) ? 'skip' : 'open');
  function answerText(it, s) {
    const picked = s.sel.map((k) => { const o = it.options.find((x) => x.key === k); return o ? `${o.key}. ${o.label}` : k; }).join('; ');
    const note = s.note.trim();
    return picked && note ? `${picked} — ${note}` : picked || note;
  }

  // ---- compose draft block ----
  function headerLine(doc) { return `Answers on "${doc.title}" [${doc.id}]`; }
  function answersBlock(doc, st) {
    // multi-line notes: continuation lines are indented so mergeBlock can recognise them as part of the block
    const lines = doc.items.filter((i) => answered(st, i)).map((i) => {
      const t = answerText(i, st.d[i.id]).split('\n').map((l) => l.trim()).filter(Boolean);
      return `- [${i.id}] ${i.title} → ${t.join('\n  ')}`;
    });
    return lines.length ? [headerLine(doc), ...lines].join('\n') : '';
  }
  function mergeBlock(draft, header, block) {
    const lines = draft.split('\n');
    const idTag = /\[[^\]]+\]$/.exec(header);
    let at = lines.indexOf(header);
    if (at < 0 && idTag) at = lines.findIndex((l) => l.startsWith('Answers on "') && l.endsWith(idTag[0]));
    if (at >= 0) {
      let end = at + 1;
      while (end < lines.length && (lines[end].startsWith('- [') || lines[end].startsWith('  '))) end++;
      lines.splice(at, end - at, ...(block ? block.split('\n') : []));
      return lines.join('\n').replace(/\n{3,}/g, '\n\n');
    }
    if (!block) return draft;
    return draft ? `${draft.replace(/\s+$/, '')}\n\n${block}\n` : `${block}\n`;
  }
  function syncDraft(w) {
    const { doc, st } = w;
    const conv = st.conv;
    const active = typeof currentConversationId !== 'undefined' && currentConversationId === conv;
    const cur = active ? composeText.value : getDraft(conv);
    const next = mergeBlock(cur, headerLine(doc), answersBlock(doc, st));
    if (next === cur) return;
    setDraft(conv, next);
    if (active) {
      composeText.value = next;
      if (typeof autoGrowComposeText === 'function') autoGrowComposeText();
    }
  }

  // ---- chips in the message ----
  const widgets = {};
  let open = null; // { key, popover }
  let focusInfo = null;

  function drawChip(w, it) {
    const c = w.chips[it.id];
    const stt = status(w.st, it);
    c.className = `ask-chip st-${stt}${open && open.key === w.key && w.doc.items[w.st.cur] === it ? ' active' : ''}`;
    c.textContent = '';
    c.appendChild(el('span', `ask-badge${stt === 'ok' ? ' done' : ''}`, `${stt === 'ok' ? '✓ ' : ''}${it.id}`));
    c.appendChild(el('span', 'ask-chip-text', short(stt === 'ok' ? answerText(it, w.st.d[it.id]) : it.title, 48)));
  }
  function drawChips(w) { if (!w.chips) return; w.doc.items.forEach((it) => drawChip(w, it)); }

  function build(key, doc, st) {
    const root = el('div', 'ask-block');
    if (doc.title) root.appendChild(el('div', 'ask-block-title', doc.title));
    const list = el('div', 'ask-chips');
    const w = { key, doc, st, root, chips: {} };
    doc.items.forEach((it, idx) => {
      const c = el('button', 'ask-chip');
      c.type = 'button';
      c.dataset.askKey = key;
      c.dataset.askItem = it.id;
      c.addEventListener('click', (e) => {
        e.stopPropagation();
        if (open && open.key === key && st.cur === idx) closePop(); else openPop(w, idx);
      });
      w.chips[it.id] = c;
      list.appendChild(c);
    });
    root.appendChild(list);
    widgets[key] = w;
    drawChips(w);
    return root;
  }

  // ---- popover ----
  function closePop() {
    if (open) { open.popover.remove(); const w = widgets[open.key]; open = null; if (w) drawChips(w); }
  }
  function openPop(w, idx) {
    if (open) { open.popover.remove(); open = null; }
    w.st.cur = idx;
    if (w.st.view === 'answered' && !w.doc.items.some((i) => answered(w.st, i))) w.st.view = 'card';
    if (w.st.view !== 'answered' && w.st.view !== 'all') w.st.view = 'card';
    const pop = el('div', 'ask-pop');
    pop.setAttribute('role', 'dialog');
    pop.addEventListener('click', (e) => e.stopPropagation());
    document.body.appendChild(pop);
    open = { key: w.key, popover: pop };
    renderPop();
  }
  function renderPop() {
    if (!open) return;
    const w = widgets[open.key];
    if (!w) { closePop(); return; }
    const { doc, st } = w;
    const pop = open.popover;
    const fi = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('ask-note')
      ? { start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd } : null;
    paint(w, pop, true);
    drawChips(w);
    place(w);
    if (fi) {
      const t = pop.querySelector('.ask-note');
      if (t) { t.focus(); try { t.setSelectionRange(fi.start, fi.end); } catch (e) { /* ignore */ } }
    }
  }

  // Header + card/table body drawn into `host` (the popover, or an inbox slide when w.inline is set).
  function paint(w, host, withClose) {
    const { doc, st } = w;
    const pop = host;
    pop.textContent = '';
    const head = el('div', 'ask-ph');
    const seg = el('div', 'ask-seg');
    [['card', 'Card'], ['answered', 'Answered'], ['all', 'All']].forEach(([v, label]) => {
      const b = el('button', st.view === v ? 'on' : '', label);
      b.type = 'button';
      b.addEventListener('click', () => { st.view = v; saveState(); rerender(w); });
      seg.appendChild(b);
    });
    head.appendChild(seg);
    head.appendChild(el('span', 'ask-muted', `${doc.items.filter((i) => answered(st, i)).length}/${doc.items.length} answered`));
    head.appendChild(el('span', 'ask-sp'));
    if (withClose) {
      const x = el('button', 'ask-x', '✕');
      x.type = 'button';
      x.addEventListener('click', closePop);
      head.appendChild(x);
    }
    pop.appendChild(head);
    pop.appendChild(st.view === 'card' ? cardView(w) : tableView(w));
  }

  function rerender(w) {
    if (!w.inline) { renderPop(); return; }
    const a = document.activeElement;
    const fi = a && a.classList && a.classList.contains('ask-note') && w.inline.contains(a) ? { start: a.selectionStart, end: a.selectionEnd } : null;
    paint(w, w.inline, false);
    if (fi) { const t = w.inline.querySelector('.ask-note'); if (t) { t.focus(); try { t.setSelectionRange(fi.start, fi.end); } catch (e) { /* ignore */ } } }
    if (w.onChange) w.onChange();
  }

  function place(w) {
    if (!open) return;
    const chip = w.chips[w.doc.items[w.st.cur].id];
    const pop = open.popover;
    if (!chip || !document.body.contains(chip)) { closePop(); return; }
    const r = chip.getBoundingClientRect();
    const width = Math.min(540, window.innerWidth - 24);
    pop.style.width = `${width}px`;
    const h = pop.offsetHeight;
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    pop.style.top = `${top}px`;
    pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - width - 12))}px`;
  }

  function after(w) { saveState(); syncDraft(w); drawChips(w); }

  function pick(w, it, key) {
    const s = itemSt(w.st, it);
    s.skip = false;
    if (it.mode === 'multi') s.sel = s.sel.includes(key) ? s.sel.filter((k) => k !== key) : [...s.sel, key];
    else s.sel = s.sel[0] === key && !s.note.trim() ? [] : [key];
    after(w);
    rerender(w);
    if (it.mode !== 'multi' && s.sel.length) setTimeout(() => { const cw = w.inline ? w : widgets[w.key]; if (cw && (w.inline || (open && open.key === cw.key)) && cw.doc.items[cw.st.cur].id === it.id && cw.st.view === 'card') next(cw); }, 170);
  }
  function setNote(w, it, text) {
    const s = itemSt(w.st, it);
    s.note = text;
    if (text.trim()) s.skip = false;
    after(w);
  }
  function skip(w, it) {
    w.st.d[it.id] = { sel: [], note: '', skip: true };
    after(w);
    next(w);
  }
  function go(w, d) { w.st.cur = Math.max(0, Math.min(w.doc.items.length - 1, w.st.cur + d)); saveState(); rerender(w); }
  function next(w) {
    const n = w.doc.items.findIndex((x, i) => i > w.st.cur && status(w.st, x) === 'open');
    if (n >= 0) { w.st.cur = n; saveState(); rerender(w); return; }
    w.st.view = 'answered';
    saveState();
    if (w.inline) rerender(w); else closePop();
  }

  function cardView(w) {
    const { doc, st } = w;
    const it = doc.items[st.cur];
    const s = itemSt(st, it);
    const wrap = el('div', 'ask-body');
    const t = el('div', 'ask-ct');
    t.appendChild(el('span', 'ask-badge', it.id));
    t.appendChild(el('b', '', it.title));
    t.appendChild(el('span', 'ask-muted ask-right', `${st.cur + 1} / ${doc.items.length}${it.mode === 'multi' ? ' · pick several' : ''}`));
    wrap.appendChild(t);
    it.options.forEach((o, i) => {
      const sel = s.sel.includes(o.key);
      const r = el('div', `ask-opt${sel ? ' sel' : ''}`);
      r.appendChild(el('kbd', '', String(i + 1)));
      const txt = el('span', 'ask-opt-text');
      txt.appendChild(el('span', 'ask-lbl', o.label));
      if (o.rec) txt.appendChild(el('span', 'ask-rec', 'suggested'));
      if (o.note) txt.appendChild(el('span', 'ask-onote', o.note));
      r.appendChild(txt);
      r.addEventListener('click', () => pick(w, it, o.key));
      wrap.appendChild(r);
    });
    const ta = el('textarea', 'ask-note');
    ta.rows = 4;
    ta.placeholder = `${it.hint || 'Add a note'}  (Tab)`;
    ta.value = s.note;
    ta.addEventListener('input', () => setNote(w, it, ta.value));
    ta.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') { if (w.inline) ta.blur(); else closePop(); }
      else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); next(w); }
    });
    wrap.appendChild(ta);
    const f = el('div', 'ask-pf');
    const back = el('button', 'ask-btn', '‹ back');
    back.type = 'button';
    back.disabled = st.cur === 0;
    back.addEventListener('click', () => go(w, -1));
    const sk = el('button', 'ask-btn', 'skip');
    sk.type = 'button';
    sk.addEventListener('click', () => skip(w, it));
    const nx = el('button', 'ask-btn primary', 'next ›');
    nx.type = 'button';
    nx.addEventListener('click', () => next(w));
    f.append(back, sk, el('span', 'ask-sp'), el('span', 'ask-muted', '1-9 pick · Tab note · ←→ · Esc'), nx);
    wrap.appendChild(f);
    return wrap;
  }

  function tableView(w) {
    const { doc, st } = w;
    const wrap = el('div', 'ask-body');
    const rows = doc.items.filter((i) => st.view === 'all' || status(st, i) === 'ok');
    if (!rows.length) wrap.appendChild(el('div', 'ask-muted ask-empty', 'Nothing answered yet.'));
    const tbl = el('div', 'ask-tbl');
    rows.forEach((it) => {
      const stt = status(st, it);
      const r = el('div', `ask-row st-${stt}`);
      r.appendChild(el('span', 'ask-badge', it.id));
      r.appendChild(el('span', 'ask-tt', it.title));
      r.appendChild(el('span', 'ask-ta', stt === 'ok' ? answerText(it, st.d[it.id]) : stt === 'skip' ? 'skipped' : '—'));
      r.addEventListener('click', () => { st.cur = doc.items.indexOf(it); st.view = 'card'; saveState(); rerender(w); });
      tbl.appendChild(r);
    });
    wrap.appendChild(tbl);
    const f = el('div', 'ask-pf');
    const sg = el('button', 'ask-btn', 'use suggested for the rest');
    sg.type = 'button';
    sg.addEventListener('click', () => {
      doc.items.forEach((it) => {
        const r = it.options.find((o) => o.rec);
        if (r && !answered(st, it)) { const s = itemSt(st, it); s.sel = [r.key]; s.skip = false; }
      });
      after(w); rerender(w);
    });
    const done = el('button', 'ask-btn primary', 'done');
    done.type = 'button';
    done.addEventListener('click', closePop);
    f.append(sg, el('span', 'ask-sp'), el('span', 'ask-muted', 'click a row to edit · answers are in the compose box'), done);
    wrap.appendChild(f);
    return wrap;
  }

  // ---- integration with renderMessages() ----
  function captureFocus() {
    focusInfo = null;
    if (!open) return;
    const a = document.activeElement;
    if (a && a.classList && a.classList.contains('ask-note') && open.popover.contains(a)) {
      focusInfo = { start: a.selectionStart, end: a.selectionEnd };
    }
    // renderMessages() is synchronous: re-anchor (or close) once it has finished.
    Promise.resolve().then(reanchor);
  }
  function reanchor() {
    if (!open) return;
    const w = widgets[open.key];
    const chip = w && w.doc.items[w.st.cur] && w.chips[w.doc.items[w.st.cur].id];
    const active = typeof currentConversationId !== 'undefined' && w && currentConversationId === w.st.conv;
    if (!w || !active || !chip || !document.body.contains(chip)) { closePop(); return; }
    renderPop();
    if (focusInfo) {
      const t = open.popover.querySelector('.ask-note');
      if (t) { t.focus(); try { t.setSelectionRange(focusInfo.start, focusInfo.end); } catch (e) { /* ignore */ } }
      focusInfo = null;
    }
  }

  function apply(bubble, msgId, turnText, conversationId, running) {
    if (running || !turnText || turnText.indexOf('```ask') < 0) return;
    const codes = bubble.querySelectorAll('pre > code.language-ask');
    if (!codes.length) return;
    const docs = parse(turnText);
    codes.forEach((code) => {
      const doc = docs.find((d) => d.body === code.textContent.trim());
      if (!doc) return;
      const key = `${msgId}|${doc.id}`;
      const st = askState[key] || (askState[key] = { d: {}, view: 'card', cur: 0 });
      st.conv = conversationId;
      code.parentElement.replaceWith(build(key, doc, st));
    });
  }

  document.addEventListener('click', () => { if (open) closePop(); });
  window.addEventListener('resize', () => { if (open) renderPop(); });
  // Scrolling (incl. renderMessages() restoring scrollTop) follows the chip; closes once it leaves the viewport.
  document.addEventListener('scroll', (e) => {
    if (!open || open.popover.contains(e.target)) return;
    const w = widgets[open.key];
    const chip = w && w.chips[w.doc.items[w.st.cur].id];
    const r = chip && chip.getBoundingClientRect();
    if (!r || r.bottom < 0 || r.top > window.innerHeight) closePop(); else place(w);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!open || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'Escape') { closePop(); return; }
    if (isEditableTarget(e.target)) return;
    const w = widgets[open.key];
    if (!w || w.st.view !== 'card') return;
    const it = w.doc.items[w.st.cur];
    if (/^[1-9]$/.test(e.key) && it.options[+e.key - 1]) { e.preventDefault(); pick(w, it, it.options[+e.key - 1].key); }
    else if (e.key === 'Tab' && !e.shiftKey) { const t = open.popover.querySelector('.ask-note'); if (t) { e.preventDefault(); t.focus(); } }
    else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); next(w); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(w, -1); }
  });

  // ---- inline mount (Messages inbox): same card/table views and state as the in-message chips ----
  function stateFor(msgId, doc, conv) {
    const key = `${msgId}|${doc.id}`;
    const st = askState[key] || (askState[key] = { d: {}, view: 'card', cur: 0 });
    st.conv = conv;
    if (st.view !== 'card' && st.view !== 'all' && st.view !== 'answered') st.view = 'card';
    return st;
  }
  function mountInline(container, msgId, doc, conv, onChange) {
    const st = stateFor(msgId, doc, conv);
    if (st.view === 'answered' && !doc.items.some((i) => answered(st, i))) st.view = 'card';
    const w = { key: `${msgId}|${doc.id}`, doc, st, inline: container, onChange, root: container, chips: null };
    paint(w, container, false);
    return w;
  }
  const inlineApi = {
    mountInline,
    stats(msgId, doc, conv) {
      const st = stateFor(msgId, doc, conv);
      return { total: doc.items.length, answered: doc.items.filter((i) => answered(st, i)).length, skipped: doc.items.filter((i) => skipped(st, i)).length, done: doc.items.filter((i) => answered(st, i) || skipped(st, i)).length };
    },
    block(msgId, doc, conv) { return answersBlock(doc, stateFor(msgId, doc, conv)); },
    // Remove this widget's answer block from the conversation draft and forget its state after sending.
    clear(msgId, doc, conv) {
      const key = `${msgId}|${doc.id}`;
      const active = typeof currentConversationId !== 'undefined' && currentConversationId === conv;
      const cur = active ? composeText.value : getDraft(conv);
      const next = mergeBlock(cur, headerLine(doc), '');
      if (next !== cur) { setDraft(conv, next); if (active) composeText.value = next; }
      delete askState[key];
      saveState();
    },
    pickIndex(w, n) { const it = w.doc.items[w.st.cur]; if (it && w.st.view === 'card' && it.options[n]) pick(w, it, it.options[n].key); },
    nextItem(w) { if (w.st.view === 'card') next(w); },
    prevItem(w) { go(w, -1); },
  };

  window.AskWidget = { ...inlineApi, apply, captureFocus, parse, mergeBlock, headerLine, state: askState };
})();
