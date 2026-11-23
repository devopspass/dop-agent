#!/usr/bin/env bash
# Usage: export_public.sh <integration-ref> <out-dir>
# Builds the public tree from `git archive <ref> dop-agent` (tracked files only).
set -euo pipefail
REF="${1:?usage: $0 <integration-ref> <out-dir>}"; OUT="${2:?usage: $0 <integration-ref> <out-dir>}"
REPO="$(git rev-parse --show-toplevel)"
[ -d "$OUT" ] && [ -n "$(ls -A "$OUT")" ] && { echo "ERROR: $OUT is not empty" >&2; exit 1; }
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
git -C "$REPO" rev-parse --verify -q "$REF^{commit}" >/dev/null || { echo "ERROR: bad ref $REF" >&2; exit 1; }

git -C "$REPO" archive "$REF" dop-agent | tar -x -C "$OUT" --strip-components=1

# dop-agent/.github/{workflows,ISSUE_TEMPLATE,PULL_REQUEST_TEMPLATE.md,dependabot.yml} and
# dop-agent/skills land at <out>/.github and <out>/skills automatically after --strip-components=1.

# Denylist (paths relative to out-dir; directories removed recursively).
DENY_DIRS=(aws global-protect slack ui_demos docs/cognitive-overhead)
DENY_FILES=(.github/copilot-instructions.md .env WATCHERS.md)
for d in "${DENY_DIRS[@]}"; do rm -rf "${OUT:?}/$d"; done
for f in "${DENY_FILES[@]}"; do rm -f "${OUT:?}/$f"; done
find "$OUT" \( -name '*.db' -o -name '*.db-*' -o -name '*.sqlite*' -o -name '*.bak-*' \
  -o -name 'WATCHERS.md' -o -name 'session*.json' -o -name '.DS_Store' -o -name '.env' \) -type f -delete
find "$OUT" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$OUT" -type d -empty -delete 2>/dev/null || true

# Verify nothing forbidden remains.
BAD="$(find "$OUT" \( -name '*.db*' -o -name '.env' -o -name '*.bak-*' -o -name 'WATCHERS.md' \
  -o -name 'session*.json' -o -name '__pycache__' -o -name '.DS_Store' -o -name '*.pem' -o -name '*.key' \
  -o -path '*/aws/*' -o -path '*/global-protect/*' -o -path '*/slack/*' -o -path '*/ui_demos/*' \
  -o -path '*/docs/cognitive-overhead/*' -o -path '*/.github/copilot-instructions.md' \) | head -20)"
if [ -n "$BAD" ]; then echo "FAIL: forbidden files in export:"; echo "$BAD"; exit 1; fi

echo "Export of $(git -C "$REPO" rev-parse --short "$REF") -> $OUT"
echo "Top-level manifest:"; (cd "$OUT" && du -sk * .[!.]* 2>/dev/null | sort -rn | head -40)
echo "Files: $(find "$OUT" -type f | wc -l)  Total: $(du -sh "$OUT" | cut -f1)"
(cd "$OUT" && find . -type f -exec ls -l {} + | awk '{print $5"\t"$9}' | sort -rn | head -15 | sed 's/^/largest: /')
echo "OK"
