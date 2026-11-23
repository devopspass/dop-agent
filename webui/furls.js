// Link-hover status cards driven by GET /api/integrations/ui (the integration registry's
// secret-free projection). No per-integration knowledge lives here: URL regexes, url templates,
// status-emoji sets, card field lists, icons and watch types all come from the projection, so a
// new integration kind added in YAML works without JS changes. If the projection can't be
// loaded, nothing is recognised (no hover cards) and the page keeps working.
(function (root) {
  let projection = null;
  let compiled = [];
  let watchRes = {};

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function setProjection(p) {
    projection = p && Array.isArray(p.integrations) ? p : null;
    compiled = [];
    watchRes = {};
    if (!projection) return;
    for (const it of projection.integrations) {
      for (const [kname, k] of Object.entries(it.kinds || {})) {
        if (!k.url_match) continue;
        try { compiled.push({ re: new RegExp(k.url_match), it, kname, k }); } catch (e) { /* skip bad regex */ }
      }
    }
    for (const [type, meta] of Object.entries(projection.watch_types || {})) {
      try { watchRes[type] = new RegExp(meta.key_match_js); } catch (e) { /* skip */ }
    }
  }

  function load(fetchFn) {
    return Promise.resolve()
      .then(() => fetchFn('/api/integrations/ui'))
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(setProjection)
      .catch(() => setProjection(null));
  }

  function bare(href) { return String(href || '').split('#')[0].split('?')[0]; }

  // -> {it, kname, k, ident} | null
  function matchUrl(href) {
    const url = bare(href);
    for (const c of compiled) {
      const m = c.re.exec(url);
      if (m) return { it: c.it, kname: c.kname, k: c.k, ident: Object.assign({}, m.groups || {}) };
    }
    return null;
  }

  function fill(tpl, vals) {
    let missing = false;
    const out = String(tpl).replace(/\{(\w+)\}/g, (_, n) => {
      if (vals[n] == null || vals[n] === '') { missing = true; return ''; }
      return vals[n];
    });
    return missing ? '' : out;
  }

  // Browser URL of a watch (type + key) via the kind's url_template; '' when none (e.g. '@ref' keys).
  function watchLinkUrl(type, key) {
    key = (key || '').trim();
    const re = watchRes[type];
    if (!projection || !key || !re) return '';
    const m = re.exec(key);
    if (!m) return '';
    for (const it of projection.integrations) {
      for (const k of Object.values(it.kinds || {})) {
        if (k.watch_type === type && k.url_template) {
          return fill(k.url_template, Object.assign({}, m.groups || {}));
        }
      }
    }
    return '';
  }

  function emojiMap(integrationName) {
    const it = projection && projection.integrations.find((i) => i.name === integrationName);
    const map = {};
    for (const k of Object.values((it && it.kinds) || {})) Object.assign(map, k.status_emoji || {});
    return map;
  }

  function emojiFor(map, v) {
    if (typeof v !== 'string') return '';
    return map[v] || map[v.toLowerCase()] || '';
  }

  function kindSpec(data) {
    const it = projection && projection.integrations.find((i) => i.name === data.integration);
    return (it && it.kinds && it.kinds[data.kind]) || null;
  }

  function projectSlug(project) {
    const parts = String(project || '').split('/').filter(Boolean);
    return parts[parts.length - 1] || '';
  }

  // Card HTML from the generic /api/unfurl payload. Two layouts:
  //  - payload has a `project`: row1 = icon + repo slug; row2 = link label + status + title/fields
  //  - otherwise:               row1 = icon + link label + status; row2 = title (+ extra fields)
  function cardHtml(data) {
    const k = kindSpec(data);
    if (!k) return null;
    const map = emojiMap(data.integration);
    const ident = data.ident || {};
    const fields = data.fields || {};
    const label = (k.card_label && fill(k.card_label, ident)) || data.key || data.title || '';
    const status = data.status || '';
    const sEmoji = data.status_emoji || emojiFor(map, status);
    const seen = new Set([label, status, data.title, ...Object.values(ident)].filter(Boolean));
    const badges = [];
    const plain = [];
    for (const f of k.card_fields || []) {
      const v = fields[f];
      if (f === 'project' || v == null || v === '' || seen.has(String(v))) continue;
      seen.add(String(v));
      if (typeof v === 'boolean') badges.push(`${v ? '✅' : '⏳'} ${esc(f)}`);
      else if (emojiFor(map, v)) badges.push(`${emojiFor(map, v)} ${esc(v)}`);
      else plain.push(esc(v));
    }
    const statusTxt = [status ? `${sEmoji ? sEmoji + ' ' : ''}${esc(status)}` : '', ...badges].filter(Boolean).join(' · ');
    const statusSpan = statusTxt ? `<span class="unfurl-status">${statusTxt}</span>` : '';
    const icon = k.icon || data.driver || '';
    const iconHtml = `<img class="unfurl-icon" src="${esc(k.icon_url || '')}" alt="${esc(icon)}" onerror="this.style.display='none'" />`;
    const link = `<a href="${esc(data.url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
    const project = fields.project || data.project;
    if (project) {
      const rest = [data.title && data.title !== label ? esc(data.title) : '', ...plain].filter(Boolean);
      return `
      <div class="unfurl-row1">${iconHtml}<span class="unfurl-project">${esc(projectSlug(project)) || esc(icon)}</span></div>
      <div class="unfurl-row2">${link} ${statusSpan}${rest.length ? ' · ' + rest.join(' · ') : ''}</div>`;
    }
    const row2 = [data.title && data.title !== label ? esc(data.title) : '', ...plain].filter(Boolean).join(' · ');
    return `
      <div class="unfurl-row1">${iconHtml}${link} ${statusSpan}</div>
      <div class="unfurl-row2">${row2}</div>`;
  }

  // Watch type groups from the projection: [{type, emoji}] in registry order.
  function watchTypes() {
    return projection ? Object.entries(projection.watch_types || {}).map(([type, m]) => ({ type, emoji: m.emoji || '' })) : [];
  }

  // Package-driver watcher recipes (script watches with meta.watcher): [{id, emoji, title}], one per recipe id.
  function watcherRecipes() {
    const seen = new Map();
    for (const w of projection ? Object.values(projection.watchers || {}) : []) if (!seen.has(w.id)) seen.set(w.id, w);
    return [...seen.values()];
  }

  const api = { load, setProjection, matchUrl, watchLinkUrl, cardHtml, watchTypes, watcherRecipes, ready: Promise.resolve(),
                isLink: (href) => matchUrl(href) !== null, _state: () => projection };
  root.Furls = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
