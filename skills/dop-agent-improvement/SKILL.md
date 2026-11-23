---
name: dop-agent-improvement
description: "Use when: making ANY change to the dop-agent/ codebase (dop_agent.py and its modules), diagnosing bot bugs (session/thread mix-ups, restarts, timeouts, render/upload failures), or restarting/reinstalling the launchd daemon. Load BEFORE editing any dop-agent/ file; also read that module's own docstring."
argument-hint: "[what you're changing or debugging in dop-agent]"
---

# DOP Agent — Improvement & Change-Safety Guide

`dop-agent/dop_agent.py` (repo: `<repo>/dop-agent/`)
is a **personal, always-on bridge** between the owner's own chat account and
GitHub Copilot CLI running in `autopilot` (yolo/all-tools-approved) mode. It is
a `launchd` daemon, not a throwaway script — treat every change like a
production service change: read this skill, check `git status`/`git diff`
first, make a surgical fix, **commit it**, then restart.

## 0. Module map — as of the handlers.py extraction, `dop_agent.py` is now
just wiring/entry-point. The bot is split into single-purpose modules, each
carrying its **own docstring with module-specific gotchas** — read the
target file's docstring before editing it; this skill only covers
cross-cutting/whole-bot concerns (runtime, lifecycle, process discipline),
not per-module detail.

| Editing... | Module | What lives there |
|---|---|---|
| Slack event/action handlers, the core turn engine | `handlers.py` | `handle_dm`, `handle_mention`, transcript-button actions, `_handle()`, voice-transcription glue |
| Channel-adapter abstraction (behavior-preserving refactor, complete) | `adapters.py` | `Adapter` protocol, `SlackAdapter`, `WebAdapter` — `watchers.py`, `web_server.py`'s `send_message()`, and every Slack-client call in `handlers.py` (sends, `say()`-shorthand, reactions, uploads, deletes, permalinks) route through it; `handlers.py` no longer calls `client.*`/`say()` directly (see the module's own docstring for design notes) |
| Background watch polling | `watchers.py` | MR/pipeline/Teams/Jira/time/self-restart watch types, `WATCHERS.md` writer |
| Watch token savings | `watch_payload.py`, `watch_scripts.py` (+ `scripts/watch/`), `watch_suppress.py` | Rich fire payload (`WATCH_RICH_PAYLOAD`), script-only time watches (`meta.script`, allow-listed dir, no model turn), allow-list fire suppression + `watch_fires` log (`WATCH_SUPPRESS` off/dry/on, default dry). New scripts must be `chmod 755`, not group/world-writable, read-only; see README "Watcher token savings" |
| Env vars, logging, MCP config | `bot_config.py` | Startup-time config loading |
| Session-id persistence, message mirroring | `persistence.py` | `sessions.json`/`bot_store.db` save/load, crash-safe sweep files |
| Per-thread Copilot session lifecycle | `copilot_session.py` | `CopilotConversation`, conversation registry, event dedup |
| Crash/restart recovery | `crash_recovery.py` | Reading back a lost answer from the Copilot event log |
| Voice/audio transcription | `transcription.py` | Whisper-based transcription |
| Table/Markdown → image rendering | `render_tables.py` / `render_markdown.py` | WeasyPrint/PyMuPDF/Pillow pipeline |
| Slack `mrkdwn` formatting/chunking | `slack_formatting.py` | Markdown→Slack conversion, long-message splitting |
| Session groups (rollup/overview/suggest) | `groups.py` | Pure helpers over the session payload; group CRUD itself lives in `message_store.py`, UI in `webui/groups.js` |
| SQLite data layer | `message_store.py` | `bot_store.db` schema/queries (lowest-level, no project-local deps) |
| Web UI | `web_server.py` | FastAPI app, SSE updates, browser-facing routes |
| Entry point / wiring | `dop_agent.py` | `app` construction, `handlers.register(app)`, `watchers.configure(...)`, crash-recovery resume, `main()` |

