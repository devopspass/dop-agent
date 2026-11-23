# Security implementation notes

## Security

- Only the user whose Slack ID matches `ALLOWED_USER_ID` in `.env` can interact with the bot.
- Anyone else gets `⛔ Not authorized. Your user ID: ...` and Copilot is never invoked.
- **All Slack action buttons are authorization-gated**, not just the `message`/`app_mention`
  events: `stop_working_turn`, `send_transcript_to_copilot`, `auto_send_transcript`,
  and `preview_transcript` all call `is_authorized()` before acting, so a
  non-`ALLOWED_USER_ID` Slack member who can see a message (e.g. in a shared
  channel) can't abort your turn or forward a transcript into the session.
- **`is_authorized()` fails closed**: a blank/misconfigured `ALLOWED_USER_ID`
  denies everyone (never grants open access to autopilot/`--yolo` code execution).
- The `.env` file is in `.gitignore` — never committed.
- The installed launchd plist with the real token lives in `~/Library/LaunchAgents/` — also not committed.
- **Duplicate event protection**: Slack can redeliver the same `message`/`app_mention` event (e.g. after a brief WebSocket reconnect, such as during a VPN reconnect). The bot dedups by `(channel, ts)` in a bounded in-memory cache so retried deliveries are silently ignored instead of being processed twice.
- **Web UI login is rate-limited per IP** (5 wrong passwords / 15 min lockout)
  and `WEB_UI_SECRET_KEY` is generated once and persisted to `.env` instead of
  regenerating (and invalidating every open session) on every restart — see
  the [Web UI](#web-ui) section above.
