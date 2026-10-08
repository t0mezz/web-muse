"""Shell auto-approve policy: matchers, allowed-commands config, decisions."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

LOG = logging.getLogger("web_muse.sessions")


# Shell allowlist auto-decided for GitHub-opened sessions.
#
# MSP v1 has no per-session command policy on the wire (SessionConfig admits
# only mcpServers; ApprovalMode is select-never-create), so "gh may run"
# cannot be sent to the host. Instead the bridge answers the host's
# approval/request itself via approval/decide before any card reaches the
# UI, so the agent never parks on an unanswered prompt. Anything outside
# this list falls through to the normal UI approval card.
#
# NOTE: `gh` is covered in full, including destructive subcommands
# (`gh repo delete`, `gh release delete`, `gh secret set`, `gh pr merge`,
# raw `gh api` writes, ...). That breadth is explicit user policy for
# GitHub-opened sessions. `git` is covered except the destructive forms
# carved back out in GH_GIT_DENY_RE below.
GH_AUTO_ALLOW_RE = (
    re.compile(r"^gh(\s|$)"),
    re.compile(r"^git(\s|$)"),
)

# Read-only inspection + common build/test toolchains, also pre-approved
# in GitHub-opened sessions so agents can explore repos and run checks
# without stalling. Shell file mutation outside git (rm/mv/cp, shell
# redirection) is deliberately NOT here — the agent's file tools own
# that job — and `find`'s write modes are carved back out below.
# Network fetchers (curl/wget/ssh) are excluded on purpose: anything
# that leaves the machine still asks a human.
#
# The word lists are the single source of truth: the matchers below are
# compiled from them, and the seeded instruction file renders them
# verbatim (see github_preapproved_section), so the doc can never drift
# from what the bridge actually auto-decides.
AUTO_ALLOW_INSPECT = (
    "ls", "cat", "head", "tail", "less", "find", "grep", "rg", "tree",
    "wc", "file", "stat", "diff", "jq", "pwd", "echo", "printf",
)
AUTO_ALLOW_TOOLCHAINS = (
    "node", "npm", "npx", "python3", "pip", "pip3", "pytest", "uv",
    "uvx", "cargo", "rustc", "go", "make", "tsc",
)
GH_AUTO_ALLOW_TOOLS_RE = (
    re.compile(r"^(?:%s)(\s|$)" % "|".join(AUTO_ALLOW_INSPECT)),
    re.compile(r"^(?:%s)(\s|$)" % "|".join(AUTO_ALLOW_TOOLCHAINS)),
)

# Everyday-git carve-outs, mirrored in the instruction text below.
GIT_DENY_SUMMARY = (
    "`reset --hard`, `clean -f`/`--force`, `push --force`/`-f`/`--delete`, "
    "`branch -D`, `stash drop`/`clear`"
)

# `find` stays inspection-only: -delete/-exec* would smuggle writes and
# process execution past the allowlist above.
GH_TOOL_DENY_RE = (
    re.compile(r"\bfind\b.*\s(-delete|-exec|-execdir)(\s|$)"),
)

# Destructive git forms: discarded work is unrecoverable, so these keep
# prompting a human even in GitHub-opened sessions. Matched with search
# (flags can sit anywhere in the argv).
GH_GIT_DENY_RE = (
    re.compile(r"(^|\s)--force(\s|$)"),    # push --force, clean --force
    re.compile(r"(^|\s)--hard(\s|$)"),     # reset --hard
    re.compile(r"(^|\s)-f(\s|$)"),         # push -f
    re.compile(r"\bclean\s+-[a-zA-Z]*f"),  # clean -fd / -fx / ...
    re.compile(r"\bpush\b.*\s--delete(\s|$)"),  # push --delete
    re.compile(r"\bbranch\s+-[a-zA-Z]*D\b"),   # branch -D
    re.compile(r"\bstash\s+(drop|clear)\b"),   # stash drop / clear
)


# Protected allowed-commands config: server/allowed_commands.json holds the
# same allow/deny semantics as the GH_* matchers above, but as data the
# bridge validates on load. Protection is threefold: the file lives under
# server/ (never under the served web/ dir, so no WS route can reach it),
# every value is type/shape/regex-checked with a builtin fallback, and a
# corrupt file degrades to prompting (never to wider auto-approval).
# Nothing here — rows aside — ever crosses the WS boundary to the browser.
ALLOWED_COMMANDS_FILE = str(
    Path(__file__).resolve().parent.parent / "allowed_commands.json")
MAX_ALLOWED_PATTERNS = 200
MAX_PATTERN_LEN = 500
SAFE_COMMAND_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _builtin_allowed_config():
    """Compiled GH_* matchers as a config dict (fallback + parity base)."""
    return {
        "allow": list(GH_AUTO_ALLOW_RE) + list(GH_AUTO_ALLOW_TOOLS_RE),
        "deny": [(("find",), list(GH_TOOL_DENY_RE)),
                 (("git",), list(GH_GIT_DENY_RE))],
    }


def _compile_patterns(patterns, warnings, what):
    """Compile a pattern list, dropping bad entries with a warning each."""
    compiled = []
    for pat in patterns or []:
        if (not isinstance(pat, str) or not pat
                or len(pat) > MAX_PATTERN_LEN):
            warnings.append(f"allowed-commands: dropping invalid {what} "
                            f"entry {pat!r}")
            continue
        if len(compiled) >= MAX_ALLOWED_PATTERNS:
            warnings.append(f"allowed-commands: too many {what} entries; "
                            "keeping the first "
                            f"{MAX_ALLOWED_PATTERNS}")
            break
        try:
            compiled.append(re.compile(pat))
        except re.error as e:
            warnings.append(f"allowed-commands: dropping bad {what} "
                            f"regex {pat!r} ({e})")
    return compiled


def load_allowed_commands(path=None):
    """Load and validate the allowed-commands config (never raises).

    Returns (cfg, warnings): cfg is {"allow": [re], "deny":
    [(commands, [re])]}. A missing/unparseable file, or an unusable side
    of it, falls back to the builtin GH_* matchers with a warning — so a
    corrupt file degrades to the audited default, never to silent
    allow-everything. An explicit empty allow list is honored (nothing
    auto-approved).
    """
    builtin = _builtin_allowed_config()
    p = path if path is not None else ALLOWED_COMMANDS_FILE
    try:
        raw = json.loads(Path(p).read_text())
    except FileNotFoundError:
        return builtin, [f"allowed-commands: no file at {p}; "
                         "using builtin matchers"]
    except (OSError, ValueError) as e:
        LOG.warning("allowed-commands: unreadable %s (%s); using builtin "
                    "matchers", p, e)
        return builtin, [f"allowed-commands: unreadable file ({e}); "
                         "using builtin matchers"]
    if not isinstance(raw, dict):
        return builtin, ["allowed-commands: top level must be an object; "
                         "using builtin matchers"]
    warnings = []
    cfg = {}
    if "allow" not in raw:
        cfg["allow"] = builtin["allow"]
    elif not isinstance(raw["allow"], list):
        warnings.append("allowed-commands: 'allow' must be a list; "
                        "using builtin allow matchers")
        cfg["allow"] = builtin["allow"]
    else:
        cfg["allow"] = _compile_patterns(raw["allow"], warnings, "allow")
        if raw["allow"] and not cfg["allow"]:
            warnings.append("allowed-commands: no usable 'allow' entries; "
                            "using builtin allow matchers")
            cfg["allow"] = builtin["allow"]
    if "deny" not in raw:
        cfg["deny"] = builtin["deny"]
    elif not isinstance(raw["deny"], list):
        warnings.append("allowed-commands: 'deny' must be a list; "
                        "using builtin deny rules")
        cfg["deny"] = builtin["deny"]
    else:
        rules = []
        for entry in raw["deny"]:
            if not isinstance(entry, dict) or not isinstance(
                    entry.get("patterns"), list):
                warnings.append("allowed-commands: dropping malformed "
                                f"deny entry {entry!r}")
                continue
            commands = entry.get("commands") or []
            if not isinstance(commands, list) or not all(
                    isinstance(c, str) and SAFE_COMMAND_NAME.fullmatch(c)
                    for c in commands):
                warnings.append("allowed-commands: dropping deny entry "
                                f"with bad 'commands' {entry!r}")
                continue
            patterns = _compile_patterns(entry["patterns"], warnings,
                                         "deny")
            if entry["patterns"] and not patterns:
                warnings.append("allowed-commands: deny entry has no "
                                f"usable patterns {entry!r}")
                continue
            rules.append((tuple(commands), patterns))
        cfg["deny"] = rules
    for w in warnings:
        LOG.warning("%s", w)
    return cfg, warnings


_DEFAULT_ALLOWED = None


def default_allowed_config():
    """Process-wide allowed-commands config (shipped file or builtins)."""
    global _DEFAULT_ALLOWED
    if _DEFAULT_ALLOWED is None:
        _DEFAULT_ALLOWED, _ = load_allowed_commands()
    return _DEFAULT_ALLOWED


def shell_allowed(command, cfg):
    """True when a shell command string passes an allowed-commands config.

    Pure: allow entries match from the command start; deny rules search
    anywhere but only fire for their listed leading commands (an empty
    command list scopes a rule to every allowed command).
    """
    cmd = command.strip() if isinstance(command, str) else ""
    if not cmd:
        return False
    if not any(pat.match(cmd) for pat in cfg.get("allow", ())):
        return False
    first = cmd.split()[0]
    for commands, patterns in cfg.get("deny", ()):
        if commands and first not in commands:
            continue
        if any(pat.search(cmd) for pat in patterns):
            return False
    return True


def shell_auto_allowed(subject, cfg=None):
    """True when a shell approval subject is auto-decided bridge-side."""
    if not isinstance(subject, dict) or subject.get("kind") != "shell":
        return False
    cmd = subject.get("command")
    if not isinstance(cmd, str):
        return False
    return shell_allowed(
        cmd, default_allowed_config() if cfg is None else cfg)