**Adapter extension guide.** Adding a new chat platform (Telegram is the worked example, see the Telegram section below): implement the `Adapter` protocol in `adapters.py` (`platform`, `capabilities`, `send/update/delete/...`, `message_ref`/`external_ref`, `thread_key_from_binding`/`external_key`, neutral `Button` rendering), register it in `adapters.REGISTRY`, and feed inbound messages as `IncomingMessage` into `handlers.handle_incoming` (per-platform auth, dedup, status editing via `ThrottledEditor`, crash recovery via `active_status` entries carrying `platform`/`thread_key`). `tests/adapter_contract/` (`FakeAdapter` + `contract_harness.py`) is the spec. `conversation_bindings` is the only conversation-to-platform mapping (legacy `conversations.slack_key` is no longer created/read/written; old DBs keep it inert). The Web UI session payload exposes `platform_links`. Known gaps: inbound attachments/voice are Slack-only; `watches.channel/thread_ts` and `copilot_session._conversation_id_for` are still Slack-shaped.

See `dop-agent/README.md`'s "Files" table for the same map with slightly
more prose, kept in sync with this one.

---

## 1. Exact runtime requirements (do not deviate)

| Item | Required value | Why it matters |
|---|---|---|
| Python interpreter | **your Python 3.12+ interpreter** (e.g. Homebrew `python3.13`) | Hardcoded in the launchd plist `ProgramArguments`. The system `python3` (`/usr/bin/python3`, currently 3.9.6) does **not** have the bot's dependencies installed — running/testing with plain `python3` will fail with `ModuleNotFoundError` and give a false impression the code is broken. Always test with `<python> -u dop_agent.py` or `<python> -m py_compile dop_agent.py`. |
| Required packages (must be importable under python3.13) | `github-copilot-sdk` (module name `copilot`, **not** `github_copilot_sdk`), `slack-bolt`, `slack_sdk`, `python-dotenv` | Core — bot won't start at all without these. |
| Optional/rendering packages | `weasyprint`, `pymupdf` (module `fitz`), `pillow` (`PIL`), `markdown` | Used for table-image and `/md` Markdown-file rendering. As of the current uncommitted working copy these are now wrapped in `try/except ModuleNotFoundError` with a graceful fallback to `TABLE_RENDER_MODE=ascii` — but confirm that fallback logic is still in place before assuming these are hard requirements. |
| Optional/voice packages | `openai-whisper` (pure PyTorch, works on Apple Silicon or Intel) + system `ffmpeg` CLI | Voice/audio message transcription. Model weights cached under `~/.cache/whisper`, downloaded from OpenAI's Azure CDN (not HuggingFace — some corporate networks block HF by SNI). |
| System libraries (Homebrew) | `cairo pango gdk-pixbuf libffi ffmpeg` | WeasyPrint (table/markdown rendering) needs the first four; ffmpeg needed by Whisper. |
| Node.js + Copilot CLI | Whatever `copilot --version` resolves to on PATH | The Python SDK shells out to/embeds the Copilot CLI binary internally. |
| Working directory (`COPILOT_WORKDIR`) | **the bot workdir (`<repo>`)** — hardcoded expectation, must never change | Every Copilot session the bot creates/resumes uses this as `working_directory`. A real incident happened where the agent ended up executing in an unrelated `mr-render/` subfolder — if you see the bot operating outside this path, that's a bug (check the client is always being constructed with `working_directory=COPILOT_WORKDIR` in `_ensure_session()`, and that no session-resume path silently drops it). |
| MCP servers | Loaded from `~/.copilot/mcp-config.json` (`MCP_SERVERS = _load_mcp_servers()`), passed explicitly to both `create_session()` and `resume_session()` | Interactive `copilot` CLI picks up `~/.copilot/mcp-config.json` automatically; **programmatic SDK sessions do NOT** — they must be passed `mcp_servers=...` explicitly or GitLab/Jira/Confluence/Grafana tools silently won't be available in bot sessions even though they work fine in a normal terminal `copilot` session. This was a real gap that had to be fixed once already — don't remove it. |

### `.env` variables (see `.env.example` for the canonical template)

