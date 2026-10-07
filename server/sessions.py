"""Session routing: WS connections <-> MSP sessionIds, cursor store, frame mapping.

Pure mapping helpers (build_* / map_*) are unit-tested in
tests/test_msp_mapping.py against recorded fixtures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from server.github import GithubError, filter_repos, is_safe_leaf_name, merge_branch_names, merge_repo_rows, repo_dir_name, run_gh_branches, run_gh_clone, run_gh_list, validate_branch, validate_fullname
from server.msp import MspError, uuid7

LOG = logging.getLogger("web_muse.sessions")

# JSON-RPC "method not found": a --no-session-log (memory-only) host serves
# no view-store methods — session/resume, session/read, view/subscribe and
# view/page all answer -32601 there (verified live). Durable hosts serve
# them. The bridge falls back below instead of failing the UI.
METHOD_NOT_FOUND = -32601

# A serve host cannot load sessions whose retained permission profile it
# cannot compose (verified live: TUI-created `:auto-review` sessions are
# refused with -32603 "retained session refused", while serve-created
# sessions resume fine; the TUI resumes in-process where the reviewer
# exists). This is a host capability gap, not a missing method, so the
# bridge degrades honestly (metadata + live attach) instead of failing
# the switch — same philosophy as the --no-session-log fallback below.
RETAINED_REFUSED_CODE = -32603
RETAINED_REFUSED_MARKER = "retained session refused"


def _retained_refused(e):
    """True when the host refuses to load a stored session (-32603).

    Matched on code plus the host's marker text so unrelated internal
    errors still surface instead of degrading silently.
    """
    return (getattr(e, "code", None) == RETAINED_REFUSED_CODE
            and RETAINED_REFUSED_MARKER in str(e).lower())


# MSP methods that are commands: the bridge must NOT accept a client commandId,
# it always mints a fresh UUIDv7 (see MspClient.command).
COMMAND_METHODS = {
    "session/start": "session/start",
    "session/resume": "session/resume",
    "session/fork": "session/fork",
    "session/rename": "session/rename",
    "session/delete": "session/delete",
    "session/setModel": "session/setModel",
    "session/setApprovalMode": "session/setApprovalMode",
    "session/setReasoningEffort": "session/setReasoningEffort",
    "session/compact": "session/compact",
    "turn/start": "turn/start",
    "turn/interrupt": "turn/interrupt",
    "turn/cancel": "turn/cancel",
    "turn/steer": "turn/steer",
    "turn/unqueue": "turn/unqueue",
    "approval/decide": "approval/decide",
    "userInput/answer": "userInput/answer",
}

# MSP ApprovalMode closed enum (schema $defs/ApprovalMode). All four are
# selectable, including allowAll ("approve all" in the Session panel):
# with it the host runs every command without prompting, so it carries
# an explicit warning in the UI and should be used only in throwaway
# sessions. (The host default onRequest applies unless changed here or
# in the TUI.)
VALID_APPROVAL_MODES = {"allowAll", "promptUnmatched", "onRequest",
                        "denyUnmatched"}

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
    Path(__file__).resolve().parent / "allowed_commands.json")
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


# Agent orders channel: agents cannot write outside their workspace, so
# app customization arrives as `.web-muse/orders.json` files (protocol in
# server/orders_skill.md, seeded into every session workspace). The bridge
# checks the file after each turn, validates every order, and acts:
# `theme.apply` executes at once (fanned out as a themeApply event the
# frontend applies); `theme.save`, `allowedCommands.update` and
# `bridge.restart` only stage — a human's ordersDecide runs them, so
# agents never loosen their own policy or bounce the backend alone.
# Everything else is rejected with a reason in the receipt file.
ORDERS_DIRNAME = ".web-muse"
ORDERS_FILENAME = "orders.json"
ORDERS_RECEIPT = "orders.receipt.json"
ORDERS_SKILL_FILE = "ORDERS.md"
ORDERS_MAX_BYTES = 65536
ORDERS_MAX_COUNT = 20
ORDERS_RECEIPT_CAP = 200
ORDERS_TEMPLATE = (
    Path(__file__).resolve().parent / "orders_skill.md")
# Saved themes live under <repo>/web/themes; an approved theme.save order
# writes one file there so it appears under bare `/theme`.
DEFAULT_THEMES_DIR = (
    Path(__file__).resolve().parent.parent / "web" / "themes")

ORDER_ACTIONS = ("theme.apply", "theme.save", "allowedCommands.update",
                 "bridge.restart")

# Agent-requested backend restarts (bridge.restart): an agent can only
# *propose* one — a human's ordersDecide runs it, because restarting
# drops every browser connection and interrupts running turns. Params
# are optional: {"reason": "...", "delaySeconds": 2}.
RESTART_REASON_MAX = 500
RESTART_DELAY_DEFAULT = 2
RESTART_DELAY_MIN = 0
RESTART_DELAY_MAX = 30

# Theme value formats, per key shape in web/theme.js (pinned by
# tests/test_orders.py): hex roles take #rgb / #rrggbb / #rrggbbaa,
# `glow` takes an "r, g, b" triplet (0-255 each, applied verbatim into
# CSS), and `scrim` takes an rgba(...) color. Anything else is rejected
# with the expected shape in the reason so the agent can fix and resend.
HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")
GLOW_RE = re.compile(
    r"\s*(?:[01]?\d{1,2}|2[0-4]\d|25[0-5])"
    r"\s*,\s*(?:[01]?\d{1,2}|2[0-4]\d|25[0-5])"
    r"\s*,\s*(?:[01]?\d{1,2}|2[0-4]\d|25[0-5])\s*\Z")
SCRIM_RE = re.compile(r"rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,"
                      r"\s*(?:0|1|0?\.\d+)\s*\)\Z")


def theme_color_error(key, value):
    """Reason string when a theme value has the wrong shape; None when ok.

    Pure: `glow`/`scrim` are non-hex roles (see web/theme.js), everything
    else is a hex role.
    """
    if key == "glow":
        if isinstance(value, str) and GLOW_RE.match(value):
            return None
        return (f"bad color value for 'glow': want an \"r, g, b\" triplet "
                f"(0-255 each), e.g. \"174, 172, 120\"; got {value!r}")
    if key == "scrim":
        if isinstance(value, str) and SCRIM_RE.match(value):
            return None
        return (f"bad color value for 'scrim': want rgba(r, g, b, a), e.g. "
                f"\"rgba(241, 230, 209, 0.4)\"; got {value!r}")
    if isinstance(value, str) and HEX_COLOR_RE.match(value):
        return None
    return (f"bad color value for {key!r}: want #rgb, #rrggbb or "
            f"#rrggbbaa, e.g. \"#AEAC78\"; got {value!r}")


# Saved-theme names the bridge writes (web/themes/<name>.json via an
# approved theme.save order): lowercase stems so they are safe path
# segments and stable under the /theme command's case-insensitive match.
THEME_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")

# Theme color names the bridge accepts (must match web/theme.js COLORS;
# tests/test_orders.py pins parity both ways).
THEME_KEYS = frozenset({
    "bg", "panel", "panel2", "line", "fg", "dim", "faint",
    "accent", "ok", "warn", "err", "user", "agent",
    "select", "warnBg", "warnFg", "errFg", "codeBg", "cardBg",
    "pickedBg", "onOk", "onAccent", "chipInk", "light",
    "glow", "scrim", "star", "starBg0", "starBg1",
})


def seed_orders_skill(root):
    """Write the orders skill doc into one workspace (best-effort).

    Every session workspace gets `.web-muse/ORDERS.md` once; an existing
    file is never overwritten. Never raises: seeding must not fail a
    session start. Returns (seeded, path).
    """
    if not root:
        return False, ""
    target = Path(root) / ORDERS_DIRNAME / ORDERS_SKILL_FILE
    try:
        if target.is_file():
            return False, str(target)
        text = ORDERS_TEMPLATE.read_text()
    except OSError:
        return False, str(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    except OSError:
        LOG.warning("orders skill unwritable at %s", target)
        return False, str(target)
    return True, str(target)


def _read_receipt(odir):
    """Processed-id map from the receipt file; {} when absent/broken."""
    try:
        raw = json.loads((Path(odir) / ORDERS_RECEIPT).read_text())
    except (OSError, ValueError):
        return {}
    processed = raw.get("processed") if isinstance(raw, dict) else None
    if not isinstance(processed, dict):
        return {}
    return {k: v for k, v in processed.items()
            if isinstance(k, str) and isinstance(v, dict)}


def _write_receipt(odir, processed):
    """Merge new statuses into the receipt file (best-effort, capped)."""
    merged = dict(_read_receipt(odir))
    merged.update(processed)
    while len(merged) > ORDERS_RECEIPT_CAP:
        merged.pop(next(iter(merged)))
    try:
        Path(odir).mkdir(parents=True, exist_ok=True)
        (Path(odir) / ORDERS_RECEIPT).write_text(
            json.dumps({"processed": merged}, indent=2))
    except OSError:
        LOG.warning("orders receipt unwritable in %s", odir)


def _validate_allowed_update(raw):
    """Strict-validate an allowedCommands.update payload.

    Same shapes as the config file, but any invalid entry rejects the
    whole order (file loading instead drops entries with warnings).
    Returns ({"allow": [...], "deny": [...]}, None) or (None, reason).
    """
    if not isinstance(raw, dict):
        return None, "params must be an object with allow and/or deny"
    if "allow" not in raw and "deny" not in raw:
        return None, "params needs allow and/or deny"
    out = {}
    if "allow" in raw:
        if not isinstance(raw["allow"], list):
            return None, "'allow' must be a list of regex strings"
        w = []
        compiled = _compile_patterns(raw["allow"], w, "allow")
        if raw["allow"] and (w or not compiled):
            return None, f"unusable 'allow' entries: {'; '.join(w)}"
        out["allow"] = [p for p in raw["allow"]
                        if isinstance(p, str) and p
                        and len(p) <= MAX_PATTERN_LEN]
    if "deny" not in raw:
        return out, None
    if not isinstance(raw["deny"], list):
        return None, "'deny' must be a list of {commands, patterns}"
    rules = []
    for entry in raw["deny"]:
        if not isinstance(entry, dict) \
                or not isinstance(entry.get("patterns"), list):
            return None, f"malformed deny entry {entry!r}"
        commands = entry.get("commands") or []
        if not isinstance(commands, list) or not all(
                isinstance(c, str) and SAFE_COMMAND_NAME.fullmatch(c)
                for c in commands):
            return None, f"bad 'commands' in deny entry {entry!r}"
        w = []
        compiled = _compile_patterns(entry["patterns"], w, "deny")
        if entry["patterns"] and (w or not compiled):
            return None, (f"unusable patterns in deny entry {entry!r}: "
                           f"{'; '.join(w)}")
        rules.append({"commands": list(commands),
                      "patterns": [p for p in entry["patterns"]
                                   if isinstance(p, str) and p
                                   and len(p) <= MAX_PATTERN_LEN]})
    out["deny"] = rules
    return out, None


def _validate_restart(raw):
    """Strict-validate a bridge.restart payload.

    Both params are optional: `reason` (short human-readable why,
    shown on the approval card) and `delaySeconds` (grace between the
    human's approval reply and the actual restart, so the reply and the
    bridgeRestarting event flush first). Returns (params, None) or
    (None, reason).
    """
    if not isinstance(raw, dict):
        return None, "params must be an object"
    out = {}
    reason = raw.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        return None, "'reason' must be a string"
    if len(reason) > RESTART_REASON_MAX:
        return None, (f"'reason' must be at most {RESTART_REASON_MAX} "
                       f"chars")
    out["reason"] = reason
    delay = raw.get("delaySeconds", RESTART_DELAY_DEFAULT)
    if isinstance(delay, bool) or not isinstance(delay, int):
        return None, ("'delaySeconds' must be an integer "
                      f"{RESTART_DELAY_MIN}-{RESTART_DELAY_MAX}")
    if not RESTART_DELAY_MIN <= delay <= RESTART_DELAY_MAX:
        return None, ("'delaySeconds' must be between "
                      f"{RESTART_DELAY_MIN} and {RESTART_DELAY_MAX}")
    out["delaySeconds"] = delay
    return out, None


HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3}([0-9a-fA-F]{2})?)?")


def _rgb_triplet(value):
    """Parse an "r, g, b" triplet (each part 0-255) into [r, g, b], else None."""
    if not isinstance(value, str):
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 3:
        return None
    nums = []
    for p in parts:
        if re.fullmatch(r"\d{1,3}", p) is None:
            return None
        n = int(p)
        if n > 255:
            return None
        nums.append(n)
    return nums


def _valid_theme_value(key, value):
    """True when a theme.apply value has the shape its role needs.

    Every role takes #rgb, #rrggbb or #rrggbbaa, except glow (an
    "r, g, b" triplet with each part 0-255) and scrim (an rgba(...)
    color). Mirrors the shapes documented in server/orders_skill.md.
    """
    if not isinstance(value, str) or not value \
            or len(value) > MAX_PATTERN_LEN:
        return False
    if key == "glow":
        return _rgb_triplet(value) is not None
    if key == "scrim":
        m = re.fullmatch(r"rgba\((.*)\)", value.strip(), re.S)
        if not m:
            return False
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) != 4 \
                or _rgb_triplet(",".join(parts[:3])) is None:
            return False
        try:
            alpha = float(parts[3])
        except ValueError:
            return False
        return 0.0 <= alpha <= 1.0
    return HEX_COLOR.fullmatch(value) is not None


def validate_orders(payload, seen_ids):
    """Split an orders payload into (valid, rejected).

    valid: [(id, action, params)]; rejected: {id: reason}. Unknown
    actions, bad shapes, duplicate ids (in-file or already receipted),
    and invalid action params are all rejected, never raised.
    """
    valid, rejected = [], {}
    if not isinstance(payload, dict) or not isinstance(
            payload.get("orders"), list):
        return valid, rejected
    seen = set(seen_ids or ())
    for entry in payload["orders"]:
        if not isinstance(entry, dict):
            continue
        oid = entry.get("id")
        if not isinstance(oid, str) or not oid or len(oid) > 128:
            continue
        if oid in seen:
            rejected[oid] = "duplicate order id (already processed)"
            continue
        seen.add(oid)
        action = entry.get("action")
        params = entry.get("params") or {}
        if action not in ORDER_ACTIONS:
            rejected[oid] = (f"unknown action {action!r}; want one of "
                             f"{sorted(ORDER_ACTIONS)}")
            continue
        if not isinstance(params, dict):
            rejected[oid] = "params must be an object"
            continue
        if action in ("theme.apply", "theme.save"):
            colors = params.get("colors")
            if not isinstance(colors, dict) or not colors:
                rejected[oid] = "'colors' must be a non-empty object"
                continue
            bad_keys = [k for k in colors if k not in THEME_KEYS]
            if bad_keys:
                rejected[oid] = (f"unknown color names {sorted(bad_keys)}; "
                                 f"want: {sorted(THEME_KEYS)}")
                continue
            bad_vals = [k for k, v in colors.items()
                        if not _valid_theme_value(k, v)]
            if bad_vals:
                rejected[oid] = (f"bad color values for {sorted(bad_vals)}: "
                                 "hex #rgb/#rrggbb/#rrggbbaa, except glow "
                                 "('r, g, b' with each part 0-255) and "
                                 "scrim (rgba(r, g, b, alpha))")
                continue
            if action == "theme.apply":
                valid.append((oid, action, {"colors": dict(colors)}))
                continue
            name = params.get("name")
            if not isinstance(name, str) or not THEME_NAME_RE.match(name):
                rejected[oid] = ("'name' must match [a-z0-9-]{1,64}, "
                                 "starting with [a-z0-9] "
                                 "(e.g. \"harbor-dusk\")")
                continue
            valid.append((oid, action,
                          {"name": name, "colors": dict(colors)}))
        elif action == "allowedCommands.update":
            update, reason = _validate_allowed_update(params)
            if reason is not None:
                rejected[oid] = reason
                continue
            valid.append((oid, action, update))
        elif action == "bridge.restart":
            restart, reason = _validate_restart(params)
            if reason is not None:
                rejected[oid] = reason
                continue
            valid.append((oid, action, restart))
    return valid, rejected


def pick_approve_once_choice(choices):
    """ChoiceId of the narrowest approve choice, or None.

    Prefers a one-shot `approved` choice; falls back to any non-amendment
    approve decision (`approvedForSession`). Amendment decisions (which
    would persist a policy rule) and denials are never picked, and None
    means "leave it to the UI card".
    """
    if not isinstance(choices, list):
        return None
    cands = [c for c in choices
             if isinstance(c, dict) and c.get("choiceId")
             and c.get("decision") in ("approved", "approvedForSession")]
    if not cands:
        return None
    for c in cands:
        if c.get("decision") == "approved" and c.get("scope") == "once":
            return c["choiceId"]
    return cands[0]["choiceId"]

# Offered reasoning-effort tiers: the middle six of the MSP ReasoningEffort
# vocabulary (schema $defs/ReasoningEffort). The `none` and `ultra`
# extremes are deliberately excluded — `none` silently degrades answer
# quality and `ultra` burns budget with little return.
VALID_REASONING_EFFORTS = {"minimal", "low", "medium", "high",
                           "xhigh", "max"}

# MSP view/page directions (schema $defs/ViewPageDirection).
VALID_PAGE_DIRECTIONS = {"forward", "backward"}

# Default session-name length: the web panel names a session from the
# first few characters of its initial prompt (same convention as the
# muse TUI), as a fallback until an explicit rename takes precedence.
DEFAULT_SESSION_NAME_LEN = 40


def default_session_name(text, limit=DEFAULT_SESSION_NAME_LEN):
    """Initial-prompt prefix used as a session's default (fallback) name.

    Whitespace (including newlines) collapses to single spaces and the
    result hard-slices to `limit` characters. Returns "" when there is
    no usable text (blank prompt, images-only turn), so callers skip
    the rename instead of setting an empty name.
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def build_turn_input(text, images=None, skills=None):
    """WS prompt payload -> MSP TurnInputPart list.

    skills: optional list of {selector, arguments?} dicts, each becoming
    a {type: skill} part (selector required, arguments optional free
    text — the wire twin of what the TUI accepts after the shortcut
    token). Skill parts come first so "/selector args" reads as the
    skill invocation with args, not as plain user text.
    """
    parts = []
    for sk in skills or []:
        if not isinstance(sk, dict):
            raise ValueError("skill entries must be {selector, arguments?}")
        selector = sk.get("selector", "")
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError("skill entries need a non-empty selector")
        arguments = sk.get("arguments", "")
        if arguments is None:
            arguments = ""
        if not isinstance(arguments, str):
            raise ValueError("skill arguments must be text")
        part = {"type": "skill", "selector": selector.strip()}
        if arguments.strip():
            part["arguments"] = arguments.strip()
        parts.append(part)
    if text:
        parts.append({"type": "text", "text": text})
    for img in images or []:
        parts.append({
            "type": "image",
            "mediaType": img.get("mediaType", "image/png"),
            "base64Data": img.get("base64Data", ""),
        })
    if not parts:
        raise ValueError("prompt needs text, images, or a skill")
    return parts


