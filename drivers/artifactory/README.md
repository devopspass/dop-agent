# Artifactory driver

Package driver for JFrog Artifactory (`drivers/artifactory/`): CLI, watcher script, skill template, furl cards. Off by default;
every enabled instance gets a generated skill `artifactory-<instance>`. No host is built in.

## Configure (`~/.dop-agent/config.yaml`)
```yaml
integrations:
  my-artifactory:
    driver: artifactory
    enabled: true
    base_url: https://artifactory.example.com      # server root (a trailing /artifactory is stripped)
    auth:
      type: anonymous | access_token | sso_token
      token: ${env:ARTIFACTORY_TOKEN}              # access_token only (env/file/keychain ref)
    options:
      upload_mode: rest | none                     # default rest
      cdp_url: http://localhost:9222               # sso_token only
      title: Label                                 # optional, for lists and the skill description (default: instance name)
```
Settings -> Integrations -> Test runs `cli.py test` (config, reach/ping, repository read, token, upload config).

## Auth types
| Type | Use |
|---|---|
| `anonymous` | read-only servers |
| `access_token` | token in config as a secret ref |
| `sso_token` | SAML/OIDC-only servers: `token authorize` drives a signed-in browser (CDP) through the JFrog UI, stores the minted token chmod 600 in `~/.dop-agent/driver-state/artifactory/<instance>.json` and revokes the previous one. Needs `websocket-client` |

## CLI
`python cli/cli.py --instance NAME <command>` (instance optional when exactly one is enabled): `test`, `fetch`, `repos`, `ls`, `info`,
`search`, `aql`, `get`, `verify`, `raw`, `upload`, `token`. Writes (`upload`, non-GET `raw`) print a plan and need `--yes`; `upload`
refuses to overwrite unless `--force`. Tokens are never printed.

Upload modes: `rest` = `PUT` to the repo (needs write permission), then compares sha256; `none` disables `upload`.

## Watchers (`watch/artifactory_watch.py`, via `watch_script`)
`files` (new / overwritten files below `--path REPO/DIR`; `--depth`, `--name`, `--new-only`), `token-expiry` (`--days`, default 30), `health` (server stops/resumes answering). Recipes: `artifactory-new-files`,
`artifactory-file-changes`, `artifactory-token-expiry`, `artifactory-health`. First run = silent baseline.

## Furls
Download links `<base_url>/artifactory/<repo>/<path>` and UI links `<base_url>/ui/repos/tree/{General|Properties|Builds}/<repo>/<path>` unfurl into a card
(repo, size, modified, modifiedBy, sha256). Matching is by `base_url`, so several servers never mix.
