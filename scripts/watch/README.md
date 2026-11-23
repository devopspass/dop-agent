# Allow-listed watch scripts

Scripts run by script-only `time` watches (`meta.script`) and `watch_script` watches; see `watch_scripts.py`
and README "Watcher token savings". Resolution is by basename over: this directory (empty by default),
each `drivers/*/watch/` (in git, allow-listed), `~/.dop-agent/watch-plugins/` and `~/.dop-agent/drivers/*/watch/`
(user-written, not in git; need one `watch_script(..., dry_run=True)` per script version before a watch may use them).
Rules: basename only, `chmod 755`, no symlinks, not group/world-writable, read-only by convention,
print the notice to stdout, exit non-zero on failure (auth/VPN) so the model is woken.

## `script` watches (exit-code contract) — `watch_script` MCP tool

| Exit code | Meaning | Bot action |
|---|---|---|
| 0 | nothing new | silent |
| 200 | changed | wake the session: prompt + output head + path of the full output file |
| anything else / timeout | error | after `retries` consecutive failures: wake the session and stop the watch |

* stdout+stderr are truncated in the wake text; the raw output (≤1 MB, all kept, never pruned) is in `~/.dop-agent/watch-out/<id>/`.
* The script owns "what changed": keep previous state under `$WATCH_STATE_DIR` (per watch, persists across runs). `$WATCH_ID` is also set.
* Secrets: read token files by path (`~/.jira-token`); never pass them as args.
* Schedule: `cron:<5 fields, local time>` (e.g. `cron:*/10 * * * *`), `in:30m`, `at:<YYYY-MM-DDTHH:MM>`. First run happens immediately (`run_immediately`, default true) so scripts can record a baseline.
* Optional: `until_changed`, `max_runs`, `expires_in` (`7d`), `retries`. No defaults for limits.
* Example: `drivers/jsm/watch/jsm_watch.py alerts` (see the JSM section below): `watch_script(session_id=<id>, script="jsm_watch.py", watcher="jsm-alerts", integration=<instance>, schedule="cron:*/10 * * * *", prompt="triage new/closed alerts")`.
* Needs `croniter` in the bot's python3.13 (`pip install croniter`); `time` watches accept full cron keys too.

## `jsm_watch.py` — Jira Service Management watchers

Lives in `drivers/jsm/watch/` (package driver; `watch_script(..., watcher=<recipe>, integration=<instance>)`). Read-only; modes `alerts` (Ops alerts opened/closed, `--query`), `queue` (`--desk --queue`, new issues), `requests` (new / status change), `approvals` (newly waiting for my approval), `sla` (breached or due within `--minutes`, each once), `oncall` (`--schedule`/`--name`, on-call people changed). Recipes `jsm-alerts|jsm-queue|jsm-requests|jsm-approvals|jsm-sla|jsm-oncall`. State `jsm-<mode>-<hash>.json` (ids/status only); first run = silent baseline; scans bounded by `--limit`; 429/5xx/network = silent skip, other errors exit 1. The instance (`--instance` / `JSM_INSTANCE`, set by the driver) is resolved from config.yaml by the CLI; the token never goes through args/env.

## `teams_watch.py` — Microsoft Teams watchers

