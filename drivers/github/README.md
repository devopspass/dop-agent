# GitHub driver (`github`)

Package driver for GitHub (github.com and GitHub Enterprise Server/Cloud): PRs (status, review threads, checks,
review/merge/comment), Actions runs and job logs, branches/tags, issues, repo/user lookup, token-free watchers and
link cards. Self-contained: CLI in `cli/`, watcher in `watch/`, skill template in `skill/`, manifest in `driver.yaml`,
`icon.svg`. No host is built in; off by default. One instance per host/token; each enabled instance gets its own skill
`github-<instance>` (rendered before every session).

```yaml
integrations:
  gh-com:
    driver: github
    enabled: true
    base_url: https://github.com            # the WEB url
    auth: { type: private_token, token: "${env:GITHUB_TOKEN}" }   # env/file/keychain ref; classic scopes repo+workflow
    options: { title: GitHub.com }
  gh-corp:
    driver: github
    base_url: https://ghe.example.com       # GitHub Enterprise Server
    auth: { type: private_token, token: "${file:~/.dop-agent/secrets/ghe-token}" }
    # options: { api_url: https://api.ghe.example.com }   # only when the API is not at <base_url>/api/v3
```

API endpoints are derived from `base_url`: `github.com` → `https://api.github.com`, `<sub>.ghe.com` →
`https://api.<sub>.ghe.com`, anything else → `<base_url>/api/v3` (GraphQL `/api/graphql`); `api_url` overrides.
The token is resolved by `cli/ghconfig.py` from the instance (never in argv/env/skills, never printed). Enterprise
SSO-protected orgs need the token authorized for SSO.

## CLI
`python <driver>/cli/cli.py --instance <name> <command>`; every command is listed in the skill and in `cli.py -h`.
`test` returns `{ok, message, steps}` (requests, config, reach, auth, scopes, graphql); `fetch --kind pull_request|issue|run
--ident '{"repo":"o/r","id":"1"}'` returns the link-card JSON. Refs are explicit: `owner/name` and `owner/name#<n>`.
Writes execute immediately (the skill requires drafting in chat first); `api` non-GET needs `--yes`.

## Watchers (`watch_script(script="github_watch.py", watcher=<recipe>, integration=<instance>)`)
Recipes: `github-review-requested`, `github-assigned`, `github-my-pr-checks`, `github-my-pr-notes`, `github-pr`
(`--ref o/r#N`), `github-run` (`--ref o/r#RUN_ID`), `github-branch-runs` (`--repo --branch --workflow`).
Exit 0 silent / 200 changed / other = error; first run = baseline; state `github-<instance>-<mode>[-<target>].json`
under `WATCH_STATE_DIR` (ids/cursors only); 429/5xx/rate limit = silent skip. All watchers are token-free script
watches; `github-pr` and `github-run` report merged/closed/finished so the watch can be removed.

## Link cards
`https://<host>/<owner>/<repo>/pull/<n>`, `/issues/<n>`, `/actions/runs/<id>` of an enabled instance's host.

Tests: `tests/test_github_driver.py`.