def notification_session_id(method, params):
    """Route key for an MSP notification/request; None => broadcast."""
    if not isinstance(params, dict):
        return None
    sid = params.get("sessionId")
    if sid:
        return sid
    if method in ("session/started", "session/listChanged", "session/closed"):
        sess = params.get("session")
        if isinstance(sess, dict) and sess.get("sessionId"):
            return sess["sessionId"]
        # session/closed carries sessionId at top level; listChanged is global.
        return params.get("sessionId")
    return None


def map_notification_to_ws(method, params):
    """MSP notification -> WS server->client frame (thin envelope)."""
    return {"type": "event", "method": method, "params": params}


def map_server_request_to_ws(method, params):
    """MSP server-initiated request -> WS frame (receipt already sent)."""
    if method == "approval/request":
        return {"type": "approval", "approval": params}
    if method == "userInput/request":
        return {"type": "userInput", "prompt": params}
    return {"type": "event", "method": method, "params": params}


def settings_path():
    """User settings file where `mcpServers` entries live (per muse mcp help)."""
    return Path(os.path.expanduser("~")) / ".config" / "muse" / "settings.json"


def read_mcp_servers(path=None):
    """Local MCP server inventory.

    MSP v1 (stable AND experimental schema exports, verified against the
    `muse schema` bundle) exposes no mcp/* methods, so /mcp is served from
    the same settings.json the `muse mcp login` command uses. Returns a
    JSON-able dict; never raises on missing/unparseable files.
    """
    p = Path(path) if path else settings_path()
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return {"servers": [], "source": str(p), "configured": False,
                "hint": "no settings.json yet; run `muse login` or add an "
                        "mcpServers entry, then `muse mcp login <server>`"}
    except (OSError, ValueError) as e:
        return {"servers": [], "source": str(p), "configured": False,
                "hint": f"settings unreadable: {e}"}
    entries = raw.get("mcpServers") or {}
    servers = []
    for name, cfg in entries.items() if isinstance(entries, dict) else []:
        if not isinstance(cfg, dict):
            continue
        # Never echo secrets: report shape only (transport/url/command names
        # are config diagnostics-safe; headers/env values are not).
        servers.append({
            "name": name,
            "transport": cfg.get("transport", "streamableHttp"),
            "url": cfg.get("url"),
            "command": cfg.get("command"),
            "hasHeaders": bool(cfg.get("headers")),
            "hasEnv": bool(cfg.get("env")),
        })
    return {"servers": servers, "source": str(p),
            "configured": bool(servers),
            "hint": "" if servers else
                    "no mcpServers entries; add one under mcpServers in "
                    "settings.json, or attach one to a new session with "
                    "`/new --mcp <name>`"}


