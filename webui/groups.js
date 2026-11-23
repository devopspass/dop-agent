// Session groups ("workstreams") UI. Loaded after app.js and shares its
// global state (`sessions`, `groups`, `api`, `escapeHtml`, `openSession`,
// `currentConversationId`, `renderSessionList`...). app.js calls into the
// functions below from renderSessionList()/updateThreadHeader()/openSession().
//
// Model: a session is in at most one group (no nesting); no group = "Ungrouped".
// The group's master (if any) always sorts first inside its group. Archiving a
// group archives its sessions server-side; deleting only ungroups them.

const GROUP_FILTER_KEY = 'dop-agent-group-filter';
const GROUPED_VIEW_KEY = 'dop-agent-grouped-view';
const COLLAPSED_GROUPS_KEY = 'dop-agent-collapsed-groups';
const DISMISSED_SUGGEST_KEY = 'dop-agent-dismissed-group-suggestions';
const GROUP_STATUS_ICON = { errored: '🔴', running: '🟡', unread: '🔵', idle: '⚪' };
const GROUP_STATUS_LABEL = { errored: 'has a failed session', running: 'something is running', unread: 'unread activity', idle: 'all idle' };

let groupFilter = localStorage.getItem(GROUP_FILTER_KEY) || 'all'; // 'all' | 'none' | <group id>
let groupedView = localStorage.getItem(GROUPED_VIEW_KEY) === '1';
let collapsedGroups = new Set(JSON.parse(localStorage.getItem(COLLAPSED_GROUPS_KEY) || '[]'));
let dismissedSuggestions = new Set(JSON.parse(localStorage.getItem(DISMISSED_SUGGEST_KEY) || '[]'));
let overviewGroupId = null;
const suggestionCache = new Map(); // conversation_id -> suggestion|null

const groupFiltersEl = document.getElementById('group-filters');
const groupBannerEl = document.getElementById('group-banner');
const groupOverviewEl = document.getElementById('group-overview');

function saveGroupPrefs() {
  localStorage.setItem(GROUP_FILTER_KEY, groupFilter);
  localStorage.setItem(GROUPED_VIEW_KEY, groupedView ? '1' : '0');
  localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify([...collapsedGroups]));
}

function groupById(id) { return groups.find((g) => g.id === id) || null; }

function sessionMatchesGroupFilter(s) {
  if (groupFilter === 'all') return true;
  if (groupFilter === 'none') return !s.group_id;
  return s.group_id === groupFilter;
}

// Active (non-archived) groups only; archived groups' sessions live under the Archived chip.
function visibleGroups() { return groups.filter((g) => !g.archived); }

function statusDot(g) {
  const st = g.status || 'idle';
  return `<span class="group-status-dot status-${st}" title="${GROUP_STATUS_LABEL[st]}">${GROUP_STATUS_ICON[st]}</span>`;
}

function renderGroupFilters() {
  if (groupFilter !== 'all' && groupFilter !== 'none' && !groupById(groupFilter)) groupFilter = 'all';
  const ungrouped = sessions.filter((s) => !s.group_id && !s.archived).length;
  const chipHtml = (g) => `<button type="button" class="group-chip ${groupFilter === g.id ? 'active' : ''}" data-group="${g.id}" style="--group-color:${escapeHtml(g.color || '#888')}" title="${escapeHtml(g.title)}">`
    + `${statusDot(g)}${g.emoji ? escapeHtml(g.emoji) + ' ' : ''}<span class="group-chip-title">${escapeHtml(g.title)}</span> <span class="group-chip-count">${g.session_count}</span></button>`;
  const cur = groupFilter === 'all' ? 'All groups' : groupFilter === 'none' ? `Ungrouped ${ungrouped}`
    : (() => { const g = groupById(groupFilter); return `${g.emoji ? g.emoji + ' ' : ''}${g.title}`; })();
  const items = [
    `<button type="button" class="group-chip ${groupFilter === 'all' ? 'active' : ''}" data-group="all">All groups</button>`,
    `<button type="button" class="group-chip ${groupFilter === 'none' ? 'active' : ''}" data-group="none">Ungrouped <span class="group-chip-count">${ungrouped}</span></button>`,
    ...visibleGroups().map(chipHtml),
  ];
  const wasOpen = groupFiltersEl.querySelector('.group-dropdown-menu:not([hidden])');
  groupFiltersEl.innerHTML =
    '<div class="group-filter-row">'
    + `<button type="button" class="group-dropdown-toggle" data-action="dropdown" title="Filter sessions by group">${escapeHtml(cur)} ▾</button>`
    + '<button type="button" class="group-chip group-chip-action" data-action="new" title="Create a new group">＋</button>'
    + `<button type="button" class="group-chip group-chip-action ${groupedView ? 'active' : ''}" data-action="toggle-view" title="Toggle grouped view (collapsible group headers)">☰ Grouped</button>`
    + '</div>'
    + `<div class="group-dropdown-menu" ${wasOpen ? '' : 'hidden'}>${items.join('')}</div>`;
}

