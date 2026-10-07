# web-muse

Localhost web wrapper around Muse: a browser UI bridged to one persistent
`muse serve` host (MSP v1 stdio). Python stdlib only — no build step, no deps.

## Start

```sh
python3 -m server.main --port 8000
# open http://127.0.0.1:8000/
```

## Run as a systemd service

The bridge ships as a per-user service (it must run as you so your
`muse login` passes through to the `muse serve` child — do not run it
as root):

```sh
./deploy/install.sh
# open http://127.0.0.1:8000/
```

This installs [`deploy/web-muse.service`](deploy/web-muse.service) to
`~/.config/systemd/user/`, then enables and starts it
(`Restart=on-failure`). Useful commands:

```sh
systemctl --user status web-muse
journalctl --user -u web-muse -f
systemctl --user restart web-muse
systemctl --user disable --now web-muse   # stop + don't start at login
```

Notes:

- The unit assumes a checkout at `~/web-muse`, Python at
  `/usr/bin/python3`, and `muse` at `~/.local/bin/muse`. If any path
  differs, override with `systemctl --user edit web-muse` (see the
  commented example at the top of the unit).
- Extra flags (`--provider`, `--model`, `--allow-host`, …) go in the
  same override as a replacement `ExecStart=` (clear it first, then
  set the new line — see the unit header).
- `loginctl enable-linger` (needs admin once) keeps the service
  running after you log out; without it, user services stop at logout.

Useful flags: `--provider echo` (offline smoke test), `--provider meta`,
`--model <id>`, `--trust-workspace` (default on; `--no-trust-workspace`
opts out), `--no-session-log`, `-v`.

The bridge binds **loopback only** and spawns `muse serve` inheriting this
process's environment, so your existing subscription login (`muse login`)
passes through untouched. No API keys are accepted or stored here.

## Layout

- `server/main.py` — entrypoint: spawn serve, run HTTP+WS on 127.0.0.1:8000
- `server/msp.py` — MSP client: NDJSON JSON-RPC, handshake, id map, receipts
- `server/sessions.py` — WS↔sessionId routing, cursor store, frame mapping
- `server/ws.py` — stdlib HTTP static server + minimal RFC 6455 WebSocket
- `web/` — static UI (`index.html`, `app.js`, `style.css`, `progress.html`)
- `web/themes/` — saved theme palettes (`<name>.json`); see `web/themes/README.md`
- `tests/` — frame-mapping tests with recorded fixtures + full-stack smoke test

## WS protocol (`/ws`, JSON text frames)

Client→server (each `{id, type, ...}` gets `{id, type:"result", ok, result|error}`):

