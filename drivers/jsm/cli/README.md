# JSM driver CLI

`cli.py` is a stdlib-only CLI (no third-party packages) for Jira Service Management. Usage: see the module docstring
(`python cli.py --help` prints it). Two backends, one token:

| API | Base | Used for |
|---|---|---|
| `desk` | `<base_url>/rest/servicedeskapi` | requests, queues, SLAs, approvals, comments, transitions, create |
| `ops` | `https://api.atlassian.com/jsm/ops/api/<cloudId>/v1` | alerts, schedules, on-call, teams |
| `jira` | `<base_url>/rest/api/3` | `/myself` (Test), user-name lookup for on-call |

Auth: HTTP Basic `email:token`; the token comes from `integrations.<instance>.auth.token` (a `${env:X}`/`${file:P}`/`${keychain:S}` ref resolved by `jsmconfig.py`; selected with `--instance`; never printed, scrubbed from error text).
The cloud id is detected from `<base_url>/_edge/tenant_info` unless `options.cloud_id` is set. Writes support `--dry-run`.
`fetch`/`test` are used by dop-agent (furl cards, Settings Test button). Watcher script: `../watch/jsm_watch.py`.