| Var | Current value semantics | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `xoxb-…` / `xapp-…` | Secrets — never commit, never print in full to logs. |
| `ALLOWED_USER_ID` | your chat user id | Hard authorization gate — every message handler must check `is_authorized()` before ever touching Copilot. |
| `COPILOT_GITHUB_TOKEN` | GitHub PAT with Copilot access | Passed to `CopilotClient(github_token=...)`. Also duplicated into the launchd plist's `EnvironmentVariables` (plist doesn't source `.env`). |
| `COPILOT_WORKDIR` | `<repo>` | See table above — must stay pinned. |
| `SESSION_TIMEOUT` | `1800` (seconds) | Do not lower this back down without a specific reason — long agent turns (pipeline polling, multi-file renders) routinely take 10+ minutes. |
| `TABLE_RENDER_MODE` | `image` (default) or `ascii` | Falls back to `ascii` automatically now if rendering deps are missing (see uncommitted fix). |
| `WHISPER_MODEL` | `large` (default) | Upgraded from `base`→`small`→`large` across sessions for accuracy; `mlx-whisper` is also installed but the code path in `dop_agent.py` uses `openai-whisper`, not `mlx-whisper` — don't assume MLX is being used just because it's `pip install`ed. |
| `STARTUP_NOTIFY_CHANNEL` | DM channel id for the "✅ Bot started" ping | Optional; falls back to last known DM channel in `sessions.json`. |
| `COPILOT_BIN` | present in `.env` but **not referenced anywhere in `dop_agent.py`** | Dead/leftover variable — a small symptom of the "chaotic changes" problem. Either wire it up (e.g. to let the SDK point at a non-default `copilot` binary) or remove it from `.env`/`.env.example` — don't leave unused config lying around. |

---

## 2. Auto-start / process lifecycle (launchd)

- **Daemon type**: macOS `launchd` **user agent**, plist at
  `~/Library/LaunchAgents/com.dop-agent.plist` (installed,
  real token — not in git) mirroring the template
  `dop-agent/com.dop-agent.plist` (in git, token redacted
  as `******`).
- **`ProgramArguments`**: `["<python>", ".../dop-agent/dop_agent.py"]` — no venv activation, so `python3.13`'s **global/Homebrew** site-packages must have every dependency installed directly (`pip install ... ` without `--user` inside a venv that launchd can't see).
- **`RunAtLoad` + `KeepAlive`**: true — starts on login, and launchd restarts it automatically on any crash/non-zero exit. `ThrottleInterval: 10` caps restart frequency to avoid a crash-loop hammering the Slack/Copilot APIs.
- **`EnvironmentVariables`** in the plist only sets `PATH`, `HOME`, and `COPILOT_GITHUB_TOKEN` — everything else the bot needs comes from `.env` via `python-dotenv` (`load_dotenv()`), loaded relative to the script's own directory. If you add a new required env var, it must go in `.env` (or `.env.example` as documented default), **not** assumed to be in the shell environment, since launchd processes don't inherit `~/.bash_profile`/`~/.zshrc`.
- **Logs**: `~/.copilot/dop-agent-error.log` (stdout+stderr, the real log — check here first) and `~/.copilot/dop-agent.log` (usually empty).
- **Manage — use `dop-agent/bot_ctl.sh` instead of raw `launchctl`**:
  ```bash
  dop-agent/bot_ctl.sh status    # running? pid? + last 10 log lines
  dop-agent/bot_ctl.sh restart   # unload+load, polls until pid reappears, tails startup banner
  dop-agent/bot_ctl.sh stop      # unload only
  dop-agent/bot_ctl.sh start     # load only, waits for pid
  dop-agent/bot_ctl.sh logs      # tail -f the error log
  ```
  `restart` actively polls for the new PID (up to 20s) and prints the
  startup banner, so you get a real pass/fail instead of assuming the
  unload/load pair worked. Raw `launchctl unload`/`load`/`kickstart -k
  gui/$(id -u)/com.dop-agent` still work if you need
  them directly, but prefer the script so restarts are verified, not
  assumed.
- **⚠️ Self-restart hazard**: if you are *editing the bot from inside a Slack
  thread being served by the very same bot process*, restarting it
  (`kickstart -k`) will kill your own current turn mid-response. Finish
  applying/verifying the fix, tell the user a restart is needed and why, and
  either let them trigger it or do it as the very last action of the turn.

---

## 3. Session ↔ Slack-thread architecture (the part that breaks most often)

