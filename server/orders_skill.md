# App orders: ask web-muse to change itself

Some changes live outside this workspace, where you cannot write: the
app theme and the bridge's own policy files. For those, write an order
file here and the app carries it out after your turn.

## Steps

1. Write `.web-muse/orders.json` in this workspace root:
   {"orders": [{"id": "theme-1", "action": "theme.apply",
     "params": {"colors": {"accent": "#AEAC78"}}}]}
   Done when: the file is valid JSON and every order has a unique `id`,
   a listed action below, and a `params` object.
2. End your turn, then read `.web-muse/orders.receipt.json`.
   Done when: every `id` you sent appears under `processed` with a
   `status`. `applied` means done. `needsConfirm` means a human decides
   in the app UI — nothing further for you to do. `rejected` names the
   `reason` — fix it, use a fresh `id`, and send again.
3. After a `theme.apply` reports `applied`, describe the visible result
   for the human so they can confirm it. Done when: you stated what
   changed and where to look.

## Actions

- `theme.apply` — recolors the app immediately. `params.colors` maps a
  theme color name to a new value. Unknown names are rejected and the
  receipt lists them.
- `allowedCommands.update` — proposes an allow/deny policy change in the
  config file's `allow`/`deny` shape. Never applies directly: it waits
  for a human's approval in the app, reported as `needsConfirm`.

## Limits

- Keep the file under 64KB with at most 20 orders; every `id` runs
  once, so resending needs a new `id`.
- Orders trigger only the actions above. Anything else is rejected.
