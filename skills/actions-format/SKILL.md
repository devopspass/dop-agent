---
name: actions-format
description: "Use when: you end a message with a short reply the user will likely type next (\"proceed\", \"fix gaps\", \"go\", \"yes, create it\", \"skip tests\") — emit them as an `actions` fenced block so the dop-agent Web UI shows clickable quick-reply chips that insert the text into the user's compose box (Slack shows a plain code block, Telegram shows inline buttons, so keep the prose prompt too)."
---

# Quick-reply actions format

Emit **one** fenced block with the info string `actions` at the end of the message:

````
```actions id=next
- * Proceed | proceed
- Fix gaps | fix gaps
- Skip tests
```
````

- Line grammar: `- label | text sent` (`| text` optional); prefix `* ` for the primary action (`- * label`). Never write `[*]` — brackets render literally; the text defaults to the label.
- 1–4 short actions; text ≤ 500 chars (longer text is truncated). The block must start at line start (never inline/backtick-wrapped).
- Optional `id=` / `title=` attributes on the fence line (`id` prefixes the chip ids).
- Clicking inserts the text into the compose box (no auto-send, no duplicate line); in the Messages inbox it sends directly.
- Chips render only once the turn has finished; until then, and in Slack, the block shows as plain code, so keep the prose prompt ("Say 'proceed' to implement."). Telegram turns each action into an inline button.
- Picking between alternatives (A or B, which option) = decision → `ask-format`, NOT actions. Real choices with options/notes → use `ask-format`; lists to triage → `ideas-format`. Not for destructive actions the user has not asked for: label them clearly.
