/*
 * Quick-reply ("actions") widget. An assistant message with a fenced block
 *
 *   ```actions id=next
 *   - * Proceed | proceed
 *   - Fix gaps | fix gaps
 *   - Skip tests
 *   ```
 *
 * renders each line as a clickable chip. Line grammar: `- [*] label [| text]`
 * (`*` = primary, text defaults to the label). Default click behaviour
 * inserts the text into the conversation's compose draft (no auto-send,
 * never duplicated); the Messages inbox overrides it via the onClick option
 * of render() or ActionsWidget.onClick to send directly.
 *
 * Convention: dop-agent/skills/actions-format. The widget keeps no state of its
 * own (renderMessages() rebuilds bubbles on every update); blocks of a still
 * running turn or without valid lines stay plain code blocks, which is also
 * what Slack/other mirrors show.
 */
(function () {
  const FENCE_RE = /```actions([^\n]*)\n([\s\S]*?)\n[ \t]*```/g;
  const LINE_RE = /^-\s*(\*)?\s*(.+)$/;
  const MAX_TEXT = 500;

  function parseAttrs(s) {
    const attrs = {};
    (s.match(/(\w+)=("[^"]*"|\S+)/g) || []).forEach((a) => {
      const i = a.indexOf('=');
      attrs[a.slice(0, i)] = a.slice(i + 1).replace(/^"|"$/g, '');
    });
    return attrs;
  }

  function parseBody(body, blockId) {
    const out = [];
    body.split('\n').forEach((line) => {
      const m = LINE_RE.exec(line.trim());
      if (!m) return;
      const bar = m[2].indexOf('|');
      const label = (bar < 0 ? m[2] : m[2].slice(0, bar)).trim();
      const text = (bar < 0 ? label : m[2].slice(bar + 1).trim() || label).slice(0, MAX_TEXT);
      if (!label) return;
      out.push({ id: `${blockId}-${out.length + 1}`, label, text, primary: !!m[1] });
    });
    return out;
  }

  // parse(text) -> flat [{id,label,text,primary}] across all actions blocks (ids are unique per message).
  function parseBlocks(text) {
    const blocks = [];
    let m;
    FENCE_RE.lastIndex = 0;
    while ((m = FENCE_RE.exec(text || ''))) {
      const attrs = parseAttrs(m[1]);
      const body = m[2].trim();
      const id = attrs.id || `actions${blocks.length + 1}`;
      const actions = parseBody(body, id);
      if (actions.length) blocks.push({ id, title: attrs.title || '', body, actions });
    }
    return blocks;
  }
  function parse(text) {
    return parseBlocks(text).reduce((acc, b) => acc.concat(b.actions), []);
  }

  // Append text to the conversation's compose draft unless a line already equals it.
  function insertIntoDraft(conv, text) {
    const active = typeof currentConversationId !== 'undefined' && currentConversationId === conv;
    const cur = active ? composeText.value : getDraft(conv);
    const lines = cur.split('\n').map((l) => l.trim());
    let next = cur;
    if (!lines.includes(text.trim())) next = cur && !cur.endsWith('\n') ? `${cur}\n${text}` : cur + text;
    if (next !== cur) setDraft(conv, next);
    if (active) {
      if (next !== cur) {
        composeText.value = next;
        composeText.dispatchEvent(new Event('input', { bubbles: true }));
      }
      composeText.focus();
      composeText.setSelectionRange(composeText.value.length, composeText.value.length);
    }
    return next;
  }

  const api = { onClick: null };

  // render(container, actions, {conv, onClick}) -> the wrapper element appended to container.
  function render(container, actions, opts) {
    opts = opts || {};
    const wrap = document.createElement('div');
    wrap.className = 'actions-block';
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', 'Quick replies');
    actions.forEach((a) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `actions-chip${a.primary ? ' primary' : ''}`;
      b.dataset.actionId = a.id;
      b.textContent = a.label;
      b.title = a.text === a.label ? a.label : a.text;
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        const hook = opts.onClick || api.onClick || ((act, ctx) => insertIntoDraft(ctx.conv, act.text));
        hook(a, { conv: opts.conv, button: b });
        b.classList.add('used');
      });
      wrap.appendChild(b);
    });
    container.appendChild(wrap);
    return wrap;
  }

  function apply(bubble, msgId, turnText, conversationId, running) {
    if (running || !turnText || turnText.indexOf('```actions') < 0) return;
    const codes = bubble.querySelectorAll('pre > code.language-actions');
    if (!codes.length) return;
    const blocks = parseBlocks(turnText);
    codes.forEach((code) => {
      const blk = blocks.find((b) => b.body === code.textContent.trim());
      if (!blk) return;
      const holder = document.createElement('div');
      render(holder, blk.actions, { conv: conversationId });
      code.parentElement.replaceWith(holder.firstChild);
    });
  }

  Object.assign(api, { parse, parseBlocks, render, apply, insertIntoDraft });
  window.ActionsWidget = api;
})();
