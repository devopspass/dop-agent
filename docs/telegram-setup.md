# Enable the Telegram adapter

Architecture: [telegram-architecture.md](telegram-architecture.md). The adapter is **off** until a token and an allow-list are configured. It uses long polling, so the host only needs **outbound** HTTPS to `api.telegram.org`.

## 1. Create the bot

1. In Telegram open **@BotFather** -> `/newbot` -> pick name/username -> copy the token.
2. For group use keep BotFather `/setjoingroups` enabled; `/setprivacy` -> Disable is not needed when the bot is group admin.
3. Get your numeric user id from **@userinfobot** (repeat for every person who may use the bot).

## 2. Create the forum group (recommended per-session view)

1. New **private** group (add the bot, or add it afterwards).
2. Group settings -> **Topics** -> enable (it becomes a forum supergroup).
3. Make the bot **admin** with *Manage Topics* (and *Delete messages*, optional).
4. Get the chat id (starts with `-100`): forward a group message to **@RawDataBot**, or take it from the web-client URL (`-100` + number).

Without a group the bot works in **DM mode** (one active session per chat, `/new` for another).

## 3. Configure

**Web UI:** *Settings -> Integrations -> Chat adapters -> Telegram -> Edit*. Store the token (secret file, keychain or env ref), set the allowed user ids and optional forum chat id, press **Test connection** (`getMe`, plus a forum/Topics check when a chat id is set) and **Save**. The enable switch is there too. Changes need a daemon restart (*Advanced -> Restart bot*). Slack has the same card.

**Or `dop-agent/.env`:**

```
TELEGRAM_BOT_TOKEN=123456:ABC...          # or TELEGRAM_BOT_TOKEN_FILE=/path (chmod 600)
TELEGRAM_ALLOWED_USER_IDS=111111111,222222222
TELEGRAM_FORUM_CHAT_ID=-1001234567890     # empty = DM mode
NEW_SESSION_PLATFORM=telegram             # optional: new Web UI sessions also get a topic
# TELEGRAM_MAX_UPDATE_AGE=600  TELEGRAM_POLL_TIMEOUT=50   # optional tuning
```

`TELEGRAM_ALLOWED_USER_IDS` is authoritative: ids removed from the list are revoked on the next start. An empty list keeps Telegram off.

## 4. Start

```
dop-agent/bot_ctl.sh restart
grep -i telegram ~/.copilot/dop-agent-error.log | tail
```

Expected: `Telegram adapter registered (forum chat: yes, allowed users: N)`. The self-check logs actionable errors (not a forum, missing Manage Topics).

## 5. Use

- Post in **General** -> a new topic (session) is created and the answer streams there; reply in the topic to continue.
- `/new [title]` creates a topic (group) or a new numbered session (DM). `/stop` interrupts the running turn, also available as the Stop button. `/help` shows commands.
- Renaming/archiving the session in the Web UI renames/closes the topic.

## Turn it off

Set `adapters.telegram.enabled: false` (Settings -> Integrations -> Chat adapters, or the config file) and restart. Slack/Web are unaffected.

## Troubleshooting

| Symptom | Check |
|---|---|
| No "Telegram adapter registered" | token set? allow-list non-empty? `adapters.telegram.enabled` true? token file mode 600? |
| `Telegram rejected the bot token (401/404)` | wrong/revoked token (re-issue in BotFather). |
| Bot silent for you | your numeric id is not in `TELEGRAM_ALLOWED_USER_IDS` (denied ids are logged); strangers are dropped silently by design. |
| `not a forum ... falling back to DM mode` | enable Topics in the group; chat id must start with `-100`. |
| `lacks 'Manage Topics'` | give the bot the admin right. |
| Repeated 409 in log | another process uses the same token (old poller, other host) or restart overlap (clears in <= ~60 s). |
| Old messages not answered after downtime | by design: updates older than `TELEGRAM_MAX_UPDATE_AGE` are dropped. |
| Button says "expired" | the token row was purged (older than 30 days) - ask again. |

## Manual test checklist (needs real Telegram)

