"""Bridge-owned instruction seeds for session workspaces and clones."""

from __future__ import annotations

import logging
from pathlib import Path

from server.sessions.policy import AUTO_ALLOW_INSPECT, AUTO_ALLOW_TOOLCHAINS, GIT_DENY_SUMMARY

LOG = logging.getLogger("web_muse.sessions")


# -- Seeded instructions (AGENTS.md) --------------------------------------
# Every bridge-owned session workspace gets an AGENTS.md at its root:
# plain sessions get a short orientation + orders pointer, repo sessions
# get the same plus "work in ./<leaf>/". The clone leaf itself gets the
# GitHub instructions below as its own AGENTS.md — but only when the repo
# ships none of its own (the seed never overwrites, so a repo's rules
# always win and both files coexist via the session root).
GITHUB_INSTRUCTIONS_FILENAME = "AGENTS.md"
# Pre-rename name: recognized (never written) so sessions cloned before
# the rename keep their gh auto-approve and instruction detection.
LEGACY_GITHUB_INSTRUCTIONS_FILENAME = "WEB-MUSE.md"
GITHUB_INSTRUCTIONS_MARKER = (
    "# web-muse: instructions for GitHub-cloned sessions "
    "(do not commit this file)")
GITHUB_INSTRUCTIONS_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "github_instructions.md")
SESSION_AGENTS_MARKER = (
    "# web-muse: session workspace (bridge-owned)")


def github_preapproved_section():
    """Explicit pre-approved command list, generated from the matchers.

    Appended to every seeded instruction file so agents see exactly what
    runs without prompting. Built from AUTO_ALLOW_* (never hand-copied),
    so the doc tracks the bridge's real policy.
    """
    inspect = ", ".join(f"`{c}`" for c in AUTO_ALLOW_INSPECT)
    chains = ", ".join(f"`{c}`" for c in AUTO_ALLOW_TOOLCHAINS)
    return (
        "## Pre-approved commands (exact list — no prompt in this session)\n"
        "\n"
        "These run WITHOUT prompting. Anything else raises an approval card\n"
        "a human must click, and while it waits you are BLOCKED — stay\n"
        "inside this list whenever you can.\n"
        "\n"
        "- `gh` — every subcommand.\n"
        "- `git` — every subcommand EXCEPT: " + GIT_DENY_SUMMARY + ".\n"
        "- Shell inspection: " + inspect + ".\n"
        "- Build/test runners: " + chains + ".\n"
        "- Still asks a human: shell file writes (`rm`, `mv`, `cp`,\n"
        "  `find -delete`), network fetchers (`curl`, `wget`, `ssh`), and\n"
        "  `sudo` / interactive / installer commands.\n"
    )


def render_github_instructions(full_name, template=None):
    """Render the instruction template for one repo (pure)."""
    text = template
    if text is None:
        text = GITHUB_INSTRUCTIONS_TEMPLATE.read_text()
    text = text.replace("__FULL_NAME__", full_name)
    return text.rstrip() + "\n\n" + github_preapproved_section()


def render_session_agents(repo_leaf=None, full_name=None):
    """Render the session-root AGENTS.md (pure).

    Plain sessions get orientation + orders pointer; repo sessions also
    name their clone leaf so the agent works in ./<leaf>/ and never at
    the session root. Starts with SESSION_AGENTS_MARKER.
    """
    lines = [SESSION_AGENTS_MARKER, ""]
    if repo_leaf:
        where = (f"This directory is your session workspace. The repo "
                 f"{full_name or repo_leaf} lives in `./{repo_leaf}/` — "
                 f"do all repo work (git, gh, edits, builds, tests) there, "
                 f"never at the session root.")
    else:
        where = ("This directory is your session workspace. Do all work "
                 "here.")
    lines += [where, "",
              "App orders: to change the app itself (theme, bridge policy), "
              "read `.web-muse/ORDERS.md` and send an order file — "
              "that doc is the full protocol.",
              "Skills load from this workspace root; a repo clone's own "
              "AGENTS.md governs inside the clone when one exists."]
    return "\n".join(lines) + "\n"


def seed_session_agents(root, repo_leaf=None, full_name=None):
    """Write the session-root AGENTS.md into one workspace (best-effort).

    An existing file is never overwritten (a repo checkout or an older
    seed always wins). Never raises: seeding must not fail a session
    start. Returns (seeded, path).
    """
    if not root:
        return False, ""
    target = Path(root) / GITHUB_INSTRUCTIONS_FILENAME
    try:
        if target.is_file():
            return False, str(target)
    except OSError:
        return False, str(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_session_agents(repo_leaf, full_name))
    except OSError:
        LOG.warning("session agents file unwritable at %s", target)
        return False, str(target)
    return True, str(target)


def seed_github_instructions(dest, full_name):
    """Write the bridge-owned instruction file into a fresh clone leaf.

    The file is AGENTS.md, seeded only when the repo ships none of its
    own: an existing file (repo rules or an earlier seed) is never
    overwritten, so repo-authored rules always win. Returns (seeded,
    path). Raises OSError on write failure (the caller logs and
    continues — seeding must never fail the open).
    """
    target = Path(dest) / GITHUB_INSTRUCTIONS_FILENAME
    if target.exists():
        return False, str(target)
    target.write_text(render_github_instructions(full_name))
    return True, str(target)


def _read_marker_first_line(path):
    """First line of a file, or "" when unreadable (never raises)."""
    try:
        with open(path, "r", errors="replace") as f:
            return f.readline().rstrip("\n")
    except (OSError, ValueError):
        return ""


def has_seeded_instructions(root):
    """True when root holds a bridge-seeded instruction file.

    Recognizes the session-root AGENTS.md (SESSION_AGENTS_MARKER), the
    clone-leaf AGENTS.md (GITHUB_INSTRUCTIONS_MARKER), and the pre-rename
    WEB-MUSE.md leaf file — so sessions from before the rename keep
    their gh auto-approve. A repo's own AGENTS.md never matches (no
    marker), so it is never mistaken for a seed.
    """
    if not root:
        return False
    try:
        first = _read_marker_first_line(
            Path(root) / GITHUB_INSTRUCTIONS_FILENAME)
    except (OSError, ValueError):
        return False
    if first.startswith((SESSION_AGENTS_MARKER,
                         GITHUB_INSTRUCTIONS_MARKER)):
        return True
    legacy = _read_marker_first_line(
        Path(root) / LEGACY_GITHUB_INSTRUCTIONS_FILENAME)
    return legacy.startswith(GITHUB_INSTRUCTIONS_MARKER)