function closeGroupDropdown() {
  const m = groupFiltersEl.querySelector('.group-dropdown-menu');
  if (m) m.hidden = true;
}
document.addEventListener('click', (e) => { if (!groupFiltersEl.contains(e.target)) closeGroupDropdown(); });

groupFiltersEl.addEventListener('click', async (e) => {
  const toggle = e.target.closest('.group-dropdown-toggle');
  if (toggle) { const m = groupFiltersEl.querySelector('.group-dropdown-menu'); m.hidden = !m.hidden; return; }
  const chip = e.target.closest('.group-chip');
  if (!chip) return;
  if (chip.dataset.action === 'new') { await createGroupPrompt(); return; }
  if (chip.dataset.action === 'toggle-view') { groupedView = !groupedView; saveGroupPrefs(); renderSessionList(); return; }
  groupFilter = chip.dataset.group;
  closeGroupDropdown();
  saveGroupPrefs();
  renderSessionList();
});
groupFiltersEl.addEventListener('dblclick', (e) => {
  const chip = e.target.closest('.group-chip[data-group]');
  if (chip && groupById(chip.dataset.group)) openGroupOverview(chip.dataset.group);
});
// Chips are also drop targets: drop a session on a group chip to move it there.
groupFiltersEl.addEventListener('dragover', (e) => {
  const chip = e.target.closest('.group-chip[data-group]');
  if (!chip || chip.dataset.group === 'all') return;
  e.preventDefault();
  chip.classList.add('drag-over');
});
groupFiltersEl.addEventListener('dragleave', (e) => {
  const chip = e.target.closest('.group-chip');
  if (chip) chip.classList.remove('drag-over');
});
groupFiltersEl.addEventListener('drop', async (e) => {
  const chip = e.target.closest('.group-chip[data-group]');
  if (!chip || chip.dataset.group === 'all') return;
  e.preventDefault();
  chip.classList.remove('drag-over');
  const id = e.dataTransfer.getData('text/plain');
  if (id) await assignGroup(id, chip.dataset.group === 'none' ? null : chip.dataset.group);
});

// ---- list ordering / headers ------------------------------------------------

// Master first, then manual drag-and-drop order (group_order), then the default list order.
function sortMasterFirst(list) {
  const idx = new Map(list.map((s, i) => [s.conversation_id, i]));
  const key = (s) => (s.group_order == null ? 1e6 + idx.get(s.conversation_id) : s.group_order);
  return [...list].sort((a, b) =>
    (b.is_group_master ? 1 : 0) - (a.is_group_master ? 1 : 0) || key(a) - key(b));
}

// Returns the sessions in display order. In grouped view, interleaves
// `{__groupHeader: ...}` pseudo-entries (master first inside each group).
function orderSessionsForDisplay(list) {
  if (!groupedView) {
    // Flat view: still keep a group's master ahead of its siblings when filtering to one group.
    return groupFilter !== 'all' && groupFilter !== 'none' ? sortMasterFirst(list) : list;
  }
  const out = [];
  for (const g of visibleGroups()) {
    const members = sortMasterFirst(list.filter((s) => s.group_id === g.id));
    if (!members.length && groupFilter !== g.id) continue;
    out.push({ __groupHeader: true, group: g, count: members.length });
    if (!collapsedGroups.has(g.id)) out.push(...members);
  }
  const rest = list.filter((s) => !s.group_id || !groupById(s.group_id) || groupById(s.group_id).archived);
  if (rest.length && out.length) out.push({ __groupHeader: true, group: null, count: rest.length });
  if (!collapsedGroups.has('__none')) out.push(...rest);
  return out;
}

