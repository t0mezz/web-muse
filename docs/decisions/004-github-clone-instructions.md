# 004: Per-session instruction set + command allowlist for GitHub-cloned sessions

**Status:** Implemented (Track 1 seed + gh/everyday-git auto-approve;
`--trust-workspace` default on). The analysis below is kept as the
decision record.

**Bottom line up front:** no wire mechanism for either half exists today.
`session/start` accepts no instructions/rules/allowlist key (schema-checked:
absent in stable *and* experimental bundles), approval modes are
select-never-create with a closed 4-value enum, sandbox switches are
host-lifetime flags, and `--permission-profile` is TUI-only. The only
host-honored instruction channel is **workspace files loaded under
`--trust-workspace`** (currently off), and the only per-session
command-gating knob is **`approvalMode` at `session/start`**. The
recommendation (§c) combines both with bridge-side file seeding.

## (a) Current session-creation flow for GitHub clones

Step-by-step, UI → bridge → `gh` → MSP host:

1. **UI sends `githubOpen`.** `githubOpenRepo()` in `web/app.js:1857`
   sends `{type: "githubOpen", fullName, name, opId}` (`web/app.js:1865`).
   Note: it sends **no `mcpAttach`, no `approvalMode`, no `config`** —
   the drawer `open` button path (`web/app.js:1986`) likewise passes only
   the repo name. (`githubClone` is the clone-without-session twin,
   `web/app.js:1839`, sending `{type: "githubClone", fullName, opId}`.)
2. **Bridge admits the op async.** `handle_client_message` routes
   `"githubOpen"` to `_do_github_open()` (`server/sessions.py:789-796`),
   which validates `owner/repo` (`server/github.py:60-71`, via
   `validate_fullname`), mints a fresh `sessionId` (`uuid7()`),
   resolves the leaf dest, and calls `_launch_clone()`
   (`server/sessions.py:1112-1120`). The WS reply is instant
   (`{accepted, opId, ...}`); the outcome later arrives as a global
   `githubCloneResult` event — same pattern as `githubClone`
   (`server/sessions.py:960-977`, `README.md:94-102`).
3. **Clone runs bridge-side.** `_clone_task()` awaits `run_gh_clone()`
   (`server/sessions.py:987-996`), which shells out to
   `gh repo clone <fullName> <dest> -- --depth 1`
   (`server/github.py:126-129`) — shallow, default branch only — into
   the leaf `workspaces/<sid>/repo/` (`server/sessions.py:919-938`;
   leaf creation refuses a session that already holds a clone with
   `dest_exists`, and wipes non-git residue so retries start clean).
   Progress streams as global `githubCloneProgress` events
   (`server/sessions.py:940-951`).
4. **Session starts rooted at the clone.** On clone success,
   `_open_cloned()` builds `new_msg = {"workspaceRoot": dest,
   "sessionId": session_id}` plus optional `mcpAttach`/`name`, and calls
   the shared `_do_new()` (`server/sessions.py:1048-1066`). `_do_new()`
   forwards exactly `approvalMode, modelId, providerId, sessionId,
   workspaceRoot, workspaceRoots, config` to `session/start`
   (`server/sessions.py:857-862`), resolves `mcpAttach` names to secrets
   bridge-side via `build_session_mcp_config()`
   (`server/sessions.py:882-900`, `server/sessions.py:214-264`), mints
   `commandId` fresh, and calls `session/start`
   (`server/sessions.py:901`). Optional `name` becomes a
   `session/rename` (`server/sessions.py:1055-1063`).
5. **UI attaches.** `onGithubResult()` (`web/app.js:1876`) records the
   dest in the browser first-use allow-list without a prompt — "picking
   the repo is the consent" (`web/app.js:1788-1800`,
   `server/sessions.py:789-796` comment) — prints
   `Opened <repo> → session <id> (<dir>)`, and opens the session
   (`web/app.js:1888-1893`).

Two load-bearing facts for what follows:

