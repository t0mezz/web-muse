# web-muse: instructions for GitHub-cloned sessions (do not commit this file)

This file was placed here by the web-muse app when this repo
(__FULL_NAME__) was cloned for an agent session. It tells the agent how
to work in this checkout. It is NOT part of the repo: leave it untracked,
never `git add` or commit it, and do not delete it mid-session.

## Workspace layout

- Your current directory IS the repo root: the app cloned __FULL_NAME__
  into your session workspace and started this session inside it. There
  is no `./repo/...` subdirectory — `./package.json`, `./src`, and
  friends live right here.
- The only non-repo file present is this `AGENTS.md`.

## Branches

- Do all work on a NEW branch cut from the up-to-date default branch:
  `git fetch origin`, then `git switch -c feat/<short-description>`
  (`fix/<short-description>` for bug fixes). Short, lowercase,
  hyphen-separated names.
- NEVER commit directly to the default branch (`main`/`master`).
- NEVER force-push, rewrite published history, or delete branches unless
  the user explicitly asks.

## Pull requests with `gh`

- Push the branch first: `git push -u origin <branch>`.
- Open the PR with:
  `gh pr create --title "<type>: <one-line summary>" --body "<what changed and why; test plan>"`
- Check it landed: `gh pr view <number>` or `gh pr status`.
- NEVER merge, close, edit, or comment on a PR unless the user explicitly
  asks. After opening the PR, report its number and URL and stop.
- Before opening a PR, run the repo's own checks first when they are
  quick (build, lint, tests); name them in the PR body.
- Ensure a commit identity first: if `git config user.name` is empty,
  set it repo-local from the GitHub user (`gh api user --jq .login`)
  with the `users.noreply.github.com` email — never `--global`.
- If `git push` asks for a username (`could not read Username`), git has
  no credential helper: STOP and report it — the owner runs
  `gh auth setup-git` once in a terminal (wires git to the `gh` login),
  then you retry the push. Same for any `gh` failure naming auth.
- In-session network goes through the host sandbox: if `gh` or `git`
  fails on network access (rather than approval), STOP and report it —
  re-auth (`gh auth login` in a terminal) and sandbox policy are the
  owner's job, not retry material.

## Approvals: stay inside the pre-approved list

- The exact list is at the end of this file ("Pre-approved commands").
  Those run WITHOUT prompting — use them freely.
- Anything else raises an approval card a human must click, and while it
  waits you are BLOCKED. No `sudo`, no interactive flags, no installers,
  no force/destructive flags, nothing outside the list unless necessary.
- If a prompt appears anyway, STOP and tell the user exactly which
  command needs approval and why — never retry-loop around it.
