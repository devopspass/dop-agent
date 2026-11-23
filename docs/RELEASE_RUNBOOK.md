# Public release runbook (v0.1.0)

Run from the main repo checkout on the integration branch. Scripts live in `dop-agent/scripts/release/`.

## 1. Integration merge

Create `integration/public-release` from `feature/dop-agent-rename` and merge in this order
(each later branch builds on files touched by earlier ones):

1. `feature/public-licensing` (LICENSE, pyproject)
2. `feature/public-privacy` (cleanup, `.github/skills` -> `dop-agent/skills`, `check_public_tree.sh`)
3. `feature/public-docs` (README, community files)
4. `feature/public-screenshots`
5. `feature/public-ci` (Docker, systemd, workflows, `.gitleaks.toml`)
6. `feature/public-security1`
7. `feature/public-security2`
8. `feature/public-export` (these scripts)

Conflict hotspots: `README.md` (docs vs. privacy vs. screenshots), `requirements.txt` / `pyproject.toml`
(licensing vs. ci vs. security), `bot_ctl.sh` (privacy vs. ci vs. security), `.gitignore`, `.github/`.
Resolve by keeping both intents; re-run tests after each merge.

## 2. Pipeline

```bash
REF=integration/public-release; OUT=/tmp/dop-agent-public
dop-agent/scripts/release/export_public.sh $REF $OUT   # git archive, denylist, manifest
dop-agent/scripts/release/scan_export.sh $OUT          # must end with RESULT: PASS
dop-agent/scripts/release/verify_export.sh $OUT        # venv, py_compile, tests, Web UI smoke
PUBLIC_GIT_EMAIL='<id>+<user>@users.noreply.github.com' dop-agent/scripts/release/init_repo.sh $OUT
```

Notes:
- `export_public.sh` refuses a non-empty out-dir; only tracked files at `$REF` are exported (ignored files cannot leak).
- `scan_export.sh` needs `brew install gitleaks trufflehog`; detect-secrets and pip-licenses are auto-installed into `/tmp` venvs.
  gitleaks uses `<out>/.gitleaks.toml` if present, else `scripts/release/gitleaks.default.toml`.
  detect-secrets compares with `<out>/.secrets.baseline` (else `scripts/release/secrets.baseline`, absent = every finding fails;
  generate one only after reviewing each finding).
  Banned-term allowlist (vendored/minified JS, images) is `ALLOW_PATHS` in the script; justify any additions.
  Review `/tmp/uniq-emails.txt`, `/tmp/uniq-hosts.txt`, `/tmp/uniq-ips.txt` manually (version-like strings appear as IPs).
- `init_repo.sh` refuses non-`@users.noreply.github.com` emails unless `FORCE_EMAIL=1`. A personal address becomes
  permanently public in history; choose consciously. `TAG=0` skips the `v0.1.0` tag.

## 3. Checklist

- [ ] Old `devopspass/devopspass` repo deleted by owner; empty `devopspass/dop-agent` created (no README/license)
- [ ] Branches merged in the order above, conflicts resolved
- [ ] `pytest` green on the integration branch; `check_public_tree.sh` passes
- [ ] `export_public.sh` OK, manifest reviewed
- [ ] `scan_export.sh` RESULT: PASS (gitleaks, trufflehog, detect-secrets, banned terms)
- [ ] Emails/hostnames/IPs lists reviewed; every PNG opened and checked
- [ ] `pip-licenses` reviewed (note pymupdf is AGPL: confirm compatibility with chosen license)
- [ ] `verify_export.sh` VERIFY OK
- [ ] Git identity decided (noreply vs `FORCE_EMAIL=1`)
- [ ] `init_repo.sh` run; `git log` shows one commit, no Co-authored-by; tag `v0.1.0`
- [ ] `git remote add origin ...; git push -u origin main; git push origin v0.1.0`
- [ ] Branch protection on `main`; secret scanning + push protection; Dependabot; private vulnerability reporting
- [ ] `gh release create v0.1.0 --title v0.1.0 --notes-file <notes>`
- [ ] Post-release: Actions green; fresh clone passes `verify_export.sh`; README renders (images, links);
      Issues/PR templates appear; repo description/topics set; delete `/tmp` export and scan artifacts
