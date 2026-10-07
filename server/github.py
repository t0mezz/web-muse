"""GitHub sessions (stage 2, gh-only v1): list and clone repos via the `gh` CLI.

Every GitHub touchpoint shells out to `gh` (argv-only, never via a shell),
so the bridge never holds a token: `gh` owns auth storage entirely (see
`gh auth login`; the token lives in `~/.config/gh/hosts.yml`, mode 0600).
Nothing GitHub-shaped — repo names aside — ever crosses the WS boundary
to the browser, and `gh` stderr is redacted before it reaches any frame
or log line.

Conventions (matching `server/sessions.py`):
- `validate_fullname` rejects anything outside `owner/repo` charset before
  it can reach `exec` — no shell, no remapping, no silent fixes.
- Failures raise `GithubError`, which carries a machine-readable `code`
  the UI switches on (`gh_missing`, `gh_unauth`, `invalid_repo`,
  `clone_failed`, `list_failed`, `dest_exists`, `lax_hosts_perms`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

LOG = logging.getLogger("web_muse.github")

GH_BIN = "gh"

# owner/repo, conservative charset (matches GitHub login + repo slug rules
# closely enough to block traversal/flag-injection without allowlisting).
# Dot-segments ("..") and leading dots/dashes are rejected separately below:
# the name never becomes a path here, but ".." and "-x" still have no place
# in a repo slug, and "-x/y" could parse as a flag where the argv lands.
FULLNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")

LIST_FIELDS = "nameWithOwner,isPrivate,defaultBranchRef,updatedAt"

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

# Token-shaped secrets that must never reach a frame or log line, even
# inside a pasted `gh` stderr tail.
_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]+"),
    re.compile(r"https://[^@\s]+@"),
)


class GithubError(Exception):
    """Bridge-side GitHub failure with a UI-switchable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_fullname(full_name):
    """Check `owner/repo`; return it unchanged or raise ValueError."""
    name = full_name if isinstance(full_name, str) else ""
    name = name.strip()
    parts = name.split("/")
    if (not FULLNAME_RE.fullmatch(name) or len(parts) != 2
            or any(p in (".", "..") or p[:1] in (".", "-") for p in parts)):
        raise ValueError(
            f"invalid repo {full_name!r}: want 'owner/repo' "
            "(letters, digits, dot, dash, underscore; "
            "no dot-segments or leading dots/dashes)")
    return name


def normalize_limit(limit):
    """Clamp a client-supplied list limit to 1..MAX (default DEFAULT)."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIST_LIMIT
    return max(1, min(MAX_LIST_LIMIT, n))


def build_list_argv(limit=DEFAULT_LIST_LIMIT, gh_bin=GH_BIN):
    """`gh repo list` argv: JSON rows, newest activity first."""
    return [gh_bin, "repo", "list", "--json", LIST_FIELDS,
            "--limit", str(normalize_limit(limit))]


def parse_repo_rows(payload):
    """Parsed `gh --json` list -> UI rows (pure; never raises on shape).

    Input: list of {nameWithOwner, isPrivate, defaultBranchRef, updatedAt}.
    Output: [{name, fullName, private, defaultBranch, updatedAt}].
    """
    rows = []
    if not isinstance(payload, list):
        return rows
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        full = entry.get("nameWithOwner") or ""
        if not isinstance(full, str) or "/" not in full:
            continue
        branch_ref = entry.get("defaultBranchRef") or {}
        branch = branch_ref.get("name") if isinstance(branch_ref, dict) else None
        rows.append({
            "name": full.split("/")[-1],
            "fullName": full,
            "private": bool(entry.get("isPrivate")),
            "defaultBranch": branch,
            "updatedAt": entry.get("updatedAt"),
        })
    return rows


def filter_repos(rows, search):
    """Bridge-side prefix filter on fullName (case-insensitive)."""
    q = (search or "").strip().lower()
    if not q:
        return list(rows)
    return [r for r in rows
            if isinstance(r.get("fullName"), str)
            and r["fullName"].lower().startswith(q)]


def validate_branch(name):
    """Check a git branch name; return it unchanged or raise ValueError.

    Conservative charset (letters, digits, dot, dash, underscore, slash)
    with no `..` segments, empty segments, or leading dots/dashes — the
    value travels as a `git clone --branch` argv element (never a shell),
    but `-x` still has no place as a branch and `..` never does.
    """
    branch = name if isinstance(name, str) else ""
    branch = branch.strip()
    parts = branch.split("/")
    if (not branch or len(branch) > 255
            or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", branch)
            or branch.startswith(("-", ".", "/")) or branch.endswith("/")
            or "//" in branch or ".." in branch
            or any(p[:1] in (".", "-") or p.endswith(".lock")
                   for p in parts)):
        raise ValueError(
            f"invalid branch {name!r}: letters, digits, dot, dash, "
            "underscore, slash; no dot-segments or leading dots/dashes")
    return branch


LEAF_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")

# Fallback leaf name when a repo slug is somehow not dir-safe (validated
# fullNames always yield a safe slug; this is defense in depth so session
# layout never breaks on an odd name).
FALLBACK_LEAF_NAME = "repo"


def repo_dir_name(full_name):
    """Clone-leaf dir name for one `owner/repo` fullname (pure).

    Returns the repo slug as-is (validated slugs are dir-safe), or
    FALLBACK_LEAF_NAME when the slug is missing or not dir-safe
    (leading dots/dashes, dot-segments, bad charset, wrong length).
    Never raises.
    """
    try:
        slug = (full_name or "").strip().split("/")[-1]
    except (AttributeError, IndexError):
        return FALLBACK_LEAF_NAME
    if (not LEAF_NAME_RE.fullmatch(slug) or slug[:1] in (".", "-")
            or slug in (".", "..")):
        return FALLBACK_LEAF_NAME
    return slug


def is_safe_leaf_name(name):
    """True when a clone-leaf dir name is safe to create/remove (pure)."""
    return (isinstance(name, str) and bool(LEAF_NAME_RE.fullmatch(name))
            and name[:1] not in (".", "-") and name not in (".", ".."))


def build_clone_argv(full_name, dest, gh_bin=GH_BIN, branch=None):
    """`gh repo clone` argv (shallow); fullname must already be validated.

    A validated branch pins the clone to that branch (`git --branch`);
    None clones the repo default.
    """
    argv = [gh_bin, "repo", "clone", full_name, str(dest),
            "--", "--depth", "1"]
    if branch is not None:
        argv += ["--branch", validate_branch(branch)]
    return argv


def build_branches_argv(full_name, gh_bin=GH_BIN):
    """`gh api` argv listing one repo's branches (first page, 100 max)."""
    return [gh_bin, "api", f"repos/{full_name}/branches?per_page=100"]


