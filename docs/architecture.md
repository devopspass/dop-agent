# Architecture details

## Architecture

```
Slack (Socket Mode)      Web UI / API (FastAPI)
        ↕                        ↕
      adapters.py + handlers.py / web_server.py
                    ↕
      bot_store.db (conversations + bindings + transcript)
                    ↕
      CopilotConversation / GitHub Copilot Python SDK
                    ↕
               Copilot CLI binary
                    ↕
             GitHub Copilot API
```

- **Transport**: Slack [Socket Mode](https://api.slack.com/apis/connections/socket) — outbound WebSocket only, works behind corporate firewalls, no public URL needed.
- **Copilot SDK**: [`github-copilot-sdk`](https://github.com/github/copilot-sdk/tree/main/python) — official Python SDK, manages a persistent `CopilotSession` per Slack **thread**.
- **Agent mode**: `autopilot` with `PermissionHandler.approve_all` — all file/shell tool calls are auto-approved (equivalent to `copilot --yolo`).
- **Sessions**: one `CopilotSession` per bot-owned **conversation UUID**. That UUID can be bound to a Slack thread, to the Web UI, or to both at once via `conversation_bindings` in `bot_store.db`. Two independent top-level Slack messages get two independent sessions; the same conversation can be opened from the Web UI without creating a second Copilot session.
- **Session announcement**: the very first reply in a new thread is `🧵 New session: <uuid>` (posted before the "Working..." status/response), so you always know which Copilot session a given thread maps to — handy for `copilot session logs <uuid>`.
- **Session persistence**: `bot_store.db` stores the conversation UUID, its `copilot_session_id`, and one-or-more transport bindings in `conversation_bindings`. On bot restart (crash, `launchctl` reload, machine reboot), the bot resumes the same Copilot session from that UUID; if resume fails (expired/deleted session), it transparently creates a fresh one and updates that row.

### Multi-channel conversation model

- `conversations` = the logical session/transcript owner (`id`, `copilot_session_id`, pins, title/note metadata).
- `conversation_bindings` = how that logical conversation is reached from each transport (`slack` thread key like `C123:1717.42`, `web` binding keyed by the conversation UUID itself).
- `allowed_identities` = durable per-platform allow-list (`slack` user ids plus the Web UI auth surface).
- **Broadcasting/loop prevention**: Web-origin messages are recorded once into the shared transcript, then mirrored out to other bound adapters (e.g. Slack) via `ConversationBroadcaster`, tagged with their origin binding so the broadcaster never re-delivers a message back to the same adapter/binding that created it. Slack-origin turns already hit the shared transcript through `SlackAdapter`'s persistence path, so the Web UI sees them immediately without a second synthetic delivery.

---

## Files

| File | Purpose |
|------|---------|
| `dop_agent.py` | Entry point — Slack Bolt app instance, wiring (`handlers.register(app)`, `watchers.configure(...)`), crash-recovery resume logic (`_resume_interrupted_turns`, `_resume_interrupted_transcriptions`), and `main()` |
| `handlers.py` | Slack event/action handlers (`handle_dm`, `handle_mention`, transcript buttons), the core turn-processing engine (`_handle`), and voice-transcription glue (`maybe_transcribe_audio`) — registered onto the live `AsyncApp` via `handlers.register(app)` |
| `model_routing.py` | Per-session model routing: `task_kind` -> model map, allow-list (SDK `list_models`, cached 1h), prod guard, AIU rates/estimator, release-loop brief check; see "Model routing" |
| `adapters.py` | Channel-adapter abstraction (`Adapter` protocol, `SlackAdapter`, `WebAdapter`) plus the `ConversationBroadcaster` fan-out helper — `watchers.py`, the Web UI's `send_message()`, and every Slack-client call in `handlers.py` (sends, reactions, uploads, deletes, permalinks) route through it |
| `telegram_*.py` | Telegram adapter (opt-in): `telegram_api.py` transport seam/typed API, `telegram_format.py`, `telegram_adapter.py`, `telegram_poller.py` (long polling), `telegram_store.py`, `telegram_config.py`, `telegram_boot.py` - see [Telegram](#telegram) |
| `watchers.py` | Background watch polling (`self_restart`/`time`/`session_turn`/`script` plus integration-provided types) and the `WATCHERS.md` snapshot writer — late-bound to the live app/`_handle` via `watchers.configure(app, handle_fn)`. Token-savings helpers: `watch_payload.py` (rich fire payload), `watch_scripts.py` + `scripts/watch/` (script-only time watches), `watch_suppress.py` (fire classifier/allow-list) |
| `bot_config.py` | Env-var loading, logging setup, MCP server config, `message_store` DB init |
| `persistence.py` | Session-id persistence + user-visible message mirror (`bot_store.db`), crash-safe "active status"/"pending transcription" sweep files |
| `copilot_session.py` | `CopilotConversation` class, the per-thread conversation registry, thread/event-dedup helpers |
| `crash_recovery.py` | Reads back a lost final answer from the Copilot runtime's own durable event log after an unclean restart |
| `transcription.py` | Whisper-based voice/audio transcription (path-based, segments long audio) |
| `audio_store.py` | Durable recordings in `~/.dop-agent/audio/` + sidecar job state (resume/retention) |
| `render_tables.py` / `render_markdown.py` | Markdown table → PNG and full Markdown-doc → PNG rendering |
| `slack_formatting.py` | Markdown→Slack `mrkdwn` conversion and long-message chunking |
| `message_kinds.py` | Pure read-time classifier of transcript turns (watch registered / fired / reply, ACK vs report, errors) used by the Web UI's Smart view |
| `message_store.py` | Bot-owned SQLite data layer for conversations/messages (`bot_store.db`) |
| `usage_index.py` + `webui/usage.js/.css` | Token/AIU usage widget: incremental index of `~/.copilot/session-state/*/events.jsonl` into `usage_index.db` (rebuildable cache), `GET /api/usage`, header chips (today/7d/30d AIU, quota), thread-header AIU/🧠 context/♨ cache chips, sidebar AIU suffix. `USAGE_WIDGET=off` disables it. Quota comes from GitHub `copilot_internal/user` (real reset date; CLI `account.getQuota` as fallback), refreshed lazily + by a background task every `QUOTA_TTL_SECONDS` (default 3600, min 60), keeps the last good value on failure, `?refresh=1` forces a fetch (≥30 s apart). Quota unit (AIU vs premium requests) is unverified. |
| `bot_store.db` | Runtime SQLite store for logical conversations, per-platform bindings, allow-listed identities, pins, and mirrored transcript/messages (not committed to git) |
| `migrate_to_message_store.py` | Imports `sessions.json` / `pinned_sessions.json` plus `~/.copilot/session-store.db` turns into `bot_store.db` |
| `session_titles.json` | Custom Web UI title overrides (not committed to git) |
| `.env` | Secrets (not committed to git) |
| `.env.example` | Template for `.env` |
| `com.dop-agent.plist` | Template launchd plist (no real token) |
| `~/Library/LaunchAgents/com.dop-agent.plist` | Installed plist with real token (not in git) |
| `groups.py` | Session-group helpers: status rollup, group overview aggregate, new-session group suggestion |
| `group_backfill.py` | One-off dry-run/`--apply` script that proposes groups for pre-existing sessions |
| `web_server.py` | Local browser Web UI (FastAPI) — see [Web UI](#web-ui) below |
| `skills/` | Skills bundled with dop-agent (`actions-format`, `ask-format`, `chart-format`, `ideas-format`, `four-phase-plan`); `bot_config.SKILL_DIRECTORIES` adds this dir to the workdir's `.github/skills` for every bot session, regardless of workdir |
| `webui/` | Static frontend for the Web UI (`login.html`, `index.html`, `app.js`, `style.css`, `ask.js`/`ask.css` = clickable Q/D chips + answer popover for ```ask fenced blocks (skill `ask-format`), `actions.js`/`actions.css` = quick-reply chips for ```actions fenced blocks (`- [*] label [| text]`; click inserts the text into the compose draft without sending and without duplicating; `window.ActionsWidget` exposes `parse`, `render(container, actions, {conv, onClick})`, `apply`, `insertIntoDraft` and a global `onClick` override so the inbox can send directly; skill `actions-format`; Slack shows the plain code block), `ideas.js`/`ideas.css` = interactive ok/nok/skip widget for ```ideas fenced blocks, see skill `ideas-format`, `inbox.js` = **Messages inbox** (sidebar mode Sessions | Inbox: sessions whose last assistant message has unanswered ```ask/```ideas blocks, shown as slides with the real widgets mounted inline; below them a muted **Unread · N** section lists sessions with no pending asks that are unread (computed client-side from the sessions list, shown under the All filter only; click/Enter opens the session; the header badge counts ask sessions only); `GET /api/inbox`, `PUT/DELETE /api/inbox/{conv}/dismiss?message_id=`; answers/drafts are per-browser localStorage; "Send to session" posts only the answer blocks via `/api/send`, which interrupts a running turn — the UI asks first), `mermaid.js`/`mermaid.css` = renders ```mermaid fenced blocks as SVG diagrams (lazy-loaded from jsdelivr, Diagram/Source toggle; Web UI only — Slack shows the plain code block)) |

**Adapter extension guide.** Adding a new chat platform (Telegram is the worked example: see [Telegram](#telegram) and `telegram_adapter.py`): implement the `Adapter` protocol in `adapters.py` (`platform`, `capabilities`, `send/update/delete/...`, `message_ref`/`external_ref`, `thread_key_from_binding`/`external_key`, neutral `Button` rendering), register it in `adapters.REGISTRY`, and feed inbound messages as `IncomingMessage` into `handlers.handle_incoming` (per-platform auth, dedup, status editing via `ThrottledEditor`, crash recovery via `active_status` entries carrying `platform`/`thread_key`). `tests/adapter_contract/` (`FakeAdapter` + `contract_harness.py`) is the spec. `conversation_bindings` is the only conversation-to-platform mapping. The Web UI session payload exposes `platform_links`. Known gaps: inbound attachments/voice are Slack- and Telegram-only; `watches.channel/thread_ts` and `copilot_session._conversation_id_for` are still Slack-shaped.

---

**Recent filter:** the sidebar's 🕒 Recent chip lists sessions with activity in the last N hours (Settings → Sessions list, default 8, 1–720, stored in the browser; blank/0/invalid input reverts). "Activity" is the session's `updated_at`, so watch-fired and system messages count. Archived sessions are excluded; the session you have open stays listed until you switch away. The list re-checks once a minute so sessions age out, the chip tooltip shows the window, and Recent combines with the group filter.