Run the checklist in [../README.md](../README.md#manual-test-checklist-needs-real-telegram) once after enabling; automated tests use a fake Bot API, not the real one.

## Notes from the former README

## Telegram

Docs: [architecture](docs/telegram-architecture.md), [setup guide](docs/telegram-setup.md).

Optional third surface next to Slack and the Web UI. **Off unless `TELEGRAM_BOT_TOKEN` is set** (and `TELEGRAM_ALLOWED_USER_IDS` is non-empty - fail closed). Never changes Slack/Web behaviour; a Telegram failure is logged and swallowed.

- **Transport:** long polling `getUpdates` over outbound HTTPS only (no webhook, no inbound port; `deleteWebhook` at start). Offset is stored in sqlite *before* dispatch; restarts resume without replaying, updates older than `TELEGRAM_MAX_UPDATE_AGE` (600 s) are dropped, edits are ignored. A 409 (second poller / leftover webhook) backs off 5-60 s.
- **Per-session view:** a private *forum supergroup* (`TELEGRAM_FORUM_CHAT_ID`), one topic per session (topic title follows the session title, archiving closes it, a deleted topic is recreated, a closed one reopened). Messages in *General* spawn a new topic. Without a forum chat: DM mode - `/new` starts a numbered slot, replying to a bot message routes to that session.
- **Rendering:** markdown -> Telegram HTML (tables as `<pre>`), 4000 UTF-16 budget with fence-aware splitting, plain-text fallback on parse errors. `actions`/`ask` fences become inline buttons, `ideas` a numbered list. Status message has a Stop button (`st:`); other buttons use short sqlite tokens (`ab:`) because `callback_data` is limited to 64 bytes.
- **Media:** photos/documents (<=20 MB) are saved under `~/.dop-agent/telegram_files/` and passed as `[Attachment: path]`; voice/audio go through the existing transcription. Outbound: `sendPhoto` with `sendDocument` fallback, albums for 2-10 files (<=50 MB each).
- **Commands:** `/start`, `/help`, `/new [title]`, `/stop`.
- **Config** (`.env.example`): `TELEGRAM_BOT_TOKEN` or mode-600 `TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_ALLOWED_USER_IDS` (authoritative: ids removed from the list are revoked at startup), `TELEGRAM_FORUM_CHAT_ID`, `TELEGRAM_MAX_UPDATE_AGE`, `TELEGRAM_POLL_TIMEOUT`, `NEW_SESSION_PLATFORM=telegram` (new Web UI sessions get a topic). Disable: `adapters.telegram.enabled: false` + restart. httpx loggers are forced to WARNING so the token-bearing URL is never logged.
- **Files:** `telegram_api.py` (transport seam, rate limiter, typed API), `telegram_format.py`, `telegram_adapter.py`, `telegram_poller.py`, `telegram_store.py`, `telegram_config.py`, `telegram_boot.py` (wiring from `dop_agent.main`); tests in `tests/telegram/` use `tests/fakes/fake_telegram.py`, a strict in-memory Bot API (HTML validity, 4096 limit, 64-byte callback data, rights, TOPIC_CLOSED, 409...). The real API was never contacted during development.

### Setup

1. In Telegram talk to **@BotFather**: `/newbot`, copy the token.
2. Find your numeric user id (e.g. @userinfobot).
3. Create a **private group**, open *Group settings -> Topics -> enable* (turns it into a forum supergroup), add the bot as **admin** with *Manage Topics* (+ *Delete messages*). Get the chat id (starts with `-100`; e.g. forward a message to @RawDataBot or read it from the web-client URL).
4. `.env`: `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_ALLOWED_USER_IDS=<your id>`, `TELEGRAM_FORUM_CHAT_ID=-100...`, optionally `NEW_SESSION_PLATFORM=telegram`.
Instead of `.env`, steps 4 can be done in the Web UI: *Settings → Integrations → Chat adapters → Telegram → Edit* (tokens are stored as refs/secret files, **Test connection** calls `getMe`; the same card exists for Slack, and the enable switch needs a restart to apply).
5. `dop-agent/bot_ctl.sh restart`; the log shows `Telegram adapter registered`. The startup self-check logs actionable hints (not a forum, missing rights).

### Manual test checklist (needs real Telegram)

- [ ] `/start` in the bot DM and in the group answers with the help text; a stranger's message gets no reply at all.
- [ ] Message in *General* -> a new topic appears, the text is copied, a "Continued in" link replies; the answer streams in the topic with a Stop button; Stop interrupts.
- [ ] Reply in the topic continues the same session; markdown, tables, long answers (>4000 chars, code fences) render without raw tags.
- [ ] `actions`/`ask` buttons: tap once -> sent once, keyboard disappears; a button tapped after a restart shows "expired" instead of hanging.
- [ ] Rename the session in the Web UI -> topic title follows; archive -> topic closed; unarchive -> reopened.
- [ ] Delete a topic by hand, then send from the Web UI -> a new topic is created.
- [ ] Send a photo, a PDF, a >20 MB file (warning), a voice note (transcribed).
- [ ] Image/file produced by the model arrives as photo/document/album.
- [ ] A watch fires into a Telegram-bound session -> message lands in its topic.
- [ ] Restart during a running turn -> the turn is recovered into the same topic; no old messages are replayed (stale backlog dropped).
- [ ] Start a second poller with the same token -> 409 backoff in the log, no crash; stop it -> recovers.
- [ ] `adapters.telegram.enabled: false` + restart -> Telegram silent, Slack/Web unaffected.
- [ ] Group flood (>18 msgs/min) shows no 429 storm; token does not appear in `bot.log`.

Known limitations: edits to sent messages by the user are ignored; Slack-style `bt:` generic buttons are unsupported; Web messages are mirrored into a topic only for sessions bound to Telegram (or created with `NEW_SESSION_PLATFORM=telegram`).