- `_open_cloned()` is the **single choke point**: every github-opened
  session passes through `server/sessions.py:1048-1066`, which today
  forwards only `workspaceRoot/sessionId/mcpAttach/name`. Any
  per-github-session default (approval mode, config, seeded files) slots
  in here with no protocol change.
- The bridge spawns the host as `[muse_bin, serve, (--provider,
  --model, --no-session-log, --trust-workspace)?]` only
  (`server/main.py:126-136`). The deployed unit passes **no**
  `--trust-workspace` (`deploy/web-muse.service`, `ExecStart=` line),
  and `main.py` defaults it off (`server/main.py:48-49`), so workspace
  files currently load for **no** session.

## (b) Candidate mechanisms (source-cited pros/cons)

### B1. `session/start` `config` — REJECTED (no such key)

- The stable schema's `SessionConfig` admits **only `mcpServers`**:
  "Reserved for per-session overrides owned by a future Configuration
  section. Issue #32760 admits only its `mcpServers` member."
  (`msp.schema.json`, `$defs/SessionConfig`, `muse` 1.4.2 stable
  bundle; fingerprint in `server/msp.py:17-20`).
- Unknown keys are "ignored on the wire, never rejected" (same `$def`),
  so a `config.instructions` / `config.rules` / `config.allowlist`
  would be **silently dropped** — the worst failure mode (looks sent,
  never applied).
- Verified identical in the experimental bundle
  (`muse schema generate-json-schema --out DIR --experimental`:
  `SessionConfig` and `SessionStartParams` byte-equivalent on these
  defs). String search over the full stable bundle finds no
  `permissionProfile`, `instructions`, `systemPrompt`, or
  `allowlist`/`allowList`/`denylist` anywhere; the three `rules`
  hits are prose ("supply inline rules", "`:workspace_roots` rules").
- `SessionStartParams` keys are exactly `approvalMode, commandId,
  config, modelId, providerId, sessionId, workspaceRoot,
  workspaceRoots` (stable bundle). **There is no per-session
  instruction or policy surface on the wire.**
- Pro: zero bridge work *if* it existed. Con: it does not exist; anything
  we put there today is dead bytes. (Upshot: this is a host feature
  request, not an implementation path — see §d Q1.)

### B2. Workspace files (`AGENTS.md` / skills / rules) + `--trust-workspace` — VIABLE, recommended for instructions

- `muse serve --help` (1.4.2): "`--trust-workspace` — Load each session
  workspace's skills and rules." TUI help agrees: "`--trust-workspace`
  — Trust this workspace for this run (load its skills and rules); does
  not save trust" (`muse --help`, Safety section).
