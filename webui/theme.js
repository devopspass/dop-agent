/*
 * Theme switcher: preference = 'dark' (default) | 'light' | 'system', persisted in localStorage ('dop-theme').
 * Loaded synchronously in <head> so <html data-theme> is set before first paint (no flash).
 * With no stored value the theme is dark; 'system' follows prefers-color-scheme live.
 * Consumers (mermaid.js, chart.js) listen for the 'dop-theme-change' event on window
 * (detail: { pref, theme }) to re-theme; dopTheme.palette() gives them resolved colours.
 */
(function () {
  const KEY = 'dop-theme';
  const PREFS = ['dark', 'light', 'system'];
  const HLJS = { dark: 'github-dark.min.css', light: 'github.min.css' };
  const META_COLOR = { dark: '#1a1d21', light: '#ffffff' };
  const LABEL = { dark: '🌙 Dark', light: '☀️ Light', system: '🖥 Auto' };
  const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

  function pref() {
    try { const v = localStorage.getItem(KEY); return PREFS.includes(v) ? v : 'dark'; } catch (e) { return 'dark'; }
  }
  function resolve(p) { return p === 'system' ? (mq && mq.matches ? 'light' : 'dark') : p; }
  function ensureMeta(name) {
    let m = document.querySelector('meta[name="' + name + '"]');
    if (!m && document.head) { m = document.createElement('meta'); m.name = name; document.head.appendChild(m); }
    return m;
  }
  let lastTheme = null;
  function apply() {
    const p = pref();
    const t = resolve(p);
    const root = document.documentElement;
    if (lastTheme !== null && lastTheme !== t && !(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches)) {
      root.classList.add('theme-anim');
      clearTimeout(apply._t);
      apply._t = setTimeout(() => root.classList.remove('theme-anim'), 250);
    }
    root.setAttribute('data-theme', t);
    root.setAttribute('data-theme-pref', p);
    const cs = ensureMeta('color-scheme'); if (cs) cs.setAttribute('content', t);
    const tc = ensureMeta('theme-color'); if (tc) tc.setAttribute('content', META_COLOR[t]);
    document.querySelectorAll('link[href*="highlight.js"]').forEach((l) => {
      const want = l.href.replace(HLJS[t === 'light' ? 'dark' : 'light'], HLJS[t]);
      if (want !== l.href) l.href = want;
    });
    const btn = document.getElementById('theme-toggle');
    if (btn) {
      const next = PREFS[(PREFS.indexOf(p) + 1) % PREFS.length];
      const cur = 'Theme: ' + p + (p === 'system' ? ' (' + t + ')' : '');
      btn.textContent = LABEL[p];
      btn.setAttribute('aria-label', cur + '. Switch to ' + next);
      btn.title = cur + ' — click for ' + next;
    }
    document.querySelectorAll('select[data-theme-select]').forEach((s) => { if (s.value !== p) s.value = p; });
    const prev = lastTheme;
    lastTheme = t;
    if (prev !== t) {
      try { window.dispatchEvent(new CustomEvent('dop-theme-change', { detail: { pref: p, theme: t } })); } catch (e) {}
    }
  }
  function set(p) {
    if (!PREFS.includes(p)) return;
    try { localStorage.setItem(KEY, p); } catch (e) {}
    apply();
  }
  function css(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  window.dopTheme = {
    PREFS,
    pref,
    get: () => resolve(pref()),
    set,
    toggle() { set(PREFS[(PREFS.indexOf(pref()) + 1) % PREFS.length]); },
    // Resolved colours for canvas/SVG renderers that cannot use CSS variables.
    palette() {
      return {
        light: resolve(pref()) === 'light',
        text: css('--slack-text-primary', '#d1d2d3'),
        muted: css('--slack-text-secondary', '#ababad'),
        border: css('--slack-border', '#35373b'),
        bg: css('--slack-main-bg', '#1a1d21'),
        surface: css('--slack-bubble-assistant-bg', '#222529'),
        surface2: css('--slack-bubble-user-bg', '#2b2d31'),
        accent: css('--slack-accent', '#1164a3'),
        link: css('--slack-link', '#1d9bd1'),
      };
    },
  };

  apply();
  if (mq) {
    const onChange = () => { if (pref() === 'system') apply(); };
    if (mq.addEventListener) mq.addEventListener('change', onChange); else if (mq.addListener) mq.addListener(onChange);
  }
  window.addEventListener('storage', (e) => { if (e.key === KEY || e.key === null) apply(); }); // cross-tab sync
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', (e) => { e.preventDefault(); window.dopTheme.toggle(); });
    document.addEventListener('change', (e) => {
      const s = e.target.closest && e.target.closest('select[data-theme-select]');
      if (s) set(s.value);
    });
  });
})();