| type | MSP call |
|---|---|
| `prompt {sessionId?, text, images?, ifBusy?}` | `session/start?` then `turn/start` |
| `new {…, mcpAttach?}` | `session/start` (+ bridge-side `config.mcpServers`) |
| `list {cursor?, limit?}` | `session/list` |
| `resume {sessionId}` | `session/resume` (+attach) |
| `read`, `fork`, `rename` | `session/*` |
| `delete` | removes session files from disk (no host call) |
| `interrupt`, `cancel`, `steer`, `unqueue` | `turn/*` |
| `approve {approvalId, choiceId, requirementId, feedback?}` | `approval/decide` |
| `answer {userInputId, answers}` | `userInput/answer` |
| `subscribe {sessionId, after?}` / `unsubscribe` | `view/subscribe` / `view/unsubscribe` |
| `page {sessionId, limit, cursor?, direction?}` | `view/page` |
| `models {sessionId?}` / `setModel` | `model/list` / `session/setModel` |
| `setApprovalMode` (allowAll permitted, warned in UI) | `session/setApprovalMode` |
| `compact`, `usage`, `pending` | `session/compact`, `usage/read`, `approval/listPending` |
| `setEffort {reasoningEffort}` | `session/setReasoningEffort` (tier validated) |
| `skills` | `skill/list` |
| `readOutput {itemId, outputRef, ...}` | `item/readOutput` |
| `mcp` | local `settings.json` inventory (MSP v1 has no mcp/* methods) |
| `browse {path?}` | list one server-side directory for the `+` explorer (empty → `$HOME`) |
| `githubRepos {search?, limit?}` | `gh repo list` rows `{name, fullName, private, defaultBranch, updatedAt}` (known rows cached, `gh` still checked for new ones every call; `stale: true` when `gh` fails and cached rows are served) |
| `githubClone {fullName, sessionId?, opId?}` | admit a shallow `gh repo clone` into `workspaces/<sessionId>/repo/` (cancellable) |
| `githubOpen {fullName, name?, mcpAttach?, opId?}` | admit a clone + `session/start` rooted at the clone |
| `githubCancel {opId}` | cancel a running clone |
| `githubClean {sessionId}` | delete one session's `repo/` leaf (session kept) |
| `ordersDecide {sessionId, orderId, approved}` | human verdict on a staged agent policy order (executes `allowedCommands.update`) |

Server→client: `{type:"hello"}`, `{type:"event", method, params}` (MSP
notifications routed by sessionId), `{type:"approval"}`, `{type:"userInput"}`.
Clone progress streams as global `{type:"event", method:"githubCloneProgress",
params:{opId, fullName, phase, line?}}` (`started|progress|completed|
cancelled`; no sessionId, so every client renders it). The outcome follows
as global `{type:"event", method:"githubCloneResult",
params:{opId, ok, result|error}}` — clones are admitted instantly (like
`compact`) so the connection stays responsive and `githubCancel` can
preempt a hanging clone. Agent orders arrive the same way: `themeApply`
(applies a validated theme at once) and `ordersPending` (a policy order
staged for a human's `ordersDecide`); every session workspace carries
the protocol in `.web-muse/ORDERS.md`, checked after each turn.

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
`/models /model /effort /default-effort /skills /mcp /output /compact /usage /pending`
`/interrupt /stop /cancel /steer /unqueue /older`
`/github list|clone|open|clean|cancel`
`/theme [name]`

`/model <id>` refreshes the catalog first, then matches exact → prefix →
substring; ambiguous prefixes list the candidates instead of guessing.
`/models` prints the full catalog rows (limits, cost, effort variants,
default marker, catalog source). The top-right picker and `/model` both
remember the choice as the default for created chats (`/new`, first
prompt, `/github open` carry it as the starting model); opening an
existing session never re-models it. The effort picker (left of the
model picker), `/effort`, and `/default-effort` do the same for
reasoning effort: the pick is remembered as the default for created
chats and applied to the current session when one is open.
`/effort <tier>` needs an open session;
`/default-effort <tier|clear|show>` works with none open.

Approval cards render only in the right-hand inspector (Approvals tab —
it opens itself on desktop when one arrives); the transcript keeps just
a pointer line. A follow-up sent while a turn runs queues host-side by
default and now says so (`queued behind the running turn …`);
`/unqueue` reclaims it.

`/usage` prints the subscription block plus the session cumulative tokens /
cost and the context-window line. The footer keeps two separate lines:
session tokens (`#sess-usage`) and subscription (`#usage`).

`/theme` with no argument lists the saved palettes in `web/themes/`
(drop a `<name>.json` file there to add one); `/theme <name>` applies
it, replacing the stored override so no colors leak across switches.

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
Deleting a session (`/delete`) removes its files from disk — the dated
session dir (`~/.local/share/muse/sessions/YYYY/MM/DD/<sessionId>/`)
and its view-store dir (`.msp-view-v1/<sessionId>/`) — directly, without
calling the host (whose deletion registry rejects back-to-back deletes
with `Store(Busy)`). The workspace directory is kept.

To work on an existing on-device directory instead, root the session
there at creation: `/new --path /dir` in the composer, or press `+`
for the filesystem explorer (opens at the server's `$HOME`; the path
field at the top accepts a pasted absolute path to jump there, and
`Use default workspace` keeps the default workspace). Any path
is allowed, but the first session touching one asks for an explicit
allow in the browser (remembered per browser); the bridge also
rejects roots that are not existing directories and creates nothing
outside its workspace base.

## GitHub sessions (gh-only v1)

`GitHub repos` in the sessions drawer (or `/github list [search]`) lists
your repos via the `gh` CLI — install it and run `gh auth login` in a
terminal first; the bridge never holds a token. `Open` (or
`/github open <owner/repo> [name]`) shallow-clones (`--depth 1`, default
branch only) into `workspaces/<sessionId>/repo/` and roots a session
there in one step; `Clone` (or `/github clone`) clones without opening.
Picking a repo to open is the consent, recorded in the browser's
directory allow-list like a manual-root confirm. One session per clone:
reopening a session reattaches to its dir, opening the same repo again
reclones fresh. `/github clean` removes one session's clone (session
kept); `/github cancel` stops a running clone. Deleting a session does
not delete its clone. Failures carry a machine-readable `code`
(`gh_missing`, `gh_unauth`, `invalid_repo`, …) and the UI prints the
`gh:` stderr tail verbatim.

Every fresh clone also gets the bridge's instruction file
(`server/github_instructions.md` rendered with the repo name) as
`AGENTS.md` — unless the repo ships its own, which is never overwritten
— covering branch/PR conventions with `gh` and how to avoid approval
prompts. The host loads it via `--trust-workspace` (default on; opt out
with `--no-trust-workspace`). Sessions rooted at a seeded clone
(`githubOpen` now, or a later `/new --path` at the same leaf) also get a
narrow auto-approve policy: all `gh`, everyday `git`, read-only shell
inspection (`ls`, `cat`, `grep`, `find`, …) and common build/test
runners (`npm`, `pytest`, `cargo`, `go`, `make`, …) run without
prompting, while destructive git (`reset --hard`, `clean -f`,
`push --force`, `branch -D`, …), shell mutation (`rm`, `mv`, `find
-delete`) and network fetchers (`curl`, `ssh`, …) still ask a human.
Auto-decisions are announced in the transcript (`auto-approved (github
policy): …`). In-session network itself is host sandbox policy: the
deployed unit runs `--sandbox-network enabled` so `gh`/`git` can reach
github.com (push, PR create); narrower modes stall those flows on
sandbox denials, and `gh` auth stays yours (`gh auth login`).

Sessions whose workspace directory was removed from disk are hidden
from the session bar (with a count note). `/sync` (or `Sync now` in the
bar) previews and, after confirm, removes those sessions' files from
disk — workspace-base roots only; external manual roots are always left
alone. The bar lists bridge-created sessions first (tracked in
`.web-muse-bridge-sids.json` at the session-store root, so the grouping
survives restarts), then TUI-created ones in host order.

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
