---
name: dop-agent-tooling-review
description: >-
  Use when: periodically reviewing Copilot CLI session logs for this repo to
  find repeated/inefficient tool-call and bash-command patterns since the
  last review, and to propose or track tooling improvements (new scripts,
  skill updates, script fixes). Triggers on requests like "do a tooling
  review", "review recent sessions for improvements", "check what we can
  automate", or "resume the tooling review".
argument-hint: "[none — always reviews the current repo since the last recorded review]"
---

# DOP Agent Tooling Review

> **Output blocks:** improvement proposals → `ideas-format`; pattern counts → `chart-format`.

Formalizes the recurring "look at recent session logs and find what should be
automated" exercise into a repeatable process with persistent state, so each
review builds on the last instead of re-scanning everything from scratch.

Backed by `dop-agent/session_review.py` in this repo, which does the
data aggregation (tool-call counts, categorized/clustered bash commands,
diffed backlog). All judgment — what's worth automating, what's noise, what
to recommend — remains the agent's job every time.

State lives in `~/.copilot/tooling-review-state.json`, keyed by repo root, so
it works the same way for any repo this skill is run in.

## Workflow

### 1. Scan

```bash
python3 <repo>/dop-agent/session_review.py scan
```

Prints JSON with:
- `window` — the period covered (since the last review's end, or a 21-day
  bootstrap window on first run for this repo)
- `sessions_found` / `sessions_with_events`
- `tool_counts` — MCP/built-in tool call counts, most common first
- `bash_categories` — bash commands bucketed into categories (gitlab
  curl/poll, teams script usage, mr-render usage, vpn/CDP, bare sleep,
  ps-wait loops, inline python, git, jira/confluence curl, grafana curl,
  ssh/`c` wrapper, aws-cli, sqlite analysis, package installs, find, grep,
  other curl, misc)
- `bash_examples` — a few example commands per category
- `top_repeated_bash_signatures` — near-duplicate commands (numbers and tmp
  paths normalized away) that occurred 3+ times, most repeated first
- `previous_review.open_suggestions` — backlog items flagged in the last
  review that weren't acted on yet (empty/absent on first run)

### 2. Analyze

Using the scan output, write a human-facing analysis with **numbered
sub-headings** (one per finding), each covering:

- What the pattern is and how many times it occurred this window
- Whether it's genuinely automatable or one-off/expected noise
- For carried-over `open_suggestions`: has it grown, shrunk, or been
  resolved since the last review?
- A concrete, concise suggestion (new script, skill/instructions update,
  script fix) — analysis only, do not implement unless the user says so

Things worth explicitly checking each review, based on patterns seen so far:
- Repeated raw `curl` calls to a service that has an MCP tool or existing
  script covering the same operation
- Sleep/poll loops waiting on a GitLab MR/pipeline that block the current
  turn instead of registering an `mr`/`pipeline` watch via the `watch_add`
  MCP tool (the
  `gitlab-poll` skill/`gitlab_poll.py` CLI is disabled — see
  `skills/disabled/gitlab-poll/SKILL.md`; the `watch-manage` skill
  is likewise disabled in favor of the MCP tools — see
  `skills/disabled/watch-manage/SKILL.md`); or sleep/poll loops for
  other target systems that currently have no helper available at all
- A script requiring a manual token/env-var setup step before every run
  (candidate for auto-load-on-invocation, as done for `teams_chats.py`)
- An `.instructions.md` file describing low-level mechanics that a skill
  already owns and could drift out of sync with (as found with
  the former `MS-Teams.instructions.md`, now moved into the teams-retrieval skill)
- Manual process-management dances (start/stop/restart via raw `kill` +
  `nohup` + `sleep` + `tail`) that could become a small `ctl` script

### 3. Record

Whether or not anything was implemented in this session, close out the
review so the next one can build on it:

```bash
echo '{
  "findings": [
    {"id": "short-slug", "status": "implemented|suggested|rejected", "detail": "..."}
  ],
  "actions_taken": ["short free-text description of anything actually implemented"],
  "open_suggestions": [
    {"id": "short-slug", "detail": "...", "since": "YYYY-MM-DD"}
  ]
}' | python3 <repo>/dop-agent/session_review.py record --review-session-id <this-session-id>
```

`<this-session-id>` is this Copilot CLI session's own id — read it from the
`Session folder: /Users/.../session-state/<uuid>` line already present in
your system context (no tool call needed).

Carry forward any `open_suggestions` from `previous_review` that are still
unresolved (copy them into the new `open_suggestions` array, updating detail
if the situation changed), so backlog items aren't silently dropped between
reviews.

### Other commands

```bash
# Show past review entries for this repo (findings, actions, backlog, dates)
python3 <repo>/dop-agent/session_review.py history
```
