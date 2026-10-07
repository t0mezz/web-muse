# Saved themes

Each `<name>.json` file here is one named theme. The composer applies
one with `/theme <name>`; bare `/theme` lists every file found here
(served by the bridge as `GET /themes`).

## Format

A flat JSON object mapping a theme color name to a new value — the
same shape as `localStorage["web-muse:theme"]` overrides:

```json
{"accent": "#AEAC78", "bg": "#FCF0DA"}
```

Keys must exist in `../theme.js` (`WebMuseTheme.colors`); unknown
keys are ignored when the theme is applied. Missing keys fall back
to the defaults in `../theme.js`, so a file may hold a full palette
(like `default.json`) or just a few accents.

Applying a theme replaces the stored override wholesale, so switching
themes never leaks colors from the previous one. Agent `theme.apply`
orders replace the same way; an approved agent `theme.save` order lands
here as a new `<name>.json` file.

## Iterating

Editing a file here takes effect in one step — no bridge restart, no
switching away and back. The composer fetches theme files uncached, so
just re-apply: `/theme <name>` for any saved theme, or `/theme reload`
for the active one (bare `/theme` marks it with `*`).

## Colors

What each key in `../theme.js` (`WebMuseTheme.colors`) is used for.
All keys take a hex color, except `glow` (an `"r, g, b"` triplet) and
`scrim` (an `rgba(...)` string). `--mono` in `style.css` looks like a
theme key but is not one — it is the fixed monospace font stack.

### Surfaces

| Key      | Used for |
|----------|----------|
| `bg`     | Page background (`body`); fill of the composer input, card inputs/options, directory rows and popups; text color on the send/stop buttons (which sit on `fg`). |
| `panel`  | Raised surfaces: session sidebar, inspector drawer, running slash-popup, repo pills. |
| `panel2` | Sunken/active surfaces: session search field, hovered session/dir/repo rows, user message bubble (`.tline.user .txt`), markdown table headers, toast background. |
| `line`   | All 1px borders and dividers; off-state track of the starfield toggle (`#stars-toggle`). |

### Text

| Key     | Used for |
|---------|----------|
| `fg`    | Primary text everywhere; also the fill of the send/stop buttons and the focus color of dim controls. |
| `dim`   | Secondary text: toolbar, chips, tool/thinking lines, code blocks, slash-command names, empty states, session previews. |
| `faint` | Tertiary text: timestamps, section labels, transcript headings, option descriptions, file rows, breadcrumb separators. |

### Accent and status

| Key      | Used for |
|----------|----------|
| `accent` | Links in rendered markdown, the active tab underline, the streaming caret, the thinking spinner, the selected option border, the toast border, and the on-state of the starfield toggle. |
| `focus`  | Keyboard `:focus-visible` outline (2px shared ring). A darker accent on light themes so it holds 3:1 on every surface; dark themes reuse their accent. Guarded per theme by `tests/test_focus_rings.py`. |
| `ok`     | Connection dot (`.dot.on`), running-session icon, approval-card Allow button (with `onOk` text). |
| `warn`   | Busy connection dot, approval notice icon, tool-row kind label, approval-card and notice-pill borders. |
| `err`    | Offline connection dot; destructive/failed states (danger row hover, failed tool-group border, deny button, error toast border). |

### Message bubbles (reserved)

| Key     | Used for |
|---------|----------|
| `user`  | Defined but currently unreferenced: the user bubble uses `panel2` + `line` (`.tline.user .txt`). Reserved for future user-bubble theming. |
| `agent` | Defined but currently unreferenced: agent rows are transparent (`.tline.agent`). Reserved for future agent-bubble theming. |

### Selection and highlight backgrounds

| Key        | Used for |
|------------|----------|
| `select`   | Selected/checked states: active session row, checked checkboxes. |
| `warnBg`   | Notice pill background (`#notice`, with `warn` border and `warnFg` text). |
| `warnFg`   | Text on `warnBg` (the notice pill). |
| `errFg`    | Error text: failed transcript lines and tool groups, failed turn headings, directory errors. |
| `codeBg`   | Code/pre blocks in the transcript. |
| `cardBg`   | Approval/question card background (with `warn` border). |
| `pickedBg` | Picked option in a question card (with `accent` border). |

### On-colors and chrome

| Key        | Used for |
|------------|----------|
| `onOk`     | Text on the `ok` Allow button. |
| `onAccent` | Text on the `accent` primary directory button. |
| `chipInk`  | Text of the running status chip (which sits on `light`). |
| `light`    | Near-white chrome: 3D inset highlights on raised buttons, running-chip fill, copy-button/send-button hover fill, starfield-toggle thumb. |

### Effects

| Key     | Used for |
|---------|----------|
| `glow`  | Triplet behind `--glow-color`: composer focus aura, starfield-toggle focus ring, streaming glow keyframes. Note: `#input.idle-glow` hardcodes its own blue (`110, 168, 254`) in `style.css`, so the idle composer aura does not follow this key. |
| `scrim` | Translucent wash over the topbar and, with stars on, behind the sidebar/inspector. Stays inline-translucent by design. |

### Starfield (`stars.js`, not CSS vars)

Read live via `WebMuseTheme.get()` and painted as inline styles
(box-shadows, backdrop gradient), so they apply to the next field
created — `app.js` rebuilds the field on every theme apply.

| Key       | Used for |
|-----------|----------|
| `star`    | Star point color (default per layer, overridable per call). |
| `starBg0` | Top stop of the sky gradient. |
| `starBg1` | Bottom stop of the sky gradient. |
