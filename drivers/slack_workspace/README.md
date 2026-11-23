# Slack workspace driver (`slack_workspace`)

Package driver for Slack workspaces **other than the bot's own** (independent of `adapters.py`/`handlers.py`).
Off by default; one `slack-ws-<instance>` skill per enabled instance.

## Configure (`~/.dop-agent/config.yaml`)
```yaml
integrations:
  partner-slack:
    driver: slack_workspace
    enabled: true
    base_url: https://<team>.slack.com
    auth: { type: token, token: '${env:SLACK_PARTNER_TOKEN}' }   # or ${file:..} / ${keychain:..}
    options: { write_mode: confirm }                              # none = read-only; api_url, title optional
```
Token: user token `xoxp-` (scopes `channels:read groups:read im:read mpim:read channels:history groups:history
im:history mpim:history users:read users:read.email search:read chat:write`) or a bot token `xoxb-` (no `search`;
sees only joined channels). Use Settings → Test to verify (`config`, `auth`, `channels`, `search` steps).

## CLI
`test`, `fetch`, `channels`, `history`, `thread`, `search`, `user`, `users`, `permalink`, `post` (dry-run draft
until `--yes`; blocked with `write_mode: none`). Tokens are never printed.

## Watchers (script, read-only; first run = baseline)
`slack-ws-new-messages`, `slack-ws-mentions`, `slack-ws-thread-replies`, `slack-ws-channel-threads`
(`--channel` ID or `#name`, repeatable; `slack-ws-thread-replies` also needs `--thread-ts`; optional `--keyword`,
`--include-self`, `--days`, `--limit`; see `watch/slack_watch.py`). Link cards for message permalinks (matched by `base_url`).
Tests: `tests/test_slack_workspace_driver.py` (fake Web API).
