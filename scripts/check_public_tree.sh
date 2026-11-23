#!/usr/bin/env bash
# Fails if the tree contains org/person-specific terms that must not be published.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PATTERN='luminor|epam|dsm|\banh\b|solco|\bessco\b|panavora|noviwest|hariton|aliaksei|kharyton|onelum|corebanking|flexcube|fcubs|U6GH4SJTH|/Users/[A-Za-z]|git\.epam|atlassian\.net/browse/[A-Z]+-[0-9]+'
cd "$ROOT" || exit 2
hits=$({ grep -rIniE "$PATTERN" . \
  --exclude-dir=.git --exclude-dir=vendor --exclude-dir=node_modules --exclude-dir=__pycache__ \
  --exclude-dir=.pytest_cache --exclude=check_public_tree.sh --exclude=scan_export.sh --exclude=.git
  # /Users/<name> is only a violation with a real-looking case-sensitive prefix
  } | grep -viE '/users/me\b|(example|acme|evil)\.atlassian\.net/browse/[A-Z]+-[0-9]+' | cut -c1-200)
if [[ -n "$hits" ]]; then
  echo "$hits"
  echo "check_public_tree: banned terms found" >&2
  exit 1
fi
echo "check_public_tree: clean"
