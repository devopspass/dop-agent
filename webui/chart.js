/*
 * Markdown-defined charts. An assistant message containing a fenced block
 *
 *   ```chart
 *   type: donut
 *   title: AIU by origin
 *   data:
 *     - Watcher: 71
 *     - Interactive: 123
 *   ```
 *
 * is rendered as a theme-aware Chart.js chart with a Chart/Source
 * toggle. Slack is unaffected (plain code). Convention: dop-agent/skills/chart-format.
 *
 * Design notes (mirrors mermaid.js):
 *  - renderMessages() rebuilds every bubble on each update: the parsed spec is
 *    cached by source hash, the Chart instance is re-created synchronously
 *    (animation only on first mount) and the previous one destroyed via a
 *    registry keyed "<msgId>:<idx>". The Chart/Source choice lives in viewState.
 *  - Blocks of a still-running turn stay plain code (the fence may be partial).
 *  - Chart.js is vendored (/static/vendor/chart.umd.min.js) and lazy-loaded on
 *    the first block; a load/parse failure keeps the code block plus a note.
 *  - Agent text is semi-trusted: every label is set via textContent, never
 *    innerHTML; data is capped at MAX_POINTS.
 *  - Types: donut pie bar hbar stacked line burndown (Chart.js), spark (SVG).
 *    Body is YAML-ish (canonical) or JSON (starts with '{').
 */
