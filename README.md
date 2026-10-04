# web-muse

Localhost web wrapper around Muse: a browser UI bridged to one persistent
`muse serve` host (MSP v1 stdio). Python stdlib only — no build step, no deps.

## Start

```sh
python3 -m server.main --port 8000
# open http://127.0.0.1:8000/
```

Useful flags: `--provider echo` (offline smoke test), `--provider meta`,
`--model <id>`, `--trust-workspace`, `--no-session-log`, `-v`.

The bridge binds **loopback only** and spawns `muse serve` inheriting this
process's environment, so your existing subscription login (`muse login`)
passes through untouched. No API keys are accepted or stored here.

## Layout

- `server/main.py` — entrypoint: spawn serve, run HTTP+WS on 127.0.0.1:8000
- `server/msp.py` — MSP client: NDJSON JSON-RPC, handshake, id map, receipts
- `server/sessions.py` — WS↔sessionId routing, cursor store, frame mapping
- `server/ws.py` — stdlib HTTP static server + minimal RFC 6455 WebSocket
- `web/` — static UI (`index.html`, `app.js`, `style.css`, `progress.html`)
- `tests/` — frame-mapping tests with recorded fixtures + full-stack smoke test

## WS protocol (`/ws`, JSON text frames)

Client→server (each `{id, type, ...}` gets `{id, type:"result", ok, result|error}`):

| type | MSP call |
|---|---|
| `prompt {sessionId?, text, images?, ifBusy?}` | `session/start?` then `turn/start` |
| `new {…, mcpAttach?}` | `session/start` (+ bridge-side `config.mcpServers`) |
| `list {cursor?, limit?}` | `session/list` |
| `resume {sessionId}` | `session/resume` (+attach) |
| `read`, `fork`, `rename`, `delete` | `session/*` |
| `interrupt`, `cancel`, `steer`, `unqueue` | `turn/*` |
| `approve {approvalId, choiceId, requirementId, feedback?}` | `approval/decide` |
| `answer {userInputId, answers}` | `userInput/answer` |
| `subscribe {sessionId, after?}` / `unsubscribe` | `view/subscribe` / `view/unsubscribe` |
| `page {sessionId, limit, cursor?, direction?}` | `view/page` |
| `models {sessionId?}` / `setModel` | `model/list` / `session/setModel` |
| `setApprovalMode` (allowAll rejected) | `session/setApprovalMode` |
| `compact`, `usage`, `pending` | `session/compact`, `usage/read`, `approval/listPending` |
| `setEffort {reasoningEffort}` | `session/setReasoningEffort` (tier validated) |
| `skills` | `skill/list` |
| `readOutput {itemId, outputRef, ...}` | `item/readOutput` |
| `mcp` | local `settings.json` inventory (MSP v1 has no mcp/* methods) |

Server→client: `{type:"hello"}`, `{type:"event", method, params}` (MSP
notifications routed by sessionId), `{type:"approval"}`, `{type:"userInput"}`.

Every MSP command gets a fresh UUIDv7 `commandId` minted by the bridge;
client-supplied ids are never forwarded.

`turn/steer` requires the exact running turn: the bridge tracks it from
`turn/started` fan-out and fills `expectedTurnId` when the client omits it.

## Host reality (verified live against `muse serve` 1.4.2)

Durable hosts (the default) serve the full surface the UI needs:
`session/resume` history, `view/subscribe` cursors, `view/page` `{events,
nextCursor}`, live `item/delta` + `turn/completed` streaming. Two caveats:

- `setModel` exists but the echo provider rejects it (`invalid_target` —
  no model runtime); the rejection passes through to the UI.
- `--no-session-log` (memory-only) hosts are degraded by the host itself:
  `session/resume`, `session/read`, `view/subscribe`, and `view/page` all
  answer `-32601`, and turns flip `running→idle` with zero transcript
  events. The bridge degrades honestly there instead of failing the UI:
  `resume`/`read` fall back to `session/list` metadata + `history.mode:
  "none"` (`resume_unserved_by_host`), `subscribe` attaches locally, and
  `page` fails with `view/page is not served by this host` (the UI then
  disables `/older`). Prefer durable mode for the full UI.

## Slash commands (in the composer, `Tab`-completed)

`/help /new /list /sessions /resume /open /rename /fork /delete /clear`
`/models /model /effort /skills /mcp /output /compact /usage /pending`
`/interrupt /stop /cancel /steer /older`

`/model <id>` refreshes the catalog first, then matches exact → prefix →
substring; ambiguous prefixes list the candidates instead of guessing.
`/models` prints the full catalog rows (limits, cost, effort variants,
default marker, catalog source).

`/usage` prints the subscription block plus the session cumulative tokens /
cost and the context-window line. The footer keeps two separate lines:
session tokens (`#sess-usage`) and subscription (`#usage`).

`/mcp` lists the `mcpServers` entries from `~/.config/muse/settings.json`
(the same file `muse mcp login` uses) — MSP v1, stable and experimental,
exposes no MCP methods over the wire, so there is nothing else to query.
Per-session servers attach at creation only (`session/start` is the sole
wire touchpoint): `/new [name] --mcp a,b` resolves the named entries
bridge-side (secrets never reach the browser) into `config.mcpServers`.
OAuth stays in the terminal: `muse mcp login <server>`.

## Session workspaces

Every new session gets its own directory, `workspaces/<sessionId>/` under
the project (created before `session/start`, sent as `workspaceRoot`) —
this is what gives the agent shell and file tools. Override per session
by sending `workspaceRoot`, change the base with `--workspace-base DIR`,
or pass `--workspace-base ""` to restore the old workspaceless behavior.
Deleting a session does not delete its workspace directory.

To work on an existing on-device directory instead, root the session
there at creation: `/new --path /dir` in the composer, or press `+`
and type the directory (blank keeps the default workspace). Any path
is allowed, but the first session touching one asks for an explicit
allow in the browser (remembered per browser); the bridge also
rejects roots that are not existing directories and creates nothing
outside its workspace base.

## HTTP notes

Loopback bind plus a Host allowlist (`127.0.0.1`, `localhost`, `::1`;
foreign Host → 403) as a DNS-rebinding guard. Behind a tunnel (Cloudflare,
Tailscale funnel, …) trust its public hostname explicitly:

```sh
python3 -m server.main --port 8000 --allow-host muse.example.com
# or: WEB_MUSE_ALLOWED_HOSTS=muse.example.com,other.example \
#   python3 -m server.main --port 8000
```

Point the tunnel origin at `http://127.0.0.1:8000` (not `http://localhost:8000`):
some tunnel clients resolve `localhost` to `::1` first and fail when only
IPv4 is listening. The bridge now binds both `127.0.0.1` and `::1`, so
either origin works — but a literal `127.0.0.1` origin is the robust choice.

Unknown extensionless paths serve the app shell; missing dotted asset
paths are real 404s.

## Progress page

`/progress.html` (⏱ in the topbar) polls `/health` every 2s — bridge/MSP
liveness, server version, schema fingerprint, uptime, connections — plus a
live WebSocket event ticker.

## Tests

```sh
python3 -m unittest discover -s tests -v
# full stack only (spawns muse serve --provider echo on an ephemeral port):
python3 -m unittest tests.test_smoke -v
```
