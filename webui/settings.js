// Settings UI (tabs per config section, plus Integrations and Advanced) on top of the settings REST API
// (settings_api.py). The Browser tab (quiet hours, Recent window) stays in app.js/index.html.
// Nothing here ever receives a secret value: the API returns refs/state only, and secret inputs
// are write-only (cleared after every save/test, never re-rendered from a response).
(function () {
  'use strict';

  const SECTIONS = ['agent', 'transcription', 'adapters', 'mcp', 'watchers', 'unfurl'];
  const TRANSCRIPTION_RE = /^agent\.transcription\./;
  const tabOf = (path) => (TRANSCRIPTION_RE.test(path) ? 'transcription' : path.split('.')[0]);
  const SECTION_TITLES = { agent: 'Agent', adapters: 'Web adapter', mcp: 'MCP', watchers: 'Watchers', unfurl: 'Link cards', transcription: 'Transcription (Whisper & recordings)' };
  const isSec = (t) => SECTIONS.includes(t);
  const CHAT_ADAPTER_RE = /^adapters\.(slack|telegram)\./;
  const SECRET_MODES = [
    ['keep', 'Keep current'], ['env', 'Env variable'], ['file', 'Secret file (write)'],
    ['keychain', 'Keychain (write)'], ['ref', 'Reference'],
  ];
  const $ = (sel, root) => (root || document).querySelector(sel);
  const overlay = () => document.getElementById('settings-overlay');

  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden') n[k] = v;
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }

  async function sapi(method, path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (method !== 'GET') headers['X-Requested-With'] = 'dop';
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      return { ok: false, status: 0, data: { detail: 'Network error: ' + e.message } };
    }
    if (res.status === 401) { window.location.href = '/login'; }
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    return { ok: res.ok, status: res.status, data };
  }

  const errText = (r) => {
    const d = r.data || {};
    const list = (d.errors || []).map((e) => (e.path ? e.path + ': ' : '') + e.message);
    return [d.detail || 'HTTP ' + r.status].concat(list).join(' — ');
  };

  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const humanize = (s) => s.replace(/_/g, ' ');

  const S = {
    tab: 'integrations', loaded: {},
    settings: null, schema: null, rev: null, meta: null,
    dirty: {}, unlocked: {}, errors: {}, conflict: false, notice: null,
    vtimer: null,
    drivers: null, integrations: null, adapters: null,
    inner: null, q: '',
  };

  // ---------- generic UI bits ----------
  function banner(kind, ...kids) { return el('div', { class: 'sx-banner sx-' + kind, role: kind === 'bad' ? 'alert' : 'status' }, kids); }
  function badge(text, kind, title) { return el('span', { class: 'sx-badge' + (kind ? ' sx-b-' + kind : ''), title }, text); }
  function loading(msg) { return el('div', { class: 'sx-empty', role: 'status' }, msg || 'Loading…'); }
  function failure(msg, retry) {
    return el('div', { class: 'sx-empty sx-err', role: 'alert' }, el('div', { text: msg }),
      retry && el('button', { type: 'button', class: 'sx-btn', onclick: retry, text: 'Retry' }));
  }

  // Write-only secret input. get() -> undefined (no change) | null (remove) | secret-input object.
  function secretWidget({ state, ref, allowRemove, onChange, label }) {
    const isSet = state === 'set';
    const modes = SECRET_MODES.filter(([m]) => m !== 'keep' || isSet || allowRemove);
    const sel = el('select', { 'aria-label': (label || 'Secret') + ' storage' },
      modes.map(([m, t]) => el('option', { value: m, text: m === 'keep' && !isSet ? 'Not set' : t })));
    if (allowRemove && isSet) sel.append(el('option', { value: 'remove', text: 'Remove' }));
    sel.value = modes.some(([m]) => m === 'keep') ? 'keep' : 'env';
    const inp = (type, ph, aria) => el('input', { type, placeholder: ph, 'aria-label': aria, autocomplete: type === 'password' ? 'new-password' : 'off', spellcheck: 'false', oninput: fire });
    const envIn = inp('text', 'ENV_VAR_NAME', 'Environment variable name');
    const valIn = inp('password', 'secret value (write-only)', 'Secret value');
    const nameIn = inp('text', 'file name (optional)', 'Secret file name');
    const refIn = inp('text', '${env:NAME} | ${file:~/path} | ${keychain:service}', 'Secret reference');
    const status = el('div', { class: 'sx-hint', 'data-secret-state': state || 'unset' },
      isSet ? ['Currently set', ...(ref ? [' via ', el('code', { text: ref })] : [' (from environment)'])] : 'Not set');
    const inputs = el('div', { class: 'sx-secret-inputs' }, envIn, valIn, nameIn, refIn);
    function get() {
      const m = sel.value;
      if (m === 'keep') return undefined;
      if (m === 'remove') return null;
      if (m === 'env') return envIn.value.trim() ? { mode: 'env', env: envIn.value.trim() } : undefined;
      if (m === 'ref') return refIn.value.trim() ? { mode: 'ref', ref: refIn.value.trim() } : undefined;
      if (m === 'file') {
        if (valIn.value) return { mode: 'file', value: valIn.value, ...(nameIn.value.trim() ? { name: nameIn.value.trim() } : {}) };
        return nameIn.value.trim() ? { mode: 'file', name: nameIn.value.trim() } : undefined;
      }
      return valIn.value ? { mode: 'keychain', value: valIn.value } : undefined;
    }
    function show() {
      const m = sel.value;
      envIn.hidden = m !== 'env';
      valIn.hidden = !(m === 'file' || m === 'keychain');
      nameIn.hidden = m !== 'file';
      refIn.hidden = m !== 'ref';
      inputs.hidden = m === 'keep' || m === 'remove';
    }
    function fire() { show(); if (onChange) onChange(get()); }
    sel.addEventListener('change', () => { fire(); });
    show();
    const root = el('div', { class: 'sx-secret' }, status, el('div', { class: 'sx-secret-row' }, sel, inputs));
    return { root, get, reset() { sel.value = isSet ? 'keep' : (allowRemove ? 'keep' : 'env'); envIn.value = valIn.value = nameIn.value = refIn.value = ''; show(); }, clearValues() { valIn.value = ''; } };
  }

  // ---------- tabs / lifecycle ----------
  function showTab(name) {
    S.tab = name;
    document.querySelectorAll('#settings-tabs .sx-tab').forEach((b) => {
      const on = b.dataset.stab === name && !searching();
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = (on || (searching() && b.dataset.stab === name)) ? 0 : -1;
    });
    const paneName = (isSec(name) || searching()) ? 'general' : name;
    for (const p of ['browser', 'general', 'integrations', 'advanced']) {
      const pane = document.getElementById('settings-pane-' + p);
      if (pane) pane.hidden = p !== paneName;
    }
    if (searching()) { if (!S.loaded.general) loadGeneral(); else renderGeneral(); return; }
    if (isSec(name)) { if (!S.loaded.general) loadGeneral(); else renderGeneral(); }
    if (name === 'integrations') loadIntegrations();
    if (name === 'advanced') renderAdvanced();
  }

  const searching = () => S.q.trim() !== '';

  function settingMatches(path, q) {
    const st = S.settings[path], sch = S.schema[path] || {};
    const hay = [settingLabel(path), path, ...(st.env_vars || []), st.env_var, sch.description, SECTION_TITLES[tabOf(path)]];
    return hay.some((h) => h && String(h).toLowerCase().includes(q));
  }

  function setSearch(q) {
    const was = searching();
    S.q = q;
    const input = document.getElementById('settings-search');
    if (input && input.value !== q) input.value = q;
    if (searching() || was) showTab(S.tab);
  }

  function tabKeys(e) {
    const tabs = [...document.querySelectorAll('#settings-tabs .sx-tab')];
    const i = tabs.indexOf(document.activeElement);
    if (i < 0 || !['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    const n = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[n].focus();
    showTab(tabs[n].dataset.stab);
  }

  function dirtyCount() { return Object.keys(S.dirty).length; }
  function guardLeave(next, to) {
    if (isSec(S.tab) && isSec(to)) { next(); return; }
    if (dirtyCount() && !window.confirm('You have unsaved changes in settings. Discard them?')) return;
    if (dirtyCount()) { S.dirty = {}; S.errors = {}; S.conflict = false; S.loaded.general = false; }
    next();
  }

  const api = {
    onOpen() { showTab(S.tab); },
    onClose() { closeInner(); },
    canClose() {
      if (dirtyCount() && !window.confirm('You have unsaved changes in settings. Close and discard them?')) return false;
      if (dirtyCount()) { S.dirty = {}; S.errors = {}; S.conflict = false; S.loaded.general = false; }
      return true;
    },
    hasInnerDialog() { return !!S.inner; },
    // Deep link entry (#settings=<tab>&q=<text>): select a tab and/or prefill the search box.
    openTo(tab, q) {
      const known = [...document.querySelectorAll('#settings-tabs .sx-tab')].map((b) => b.dataset.stab);
      if (tab && known.includes(tab)) S.tab = tab;
      S.q = q || '';
      const input = document.getElementById('settings-search');
      if (input) input.value = S.q;
      if (tab !== 'advanced') S.loaded.advanced = false;
      showTab(S.tab);
    },
  };
  window.DopSettings = api;

  // ---------- General ----------
  function flattenSchema(node, prefix, out) {
    for (const [k, v] of Object.entries((node && node.properties) || {})) {
      const path = prefix ? prefix + '.' + k : k;
      if (v.properties) flattenSchema(v, path, out); else out[path] = v;
    }
    return out;
  }

  async function loadGeneral() {
    const pane = document.getElementById('settings-pane-general');
    pane.replaceChildren(loading());
    const [s, sc] = await Promise.all([sapi('GET', '/api/settings'), sapi('GET', '/api/settings/schema')]);
    if (!s.ok || !sc.ok) { pane.replaceChildren(failure('Could not load settings: ' + errText(s.ok ? sc : s), loadGeneral)); return; }
    S.settings = s.data.settings; S.meta = s.data; S.rev = s.data.revision;
    S.schema = flattenSchema(sc.data, '', {});
    S.dirty = {}; S.errors = {}; S.conflict = false; S.unlocked = {};
    S.loaded.general = true;
    renderGeneral();
  }

  function curValue(path) { return S.settings[path].value; }

  function setDirty(path, val, equalToOrig) {
    if (equalToOrig) delete S.dirty[path]; else S.dirty[path] = val;
    updateSaveBar();
    scheduleValidate();
  }

  function fieldControl(path, st, sch) {
    const locked = st.source === 'env' && !S.unlocked[path];
    const id = 'sx-f-' + path.replace(/\./g, '-');
    const common = { id, 'data-path': path, disabled: locked };
    if (st.secret) {
      const w = secretWidget({
        state: st.state === 'set' ? 'set' : 'unset', ref: st.ref, allowRemove: true, label: path,
        onChange: (v) => setDirty(path, v, v === undefined),
      });
      w.root.dataset.path = path;
      if (locked) w.root.querySelectorAll('select,input').forEach((n) => { n.disabled = true; });
      return w.root;
    }
    const orig = st.value;
    const shown = path in S.dirty ? S.dirty[path] : orig;
    if (st.type === 'bool') {
      return el('input', { ...common, type: 'checkbox', checked: !!shown, onchange: (e) => setDirty(path, e.target.checked, e.target.checked === !!orig) });
    }
    if (sch && sch.enum) {
      const s = el('select', { ...common, onchange: (e) => setDirty(path, e.target.value, e.target.value === orig) },
        sch.enum.map((o) => el('option', { value: o, text: o })));
      s.value = shown;
      return s;
    }
    if (st.type === 'int' || st.type === 'float') {
      return el('input', {
        ...common, type: 'number', step: st.type === 'int' ? '1' : 'any', value: shown == null ? '' : shown,
        placeholder: st.default == null ? '' : String(st.default),
        oninput: (e) => {
          const raw = e.target.value.trim();
          const v = raw === '' ? null : Number(raw);
          setDirty(path, v, same(v, orig));
        },
      });
    }
    if (st.type === 'list') {
      return el('input', {
        ...common, type: 'text', value: (shown || []).join(', '), placeholder: 'comma separated',
        oninput: (e) => {
          const v = e.target.value.split(',').map((x) => x.trim()).filter(Boolean);
          setDirty(path, v, same(v, orig || []));
        },
      });
    }
    return el('input', { ...common, type: 'text', value: shown == null ? '' : shown, placeholder: st.default == null ? '' : String(st.default), autocomplete: 'off', oninput: (e) => setDirty(path, e.target.value, e.target.value === (orig == null ? '' : orig)) });
  }

  function settingLabel(path) {
    const sch = S.schema[path] || {};
    const rest = path.split('.').slice(1).join(' › ');
    return (sch.title && sch.title !== path.split('.').pop() ? sch.title : humanize(rest)) || path;
  }

  function fieldRow(path) {
    const st = S.settings[path];
    const sch = S.schema[path] || {};
    const label = settingLabel(path);
    const badges = el('span', { class: 'sx-badges' },
      badge(st.source, st.source === 'env' ? 'ovr' : st.source === 'file' ? 'file' : '', 'Effective value comes from: ' + st.source),
      st.restart_required && badge('restart required', 'restart', st.reason || 'applies after daemon restart'),
      !st.restart_required && badge('hot', 'hot', 'applies on save'));
    const lockedNow = st.source === 'env' && !S.unlocked[path];
    const row = el('div', { class: 'sx-row', 'data-row': path },
      el('div', { class: 'sx-lbl' },
        el('label', { for: 'sx-f-' + path.replace(/\./g, '-'), text: label }),
        sch.description && el('span', { class: 'sx-info', tabindex: '0', role: 'img', 'aria-label': sch.description, 'data-tip': sch.description, text: 'ⓘ' }),
        el('code', { class: 'sx-env', text: (st.env_vars && st.env_vars[0]) || path }),
        badges),
      el('div', { class: 'sx-ctl' },
        fieldControl(path, st, sch),
        lockedNow && el('div', { class: 'sx-hint' }, '🔒 Set by env var ', el('code', { text: st.env_var || '' }), '. ',
          el('button', { type: 'button', class: 'sx-link', onclick: () => { S.unlocked[path] = true; renderGeneral(true); }, text: 'Edit file value anyway' }),
          ' (env still wins until unset)'),
        st.source === 'env' && S.unlocked[path] && el('div', { class: 'sx-hint sx-warn-t', text: 'Env var still overrides what you save here.' }),
        el('div', { class: 'sx-errmsg', 'data-err': path, role: 'alert' })));
    return row;
  }

  function renderGeneral(keepScroll) {
    const pane = document.getElementById('settings-pane-general');
    const top = pane.parentElement.scrollTop;
    const m = S.meta;
    const kids = [];
    kids.push(el('div', { class: 'sx-sub' }, 'Config file ', el('code', { text: m.config_path }), m.file_present ? '' : ' (not created yet — saving creates it)', ' · rev ', el('code', { text: m.revision })));
    if (!m.mode_ok) kids.push(banner('warn', 'Config file permissions are looser than 600.'));
    for (const e of m.file_errors || []) kids.push(banner('bad', 'Config file error: ' + e));
    if ((m.restart_pending || []).length) kids.push(banner('warn', 'Restart pending for: ', m.restart_pending.map((r) => r.path).join(', ')));
    if (S.notice) kids.push(S.notice);
    if (S.conflict) {
      kids.push(el('div', { class: 'sx-banner sx-bad', id: 'sx-conflict', role: 'alert' },
        el('b', { text: 'Config changed on disk (revision conflict). ' }),
        'Your edits were not saved. ',
        el('button', { type: 'button', class: 'sx-btn', 'data-act': 'conflict-reload', onclick: async () => { const keep = Object.fromEntries(Object.entries(S.dirty).filter(([p]) => !S.settings[p].secret)); await loadGeneral(); S.dirty = keep; renderGeneral(); scheduleValidate(); }, text: 'Reload & keep my edits' }), ' ',
        el('button', { type: 'button', class: 'sx-btn', 'data-act': 'conflict-discard', onclick: loadGeneral, text: 'Discard mine' })));
    }
    const bySection = {};
    for (const path of Object.keys(S.settings).filter((x) => !CHAT_ADAPTER_RE.test(x))) (bySection[tabOf(path)] = bySection[tabOf(path)] || []).push(path);
    if (searching()) {
      const q = S.q.trim().toLowerCase();
      let hits = 0;
      for (const sec of SECTIONS) {
        const paths = (bySection[sec] || []).filter((p) => settingMatches(p, q));
        if (!paths.length) continue;
        hits += paths.length;
        kids.push(el('section', { class: 'sx-card', 'data-section': sec },
          el('h3', { class: 'sx-hd', text: SECTION_TITLES[sec] || sec }), paths.map(fieldRow)));
      }
      if (!hits) kids.push(el('div', { class: 'sx-empty', id: 'sx-no-match', role: 'status', text: 'No matching settings' }));
    } else for (const sec of [S.tab]) {
      if (!bySection[sec]) { kids.push(el('div', { class: 'sx-empty', text: 'No settings in this section.' })); continue; }
      kids.push(el('section', { class: 'sx-card', 'data-section': sec },
        el('h3', { class: 'sx-hd', text: SECTION_TITLES[sec] || sec }), bySection[sec].map(fieldRow)));
    }
    kids.push(el('div', { class: 'sx-savebar', id: 'sx-savebar' },
      el('span', { id: 'sx-dirty-count', class: 'sx-grow' }),
      el('button', { type: 'button', class: 'sx-btn', id: 'sx-revert', onclick: () => { S.dirty = {}; S.errors = {}; S.conflict = false; renderGeneral(); }, text: 'Revert' }),
      el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-save', onclick: saveGeneral, text: 'Save' })));
    pane.replaceChildren(...kids);
    pane.parentElement.scrollTop = keepScroll ? top : 0;
    updateSaveBar();
    if (Object.keys(S.errors).length) showErrors();
  }

  function updateSaveBar() {
    const n = dirtyCount();
    const c = document.getElementById('sx-dirty-count');
    if (!c) return;
    const nErr = Object.keys(S.errors).length;
    c.textContent = n ? `${n} unsaved change${n > 1 ? 's' : ''}${nErr ? ` · ${nErr} invalid` : ''}` : 'No unsaved changes';
    document.getElementById('sx-save').disabled = !n || nErr > 0;
    document.getElementById('sx-revert').disabled = !n && !S.conflict;
    document.querySelectorAll('[data-row]').forEach((r) => r.classList.toggle('sx-dirty', r.dataset.row in S.dirty));
  }

  function showErrors() {
    document.querySelectorAll('[data-err]').forEach((n) => { n.textContent = S.errors[n.dataset.err] || ''; n.parentElement.parentElement.classList.toggle('sx-invalid', !!S.errors[n.dataset.err]); });
    updateSaveBar();
  }

  function scheduleValidate() {
    clearTimeout(S.vtimer);
    if (!dirtyCount()) { S.errors = {}; showErrors(); return; }
    S.vtimer = setTimeout(async () => {
      const r = await sapi('POST', '/api/settings/validate', { revision: S.rev, changes: S.dirty });
      S.errors = {};
      if (r.data && !r.data.valid && r.ok !== undefined) {
        for (const e of r.data.errors || []) S.errors[e.path] = (S.errors[e.path] ? S.errors[e.path] + '; ' : '') + e.message;
        if (!(r.data.errors || []).length && !r.ok) S.errors[Object.keys(S.dirty)[0]] = errText(r);
      }
      showErrors();
    }, 350);
  }

  async function saveGeneral(force) {
    const btn = document.getElementById('sx-save');
    btn.disabled = true;
    const body = { changes: S.dirty };
    if (force !== true) body.revision = S.rev;
    const r = await sapi('PATCH', '/api/settings', body);
    if (r.status === 409) {
      S.conflict = true;
      const cur = r.data && r.data.revision;
      renderGeneral(true);
      if (cur) document.getElementById('sx-conflict').append(' (current revision ', el('code', { text: cur }), ')');
      return;
    }
    if (!r.ok) {
      S.errors = {};
      for (const e of (r.data && r.data.errors) || []) S.errors[e.path] = e.message;
      if (!Object.keys(S.errors).length) S.notice = banner('bad', errText(r));
      else S.notice = null;
      renderGeneral(true); showErrors();
      return;
    }
    const d = r.data;
    const bits = [`Saved ${d.applied.length} setting${d.applied.length === 1 ? '' : 's'}.`];
    if (d.restart_required.length) bits.push(' Restart required for: ' + d.restart_required.map((x) => x.path).join(', ') + '.');
    if (d.env_overrides && d.env_overrides.length) bits.push(' Env still overrides: ' + d.env_overrides.map((x) => `${x.path} (${x.env_var})`).join(', ') + '.');
    S.notice = banner(d.restart_required.length || (d.env_overrides || []).length ? 'warn' : 'ok', bits.join(''));
    await loadGeneral();
    S.notice = S.notice || null;
    renderGeneral();
    refreshFurls();
  }

  function refreshFurls() {
    if (window.Furls && window.Furls.load) window.Furls.load((u) => fetch(u));
  }

  // ---------- Integrations ----------
  async function loadIntegrations() {
    const pane = document.getElementById('settings-pane-integrations');
    if (!S.integrations) pane.replaceChildren(loading());
    const [i, d, a] = await Promise.all([sapi('GET', '/api/integrations'), S.drivers ? { ok: true, data: S.drivers } : sapi('GET', '/api/drivers'), sapi('GET', '/api/adapters')]);
    if (!i.ok || !d.ok) { pane.replaceChildren(failure('Could not load integrations: ' + errText(i.ok ? d : i), loadIntegrations)); return; }
    S.integrations = i.data; S.drivers = d.data; S.adapters = a.ok ? a.data.adapters : [];
    renderIntegrations();
    scheduleStatusRefresh();
  }

  let statusTimer = null;
  let statusTries = 0;
  function scheduleStatusRefresh() {
    clearTimeout(statusTimer);
    const pending = (S.integrations || []).some((it) => it.status && (it.status.state === 'unknown' || it.status.state === 'checking'));
    if (!pending) { statusTries = 0; return; }
    if (statusTries >= 5) return;
    const delay = Math.min(2000 * Math.pow(1.5, statusTries), 8000);
    statusTries += 1;
    statusTimer = setTimeout(() => {
      const pane = document.getElementById('settings-pane-integrations');
      if (pane && pane.offsetParent !== null) loadIntegrations();
    }, delay);
  }

  const driverOf = (id) => (S.drivers || []).find((d) => d.id === id);
  const STATUS_KIND = { ok: 'ok', error: 'bad', invalid: 'bad', disabled: 'off', unknown: 'off', checking: 'warn' };

  function driverIcon(drv) {
    const url = drv && drv.icon_url || '';
    const box = el('span', { class: 'sx-ico', 'aria-hidden': 'true' });
    if (url) {
      const img = el('img', { src: url, alt: '', width: 20, height: 20 });
      img.addEventListener('error', () => { img.remove(); box.textContent = (drv.title || '?')[0].toUpperCase(); });
      box.append(img);
    } else box.textContent = (drv && drv.title || '?')[0].toUpperCase();
    return box;
  }

  function renderIntegrations() {
    const pane = document.getElementById('settings-pane-integrations');
    const list = S.integrations;
    const head = el('div', { class: 'sx-toolbar' },
      el('span', { class: 'sx-grow sx-sub', text: `${list.length} integration${list.length === 1 ? '' : 's'}` }),
      el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-add-int', onclick: () => openWizard(null), text: '+ Add integration' }));
    let body;
    if (!list.length) {
      body = el('div', { class: 'sx-empty' }, 'No integrations configured yet. Add an integration to enable link hover cards and watches.');
    } else {
      body = el('section', { class: 'sx-card' }, list.map((it) => {
        const drv = driverOf(it.driver);
        const st = it.status || {};
        const tog = el('input', { type: 'checkbox', class: 'sx-switch', checked: it.enabled, 'aria-label': `Enable ${it.name}`, 'data-toggle': it.name,
          onchange: async (e) => {
            tog.disabled = true;
            const r = await sapi('PATCH', '/api/integrations/' + encodeURIComponent(it.name), { enabled: e.target.checked });
            if (!r.ok) { e.target.checked = !e.target.checked; S.notice = banner('bad', errText(r)); }
            else refreshFurls();
            await loadIntegrations();
          } });
        return el('div', { class: 'sx-irow', 'data-int': it.name },
          driverIcon(drv),
          el('div', { class: 'sx-grow' },
            el('div', { class: 'sx-nm' }, it.name, ' ', el('span', { class: 'sx-meta', text: it.title || it.driver })),
            el('div', { class: 'sx-meta' }, it.base_url || 'no base URL', ' · ', `${it.watch_count} watch${it.watch_count === 1 ? '' : 'es'}`),
            !it.valid && it.errors.length ? el('div', { class: 'sx-errmsg', text: it.errors.join('; ') }) : null),
          el('span', { class: 'sx-status', title: st.detail || '' }, el('span', { class: 'sx-dot sx-d-' + (STATUS_KIND[st.state] || 'off') }), ' ', st.state === 'checking' ? 'checking…' : (st.state || 'unknown')),
          tog,
          el('button', { type: 'button', class: 'sx-btn', 'data-act': 'edit', onclick: () => openWizard(it), text: 'Edit' }),
          el('button', { type: 'button', class: 'sx-btn sx-danger', 'data-act': 'delete', onclick: () => openDelete(it), text: 'Delete' }));
      }));
    }
    pane.replaceChildren(S.notice || '', adaptersCard(), head, body);
    S.notice = null;
  }

  // ---- chat adapters (Slack / Telegram): fixed singletons, edited in a modal ----
  function adaptersCard() {
    const list = S.adapters || [];
    if (!list.length) return '';
    const pending = list.some((a) => (a.restart_pending || []).length);
    return el('section', { class: 'sx-card', id: 'sx-adapters' },
      el('h3', { class: 'sx-hd', text: 'Chat adapters' }),
      pending ? banner('warn', 'Adapter changes apply after a daemon restart (Advanced → Restart bot).') : '',
      list.map((a) => {
        const st = a.status || {};
        const tog = el('input', { type: 'checkbox', class: 'sx-switch', checked: a.enabled, disabled: !a.configured, title: a.configured ? '' : 'Configure the adapter first', 'aria-label': `Enable ${a.title}`, 'data-adapter-toggle': a.id,
          onchange: async (e) => {
            tog.disabled = true;
            const r = await sapi('PATCH', '/api/adapters/' + a.id, { enabled: e.target.checked });
            if (!r.ok) { e.target.checked = !e.target.checked; S.notice = banner('bad', errText(r)); }
            await loadIntegrations();
          } });
        return el('div', { class: 'sx-irow', 'data-adapter': a.id },
          driverIcon({ title: a.title, icon_url: a.icon_url || '' }),
          el('div', { class: 'sx-grow' },
            el('div', { class: 'sx-nm' }, a.title, ' ', el('span', { class: 'sx-meta', text: a.running ? 'running' : 'not running' })),
            el('div', { class: 'sx-meta', text: a.help }),
            !a.valid && a.errors.length ? el('div', { class: 'sx-errmsg', text: a.errors.join('; ') }) : null),
          el('span', { class: 'sx-status', title: st.detail || '' }, el('span', { class: 'sx-dot sx-d-' + (STATUS_KIND[st.state] || 'off') }), ' ', st.state === 'disabled' && st.detail ? `off — ${st.detail}` : (st.state || 'unknown')),
          tog,
          el('button', { type: 'button', class: 'sx-btn', 'data-act': 'edit-adapter', onclick: () => openAdapter(a), text: 'Edit' }));
      }));
  }

  function openAdapter(a) {
    const ctl = {};      // field -> { get(): value|undefined, secret }
    const draft = () => {
      const fields = {};
      for (const [f, c] of Object.entries(ctl)) { const v = c.get(); if (v !== undefined) fields[f] = v; }
      return fields;
    };
    const rows = Object.entries(a.fields).map(([f, v]) => {
      const label = humanize(f) + (a.required.includes(f) ? ' *' : '');
      if (v.secret) {
        const w = secretWidget({ state: v.state, ref: v.ref, allowRemove: true, label: label });
        ctl[f] = { get: w.get, w };
        return inputRow(label, w.root);
      }
      const isList = v.type === 'list';
      const cur = v.value == null ? '' : (isList ? (v.value || []).join(', ') : String(v.value));
      const inp = el('input', { type: v.type === 'int' || v.type === 'float' ? 'number' : 'text', value: cur, 'aria-label': label, 'data-field': f, autocomplete: 'off', step: v.type === 'float' ? 'any' : null });
      ctl[f] = { get: () => {
        if (inp.value === cur) return undefined;
        const t = inp.value.trim();
        if (isList) return t ? t.split(',').map((x) => x.trim()).filter(Boolean) : [];
        if (t === '') return null;
        return v.type === 'int' ? parseInt(t, 10) : v.type === 'float' ? parseFloat(t) : t;
      } };
      return inputRow(label, inp, v.restart_required ? 'applies after restart' : null);
    });
    const out = el('div', { id: 'sx-adapter-out' });
    const testBtn = el('button', { type: 'button', class: 'sx-btn', id: 'sx-adapter-test', text: 'Test connection' });
    const saveBtn = el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-adapter-save', text: 'Save' });
    const secretsClear = () => Object.values(ctl).forEach((c) => c.w && c.w.clearValues());
    testBtn.addEventListener('click', async () => {
      testBtn.disabled = true; out.replaceChildren(loading('Testing…'));
      const r = await sapi('POST', '/api/adapters/test', { id: a.id, fields: draft() });
      testBtn.disabled = false; secretsClear();
      if (!r.ok) { out.replaceChildren(banner('bad', errText(r))); return; }
      const d = r.data;
      out.replaceChildren(d.ok ? banner('ok', `Connected as ${d.identity} (${d.latency_ms} ms)`) : banner('bad', `${d.error.code}: ${d.error.message}`));
    });
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      const r = await sapi('PATCH', '/api/adapters/' + a.id, { fields: draft() });
      saveBtn.disabled = false; secretsClear();
      if (!r.ok) { out.replaceChildren(banner('bad', errText(r))); return; }
      closeInner();
      const rr = r.data.restart_required || [];
      S.notice = banner(rr.length ? 'warn' : 'ok', `Saved ${a.title}.` + (rr.length ? ' Restart the daemon to apply.' : ''));
      loadIntegrations();
    });
    openModal(`Edit ${a.title}`, [el('p', { class: 'sx-hint', text: a.help }), ...rows, out],
      [el('span', { class: 'sx-grow' }), el('button', { type: 'button', class: 'sx-btn', onclick: closeInner, text: 'Cancel' }), testBtn, saveBtn]);
  }

  // ---- modal infrastructure (inside the settings overlay, focus-trapped) ----
  function openModal(titleText, content, footer) {
    closeInner();
    const prev = document.activeElement;
    const dlg = el('div', { class: 'sx-modal-bg' },
      el('div', { class: 'sx-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': titleText },
        el('div', { class: 'sx-modal-hd' }, el('h3', { text: titleText }), el('button', { type: 'button', class: 'sx-x', 'aria-label': 'Close dialog', onclick: closeInner, text: '✕' })),
        el('div', { class: 'sx-modal-body' }, content),
        el('div', { class: 'sx-modal-ft' }, footer)));
    dlg.addEventListener('mousedown', (e) => { if (e.target === dlg) closeInner(); });
    overlay().append(dlg);
    S.inner = { node: dlg, prev };
    const f = dlg.querySelector('input:not([hidden]):not([disabled]),select,button.sx-card-btn');
    (f || dlg.querySelector('.sx-x')).focus();
    return dlg;
  }
  function closeInner() {
    if (!S.inner) return;
    S.inner.node.remove();
    const p = S.inner.prev; S.inner = null;
    if (p && p.focus && document.contains(p)) p.focus();
  }
  function replaceModal(content, footer, title) {
    const m = S.inner.node.querySelector('.sx-modal');
    m.querySelector('.sx-modal-body').replaceChildren(content);
    m.querySelector('.sx-modal-ft').replaceChildren(...[].concat(footer));
    if (title) { m.querySelector('h3').textContent = title; m.setAttribute('aria-label', title); }
  }

  document.addEventListener('keydown', (e) => {
    if (overlay().style.display === 'none') return;
    if (e.key === 'Escape' && S.inner) { e.preventDefault(); closeInner(); return; }
    if (e.key === 'Tab') {
      const scope = S.inner ? S.inner.node : overlay();
      const f = [...scope.querySelectorAll('button,input,select,textarea,a[href],[tabindex]')]
        .filter((n) => !n.disabled && !n.hidden && n.tabIndex >= 0 && n.offsetParent !== null);
      if (!f.length) return;
      const first = f[0]; const last = f[f.length - 1];
      if (!scope.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // ---- delete ----
  function openDelete(it) {
    const input = el('input', { type: 'text', id: 'sx-del-confirm', autocomplete: 'off', 'aria-label': 'Type the integration name to confirm', placeholder: it.name });
    const go = el('button', { type: 'button', class: 'sx-btn sx-danger', id: 'sx-del-go', disabled: true, text: 'Delete integration' });
    input.addEventListener('input', () => { go.disabled = input.value !== it.name; });
    const msg = el('div', { class: 'sx-errmsg' });
    go.addEventListener('click', async () => {
      go.disabled = true;
      const r = await sapi('DELETE', `/api/integrations/${encodeURIComponent(it.name)}?confirm=${encodeURIComponent(it.name)}`);
      if (!r.ok) { msg.textContent = errText(r); go.disabled = false; return; }
      closeInner();
      S.notice = banner('ok', `Deleted ${it.name}.` + (r.data.paused_watches ? ` ${r.data.paused_watches} watch(es) paused.` : ''));
      refreshFurls();
      loadIntegrations();
    });
    openModal(`Delete ${it.name}?`, [
      banner('warn', it.watch_count ? `${it.watch_count} watch(es) depend on this integration and will be paused.` : 'Link hover cards for this integration will stop working.'),
      el('p', {}, 'Type ', el('code', { text: it.name }), ' to confirm.'), input, msg,
    ], [el('button', { type: 'button', class: 'sx-btn', onclick: closeInner, text: 'Cancel' }), go]);
  }

  // ---- add/edit wizard ----
  let W = null;

  function openWizard(it) {
    const edit = !!it;
    W = { edit, step: edit ? 2 : 1, driver: edit ? driverOf(it.driver) : null, name: edit ? it.name : '', base_url: edit ? it.base_url : '', origBase: edit ? it.base_url : '',
      authType: edit ? it.auth.type : null, fieldVals: {}, secrets: {}, options: edit ? { ...it.options } : {},
      kinds: edit ? JSON.parse(JSON.stringify(it.kinds || {})) : {}, orig: it, test: null, enabled: edit ? it.enabled : true };
    openModal(edit ? `Edit ${it.name}` : 'Add integration', '', []);
    if (edit) W.fieldVals = Object.fromEntries(Object.entries(it.auth.fields).filter(([, f]) => !f.secret).map(([k, f]) => [k, f.value || '']));
    drawWizard();
  }

  const STEPS = ['Driver', 'Connection', 'Test', 'Watches'];
  function stepBar() {
    return el('ol', { class: 'sx-steps' }, STEPS.map((s, i) => el('li', { class: (i + 1 === W.step ? 'on' : i + 1 < W.step ? 'done' : ''), text: `${i + 1}. ${s}` })));
  }

  // Changing base_url of an instance that holds secrets would send them to the new host: ask first.
  function confirmHostChange(body) {
    if (!W.edit || (body.base_url || '') === (W.origBase || '')) return true;
    if (!window.confirm(`Change base URL from "${W.origBase || '(none)'}" to "${body.base_url || '(none)'}"?\nThe saved credentials of this integration will be sent to the new host.`)) return false;
    body.confirm_base_url = true;
    return true;
  }

  function bodyOf() {
    const d = W.driver;
    const body = { name: W.name.trim(), driver: d.id, enabled: W.enabled, auth: { type: W.authType, fields: {} } };
    if ('base_url' in d.settings) body.base_url = W.base_url.trim();
    const at = d.auth_types[W.authType];
    for (const [fname, spec] of Object.entries((at && at.fields) || {})) {
      if (spec.secret) { const v = W.secrets[fname] && W.secrets[fname].get(); if (v !== undefined) body.auth.fields[fname] = v; }
      else if (W.fieldVals[fname] !== undefined && W.fieldVals[fname] !== '') body.auth.fields[fname] = W.fieldVals[fname];
    }
    const opts = {};
    for (const [k, v] of Object.entries(W.options)) if (v !== '' && v != null) opts[k] = v;
    if (Object.keys(opts).length || W.edit) body.options = W.edit ? { ...Object.fromEntries(Object.keys(d.options).map((k) => [k, null])), ...opts } : opts;
    if (!W.edit && !Object.keys(opts).length) delete body.options;
    return body;
  }

  function kindsBody() {
    const out = {};
    for (const [kn, k] of Object.entries(W.driver.kinds)) {
      const cur = W.kinds[kn] || {};
      const o = {};
      if (cur.enabled === false) o.enabled = false; else if (W.edit && cur.enabled === undefined) o.enabled = true; else if (W.edit) o.enabled = true;
      const iv = cur.watch && cur.watch.poll_interval_s;
      if (iv != null && iv !== '' && Number(iv) !== k.poll_interval_s) o.watch = { poll_interval_s: Number(iv) };
      if (Object.keys(o).length) out[kn] = o;
    }
    return out;
  }

  function drawWizard() {
    const d = W.driver;
    const err = el('div', { class: 'sx-wiz-err', 'aria-live': 'polite' });
    W.err = err;
    const back = el('button', { type: 'button', class: 'sx-btn', onclick: () => { W.step -= 1; if (W.edit && W.step < 2) { closeInner(); return; } drawWizard(); }, text: W.step === 1 || (W.edit && W.step === 2) ? 'Cancel' : 'Back' });
    if (W.step === 1 || (W.edit && W.step === 2)) back.onclick = closeInner;
    let content; let foot;
    if (W.step === 1) {
      const cards = S.drivers.map((dr) => {
        const kinds = Object.keys(dr.kinds).join(', ');
        const card = el('button', { type: 'button', class: 'sx-drv sx-card-btn' + (d && d.id === dr.id ? ' sel' : ''), 'data-driver': dr.id,
          onclick: () => { W.driver = dr; W.authType = Object.keys(dr.auth_types)[0]; W.kinds = {}; W.step = 2; drawWizard(); } },
          driverIcon(dr), el('b', { text: dr.title }), el('small', { text: kinds || '—' }));
        card._hay = `${dr.title} ${dr.id} ${kinds}`.toLowerCase();
        return card;
      });
      const none = el('div', { class: 'sx-hint', id: 'sx-drv-none', text: 'No matching drivers.', hidden: true });
      const filter = (q) => {
        const t = q.trim().toLowerCase(); let n = 0;
        cards.forEach((c) => { const ok = !t || c._hay.includes(t); c.hidden = !ok; c.style.display = ok ? '' : 'none'; if (ok) n++; });
        none.hidden = n > 0;
      };
      const search = el('input', { type: 'search', id: 'sx-drv-search', class: 'sx-drv-search', placeholder: 'Search integrations…', autocomplete: 'off', 'aria-label': 'Search integrations', oninput: (e) => filter(e.target.value) });
      content = el('div', {}, stepBar(), search, el('div', { class: 'sx-drivers' }, cards), none);
      setTimeout(() => search.focus(), 0);
      foot = [back];
    } else if (W.step === 2) {
      content = el('div', {}, stepBar(), connectionForm(), err);
      const next = el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-wiz-next', text: 'Next: test connection', onclick: () => {
        if (!/^[a-z0-9][a-z0-9_-]*$/i.test(W.name.trim())) { err.replaceChildren(banner('bad', 'Name is required (letters, digits, - _).')); return; }
        if ('base_url' in d.settings && d.settings.base_url.required && !/^https?:\/\//.test(W.base_url.trim())) { err.replaceChildren(banner('bad', 'Base URL must start with http:// or https://')); return; }
        W.step = 3; W.test = null; drawWizard();
      } });
      foot = [back, next];
    } else if (W.step === 3) {
      content = el('div', {}, stepBar(), el('div', { id: 'sx-test-out', class: 'sx-test', 'aria-live': 'polite' }, 'Run a connection test with the values you entered. Nothing is saved yet.'), err);
      const run = el('button', { type: 'button', class: 'sx-btn', id: 'sx-test-btn', text: 'Test connection', onclick: runTest });
      const next = el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-wiz-next', text: 'Next: watches', onclick: () => { W.step = 4; drawWizard(); } });
      foot = [back, run, next];
    } else {
      content = el('div', {}, stepBar(), kindsForm(), err);
      const save = el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-wiz-save', text: W.edit ? 'Save changes' : 'Create integration', onclick: wizSave });
      foot = [back, save];
    }
    replaceModal(content, foot, W.edit ? `Edit ${W.name}` : `Add integration${d ? ' — ' + d.title : ''}`);
    if (W.step === 3 && W.test) showTest(W.test);
  }

  function inputRow(label, ctl, hint) { return el('div', { class: 'sx-wrow' }, el('label', {}, label), el('div', {}, ctl, hint && el('div', { class: 'sx-hint', text: hint }))); }

  function connectionForm() {
    const d = W.driver; const f = el('div', { class: 'sx-form' });
    f.append(inputRow('Name', el('input', { type: 'text', id: 'sx-w-name', value: W.name, disabled: W.edit, autocomplete: 'off', oninput: (e) => { W.name = e.target.value; } }), 'Unique id used in config and watches.'));
    if ('base_url' in d.settings) {
      const bs = d.settings.base_url;
      f.append(inputRow('Base URL', el('input', { type: 'text', id: 'sx-w-base', value: W.base_url, placeholder: bs.example || '', autocomplete: 'off', oninput: (e) => { W.base_url = e.target.value; } })));
    }
    const types = Object.keys(d.auth_types);
    if (types.length > 1) {
      const s = el('select', { id: 'sx-w-authtype', onchange: (e) => { W.authType = e.target.value; W.secrets = {}; drawWizard(); } },
        types.map((t) => el('option', { value: t, text: d.auth_types[t].title || t, selected: t === W.authType })));
      f.append(inputRow('Auth type', s));
    }
    const at = d.auth_types[W.authType] || { fields: {} };
    const same_type = W.edit && W.orig.auth.type === W.authType;
    for (const [fname, spec] of Object.entries(at.fields)) {
      if (spec.secret) {
        const prior = same_type ? W.orig.auth.fields[fname] : null;
        const w = W.secrets[fname] || secretWidget({ state: prior && prior.state === 'set' ? 'set' : 'unset', ref: prior && prior.ref, label: fname });
        W.secrets[fname] = w;
        f.append(inputRow(humanize(fname) + (spec.required ? ' *' : ''), w.root));
      } else {
        f.append(inputRow(humanize(fname) + (spec.required ? ' *' : ''), el('input', { type: 'text', 'data-field': fname, value: W.fieldVals[fname] || '', autocomplete: 'off', oninput: (e) => { W.fieldVals[fname] = e.target.value; } })));
      }
    }
    for (const [oname, o] of Object.entries(d.options)) {
      const val = W.options[oname];
      const ctl = o.type === 'bool'
        ? el('input', { type: 'checkbox', 'data-option': oname, checked: val == null ? !!o.default : !!val, onchange: (e) => { W.options[oname] = e.target.checked; } })
        : el('input', { type: o.type === 'int' || o.type === 'float' ? 'number' : 'text', 'data-option': oname, value: val == null ? '' : val, placeholder: o.default == null ? '' : String(o.default),
          oninput: (e) => { const v = e.target.value; W.options[oname] = v === '' ? '' : (o.type === 'int' || o.type === 'float') ? Number(v) : v; } });
      f.append(inputRow(humanize(oname), ctl, o.help));
    }
    if (!Object.keys(at.fields).length && !('base_url' in d.settings)) f.append(el('div', { class: 'sx-hint', text: 'This driver needs no credentials.' }));
    return f;
  }

  function showTest(r) {
    const out = document.getElementById('sx-test-out');
    if (!out) return;
    out.replaceChildren(r.ok
      ? banner('ok', '✓ Connected', r.identity ? ' as ' + r.identity : '', ` (${r.latency_ms} ms)`)
      : banner('bad', '✗ ' + ((r.error && r.error.message) || 'Test failed'), r.error && r.error.code ? ` [${r.error.code}]` : ''));
    // package drivers (Teams): per-step result, so the failing step is visible
    if (Array.isArray(r.steps) && r.steps.length) {
      out.append(el('ul', { class: 'sx-hint', id: 'sx-test-steps' }, r.steps.map((st) =>
        el('li', { text: `${st.ok ? '✓' : '✗'} ${st.step}${st.detail ? ' — ' + st.detail : ''}` }))));
    }
    if (r.saved_options && r.saved_options.tenant_id) {
      W.options.tenant_id = r.saved_options.tenant_id;
      out.append(el('div', { class: 'sx-hint', text: 'Saved tenant_id ' + r.saved_options.tenant_id + ' (links are matched to this account).' }));
    }
  }

  async function runTest() {
    const b = document.getElementById('sx-test-btn');
    b.disabled = true; b.textContent = 'Testing…';
    const tbody = bodyOf();
    if (!confirmHostChange(tbody)) return;
    const r = await sapi('POST', '/api/integrations/test', tbody);
    b.disabled = false; b.textContent = 'Test connection';
    W.test = r.data && 'ok' in r.data ? r.data : { ok: false, error: { message: errText(r) } };
    showTest(W.test);
  }

  function kindsForm() {
    const d = W.driver;
    const wrap = el('div', { class: 'sx-form' }, el('div', { class: 'sx-hint', text: 'Choose which object kinds to recognise (hover cards) and watch, and how often to poll.' }));
    for (const [kn, k] of Object.entries(d.kinds)) {
      const cur = W.kinds[kn] = W.kinds[kn] || {};
      const en = cur.enabled !== false;
      const iv = el('input', { type: 'number', min: '1', 'data-interval': kn, value: (cur.watch && cur.watch.poll_interval_s) || '', placeholder: String(k.poll_interval_s || ''),
        disabled: !en, 'aria-label': `${kn} poll interval seconds`, oninput: (e) => { cur.watch = { ...(cur.watch || {}), poll_interval_s: e.target.value }; } });
      const cb = el('input', { type: 'checkbox', 'data-kind': kn, checked: en, onchange: (e) => { cur.enabled = e.target.checked; iv.disabled = !e.target.checked; } });
      wrap.append(el('div', { class: 'sx-kind' },
        el('label', {}, cb, ' ', el('b', { text: kn })),
        el('span', { class: 'sx-meta', text: `${k.watch_type ? 'watch: ' + k.watch_type : 'no watch'}${k.url ? ' · link cards' : ''}${(k.events || []).length ? ' · ' + k.events.join(', ') : ''}` }),
        el('span', { class: 'sx-grow' }), k.watch_type && el('label', { class: 'sx-meta' }, 'every ', iv, ' s')));
    }
    wrap.append(el('label', { class: 'sx-kind' }, el('input', { type: 'checkbox', id: 'sx-w-enabled', checked: W.enabled, onchange: (e) => { W.enabled = e.target.checked; } }), ' Enabled'));
    return wrap;
  }

  async function wizSave() {
    const b = document.getElementById('sx-wiz-save');
    const body = bodyOf();
    if (!confirmHostChange(body)) return;
    b.disabled = true;
    body.kinds = kindsBody();
    let r;
    if (W.edit) { delete body.name; delete body.driver; r = await sapi('PATCH', '/api/integrations/' + encodeURIComponent(W.name), body); }
    else r = await sapi('POST', '/api/integrations', body);
    b.disabled = false;
    if (!r.ok) { W.err.replaceChildren(banner('bad', errText(r))); return; }
    Object.values(W.secrets).forEach((s) => s.clearValues());
    const nm = W.name; W = null;
    closeInner();
    S.notice = banner('ok', `Saved integration ${nm}.`);
    S.loaded.advanced = false;
    refreshFurls();
    loadIntegrations();
  }

  // ---------- Advanced ----------
  function renderAdvanced() {
    const pane = document.getElementById('settings-pane-advanced');
    if (S.loaded.advanced) return;
    S.loaded.advanced = true;
    const yamlPre = el('pre', { class: 'sx-yaml', id: 'sx-yaml', tabindex: '0', 'aria-label': 'Config YAML (secrets masked)' }, 'Loading…');
    const reloadOut = el('div', { id: 'sx-reload-out' });
    const importOut = el('div', { id: 'sx-import-out' });
    const wtOut = el('div', { id: 'sx-wt-out' }, loading());
    const envPath = el('input', { type: 'text', id: 'sx-import-path', placeholder: 'path to .env (default: bot .env)', 'aria-label': '.env path', autocomplete: 'off' });
    const handling = {};

    const loadYaml = async () => {
      const r = await sapi('GET', '/api/settings/yaml');
      yamlPre.textContent = r.ok ? (r.data.yaml || '# (no config file yet)') : 'Error: ' + errText(r);
    };
    const runImport = async (dry) => {
      importOut.replaceChildren(loading(dry ? 'Analysing…' : 'Applying…'));
      const body = { dry_run: dry, secret_handling: handling };
      if (envPath.value.trim()) body.env_path = envPath.value.trim();
      const r = await sapi('POST', '/api/settings/import-env', body);
      if (!r.ok) { importOut.replaceChildren(banner('bad', errText(r))); return; }
      const rows = r.data.mappings || [];
      const tbl = el('table', { class: 'sx-table' }, el('thead', {}, el('tr', {}, ['Variable', 'Setting', 'Action', 'Value', 'Secret storage'].map((h) => el('th', { text: h })))),
        el('tbody', {}, rows.map((m) => {
          let sel = '';
          if (m.secret) {
            sel = el('select', { 'aria-label': `Storage for ${m.var}`, onchange: (e) => { handling[m.var] = e.target.value; } },
              ['env', 'file', 'keychain'].map((o) => el('option', { value: o, text: o, selected: (handling[m.var] || 'env') === o })));
          }
          return el('tr', {}, el('td', {}, el('code', { text: m.var })), el('td', { text: m.path }),
            el('td', {}, badge(m.action, m.action === 'set' ? 'hot' : m.action === 'error' ? 'bad' : ''), m.shadowed_by_env && badge('env wins', 'ovr')),
            el('td', { text: m.secret ? '••• (masked)' : String(m.preview == null ? '' : m.preview) }), el('td', {}, sel, m.message ? el('div', { class: 'sx-errmsg', text: m.message }) : ''));
        })));
      const unm = (r.data.unmapped || []).length ? el('div', { class: 'sx-hint', text: 'Not mapped: ' + r.data.unmapped.join(', ') }) : '';
      importOut.replaceChildren(dry ? banner('warn', 'Dry run — nothing written.') : banner('ok', 'Imported into the config file.'), rows.length ? tbl : el('div', { class: 'sx-empty', text: 'Nothing to import.' }), unm);
      if (!dry) { S.loaded.general = false; loadYaml(); }
    };
    const doReload = async () => {
      reloadOut.replaceChildren(loading('Reloading…'));
      const r = await sapi('POST', '/api/settings/reload');
      if (!r.ok) { reloadOut.replaceChildren(banner('bad', errText(r))); return; }
      const d = r.data;
      reloadOut.replaceChildren(
        (d.file_errors || []).length ? banner('bad', 'File errors: ' + d.file_errors.join('; ')) : banner('ok', `Reloaded (rev ${d.revision}). Integrations reloaded: ${(d.integrations_reloaded || []).join(', ') || 'none'}.`),
        (d.restart_required || []).length ? banner('warn', 'Restart required for: ' + d.restart_required.map((x) => x.path || x).join(', ')) : '');
      S.loaded.general = false; loadYaml(); refreshFurls();
    };
    const restartOut = el('div', { id: 'sx-restart-out' });
    const doRestart = async () => {
      if (!window.confirm('Restart the bot now? In-flight turns are interrupted and resume after startup; the Web UI is unavailable for a few seconds.')) return;
      const btn = document.getElementById('sx-restart-btn');
      btn.disabled = true;
      const before = await sapi('GET', '/api/health');
      const prevUptime = before.ok ? before.data.uptime_seconds : Infinity;
      restartOut.replaceChildren(loading('Requesting restart…'));
      const r = await sapi('POST', '/api/settings/restart', { confirm: true });
      if (!r.ok) { btn.disabled = false; restartOut.replaceChildren(banner('bad', errText(r))); return; }
      const t0 = Date.now();
      let down = false;
      while (Date.now() - t0 < 120000) {
        restartOut.replaceChildren(loading(`Restarting… waiting for the bot to come back (${Math.round((Date.now() - t0) / 1000)}s)`));
        await new Promise((res) => setTimeout(res, 2000));
        const h = await sapi('GET', '/api/health');
        if (!h.ok) { down = true; continue; }
        if (down || h.data.uptime_seconds < prevUptime) {
          btn.disabled = false;
          restartOut.replaceChildren(banner('ok', `Bot is back up (version ${h.data.bot_version || 'unknown'}).`));
          return;
        }
      }
      btn.disabled = false;
      restartOut.replaceChildren(banner('bad', 'Bot did not come back within 2 minutes. Check ~/.copilot/dop-agent-error.log or run bot_ctl.sh status.'));
    };
    const loadWT = async () => {
      const r = await sapi('GET', '/api/watch-types');
      if (!r.ok) { wtOut.replaceChildren(failure('Could not load watch types: ' + errText(r), loadWT)); return; }
      const reg = r.data.registry || {};
      const rows = Object.entries(reg).map(([t, m]) => el('tr', {}, el('td', {}, m.emoji || '', ' ', el('code', { text: t })), el('td', { text: (m.integrations || []).join(', ') || '—' }),
        el('td', { text: m.poll_interval_s != null ? m.poll_interval_s + ' s' : '—' }), el('td', { text: (m.events || []).join(', ') || '—' })));
      for (const t of r.data.builtin || []) rows.push(el('tr', {}, el('td', {}, el('code', { text: t })), el('td', { text: 'built-in' }), el('td', { text: '—' }), el('td', { text: '—' })));
      wtOut.replaceChildren(el('table', { class: 'sx-table' }, el('thead', {}, el('tr', {}, ['Type', 'Integrations', 'Poll', 'Events'].map((h) => el('th', { text: h })))), el('tbody', {}, rows)));
    };
    pane.replaceChildren(
      el('section', { class: 'sx-card' }, el('h3', { class: 'sx-hd' }, 'Config YAML (read-only, secrets masked)', el('span', { class: 'sx-grow' }), el('button', { type: 'button', class: 'sx-btn', onclick: loadYaml, text: 'Refresh' })), yamlPre),
      el('section', { class: 'sx-card' }, el('h3', { class: 'sx-hd', text: 'Reload from disk' }),
        el('div', { class: 'sx-pad' }, el('p', { class: 'sx-hint', text: 'Re-read the config file after hand edits. Hot settings apply immediately; restart-class settings are listed.' }),
          el('button', { type: 'button', class: 'sx-btn', id: 'sx-reload-btn', onclick: doReload, text: 'Reload config' }), reloadOut)),
      el('section', { class: 'sx-card' }, el('h3', { class: 'sx-hd', text: 'Restart bot' }),
        el('div', { class: 'sx-pad' }, el('p', { class: 'sx-hint', text: 'Restart the bot process (needed for restart-class settings). It is brought back up automatically by launchd, or re-executed in place when run manually.' }),
          el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-restart-btn', onclick: doRestart, text: 'Restart bot' }), restartOut)),
      el('section', { class: 'sx-card' }, el('h3', { class: 'sx-hd', text: 'Import from .env' }),
        el('div', { class: 'sx-pad' }, envPath, ' ', el('button', { type: 'button', class: 'sx-btn', id: 'sx-import-dry', onclick: () => runImport(true), text: 'Dry run' }), ' ',
          el('button', { type: 'button', class: 'sx-btn sx-primary', id: 'sx-import-apply', onclick: () => { if (window.confirm('Write the previewed values into the config file?')) runImport(false); }, text: 'Apply' }), importOut)),
      el('section', { class: 'sx-card' }, el('h3', { class: 'sx-hd', text: 'Watch types' }), el('div', { class: 'sx-pad' }, wtOut)));
    loadYaml(); loadWT();
  }

  // ---------- wiring ----------
  function init() {
    const tabs = document.getElementById('settings-tabs');
    if (!tabs) return;
    tabs.addEventListener('click', (e) => {
      const b = e.target.closest('.sx-tab');
      if (!b || (b.dataset.stab === S.tab && !searching())) return;
      guardLeave(() => { S.q = ''; const si = document.getElementById('settings-search'); if (si) si.value = ''; if (b.dataset.stab !== 'advanced') S.loaded.advanced = false; showTab(b.dataset.stab); }, b.dataset.stab);
    });
    tabs.addEventListener('keydown', tabKeys);
    const input = document.getElementById('settings-search');
    if (input) {
      input.addEventListener('input', () => setSearch(input.value));
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && input.value !== '') { e.preventDefault(); e.stopPropagation(); setSearch(''); }
      });
    }
    document.addEventListener('keydown', (e) => {
      const ov = overlay();
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || !ov || ov.style.display === 'none' || S.inner) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (input) { e.preventDefault(); e.stopPropagation(); input.focus(); input.select(); }
    }, true);
  }
  init();
})();