def parse_branch_names(payload):
    """Parsed `gh api branches` payload -> [name] (pure; never raises).

    Input: list of {name, ...}. Output: branch names in listed order.
    """
    names = []
    if not isinstance(payload, list):
        return names
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def merge_repo_rows(cached, fresh):
    """Union of cached + fresh repo rows, keyed by fullName (pure).

    Fresh rows win on conflicts and keep `gh` order first; cached-only
    rows trail in their existing order. Lets the picker accumulate
    known repos across `--limit` pages while every listing still
    checks `gh` for new ones.
    """
    seen = {}
    for row in cached or []:
        if isinstance(row, dict) and isinstance(row.get("fullName"), str):
            seen.setdefault(row["fullName"], row)
    merged = []
    for row in fresh or []:
        if not (isinstance(row, dict)
                and isinstance(row.get("fullName"), str)):
            continue
        seen[row["fullName"]] = row
        merged.append(row)
    merged += [row for name, row in seen.items()
               if name not in {r.get("fullName") for r in merged
                               if isinstance(r, dict)}]
    return merged


def merge_branch_names(cached, fresh):
    """Union of cached + fresh branch names (pure).

    Fresh order first, cached-only names appended. New branches from
    `gh` always appear; branches seen before survive a sparse listing.
    """
    merged = [n for n in (fresh or [])
              if isinstance(n, str) and n]
    merged += [n for n in (cached or [])
               if isinstance(n, str) and n and n not in merged]
    return merged


def redact(text):
    """Strip token-shaped secrets from `gh` output before framing/logging."""
    out = text if isinstance(text, str) else str(text)
    for pat in _TOKEN_PATTERNS:
        out = pat.sub("https://***@" if pat.pattern.startswith("https")
                      else "***", out)
    return out


def map_gh_error(stderr, returncode, op):
    """Redacted `gh` stderr -> GithubError with a UI-switchable code."""
    lines = stderr.strip().splitlines()[-5:] if isinstance(stderr, str) else []
    tail = redact("\n".join(lines))
    low = tail.lower()
    if ("not logged in" in low or "gh auth login" in low
            or "authentication required" in low or "could not authenticate" in low):
        return GithubError("gh_unauth",
                           "gh is not logged in (run `gh auth login` "
                           "in a terminal)" + (f": {tail}" if tail else ""))
    if ("could not resolve to a repository" in low or " 404 " in low
            or "not found" in low):
        return GithubError("invalid_repo",
                           f"repo not found or no access{': ' + tail if tail else ''}")
    if "already exists" in low:
        return GithubError("dest_exists", tail or "destination already exists")
    verb = {"list": "list", "branches": "branches"}.get(op, "clone")
    return GithubError(f"{verb}_failed",
                       f"gh {verb} failed (exit {returncode})"
                       + (f": {tail}" if tail else ""))


