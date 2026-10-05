# 003: GitHub sessions — stage-2 plan (ACCEPTED as gh-only v1)

**Status:** Accepted 2026-10-05 after the stage-2 interview; implemented
as gh-only v1 (`server/github.py`, WS `github*` types, drawer picker +
`/github` family, `tests/test_github.py`). PAT fallback, OAuth device
flow, scheduled clone GC, and full-history/branch picker remain deferred.

## Goal

Log in with a GitHub account, list the user's repos, clone one onto
the device, and open a session rooted at the clone — reusing the
stage-1 manual-root plumbing (first-use confirm, `workspaceRoot` at
creation, bridge-side validation).

## Proposed shape (to be challenged in the stage-2 interview)

1. **Auth.** Three candidate methods, none chosen:
   - OAuth device flow (`POST github.com/login/device/code`, user code
     verified in a browser, token stored bridge-side). No secrets in
     the browser; needs a registered OAuth app (client id) and polling.
   - Personal access token pasted once (terminal or settings file, like
     `muse mcp login`), stored next to MCP secrets in
     `~/.config/muse/settings.json`. Simplest; token handling is the
     user's job.
   - Reuse host credential: shell out to `gh` if installed. Zero new
     secrets; fails closed when `gh` is absent (it is absent today).
2. **Repo listing.** Bridge-side `GET api.github.com/user/repos`
   (paginated, auth token in `Authorization` header, never sent to the
   browser); new `/ws` message (e.g. `githubRepos {}`) returning
   `{name, fullName, private, defaultBranch, updatedAt}` rows for a UI
   picker. Cache briefly; surface rate-limit errors honestly.
3. **Clone.** Bridge-side `git clone --depth 1 <url> <dest>` via
   subprocess (stdlib only, matching the repo's no-deps rule), then
   open the session with `workspaceRoot` = clone dir. Destination
   default proposal: a new base dir (e.g. `clones/<owner>/<repo>/`)
   so device repos stay separate from ephemeral `workspaces/`; exact
   base + naming (branch? rename on clash?) undecided.
4. **UI.** Repo picker (reuse the sessions-drawer list pattern or a
   card) with private-repo badge; clone progress is a long op — needs
   progress/error reporting, not a silent hang.
5. **Tests.** Python tests for URL/base validation and clone-dest
   naming (tmp dirs, `git` stubbed or `git init` fixtures); source-level
   UI tests following the repo convention; full suite green.

## Touchpoints (expected)

- `server/`: new module or `sessions.py` extension for GitHub API +
  clone; new `githubRepos`/`githubClone` WS message types; token
  storage beside MCP settings.
- `web/`: picker UI, clone progress, `/github` slash command family.
- `README.md`, `docs/decisions/002` follow-up, new tests.

## Risks

- Token storage on a shared Pi: file permissions + never-to-browser are
  load-bearing; needs the same "secrets never cross the boundary" rule
  as MCP.
- `git` binary/network availability on the host; large repos and
  `--depth 1` vs full history tradeoff.
- Private-repo URLs embed tokens — clone URLs must be built
  bridge-side and never logged or echoed to the UI.
- Rate limits / pagination on large repo lists.

## Open questions (stage-2 interview)

1. Auth method (device flow vs PAT vs `gh` reuse)?
2. Clone destination base + clash/branch policy?
3. Session↔repo mapping (one session per clone? re-clone or pull on
   reopen? update strategy for existing clones)?
4. Picker UX shape and clone-progress reporting?