def read_settings_raw(path=None):
    """Parsed settings.json; {} when missing/unparseable (never raises)."""
    p = Path(path) if path else settings_path()
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# SessionMcpServerMode + SessionMcpStdioFraming closed enums: invalid optional
# values are dropped with a warning (the host would reject the whole start).
VALID_MCP_MODES = {"required", "optional"}
VALID_MCP_FRAMINGS = {"auto", "contentLength", "lineDelimitedJson"}


def build_session_mcp_config(names, path=None):
    """settings.json entries -> wire SessionConfig {"mcpServers": {...}}.

    Secrets (headers/env values) are resolved here, bridge-side, so they
    never cross the browser boundary. Returns (config, warnings).
    Raises ValueError on unknown names (with the known-name list).
    """
    entries = read_settings_raw(path).get("mcpServers") or {}
    if not isinstance(entries, dict):
        entries = {}
    servers = {}
    warnings = []
    for name in names or []:
        cfg = entries.get(name)
        if not isinstance(cfg, dict):
            known = sorted(entries) or ["(none configured)"]
            raise ValueError(f"unknown MCP server {name!r}; known: "
                             + ", ".join(known))
        if cfg.get("command"):
            arm = {"transport": "stdio", "command": cfg["command"]}
            if isinstance(cfg.get("args"), list):
                arm["args"] = [str(a) for a in cfg["args"]]
            if isinstance(cfg.get("env"), dict):
                arm["env"] = {str(k): str(v)
                              for k, v in cfg["env"].items()}
            if cfg.get("framing") in VALID_MCP_FRAMINGS:
                arm["framing"] = cfg["framing"]
            elif cfg.get("framing") is not None:
                warnings.append(f"{name}: dropping invalid framing "
                                f"{cfg['framing']!r}")
            if cfg.get("mode") in VALID_MCP_MODES:
                arm["mode"] = cfg["mode"]
            elif cfg.get("mode") is not None:
                warnings.append(f"{name}: dropping invalid mode "
                                f"{cfg['mode']!r}")
        elif cfg.get("url"):
            arm = {"transport": "streamableHttp", "url": cfg["url"]}
            if isinstance(cfg.get("headers"), dict):
                arm["headers"] = {str(k): str(v)
                                  for k, v in cfg["headers"].items()}
            if cfg.get("mode") in VALID_MCP_MODES:
                arm["mode"] = cfg["mode"]
            elif cfg.get("mode") is not None:
                warnings.append(f"{name}: dropping invalid mode "
                                f"{cfg['mode']!r}")
        else:
            warnings.append(f"{name}: skipped (entry has neither "
                            "command nor url)")
            continue
        servers[name] = arm
    return ({"mcpServers": servers} if servers else {}), warnings


# Client-supplied sessionIds become directory names: strict charset, no
# leading dot, bounded length. Anything else is rejected, never remapped
# (silent remapping could park two sessions in one workspace).
SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def session_workspace_dir(base, session_id):
    """Directory for one session under `base`; created, absolute, contained.

    Raises ValueError on unsafe ids or containment failure. Never raises
    on pre-existing directories.
    """
    sid = session_id or ""
    if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
            or sid in (".", ".."):
        raise ValueError(f"unsafe sessionId for workspace dir: {sid!r}")
    root = Path(base).resolve()
    target = (root / sid).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"workspace dir escapes base: {sid!r}")
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def validate_manual_root(path):
    """Check a client-supplied session directory: absolute, existing dir.

    Manual roots may point anywhere on the device (first-use confirm
    happens in the UI), but the bridge creates nothing outside its own
    workspace base — so the directory must already exist. Raises
    ValueError, which the caller turns into an error reply.
    """
    p = path if isinstance(path, str) else ""
    if not p.strip():
        raise ValueError("workspaceRoot must be a non-empty path")
    if not os.path.isdir(p):
        raise ValueError(f"workspaceRoot is not an existing directory: {p!r}")
    return str(Path(p).resolve())


def browse_home():
    """Default explorer root: the server's $HOME, falling back to /."""
    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        return str(Path(home).resolve())
    return "/"


def browse_dir(path=None):
    """List one directory for the new-session explorer.

    Any on-device path may be listed (manual roots may point anywhere);
    nothing is created. Returns a JSON-able dict:
    {path, parent, home, entries:[{name, path, isDir, isHidden}]} sorted
    dirs-first, alphabetical (case-insensitive). Raises ValueError on a
    missing/non-directory path or an unreadable directory.
    """
    raw = path if isinstance(path, str) else ""
    raw = raw.strip() or browse_home()
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    target = Path(raw)
    if not target.is_absolute():
        raise ValueError(f"browse path must be absolute: {raw!r}")
    if not os.path.isdir(target):
        raise ValueError(f"not an existing directory: {raw!r}")
    resolved = str(target.resolve())
    try:
        with os.scandir(resolved) as it:
            rows = [(e.name, e.path, e.is_dir(follow_symlinks=True))
                    for e in it]
    except OSError as e:
        raise ValueError(f"directory unreadable: {resolved!r} ({e})")
    entries = [{
        "name": name,
        "path": str(Path(resolved) / name),
        "isDir": bool(is_dir),
        "isHidden": name.startswith("."),
    } for name, _, is_dir in rows]
    entries.sort(key=lambda e: (not e["isDir"], e["name"].lower(), e["name"]))
    parent = str(Path(resolved).parent)
    return {"path": resolved, "parent": parent, "home": browse_home(),
            "entries": entries}


def muse_sessions_base():
    """On-disk MSP session store root (~/.local/share/muse/sessions)."""
    return Path(os.path.expanduser("~")) / ".local" / "share" / "muse" \
        / "sessions"


def delete_session_files(session_id, base=None):
    """Remove one session's on-disk store dirs (rm -rf semantics).

    Deletes the dated main dir (sessions/YYYY/MM/DD/<sessionId>/) and the
    view-store dir (sessions/.msp-view-v1/<sessionId>/), and nothing else.
    The host is never called: its deletion registry admits one deletion
    at a time and rejects back-to-back deletes with Store(Busy), so file
    removal is the reliable path (verified live: with both dirs gone the
    host answers resume with -32020 "was not found", and a restarted host
    no longer lists the session).

    Raises ValueError on unsafe ids or containment failure. Missing dirs
    are success (already gone — rm -f semantics). Returns the removed
    path strings.
    """
    sid = session_id or ""
    if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
            or sid in (".", ".."):
        raise ValueError(f"unsafe sessionId for session delete: {sid!r}")
    root = Path(base).resolve() if base else muse_sessions_base().resolve()
    targets = [root / ".msp-view-v1" / sid]
    # Dated layout is YYYY/MM/DD/<sid>; glob only (the safe charset holds
    # no glob metacharacters) instead of a full-tree walk.
    targets += [p for p in root.glob(f"????/??/??/{sid}")]
    removed = []
    for target in targets:
        if target.is_symlink():
            # Lexical location is inside the store by construction; drop
            # the link itself, never its target.
            target.unlink()
            removed.append(str(target))
            continue
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"refusing to delete outside session store: {sid!r}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
            removed.append(str(resolved))
        elif resolved.exists():
            raise ValueError(
                f"refusing to delete non-directory session path: {resolved}")
        # else: already gone — still success.
    return sorted(removed)


def _workspace_under_base(workspace_root, base):
    """True when workspace_root is contained under base (both resolved)."""
    if not base or not isinstance(workspace_root, str) or not workspace_root:
        return False
    try:
        resolved_base = Path(base).resolve()
        resolved_root = Path(workspace_root).resolve()
        return resolved_base in resolved_root.parents or resolved_root == resolved_base
    except (OSError, ValueError):
        return False


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
    Path(__file__).resolve().parent / "github_instructions.md")
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