function buildGroupHeader(entry) {
  const g = entry.group;
  const key = g ? g.id : '__none';
  const el = document.createElement('div');
  el.className = 'group-header' + (collapsedGroups.has(key) ? ' collapsed' : '');
  el.dataset.groupId = g ? g.id : 'none';
  if (g) el.style.setProperty('--group-color', g.color || '#888');
  if (g) {
    el.draggable = true;
    el.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('application/x-group-id', g.id);
      e.dataTransfer.setData('text/plain', '');
      e.dataTransfer.effectAllowed = 'move';
    });
  }
  el.innerHTML = `
    <span class="group-caret">${collapsedGroups.has(key) ? '▸' : '▾'}</span>
    ${g ? statusDot(g) : ''}
    <span class="group-header-title">${g && g.emoji ? escapeHtml(g.emoji) + ' ' : ''}${escapeHtml(g ? g.title : 'Ungrouped')}</span>
    <span class="group-chip-count">${entry.count}</span>
    ${g ? '<button type="button" class="group-header-btn" data-act="overview" title="Group overview">📊</button><button type="button" class="group-header-btn" data-act="menu" title="Group actions">⋯</button>' : ''}`;
  el.addEventListener('click', (e) => {
    const btn = e.target.closest('.group-header-btn');
    if (btn) {
      e.stopPropagation();
      if (btn.dataset.act === 'overview') openGroupOverview(g.id);
      else openGroupMenu(e.clientX, e.clientY, g);
      return;
    }
    if (collapsedGroups.has(key)) collapsedGroups.delete(key); else collapsedGroups.add(key);
    saveGroupPrefs();
    renderSessionList();
  });
  el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drag-over'); });
  el.addEventListener('dragleave', () => el.classList.remove('drag-over'));
  el.addEventListener('drop', async (e) => {
    e.preventDefault();
    el.classList.remove('drag-over');
    const draggedGroup = e.dataTransfer.getData('application/x-group-id');
    if (draggedGroup) {
      if (g && draggedGroup !== g.id) await reorderGroups(draggedGroup, g.id);
      return;
    }
    const id = e.dataTransfer.getData('text/plain');
    if (id) await assignGroup(id, g ? g.id : null);
  });
  return el;
}

function applyGroupDecoration(item, s) {
  const g = s.group_id ? groupById(s.group_id) : null;
  if (!g) return;
  item.classList.add('grouped');
  item.style.setProperty('--group-color', g.color || '#888');
  if (s.is_group_master) item.classList.add('group-master');
  item.dataset.groupId = g.id;
  if (!groupedView) {
    // Tag is appended after render (item.innerHTML is set later in app.js), so defer.
    queueMicrotask(() => {
      const m = item.querySelector('.session-meta');
      if (m && !m.querySelector('.session-group-tag')) {
        m.insertAdjacentHTML('beforeend', ` · <span class="session-group-tag"><i class="gd" style="background:${escapeHtml(g.color || '#888')}"></i>${escapeHtml(g.title)}</span>`);
      }
    });
  }
}

// ---- mutations ----------------------------------------------------------------

async function assignGroup(conversationId, groupId) {
  try {
    await api(`/api/sessions/${conversationId}/group`, { method: 'PUT', body: JSON.stringify({ group_id: groupId }) });
    await refreshSessionList();
  } catch (e) { alert('Could not change group: ' + e.message); }
}

async function createGroupPrompt(thenAssignId) {
  const title = (window.prompt('New group title:') || '').trim();
  if (!title) return null;
  try {
    const g = await api('/api/groups', { method: 'POST', body: JSON.stringify({ title }) });
    if (thenAssignId) await api(`/api/sessions/${thenAssignId}/group`, { method: 'PUT', body: JSON.stringify({ group_id: g.id }) });
    await refreshSessionList();
    return g;
  } catch (e) { alert('Could not create group: ' + e.message); return null; }
}

