# 002: External sessions

**Status:** Final (scope explicitly accepted; user replied "accept, and
build it" to the scope text below in this session, 2026-10-04)

## Goal

Let sessions work on directories outside the built-in
`workspaces/<sessionId>/` tree. Initial ideas from the user:

1. Manually add sessions from device storage (point a session at an
   existing local directory).
2. Log in with a GitHub account, fetch the user's repos, and let the
   client clone them to work on them.

## Current behavior (researched, not decided)

- `session/start` accepts `workspaceRoot`/`workspaceRoots`, which override
  the default `workspaces/<sessionId>/` dir (`server/sessions.py`); the
  browser client can already name its own root(s).
- `session/list` accepts a `workspaceRoot` parameter passthrough.
- No GitHub, clone, OAuth, token, or credential code exists anywhere in
  the repo (searched); `gh` is not installed on the host.
- The browser client is static files only — any clone must run
  bridge-side (needs a `git` binary + network on the host), exposed as
  new `/ws` message types plus UI.
- Existing secrets precedent: MCP secrets resolve bridge-side from
  `~/.config/muse/settings.json` and never cross the browser boundary.

## Decisions

1. **Manual paths first, GitHub second.** Stage 1 (this record) covers
   pointing sessions at existing local directories only — no
   credentials, no network, no new binaries. It proves the workspace
   plumbing (`workspaceRoot` override, directory trust) that cloning
   reuses. GitHub login + clone is a deferred stage 2 with its own
   interview; the user will receive its implementation plan as a doc
   when stage 1 lands. (Accepted 2026-10-04.)
2. **Any path, first-use confirm.** Manual roots may point anywhere on
   the device, but the first session that touches a directory triggers
   an explicit confirm (allow/deny) before `session/start` runs; denied
   or unconfirmed directories are rejected with a clear error.
   (Accepted 2026-10-04.)
3. **Root at creation, two entries.** The directory is fixed when the
   session is created (never re-rooted later): `/new --path <dir>` in
   the composer, and the `+` button asks for the path via the same
   prompt convention as rename. Both flow through the Decision-2
   confirm before `session/start`. (Accepted 2026-10-04.)

## Scope (accepted)

- **In:** `/new --path <dir>` parsing in `web/app.js` (extends the
  existing `--mcp` arg pattern, `/help` + Tab-completion updated); `+`
  button (`btn-new`) asks for the path via the rename-style prompt;
  first-use allow/deny confirm in the UI before `session/start`,
  remembered per browser; bridge-side validation that a manual root
  exists and is a directory (nothing auto-created); README
  session-workspaces note; source-level + validation tests, all on the
  `external-sessions` branch.
- **Out:** GitHub login/clone (stage 2, separate interview), re-rooting
  live sessions, server-side confirm storage, any auth/token code.
- **Done means:** (1) `/new --path` and `+` both create sessions rooted
  at the typed dir after confirm; (2) denied/unconfirmed/nonexistent
  paths never reach `session/start`, with a clear error; (3) full suite
  green; (4) this record marked Final upon acceptance.
- **Acceptance:** user replied "accept, and build it" to the scope text
  above in this session (2026-10-04). Accepting this record never
  approves later stages; each stage returns for its own interview.

## Unresolved

1. ~~Commit identity~~ — settled (see git history);
   base commit `c0757fa` on `main`, feature branch `external-sessions`.
2. ~~Staging order~~ — settled by Decision 1 above.
3. GitHub auth method (stage 2): OAuth device flow, personal-access-token
   entry, or reuse of an existing host credential (e.g. `gh`)?
4. ~~Manual-path trust~~ — settled by Decision 2 above. (Clone
   destination stays a stage-2 question.)
5. ~~Session↔directory mapping~~ — settled by Decision 3 above.
6. ~~Stage-1 boundary/done~~ — settled by the accepted scope above.
7. Stage 2 (GitHub) is intentionally undesigned: auth method, clone
   destination, and session↔repo mapping each need their own interview.
   See the stage-2 plan doc for the proposal.