class SessionRouter:
    """Tracks WS<->sessionId subscriptions and last-seen view cursors."""

    def __init__(self, msp, workspace_base=None, gh_bin="gh",
                 sessions_base=None, allowed_commands_path=None,
                 themes_dir=None, on_restart=None):
        self._msp = msp
        # Base dir for per-session workspaces (None = send no workspaceRoot).
        self._workspace_base = str(workspace_base) if workspace_base else None
        # On-disk session store root for file-based deletes (None = the
        # default ~/.local/share/muse/sessions).
        self._sessions_base = str(sessions_base) if sessions_base else None
        # Sessions this bridge removed from disk: hidden from list/resume
        # until the serve host reindexes (it keeps listing them from
        # memory while running).
        self._deleted_sids = set()
        # Sessions started through this bridge (any path: new, prompt,
        # githubOpen): the UI lists them before TUI-created ones.
        # Persisted as one root-level file in the session store (beside,
        # never inside, session dirs) so a bridge restart keeps the
        # grouping. sessions_base=None keeps it memory-only — which is
        # also what makes the test suite hermetic (no home-dir writes).
        # Production passes an explicit store dir (see deploy notes).
        self._bridge_sids = set()
        self._bridge_sids_file = (
            str(Path(self._sessions_base) / ".web-muse-bridge-sids.json")
            if self._sessions_base else None)
        self._load_bridge_sids()
        self._gh_bin = gh_bin or "gh"
        # Shell auto-approve policy: validated server-side config
        # (allowed_commands_path, defaulting to the shipped file), never
        # client-supplied. Corrupt files fall back to builtins (logged).
        # The path is kept: approved policy orders rewrite this file and
        # reload it, so no restart is needed for order-driven updates.
        self._allowed_path = (allowed_commands_path
                              or ALLOWED_COMMANDS_FILE)
        self._allowed, _ac_warnings = load_allowed_commands(
            self._allowed_path)
        for _w in _ac_warnings:
            LOG.warning("%s", _w)
        # Agent orders channel: sessionId -> workspace root (for locating
        # `.web-muse/orders.json`) and staged orders awaiting a human's
        # ordersDecide (policy updates, saved themes, backend restarts),
        # keyed (sessionId, orderId).
        self._workspace_roots = {}
        self._pending_orders = {}
        # Approved-restart executor: set by server/main.py to the real
        # graceful-restart coroutine; None in tests unless injected, in
        # which case an approved restart is recorded but not executed.
        self._on_restart = on_restart
        # Destination for approved theme.save orders (None = the shipped
        # web/themes dir). Kept as a path: approved saves write one
        # <name>.json file there, which is what bare `/theme` lists.
        # Tests pass a tmp dir to stay hermetic (same pattern as the
        # allowed-commands path above).
        self._themes_dir = (str(themes_dir) if themes_dir is not None
                            else str(DEFAULT_THEMES_DIR))
        # Known repos/branches: every listing still shells out to `gh`
        # for new entries, then merges them in — so repeat picker opens
        # keep working (stale) when `gh` hiccups and accumulate rows
        # across `--limit` pages. Per-router (not global), which keeps
        # the test suite hermetic.
        self._known_repos = []
        self._known_branches = {}
        # Clone opId -> [task, conn, fullName]. Terminal-event ownership
        # goes to whoever pops the record first (finishing task or
        # canceller), so exactly one githubCloneResult fires even when a
        # cancel lands before the task's first step (a coroutine cancelled
        # that early never runs its body, not even `finally`).
        self._github_ops = {}
        # Sessions opened via githubOpen: shell approvals matching
        # GH_AUTO_ALLOW_RE are decided bridge-side (gh PR flow never
        # parks on an unanswered prompt). Scoped here — not global — so
        # other sessions keep prompting as before.
        self._gh_auto_sids = set()
        self._subs = {}   # sessionId -> set of ClientConnection
        # Sessions this bridge created while still unnamed: the first
        # prompt names them from its initial text (default fallback).
        # An explicit rename drops the id, so user/host names always win.
        self._auto_name_pending = set()
        self._cursors = {}  # sessionId -> last viewCursor seen
        self._turns = {}  # sessionId -> running turnId (from turn/started)
        self._conns = set()
        self._lock = asyncio.Lock()

    def _load_bridge_sids(self):
        """Restore the bridge-created set (never raises)."""
        if not self._bridge_sids_file:
            return
        try:
            raw = json.loads(Path(self._bridge_sids_file).read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, list):
            self._bridge_sids = {s for s in raw
                                 if isinstance(s, str) and s}

    def _save_bridge_sids(self):
        """Persist the bridge-created set (never raises)."""
        if not self._bridge_sids_file:
            return
        try:
            p = Path(self._bridge_sids_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(sorted(self._bridge_sids)))
        except OSError:
            LOG.warning("bridge sid store unwritable", exc_info=True)

    def _remember_bridge_sid(self, session_id):
        if session_id and session_id not in self._bridge_sids:
            self._bridge_sids.add(session_id)
            self._save_bridge_sids()

    # -- connection registry ------------------------------------------------
    def add_conn(self, conn):
        self._conns.add(conn)

    def drop_conn(self, conn):
        self._conns.discard(conn)
        for members in self._subs.values():
            members.discard(conn)

    # -- subscriptions ------------------------------------------------------
    async def subscribe(self, conn, session_id, after=None):
        params = {"sessionId": session_id}
        if after:
            params["after"] = after
        result = await self._msp.call("view/subscribe", params)
        async with self._lock:
            self._subs.setdefault(session_id, set()).add(conn)
            conn.sessions.add(session_id)
        return result

    async def unsubscribe(self, conn, session_id):
        try:
            result = await self._msp.call("view/unsubscribe",
                                          {"sessionId": session_id})
        finally:
            async with self._lock:
                self._subs.get(session_id, set()).discard(conn)
                conn.sessions.discard(session_id)
        return result

    def cursor(self, session_id):
        return self._cursors.get(session_id, "")

    # -- fan-out ------------------------------------------------------------
    def on_notification(self, method, params):
        if isinstance(params, dict) and params.get("viewCursor") \
                and params.get("sessionId"):
            self._cursors[params["sessionId"]] = params["viewCursor"]
        # Track the running turn per session so steer/interrupt/cancel can
        # default their exact-turn target (turn/steer REQUIRES expectedTurnId).
        if isinstance(params, dict) and params.get("sessionId"):
            if method == "turn/started" and params.get("turnId"):
                self._turns[params["sessionId"]] = params["turnId"]
            elif method == "turn/completed":
                if self._turns.get(params["sessionId"]) == params.get("turnId"):
                    self._turns.pop(params["sessionId"], None)
                # Agent orders are checked after each turn, in the
                # background: the agent's file lands during the turn, so
                # completion is the earliest moment it can be complete.
                if params["sessionId"] in self._workspace_roots:
                    try:
                        asyncio.create_task(
                            self._check_orders(params["sessionId"]))
                    except RuntimeError:
                        pass  # no running loop (tests): caller checks directly
        frame = map_notification_to_ws(method, params)
        sid = notification_session_id(method, params)
        targets = self._targets_for(sid)
        for conn in targets:
            conn.queue_frame(frame)

    def on_server_request(self, method, params):
        if method == "approval/request" and self._auto_approve_gh_pr(params):
            return
        frame = map_server_request_to_ws(method, params)
        sid = notification_session_id(method, params)
        targets = self._targets_for(sid)
        # Approval/userInput must reach a surface: fall back to broadcast
        # when nobody subscribed to the session yet.
        if not targets and method in ("approval/request", "userInput/request"):
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(frame)

    def _auto_approve_gh_pr(self, params):
        """Decide a gh-PR approval for a GitHub-opened session (no UI card).

        Returns True when the approval was consumed: it matched the
        allowlist and an approve choice existed, so the decide was queued
        and a notice event fanned out instead of the approval card.
        Anything else returns False and the caller forwards the card
        unchanged (the agent parks until a human decides, as before).
        """
        if not isinstance(params, dict):
            return False
        sid = params.get("sessionId")
        if not sid or sid not in self._gh_auto_sids:
            return False
        subject = params.get("subject")
        if not shell_auto_allowed(subject, self._allowed):
            return False
        choice_id = pick_approve_once_choice(params.get("availableChoices"))
        if choice_id is None:
            LOG.warning("gh auto-approve: no approve choice for %s",
                        params.get("approvalId"))
            return False
        command = subject.get("command", "").strip()
        decide = {"sessionId": sid,
                  "approvalId": params.get("approvalId"),
                  "choiceId": choice_id,
                  # Race guard: must equal the request's current stage.
                  "requirementId": params.get("currentRequirementId")}
        try:
            asyncio.create_task(self._gh_auto_decide(decide, command))
        except RuntimeError:
            return False  # no running loop: fall through to the UI card
        notice = {"type": "event", "method": "githubAutoApproved",
                  "params": {"sessionId": sid,
                             "approvalId": params.get("approvalId"),
                             "command": command}}
        targets = self._targets_for(sid)
        if not targets:
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(notice)
        return True

    async def _gh_auto_decide(self, decide, command):
        """Send the queued approval/decide (all failures are logged)."""
        try:
            await self._msp.command("approval/decide", decide)
        except Exception:
            LOG.warning("gh auto-approve decide failed for %s (%s)",
                        decide.get("approvalId"), command, exc_info=True)
        else:
            LOG.info("gh auto-approved in session %s: %s",
                     decide.get("sessionId"), command)

    # -- Agent orders ------------------------------------------------------
    def _emit_orders_event(self, sid, method, params):
        """Fan one orders event out (session targets, else broadcast)."""
        frame = {"type": "event", "method": method, "params": params}
        targets = self._targets_for(sid)
        if not targets:
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(frame)

    def _broadcast_orders_event(self, method, params):
        """Fan one orders event out to every connection.

        Used for bridge restarts: the restart drops *all* browser tabs,
        not just the ordering session's, so every client must see the
        card and the bridgeRestarting notice.
        """
        frame = {"type": "event", "method": method, "params": params}
        for conn in list(self._conns):
            conn.queue_frame(frame)

    async def _check_orders(self, sid):
        """Validate one session's orders file and act (never raises)."""
        root = self._workspace_roots.get(sid)
        if not root:
            return
        odir = Path(root) / ORDERS_DIRNAME
        ofile = odir / ORDERS_FILENAME
        try:
            if not ofile.is_file() \
                    or ofile.stat().st_size > ORDERS_MAX_BYTES:
                return
            payload = json.loads(ofile.read_text())
        except (OSError, ValueError) as e:
            LOG.warning("orders: unreadable file for session %s (%s)",
                        sid, e)
            return
        if isinstance(payload, dict) \
                and isinstance(payload.get("orders"), list) \
                and len(payload["orders"]) > ORDERS_MAX_COUNT:
            LOG.warning("orders: too many orders for session %s (%d)",
                        sid, len(payload["orders"]))
            return
        receipt = _read_receipt(odir)
        valid, rejected = validate_orders(payload, receipt)
        processed = {oid: {"status": "rejected", "reason": reason}
                     for oid, reason in rejected.items()}
        for oid, action, params in valid:
            if action == "theme.apply":
                colors = params["colors"]
                self._emit_orders_event(sid, "themeApply",
                                        {"sessionId": sid, "orderId": oid,
                                         "colors": colors})
                processed[oid] = {"status": "applied"}
                LOG.info("orders: theme.apply from session %s (%d "
                         "colors)", sid, len(colors))
            elif action == "theme.save":
                self._pending_orders[(sid, oid)] = {
                    "action": action, "params": params}
                self._emit_orders_event(sid, "ordersPending",
                                        {"sessionId": sid, "orderId": oid,
                                         "action": action, "params": params,
                                         "needsConfirm": True})
                processed[oid] = {"status": "needsConfirm"}
                LOG.info("orders: theme.save staged for session %s "
                         "(awaiting human)", sid)
            elif action == "allowedCommands.update":
                self._pending_orders[(sid, oid)] = {
                    "action": action, "params": params}
                self._emit_orders_event(sid, "ordersPending",
                                        {"sessionId": sid, "orderId": oid,
                                         "action": action, "params": params,
                                         "needsConfirm": True})
                processed[oid] = {"status": "needsConfirm"}
                LOG.info("orders: policy update staged for session %s "
                         "(awaiting human)", sid)
            elif action == "bridge.restart":
                self._pending_orders[(sid, oid)] = {
                    "action": action, "params": params}
                self._broadcast_orders_event(
                    "ordersPending",
                    {"sessionId": sid, "orderId": oid,
                     "action": action, "params": params,
                     "needsConfirm": True})
                processed[oid] = {"status": "needsConfirm"}
                LOG.info("orders: bridge restart staged for session %s "
                         "(awaiting human)", sid)
        if processed:
            _write_receipt(odir, processed)

    def _mark_order(self, sid, oid, status, reason=""):
        """Record one order's terminal status in its session receipt."""
        root = self._workspace_roots.get(sid)
        if not root:
            return
        entry = {"status": status}
        if reason:
            entry["reason"] = reason
        _write_receipt(Path(root) / ORDERS_DIRNAME, {oid: entry})

    async def _decide_order(self, msg):
        """Run a human's ordersDecide on a staged order."""
        sid = msg.get("sessionId")
        oid = msg.get("orderId")
        approved = msg.get("approved")
        if not isinstance(approved, bool):
            raise ValueError("approved must be true or false")
        rec = self._pending_orders.pop((sid, oid), None)
        if rec is None:
            raise ValueError(f"unknown order {oid!r} for session {sid!r}")
        if not approved:
            self._mark_order(sid, oid, "denied")
            LOG.info("orders: %s denied for session %s",
                     rec.get("action", "order"), sid)
            return {"orderId": oid, "approved": False}
        if rec.get("action") == "theme.save":
            self._apply_theme_save(sid, oid, rec["params"])
        elif rec.get("action") == "bridge.restart":
            # Receipt first: the restart kills this process, so the
            # agent's receipt must already say approved when we go down.
            self._mark_order(sid, oid, "approved")
            self._broadcast_orders_event(
                "bridgeRestarting",
                {"sessionId": sid, "orderId": oid,
                 "params": rec["params"]})
            self._schedule_restart(rec["params"])
            LOG.info("orders: bridge restart approved for session %s "
                     "(restarting)", sid)
            return {"orderId": oid, "approved": True, "restarting": True}
        else:
            self._apply_policy_update(sid, oid, rec["params"])
        return {"orderId": oid, "approved": True}

    def _schedule_restart(self, params):
        """Queue the approved restart after its grace delay (never raises).

        The delay lets this approval's reply frame and the
        bridgeRestarting event flush to browsers before the process
        exits; without it the approver would see only a dropped socket.
        """
        delay = params.get("delaySeconds", RESTART_DELAY_DEFAULT) \
            if isinstance(params, dict) else RESTART_DELAY_DEFAULT
        try:
            delay = int(delay)
        except (TypeError, ValueError):
            delay = RESTART_DELAY_DEFAULT
        delay = min(max(delay, RESTART_DELAY_MIN), RESTART_DELAY_MAX)
        try:
            asyncio.create_task(self._run_restart_later(delay))
        except RuntimeError:
            LOG.warning("orders: no running loop; restart not scheduled")

    async def _run_restart_later(self, delay):
        """Sleep out the grace delay, then run the restart hook."""
        try:
            await asyncio.sleep(delay)
            hook = self._on_restart
            if hook is None:
                LOG.error("orders: bridge restart approved but no "
                          "restart hook is configured; staying up")
                return
            if asyncio.iscoroutinefunction(hook):
                await hook(delay)
            else:
                hook(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("orders: bridge restart failed")

    def _list_orders(self, sid):
        """All staged (needsConfirm) orders, optionally session-scoped.

        Lets fresh or reconnected clients re-render cards that were
        fanned out as ordersPending events before they attached.
        """
        out = []
        for (psid, oid), rec in self._pending_orders.items():
            if sid and psid != sid:
                continue
            out.append({"sessionId": psid, "orderId": oid,
                        "action": rec.get("action"),
                        "params": rec.get("params", {}),
                        "needsConfirm": True})
        return {"orders": out}

    def _apply_theme_save(self, sid, oid, params):
        """Write an approved theme.save order to web/themes/<name>.json."""
        name = params.get("name") if isinstance(params, dict) else None
        colors = params.get("colors") if isinstance(params, dict) else None
        if not isinstance(name, str) or not THEME_NAME_RE.match(name):
            raise ValueError(f"bad theme name {name!r}")
        if not isinstance(colors, dict) or not colors:
            raise ValueError("theme.save needs a non-empty colors object")
        for k, v in colors.items():
            if k not in THEME_KEYS:
                raise ValueError(f"unknown color name {k!r}")
            err = theme_color_error(k, v)
            if err is not None:
                raise ValueError(err)
        target = Path(self._themes_dir) / f"{name}.json"
        if target.resolve().parent != Path(self._themes_dir).resolve():
            raise ValueError(f"refusing to save outside themes dir: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dict(colors), indent=2) + "\n")
        self._mark_order(sid, oid, "approved")
        LOG.info("orders: theme saved as %s for session %s", target, sid)

    def _apply_policy_update(self, sid, oid, update):
        """Rewrite the allowed-commands file from an approved order."""
        try:
            base = json.loads(Path(self._allowed_path).read_text())
            if not isinstance(base, dict):
                base = {}
        except (OSError, ValueError):
            base = {}
        merged = dict(base)
        merged["_comment"] = (
            "Updated by approved agent order "
            f"{oid} (session {sid}); previous version kept in "
            f"{Path(self._allowed_path).name}.bak")
        if "allow" in update:
            merged["allow"] = update["allow"]
        if "deny" in update:
            merged["deny"] = update["deny"]
        if "allow" not in merged or "deny" not in merged:
            raise ValueError("current policy has no allow/deny to merge "
                             "into; refusing partial update")
        tmp = Path(self._allowed_path).with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(merged, indent=2))
            try:
                bak = Path(str(self._allowed_path) + ".bak")
                bak.write_bytes(Path(self._allowed_path).read_bytes())
            except OSError:
                pass  # backup best-effort; the tmp file is authoritative
            os.replace(tmp, self._allowed_path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        self._allowed, warnings = load_allowed_commands(self._allowed_path)
        for w in warnings:
            LOG.warning("%s", w)
        self._mark_order(sid, oid, "approved")
        LOG.info("orders: policy update applied for session %s", sid)

    def _targets_for(self, session_id):
        if session_id:
            subs = self._subs.get(session_id, set())
            if subs:
                return list(subs)
            # Session-scoped events also go to conns with no subscription yet
            # (fresh clients waiting for their first subscribe).
            unattached = [c for c in self._conns if not c.sessions]
            return unattached
        return list(self._conns)

    # -- GitHub picker caches ------------------------------------------------
    async def _github_repos(self, search=None, limit=None):
        """Cached repo rows; `gh` is still checked for new ones every call.

        Returns (rows, stale): fresh rows are merged into the known set
        and served; when `gh` fails and known rows exist, the known
        (search-filtered) rows are served with stale True instead of
        erroring the picker.
        """
        try:
            fresh = await run_gh_list(search, limit, gh_bin=self._gh_bin)
        except GithubError:
            if self._known_repos:
                LOG.warning("githubRepos: gh failed, serving %d known "
                            "row(s)", len(self._known_repos))
                return filter_repos(self._known_repos, search), True
            raise
        # Fresh rows arrive search-filtered; merging keeps each complete
        # row, so the known set only ever grows toward the full list.
        self._known_repos = merge_repo_rows(self._known_repos, fresh)
        return filter_repos(self._known_repos, search), False

    async def _github_branches(self, full_name):
        """Cached branch names for one repo; `gh` still checked every call.

        Returns (names, stale) with the same fresh-or-known contract as
        _github_repos. Invalid names raise ValueError (never served from
        cache).
        """
        try:
            fresh = await run_gh_branches(full_name, gh_bin=self._gh_bin)
        except GithubError:
            known = self._known_branches.get(full_name)
            if known:
                LOG.warning("githubBranches: gh failed for %s, serving "
                            "%d known branch(es)", full_name, len(known))
                return list(known), True
            raise
        self._known_branches[full_name] = merge_branch_names(
            self._known_branches.get(full_name), fresh)
        return list(self._known_branches[full_name]), False

    # -- WS request dispatch ------------------------------------------------
    async def handle_client_message(self, conn, msg):
        """Route one decoded WS frame; returns the reply frame (or None)."""
        if not isinstance(msg, dict):
            return {"type": "result", "ok": False,
                    "error": {"message": "frame must be a JSON object"}}
        rid = msg.get("id")
        mtype = msg.get("type", "")

        def reply(ok, **kw):
            frame = {"type": "result", "ok": ok}
            if rid is not None:
                frame["id"] = rid
            frame.update(kw)
            return frame

        try:
            if mtype == "prompt":
                return reply(True, result=await self._do_prompt(conn, msg))
            if mtype == "new":
                return reply(True, result=await self._do_new(conn, msg))
            if mtype == "list":
                p = {}
                for k in ("cursor", "limit", "updatedAfter", "workspaceRoot"):
                    if msg.get(k) is not None:
                        p[k] = msg[k]
                if msg.get("filter") is not None:
                    p["filter"] = msg["filter"]
                result = await self._msp.call("session/list", p)
                # Auto-sync: workspace dir gone => delete the session (scoped to base).
                if isinstance(result, dict) and isinstance(result.get("sessions"), list):
                    for s in list(result["sessions"]):
                        if not isinstance(s, dict):
                            continue
                        root = s.get("workspaceRoot")
                        sid = s.get("sessionId")
                        if not sid or not isinstance(root, str) or not root:
                            continue
                        if os.path.isdir(root):
                            continue
                        if not _workspace_under_base(root, self._workspace_base):
                            continue
                        try:
                            delete_session_files(sid, base=self._sessions_base)
                        except Exception:
                            LOG.warning("auto-delete missing workspace failed for %s", sid, exc_info=True)
                            continue
                        try:
                            self._drop_routing(sid)
                        except Exception:
                            pass
                        self._deleted_sids.add(sid)
                # Mark bridge-started rows for first-group sorting in the
                # UI. Presence-only (no False key) so untouched rows stay
                # byte-identical to the host's.
                if isinstance(result, dict):
                    for s in result.get("sessions") or []:
                        if isinstance(s, dict) \
                                and s.get("sessionId") in self._bridge_sids:
                            s["bridgeCreated"] = True
                # Sessions removed from disk read as gone even though the
                # running host still lists them from memory.
                rows = result.get("sessions") \
                    if isinstance(result, dict) else None
                if isinstance(rows, list) and self._deleted_sids:
                    result["sessions"] = [
                        s for s in rows
                        if not (isinstance(s, dict) and s.get("sessionId")
                                in self._deleted_sids)]
                return reply(True, result=result)
            if mtype == "resume":
                if msg.get("sessionId") in self._deleted_sids:
                    raise MspError(-32000,
                                   f"unknown session {msg.get('sessionId')}")
                try:
                    result = await self._msp.command(
                        "session/resume",
                        _pick(msg, ("sessionId", "cursor", "excludeItems",
                                    "history")))
                except MspError as e:
                    if e.code == METHOD_NOT_FOUND:
                        result = await self._resume_fallback(
                            msg.get("sessionId"))
                    elif _retained_refused(e):
                        result = await self._resume_fallback(
                            msg.get("sessionId"),
                            none_reason="resume_refused_by_host",
                            fallback="resume_refused_by_host")
                    else:
                        raise
                self._attach(conn, msg.get("sessionId"))
                return reply(True, result=result)
            if mtype == "read":
                if msg.get("sessionId") in self._deleted_sids:
                    raise MspError(-32000,
                                   f"unknown session {msg.get('sessionId')}")
                try:
                    return reply(True, result=await self._msp.call(
                        "session/read",
                        _pick(msg, ("sessionId", "excludeItems"))))
                except MspError as e:
                    if e.code == METHOD_NOT_FOUND:
                        # Read-only fallback: same metadata, no attach.
                        return reply(True, result=await self._resume_fallback(
                            msg.get("sessionId")))
                    if _retained_refused(e):
                        return reply(True, result=await self._resume_fallback(
                            msg.get("sessionId"),
                            none_reason="resume_refused_by_host",
                            fallback="resume_refused_by_host"))
                    raise
            if mtype == "fork":
                result = await self._msp.command(
                    "session/fork",
                    _pick(msg, ("sessionId", "cutPoint", "excludeItems")))
                return reply(True, result=result)
            if mtype == "rename":
                result = await self._msp.command(
                    "session/rename", _pick(msg, ("sessionId", "name")))
                # Explicit renames take precedence over the default: the
                # session leaves the auto-name set, present or future.
                self._auto_name_pending.discard(msg.get("sessionId"))
                return reply(True, result=result)
            if mtype == "delete":
                # File-based delete: rm -rf the session's store dirs, never
                # the host's deletion registry (Store(Busy) on back-to-back
                # deletes). Missing dirs are success (already gone).
                sid = msg.get("sessionId")
                removed = delete_session_files(
                    sid, base=self._sessions_base)
                self._drop_routing(sid)
                self._deleted_sids.add(sid)
                return reply(True, result={"deleted": True,
                                           "sessionId": sid,
                                           "removed": removed})
            if mtype == "interrupt":
                return reply(True, result=await self._msp.command(
                    "turn/interrupt",
                    _pick(msg, ("sessionId", "turnId", "retract"))))
            if mtype == "cancel":
                return reply(True, result=await self._msp.command(
                    "turn/cancel", _pick(msg, ("sessionId", "turnId"))))
            if mtype == "steer":
                p = _pick(msg, ("sessionId", "expectedTurnId", "reasoningEffort"))
                p["input"] = build_turn_input(msg.get("text", ""),
                                              msg.get("images"))
                # turn/steer requires the exact running turn; default it from
                # the last turn/started we fanned out when the client omits it.
                if not p.get("expectedTurnId"):
                    tracked = self._turns.get(p.get("sessionId", ""))
                    if tracked:
                        p["expectedTurnId"] = tracked
                    else:
                        return reply(False, error={
                            "message": "steer needs the running turn: no turn "
                                       "is tracked for this session (send "
                                       "expectedTurnId or wait for "
                                       "turn/started)"})
                return reply(True, result=await self._msp.command("turn/steer", p))
            if mtype == "unqueue":
                return reply(True, result=await self._msp.command(
                    "turn/unqueue", _pick(msg, ("sessionId", "turnId"))))
            if mtype == "approve":
                return reply(True, result=await self._msp.command(
                    "approval/decide",
                    _pick(msg, ("sessionId", "approvalId", "choiceId",
                                "requirementId", "feedback"))))
            if mtype == "answer":
                return reply(True, result=await self._msp.command(
                    "userInput/answer",
                    _pick(msg, ("sessionId", "userInputId", "answers"))))
            if mtype == "subscribe":
                try:
                    result = await self.subscribe(conn, msg["sessionId"],
                                                  msg.get("after"))
                except MspError as e:
                    if e.code != METHOD_NOT_FOUND:
                        raise
                    # Memory-only hosts stream nothing anyway; attach
                    # locally so live routing still works if events flow.
                    self._attach(conn, msg["sessionId"])
                    result = {"viewCursor": self.cursor(msg["sessionId"]),
                              "replay": "unserved_by_host"}
                return reply(True, result=result)
            if mtype == "unsubscribe":
                result = await self.unsubscribe(conn, msg["sessionId"])
                return reply(True, result=result)
            if mtype == "page":
                p = _pick(msg, ("sessionId", "cursor", "anchor", "direction"))
                p["limit"] = msg.get("limit", 100)
                if p.get("direction") is not None \
                        and p["direction"] not in VALID_PAGE_DIRECTIONS:
                    return reply(False, error={
                        "message": "direction must be one of "
                                   f"{sorted(VALID_PAGE_DIRECTIONS)}"})
                try:
                    return reply(True, result=await self._msp.call("view/page", p))
                except MspError as e:
                    if e.code != METHOD_NOT_FOUND:
                        raise
                    return reply(False, error={
                        "message": "view/page is not served by this host; "
                                   "history paging unavailable",
                        "code": METHOD_NOT_FOUND})
            if mtype == "models":
                p = {}
                if msg.get("sessionId"):
                    p["sessionId"] = msg["sessionId"]
                return reply(True, result=await self._msp.call("model/list", p))
            if mtype == "setModel":
                p = {"sessionId": msg["sessionId"], "model": msg["model"]}
                return reply(True, result=await self._msp.command(
                    "session/setModel", p))
            if mtype == "setApprovalMode":
                mode = msg.get("mode", "")
                if mode not in VALID_APPROVAL_MODES:
                    return reply(False, error={
                        "message": f"unknown approval mode {mode!r}; "
                                   f"want one of {sorted(VALID_APPROVAL_MODES)}"})
                return reply(True, result=await self._msp.command(
                    "session/setApprovalMode",
                    {"sessionId": msg["sessionId"], "mode": mode}))
            if mtype == "setEffort":
                effort = msg.get("reasoningEffort", "")
                if effort not in VALID_REASONING_EFFORTS:
                    return reply(False, error={
                        "message": f"unknown reasoning effort {effort!r}; "
                                   f"want one of {sorted(VALID_REASONING_EFFORTS)}"})
                return reply(True, result=await self._msp.command(
                    "session/setReasoningEffort",
                    {"sessionId": msg["sessionId"],
                     "reasoningEffort": effort}))
            if mtype == "skills":
                return reply(True, result=await self._msp.call(
                    "skill/list", {"sessionId": msg["sessionId"]}))
            if mtype == "plugins":
                return reply(True, result=await self._msp.call(
                    "plugin/list", {"sessionId": msg["sessionId"]}))
            if mtype == "readOutput":
                return reply(True, result=await self._msp.call(
                    "item/readOutput",
                    _pick(msg, ("sessionId", "itemId", "outputRef",
                                "offsetBytes", "lengthBytes"))))
            if mtype == "mcp":
                # No mcp/* methods exist on MSP v1 (stable+experimental):
                # serve the local settings.json inventory instead.
                return reply(True, result=read_mcp_servers())
            if mtype == "browse":
                # Filesystem explorer for the + dialog: list one
                # directory; empty/missing path opens the server's $HOME.
                return reply(True, result=browse_dir(msg.get("path")))
            if mtype == "compact":
                return reply(True, result=await self._msp.command(
                    "session/compact", _pick(msg, ("sessionId", "turnId"))))
            if mtype == "usage":
                return reply(True, result=await self._msp.call("usage/read", {}))
            if mtype == "pending":
                return reply(True, result=await self._msp.call(
                    "approval/listPending", {"sessionId": msg["sessionId"]}))
            if mtype == "githubRepos":
                # Repo picker rows: known rows cached, `gh` still
                # checked for new ones every call (stale only when
                # `gh` fails and known rows exist).
                repos, stale = await self._github_repos(
                    msg.get("search"), msg.get("limit"))
                result = {"repos": repos}
                if stale:
                    result["stale"] = True
                return reply(True, result=result)
            if mtype == "githubBranches":
                # Branch picker rows for one repo: same cache contract.
                branches, stale = await self._github_branches(
                    msg.get("fullName"))
                result = {"branches": branches}
                if stale:
                    result["stale"] = True
                return reply(True, result=result)
            if mtype == "githubClone":
                # Shallow-clone into workspaces/<sessionId>/<repo>/, admitted
                # async: the reply is instant ({accepted, opId, ...}) and
                # the outcome streams back as a githubCloneResult event, so
                # the connection stays responsive and githubCancel can
                # preempt a hanging clone. Progress streams as
                # githubCloneProgress events (no sessionId: global).
                return reply(True, result=self._do_github_clone(
                    conn, msg))
            if mtype == "githubCancel":
                return reply(True, result=self._cancel_github_op(
                    conn, msg.get("opId")))
            if mtype == "ordersDecide":
                # Human verdict on a staged order (gated actions —
                # policy updates, saved themes, backend restarts —
                # never run without this).
                return reply(True, result=await self._decide_order(msg))
            if mtype == "ordersList":
                # Staged orders awaiting a human (for clients that
                # attached after the ordersPending fan-out, e.g. on
                # reload or reconnect).
                return reply(True, result=self._list_orders(
                    msg.get("sessionId")))
            if mtype == "githubClean":
                # Delete one session's clone leaf after confirm (the UI
                # confirms; the bridge only enforces the leaf shape).
                return reply(True, result=self._clean_github_clone(
                    msg.get("sessionId")))
            if mtype == "githubOpen":
                # Clone + session/start rooted at the clone, admitted async
                # like githubClone (same events; the result carries the
                # started session). The browser records the dest in its
                # first-use allow list; picking the repo in the UI is the
                # consent, equivalent to the manual-root confirm.
                return reply(True, result=self._do_github_open(
                    conn, msg))
            if mtype == "ping":
                return reply(True, result={"pong": True})
            return reply(False, error={"message": f"unknown type {mtype!r}"})
        except KeyError as e:
            return reply(False, error={"message": f"missing field: {e}"})
        except ValueError as e:
            return reply(False, error={"message": str(e)})
        except Exception as e:  # MspError and friends -> error reply
            code = getattr(e, "code", None)
            err = {"message": str(e)}
            if code is not None:
                err["code"] = code
            data = getattr(e, "data", None)
            if data is not None:
                err["data"] = data
            return reply(False, error=err)

    async def _do_prompt(self, conn, msg):
        session_id = msg.get("sessionId")
        if not session_id:
            started = await self._do_new(conn, msg)
            session_id = started["session"]["sessionId"]
        p = {"sessionId": session_id,
             "input": build_turn_input(msg.get("text", ""),
                                       msg.get("images"),
                                       msg.get("skills"))}
        if msg.get("ifBusy"):
            p["ifBusy"] = msg["ifBusy"]
        if msg.get("displayText"):
            p["displayText"] = msg["displayText"]
        if msg.get("reasoningEffort"):
            p["reasoningEffort"] = msg["reasoningEffort"]
        result = await self._msp.command("turn/start", p)
        result["sessionId"] = session_id
        # First prompt in a bridge-created (still unnamed) session names
        # it from the initial text; best effort, never fails the turn.
        await self._auto_name_from_prompt(session_id, msg.get("text"))
        return result

    async def _auto_name_from_prompt(self, session_id, text):
        """Rename one pending session to its initial-prompt prefix.

        No-op unless the session is still in the auto-name set (created
        unnamed via this bridge, never explicitly renamed). The rename
        itself is best effort: failures log and leave the turn result
        untouched.
        """
        if session_id not in self._auto_name_pending:
            return None
        self._auto_name_pending.discard(session_id)
        name = default_session_name(text)
        if not name:
            return None
        try:
            await self._msp.command(
                "session/rename",
                {"sessionId": session_id, "name": name})
        except Exception as e:
            LOG.warning("auto-name failed for %s: %s", session_id, e)
            return None
        return name

    async def _do_new(self, conn, msg):
        p = {}
        for k in ("approvalMode", "modelId", "providerId", "sessionId",
                  "workspaceRoot", "workspaceRoots", "config"):
            if msg.get(k) is not None:
                p[k] = msg[k]
        # Remembered effort default (picker / /effort / /default-effort):
        # validated up front so a bad tier fails before creating the
        # session, applied after start via session/setReasoningEffort
        # (session/start takes no effort field on MSP v1).
        effort = msg.get("reasoningEffort")
        if effort is not None:
            if effort not in VALID_REASONING_EFFORTS:
                raise ValueError(
                    f"unknown reasoning effort {effort!r}; "
                    f"want one of {sorted(VALID_REASONING_EFFORTS)}")
        if (p.get("approvalMode") is not None
                and p["approvalMode"] not in VALID_APPROVAL_MODES):
            raise ValueError(
                "unknown approvalMode "
                f"{p['approvalMode']!r}; want one of "
                f"{sorted(VALID_APPROVAL_MODES)}")
        # Client-supplied roots are validated (must exist; nothing is
        # created outside the base). Auto-created workspaces below skip
        # this — they are made by session_workspace_dir.
        if msg.get("workspaceRoot") is not None:
            p["workspaceRoot"] = validate_manual_root(msg.get("workspaceRoot"))
        if isinstance(msg.get("workspaceRoots"), list):
            p["workspaceRoots"] = [validate_manual_root(r)
                                   for r in msg.get("workspaceRoots")]
        # Default workspace: every new session gets its own directory under
        # the base, unless the client named its own root(s). A client-made
        # sessionId is minted here when absent so the dir exists pre-start.
        auto_ws = False
        if self._workspace_base and p.get("workspaceRoot") is None \
                and p.get("workspaceRoots") is None:
            sid = p.get("sessionId") or uuid7()
            wsdir = session_workspace_dir(self._workspace_base, sid)
            p["sessionId"] = sid
            p["workspaceRoot"] = wsdir
            auto_ws = True
        # mcpAttach: resolve settings.json server names bridge-side (secrets
        # never cross the browser boundary); explicit config.mcpServers wins.
        attach = msg.get("mcpAttach")
        attached = []
        if attach:
            if not isinstance(attach, list) or not all(
                    isinstance(n, str) for n in attach):
                raise ValueError("mcpAttach must be a list of server names")
            built, warnings = build_session_mcp_config(attach)
            if warnings:
                LOG.warning("mcpAttach warnings: %s", "; ".join(warnings))
            if built.get("mcpServers"):
                merged = dict(built["mcpServers"])
                explicit = (p.get("config") or {}).get("mcpServers") or {}
                if isinstance(explicit, dict):
                    merged.update(explicit)
                p["config"] = dict(p.get("config") or {})
                p["config"]["mcpServers"] = merged
                attached = sorted(merged)
        result = await self._msp.command("session/start", p)
        if effort is not None:
            try:
                await self._msp.command(
                    "session/setReasoningEffort",
                    {"sessionId": result["session"]["sessionId"],
                     "reasoningEffort": effort})
            except Exception:
                LOG.warning("default effort %r not applied to session %s",
                            effort, result["session"]["sessionId"],
                            exc_info=True)
        self._attach(conn, result["session"]["sessionId"])
        # A fresh session under a previously deleted id exists again.
        self._deleted_sids.discard(result["session"]["sessionId"])
        # Started through this bridge (whatever the path): sort first.
        self._remember_bridge_sid(result["session"]["sessionId"])
        # Hosts that leave the name empty mark the session for default
        # naming on its first prompt; already-named sessions never enter.
        sid = result["session"]["sessionId"]
        if result["session"].get("name"):
            self._auto_name_pending.discard(sid)
        else:
            self._auto_name_pending.add(sid)
        if attached:
            result["mcpAttached"] = attached
        if p.get("workspaceRoot"):
            result["workspaceRoot"] = p["workspaceRoot"]
            # Every workspace-backed session learns the orders protocol
            # (best-effort skill seed) and is checked for orders after
            # each turn. Manual roots included: one hidden doc file.
            self._workspace_roots[sid] = p["workspaceRoot"]
            seed_orders_skill(p["workspaceRoot"])
            if auto_ws:
                # Bridge-owned session dir: plain AGENTS.md at the root.
                # Manual roots are the user's own dirs — no root-level
                # file is seeded there. (Repo sessions seed theirs in
                # _open_cloned, which routes through here too — but with
                # an explicit workspaceRoot, so auto_ws is False there.)
                seed_session_agents(p["workspaceRoot"])
        if p.get("workspaceRoot") and has_seeded_instructions(
                p.get("workspaceRoot")):
            # Rooted at a seeded workspace (githubOpen session dir, a
            # later `new --path` at the session dir or its clone leaf, or
            # a pre-rename WEB-MUSE.md leaf): gh/git approvals
            # auto-decide and the instruction file is already on disk.
            self._gh_auto_sids.add(sid)
        return result

    # -- GitHub clones (gh-only v1) -----------------------------------------
    def _clone_leaf(self, session_id, full_name=None):
        """`workspaces/<sid>/<repo>` leaf for a clone: contained and reusable.

        The leaf name is the repo slug (legacy sessions used "repo", kept
        as the fallback when no fullname is given). The leaf is created
        only by cloning, so a non-git residue is always a previous
        attempt's leftover: remove it so retries start clean.
        Raises ValueError (bad id / no workspace base) or GithubError
        (dest_exists when this session already holds a clone).
        """
        if not self._workspace_base:
            raise ValueError("github clone needs a workspace base "
                             "(bridge started with --workspace-base \"\")")
        wsdir = session_workspace_dir(self._workspace_base, session_id or "")
        leaf = Path(wsdir) / (repo_dir_name(full_name)
                              if full_name is not None else "repo")
        if leaf.is_dir() and (leaf / ".git").is_dir():
            raise GithubError(
                "dest_exists",
                f"session {session_id} already holds a clone at {leaf}")
        if leaf.exists() or leaf.is_symlink():
            shutil.rmtree(leaf, ignore_errors=True)
        return str(leaf)

    def _find_clone_leaf(self, session_id):
        """Locate one session's clone leaf, whatever its name (pure-ish).

        Returns the leaf Path (a direct child of the session dir holding
        `.git`), or None. Legacy "repo" leaves match the same way. Raises
        ValueError on a bad id or missing workspace base. Never raises on
        a missing session dir (None — nothing to clean).
        """
        sid = session_id or ""
        if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
                or sid in (".", ".."):
            raise ValueError(f"unsafe sessionId for github clean: {sid!r}")
        if not self._workspace_base:
            raise ValueError("github clean needs a workspace base")
        root = Path(self._workspace_base).resolve()
        wsdir = (root / sid).resolve()
        if wsdir.parent != root:
            raise ValueError(f"refusing to clean outside workspace: {sid!r}")
        if not wsdir.is_dir():
            return None
        try:
            children = sorted(p for p in wsdir.iterdir()
                              if p.is_dir() and not p.is_symlink()
                              and is_safe_leaf_name(p.name))
        except OSError:
            return None
        for child in children:
            if (child / ".git").is_dir():
                return child
        return None

    def _emit_clone_progress(self, conn, op_id, full_name, phase, line=None):
        """Push one githubCloneProgress event (global: no sessionId)."""
        try:
            params = {"opId": op_id, "fullName": full_name, "phase": phase}
            if line is not None:
                params["line"] = line
            conn.queue_frame({"type": "event",
                              "method": "githubCloneProgress",
                              "params": params})
        except Exception:
            LOG.warning("clone progress frame dropped for %s", full_name,
                        exc_info=True)

    def _check_op_id(self, op_id):
        if not isinstance(op_id, str) or not op_id.strip() \
                or len(op_id) > 128:
            raise ValueError("opId must be a 1..128 char string")
        return op_id.strip()

    def _launch_clone(self, conn, full_name, dest, op_id, session_id,
                      open_opts=None, branch=None):
        """Start the clone as a background task; reply accepted at once.

        The WS read loop awaits each handler serially, so awaiting a
        minutes-long clone inline would wedge the connection and make
        githubCancel unprocessable. Instead the outcome streams back as a
        global githubCloneResult event (ok + result, or ok False + error).
        Returns the accepted receipt; raises ValueError/GithubError on bad
        input before anything is launched.
        """
        self._emit_clone_progress(conn, op_id, full_name, "started")
        task = asyncio.create_task(
            self._clone_task(conn, full_name, dest, op_id, session_id,
                             open_opts, branch),
            name=f"github-clone-{op_id}")
        self._github_ops[op_id] = [task, conn, full_name]
        return {"accepted": True, "opId": op_id, "fullName": full_name,
                "dest": dest, "sessionId": session_id, "branch": branch}

    def _finish_op(self, op_id, task):
        """Take terminal-event ownership iff this task still owns the op."""
        rec = self._github_ops.get(op_id)
        if rec is not None and rec[0] is task:
            self._github_ops.pop(op_id, None)
            return rec
        return None

    async def _clone_task(self, conn, full_name, dest, op_id, session_id,
                          open_opts, branch=None):
        """Background clone body: progress, result event, cleanup."""
        me = asyncio.current_task()
        try:
            await run_gh_clone(
                full_name, dest, gh_bin=self._gh_bin, branch=branch,
                on_line=lambda line: self._emit_clone_progress(
                    conn, op_id, full_name, "progress", line))
            self._emit_clone_progress(conn, op_id, full_name, "completed")
            # Seed the leaf AGENTS.md into the fresh clone (both paths —
            # opened now or rooted later via `new --path`); skipped when
            # the repo ships its own. Seeding never fails the clone: on
            # error we log and report seededInstructions False.
            try:
                seeded, _ = seed_github_instructions(dest, full_name)
            except OSError:
                LOG.warning("instruction seeding failed for %s", dest,
                            exc_info=True)
                seeded = False
            if open_opts is None:
                result = {"fullName": full_name, "dest": dest,
                          "sessionId": session_id, "opId": op_id,
                          "seededInstructions": seeded}
            else:
                result = await self._open_cloned(
                    conn, full_name, dest, session_id, op_id, open_opts)
                result["seededInstructions"] = seeded
        except asyncio.CancelledError:
            # Whoever cancelled already owns the terminal event (see
            # _cancel_github_op); only emit when this task still owns it.
            if self._finish_op(op_id, me) is None:
                raise
            self._emit_clone_progress(conn, op_id, full_name, "cancelled")
            self._emit_clone_result(
                conn, op_id, False,
                error={"code": "clone_cancelled",
                       "message": f"clone of {full_name} cancelled"})
            return
        except GithubError as e:
            if self._finish_op(op_id, me) is None:
                return
            self._emit_clone_result(
                conn, op_id, False,
                error={"code": e.code, "message": str(e)})
            return
        except Exception as e:  # MspError from session/start and friends
            if self._finish_op(op_id, me) is None:
                return
            err = {"message": str(e)}
            if getattr(e, "code", None) is not None:
                err["code"] = e.code
            self._emit_clone_result(conn, op_id, False, error=err)
            return
        if self._finish_op(op_id, me) is None:
            return  # a canceller already emitted the terminal event
        self._emit_clone_result(conn, op_id, True, result=result)

    def _emit_clone_result(self, conn, op_id, ok, result=None, error=None):
        """Push the terminal githubCloneResult event (global)."""
        try:
            params = {"opId": op_id, "ok": ok}
            if result is not None:
                params["result"] = result
            if error is not None:
                params["error"] = error
            conn.queue_frame({"type": "event",
                              "method": "githubCloneResult",
                              "params": params})
        except Exception:
            LOG.warning("clone result frame dropped for op %s", op_id,
                        exc_info=True)

    async def _open_cloned(self, conn, full_name, dest, session_id, op_id,
                           open_opts):
        """session/start rooted at the session dir (open half of githubOpen).

        The agent works in ./<leaf>/ (named in the session AGENTS.md);
        the session root holds the bridge files (AGENTS.md, .web-muse/).
        The leaf AGENTS.md was seeded by _clone_task before this runs.
        """
        session_root = str(Path(dest).parent)
        leaf = Path(dest).name
        new_msg = {"workspaceRoot": session_root, "sessionId": session_id}
        if open_opts.get("mcpAttach") is not None:
            new_msg["mcpAttach"] = open_opts.get("mcpAttach")
        model = open_opts.get("model")
        if isinstance(model, dict) and isinstance(model.get("modelId"), str) \
                and model["modelId"].strip():
            new_msg["modelId"] = model["modelId"].strip()
            if isinstance(model.get("providerId"), str) \
                    and model["providerId"].strip():
                new_msg["providerId"] = model["providerId"].strip()
        if isinstance(open_opts.get("reasoningEffort"), str) \
                and open_opts["reasoningEffort"].strip():
            new_msg["reasoningEffort"] = \
                open_opts["reasoningEffort"].strip()
        if isinstance(open_opts.get("approvalMode"), str) \
                and open_opts["approvalMode"].strip():
            new_msg["approvalMode"] = open_opts["approvalMode"].strip()
        result = await self._do_new(conn, new_msg)
        # Session-root AGENTS.md naming the clone leaf (best-effort,
        # never overwrites — _do_new seeds only auto-created roots).
        seed_session_agents(session_root, leaf, full_name)
        self._gh_auto_sids.add(session_id)
        name = open_opts.get("name")
        if isinstance(name, str) and name.strip():
            await self._msp.command(
                "session/rename", {"sessionId": session_id,
                                   "name": name.strip()})
            if isinstance(result.get("session"), dict):
                result["session"]["name"] = name.strip()
            # Explicit name: the default fallback no longer applies.
            self._auto_name_pending.discard(session_id)
        result["fullName"] = full_name
        result["opId"] = op_id
        return result

    def _cancel_github_op(self, conn, op_id):
        """Cancel a running clone by opId (raises on unknown/finished op).

        The canceller takes terminal-event ownership and emits
        cancelled + clone_cancelled itself: a task cancelled before its
        first step never runs its body, so waiting on the task to report
        would leak the op with no terminal event.
        """
        op_id = self._check_op_id(op_id or "")
        rec = self._github_ops.get(op_id)
        task = rec[0] if rec else None
        if rec is None or task.done():
            if rec is not None and rec[0] is task:
                self._github_ops.pop(op_id, None)
            raise GithubError(
                "unknown_op",
                f"clone op {op_id!r} already finished"
                if task is not None and task.done() else
                f"no running clone op {op_id!r}")
        self._github_ops.pop(op_id, None)
        task.cancel()
        # Terminal events go to the originating conn (it holds the pending
        # UI state); the canceller gets the {cancelled} reply. Usually both
        # are the same client.
        origin = rec[1]
        self._emit_clone_progress(origin, op_id, rec[2], "cancelled")
        self._emit_clone_result(
            origin, op_id, False,
            error={"code": "clone_cancelled",
                   "message": f"clone of {rec[2]} cancelled"})
        return {"cancelled": True, "opId": op_id}

    def _do_github_clone(self, conn, msg):
        """Admit a clone into `workspaces/<sid>/<repo>/` (no MSP session).

        The caller then roots a session at the session dir via `new`
        (which runs the usual first-use confirm) or uses `githubOpen`
        for the combined step.
        """
        full_name = validate_fullname(msg.get("fullName"))
        sid = msg.get("sessionId") or uuid7()
        op_id = self._check_op_id(msg.get("opId") or uuid7())
        dest = self._clone_leaf(sid, full_name)
        branch = msg.get("branch")
        if branch is not None:
            branch = validate_branch(branch)
        return self._launch_clone(conn, full_name, dest, op_id, sid,
                                  branch=branch)

    def _do_github_open(self, conn, msg):
        """Admit a clone + `session/start` rooted at the session dir."""
        full_name = validate_fullname(msg.get("fullName"))
        sid = uuid7()
        op_id = self._check_op_id(msg.get("opId") or uuid7())
        dest = self._clone_leaf(sid, full_name)
        branch = msg.get("branch")
        if branch is not None:
            branch = validate_branch(branch)
        return self._launch_clone(conn, full_name, dest, op_id, sid,
                                  {"mcpAttach": msg.get("mcpAttach"),
                                   "name": msg.get("name"),
                                   "model": msg.get("model"),
                                   "reasoningEffort": msg.get("reasoningEffort"),
                                   "approvalMode": msg.get("approvalMode")},
                                  branch=branch)

    def _clean_github_clone(self, session_id):
        """Delete one session's clone leaf (raises outside the leaf).

        The leaf is found by `.git` presence, so named leaves and legacy
        "repo" leaves clean the same way. Only the clone leaf goes —
        the session dir (AGENTS.md, .web-muse/) is kept unless it ends
        up empty.
        """
        leaf = self._find_clone_leaf(session_id)
        sid = session_id or ""
        if leaf is None:
            if not self._workspace_base:
                raise ValueError("github clean needs a workspace base")
            return {"removed": False,
                    "dest": str(Path(self._workspace_base).resolve()
                                / sid)}
        if not is_safe_leaf_name(leaf.name):
            raise ValueError(
                f"refusing to clean outside clone leaf: {sid!r}")
        shutil.rmtree(leaf)
        try:  # drop the session dir too when the clone was all it held
            leaf.parent.rmdir()
        except OSError:
            pass
        return {"removed": True, "dest": str(leaf)}

    def _attach(self, conn, session_id):
        if session_id:
            self._subs.setdefault(session_id, set()).add(conn)
            conn.sessions.add(session_id)

    def _drop_routing(self, session_id):
        """Forget router state for a deleted session (subs, cursors...)."""
        members = self._subs.pop(session_id, set())
        for conn in members:
            conn.sessions.discard(session_id)
        self._cursors.pop(session_id, None)
        self._turns.pop(session_id, None)
        self._auto_name_pending.discard(session_id)
        self._gh_auto_sids.discard(session_id)
        if session_id in self._bridge_sids:
            self._bridge_sids.discard(session_id)
            self._save_bridge_sids()

    async def _resume_fallback(self, session_id, none_reason=None,
                               fallback=None):
        """Open a session when the host serves no session/resume
        (--no-session-log hosts; durable hosts serve it), or refuses to
        load it (retained permission profile the host cannot compose).

        Metadata comes from session/list (whose sessionId filter the host
        honors); history is honestly `none` — the host offers no transcript
        read-back, so the UI shows metadata and streams new turns live.
        Shape matches SessionResumeResult so clients don't branch.
        """
        if not session_id:
            raise ValueError("resume needs sessionId")
        listed = await self._msp.call(
            "session/list",
            {"filter": {"sessionId": {"anyOf": [session_id]}}, "limit": 5})
        sessions = listed.get("sessions", [])
        sess = next((s for s in sessions if s.get("sessionId") == session_id),
                    None)
        if sess is None:
            raise MspError(-32000, f"unknown session {session_id}")
        pending = {"approvals": [], "userInputs": []}
        try:
            pending = await self._msp.call(
                "approval/listPending", {"sessionId": session_id})
        except MspError:
            pass  # best effort; the UI re-fetches via /pending anyway
        return {
            "session": sess,
            "history": {"items": None, "mode": "none",
                        "noneReason": none_reason or "resume_unserved_by_host",
                        "snapshot": None},
            "pendingRequests": [
                {"kind": "approval", "approvalId": a.get("approvalId")}
                for a in pending.get("approvals", [])
                if a.get("approvalId")
            ] + [
                {"kind": "userInput", "userInputId": u.get("userInputId")}
                for u in pending.get("userInputs", [])
                if u.get("userInputId")
            ],
            "viewCursor": self.cursor(session_id),
            "fallback": fallback or "resume_unserved_by_host",
        }


def _pick(msg, keys):
    out = {}
    for k in keys:
        if k in msg and msg[k] is not None:
            out[k] = msg[k]
    return out
