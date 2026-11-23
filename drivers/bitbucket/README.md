# Bitbucket driver (`bitbucket`)

Package driver for Atlassian Bitbucket **Cloud** and **Server/Data Center**: repos, branches, commits/compare, pull
requests (status, reviewers, comments/threads, build statuses), Cloud pipelines (steps, log tail), draft-first writes,
token-free watchers and link cards. Off by default; no host is built in; one instance per host/token; each enabled
instance gets its own skill `bitbucket-<instance>`.

```yaml
integrations:
  bb-cloud:
    driver: bitbucket
    enabled: true
    base_url: https://bitbucket.org
    auth: { type: private_token, token: "${env:BB_CLOUD_TOKEN}", username: "me@example.com" }  # API token + e-mail (Basic)
    options: { workspace: my-workspace }     # default workspace for cross-repo scans
  bb-dc:
    driver: bitbucket
    enabled: true
    base_url: https://bitbucket.example.com   # Server/DC (flavor auto); context path allowed
    auth: { type: private_token, token: "${file:~/.dop-agent/secrets/bb-dc}" }   # HTTP access token (Bearer)
```

- Options: `flavor` (`auto`|`cloud`|`server`; auto = Cloud for `bitbucket.org`, else Server), `api_url`, `workspace`, `title`. REST root: Cloud `https://api.bitbucket.org/2.0`, Server
  `<base_url>/rest/api/1.0` (`options.api_url` overrides). `username` set -> Basic, else Bearer (workspace/repository/
  project/HTTP access token). Needed permissions: read repositories + pull requests (+ pipelines on Cloud); write
  scopes only for `pr-comment/approve/merge/create`. Cloud watchers need a user token (`/user`); a repository access
  token has no user, so "me" watchers do not work with it (use `bitbucket-pr`/`bitbucket-pipeline` targets there).
- Refs: `<repo>` = `workspace/slug` (Cloud) or `PROJECT_KEY/slug` (Server); PR ref `<repo>#<n>` or a PR URL.
- `test` -> `{ok, message, steps}` (requests, config, reach, auth); `fetch --kind pull_request|pull_request_server|pipeline`.
- Writes only print `DRAFT ...` unless `--yes`; `api` takes relative REST paths only (token never leaves the host).
- Watchers: `bitbucket-review-requested`, `bitbucket-my-pr-notes`, `bitbucket-my-pr-checks`, `bitbucket-pr --ref <repo>#<n>`,
  `bitbucket-pipeline --ref <repo>#<build>` (Cloud) / `<repo>@<branch>` (both). Exit 0 silent / 200 changed; first run =
  baseline; state `bitbucket-<instance>-<mode>[-<target>].json` under `WATCH_STATE_DIR`. Cloud has no cross-repo PR
  search: scans cover `--repo` or the newest 20 repos of `options.workspace`; Server uses the dashboard.
- Server has no pipelines: `build-status`/`bitbucket-pipeline` use commit build statuses.

Tests: `tests/test_bitbucket_driver.py` (fake backend).
