# web-muse: instructions for GitHub-cloned sessions (do not commit this file)

This file was seeded by the web-muse bridge into a fresh clone of
__FULL_NAME__. It is bridge-owned: do not commit it, do not move it,
and if the repo ships its own AGENTS.md, that file still governs the
project — this one only adds how to work with the clone itself.

## Where you are

- This directory IS the repo root of the clone. All `git` and `gh`
  commands below run here.
- The clone is shallow (default branch only). Fetch more history only
  when you need it.

## Branches

- Never commit to the default branch. Start work with
  `git switch -c <short-topic>` and keep one branch per task.
- NEVER merge: opening the pull request is your last step — merging,
  and closing, belong to the human.

## Pull requests

- Push the branch first: `git push -u origin <short-topic>`.
- Then open the PR with `gh pr create --fill` (or `--title`/`--body`
  when the change needs explaining).
- Check review state with `gh pr view --comments`; respond to review
  comments with new commits, never with force-pushes you were not
  asked for.

## Identity

- If a commit needs an author, use the GitHub noreply address
  (`<user>@users.noreply.github.com`); never invent a real email.
- If `gh` asks for credentials, run `gh auth setup-git` once and
  retry — do not paste tokens into files or commands.

## Approvals

- Pre-approved commands run without prompting (the bridge appends the
  exact list below). Anything else raises an approval card a human
  must click — while it waits you are BLOCKED: stop, say what you
  are waiting for, and do not work around the approval.
- When in doubt, prefer the pre-approved commands over asking.
