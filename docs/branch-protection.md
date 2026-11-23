# Branch protection (apply manually)

GitHub → *Settings → Branches → Add branch ruleset / protection rule* for `main`.

| Setting | Value |
|---|---|
| Require a pull request before merging | on, 1 approval, dismiss stale approvals on new commits |
| Require status checks to pass | on, *Require branches to be up to date* on |
| Require conversation resolution | on |
| Require linear history | on |
| Block force pushes / deletions | on |
| Do not allow bypassing the above | on (admins included) |
| Restrict tag creation for `v*` | maintainers only (tags trigger the release workflow) |

## Required status checks

Use the job names exactly as GitHub shows them:

| Workflow | Check name |
|---|---|
| CI | `lint` |
| CI | `unit (py3.12)` |
| CI | `unit (py3.13)` |
| CI | `render` |
| CI | `docker-build` |
| Secret scan | `gitleaks` |
| Secret scan | `license-check` |

`ui-tests` runs with `continue-on-error` and is informational until the Playwright suite is green; add it to the required list afterwards.

A check can only be selected as required after it has run once on the repository.

## Repository settings

- *Actions → General*: workflow permissions "Read repository contents" by default (workflows request more per job where needed, e.g. `release.yml`).
- *Code security*: enable Dependabot alerts, Dependabot security updates, secret scanning and push protection.
- Optional hardening: pin third-party actions to full commit SHAs instead of version tags (Dependabot can keep SHA pins up to date).
