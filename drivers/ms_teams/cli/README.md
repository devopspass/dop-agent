# Teams CLI

Read/write CLI for Microsoft Teams chats, meetings and calendar, built on
direct REST calls (no Graph SDK).

## Requirements
- A Chrome/Edge instance with remote debugging (default `http://localhost:9222`) and a signed-in
  Teams web tab (`teams.cloud.microsoft` or `teams.microsoft.com`). All tokens
  (Graph, IC3, skype) are read from that tab's MSAL localStorage cache over CDP.
- Python 3, stdlib only, plus `websocket-client` (`pip install websocket-client`).

## Token refresh
Each run reads the Teams MSAL cache over CDP. If CDP is unreachable the command fails. If no unexpired
token matches, the existing Teams tab is reloaded (or, if none exists, a temporary background tab is opened
and closed afterwards) and the cache is polled until MSAL has renewed the token. Tabs are never
activated (`Target.createTarget` with `background: true`, no `bringToFront`), so focus is not stolen from
your current app.

## Browser / account selection
Global options go before the command:
- `--cdp-url http://host:port` (env `TEAMS_CDP_URL`, default `http://localhost:9222`): CDP endpoint of the browser.
- `--account <upn/email>` (env `TEAMS_ACCOUNT`): use only the signed-in identity matching this UPN
  (matched against the MSAL account username and the token's `upn`/`preferred_username` claims), across all
  Teams tabs of that browser. If nothing matches, the error lists the accounts and Teams tabs found.

```
python3 cli.py --cdp-url http://localhost:9223 --account me@other.com chats-list
TEAMS_CDP_URL=http://localhost:9223 python3 cli.py teams-list
```
Use one browser profile per account, each with its own `--remote-debugging-port`.

## Commands
| Command | Args |
|---|---|
| `chats-list` | `[--json]` |
| `chats-read` | `<chatId> [--limit N]` |
| `transcript` | `<chatId> [--limit N] [--index I] [--save path]` |
| `send` | `<chatId\|me> <text>` |
| `cal-list` | `[--days N]` |
| `cal-create` | `<title> <startISO> <endISO> [--teams]` |
| `cal-delete` | `<eventId>` |
| `meeting-audit` | `[--days N]` |
| `meeting-set-recording` | `<joinUrl> <mode>` |
| `user-info` | `<email>` |
| `user-avatar` | `<email> [--save path]` |
| `teams-list` | |
| `fetch` | `--kind chat\|message\|meeting --ident <id>` (link card, used by dop-agent) |
| `test` | (checks CDP, Teams tab, token and Graph; prints JSON) |

`mode` for `meeting-set-recording` is one of `AutoRecordingAndTranscription`,
`AutoTranscriptionOnly`, `AutoRecordingOnly`, `Off`.

## Limitations
- Events are created without attendees (Graph would send real meeting invites).
- Recordings (mp4) cannot be downloaded; transcripts work via `transcript` (WebVTT URL
  from the call-recording chat message, IC3 token).
- Authentication is only via the CDP browser tab.

## Example
```
python3 cli.py chats-list
python3 cli.py send me "hello"
python3 cli.py cal-create "Standup" 2026-12-01T09:00:00 2026-12-01T09:30:00 --teams
python3 cli.py meeting-set-recording "<joinUrl>" AutoRecordingAndTranscription
python3 cli.py meeting-audit --days 14
```
