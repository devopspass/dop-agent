# Slack setup

## Slack App Setup

The Slack App is `vscodecopilot` on workspace `redguide-ci.slack.com` (App ID: `A0BM0NE0LUC`).

### Required OAuth Scopes (Bot Token)
- `chat:write`
- `im:history`
- `im:read`
- `app_mentions:read`
- `channels:history`
- `reactions:write`
- `files:write` — required to upload table images (see [Table rendering](#table-rendering) below)
- `files:read` — required to download voice/audio messages for transcription (see [Voice Transcription](#voice-transcription) below)

### Required Socket Mode Scope (App-Level Token)
- `connections:write`

### Required Event Subscriptions
- `message.im` — DM messages (top-level **and thread replies** — Slack delivers both through this same subscription, so per-thread sessions in DMs work with no scope/event changes).
- `app_mention` — @mentions in channels (only fires when the bot is actually @mentioned).
- `message.channels` — **needed for full per-thread behavior in channels.** Without it, a reply inside a thread that doesn't re-@mention the bot never reaches the bot at all, so that thread's session can't be continued by a plain reply — only fresh `@mention`s create/re-trigger a turn. Add this subscription (and reinstall the app) if you want "reply in thread, no re-mention needed" to work in channels the same way it already works in DMs. Needs the `channels:history` scope, already granted (see below). For private channels/group DMs, also add `message.groups` / `message.mpim` with `groups:history` / `mpim:history` scopes.


### Interactivity & Shortcuts
- Toggle **Interactivity** to **On** (Interactivity & Shortcuts page). No Request URL is needed — Socket Mode delivers `block_actions` payloads (e.g. the "Send to agent" button after voice transcription) over the same websocket.

### App Home
- Enable **"Allow users to send messages from messages tab"** in App Home settings.

> After any scope or event change: reinstall the app via **OAuth & Permissions → Reinstall to Workspace**.

---

## Tokens

| Token | Where to get | Variable |
|-------|-------------|----------|
| Bot Token (`xoxb-...`) | OAuth & Permissions → Bot User OAuth Token | `SLACK_BOT_TOKEN` |
| App-Level Token (`xapp-...`) | Basic Information → App-Level Tokens (scope: `connections:write`) | `SLACK_APP_TOKEN` |
| GitHub PAT | GitHub → Settings → Developer Settings → PATs (needs Copilot access) | `COPILOT_GITHUB_TOKEN` |
