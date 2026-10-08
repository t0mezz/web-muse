# App orders: ask web-muse to change itself

Some changes live outside this workspace, where you cannot write: the
app theme, the bridge's own policy files, and the backend process
itself. For those, write an order file here and the app carries it
out, usually within seconds while your turn is still running.

## Steps

1. Write `.web-muse/orders.json` in this workspace root:
   {"orders": [{"id": "theme-1", "action": "theme.apply",
     "params": {"colors": {"accent": "#AEAC78"}}}]}
   Done when: the file is valid JSON and every order has a unique `id`,
   a listed action below, and a `params` object.
2. Read `.web-muse/orders.receipt.json`.
   Done when: every `id` you sent appears under `processed` with a
   `status`. `applied` means done. `needsConfirm` means a human decides
   in the app UI — nothing further for you to do. `rejected` names the
   `reason` — fix it, use a fresh `id`, and send again. If an `id` is
   still missing after a minute, end your turn (no more tool calls)
   and read the receipt again on your next turn: the app re-checks
   after every turn.
3. After a `theme.apply` reports `applied`, describe the visible result
   for the human so they can confirm it. Done when: you stated what
   changed and where to look.

## Actions

- `theme.apply` — recolors the app immediately, including a running
  starfield (it rebuilds on the new sky). `params.colors` maps a
  theme color name to a new value. Partial sends are fine: keys you
  omit keep their current values. To revert a recolor, re-apply the
  defaults below. Unknown names and wrongly shaped values are
  rejected and the receipt lists them with the expected shape.
- `theme.save` — proposes a reusable named theme:
  `params: {"name": "harbor-dusk", "colors": {...full palette...}}`.
  Never applies directly: it waits for a human's approval in the app,
  reported as `needsConfirm`. On approval it is saved as
  `web/themes/<name>.json` and appears under bare `/theme`. Prefer this
  over `theme.apply` when the palette is worth keeping.
- `allowedCommands.update` — proposes an allow/deny policy change in the
  config file's `allow`/`deny` shape. Never applies directly: it waits
  for a human's approval in the app, reported as `needsConfirm`.
- `bridge.restart` — asks a human to restart the backend, e.g. after
  server code changed and needs a reload you cannot trigger yourself.
  Params are optional: `{"reason": "why", "delaySeconds": 2}` (`reason`
  is shown on the approval card, at most 500 chars; `delaySeconds`
  0–30 is the grace between approval and restart so the reply flushes
  first). Never restarts directly: it waits for a human's approval in
  the app, reported as `needsConfirm`. Approval briefly disconnects
  every browser tab (they reconnect on their own), interrupts running
  turns, and preserves sessions; denial does nothing.

## Theme colors

30 color names in 9 roles. Send only the roles you are changing;
omitted roles keep their current values.

Value shapes (wrong shapes are rejected, never silently applied):
every role takes `#rgb`, `#rrggbb` or `#rrggbbaa` (e.g. `"#AEAC78"`),
EXCEPT `glow`, which takes an `"r, g, b"` triplet with each part 0-255
(e.g. `"174, 172, 120"`), and `scrim`, which takes an `rgba(...)`
color (e.g. `"rgba(241, 230, 209, 0.4)"`).

- Surfaces: `bg`, `panel`, `panel2`, `line` — page, cards, second-level
  cards, borders.
- Text: `fg`, `dim`, `faint` — body, secondary, faintest, read on the
  surfaces above.
- Accent: `accent`, `focus`, `onAccent`, `glow` — highlights, the
  keyboard focus ring (the accent itself on dark themes, a darker
  accent on light ones, holding 3:1 on every surface), text sitting
  on the accent, the accent's RGB triplet reused for glows.
- Status: `ok`, `onOk`, `warn`, `warnBg`, `warnFg`, `err`, `errFg` —
  success, warning, error, each background paired with its text.
- Messages: `user`, `agent`, `select` — user bubbles, agent bubbles,
  selection highlight.
- Code and cards: `codeBg`, `cardBg`, `pickedBg` — code blocks, approval
  cards, picked options.
- Ink: `chipInk`, `light` — chips and bright overlays.
- Sky: `star`, `starBg0`, `starBg1` — starfield dots and gradient.
- Veil: `scrim` — modal overlay.

Change paired roles together, never alone: `accent` with `focus`,
`onAccent`, and `glow`; `bg` with `fg`, `dim`, `faint`; `warnBg` with `warnFg`;
`ok` with `onOk`; the `star` trio together. A lone accent or surface
recolor strands its old partners and usually lowers contrast.

Contrast (advisory, not a check — keep these readable by eye): body
text pairs (`fg`, `dim` on `bg`/`panel`) want roughly 4.5:1 or better;
large/faint text (`faint`, `warnFg`, `errFg`, `chipInk`) and text on
accents (`onAccent` on `accent`, `onOk` on `ok`) at least ~3:1. Keep
the palette on one side of the light/dark line: light surfaces need
dark text, dark surfaces need light text. When unsure, keep the
default text roles and only move the surfaces and accent.

Current defaults (copy, edit, send back the roles you change):

```json
{"bg": "#0C0C0C", "panel": "#15171A", "panel2": "#1E2126",
"line": "#33363A", "fg": "#CCD3DB", "dim": "#935B6C",
"faint": "#A2717F", "accent": "#D25380", "focus": "#D25380",
"ok": "#98614A", "warn": "#FB3D18", "err": "#ED1C24",
"user": "#1E2126", "agent": "#15171A", "select": "#3A2029",
"warnBg": "#2A1C12", "warnFg": "#8A9098", "errFg": "#ED1C24",
"codeBg": "#15171A", "cardBg": "#1E2126", "pickedBg": "#2A2E34",
"onOk": "#FFFAF4", "onAccent": "#0C0C0C", "chipInk": "#742E46",
"light": "#fff", "glow": "210, 83, 128",
"scrim": "rgba(12, 12, 12, 0.55)", "star": "#F6E3C2",
"starBg0": "#0C0C0C", "starBg1": "#050607"}
```

## Limits

- Keep the file under 64KB with at most 20 orders; every `id` runs
  once, so resending needs a new `id`.
- Saved-theme `name` is lowercase letters, digits and hyphens
  (`[a-z0-9-]{1,64}`, e.g. `harbor-dusk`).
- Orders trigger only the actions above. Anything else is rejected.
