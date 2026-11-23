#!/usr/bin/env bash
# Usage: init_repo.sh <out-dir>   Env: PUBLIC_GIT_NAME (default "DevOps Pass"), PUBLIC_GIT_EMAIL (required),
# FORCE_EMAIL=1 to allow a non-noreply email, TAG=0 to skip tag v0.1.0.
set -euo pipefail
OUT="$(cd "${1:?usage: $0 <out-dir>}" && pwd)"
NAME="${PUBLIC_GIT_NAME:-DevOps Pass}"; EMAIL="${PUBLIC_GIT_EMAIL:?set PUBLIC_GIT_EMAIL (use <id>+<user>@users.noreply.github.com)}"
if [[ "$EMAIL" != *@users.noreply.github.com ]]; then
  echo "WARNING: $EMAIL is not a GitHub noreply address. It will be permanently public in every commit"
  echo "         and cannot be removed without rewriting history. Prefer <id>+<user>@users.noreply.github.com"
  echo "         (GitHub > Settings > Emails > 'Keep my email addresses private')."
  [ "${FORCE_EMAIL:-0}" = 1 ] || { echo "REFUSING. Re-run with FORCE_EMAIL=1 to confirm consciously."; exit 1; }
  echo "FORCE_EMAIL=1 set: continuing with $EMAIL"
fi
[ -d "$OUT/.git" ] && { echo "ERROR: $OUT already a git repo" >&2; exit 1; }
cd "$OUT"
git init -q -b main
git config user.name "$NAME"; git config user.email "$EMAIL"
HOOKS="$(mktemp -d)"; git config core.hooksPath "$HOOKS"   # empty: no trailer-adding hooks
git add -A
git -c commit.gpgsign=false commit -q -m "chore: initial public release"
if [ "${TAG:-1}" = 1 ]; then git -c tag.gpgsign=false tag -a v0.1.0 -m "v0.1.0"; fi
git config --unset core.hooksPath
git log --format='%h %an <%ae> %s' -1; git log -1 --format=%B | grep -ci 'co-authored' | sed 's/^/co-authored trailers: /'
git tag -l
cat <<TXT

Remaining manual steps:
 1. Create empty repo github.com/devopspass/dop-agent (no README/license).
 2. cd $OUT && git remote add origin git@github.com:devopspass/dop-agent.git
 3. git push -u origin main && git push origin v0.1.0
 4. Settings: branch protection on main, enable secret scanning + push protection, Dependabot, private vuln reporting.
 5. gh release create v0.1.0 --title v0.1.0 --notes-file <notes>
 See docs/RELEASE_RUNBOOK.md for post-release checks.
TXT