async function patchGroup(g, body) {
  try {
    await api(`/api/groups/${g.id}`, { method: 'PATCH', body: JSON.stringify(body) });
    await refreshSessionList();
    if (overviewGroupId === g.id) openGroupOverview(g.id);
  } catch (e) { alert('Could not update group: ' + e.message); }
}

// ---- context menus ---------------------------------------------------------------

let ctxMenuEl = null;
function closeCtxMenu() { if (ctxMenuEl) { ctxMenuEl.remove(); ctxMenuEl = null; } }
document.addEventListener('click', (e) => { if (ctxMenuEl && !ctxMenuEl.contains(e.target)) closeCtxMenu(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeCtxMenu(); });

function showCtxMenu(x, y, html) {
  closeCtxMenu();
  ctxMenuEl = document.createElement('div');
  ctxMenuEl.className = 'ctx-menu';
  ctxMenuEl.innerHTML = html;
  document.body.appendChild(ctxMenuEl);
  const r = ctxMenuEl.getBoundingClientRect();
  ctxMenuEl.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 4)) + 'px';
  ctxMenuEl.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 4)) + 'px';
  return ctxMenuEl;
}

// "Move to group…": searchable list + New group… + Remove from group (+ master toggle).
function openSessionContextMenu(x, y, s) {
  const menu = showCtxMenu(x, y, `
    <div class="ctx-title">Move to group…</div>
    <input type="text" class="ctx-search" placeholder="Search groups…" />
    <div class="ctx-list"></div>
    <div class="ctx-sep"></div>
    <button type="button" class="ctx-item" data-act="new">＋ New group…</button>
    ${s.group_id ? '<button type="button" class="ctx-item" data-act="remove">✖ Remove from group</button>' : ''}
    ${s.group_id ? `<button type="button" class="ctx-item" data-act="master">${s.is_group_master ? '☆ Unset as group master' : '★ Make group master'}</button>` : ''}`);
  const listEl = menu.querySelector('.ctx-list');
  const search = menu.querySelector('.ctx-search');
  const renderList = () => {
    const q = search.value.trim().toLowerCase();
    const items = visibleGroups().filter((g) => !q || g.title.toLowerCase().includes(q));
    listEl.innerHTML = items.map((g) => `<button type="button" class="ctx-item ${g.id === s.group_id ? 'current' : ''}" data-group="${g.id}"><span class="ctx-dot" style="background:${escapeHtml(g.color || '#888')}"></span>${g.emoji ? escapeHtml(g.emoji) + ' ' : ''}${escapeHtml(g.title)}</button>`).join('')
      || '<div class="ctx-empty">No groups match</div>';
  };
  search.addEventListener('input', renderList);
  renderList();
  search.focus();
  menu.addEventListener('click', async (e) => {
    const btn = e.target.closest('.ctx-item');
    if (!btn) return;
    closeCtxMenu();
    if (btn.dataset.group) await assignGroup(s.conversation_id, btn.dataset.group);
    else if (btn.dataset.act === 'new') await createGroupPrompt(s.conversation_id);
    else if (btn.dataset.act === 'remove') await assignGroup(s.conversation_id, null);
    else if (btn.dataset.act === 'master') await setMaster(s.group_id, s.is_group_master ? null : s.conversation_id);
  });
}

async function setMaster(groupId, conversationId) {
  try {
    await api(`/api/groups/${groupId}/master`, { method: 'PUT', body: JSON.stringify({ conversation_id: conversationId }) });
    await refreshSessionList();
  } catch (e) { alert('Could not set master: ' + e.message); }
}

