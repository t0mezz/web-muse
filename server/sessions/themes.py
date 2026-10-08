"""Theme palette validation: color roles, value shapes, theme keys."""

from __future__ import annotations

import re

from server.sessions.policy import MAX_PATTERN_LEN


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
    "accent", "focus", "ok", "warn", "err", "user", "agent",
    "select", "warnBg", "warnFg", "errFg", "codeBg", "cardBg",
    "pickedBg", "onOk", "onAccent", "chipInk", "light",
    "glow", "scrim", "star", "starBg0", "starBg1",
})


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