- **One `CopilotSession` per Slack *thread*, not per channel/DM.** Key is
  `f"{channel}:{thread_ts}"` (`_conversation_id_for`). A brand-new top-level
  message (no `thread_ts` yet) uses its own `ts` as the thread root
  (`_thread_root`) — this becomes the permanent key for that thread from then
  on, including the `thread_ts` the bot passes back to Slack on its first
  reply (which is what actually turns the message into a visible thread).
- **Persistence**: `conversation_id -> Copilot session_id` saved to
  `sessions.json` (next to `dop_agent.py`, gitignored). On any restart, the
  bot calls `resume_session(saved_id, ...)` instead of creating fresh — thread
  history survives crashes/reloads. If resume fails (expired/deleted
  session), it transparently falls back to a new session and updates the map.
- **New-thread announcement**: the very first reply in a new thread is
  `🧵 New session: <uuid>` — a deliberate feature so `copilot session logs
  <uuid>` / `/status` can always be tied back to a specific thread. Preserve
  this when refactoring `_handle()`.
- **Slack self-context injection**: the first message of a brand-new session
  is prefixed with a `[Slack context: ...]` block (`_slack_thread_context` —
  workspace URL, channel/DM type, thread permalink, thread root ts) so the
  agent knows which Slack thread it's running in without extra tool calls.
  This was added specifically because "render and send to me" style commands
  need the agent to know its own channel/thread id to call
  `chat_postMessage`/file-upload against — don't strip this when touching
  `_handle()`.

### Bug already found & fixed once — do not reintroduce

**"Interrupt wipes the whole session" bug** (found and fixed in the session
right before this skill was written): a later, uncommitted change added a
`self._force_new_session = True` flag that was set whenever a new message
interrupted an in-progress turn (including just sending `stop` while busy).
On the *next* `send()`, that flag discarded the saved `session_id` in
`sessions.json` and created a brand-new Copilot session — silently wiping
all conversation history, even though the user only wanted to abort the
current turn, not reset the thread. **Symptom to watch for**: user says
"why did it forget everything after I said stop / sent a quick follow-up?"
**Fix already applied** (in the current uncommitted working copy): removed
`_force_new_session` entirely. Interrupting a turn now only calls
`session.abort()` + returns an `INTERRUPTED` sentinel — the same
session/history is reused for the next message. Genuine unrecoverable
failures (timeout, broken pipe) still explicitly clear the saved session id
themselves in their own `except` blocks — that part is correct and should
stay. **When touching interrupt/abort/retry logic, grep for
`_force_new_session` first — if it's back, that's the regression.**

### Other real incidents/behaviors worth knowing before you change things

- **Wrong working directory**: an agent turn was once observed operating
  inside an unrelated `mr-render/` folder instead of the jira repo root.
  Root cause was never fully pinned down in-session — if you see this again,
  check (a) `COPILOT_WORKDIR` is passed identically to both
  `create_session()`/`resume_session()`/`CopilotClient(...)`, and (b) no tool
  call or `cd` from a prior turn is bleeding into a "resumed" session's
  shell state.
- **`app_mention`-only channel threads**: replies inside a channel thread
  only reach the bot if the bot is **re-@mentioned** — Slack only fires
  `app_mention` for messages that mention the bot. True "reply in thread, no
  re-mention" behavior (like DMs already have) needs the `message.channels`
  event subscription (+ `channels:history` scope, already granted) added and
  the app reinstalled — tracked as a known gap, not yet enabled.
- **Broken pipe from the Copilot CLI subprocess**: handled with a
  reset-and-retry-once (`_is_broken_pipe_error`) rather than surfacing a raw
  error to Slack — keep this if refactoring `send()`.
- **Duplicate Slack event delivery**: Slack redelivers `message`/`app_mention`
  events after brief reconnects (e.g. VPN blips). Deduped via `(channel, ts)`
  in a bounded `OrderedDict` (`_seen_event_keys`, `_is_duplicate_event`) —
  don't remove without replacing with an equivalent dedup mechanism.

---

## 4. Process discipline for future changes (the actual "chaos" fix)

The recurring complaint ("dop-agent changes are chaotic") traces to
**process**, not any single bug: multiple independent Copilot CLI sessions
each made incremental edits to `dop_agent.py` directly on `main`, without
committing, across days — at the time this skill was written `git diff
dop-agent/dop_agent.py` showed **~250+ uncommitted lines** of accumulated
changes (optional-dependency guards, MCP server loading, per-thread session
keying, Slack-context injection, the interrupt-bug fix, audio-buffer
per-thread keying, etc.) sitting only in the working tree, several commits
behind `git log` history. Going forward:

