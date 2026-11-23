/*
 * Mermaid diagram rendering. An assistant message containing a fenced block
 *
 *   ```mermaid
 *   flowchart LR
 *     A --> B
 *   ```
 *
 * is rendered as a theme-aware SVG diagram with a
 * Diagram/Source toggle and a copy button. Slack is unaffected (plain code).
 *
 * Design notes:
 *  - renderMessages() rebuilds every bubble on each update, so rendered SVG
 *    is cached by source hash and inserted synchronously on rebuild (no
 *    flicker); the Diagram/Source choice lives in viewState, not the DOM.
 *  - Blocks of a still-running turn are left as plain code blocks (the fence
 *    may be partial); they render once the turn finishes.
 *  - mermaid (~2 MB) is imported lazily on the first block. If the import or
 *    a render fails, the plain code block stays, with a small note, and the
 *    next render retries (import failures are not cached).
 *  - securityLevel 'strict' + htmlLabels off: agent text is semi-trusted, so
 *    no click callbacks / raw HTML in labels.
 */
(function () {
  const MERMAID_URL = '/static/vendor/mermaid.min.js'; // vendored UMD build (no CDN)
  const DARK_THEME = {
    darkMode: true, background: '#1a1d21', primaryColor: '#1164a3', primaryTextColor: '#ffffff',
    primaryBorderColor: '#1d9bd1', lineColor: '#ababad', secondaryColor: '#2b2d31', tertiaryColor: '#222529',
    textColor: '#d1d2d3', noteBkgColor: '#2b2d31', noteTextColor: '#d1d2d3', actorBkg: '#1164a3',
    actorTextColor: '#ffffff', signalColor: '#d1d2d3', signalTextColor: '#d1d2d3', clusterBkg: '#222529',
    clusterBorder: '#35373b', edgeLabelBackground: '#222529',
  };
  const LIGHT_THEME = {
    darkMode: false, background: '#ffffff', primaryColor: '#e3effa', primaryTextColor: '#0b2e4f',
    primaryBorderColor: '#1164a3', lineColor: '#4f5358', secondaryColor: '#f1f2f4', tertiaryColor: '#f6f7f8',
    textColor: '#1d1c1d', noteBkgColor: '#fff6d6', noteTextColor: '#3d3200', actorBkg: '#e3effa',
    actorTextColor: '#0b2e4f', signalColor: '#1d1c1d', signalTextColor: '#1d1c1d', clusterBkg: '#f6f7f8',
    clusterBorder: '#b9bcc0', edgeLabelBackground: '#ffffff',
  };
  const isLight = () => !!(window.dopTheme && window.dopTheme.get() === 'light');
  const initConfig = () => ({ startOnLoad: false, theme: 'base', themeVariables: { ...(isLight() ? LIGHT_THEME : DARK_THEME), fontSize: '16px' }, securityLevel: 'strict',
    htmlLabels: false, flowchart: { htmlLabels: false, useMaxWidth: false, nodeSpacing: 40, rankSpacing: 50 },
    sequence: { useMaxWidth: false } });
  const svgCache = new Map(); // source hash -> { svg, links }
  const viewState = {}; // "<msgId>:<idx>" -> 'source' | {k,x,y} (zoom/pan survives rebuilds)
  const BIG_SOURCE = 6000; // chars; larger diagrams need an explicit "Render" click
  const MIN_K = 0.1;
  const MAX_K = 8;
  let loader = null;
  let counter = 0;

  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return `${h}-${s.length}`;
  }

  function load() {
    if (!loader) {
      loader = new Promise((resolve, reject) => {
        if (window.mermaid) return resolve(window.mermaid);
        const sc = document.createElement('script');
        sc.src = MERMAID_URL;
        sc.onload = () => (window.mermaid ? resolve(window.mermaid) : reject(new Error('mermaid missing')));
        sc.onerror = () => reject(new Error('mermaid load failed'));
        document.head.appendChild(sc);
      }).then((m) => {
        m.initialize(initConfig());
        return m;
      }).catch((e) => { loader = null; throw e; });
    }
    return loader;
  }

  function note(pre, text, onRender) {
    let n = pre.nextElementSibling;
    if (!n || !n.classList.contains('mermaid-note')) {
      n = document.createElement('div');
      n.className = 'mermaid-note';
      pre.after(n);
    }
    n.textContent = text;
    if (onRender) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mermaid-render-btn';
      b.textContent = 'Render diagram';
      b.addEventListener('click', onRender);
      n.append(' ', b);
    }
  }

  // `click NODE "url"` (flowchart) is stripped before rendering and re-applied
  // as <a> wrappers afterwards: mermaid itself stays in 'strict' mode.
  const CLICK_RE = /^[ \t]*click[ \t]+([A-Za-z0-9_-]+)[ \t]+(?:href[ \t]+)?"([^"]+)"(?:[ \t]+"[^"]*")?(?:[ \t]+(_blank|_self))?[ \t]*$/;
  function extractLinks(src) {
    const links = [];
    const kept = src.split('\n').filter((line) => {
      const m = CLICK_RE.exec(line);
      if (!m) return true;
      if (/^(https?:\/\/|#)/i.test(m[2])) links.push({ id: m[1], url: m[2], blank: !m[2].startsWith('#') });
      return false;
    });
    return { code: kept.join('\n'), links };
  }

  function applyLinks(root, links) {
    links.forEach(({ id, url, blank }) => {
      const g = root.querySelector(`g.node[id*="flowchart-${id}-"]`);
      if (!g || g.parentNode.tagName === 'a') return;
      const a = document.createElementNS('http://www.w3.org/2000/svg', 'a');
      a.setAttribute('href', url);
      if (blank) { a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener noreferrer'); }
      a.classList.add('mermaid-link');
      g.parentNode.insertBefore(a, g);
      a.appendChild(g);
    });
  }

  function naturalSize(svg) {
    const vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
    if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) return [vb[2], vb[3]];
    return [parseFloat(svg.getAttribute('width')) || 600, parseFloat(svg.getAttribute('height')) || 300];
  }

  // Zoom/pan controller shared by the inline block and the fullscreen overlay.
  function makeView(viewport, stage, w, h, saved, onChange) {
    const v = saved ? { ...saved } : { k: 1, x: 0, y: 0 };
    const vw = () => viewport.clientWidth || 600;
    const vh = () => viewport.clientHeight || 400;
    const paint = () => {
      stage.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`;
      if (onChange) onChange({ ...v });
    };
    const natural = () => { v.k = 1; v.x = w < vw() ? (vw() - w) / 2 : 0; v.y = h < vh() ? (vh() - h) / 2 : 0; paint(); };
    const fit = () => {
      v.k = Math.max(MIN_K, Math.min(MAX_K, Math.min(vw() / w, vh() / h)));
      v.x = (vw() - w * v.k) / 2; v.y = (vh() - h * v.k) / 2; paint();
    };
    const zoomAt = (factor, px, py) => {
      const k = Math.max(MIN_K, Math.min(MAX_K, v.k * factor));
      v.x = px - (px - v.x) * (k / v.k); v.y = py - (py - v.y) * (k / v.k); v.k = k; paint();
    };
    const zoomCenter = (f) => zoomAt(f, vw() / 2, vh() / 2);
    if (!saved) natural(); else paint();

    viewport.addEventListener('wheel', (e) => {
      if (!(e.ctrlKey || e.metaKey)) return; // plain wheel keeps scrolling the page
      e.preventDefault();
      const r = viewport.getBoundingClientRect();
      zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });

    const pts = new Map();
    let moved = 0;
    let lastDist = 0;
    viewport.addEventListener('pointerdown', (e) => {
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      moved = 0;
      lastDist = 0;
      viewport.classList.add('mermaid-grabbing');
    });
    viewport.addEventListener('pointermove', (e) => {
      const p = pts.get(e.pointerId);
      if (!p) return;
      const dx = e.clientX - p.x;
      const dy = e.clientY - p.y;
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      moved += Math.abs(dx) + Math.abs(dy);
      if (moved > 4 && !viewport.hasPointerCapture(e.pointerId)) viewport.setPointerCapture(e.pointerId);
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        const r = viewport.getBoundingClientRect();
        if (lastDist) zoomAt(d / lastDist, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
        lastDist = d;
      } else if (moved > 4) {
        v.x += dx; v.y += dy; paint();
      }
    });
    const up = (e) => { pts.delete(e.pointerId); lastDist = 0; if (!pts.size) viewport.classList.remove('mermaid-grabbing'); };
    viewport.addEventListener('pointerup', up);
    viewport.addEventListener('pointercancel', up);
    viewport.addEventListener('click', (e) => { if (moved > 4) { e.preventDefault(); e.stopPropagation(); } }, true);

    viewport.addEventListener('keydown', (e) => {
      const step = 40;
      const keys = {
        '+': () => zoomCenter(1.25), '=': () => zoomCenter(1.25), '-': () => zoomCenter(0.8), '0': fit, '1': natural,
        ArrowLeft: () => { v.x += step; paint(); }, ArrowRight: () => { v.x -= step; paint(); },
        ArrowUp: () => { v.y += step; paint(); }, ArrowDown: () => { v.y -= step; paint(); },
      };
      if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
    });
    return { fit, natural, zoomCenter, paint };
  }

  function buildStage(svgHtml, links) {
    const stage = document.createElement('div');
    stage.className = 'mermaid-stage';
    stage.innerHTML = svgHtml;
    const svg = stage.querySelector('svg');
    const [w, h] = svg ? naturalSize(svg) : [600, 300];
    if (svg) {
      svg.setAttribute('width', w); svg.setAttribute('height', h);
      svg.style.maxWidth = 'none';
      applyLinks(stage, links);
    }
    stage.style.width = `${w}px`; stage.style.height = `${h}px`;
    return { stage, w, h };
  }

  function toolbar(items) {
    const bar = document.createElement('div');
    bar.className = 'mermaid-bar';
    items.forEach(([cls, text, title, fn]) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = cls; b.textContent = text; b.title = title;
      b.addEventListener('click', fn);
      bar.appendChild(b);
    });
    return bar;
  }

  function openFullscreen(entry) {
    const overlay = document.createElement('div');
    overlay.className = 'mermaid-overlay';
    const viewport = document.createElement('div');
    viewport.className = 'mermaid-viewport mermaid-viewport-full';
    viewport.tabIndex = 0;
    const { stage, w, h } = buildStage(entry.svg, entry.links);
    viewport.appendChild(stage);
    let view;
    const close = () => { document.removeEventListener('keydown', onKey, true); overlay.remove(); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    const bar = toolbar([
      ['mermaid-zoom-in', '＋', 'Zoom in (+)', () => view.zoomCenter(1.25)],
      ['mermaid-zoom-out', '－', 'Zoom out (-)', () => view.zoomCenter(0.8)],
      ['mermaid-fit', 'Fit', 'Fit to window (0)', () => view.fit()],
      ['mermaid-100', '100%', 'Natural size (1)', () => view.natural()],
      ['mermaid-close', '✕ Close', 'Close (Esc)', close],
    ]);
    overlay.append(bar, viewport);
    document.body.appendChild(overlay);
    document.addEventListener('keydown', onKey, true);
    view = makeView(viewport, stage, w, h, null, null);
    view.fit();
    viewport.focus();
  }

  function mount(pre, entry, stateKey) {
    if (pre.dataset.mermaidHandled) return;
    const wrap = document.createElement('div');
    wrap.className = 'mermaid-block';
    const viewport = document.createElement('div');
    viewport.className = 'mermaid-viewport';
    viewport.tabIndex = 0;
    viewport.title = 'Ctrl/⌘+wheel or pinch to zoom, drag to pan, keys + - 0 1';
    const { stage, w, h } = buildStage(entry.svg, entry.links);
    viewport.style.height = `${Math.min(h + 16, 520)}px`;
    viewport.appendChild(stage);
    let view;
    const diagramBar = toolbar([
      ['mermaid-zoom-in', '＋', 'Zoom in', () => view.zoomCenter(1.25)],
      ['mermaid-zoom-out', '－', 'Zoom out', () => view.zoomCenter(0.8)],
      ['mermaid-fit', 'Fit', 'Fit to the box', () => view.fit()],
      ['mermaid-100', '100%', 'Natural size', () => view.natural()],
      ['mermaid-full', '⛶', 'Fullscreen', () => openFullscreen(entry)],
      ['mermaid-toggle', '', '', () => {
        viewState[stateKey] = viewState[stateKey] === 'source' ? undefined : 'source';
        apply();
      }],
      ['mermaid-copy', '📋 Copy source', '', async (e) => {
        try {
          await navigator.clipboard.writeText(src);
          e.target.textContent = '✅ Copied';
          setTimeout(() => { e.target.textContent = '📋 Copy source'; }, 1000);
        } catch (err) { console.warn('copy mermaid source failed', err); }
      }],
    ]);
    const toggle = diagramBar.querySelector('.mermaid-toggle');
    pre.before(wrap);
    pre.dataset.mermaidHandled = '1';
    wrap.append(diagramBar, viewport, pre);
    const src = pre.textContent;
    const saved = viewState[`${stateKey}:view`];
    view = makeView(viewport, stage, w, h, saved, (st) => { viewState[`${stateKey}:view`] = st; });
    function apply() {
      const showSource = viewState[stateKey] === 'source';
      viewport.style.display = showSource ? 'none' : '';
      pre.style.display = showSource ? '' : 'none';
      toggle.textContent = showSource ? '📊 Diagram' : '</> Source';
      if (!showSource && !viewState[`${stateKey}:view`]) view.natural();
    }
    apply();
  }

  function errLine(e) {
    const m = String((e && (e.str || e.message)) || e || '').split('\n').map((x) => x.trim()).find(Boolean) || 'unknown error';
    return m.slice(0, 200);
  }

  function renderOne(pre, src, key, stateKey, force) {
    pre.dataset.mermaidKey = key;
    const { code, links } = extractLinks(src);
    load().then(async (m) => {
      let svg;
      try {
        ({ svg } = await m.render(`mmd-${++counter}`, code));
      } catch (e) {
        console.warn('mermaid render failed', e);
        document.querySelectorAll('[id^="dmmd-"]').forEach((el) => el.remove());
        document.querySelectorAll(`pre[data-mermaid-key="${key}"]`).forEach((p) => note(p, `⚠️ Diagram syntax error: ${errLine(e)} — showing source.`));
        return;
      }
      svgCache.set(key, { svg, links });
      document.querySelectorAll(`pre[data-mermaid-key="${key}"]`).forEach((p) => {
        const n = p.nextElementSibling;
        if (n && n.classList.contains('mermaid-note')) n.remove();
        mount(p, svgCache.get(key), stateKey);
      });
    }).catch((e) => {
      console.warn('mermaid load failed', e);
      note(pre, '⚠️ Could not load the diagram renderer — showing source.');
    });
  }

  function apply(container, msgId, turnRunning) {
    const blocks = container.querySelectorAll('pre > code.language-mermaid');
    blocks.forEach((code, idx) => {
      const pre = code.parentElement;
      if (pre.dataset.mermaidHandled) return;
      const src = code.textContent;
      if (turnRunning) { note(pre, 'Diagram renders when the message completes.'); return; }
      const key = hash(src);
      const stateKey = `${msgId}:${idx}`;
      if (svgCache.has(key)) { mount(pre, svgCache.get(key), stateKey); return; }
      if (src.length > BIG_SOURCE && !viewState[`${stateKey}:forced`]) {
        note(pre, `Large diagram (${src.length} chars) — not rendered automatically.`, () => {
          viewState[`${stateKey}:forced`] = true;
          renderOne(pre, src, key, stateKey);
        });
        return;
      }
      renderOne(pre, src, key, stateKey);
    });
  }

  // Diagrams bake colours into the SVG: drop cached SVGs and re-initialise so the next render uses the new theme.
  window.addEventListener('dop-theme-change', () => {
    svgCache.clear();
    if (loader) loader.then((m) => m.initialize(initConfig())).catch(() => {});
  });

  window.MermaidRender = { apply };
})();
