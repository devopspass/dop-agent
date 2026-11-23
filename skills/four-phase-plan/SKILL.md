---
name: four-phase-plan
description: "Use when: the user asks for a '3 phases plan' / '4 phases plan' / 'plan → criticise → final plan', or for design-heavy or risky work planned with a built-in self-review. Flow via session_turn self-watches: plan v1, hostile critique, final plan, STOP; on 'proceed' implement, then automatic gaps review; 'fix gaps' fixes them. Chat messages only, no plan files."
---

# Four-phase plan (plan → criticise → final plan → implement → review)

> **Output blocks:** the closing 'proceed' / 'fix gaps' prompt → `actions` block (`actions-format`); flows/architecture in the plan → a `mermaid` fenced block; critique findings to triage → `ideas-format`. Open items/pending decisions in any report or final message → `ask` block, not text/table.

| Phase | Trigger | What happens | Changes allowed |
|---|---|---|---|
| 1 Investigate + plan v1 | User request | Facts, existing work, plan v1 | No |
| 2 Criticise | Self-watch from phase 1 | Hostile review, Adopt/Reject/Defer | No |
| 3 Final plan | Self-watch from phase 2 | Final plan, then STOP | No |
| Implement | User says "proceed" / "implement" | Do the work, as instructed for this run | Yes |
| 4 Review implementation | Self-watch from the implement turn | Gaps report, then STOP | No |
| 5 Fix gaps | User says "fix gaps" (or similar) | Fix the confirmed gaps | Yes |

## When to use
- The user names it ("3 phases plan", "plan, criticise, new plan").
- A wrong plan would be expensive: multi-repo or multi-env work, new automation, a rollout, a redesign.
- Don't use it for small fixes or one-liners.

## Hard rules
1. **No changes in phases 1–3 and 4.** Read-only investigation. Don't edit repos, create branches, MRs, tickets or comments, or touch AWS or envs. Put "NO CHANGES" into every watch label and every child-session prompt, because it doesn't carry over by itself. Changes happen only in the Implement turn and in Phase 5.
2. **No files.** Plans, critiques and reports are chat messages. Don't write plan files unless the user asks.
3. **Chain with session_turn self-watches.**
   - Register `watch_add(type="session_turn", key="self", conversation_id=<own conversation_id>)` as the LAST action of the turn, after the report is written, and only once per turn. A self watch fires as soon as the current turn completes. The chain stays within the self-watch `max_chain` limit (default 10 auto-resumes).
   - Phases 1 and 2 register the next phase. Phase 3 registers nothing. The Implement turn registers Phase 4. Phase 4 registers nothing.
   - If the user's original request already authorized implementation ("plan and implement it"), Phase 3 may register the Implement turn instead of stopping. Say so in the Phase 3 message.
4. **Make every watch label self-contained.** The label is the next phase's whole instruction: the phase name ("TURN 2 of 3 (criticise): ..."), what to do, NO CHANGES (where it applies), and the output format.
5. **Each message restates what the next phase needs.** Context can be compacted between turns, so quote the plan's key points and the adopted findings. Don't point at "the plan above".
6. **Plan from "what we have".** Baseline on the current state and cover it. Don't change live systems unless the user decides to. Account for the user's backlog of unreviewed or unapplied changes, and prefer small, safe, non-prod-first steps.
7. **Output style.**
   - Tables first, then short prose.
   - Every MR, ticket and session reference is a clickable link.
   - Use human-readable names, not internal codes or abbreviations.
   - Keep it concise.
8. **Proceed gate.** Don't implement until the user says "proceed" or "implement". In the Implement turn, follow the run-specific instructions the user gives (e.g. "skip tests", "don't commit", "all tests and restarts"), and record what was skipped so Phase 4 can report it.

