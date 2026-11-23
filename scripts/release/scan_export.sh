#!/usr/bin/env bash
# Usage: scan_export.sh <out-dir>   Exit 1 on FAIL. Env: SKIP_TOOLS=1 to not auto-install python tools.
set -uo pipefail
OUT="${1:?usage: $0 <out-dir>}"; OUT="$(cd "$OUT" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
FAIL=0; WARN=0
fail(){ echo "  -> FAIL: $*"; FAIL=1; }
warn(){ echo "  -> WARN: $*"; WARN=1; }
VENV=/tmp/dop-release-scan-venv
pytool(){ # name [pip-pkg]
  command -v "$1" >/dev/null 2>&1 && { command -v "$1"; return; }
  [ -x "$VENV/bin/$1" ] && { echo "$VENV/bin/$1"; return; }
  [ "${SKIP_TOOLS:-0}" = 1 ] && return
  [ -d "$VENV" ] || python3 -m venv "$VENV" >/dev/null 2>&1
  "$VENV/bin/pip" install -q "${2:-$1}" >/dev/null 2>&1 && echo "$VENV/bin/$1"
}

echo "== 1. gitleaks (dir mode) =="
if command -v gitleaks >/dev/null; then
  CFG="$OUT/.gitleaks.toml"; [ -f "$CFG" ] || CFG="$HERE/gitleaks.default.toml"
  gitleaks dir "$OUT" -c "$CFG" --no-banner --redact -v || fail "gitleaks findings (config $CFG)"
else warn "gitleaks missing: brew install gitleaks"; FAIL=1; fi

echo "== 2. trufflehog filesystem (unverified shown) =="
if command -v trufflehog >/dev/null; then
  TH="$(trufflehog filesystem "$OUT" --no-update --json 2>/dev/null | grep -c '"DetectorName"' || true)"
  echo "  findings: $TH"; [ "${TH:-0}" -gt 0 ] && { trufflehog filesystem "$OUT" --no-update 2>/dev/null | head -60; fail "trufflehog: $TH findings (review; fixtures may be allowlisted manually)"; }
else warn "trufflehog missing: brew install trufflehog"; FAIL=1; fi

echo "== 3. detect-secrets =="
DS="$(pytool detect-secrets)"
if [ -n "$DS" ]; then
  BASE="$OUT/.secrets.baseline"; [ -f "$BASE" ] || BASE="$HERE/secrets.baseline"
  # Fresh scan, compared to the baseline: any result not in baseline fails.
  (cd "$OUT" && "$DS" scan --all-files --exclude-files '(^|/)tests/|\.png$' > /tmp/ds-new.json 2>/dev/null)
  python3 - "$BASE" /tmp/ds-new.json <<'PY' || fail "detect-secrets: findings not in baseline (generate baseline only after manual review)"
import json,sys,os
b=json.load(open(sys.argv[1])).get("results",{}) if os.path.exists(sys.argv[1]) else {}
n=json.load(open(sys.argv[2])).get("results",{})
bad=[(f,s["type"],s["line_number"]) for f,l in n.items() for s in l
     if s["hashed_secret"] not in {x["hashed_secret"] for x in b.get(f,[])}]
for x in bad[:50]: print("  ",x)
print("  new findings:",len(bad)); sys.exit(1 if bad else 0)
PY
else warn "detect-secrets unavailable (pip install detect-secrets)"; FAIL=1; fi

echo "== 4. banned terms =="
PAT='luminor|epam|dsm|\banh\b|\bsolco\b|\bessco\b|panavora|noviwest|hariton|aliaksei|kharyton|onelum|corebanking|flexcube|fcubs|U6GH4SJTH|/Users/[A-Za-z]|git\.epam|@epam|@luminor'
# Allowlist: vendored/minified JS and binary files; add exact paths here with a reason.
ALLOW_PATHS='(^|/)(vendor|node_modules)/|\.min\.js$|\.png$|\.jpg$|\.ico$|\.woff2?$'
HITS="$(cd "$OUT" && grep -rIniE --exclude=scan_export.sh --exclude=check_public_tree.sh "$PAT" . 2>/dev/null | grep -viE '/users/me\b' | grep -vE "^\./($ALLOW_PATHS)|^\./[^:]*($ALLOW_PATHS)" )"
N=$(printf '%s' "$HITS" | grep -c . || true)
echo "  hits: $N"; printf '%s\n' "$HITS" | cut -d: -f1 | sort | uniq -c | sort -rn | head -25
printf '%s\n' "$HITS" > /tmp/banned-hits.txt; echo "  full list: /tmp/banned-hits.txt"
[ "$N" -gt 0 ] && fail "banned terms present"

echo "== 5. emails / hostnames / IPs for manual review =="
( cd "$OUT"
  grep -rIohE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' . | sort -u > /tmp/uniq-emails.txt
  grep -rIohE '\b[a-z0-9-]+(\.[a-z0-9-]+)+\.(com|net|org|io|dev|eu|de|nl|ai|cloud|local|internal|corp)\b' --exclude='*.min.js' . | sort -u > /tmp/uniq-hosts.txt
  grep -rIohE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' --exclude='*.min.js' . | grep -vE '^(127\.0\.0\.1|0\.0\.0\.0|255\.|0\.)' | sort -u > /tmp/uniq-ips.txt )
echo "  emails: $(wc -l </tmp/uniq-emails.txt) (/tmp/uniq-emails.txt)"; head -40 /tmp/uniq-emails.txt | sed 's/^/    /'
echo "  hosts:  $(wc -l </tmp/uniq-hosts.txt) (/tmp/uniq-hosts.txt)"
echo "  ips:    $(wc -l </tmp/uniq-ips.txt) (/tmp/uniq-ips.txt)"; head -20 /tmp/uniq-ips.txt | sed 's/^/    /'
echo "  Review these manually; only noreply/example/placeholder values may remain."
grep -vE 'noreply|example\.(com|org)|users\.noreply' /tmp/uniq-emails.txt | grep -q . && warn "non-placeholder emails exist"

echo "== 6. PNG screenshots (manual) =="
PNG=$(find "$OUT" -name '*.png' | wc -l); echo "  $PNG PNG files"
[ "$PNG" -gt 0 ] && warn "open every PNG and confirm no real names/emails/URLs/tokens are visible (session D)"

echo "== 7. licenses =="
PL="$(pytool pip-licenses pip-licenses)"
if [ -n "$PL" ]; then
  V=/tmp/dop-release-lic-venv; [ -d $V ] || python3 -m venv $V
  $V/bin/pip install -q -r "$OUT/requirements.txt" pip-licenses >/dev/null 2>&1
  $V/bin/pip-licenses --format=markdown --summary 2>/dev/null | head -30 || true
  $V/bin/pip-licenses --format=csv 2>/dev/null | grep -iE 'gpl|agpl|unknown' | grep -viE 'lgpl' | sed 's/^/  CHECK: /'
else warn "pip-licenses unavailable"; fi

echo
if [ $FAIL -ne 0 ]; then echo "RESULT: FAIL"; exit 1; fi
[ $WARN -ne 0 ] && echo "RESULT: PASS (with manual-review warnings)" || echo "RESULT: PASS"
