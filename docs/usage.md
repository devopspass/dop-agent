# Usage

## Usage

### Startup notification
On every start (or restart, e.g. via `launchctl kickstart`), the bot DMs `ALLOWED_USER_ID` a `✅ Bot started and ready.` message once its Slack connection is up — a quick way to confirm a restart actually landed.

### DM the bot
Open a DM with `@vscode-copilot` in Slack and type anything. **Each new top-level message starts a brand-new thread with its own fresh Copilot session** — the bot's first reply in that thread is `🧵 New session: <uuid>`. Reply inside that thread (text, voice message, or `/md`) to continue the same session; send a new top-level message to start an independent one.

### @mention in a channel
Invite the bot to a channel and `@vscode-copilot <your prompt>`. Same per-thread session behavior as DMs — a fresh @mention (not inside an existing thread) starts a new session; replying inside the resulting thread while re-@mentioning the bot continues it. Continuing via a plain reply with no re-mention requires the `message.channels` event subscription (see [Slack App Setup](#slack-app-setup)) — not needed for DMs.

### Built-in commands

All text is sent to Copilot as a normal prompt except the `md` shortcut below.
Watch management is via the `dop-agent` MCP server's `watch_*` tools (see
[Watchers](#watchers)).

| Command | Action |
|---------|--------|
| `md <path>` (alias `render <path>`) | Render a Markdown file to a PNG image and send it here — only triggers when `<path>` resolves to a real file; otherwise the message passes through to Copilot as normal text (see [Markdown File Rendering](#markdown-file-rendering)) |


### MCP tools

The bot runs its own in-process MCP server (`web_server.py`, mounted at
`/mcp`, gated by a static bearer token — see [Security](#security)),
automatically wired into every bot-spawned Copilot CLI session
(`bot_config.SESSION_MCP_SERVERS`) so any Slack/Web UI turn can call these
tools directly, without needing them in `~/.copilot/mcp-config.json`.

`dop_agent.py`'s `main()` deliberately starts this server and blocks on it
actually listening (`web_server.run_in_background(ready_event)`) *before*
resuming/creating any Copilot session — including the interrupted-turn and
`self_restart`-watch auto-resumes that fire right at boot. A session
created/resumed before the socket is bound gets a connection-refused
reaching `/mcp`, and the Copilot CLI SDK marks that MCP server unreachable
for the rest of that session's lifetime (no retry once the port comes up)
— symptom: these `watch_*`/`session_*` tools silently missing from a
thread's toolset right after a bot restart. If you ever see that again,
check this ordering hasn't regressed before assuming it's a config issue.

| Tool | Purpose |
|---|---|
| `watch_add` / `watch_list` / `watch_get` / `watch_pause` / `watch_resume` / `watch_remove` / `watch_pause_session` | Manage watches — see [Watchers](#watchers) below. `watch_list` is compact by default (`full=True`, `fields=`, `limit=`) and flags watches whose key/meta no longer validates with `broken_error`; `watch_pause`/`watch_resume`/`watch_remove` take one id or a list and skip `cron:*`/#251-255 unless `force`; tools take `session_id` (= the injected `conversation_id`) |
| `session_search` | Full-text search across past Copilot session history (both `~/.copilot/session-store.db` and this bot's own `bot_store.db`), annotated with whether each hit is a bot-related (Slack/Web) session |
| `session_get` | Compact by default (last 3 turns cut to 1500 chars, `truncated` hint incl. raw log path `~/.copilot/session-state/<id>/events.jsonl`; `full=True`/`sections=`/`turns=`/`max_chars=`). Full detail (turns/checkpoints, touched files + extracted ticket/MR/URL refs, rendered bot messages, Slack thread link) for one session found via `session_search` |
| `session_list` | List conversations (title, Slack thread, `copilot_session_id`, pinned/archived) without a search keyword |
| `session_create` | Start a brand-new session (Web UI conversation, auto-mirrored to Slack if a DM is already known) seeded with a mandatory initial `prompt`, and kick off its first turn in the background. Optional `parent_session_id` leaves a `#session:<id>` backlink to the calling session (the same mechanism as the Web UI's "↪ Continue in new session" button — both go through `web_server._new_conversation_id()`); raises if it doesn't refer to a real session. Optional `mirror` (default `True`) controls whether the new session fans out to every already-plugged-in destination or stays Web-UI-only. Optional `title` sets a custom session title immediately, instead of leaving it to be derived from the prompt text. Optional `pinned` (default `False`) pins the new session to the top of the session list right away. Returns `{conversation_id, web_url}` immediately — the turn itself runs asynchronously, so poll `session_get`/`session_search` for its result. |
| `session_send` | Inject a new message into an **already-existing** session — equivalent to a human typing it into the Web UI compose box or a Slack thread, reusing the exact same `send_message()` path `session_create`/the Web UI/Slack all go through (so it mirrors to Slack, records to `bot_store.db`, and kicks off a background turn identically). Requires `conversation_id` (raises `ValueError` if it doesn't refer to a real session) and `text` (raises if empty/whitespace-only). If the target is **busy** (an in-flight turn, `CopilotConversation.is_busy()` true): default `interrupt=True` aborts that turn (same semantics as any real follow-up message arriving mid-turn) and automatically appends a note stating the new message supersedes the in-progress task (quoting it, via `watchers._last_turn_snippets()`) and should be addressed now, with the old task only resumed afterward if still relevant, so the interrupted work isn't silently dropped without a trace in the conversation history; pass `interrupt=False` to instead raise immediately with a self-healing hint ("target busy — retry with `interrupt=True` or wait") and send nothing. Optional `mirror` (default `True`, same meaning as `session_create`'s). Returns `{conversation_id, web_url}` immediately — like `session_create`, the turn itself runs asynchronously; poll `session_get`/`session_search` for its result. **Caveats**: a background-turn failure after this call returns is only visible via the logs/`session_get`, not surfaced back to the caller; and there's no additional access-control check beyond "any MCP client holding a valid `conversation_id`" — this is an accepted tradeoff given the bot's existing single-user/`ALLOWED_USER_ID` trust model. |
| `session_pin` | Pin or unpin an existing session (`session_id`, `pinned` default `True`) — same effect as the Web UI's pin icon. Raises if `session_id` doesn't refer to a real session. Returns `{conversation_id, pinned}`. |

`session_create` and `session_send` are the two tools whose logic lives
directly in `web_server.py` rather than `mcp_tools.py` — both need to
actually spawn/resume a live turn (reusing `send_message()`'s existing
conversation-creation/Slack-mirroring/background-dispatch logic), which is
exactly the kind of side effect `mcp_tools.py` is deliberately kept free of
(see its module docstring) so its
`watch_*`/`session_search`/`session_get`/`session_list` functions stay
trivially unit-testable against a throwaway sqlite file.


### Live progress status

While Copilot is working:

- The clock/hourglass (⏳) reaction is added both to your message **and** to the
  thread's root message (if this is a reply further down an existing thread), so
  it's visible from the top of the thread that something is in progress — not just
  on the specific message being answered. Both reactions are removed once the turn
  finishes.
- The "🤔 _Working..._" placeholder message is updated in place (throttled to ~1/sec)
  with a **running list** of what it's doing — not just the latest action — so you
  can follow the whole sequence of steps for that turn, up to the last 12 entries
  (older ones scroll off to keep the message short). Once the model is known for
  this conversation, it's shown in parentheses, e.g. `🤔 _Working... (claude-sonnet-5)_`.
- Right under the header, a small `⏱ <N>s elapsed` marker tracks how long the turn
  has been running. If no new step has happened in 15+ seconds it also shows
  `idle <N>s`, so you can tell a slow-but-alive turn apart from one that's gone
  stale/stuck — a background heartbeat keeps this counter advancing even between
  tool calls.
- **If the agent is using todos for this turn**, a checklist is shown **first**,
  before the "Working..." header and step history — read directly from the
  session's own local `todos` table (`~/.copilot/session-state/<session_id>/session.db`,
  same schema this tool itself uses) since there's no SDK/RPC accessor for it yet.
  Re-read on every refresh so it reflects todos being added/completed live. Omitted
  entirely if the agent hasn't created any todos this turn.

```
📋 To-dos:
✅ Push fix to MR !1234
🔄 Trigger pipeline #98765
⬜ Report back with result

🤔 _Working... (claude-sonnet-5)_
⏱ 7s elapsed
🔧 *Bash*: List tmp dir (`ls -la /tmp`)
🎯 Check the config file
🔧 *View*: /path/to/config.yaml
💭 Thinking...
```

Line formats per event type:

- `🔧 *Bash*: List tmp dir (\`ls -la /tmp\`)` — shell tool: description + command.
- `🔧 *View*: /path/to/file.py (lines 1-30)` — file reads: path (+ line range if given).
- `🔧 *Grep*: \`TODO\` in src/` — searches: pattern (+ scope).
- `🔧 *Web Fetch*: https://example.com` — fetches: URL.
- `⚙️ <progress message>` — tool-reported progress updates.
- `🎯 <intent>` — the agent's stated plan/intent for the current step.
- `💭 Thinking...` — between tool calls, while the model is reasoning.

Falls back to a plain `🔧 \`Tool Name\`` line if a tool's arguments don't match any known
shape. Consecutive duplicate lines are collapsed (e.g. repeated `💭 Thinking...` while
waiting on a long tool call only shows once).

- A **"⏹️ Stop"** button is attached to the live "Working..." message so you can abort
  a turn early without having to send a new message to interrupt it. Clicking it aborts
  the in-progress Copilot turn and replaces the status message with
  `⏹️ _Interrupted — continuing with your new message..._`-style text (same path as
  sending a new message mid-turn). The button disappears once the turn's real response
  replaces the status message.
- **Crash/restart cleanup & auto-resume**: if the bot process dies (crash, manual kill,
  deploy, `launchctl kickstart`) while a turn is still in progress, its "Working..."
  placeholder would otherwise be stuck forever with a dead Stop button. On the next
  startup, the bot sweeps any such leftover messages (tracked in `active_status.json`,
  gitignored — written the instant a turn starts, cleared the instant it finishes, so it
  always reflects exactly which threads were genuinely running at the moment of the
  restart) and, for each one: replaces the placeholder with
  `⏹️ _Interrupted — bot restarted before this finished. Resuming..._`, then **automatically
  resumes that thread** — hands it back to the agent via
  `watchers.build_resume_instruction()`, which quotes that thread's exact
  last user request and (possibly cut-off) last assistant message, followed
  by `watchers.RESUME_STATUS_INSTRUCTION` (shared with the `self_restart`
  watch flow below) telling the agent that a stated intent/action isn't a
  finished result and must actually be continued, not just restated. If
  work is still pending, the agent continues it automatically; if
  everything is already complete, it just reports the status — no silent
  no-op "resuming..." ack, and no need for a human to notice and
  re-prompt "did you finish that?". Each interrupted thread resumes as its own parallel
  background task, so multiple in-flight threads at restart time don't block each other
  or delay the bot accepting new messages.
- **Crash/restart during voice transcription is auto-retried**: a `🎧 Transcribing...`
  placeholder is tracked in `pending_transcriptions.json` (gitignored — written when
  transcription starts, cleared when it finishes). On startup, any stale entry is updated to
  `🔁 _Bot restarted mid-transcription — retrying..._`, the original Slack audio file is
  re-downloaded and re-transcribed from scratch, then continues through the normal
  buffering/auto-send flow.
- **Turn timeout (`SESSION_TIMEOUT`) preserves history**: if a single turn runs longer
  than `SESSION_TIMEOUT` (e.g. a tool call hangs), the bot aborts the stuck turn and drops its
  local handle, but leaves the saved `session_id` in place. The next message in that
  thread resumes the same Copilot session (full history intact) instead of silently
  starting a brand-new one. Only if that resume attempt itself fails does the bot fall
  back to a fresh session — the existing, separate fallback already handles that case.