Lives in `drivers/ms_teams/watch/` (package driver; create with `watch_script(..., watcher=<recipe>, integration=<instance>)`; see that driver's skill). Read-only, token-free polling through `drivers/ms_teams/cli` (token from the signed-in Teams tab via CDP). One script, mode = first arg;
state in `teams-<mode>.json` under the watch state dir (cursors/ids only, no message text). First run = silent baseline; cursors only advance, so no duplicates.
Wake text is HTML-stripped and cut to `--max-chars` (200). CDP tab/token missing => exit 1 (wake after `retries`, then the watch stops; no spam). 429/5xx => silent skip.
`--cdp-url` / `--account` are forwarded as `TEAMS_CDP_URL` / `TEAMS_ACCOUNT` (honoured by `drivers/ms_teams/cli/auth.py`; with `integration=<instance>` they are injected from that instance's options, not from the global env allow-list).
Any-chat modes make one conversation-list call per tick and read only chats whose last message changed. Suggested schedule: `cron:*/2 * * * *` (messages), `cron:*/5 * * * *` (calendar, transcripts).

| Need | Args |
|---|---|
| New message in chat | `messages --chat <chatId>` |
| New message from user(s) (chat or any chat) | `messages [--chat ID] --from "Name" [--from "Other"]` |
| Message matching text/regex | `messages [--chat ID] --match "regex"` |
| @mention of me | `messages --mentions-me --exclude-self` |
| New file shared | `messages [--chat ID] --files --kind any` |
| Meeting started | `meeting-started` |
| Meeting in N minutes | `meeting-soon --minutes 10` |
| New meeting created | `meeting-created [--days 30]` |
| Meeting cancelled/rescheduled | `meeting-changed [--days 30]` |
| Transcript ready | `transcript [--chat ID]` |
| Recording available | `transcript --what recording [--chat ID]` |

Example: `watch_script(session_id=<id>, script="teams_watch.py", args=["messages","--chat","19:...@thread.v2","--from","Alice"], schedule="cron:*/2 * * * *", prompt="Alice wrote in the chat: summarize and draft a reply")`.
Not implemented (needs extra scopes/endpoints): presence change (Graph `Presence.Read`), missed call, unread-chat digest.

## `gitlab_watch.py` — GitLab watchers

Lives in `drivers/gitlab/watch/` (package driver; `watch_script(..., watcher=<recipe>, integration=<instance>)`). Read-only; modes `review-requested`, `assigned`, `my-mr-pipelines`, `my-mr-notes`, plus targeted `mr`, `pipeline`, `job`; "me" = the token's user. State `gitlab-<instance>-<mode>.json` (ids/cursors only); first run = silent baseline; scans bounded by `--limit` (30) MRs and 5 notes per MR; 429/5xx = silent skip, other errors exit 1. Token/host come from the instance in `config.yaml` (never args).

## `github_watch.py` — GitHub watchers

Lives in `drivers/github/watch/` (package driver; `watch_script(..., watcher=<recipe>, integration=<instance>)`). Read-only; modes `review-requested`, `assigned`, `my-pr-checks`, `my-pr-notes` ("me" = the token's user), plus targeted `pr --ref owner/name#N` and `run --ref owner/name#RUN_ID`, and `branch-runs --repo R [--branch B] [--workflow W]`. State `github-<instance>-<mode>[-<target>].json` (ids/cursors only); first run = silent baseline (`run` reports an already finished run once); scans bounded by `--limit` (30) PRs and 5 items per stream per PR; 429/5xx/rate limit = silent skip. Details in `drivers/github/README.md`.

## `confluence_watch.py` — Confluence watchers

Lives in `drivers/confluence/watch/` (package driver; `watch_script(..., watcher=<recipe>, integration=<instance>)`). Read-only; modes `cql <CQL>`, `space <KEY>`, `page <ID>` (new version or footer comments); flags `--new-only`, `--limit N` (30, max 100). State `confluence-<mode>-<hash>.json` (ids/version numbers only); first run = silent baseline; 429/5xx/network = silent skip, other errors exit 1. Site/account/token file come from the instance config (never args).

## `outlook_watch.py` — Microsoft Outlook watchers

Lives in `drivers/ms_outlook/watch/` (package driver; run via `watch_script(..., watcher=<recipe>, integration=<instance>)`). Read-only, token-free polling through the driver's `cli/api.py` (Outlook REST, GET only, Teams-tab token).
One script, mode = first arg; state in `outlook-<mode>.json` (message ids + cursors, never text). First run = silent baseline; cursors only advance, no duplicates.
Wake text = time, sender, subject (80 chars), snippet (`--max-chars 120`, `--no-snippet` to drop), max `--limit 10` items then `+N more`; bodies are never printed.
Account/browser: injected from the instance as `OUTLOOK_ACCOUNT`/`OUTLOOK_CDP_URL` (one browser profile per account).
Polling: no delta queries; each tick is 1 call with `receivedDateTime ge <cursor>` + client-side id dedup, so it is cheap and stateless-safe. Suggested `cron:*/5 * * * *` mail, `*/2` for event-soon, daily cron for digest.
Token: the Teams-tab token is re-grabbed automatically when expired (`auth.tab_token` reloads/opens a Teams tab, as in teams_watch).
Failures: login expired / 401 / 403 => exit 1 (driver wakes after `retries`, then stops the watch; use `retries=3`); 429/5xx => silent skip.

| Need | Args |
|---|---|
| New mail (inbox / folder) | `mail [--folder sentitems / "Projects" / <id>]` |
| From sender(s) / domain(s) | `mail --from a@b.com --from @domain.com` (repeat `--from` for your own list; no config) |
| Subject / text / regex | `mail --subject "^invoice" --match "regex on subject+preview"` |
| With attachment | `mail --attachment` |
| Importance high / flagged | `mail --importance high` / `mail --flagged` |
| Reply to a mail I sent | `thread --reply-to <sent message id>` (or `--conversation ID`); my own messages ignored |
| New calendar invite | `invite` |
| Meeting updated / rescheduled | `meeting-updated` (mail based) or `event-changed [--days 14]` (calendar snapshot, also cancel) |
| Meeting cancelled | `meeting-cancelled` |
| Response to my invite | `invite-response [--response declined]` (mail) or `my-invite-responses [--response any]` (snapshot) |
| Event starts in N min | `event-soon --minutes 10` |
| Unread above threshold | `unread --threshold 20` (fires on crossing, re-arms when it drops) |
| Out-of-office / auto-reply | `autoreply [--conversation ID]` |
| Bounce / NDR | `bounce` |
| Daily unread digest | `digest --hours 24` (silent when nothing unread) |
| Flagged mail due | `task-due --hours 24` |

Example: `watch_script(session_id=<id>, script="outlook_watch.py", integration="main", args=["thread","--reply-to","<messageId>"], schedule="cron:*/5 * * * *", retries=3, prompt="Reply received: summarize and draft an answer")`.
Not implemented: Microsoft To Do tasks (needs `Tasks.Read`), mail rules, shared mailboxes (use `--account` with a delegate login).

## `artifactory_watch.py` — Artifactory watchers

Lives in `drivers/artifactory/watch/` (package driver; `watch_script(..., watcher=<recipe>, integration=<instance>)`). Read-only; modes `files` (`--path REPO/DIR [--name GLOB]... [--depth N] [--new-only]`: new / overwritten files), `token-expiry` (`--days D`, once per token), `health` (ping up/down transitions). Recipes `artifactory-new-files|artifactory-file-changes|artifactory-token-expiry|artifactory-health`. State `artifactory-<mode>-<hash>.json` (uris + checksums only); first run = silent baseline; 401/403/404/bad args exit 1, 429/5xx/network = silent skip (except `health`). Instance comes from `ARTIFACTORY_INSTANCE`, never args.

## `wiz_watch.py` — Wiz watchers
Lives in `drivers/wiz/watch/` (package driver; run via `watch_script(..., script="wiz_watch.py", watcher=<recipe>, integration=<instance>)`). Modes: `findings` (new OPEN findings, `--env/--tag/--severity/--host`), `counts` (per-severity counts changed, `--only-increase`), `rescan-done` (`--asset-id` repeatable; needs CDP tab). State in `wiz-<mode>.json`; first run = silent baseline; exit 0/200/1, network errors are a silent skip. Tenant dir/scope injected as `WIZ_DIR`/`WIZ_SCOPE_TAGS`/`WIZ_ENV_TAG_KEY`/`WIZ_DATA_CENTER`/`WIZ_CDP_URL`.

## `powerbi_watch.py` — Power BI dataset refresh watchers

Lives in `drivers/powerbi/watch/` (run via `watch_script(..., watcher=powerbi-refresh-failed|powerbi-refresh-completed, integration=<instance>, args=["--dataset","WS_GUID:DS_GUID"])`). Read-only; state `powerbi-<mode>.json` holds finished refresh request ids; first run = silent baseline; running refreshes fire once when they finish.

## `sharepoint_watch.py` — SharePoint / OneDrive watchers

Lives in `drivers/ms_sharepoint/watch/` (run via `watch_script(..., watcher=sharepoint-folder|sharepoint-file|sharepoint-site, integration=<instance>)`). Read-only Graph GETs with the Teams-tab token. Modes: `folder --path P [--recursive] [--new-only] [--name RE] [--ext vtt,docx]`, `file --target T`, `site [--path-prefix P]` (Graph delta cursor). State `sharepoint-<mode>.json` (ids/etags/cursors only); first run = silent baseline; exit 0 none/throttled, 200 changes, 1 error.