function openGroupMenu(x, y, g) {
  const menu = showCtxMenu(x, y, `
    <div class="ctx-title">${escapeHtml(g.title)}</div>
    <button type="button" class="ctx-item" data-act="overview">📊 Overview</button>
    <button type="button" class="ctx-item" data-act="read">✅ Mark all as read</button>
    <button type="button" class="ctx-item" data-act="rename">✏️ Rename…</button>
    <button type="button" class="ctx-item" data-act="emoji">😀 Set emoji…</button>
    <button type="button" class="ctx-item" data-act="color">🎨 Set color…</button>
    <button type="button" class="ctx-item" data-act="archive">🗄️ Archive group (${g.total_count} sessions)…</button>
    <button type="button" class="ctx-item" data-act="delete">🗑️ Delete group (sessions stay)…</button>`);
  menu.addEventListener('click', async (e) => {
    const btn = e.target.closest('.ctx-item');
    if (!btn) return;
    closeCtxMenu();
    const act = btn.dataset.act;
    if (act === 'overview') openGroupOverview(g.id);
    else if (act === 'read') {
      try { await api(`/api/groups/${g.id}/read`, { method: 'PUT' }); await refreshSessionList(); }
      catch (err) { alert('Could not mark group read: ' + err.message); }
    }
    else if (act === 'rename') { const t = window.prompt('Group title:', g.title); if (t && t.trim()) await patchGroup(g, { title: t.trim() }); }
    else if (act === 'emoji') { const t = window.prompt('Emoji (empty to clear):', g.emoji || ''); if (t !== null) await patchGroup(g, { emoji: t.trim() }); }
    else if (act === 'color') { const t = window.prompt('Color (hex, e.g. #4f8cff):', g.color || ''); if (t && /^#[0-9a-fA-F]{3,8}$/.test(t.trim())) await patchGroup(g, { color: t.trim() }); }
    else if (act === 'archive') {
      if (window.confirm(`Archive group "${g.title}" and its ${g.total_count} session(s)?`)) {
        await api(`/api/groups/${g.id}/archive`, { method: 'PUT', body: JSON.stringify({ archived: true }) });
        if (groupFilter === g.id) groupFilter = 'all';
        await refreshSessionList();
      }
    } else if (act === 'delete') {
      if (window.confirm(`Delete group "${g.title}"? Its ${g.total_count} session(s) will become ungrouped.`)) {
        await api(`/api/groups/${g.id}`, { method: 'DELETE' });
        if (groupFilter === g.id) groupFilter = 'all';
        await refreshSessionList();
      }
    }
  });
}

// ---- in-session banner: group + sibling nav, or a "looks related" suggestion ---------

function renderGroupBanner(session) {
  if (!session) { groupBannerEl.style.display = 'none'; return; }
  const g = session.group_id ? groupById(session.group_id) : null;
  if (g) {
    const siblings = sortMasterFirst(sessions.filter((x) => x.group_id === g.id && !x.archived));
    const idx = siblings.findIndex((x) => x.conversation_id === session.conversation_id);
    const prev = idx > 0 ? siblings[idx - 1] : null;
    const next = idx >= 0 && idx < siblings.length - 1 ? siblings[idx + 1] : null;
    groupBannerEl.style.setProperty('--group-color', g.color || '#888');
    groupBannerEl.className = 'group-banner';
    groupBannerEl.innerHTML = `
      <span class="group-banner-label">${statusDot(g)}${g.emoji ? escapeHtml(g.emoji) + ' ' : ''}<b>${escapeHtml(g.title)}</b>
      <span class="group-banner-count">${idx >= 0 ? idx + 1 : '?'} / ${siblings.length}${session.is_group_master ? ' · ★ master' : ''}</span></span>
      <button type="button" class="group-banner-btn" data-nav="${prev ? prev.conversation_id : ''}" ${prev ? '' : 'disabled'} title="Previous session in group">‹ prev</button>
      <button type="button" class="group-banner-btn" data-nav="${next ? next.conversation_id : ''}" ${next ? '' : 'disabled'} title="Next session in group">next ›</button>
      <button type="button" class="group-banner-btn" data-overview="${g.id}">📊 Overview</button>`;
    groupBannerEl.style.display = 'flex';
    return;
  }
  maybeShowSuggestion(session);
}

groupBannerEl.addEventListener('click', async (e) => {
  const nav = e.target.closest('[data-nav]');
  if (nav && nav.dataset.nav) { openSession(nav.dataset.nav); return; }
  const ov = e.target.closest('[data-overview]');
  if (ov) { openGroupOverview(ov.dataset.overview); return; }
  const add = e.target.closest('[data-suggest-add]');
  if (add) { await assignGroup(add.dataset.session, add.dataset.suggestAdd); return; }
  const dismiss = e.target.closest('[data-suggest-dismiss]');
  if (dismiss) {
    dismissedSuggestions.add(dismiss.dataset.suggestDismiss);
    localStorage.setItem(DISMISSED_SUGGEST_KEY, JSON.stringify([...dismissedSuggestions]));
    groupBannerEl.style.display = 'none';
  }
});

// Suggestion only (never automatic): shown for an ungrouped session whose title
// shares a ticket key / distinctive words with an existing group.
async function maybeShowSuggestion(session) {
  groupBannerEl.style.display = 'none';
  const id = session.conversation_id;
  if (dismissedSuggestions.has(id) || session.archived || !visibleGroups().length || !session.turn_count) return;
  let suggestion;
  if (suggestionCache.has(id)) suggestion = suggestionCache.get(id);
  else {
    try {
      suggestion = (await api('/api/groups/suggest', { method: 'POST', body: JSON.stringify({ prompt: session.title || '' }) })).suggestion;
    } catch (e) { suggestion = null; }
    suggestionCache.set(id, suggestion);
  }
  if (!suggestion || currentConversationId !== id) return;
  const g = groupById(suggestion.group_id);
  if (!g || g.archived) return;
  groupBannerEl.style.setProperty('--group-color', g.color || '#888');
  groupBannerEl.className = 'group-banner group-banner-suggest';
  groupBannerEl.innerHTML = `
    <span class="group-banner-label">💡 Looks related to group <b>${g.emoji ? escapeHtml(g.emoji) + ' ' : ''}${escapeHtml(g.title)}</b></span>
    <button type="button" class="group-banner-btn" data-suggest-add="${g.id}" data-session="${id}">Add to group</button>
    <button type="button" class="group-banner-btn" data-suggest-dismiss="${id}">Dismiss</button>`;
  groupBannerEl.style.display = 'flex';
}

// ---- overview page ------------------------------------------------------------------

function hideGroupOverview() {
  overviewGroupId = null;
  groupOverviewEl.style.display = 'none';
}

const OV_OLDER_DAYS = 14;
let overviewShowOlder = false;
let overviewStatusFilter = 'all';

async function openGroupOverview(groupId) {
  overviewGroupId = groupId;
  let ov;
  try { ov = await api(`/api/groups/${groupId}/overview`); }
  catch (e) { alert('Could not load overview: ' + e.message); return; }
  if (window.Inbox) window.Inbox.leave();
  document.getElementById('empty-state').style.display = 'none';
  threadViewEl.style.display = 'none';
  currentConversationId = null;
  if (typeof saveLastSession === 'function') saveLastSession(null);
  renderGroupOverview(ov);
  groupOverviewEl.style.display = 'block';
  renderSessionList();
}

function ovSessionRow(s) {
  return `<tr class="ov-row" data-open="${s.conversation_id}">
    <td>${s.is_group_master ? '★ ' : ''}${escapeHtml(markdownToPlainText(s.title))}</td>
    <td><span class="status-pill status-${s.status}">${GROUP_STATUS_ICON[s.status]} ${s.status}</span></td>
    <td>${timeAgo(s.updated_at)}</td><td>${s.turn_count}</td></tr>`;
}

function renderGroupOverview(ov) {
  const g = ov.group;
  const cutoff = Date.now() - OV_OLDER_DAYS * 86400000;
  let rows = ov.sessions.filter((s) => !s.archived);
  if (overviewStatusFilter !== 'all') rows = rows.filter((s) => s.status === overviewStatusFilter);
  const isOld = (s) => !s.is_group_master && s.status === 'idle' && new Date(s.updated_at).getTime() < cutoff;
  const hidden = overviewShowOlder ? 0 : rows.filter(isOld).length;
  if (!overviewShowOlder) rows = rows.filter((s) => !isOld(s));
  const c = ov.rollup.counts;
  const artifactLink = (u) => `<a href="${escapeHtml(u)}" target="_blank" rel="noopener">${escapeHtml(u.replace(/^https?:\/\/[^/]+\//, ''))}</a>`;
  groupOverviewEl.style.setProperty('--group-color', g.color || '#888');
  groupOverviewEl.innerHTML = `
    <div class="ov-header">
      <h2>${g.emoji ? escapeHtml(g.emoji) + ' ' : ''}<span class="ov-title" title="Click to rename">${escapeHtml(g.title)}</span></h2>
      <div class="ov-totals">${['errored', 'running', 'unread', 'idle'].map((k) => `<span class="status-pill status-${k}">${GROUP_STATUS_ICON[k]} ${c[k]} ${k}</span>`).join(' ')}</div>
      <div class="ov-actions"><button type="button" class="group-banner-btn" data-ov="filter">Show only this group in list</button> <button type="button" class="group-banner-btn" data-ov="close">✕ Close</button></div>
    </div>
    ${g.description ? `<p class="ov-desc">${escapeHtml(g.description)}</p>` : '<p class="ov-desc ov-desc-empty" data-ov="desc">＋ Add a description of this group\'s goal</p>'}
    ${ov.attention.length ? `<section><h3>⚠️ Needs attention</h3><table class="ov-table">${ov.attention.map(ovSessionRow).join('')}</table></section>` : ''}
    <section><h3>Sessions <span class="group-chip-count">${rows.length}</span>
      <select id="ov-status-filter" class="ov-select">${['all', 'errored', 'running', 'unread', 'idle'].map((k) => `<option value="${k}" ${k === overviewStatusFilter ? 'selected' : ''}>${k}</option>`).join('')}</select></h3>
      <table class="ov-table"><tr><th>Title</th><th>Status</th><th>Last activity</th><th>Turns</th></tr>${rows.map(ovSessionRow).join('') || '<tr><td colspan="4" class="ov-empty">No sessions</td></tr>'}</table>
      ${hidden ? `<button type="button" class="group-banner-btn" data-ov="older">Show ${hidden} older idle session(s)</button>` : (overviewShowOlder ? '<button type="button" class="group-banner-btn" data-ov="older">Hide older idle sessions</button>' : '')}
    </section>
    <section><h3>Linked artifacts</h3>
      <div class="ov-cols"><div><b>Merge requests (${ov.artifacts.merge_requests.length})</b><ul>${ov.artifacts.merge_requests.map((u) => `<li>${artifactLink(u)}</li>`).join('') || '<li class="ov-empty">none</li>'}</ul></div>
      <div><b>Jira tickets (${ov.artifacts.tickets.length})</b><ul>${ov.artifacts.tickets.map((t) => `<li><a href="${escapeHtml((window.Furls && Furls.watchLinkUrl('ticket', t)) || '#')}" target="_blank" rel="noopener">${escapeHtml(t)}</a></li>`).join('') || '<li class="ov-empty">none</li>'}</ul></div></div>
    </section>
    <section><h3>Active watches (${ov.watches.length})</h3>
      <ul>${ov.watches.map((w) => `<li>#${w.id} <code>${escapeHtml(w.type)}</code> ${escapeHtml(w.key || '')} <span class="ov-empty">— ${escapeHtml(w.label || '')}</span></li>`).join('') || '<li class="ov-empty">none</li>'}</ul>
    </section>
    <section><h3>Timeline</h3>
      <ul class="ov-timeline">${ov.timeline.slice(0, 40).map((t) => `<li><span class="ov-empty">${timeAgo(t.at)}</span> <a href="#" data-open="${t.conversation_id}">${escapeHtml(markdownToPlainText(t.text))}</a></li>`).join('') || '<li class="ov-empty">nothing yet</li>'}</ul>
    </section>`;
}

groupOverviewEl.addEventListener('click', async (e) => {
  const open = e.target.closest('[data-open]');
  if (open) { e.preventDefault(); openSession(open.dataset.open); return; }
  const act = e.target.closest('[data-ov]');
  if (act) {
    const g = groupById(overviewGroupId);
    if (act.dataset.ov === 'close') { hideGroupOverview(); document.getElementById('empty-state').style.display = ''; renderSessionList(); }
    else if (act.dataset.ov === 'filter' && g) { groupFilter = g.id; saveGroupPrefs(); renderSessionList(); }
    else if (act.dataset.ov === 'older') { overviewShowOlder = !overviewShowOlder; openGroupOverview(overviewGroupId); }
    else if (act.dataset.ov === 'desc' && g) { const t = window.prompt('Describe this group\'s goal:'); if (t !== null) await patchGroup(g, { description: t.trim() }); }
    return;
  }
  if (e.target.closest('.ov-title')) {
    const g = groupById(overviewGroupId);
    const t = g && window.prompt('Group title:', g.title);
    if (t && t.trim()) await patchGroup(g, { title: t.trim() });
  }
});
groupOverviewEl.addEventListener('change', (e) => {
  if (e.target.id === 'ov-status-filter') { overviewStatusFilter = e.target.value; openGroupOverview(overviewGroupId); }
});


// ---- drag-and-drop reordering inside the grouped view ------------------------
function dropInsertsAfter(e, item) {
  const r = item.getBoundingClientRect();
  return e.clientY > r.top + r.height / 2;
}
function clearReorderMarks() {
  sessionListEl.querySelectorAll('.reorder-before,.reorder-after').forEach((el) => el.classList.remove('reorder-before', 'reorder-after'));
}
// Capture-phase listeners below pre-empt the pinned-reorder handlers attached to each row.
sessionListEl.addEventListener('dragover', (e) => {
  if (!groupedView) return;
  const item = e.target.closest('.session-item');
  if (!item) return;
  e.preventDefault();
  e.stopPropagation();
  clearReorderMarks();
  item.classList.add(dropInsertsAfter(e, item) ? 'reorder-after' : 'reorder-before');
}, true);
sessionListEl.addEventListener('dragleave', (e) => {
  if (groupedView && !sessionListEl.contains(e.relatedTarget)) clearReorderMarks();
}, true);
sessionListEl.addEventListener('drop', async (e) => {
  if (!groupedView) return;
  const item = e.target.closest('.session-item');
  if (!item) return;
  e.preventDefault();
  e.stopPropagation();
  const after = dropInsertsAfter(e, item);
  clearReorderMarks();
  const draggedId = e.dataTransfer.getData('text/plain');
  const targetId = item.dataset.conversationId;
  if (!draggedId || draggedId === targetId) return;
  await reorderInGroup(draggedId, targetId, after);
}, true);

async function reorderInGroup(draggedId, targetId, after) {
  const target = sessions.find((s) => s.conversation_id === targetId);
  const dragged = sessions.find((s) => s.conversation_id === draggedId);
  if (!target || !dragged) return;
  try {
    if ((dragged.group_id || null) !== (target.group_id || null)) {
      await api(`/api/sessions/${draggedId}/group`, { method: 'PUT', body: JSON.stringify({ group_id: target.group_id || null }) });
      dragged.group_id = target.group_id || null;
      dragged.is_group_master = false;
    }
    if (!target.group_id) { await refreshSessionList(); return; }
    const ids = sortMasterFirst(sessions.filter((s) => s.group_id === target.group_id && sessionMatchesFilter(s)))
      .map((s) => s.conversation_id).filter((id) => id !== draggedId);
    const at = ids.indexOf(targetId) + (after ? 1 : 0);
    ids.splice(at, 0, draggedId);
    await api('/api/groups/member-order', { method: 'PUT', body: JSON.stringify({ conversation_ids: ids }) });
    await refreshSessionList();
  } catch (err) { alert('Could not reorder: ' + err.message); }
}


// Dropping a group header on another header puts the dragged group right before it.
async function reorderGroups(draggedId, targetId) {
  const ids = visibleGroups().map((g) => g.id).filter((id) => id !== draggedId);
  ids.splice(ids.indexOf(targetId), 0, draggedId);
  try {
    await api('/api/groups/order', { method: 'PUT', body: JSON.stringify({ group_ids: ids }) });
    await refreshSessionList();
  } catch (err) { alert('Could not reorder groups: ' + err.message); }
}
