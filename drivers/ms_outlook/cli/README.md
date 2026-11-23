# Outlook CLI

Mail, calendar and contacts for Microsoft Outlook (part of the `ms_outlook` package driver).
Python 3 stdlib only. Compact output (tables / JSON lines, truncated bodies, short refs).

## Backend
Outlook REST `outlook.office.com/api/v2.0` with the token the signed-in **Teams web tab** already holds (read via CDP by the
`ms_teams` driver's `auth.py`). Needs a browser with remote debugging, a Teams tab for the account and `pip install websocket-client`.
No app registration. Global options go before the command: `--cdp-url` (env `OUTLOOK_CDP_URL`, default `http://localhost:9222`), `--account` (env `OUTLOOK_ACCOUNT`), `--json`.
`test` checks python deps, CDP, Teams tab, token and the Outlook API; `fetch --kind message|event --ident JSON` prints a link card.

## Commands
| Command | Args |
|---|---|
| `whoami` / `accounts` / `test` / `fetch` |  |
| `mail-list` | `[--folder inbox\|sent\|drafts\|archive\|deleted\|junk\|<name>] [--unread] [--from addr] [--subject words] [--query KQL] [--since 7d\|12h\|date] [--top N] [--preview]` |
| `mail-read` | `<ref\|id> [--full] [--chars N]` (HTML converted to text, default 1500 chars) |
| `mail-send` | `--to a,b --subject S [--cc --bcc --html] (--body T \| --body-file F\|-) [--dry-run] [--yes]` |
| `mail-reply` / `mail-forward` | `<ref> [--all] [--to ..] --body T [--dry-run] [--yes]` |
| `mail-send-draft` | `<ref> [--yes]` |
| `mail-mark` | `read\|unread <ref>...` |
| `mail-move` | `<folder> <ref>...` |
| `mail-folders` | |
| `mail-attachments` | `<ref> [--download DIR [--only N\|name]]` |
| `cal-list` | `[--days 7] [--start date] [--top N]` |
| `cal-create` | `<subject> <startISO> <endISO> [--location --body --teams] [--attendees a,b --yes]` (local time) |
| `cal-delete` | `<ref> [--yes]` |
| `cal-free` | `[--days 5] [--duration 30] [--from-hour 9] [--to-hour 18] [--weekends]` (computed from calendarView, UTC→local) |
| `contacts` | `[query] [--top N]` (personal contacts, client-side filter) |

## Short refs
List commands print `m1..mN` (mail) / `e1..eN` (events); pass them to read/reply/mark/move/delete instead of the long
message id. Refs are stored per account in `~/.outlook-cli/cache/` and overwritten by the next list of that type; created
drafts are `d1`. Full message ids work as well.

## Safety defaults
- `mail-send`, `mail-reply`, `mail-forward` **save a draft** and send nothing; `--yes` is the only way to send; `--dry-run` creates nothing.
  `mail-send-draft` also needs `--yes`.
- `cal-create` makes an event on your own calendar with no attendees. `--attendees` (sends invitations) is a dry-run unless `--yes`.
  `cal-delete` of an organized event with attendees (sends cancellations) needs `--yes`.
- Search (`--query/--subject`) uses `$search` (no folder-wide ordering; client-sorted, max 100 hits); other filters use `$filter`.
- Errors print one line (`HTTP <code> <METHOD> <path>: <detail>`).