## Phase 1 — Investigate and propose (plan v1)
- Gather facts: repos, existing modules/wiring, tickets (`jira_api.py search`), and recent sessions (`session_search`).
- Check what is already done or in flight before proposing anything. Reuse beats new work.
- If the topic belongs to a session group or related sessions, enumerate ALL of them (`session_list` with `group_id`, then `session_get` each) before planning. Don't plan from the sessions you remember.
- For independent fact-checks, use read-only child sessions or `explore` agents. Prefer `session_send` with `interrupt=False` into an existing idle or finished session over creating a new one. Repeat the NO-CHANGES rule in every prompt.
- Post plan v1:
  - goal and non-goals;
  - current state, with evidence and links;
  - ordered steps with dependencies;
  - assumptions, open questions and risks;
  - rough sizing.
- Then register the watch labelled "TURN 2 of 3 (criticise): ...".

## Phase 2 — Criticise, then adopt what's reasonable
Act as a hostile reviewer of your own plan.
- Look for: gaps, wrong ordering, hidden dependencies, unrealistic assumptions, missing steps (secrets, permissions, rollback, testing, cross-env impact), conflicts between decisions, under-estimated effort, and work already done elsewhere.
- Verify doubtful claims with read-only checks. Don't just assert them.
- Post a findings table:

| # | Finding | Severity | Verdict (Adopt / Reject / Defer) | Reason |

- Adopt only what is reasonable. Reject nitpicks, scope creep and speculative risks, and say why.
- Then register the watch labelled "TURN 3 of 3 (final plan): ...".

## Phase 3 — Propose the new (final) plan, then stop
- Post the final plan, which supersedes v1. Include:
  - the adopted findings;
  - any user feedback received since v1;
  - milestones M0..Mn with exit criteria;
  - critical path and parallel tracks, when relevant;
  - numbered decisions needed from the user, each with a recommended default — BLOCKING: invoke the `ask-format` skill first and emit them as an `ask` fenced block (never a table or plain list);
  - risks and mitigations;
  - effort sizing (T-shirt);
  - "what changed vs v1".
- Register no watch (unless implementation was pre-authorized, see rule 3). End with "Say 'proceed' to implement." and an `actions` block (skill `actions-format`) with `- * Proceed | proceed`.

## Implement turn (user-triggered)
- Triggered by "proceed" / "implement". Implement the final plan exactly, following the user's run-specific instructions (rule 8). Note any deviation from the plan and why.
- Post a short summary: what was done, what was skipped or not verified, and links to MRs, tickets and pipelines. Register the usual MR/pipeline watches as the workspace rules require.
- As the LAST action, register the Phase 4 watch: "TURN 4 (review implementation): ... NO CHANGES ...". The label must include the plan's key points and the list of what was skipped.

## Phase 4 — Review the implementation, report gaps
- NO CHANGES. Review what was actually built against the final plan and the user's later requests. Verify by running read-only checks. Don't claim what you didn't verify.
- Post a gaps report, most serious first, in two tables:
  - **Gaps against the plan or requests**: Gap | Severity | Evidence.
  - **Unverified or untested**: what, and why (skipped tests, no restart, nothing checked in a browser, etc.).
- Add a proposed fix list. Register no watch. End with "Say 'fix gaps' to fix these (or name which ones)." and an `actions` block (skill `actions-format`) with `- * Fix gaps | fix gaps`.

## Phase 5 — Fix gaps (user-triggered)
- Triggered by "fix gaps" or similar. Fix only the confirmed gaps, or the ones the user names, with the run-specific instructions from rule 8.
- If the fixes are substantial, the user can ask for another Phase 4 review. Register it the same way, as the last action.

## Pitfalls
- Missing, early (before the report) or duplicate next-phase watch breaks or double-fires the chain.
- A thin watch label forces the next turn to guess; make labels self-contained.
- Not enumerating all sessions in the group misses whole workstreams.
- A child-session prompt without NO-CHANGES can cause unwanted changes.
- Intermediate results kept in /tmp or files get lost; keep them in chat.
- Every critique finding needs an Adopt, Reject or Defer verdict with a reason.
- Phase 4 must list gaps, including what the Implement turn skipped.
- If a phase turn fails or can't finish, say so and ask the user; don't silently continue the chain.