1. **Check `git status`/`git diff -- dop-agent/` first**, every time,
   before assuming the file matches the last commit or the README.
2. **One logical change per commit**, committed promptly — don't let fixes
   accumulate uncommitted across sessions. Use a real commit message (see
   the git history for style: `feat: ...` / `fix: ...` prefixes are
   used here even though this isn't a ticket-tracked repo change).
3. **Update `README.md` in the same change** if behavior visible to the user
   changes (new command, new env var, new architecture detail) — the README
   is detailed and was kept in sync historically; don't let it drift.
4. **Compile-check before restarting the daemon**:
   `<python> -m py_compile dop-agent/dop_agent.py`
   — cheap, catches syntax errors before they crash the live `KeepAlive`
   process into a restart loop.
5. **Grep for dead/vestigial flags** (like the removed `_force_new_session`,
   or the currently-unused `COPILOT_BIN` env var) when doing related work —
   clean them up rather than leaving them as confusing latent state.
6. **Register a `self_restart` watch for the current session *before*
   restarting** — this is now a standing habit, not a one-off ask. Use the
   `dop-agent` MCP server's `watch_add` tool:
   ```
   watch_add(type="self_restart", session_id=<this session's conversation_id>,
              label="short description of what changed")
   ```
   Watches are keyed by `conversation_id` (the bot's own stable conversation
   UUID), not by Slack `channel`/`thread_ts` — this works identically for
   Slack-bound sessions and Web-UI-only sessions. Get `conversation_id` from
   the `[Session context: ...]` block injected at the top of every session
   (Slack-bound sessions also get workspace/channel/thread detail there; a
   Web-UI-only session just gets the `conversation_id` line), not from
   guessing. This makes the session auto-resume itself with a "🔁 Bot
   restarted successfully..." message the moment the new process comes back
   up — you never need to be asked "ok, do bot restart and check how it
   goes" again; just always do it as part of the restart step below. See
   the `watch_add` MCP tool / `skills/disabled/watch-manage/SKILL.md`
   for the full watcher system.
7. **Restart and verify** after every change: `dop-agent/bot_ctl.sh
   restart` — it unloads/loads, polls until the new pid appears (up to
   20s), and prints the startup banner (workdir, timeout, etc. — see
   `main()`'s log lines) automatically. Confirm no traceback in that
   output; if it reports the pid didn't come back, it already tailed
   the error log for you — don't `kickstart -k` blind and assume it worked.
8. **Run the full unit test suite after every change, before committing**:
   `<python> -m pytest dop-agent/tests/ -q`. Don't
   restart the daemon on a change whose tests you haven't (re-)run — a
   green `py_compile` only proves the syntax is valid, not that the
   behavior is correct (see the handlers.py extraction, where pyflakes and
   `py_compile` both passed while a real `NameError` bug only surfaced when
   a test actually exercised that code path).
9. **Adding new tests is always fine and encouraged** — do it freely
   whenever you touch a code path that isn't yet covered. **Changing the
   behavior of an *existing* test (not just adding new ones) must be
   discussed with the user first** — an existing test changing silently
   can mask a real regression instead of catching it; get explicit
   agreement before loosening/altering an assertion, not just before
   deleting the test outright.
10. **Every new feature or bugfix ships with a test whose name/docstring
    documents the feature/bug being covered, not just its mechanics.**
    This applies to both the pure-Python unit suite (`dop-agent/tests/`)
    and the Playwright UI suite (`dop-agent/ui_tests/`) — whichever is
    appropriate for the change. Concretely:
    - The test **function name** should read as a plain-English
      description of the *user-visible behavior* being verified (e.g.
      `test_scroll_position_preserved_during_passive_live_update`,
      `test_stop_button_lives_inside_the_live_status_bubble`), not a
      mechanical name like `test_case_3` or `test_render_messages_2`.
    - The test **module or function docstring** should state: what the
      bug/feature was, what the old broken behavior looked like (for
      bugfixes), and what the fix/feature now guarantees — see
      `ui_tests/test_scroll_position_preserved.py` or
      `ui_tests/test_session_switch_instant.py` for the expected level of
      detail. A future reader (including a future you, or a different
      Copilot session) should be able to understand *why* the test exists
      without needing to dig through chat history or old commits.
    - This is not optional busywork — these docstrings are the durable
      record of "why is this line of behavior here", since this repo
      doesn't have Jira tickets/PR descriptions tracking each change.

---

## 4a. Test description quality — required for every feature/bugfix test

When writing or reviewing tests for this bot, treat the test's name +
docstring as first-class documentation of the feature, not an
afterthought:

- **Bad**: `def test_button()`, or a docstring that just restates the
  mechanics ("clicks the button and checks display style").
- **Good**: name states the guarantee (`test_stop_button_shown_while_running_and_hidden_after`),
  docstring explains the "why" — prior bug symptom (if a bugfix) or the
  user-facing capability (if a new feature), and what regression the test
  actually catches.
- Before marking any P0-P4/backlog feature todo "done", confirm its test(s)
  meet this bar — not just that they pass.

---

## 4b. Web UI unfurl card layout — every `renderUnfurlCard()` type must match

`webui/app.js`'s `renderUnfurlCard(data)` renders a small floating card when
hovering a recognized link (Jira ticket, GitLab MR/pipeline/job, or the
bot's own `#open=`/`#msg=` session links). **Every card type must follow the
same two-row layout**, or it looks visibly out of place next to the others:

- **Row 1** (`.unfurl-row1`): icon + a **short** key/identifier (ticket key,
  `!<iid>`, `Pipeline #<id>`, `Job #<id>`, or similar — something you can
  read at a glance without it wrapping) + a status span (`.unfurl-status`,
  emoji + short label, optionally with extra `·`-separated bits like
  approval/pipeline state or "updated Xm ago").
- **Row 2** (`.unfurl-row2`): the actual title/summary text, which can be
  long and is allowed to wrap independently on its own line.

**Real incident**: the `own_session` card type (added for the "unfurl links
to the bot's own sessions" feature) initially skipped row 2 entirely and
crammed the full session title into row 1 next to the status — it worked,
but looked inconsistent next to the Jira/GitLab cards and the title had no
room to wrap. Fixed by giving it a short `Session <first-8-chars-of-id>`
row-1 key and moving the actual title to row 2, matching every other type.
When adding a new unfurl card type (or reviewing one), diff its `innerHTML`
template against the Jira/GitLab branches in `renderUnfurlCard()` and
confirm it has both `.unfurl-row1` and `.unfurl-row2` divs with content in
the right one — don't just eyeball that "there's some status and a title
somewhere in the card." A test asserting `card.locator('.unfurl-row2')`
exists and contains the title (while row1 does NOT) is the right regression
guard — see `ui_tests/test_own_session_link_unfurl.py`'s
`test_own_session_link_gets_an_unfurl_card_on_hover` for the pattern.

---

## 4c. MCP tool errors must be "self-healing" — include a fix hint, not just a failure

Every MCP tool exposed by this bot (`watch_*` in `mcp_tools.py`, and any
future tool) should fail in a way that teaches the calling agent what to do
next, not just report that something went wrong. This was decided during
the watch-existence-validation design discussion (see `watchers.py`'s
`_report_broken_watch()` / `_is_gitlab_not_found_error()` for the existing
precedent this generalizes from — e.g. the pipeline-404 message already
tells the caller to use `group/project@<ref>` instead of a bad `#<id>`).

- **Shape**: prefer an error string (or structured `{"error": ..., "hint":
  ...}` payload, whichever the tool already returns) that states (a) what
  failed and with which identifier, and (b) the concrete next step to fix
  it or verify the input — e.g. *"MR !106 not found in group/project
  (GitLab 404) — check the MR iid is correct; list open MRs with
  `list_merge_requests` if unsure."* rather than a bare *"MR not found"*.
- **Applies to new tools/paths as they're added** — this is a standing
  convention for this bot's MCP surface, not a one-off fix. When adding a
  new failure path to any `watch_*` tool (or any other MCP tool later added
  to `mcp_tools.py`), write the error message as if you're handing it
  straight back to another Copilot session that has no other context —
  because that's exactly what happens.
- Consider factoring a small shared helper (e.g. a `self_healing_error(msg,
  hint)` formatter) if/when three or more call sites end up hand-rolling
  the same "message + hint" string pattern, instead of duplicating the
  phrasing ad hoc.

---



## 5. Copilot Python SDK cheat-sheet (for quick reference while editing)

- **Package**: `pip install github-copilot-sdk` → import as `copilot` (not `github_copilot_sdk`).
- **Auth**: `COPILOT_GITHUB_TOKEN` env var → `CopilotClient(github_token=...)`.
- **Client init**: `CopilotClient(working_directory=..., github_token=...)` — param is `working_directory`, not `cwd`.
- **Create session**: `await client.create_session(session_id=..., on_permission_request=PermissionHandler.approve_all, mcp_servers=MCP_SERVERS)`.
- **Resume session**: `await client.resume_session(saved_id, on_permission_request=PermissionHandler.approve_all, working_directory=..., mcp_servers=MCP_SERVERS)` — must re-pass `working_directory`/`mcp_servers`, they are not implicitly remembered from the original session.
- **Send & wait**: `await session.send_and_wait("prompt", agent_mode="autopilot", timeout=SESSION_TIMEOUT)`.
- **Response**: `event.data.content` where `event.data` is `AssistantMessageData`.
- **Permission mode**: `autopilot` + `PermissionHandler.approve_all` = equivalent of `copilot --yolo` — every file/shell tool call is auto-approved, no per-tool confirmation. This is intentional (it's a personal single-user bot gated by `ALLOWED_USER_ID`) — don't "fix" this into a manual-approval flow without being asked.

---

## 6. Quick pre-flight checklist before shipping a change

- [ ] Ran with `<python>` (your Python 3.12+ interpreter), not system `python3`.
- [ ] `py_compile` passes.
- [ ] Full test suite run and green: `<python> -m pytest dop-agent/tests/ -q` (not skipped just because `py_compile`/pyflakes passed).
- [ ] If any *existing* test's behavior changed (not just new tests added), confirmed with the user first.
- [ ] `git diff -- dop-agent/` reviewed end-to-end, not just your own hunk — you may be sitting on someone else's uncommitted change too.
- [ ] `COPILOT_WORKDIR` / MCP servers / `SESSION_TIMEOUT` behavior untouched unless that's the point of the change.
- [ ] Per-thread session keying (`_conversation_id_for`, `sessions.json` persistence) still intact — no session-resetting side effects on interrupt/retry paths.
- [ ] README.md updated if user-visible behavior changed.
- [ ] New feature/bugfix has a test whose name + docstring documents the
      behavior/bug being covered (see §4 item 10 / §4a) — not just a
      mechanical pass/fail check.
- [ ] Committed with a clear message.
- [ ] `self_restart` watch registered for the current thread (see §4.6) before restarting.
- [ ] Daemon restarted via `dop-agent/bot_ctl.sh restart` (confirmed pid + clean startup banner, not just assumed).

## Telegram adapter (`telegram_*.py`)
- Off unless `TELEGRAM_BOT_TOKEN` (+ non-empty `TELEGRAM_ALLOWED_USER_IDS`); disable with `adapters.telegram.enabled: false`. Wired by `telegram_boot.setup()` in `dop_agent.main` BEFORE `web_server.init`/crash recovery; poller starts after recovery; every failure is swallowed.
- Long polling only (never webhook). **No access to api.telegram.org from this host**: all code goes through the `TelegramTransport` seam; tests use `tests/fakes/fake_telegram.py` (strict) and `tests/fakes/telegram_rig.py`; never call the real API, never log the token (httpx loggers forced to WARNING, `redact()`).
- Key = `chat` (DM) / `chat:slot` (DM /new) / `-100chat:topic` (forum). Adapter's store is `tstore` (NOT `store`: `adapters._record` uses `adapter.store`). `ensure_topic` binds before first send; status edits are detected by the Stop button (`deliver_update` does not forward role).
- Changing the adapter: run `tests/adapter_contract` (telegram param) + `tests/telegram`; keep Telegram HTML valid (the fake rejects bad markup, >4096 UTF-16, callback_data >64 B).
- Real-Telegram checklist: README "Telegram".

