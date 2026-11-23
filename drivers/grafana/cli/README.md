# Grafana driver CLI

Package driver `drivers/grafana/` (CLI `cli/cli.py`, watcher `watch/grafana_watch.py`, skill template
`skill/SKILL.md.tmpl`). Generic: no host is built in. Everything is configured per instance in
`~/.dop-agent/config.yaml`; several instances = several Grafana servers (one `grafana-<instance>` skill each).

```yaml
integrations:
  mygrafana:
    driver: grafana
    enabled: true
    base_url: https://grafana.example.com
    auth: { type: service_account_token, token: ${env:MYGRAFANA_TOKEN} }   # or ${file:...} / ${keychain:...}
    options: { title: "Prod Grafana", org_id: "1" }

  behind-proxy:
    driver: grafana
    enabled: true
    base_url: https://grafana.example.org
    auth: { type: oauth_cdp }
    options: { idp_url: https://auth.example.org, cdp_url: "http://localhost:9222" }
```

## Auth types
| type | Use | Notes |
|---|---|---|
| `service_account_token` | Grafana service account token (Viewer reads; Editor for dashboard changes) | Secret resolved by dop-agent's config resolver, never in argv/env/skill |
| `oauth_cdp` | Grafana behind an OAuth 2.0 proxy that only accepts tokens it minted itself | `idp_url` must serve `/.well-known/oauth-authorization-server` with dynamic client registration. `auth authorize` drives a signed-in CDP browser (SSO is silent, one "Authorize" click is automated), then access tokens refresh silently (`auth refresh`, also automatic on use). State: `~/.dop-agent/grafana-state/<instance>.json` (under `$DOP_AGENT_HOME` if set; chmod 600). Needs `websocket-client` for `authorize` only |

An OAuth proxy typically provisions a separate synthetic Grafana user with narrow RBAC. Check
`permissions --filter dashboards` before concluding that nothing is writable: grants can come from RBAC while the
org role is Viewer.

## Commands
`test`, `fetch`, `whoami`, `search`, `dashboard get|versions|save|move|delete`, `folder list|get`, `datasources`,
`query prom|loki`, `alerts`, `permissions`, `api`, `auth status|authorize|refresh`. Run `cli.py -h` / `<cmd> -h`.
Writes (dashboard save/move/delete, non-GET `api`) are dry runs unless `--yes` is passed.

## Watchers (`watch/grafana_watch.py`)
Modes `alerts`, `dashboard-changed`, `datasource-health`; recipes `grafana-alerts-firing`,
`grafana-dashboard-changed`, `grafana-datasource-health`. Read-only; first run records a baseline silently; state in
`$WATCH_STATE_DIR`; transient errors (429/5xx/network) are silent skips, auth errors exit 1.

## Tests
`dop-agent/tests/test_grafana_driver.py`.
