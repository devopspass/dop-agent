# Wiz driver (`wiz`)

Package driver for [Wiz](https://www.wiz.io/): vulnerability findings, fleet snapshots/diffs, on-demand rescans, watchers
and link cards. Self-contained: CLI in `cli/`, watcher in `watch/`, agent skill template in `skill/`, manifest in
`driver.yaml`. Off by default; enable one instance per Wiz tenant in Settings → Integrations (or `config.yaml`):

```yaml
integrations:
  wiz-prod:
    driver: wiz
    enabled: true
    auth: { type: wizcli }            # or: { type: service_account, client_id: ${env:WIZ_CLIENT_ID}, client_secret: ${env:WIZ_CLIENT_SECRET} }
    options:
      wiz_dir: ~/.wiz-prod          # one wizcli directory per tenant
      scope_tags: application=billing   # optional, default none (whole tenant)
      env_tag_key: environment      # tag the --env filter matches
      data_center: auto             # or eu1, us2, ...
      cdp_url: http://localhost:9222    # only for rescan
      title: Prod tenant
```

Each enabled instance gets the skill `wiz-<instance>` (rendered into `~/.dop-agent/skills-gen/`) and watcher recipes
`wiz-new-findings`, `wiz-counts`, `wiz-rescan-done` (`watch_script(..., watcher=..., integration=<instance>)`).

## Auth
- Install the CLI on macOS with `brew install --cask wizcli` (other platforms: Wiz docs).
- Reads: the OAuth token of `wizcli` (`WIZ_DIR=<wiz_dir> wizcli auth --use-device-code` or a service account), refreshed
  under an flock on `<wiz_dir>/.refresh.lock`.
- Credentials follow the gitlab driver convention: auth type `wizcli` stores nothing; type `service_account` keeps
  `client_id`/`client_secret` in `config.yaml` as `${env:X}`/`${file:P}`/`${keychain:S}` refs (Settings marks them secret).
  The CLI (`--instance`) and the watcher (`WIZ_INSTANCE`) resolve them by instance name via `wiz_config.py` and pass them
  only in the env of the `wizcli auth --print-token` subprocess - never via argv, skills or output.
- Rescan: the mutation only accepts the web UI's session cookies, so it runs `fetch()` inside a logged-in app.wiz.io tab
  over CDP (`pip install websocket-client` for the bot's Python).
- The **Test** button runs `cli.py test`: python deps → wizcli → auth file → token refresh + GraphQL probe → (info) CDP.

## CLI (`cli/cli.py`, global options before the command)
`test`, `auth-status`, `fetch`, `findings`, `count-by-path`, `snapshot collect|diff`, `rescan` (dry run unless `--yes`),
`rescan-status`. See the skill template for flags and gotchas (totalCount cost guard, queued ≠ finished, cooldown,
`Unauthorized`). Modules are `wiz_*.py` (unique names: several drivers' CLIs are imported in one test process).

## Link cards
Kinds `issue` and `finding` match path-style portal links. Wiz deep links are hash-routed and dop-agent ignores URL
fragments; links also carry no tenant id, so with several enabled Wiz instances a link stays plain. The card GraphQL
shapes are unverified against a live tenant (an error just means no card).

Tests: `dop-agent/tests/test_wiz_driver.py`.
