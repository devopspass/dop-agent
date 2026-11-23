---
name: ideas-format
description: "Use when: the user asks for a list of ideas/options/proposals to review (\"20 ideas to improve X\", \"options for Y\", \"what could we do\") — emit them as an `ideas` fenced block so the dop-agent Web UI shows an interactive ok/nok/skip triage widget whose result lands in the user's compose box."
---

# Ideas list format

When the user wants a list of ideas to triage, emit **one** fenced block with the info string `ideas`:

````
```ideas title="20 ideas for calls_transcriber" id=calls-transcriber
- [1] Auto summary and action items :: summary.md after the transcript is ready. You reread summaries, not transcripts | effort=S | prio=High
- [2] Full-text search CLI :: `ct search "stunnel"` over all transcripts | effort=S | prio=High
```
````

Rules:
- Attributes on the fence line: `title="..."` (quote if it has spaces) and `id=<short-slug>` (unique per message).
- One idea per line: `- [<id>] <short title> :: <description> | key=value | key=value`. Ids must be unique within the block (usually 1..N); a duplicate id or a malformed idea line makes the whole block render as plain code.
- Title: one short line. Description (after ` :: `): one or two sentences, may use `inline code`; no markdown tables, bold or links inside the block. Everything after the description is optional `key=value` metadata (e.g. `effort=S`, `prio=High`) shown as small tags (values only).
- Keep any intro and recommendation ("my picks: 3, 4, 5") as normal text outside the block. Don't also repeat the list as a table.
- In the Web UI the block becomes a Card / Selected / All widget; decisions are appended to the user's next message as `Feedback on "<title>" [<id>]` followed by `- [n] OK|NOK — title: comment` lines (skipped and undecided ideas are omitted; newlines in comments become ` / `). In Slack and other clients it stays a readable code block.
- Treat such feedback lines as decisions on those ideas: OK = go ahead / include, NOK = reject, no line = not decided.