- `muse init` scaffolding verified live in `/tmp/muse-init-probe/probe1`
  (never in a real workspace): `muse init --force` writes **exactly one
  file, `AGENTS.md`** ("Muse Code reads this file as project rules when
  it runs in this directory."). `muse init --help` offers only
  `[--dry-run] [--force]`. So the host's "rules" channel is, concretely,
  workspace-root markdown (+ skills — see B3).
- Repo precedent: the project root already carries such a file
  (`AGENTS.md`: code map, conventions, workflow, service).
- Pros: the **only host-honored instruction channel**; file lives with
  the clone so it survives resume/reattach (resume keeps workspaceRoot);
  works without any wire change; per-clone content is trivially
  templated bridge-side (repo name, branch, default tasks).
- Cons: (1) needs `--trust-workspace` on the serve argv — a
  **host-lifetime** switch (`muse serve --help`: "Sandbox posture and
  session durability are constructed here and apply to every session
  the host loads; neither is negotiable over the wire"), so it cannot
  be scoped to github sessions only; every session's workspace files
  (including ephemeral `workspaces/<sid>/`) then load. (2) The clone
  dir **is** the session root (`workspaceRoot = dest =
  .../repo/`), so a bridge-seeded file lands **inside the git clone**
  and shows in `git status` — pollution vs. visibility tradeoff (§d
  Q2). (3) **Untrusted-content loading**: clones are third-party code;
  once trust is on, a repo's *own* `AGENTS.md`/skills also load —
  prompt-injection surface; bridge-seeded vs repo-authored precedence
  is unknown (§d Q3). (4) Requires a host restart to enable
  (`systemctl --user restart web-muse`, per `AGENTS.md` Workflow/Service
  notes; serve flags are fixed at spawn).

### B3. Project skills in the clone (`SkillSource: project`) — VIABLE, same gate as B2

- `skill/list` is per-session: "`skill/list` params… Per-session because
  skill scope follows the session's workspace and plugin state"
  (stable bundle, `$defs/SkillListParams`); `SkillSource` enum is
  `bundled|user|project|plugin`. `muse skills list … [--workspace
  <path>] [--trust-workspace]` ties workspace skill resolution to the
  same trust flag.
- Pros: distributable instruction bundles (a checked-in skill dir in or
  beside the clone travels with it); composes with B2.
- Cons: same `--trust-workspace` host-global gate; skill authoring
  format was not mapped in this pass (only the list/inspect surface in
  `muse skills --help`); heavier than one markdown file for a fixed
  instruction set.

### B4. `approvalMode` at start (+ `session/setApprovalMode`) — VIABLE, recommended for the allowlist half (coarse)

- `session/start.approvalMode`: "The session's starting approval mode…
  Select, never create — the value names a mode the host's configuration
  already defines" (stable bundle, `$defs/SessionStartParams`).
  `session/setApprovalMode`: "**Select, never create.** A client may
  *select* a mode the host's configuration already defines; it may not
  construct one, supply inline rules, or otherwise describe a policy on
  the wire — which is why `ApprovalMode` is closed" (stable bundle,
  `$defs/SessionSetApprovalModeParams`). Closed enum:
  `allowAll|promptUnmatched|onRequest|denyUnmatched` (stable bundle,
  `$defs/ApprovalMode`, `"x-msp-openness": "closed"`).
- Bridge already forwards `approvalMode` in `_do_new()`
  (`server/sessions.py:859-864`) and exposes `setApprovalMode` with
  `allowAll` rejected bridge-side (`server/sessions.py:70-77`,
  `713-726`; `server/main.py:11-12`).
- Pros: the **only per-session command-gating knob on the wire**;
  `denyUnmatched` (or `promptUnmatched`) as the github-open default is
  a ~5-line change confined to `_open_cloned()`; tighten/loosen later
  via existing `setApprovalMode` plumbing — no host restart, no schema
  change.
- Cons: **not an allowlist** — it selects a preconfigured host mode,
  never a command list; which commands each mode gates is host-defined
  and was not enumerated in this pass. Do not present it as "only `git`
  + `gh` may run": the wire cannot express that.

### B5. `--permission-profile` — REJECTED (TUI-only, no wire form)

- `muse --help` (TUI options) lists "`--permission-profile <ID>` —
  Select a named permission profile for this session." `muse serve
  --help` lists **no** such flag (only `--disable-sandbox`,
  `--sandbox-network`, `--disable-write`, `--disable-shell`,
  `--trust-workspace`), and its header states approval "is selected on
  the wire, so there is no approval flag here" — posture is spawn-time.
- The bridge never sends any profile: `_do_new()` has no such key
  (`server/sessions.py:859-862`), and the schema has no profile-shaped
  def (§b B1 search).
- Corroborating host reality, from live verification noted in code: a
  serve host **refuses** sessions retaining a profile it cannot compose
  (`-32603 "retained session refused"`), and the bridge degrades rather
  than loads them (`server/sessions.py:28-46`, `1223-1266`).
- Pro: none available. Con: unusable from web-muse without host/TUI
  changes; TUI-created profiled sessions are precisely the ones the
  serve host chokes on.

### B6. Serve sandbox flags (`--disable-shell`, `--disable-write`, `--sandbox-network`, …) — REJECTED for per-clone scoping

- Full list in `muse serve --help` (Sandbox posture section); TUI help
  adds `--yolo` / `--disable-approval` / `--disable-sandbox`, none of
  which exist on `serve`.
- Pro: real execution teeth (shell off, writes off, network
  `restricted|enabled|proxy-only`). Con: **host-lifetime, all sessions
  or none** (`muse serve --help` header); `build_serve_argv()`
  (`server/main.py:126-136`) has no per-session path by construction.
  Running a second locked-down host just for clones (port + router +
  session-store split) is disproportionate — noted only so it isn't
  re-proposed.

### B7. First-turn text / `goal/set` as instruction carriers — FALLBACK, not recommended as the primary

- `turn/start` takes only `input` (text|image|skill parts), `displayText`
  (never model-visible), `reasoningEffort`, `ifBusy`, `workspaceRoots`
  (stable bundle, `$defs/TurnStartParams`) — a first message *is*
  model-visible and in-transcript, so seeding instructions as the first
  turn trivially "works" with zero code. But it is conversation, not
  configuration: it competes with the user's prompt, has no rules
  precedence, and `_do_prompt()` auto-names the session from that text
  (`server/sessions.py:829-832`), so a boilerplate prefix would leak
  into session names.
- `goal/set` ("Sets the session goal objective; wakes a goal-driving
  turn iff idle", stable bundle methods table) with `GoalSetParams:
  {commandId, objective, sessionId}` is session-scoped and
  disk-clean — but the bridge exposes **no** WS `goal/*` message
  (`server/sessions.py:531-812` dispatch has none), and the
  wake-a-turn side effect could start agent work unprompted right after
  clone. Needs a behavior check before use (§d Q4).

## (c) Recommended approach + implementation sketch

Recommend a two-track change, both confined to the existing choke
points — no schema/host changes, no new WS surface strictly required
for v1:

**Track 1 — instructions: seed a bridge-owned file into the clone,
behind `--trust-workspace`.**

1. Author one template, e.g. `server/github_instructions.md`
   (name TBD), with the fixed instruction set (working agreements,
   preferred commands, things to ask before doing).
2. In `_open_cloned()` (`server/sessions.py:1048`), after clone success
   and before `_do_new()`, write the rendered template into the clone
   as `AGENTS.md` **only when absent** (never overwrite a repo's own
   file — untrusted-overwrite runs both directions), or as a
   clearly-bridge-owned sibling (e.g. `.web-muse-instructions.md` —
   verify the host picks up dotfiles/non-`AGENTS.md` names first, §d
   Q2). Record `{seededInstructions: true, path}` in the
   `githubCloneResult` result alongside `fullName/opId`
   (`server/sessions.py:1064-1066` pattern).
3. Enable `--trust-workspace`: add the flag to the serve argv path
   (`server/main.py:48-49, 134-135`) — either default-on with a
   `--no-trust-workspace` opt-out, or on-by-default in the deployed unit
   (`deploy/web-muse.service` `ExecStart=`), then
   `systemctl --user restart web-muse`. Update `README.md:48` flag list
   and the GitHub-sessions section (`README.md:174-189`).
4. Tests (repo convention `test_*.py`, `AGENTS.md`): pure unit tests for
   render-seed-skip-present logic on tmp dirs (same style as
   `tests/test_github.py`, `git`-free fixtures); extend the smoke test
   only if it can assert the seeded file + start without a live host
   dependency. Full suite green (`python3 -m unittest discover -s
   tests -v`).

**Track 2 — command gating: default github-opened sessions to a
restrictive approval mode.**

1. In `_open_cloned()`, set `new_msg["approvalMode"] = "denyUnmatched"`
   (or `promptUnmatched` if deny proves unusable — try both live),
   honoring an explicit per-call override if the WS message carries one.
   `_do_new()` already forwards and validates it
   (`server/sessions.py:859-864`); `allowAll` stays rejected
   (`server/sessions.py:70-77`).
2. Optionally accept `approvalMode` on the `githubOpen` WS message
   (validated against `VALID_APPROVAL_MODES` minus `allowAll`, same as
   `setApprovalMode`, `server/sessions.py:713-726`) so callers can
   relax per repo; UI keeps no control in v1 (fixed default).
3. Document honestly in README: this selects a host-defined enforcement
   posture, **not** a command allowlist — there is deliberately no
   "only these binaries may run" on MSP v1 (B4/B6).

**Explicitly not recommended:** `config.*` injection (silently ignored,
B1), permission profiles (unreachable, B5), a second locked-down serve
host (B6), first-turn prefixing (name pollution, no precedence, B7).

## (d) Open questions

1. **Upstream feature request?** The durable fix for both halves is a
   host change: a `session/start` instructions/pinned-rules member (or
   widening `SessionConfig` beyond `mcpServers`, ADR reference #32760)
   plus a per-session command-policy select. Worth filing against the
   `muse` host, or is workspace-file seeding the accepted long-term
   answer? (No host repo/issue tracker was identified in this pass.)
2. **Seed filename vs git pollution.** Does the trusted-workspace loader
   read only `AGENTS.md` at the root, or also dotfiles/nested paths
   (which would let us seed outside `git status` noise)? Probe with a
   scratch workspace + `skill/list`-style observation under
   `--trust-workspace`; do not test inside real `workspaces/`.
3. **Precedence + injection.** When trust is on and a clone ships its
   own `AGENTS.md`/skills, what wins (bridge-seeded vs repo-authored vs
   user `~/.config` scope), and what is the disclosure story for
   malicious instructions in a cloned repo? This decides
   seed-only-when-absent vs merge-vs-rename, and whether enabling
   `--trust-workspace` globally needs a UI warning.
4. **`goal/set` semantics.** Does a post-start `goal/set` persist
   usefully as session instructions without the wake-a-turn side effect
   firing (idle clone-opened session)? If yes, it is the disk-clean
   alternative to Track 1 — needs one live probe against `muse serve`.
5. **Which approval mode means "sane clone default"?** `denyUnmatched`
   vs `promptUnmatched` behavior against real clone-session workloads
   (shell-heavy Prime/agent loops) is unverified in this pass — pick by
   trial, and record the mapping of what each mode actually gates (host
   docs or experiment), since the schema deliberately does not say.
6. **`githubClone` (no-session) path.** Seeding instructions into a
   sessionless clone is pointless until a session roots there; should
   `new --path <clone>` also seed + default the mode, or is
   `githubOpen` the only governed path? (Note `_do_github_clone`,
   `server/sessions.py:1100-1110`, roots nothing.)

## Sources consulted (all opened directly)

- `server/sessions.py` (full: routing, `_do_new`, `_do_github_open`,
  `_open_cloned`, `_clone_leaf`, `_launch_clone`/`_clone_task`,
  approval gates, resume fallbacks); `server/main.py` (serve argv,
  `--trust-workspace` flag); `server/msp.py:1-20` (pinned schema
  fingerprint); `server/github.py` (gh argv, validation, errors).
- `muse serve --help`, `muse --help` (TUI flags incl.
  `--approval-mode untrusted|on-request|never`, `--permission-profile`,
  `--trust-workspace`), `muse schema --help` + stable **and**
  experimental `generate-json-schema` exports (all `$defs`/method
  claims above are from the 1.4.2 bundles, fingerprint
  `sha256:61afea…b4432ba3568e2` v1), `muse init --help` + live
  `muse init --dry-run`/`--force` in `/tmp/muse-init-probe/probe1`
  (writes only `AGENTS.md`), `muse skills --help`, `muse config --help`,
  `muse mcp --help`, `muse sandbox --help`, `muse plugins --help`.
- `web/app.js` (`githubOpenRepo`/`githubCloneRepo`/`onGithubResult`,
  `parseNewArgs`, `newSession`, drawer list, `cmdGithub`);
  `README.md` (WS table, GitHub section); `AGENTS.md`;
  `docs/decisions/001-in-chat-selection.md`,
  `docs/decisions/002-external-sessions.md`,
  `docs/decisions/003-github-sessions-plan.md`;
  `deploy/web-muse.service`; `tests/` listing + `tests/test_github.py`
  (506 lines, no `config`/`approvalMode` coverage — gap if Track 2
  lands).
