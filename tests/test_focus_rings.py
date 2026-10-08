"""Keyboard focus rings: themed :focus-visible on hover-only controls.

`.sess-open`/`.config-btn`/`.row-opt` already use an accent outline ring;
the remaining interactive controls showed only the browser-default ring.
The shared rule extends the same 2px idiom (no mouse behavior change)
to the topbar, toolbar, inspector tabs, approval cards, repo pills,
composer buttons, directory dialog, and long-output toggles.

The ring uses --focus, not --accent: accent only reaches ~1.5-2.3:1
on light surfaces (e.g. the parchment theme), below the WCAG 1.4.11
3:1 floor, so it would swap the browser-default ring for a dimmer
custom one. Each theme carries its own focus value (dark themes,
including the default, reuse their accent, which already passes;
light themes darken it); the contrast test below pins >= 3:1
against every surface role in every saved theme.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_mobile_rendering.py.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "web" / "style.css").read_text()
THEMES_DIR = ROOT / "web" / "themes"

# Every control that previously had :hover styling but no :focus-visible
# rule of its own. Text inputs keep their :focus border-color idiom.
EXPECTED = (
    "#topbar button:focus-visible",
    "#btn-progress:focus-visible",
    "#model-picker:focus-visible",
    "#effort-picker:focus-visible",
    ".side-head button:focus-visible",
    ".sess-toggle:focus-visible",
    "#btn-older:focus-visible",
    "#tx-filters button:focus-visible",
    "#jump-latest:focus-visible",
    ".tab:focus-visible",
    "#btn-close-inspector:focus-visible",
    "#approval-mode:focus-visible",
    ".card button:focus-visible",
    ".card .opt:focus-visible",
    ".row-btns button:focus-visible",
    ".repo-pill:focus-visible",
    ".repo-opt:focus-visible",
    ".composer-btns button:focus-visible",
    "#selection-ticker:focus-visible",
    ".out-toggle:focus-visible",
    ".dir-head button:focus-visible",
    ".dir-pathrow button:focus-visible",
    ".dir-navrow button:focus-visible",
    ".dir-crumbs button:focus-visible",
    ".dir-row:focus-visible",
    ".dir-foot button:focus-visible",
)

# (selector-text, body) for every :focus-visible rule in the stylesheet.
FOCUS_RULES = re.findall(
    r"([^{}]*:focus-visible[^{}]*)\{([^}]*)\}", CSS)


def bodies_for(sel):
    return [body for sels, body in FOCUS_RULES if sel in sels]


def _lum(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


# Surface roles a 2px outline at 1px offset can sit against: the
# control's own fill on the inner edge, its parent surface outside.
SURFACES = ("bg", "panel", "panel2", "cardBg", "codeBg", "select",
            "pickedBg", "warnBg", "user", "agent")


class TestFocusRings(unittest.TestCase):
    def test_every_control_has_focus_ring(self):
        for sel in EXPECTED:
            with self.subTest(sel=sel):
                bodies = bodies_for(sel)
                self.assertTrue(bodies, f"{sel} has no :focus-visible rule")
                for body in bodies:
                    self.assertIn("outline: 2px solid var(--focus)", body)

    def test_ring_never_suppressed(self):
        for sel in EXPECTED:
            with self.subTest(sel=sel):
                for body in bodies_for(sel):
                    self.assertNotIn("outline: none", body)

    def test_focus_holds_3_to_1_in_every_theme(self):
        files = sorted(THEMES_DIR.glob("*.json"))
        self.assertTrue(files, "web/themes/ holds no palettes")
        for path in files:
            theme = json.loads(path.read_text())
            # Missing keys fall back to theme.js defaults at apply
            # time (see web/themes/README.md), so a theme without its
            # own focus inherits the default one — which only passes
            # on light surfaces. Every saved theme must carry a focus
            # value verified against its own surfaces.
            self.assertIn("focus", theme, f"{path.name} has no focus key")
            # `light`/`line` excluded: chrome fills and 1px borders,
            # never the surface behind a ring (parents are bg, panel,
            # panel2, cardBg, or transparent-to-bg). No single hue holds
            # 3:1 against both near-black dark-theme surfaces and white,
            # and the bar only requires every *adjacent* color.
            for surface in SURFACES:
                with self.subTest(theme=path.name, surface=surface):
                    ratio = contrast(theme["focus"], theme[surface])
                    self.assertGreaterEqual(
                        ratio, 3.0,
                        f"{path.name} focus {theme['focus']} vs "
                        f"{surface} {theme[surface]}: {ratio:.2f}:1")


if __name__ == "__main__":
    unittest.main()
