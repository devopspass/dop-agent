#!/usr/bin/env bash
# Fail if the core runtime dependencies (requirements.txt) pull in GPL/AGPL
# packages. The optional render extra (requirements-render.txt) is checked
# separately and may only contain the explicitly allowlisted copyleft
# packages below.
#
# Usage: scripts/check_licenses.sh [--keep]   (--keep leaves the temp venvs)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
# Explicitly accepted copyleft packages, render extra only (lower-case names).
RENDER_COPYLEFT_ALLOWLIST="${RENDER_COPYLEFT_ALLOWLIST:-pymupdf}"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

WORK="$(mktemp -d)"
[ "$KEEP" -eq 1 ] || trap 'rm -rf "$WORK"' EXIT

check() { # <label> <allowlist> <requirements files...>
  local label="$1" allow="$2"; shift 2
  local venv="$WORK/$label"
  "$PYTHON" -m venv "$venv"
  "$venv/bin/pip" install -q --disable-pip-version-check pip-licenses
  for r in "$@"; do "$venv/bin/pip" install -q --disable-pip-version-check -r "$HERE/$r"; done
  "$venv/bin/pip-licenses" --format=json > "$WORK/$label.json"
  ALLOW="$allow" LABEL="$label" "$PYTHON" - "$WORK/$label.json" <<'PY'
import json, os, re, sys

ignore = {"pip-licenses", "prettytable", "wcwidth", "pip", "setuptools", "wheel", "pip-licenses-lib"}
allow = {a.strip().lower() for a in os.environ["ALLOW"].split(",") if a.strip()}
bad = []
for pkg in json.load(open(sys.argv[1])):
    name, lic = pkg["Name"], pkg["License"]
    if name.lower().replace("_", "-") in ignore:
        continue
    # GPL/AGPL anywhere in the license string, but not LGPL.
    if re.search(r"(?<!L)GPL|General Public License", lic, re.I) and not re.search(r"\bLGPL|Lesser", lic, re.I):
        if name.lower() not in allow:
            bad.append(f"{name} ({lic})")
if bad:
    print(f"FAIL [{os.environ['LABEL']}]: copyleft packages not allowlisted:")
    print("  " + "\n  ".join(bad))
    sys.exit(1)
print(f"OK [{os.environ['LABEL']}]: no non-allowlisted GPL/AGPL packages")
PY
}

check core "" requirements.txt
check render "$RENDER_COPYLEFT_ALLOWLIST" requirements.txt requirements-render.txt
