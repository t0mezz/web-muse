# 001: In-chat object selection

**Status:** Final (all decisions and scope explicitly accepted)

## Goal

Make answering the agent's in-chat selections fast and obvious. Today a
`userInput` question card shows option radios/checkboxes *plus* a custom
textbox *plus* an Answer button, and submitting a selection feels like a
detour through the textbox.

## Current behavior (researched, not decided)

- `onUserInput` (`web/app.js`) renders one card per request: per question,
  option labels with radio/checkbox inputs, a `…or type a custom answer`
  textbox (max 500 chars), and a single Answer button.
- Single-select pre-checks the first option; a typed custom answer wins
  over picked options on submit.
- Wire answer per question is exactly one of `selectedLabel` (single),
  `selectedLabels` (multiple), or `freeText`.
- Approval cards already submit on tap per choice button.

## Decisions

1. **Single-answer questions submit on tap.** Tapping an option on a
   single-select question sends the answer immediately (one action).
   Multi-question cards keep one submit action that sends the whole form.
2. **Multi-select is toggle chips plus one Submit.** Options toggle
   visibly on tap; Submit sends the set and stays disabled (with a toast
   explaining why) until the count satisfies min/max.
3. **Custom text stays, one row per question.** On single-question
   single-select cards it has an inline Send (Enter sends too) that
   submits `freeText` immediately; in multi-question/multi-select form
   mode it fills that question's slot and ships with the single submit,
   winning over picked options for that question. Option-less questions
   are just the textbox plus Send.

## Scope (accepted)

- **In:** `userInput` card rework in `web/app.js` + styles in
  `web/style.css`, verified against the live bridge.
- **Out:** approval cards, server/`msp` wire changes, new test
  frameworks, anything outside those two files.
- **Done means:** (1) single-select tap sends immediately; (2)
  multi-question/multi-select submit via one action with min/max
  enforced; (3) custom-text row behaves per decision 3; (4) this
  record Final; (5) live on page reload, no server restart.
- **Acceptance:** user replied "accept" to the scope text above in this
  session (2026-10-04). Later stages or wider boundaries need their own
  interview; ending this interview does not authorize implementation.

## Unresolved

*None.*
- Scope boundary (artifact classes in/out, done checklist) — to be fixed
  in writing and explicitly accepted before the interview ends.
