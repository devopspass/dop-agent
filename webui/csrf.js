// CSRF guard companion: the server requires `X-Requested-With: dop` on every mutating
// /api/* request (see web_server._SecurityMiddleware). Add it to same-origin fetches.
(function () {
  const orig = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    const sameOrigin = (url.startsWith('/') && !url.startsWith('//')) || url.startsWith(location.origin + '/');
    if (sameOrigin && method !== 'GET' && method !== 'HEAD') {
      init = init ? { ...init } : {};
      const h = new Headers(init.headers || (typeof input !== 'string' && input.headers) || undefined);
      h.set('X-Requested-With', 'dop');
      init.headers = h;
    }
    return orig(input, init);
  };
})();
