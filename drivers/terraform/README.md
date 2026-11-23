# Terraform driver (`terraform`)

Package driver for HCP Terraform (Terraform Cloud) and Terraform Enterprise. CLI in `cli/`, watcher in `watch/`, skill
template in `skill/`, manifest `driver.yaml`. Off by default; one instance per host + organization.

```yaml
integrations:
  tf-main:
    driver: terraform
    enabled: true
    base_url: https://app.terraform.io      # or your TFE host
    auth: { type: api_token, token: ${env:TFE_TOKEN} }   # user/team/organization token; ${file:}/${keychain:} also work
    options: { organization: my-org, title: Landing zones }
```

Put the token in `dop-agent/.env` (`TFE_TOKEN=...`), then restart the daemon and press Test in Settings
(steps: config, auth, organization). The skill `terraform-<instance>` documents the commands.

- Read: workspaces, runs + plan summary, policy checks, state outputs (names/types; values only for non-sensitive
  outputs with `--values`), variable names (never values), drift (health assessment results).
- Writes (`trigger`, `apply`, `discard`, `cancel`) are dry runs without `--yes`; `apply`/`discard`/`cancel` refuse
  runs that are not in a matching state.
- Watchers: `terraform-run-finished`, `terraform-needs-approval`, `terraform-drift` (first run = silent baseline).
  Drift needs health assessments enabled on the workspaces.
- Run and workspace URLs under the instance's `base_url` get unfurl cards.
- Token scope: a team/organization token with read access (plus run permissions only if you want writes).
