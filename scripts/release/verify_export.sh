#!/usr/bin/env bash
# Usage: verify_export.sh <out-dir>  — fresh venv, tests, py_compile, Web UI smoke start.
set -euo pipefail
OUT="$(cd "${1:?usage: $0 <out-dir>}" && pwd)"
V="$(mktemp -d /tmp/dop-verify-venv.XXXX)"; H="$(mktemp -d /tmp/dop-verify-home.XXXX)"
PID=""; trap '[ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -rf "$V" "$H"' EXIT
python3 -m venv "$V/v"; PY="$V/v/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$OUT/requirements.txt"
for f in "$OUT"/requirements-dev.txt "$OUT"/requirements/dev.txt; do [ -f "$f" ] && "$PY" -m pip install -q -r "$f"; done
"$PY" -m pip install -q pytest pytest-asyncio 2>/dev/null || true
echo "== py_compile =="
find "$OUT" -name '*.py' -not -path '*/node_modules/*' -print0 | xargs -0 "$PY" -m py_compile
echo "ok"
echo "== unit tests =="
(cd "$OUT" && HOME="$H" "$PY" -m pytest -q)
echo "== smoke start =="
PORT=$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
(cd "$OUT" && HOME="$H" WEB_UI_HOST=127.0.0.1 WEB_UI_PORT=$PORT WEB_UI_PUBLIC_HOST=localhost \
  WEB_UI_PASSWORD=smoke-test-pw WEB_UI_SECRET_KEY=smoke-secret-0123456789abcdef \
  exec "$PY" dop_agent.py >"$H/smoke.log" 2>&1) & PID=$!
ok=0
for i in $(seq 1 40); do
  sleep 1
  kill -0 "$PID" 2>/dev/null || { echo "process died:"; tail -30 "$H/smoke.log"; exit 1; }
  curl -fsS "http://127.0.0.1:$PORT/login" >/dev/null 2>&1 && { ok=1; break; }
done
[ $ok = 1 ] || { echo "Web UI /login never came up"; tail -30 "$H/smoke.log"; exit 1; }
echo "/api/health -> $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/health") (200, or 401 when auth-protected, is fine)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login"); echo "/login -> $CODE"
[ "$CODE" = 200 ] || { echo "FAIL /login"; exit 1; }
echo "VERIFY OK"
