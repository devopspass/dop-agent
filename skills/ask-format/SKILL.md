---
name: ask-format
description: "Use when: you need to ask the user questions (Q1, Q2...) or request decisions (D1, D2...) with a few predefined answers — emit them as an `ask` fenced block so the dop-agent Web UI shows clickable chips with a popover (options + note) whose result lands in the user's compose box."
---

# Questions / decisions format

When you need answers or decisions from the user, emit **one or more** fenced blocks with the info string `ask`:

````
```ask title="MR !52 review" id=mr52
D1 single: Where should the BIND logic live?
- a: Keep it in the base module | recipe is self-contained, behind a feature flag
- b*: New example-app-bind module | follows the example-app-<APP> naming rule
Q1 multi: Which envs to roll next?
- tst*: tst
- stg: stg
? other envs?
Q2 text: Reviewer for the new MR?
? e.g. Juraj
```
````

Rules:
- Options are never inline: `Q1: Scope? [a | b]` parses as a free-text question titled `Scope? [a | b]`. Write the mode as a bare word (`D1 single: title`) and each option as its own `- key: label` line.
- Every message that still has open decisions must re-emit them as a full `ask` block; never just write "waiting on D1 above".
- Fence attributes: `title="..."` and `id=<slug>` (unique per message; defaults to the block index).
- Item header: `Q<n>` = question, `D<n>` = decision, then optional mode `single` (default) | `multi` | `text`, then `: title`. Ids are unique within the block.
- Option: `- <key>[*]: <label> | <optional note>`; key is `[A-Za-z0-9_-]+`; `*` marks the option you recommend (shown as "suggested").
- `? <hint>` after an item = placeholder for the free-text note. An item without options is a free-text question.
- Keep prose (context, your recommendation) outside the block, and add a one-line plain-text summary of what you ask — Slack and other clients show the block as plain code.
- Don't use tables, bold or links inside the block. Invalid blocks (empty, duplicate ids/keys, stray lines, option before any header) render as a plain code block.
- In the Web UI the answers are appended to the user's next message as `Answers on "<title>" [<id>]` followed by `- [D1] <question> → b. <label> — <note>` lines. Treat them as the user's decisions; items without a line are unanswered or skipped.