def hosts_file_ok(path=None):
    """Check `gh`'s token file exists with owner-only perms.

    Returns (ok, hint): ok False means GitHub ops must refuse to run.
    """
    p = Path(path) if path else (
        Path(os.path.expanduser("~")) / ".config" / "gh" / "hosts.yml")
    try:
        mode = p.stat().st_mode
    except OSError:
        return False, (f"no gh auth file at {p} "
                       "(run `gh auth login` in a terminal)")
    if mode & 0o077:
        return False, (f"refusing: {p} is group/world-accessible "
                       f"(mode {oct(mode & 0o777)}); want 0600")
    return True, ""


async def run_gh_list(search=None, limit=DEFAULT_LIST_LIMIT, gh_bin=GH_BIN,
                      timeout=30):
    """Run `gh repo list` and return filtered UI rows (raises GithubError)."""
    ok, hint = hosts_file_ok()
    if not ok:
        low = hint.lower()
        code = ("gh_unauth" if "auth login" in low else "lax_hosts_perms"
                if "refusing" in low else "gh_unauth")
        raise GithubError(code, hint)
    argv = build_list_argv(limit, gh_bin)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise GithubError("gh_missing",
                          "gh is not installed (install it, then "
                          "`gh auth login`)")
    except OSError as e:
        raise GithubError("list_failed", f"cannot run gh: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GithubError("list_failed", "gh repo list timed out")
    if proc.returncode != 0:
        raise map_gh_error(err.decode("utf-8", "replace"), proc.returncode,
                           "list")
    try:
        payload = json.loads(out.decode("utf-8"))
    except ValueError as e:
        raise GithubError("list_failed", f"gh returned bad JSON: {e}")
    return filter_repos(parse_repo_rows(payload), search)


async def run_gh_branches(full_name, gh_bin=GH_BIN, timeout=30):
    """List one repo's branch names via `gh api` (raises GithubError)."""
    full_name = validate_fullname(full_name)
    ok, hint = hosts_file_ok()
    if not ok:
        low = hint.lower()
        code = ("gh_unauth" if "auth login" in low else "lax_hosts_perms"
                if "refusing" in low else "gh_unauth")
        raise GithubError(code, hint)
    argv = build_branches_argv(full_name, gh_bin)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise GithubError("gh_missing",
                          "gh is not installed (install it, then "
                          "`gh auth login`)")
    except OSError as e:
        raise GithubError("branches_failed", f"cannot run gh: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GithubError("branches_failed", "gh api branches timed out")
    if proc.returncode != 0:
        raise map_gh_error(err.decode("utf-8", "replace"), proc.returncode,
                           "branches")
    try:
        payload = json.loads(out.decode("utf-8"))
    except ValueError as e:
        raise GithubError("branches_failed", f"gh returned bad JSON: {e}")
    return parse_branch_names(payload)


async def run_gh_clone(full_name, dest, gh_bin=GH_BIN, on_line=None,
                       branch=None):
    """Run `gh repo clone` (shallow); stream redacted stderr lines.

    on_line(line) is called per stderr line (already redacted). Cancellation
    of the awaiting task kills the child. Returns dest on success, else
    raises GithubError. A half-clone left by a failed attempt is removed so
    a retry starts clean. A validated branch pins the clone to that branch.
    """
    ok, hint = hosts_file_ok()
    if not ok:
        low = hint.lower()
        code = ("gh_unauth" if "auth login" in low else "lax_hosts_perms"
                if "refusing" in low else "gh_unauth")
        raise GithubError(code, hint)
    argv = build_clone_argv(full_name, dest, gh_bin, branch=branch)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise GithubError("gh_missing",
                          "gh is not installed (install it, then "
                          "`gh auth login`)")
    except OSError as e:
        raise GithubError("clone_failed", f"cannot run gh: {e}")
    tail = []
    try:
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = redact(raw.decode("utf-8", "replace").rstrip())
            tail.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:  # progress must never fail the clone
                    LOG.warning("clone progress callback failed", exc_info=True)
        await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    if proc.returncode != 0:
        _remove_half_clone(dest)
        raise map_gh_error("\n".join(tail[-5:]), proc.returncode, "clone")
    LOG.info("cloned %s -> %s", full_name, dest)
    return dest


def _remove_half_clone(dest):
    """Best-effort removal of a failed clone attempt (never raises)."""
    import shutil
    try:
        target = Path(dest)
        # Only touch paths that look like our clone leaf (a safe dir
        # name — the repo slug, or the legacy "repo").
        if is_safe_leaf_name(target.name) and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    except Exception:
        LOG.warning("half-clone cleanup failed for %s", dest, exc_info=True)
