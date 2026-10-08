"""Agent orders channel: files, receipts, validation, staged actions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from server.sessions.policy import MAX_PATTERN_LEN, SAFE_COMMAND_NAME, _compile_patterns
from server.sessions.themes import THEME_KEYS, THEME_NAME_RE, _valid_theme_value

LOG = logging.getLogger("web_muse.sessions")


# Agent orders channel: agents cannot write outside their workspace, so
# app customization arrives as `.web-muse/orders.json` files (protocol in
# server/orders_skill.md, seeded into every session workspace). The bridge
# checks the file on streamed turn events while the turn is still running
# (gated on file change) and again after each turn, validates every
# order, and acts:
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
    Path(__file__).resolve().parent.parent / "orders_skill.md")
# Saved themes live under <repo>/web/themes; an approved theme.save order
# writes one file there so it appears under bare `/theme`.
DEFAULT_THEMES_DIR = (
    Path(__file__).resolve().parent.parent.parent / "web" / "themes")

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
