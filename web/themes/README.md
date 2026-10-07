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
themes never leaks colors from the previous one.