(function () {
  const LIB_URL = '/static/vendor/chart.umd.min.js';
  const MAX_POINTS = 500;
  const TYPES = ['donut', 'pie', 'bar', 'hbar', 'stacked', 'line', 'burndown', 'spark'];
  const DARK_COLORS = ['#1d9bd1', '#e8912d', '#2eb67d', '#ecb22e', '#e01e5a', '#9b59b6', '#36c5f0', '#7f8c8d'];
  // Darker series colours: >=3:1 against a white surface.
  const LIGHT_COLORS = ['#0e72a6', '#c4620a', '#1b8a5f', '#8f6b00', '#c8174f', '#7d3f98', '#0b7a99', '#5f6b6d'];
  const light = () => !!(window.dopTheme && window.dopTheme.get() === 'light');
  let COLORS = DARK_COLORS;
  let SLICE_BORDER = '#222529';
  let IDEAL = '#7f8c8d';
  let FILL = 'rgba(29,155,209,.12)';
  // Chart.js draws on <canvas>, so CSS variables don't reach it: re-resolve the theme before each (re)creation.
  function applyTheme(C) {
    const L = light();
    COLORS = L ? LIGHT_COLORS : DARK_COLORS;
    IDEAL = L ? '#5f6b6d' : '#7f8c8d';
    FILL = L ? 'rgba(14,114,166,.12)' : 'rgba(29,155,209,.12)';
    const pal = window.dopTheme ? window.dopTheme.palette() : { muted: '#9a9b9e', border: '#35373b', surface: '#222529', text: '#d1d2d3' };
    SLICE_BORDER = pal.surface;
    C.defaults.color = L ? pal.muted : '#9a9b9e';
    C.defaults.borderColor = L ? pal.border : '#35373b';
    C.defaults.plugins.tooltip.backgroundColor = L ? '#ffffff' : '#222529';
    C.defaults.plugins.tooltip.titleColor = L ? '#1d1c1d' : '#d1d2d3';
    C.defaults.plugins.tooltip.bodyColor = L ? '#1d1c1d' : '#d1d2d3';
    C.defaults.plugins.tooltip.borderColor = L ? '#b9bcc0' : '#35373b';
  }
  const specCache = new Map(); // source hash -> spec
  const viewState = {}; // "<msgId>:<idx>" -> 'source'
  const instances = new Map(); // "<msgId>:<idx>" -> Chart
  const animated = new Set(); // keys already animated once
  let loader = null;

  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return `${h}-${s.length}`;
  }

  function load() {
    if (window.Chart) return Promise.resolve(window.Chart);
    if (!loader) {
      loader = new Promise((res, rej) => {
        const s = document.createElement('script');
        s.src = LIB_URL;
        s.onload = () => (window.Chart ? res(window.Chart) : rej(new Error('Chart missing')));
        s.onerror = () => rej(new Error('Chart.js failed to load'));
        document.head.appendChild(s);
      }).then((C) => {
        C.defaults.font.family = '-apple-system, Segoe UI, Helvetica, sans-serif';
        C.defaults.plugins.tooltip.borderWidth = 1;
        return C;
      }).catch((e) => { loader = null; throw e; });
    }
    return loader;
  }

  const list = (s) => String(s == null ? '' : s).split(',').map((x) => x.trim()).filter((x) => x !== '');
  const nums = (arr) => arr.map((x) => (x === '' || x === 'null' ? null : Number(x)));

  function parse(src) {
    const t = src.trim();
    let o = { data: [] };
    if (t.startsWith('{')) {
      const j = JSON.parse(t);
      Object.keys(j).forEach((k) => { if (k !== 'data') o[k] = j[k]; });
      (Array.isArray(j.data) ? j.data : []).forEach((d) => {
        if (Array.isArray(d)) o.data.push({ l: String(d[0]), v: d.slice(1).map(Number) });
        else o.data.push({ l: String(d.label != null ? d.label : d.l), v: [].concat(d.values != null ? d.values : d.v).map(Number) });
      });
      ['x', 'series'].forEach((k) => { if (Array.isArray(o[k])) o[k] = o[k].join(','); });
      if (Array.isArray(o.cards)) o.cards = o.cards.map((c) => `${c.label}=${c.value}`).join(', ');
    } else {
      let inData = false;
      src.split('\n').forEach((ln, i) => {
        if (!ln.trim()) return;
        if (inData && /^\s*-\s/.test(ln)) {
          const m = ln.replace(/^\s*-\s*/, '').match(/^"?(.*?)"?\s*:\s*(.*)$/);
          if (!m) throw new Error(`line ${i + 1}: bad data row`);
          o.data.push({ l: m[1], v: nums(list(m[2])) });
          return;
        }
        const m = ln.match(/^(\w+):\s*(.*)$/);
        if (!m) throw new Error(`line ${i + 1}: cannot parse "${ln.slice(0, 40)}"`);
        if (m[1] === 'data') { inData = true; return; }
        o[m[1]] = m[2].trim();
      });
    }
    o.type = String(o.type || '').toLowerCase();
    if (!TYPES.includes(o.type)) throw new Error(`unknown type "${o.type}" (use ${TYPES.join('|')})`);
    if (!o.data.length) throw new Error('no data rows');
    if (o.data.length > MAX_POINTS || o.data.some((d) => d.v.length > MAX_POINTS)) throw new Error('too many data points');
    if (o.data.some((d) => d.v.some((n) => n !== null && !Number.isFinite(n)))) throw new Error('non-numeric value in data');
    if (!['donut', 'pie', 'hbar', 'spark'].includes(o.type) && !o.x) throw new Error('"x:" labels required for this type');
    return o;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function cardsEl(str) {
    const box = el('div', 'chart-cards');
    String(str).split(/,\s*(?=[^,=]+=)/).forEach((c) => {
      const i = c.indexOf('=');
      if (i < 0) return;
      const card = el('div', 'chart-card');
      card.append(el('b', null, c.slice(i + 1).trim()), el('span', null, c.slice(0, i).trim()));
      box.appendChild(card);
    });
    return box;
  }

  function legendEl(names, colors) {
    const box = el('div', 'chart-legend');
    names.forEach((n, i) => {
      const s = el('span');
      const sw = el('i');
      sw.style.background = colors[i % colors.length];
      s.append(sw, document.createTextNode(n));
      box.appendChild(s);
    });
    return box;
  }

  function tableEl(o) {
    const t = el('table', 'chart-table');
    const head = el('tr');
    const cols = o.x ? ['', ...list(o.x)] : ['Item', o.unit || 'Value'];
    cols.forEach((c) => head.appendChild(el('th', null, c)));
    t.appendChild(head);
    o.data.forEach((d) => {
      const r = el('tr');
      r.appendChild(el('td', null, d.l));
      (o.x ? d.v : [d.v[0]]).forEach((v) => r.appendChild(el('td', null, v == null ? '' : String(v))));
      t.appendChild(r);
    });
    return t;
  }

  function sparkEl(o) {
    const t = el('table', 'chart-table');
    o.data.forEach((d, k) => {
      const vals = d.v.filter((v) => v != null);
      const m = Math.max(...vals, 1);
      const w = 140;
      const h = 30;
      const ns = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(ns, 'svg');
      svg.setAttribute('width', w);
      svg.setAttribute('height', h);
      const pl = document.createElementNS(ns, 'polyline');
      pl.setAttribute('fill', 'none');
      pl.setAttribute('stroke', COLORS[k % COLORS.length]);
      pl.setAttribute('stroke-width', '2');
      pl.setAttribute('points', vals.map((y, j) => `${(j * w) / Math.max(vals.length - 1, 1)},${h - (y / m) * (h - 4) - 2}`).join(' '));
      svg.appendChild(pl);
      const last = vals[vals.length - 1];
      const delta = last - vals[0];
      const r = el('tr');
      const c1 = el('td', null, d.l);
      const c2 = el('td');
      c2.appendChild(svg);
      const c3 = el('td');
      c3.append(el('b', null, String(last)), el('span', 'chart-muted', ` ${delta >= 0 ? '▲' : '▼'} ${Math.abs(delta)}`));
      r.append(c1, c2, c3);
      t.appendChild(r);
    });
    return t;
  }

  function config(o, animate) {
    const t = o.type;
    const unit = o.unit ? ` ${o.unit}` : '';
    const base = { responsive: true, maintainAspectRatio: false, animation: animate ? { duration: 400 } : false, plugins: { legend: { display: false } } };
    if (t === 'donut' || t === 'pie') {
      return { type: t === 'donut' ? 'doughnut' : 'pie', data: { labels: o.data.map((d) => d.l), datasets: [{ data: o.data.map((d) => d.v[0]), backgroundColor: COLORS, borderColor: SLICE_BORDER, borderWidth: 2 }] }, options: { ...base, cutout: t === 'donut' ? '62%' : 0, plugins: { ...base.plugins, tooltip: { callbacks: { label: (c) => ` ${c.label}: ${c.parsed}${unit}` } } } } };
    }
    if (t === 'hbar') {
      return { type: 'bar', data: { labels: o.data.map((d) => d.l), datasets: [{ data: o.data.map((d) => d.v[0]), backgroundColor: COLORS[0], borderRadius: 3 }] }, options: { ...base, indexAxis: 'y', scales: { x: { beginAtZero: true } } } };
    }
    const xs = list(o.x);
    const multi = o.data.length > 1;
    const opts = { ...base, plugins: { ...base.plugins, legend: { display: multi || t === 'burndown' } }, scales: { y: { beginAtZero: true, stacked: t === 'stacked', title: { display: !!o.unit, text: o.unit || '' } }, x: { stacked: t === 'stacked' } } };
    if (t === 'bar' || t === 'stacked') {
      return { type: 'bar', data: { labels: xs, datasets: o.data.map((d, i) => ({ label: d.l, data: d.v, backgroundColor: COLORS[i % 8], borderRadius: 2 })) }, options: opts };
    }
    const sets = o.data.map((d, i) => ({ label: d.l, data: d.v, borderColor: COLORS[i % 8], backgroundColor: t === 'burndown' ? FILL : COLORS[i % 8], fill: t === 'burndown', tension: 0, pointRadius: 3, borderWidth: 2.5 }));
    if (t === 'burndown' && o.ideal) {
      const [a, b] = o.ideal.split('->').map(Number);
      sets.push({ label: 'Ideal', data: xs.map((_, i) => a + ((b - a) * i) / Math.max(xs.length - 1, 1)), borderColor: IDEAL, borderDash: [6, 4], pointRadius: 0, fill: false, borderWidth: 2 });
    }
    return { type: 'line', data: { labels: xs, datasets: sets }, options: opts };
  }

  function note(pre, text) {
    let n = pre.nextElementSibling;
    if (!n || !n.classList.contains('chart-note')) { n = el('div', 'chart-note'); pre.after(n); }
    n.textContent = text;
  }

  function mount(pre, spec, key, Chart) {
    pre.dataset.chartHandled = '1';
    applyTheme(Chart);
    const old = instances.get(key);
    if (old) { old.destroy(); instances.delete(key); }
    const block = el('div', 'chart-block');
    const bar = el('div', 'chart-bar');
    const toggle = el('button', null, 'Source');
    toggle.type = 'button';
    bar.appendChild(toggle);
    const body = el('div', 'chart-body');
    if (spec.cards) body.appendChild(cardsEl(spec.cards));
    if (spec.title) body.appendChild(el('div', 'chart-title', spec.title));
    pre.before(block);
    block.append(bar, body, pre);
    const isSvg = spec.type === 'spark';
    if (isSvg) body.appendChild(sparkEl(spec));
    else {
      const wrap = el('div', spec.type === 'hbar' ? 'chart-canvas chart-canvas-h' : 'chart-canvas');
      if (spec.type === 'hbar') wrap.style.height = `${Math.max(120, spec.data.length * 30 + 40)}px`;
      const canvas = document.createElement('canvas');
      wrap.appendChild(canvas);
      body.appendChild(wrap);
      if (spec.type === 'donut' || spec.type === 'pie') {
        const tot = spec.data.reduce((a, d) => a + d.v[0], 0) || 1;
        body.appendChild(legendEl(spec.data.map((d) => `${d.l} — ${d.v[0]}${spec.unit ? ` ${spec.unit}` : ''} (${Math.round((d.v[0] / tot) * 1000) / 10}%)`), COLORS));
      }
      const first = !animated.has(key);
      animated.add(key);
      try {
        instances.set(key, new Chart(canvas, config(spec, first)));
      } catch (e) {
        block.before(pre);
        block.remove();
        delete pre.dataset.chartHandled;
        note(pre, `⚠️ Chart error — showing source: ${e.message}`);
        return;
      }
    }
    if (spec.table === 'yes' && !isSvg) body.appendChild(tableEl(spec));
    if (spec.type === 'donut' && spec.center) {
      const c = el('div', 'chart-center', spec.center);
      body.querySelector('.chart-canvas').appendChild(c);
    }
    const apply = () => {
      const src = viewState[key] === 'source';
      pre.style.display = src ? '' : 'none';
      body.style.display = src ? 'none' : '';
      toggle.textContent = src ? 'Chart' : 'Source';
    };
    toggle.addEventListener('click', () => { viewState[key] = viewState[key] === 'source' ? 'chart' : 'source'; apply(); });
    apply();
  }

  function apply(container, msgId, turnRunning) {
    container.querySelectorAll('pre > code.language-chart').forEach((code, idx) => {
      const pre = code.parentElement;
      if (pre.dataset.chartHandled) return;
      if (turnRunning) { note(pre, 'Chart renders when the message completes.'); return; }
      const src = code.textContent;
      const h = hash(src);
      const key = `${msgId}:${idx}`;
      let spec = specCache.get(h);
      if (!spec) {
        try { spec = parse(src); specCache.set(h, spec); } catch (e) { note(pre, `⚠️ Invalid chart — showing source: ${e.message}`); return; }
      }
      load().then((Chart) => { if (!pre.isConnected || pre.dataset.chartHandled) return; mount(pre, spec, key, Chart); })
        .catch((e) => { console.warn('chart load failed', e); note(pre, '⚠️ Could not load the chart renderer — showing source.'); });
    });
  }

  window.ChartRender = { apply, _parse: parse };
})();
