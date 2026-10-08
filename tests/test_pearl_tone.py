"""Pearl repo pill follows the surface tone: light palettes retune the gloss.

The pearl gloss is baked for dark surfaces (faint white highlights,
black shade). app.js marks the live tone on body[data-pearl] from the
panel luminance, and style.css carries a light-tuned recipe behind
that hook. These tests pin the wiring without a browser, following
tests/test_github.py's source-marker style.

Run: python3 -m unittest tests.test_pearl_tone -v   (from repo root)
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
THEMES_DIR = ROOT / "web" / "themes"


def luminance(hex_color):
    """sRGB relative luminance of a #rgb/#rrggbb color (mirrors pearlToneFor)."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    h = h[:6]
    chans = []
    for i in (0, 2, 4):
        v = int(h[i:i + 2], 16) / 255
        chans.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


class TestPearlTone(unittest.TestCase):
    def test_tone_helpers_present(self):
        for marker in ("function pearlToneFor(panel)",
                       "function syncPearlTone()",
                       "document.body.dataset.pearl"):
            self.assertIn(marker, APP_JS)
        # The 0.3 threshold is the tone contract (see the gap test below).
        self.assertRegex(APP_JS, r"return lum > 0\.3 \? \"light\" : \"dark\";")

    def test_tone_synced_on_every_theme_path(self):
        # /theme apply, agent theme.apply orders, the settings default
        # reset, and boot must all re-mark the tone, or a theme switch
        # would leave the previous recipe behind.
        calls = re.findall(r"syncPearlTone\(\);", APP_JS)
        self.assertGreaterEqual(len(calls), 4,
                                f"expected 4+ sync sites, found {len(calls)}")

    def test_light_recipe_behind_tone_hook(self):
        for selector in ('body[data-pearl="light"] .repo-pill {',
                         'body[data-pearl="light"] .repo-pill:hover {',
                         'body[data-pearl="light"] .repo-pill:active {'):
            self.assertIn(selector, STYLE_CSS)

    def test_light_recipe_has_no_glow(self):
        # Light palettes get a flat pill: the pearl sheen layers stay
        # off and no white highlight layer may hide in the light
        # recipe (the dark recipe above keeps its own glow).
        start = STYLE_CSS.index('body[data-pearl="light"] .repo-pill {')
        end = STYLE_CSS.index("@media (prefers-reduced-motion: reduce)",
                              start)
        light_block = STYLE_CSS[start:end]
        self.assertIn("display: none;", light_block)
        self.assertNotIn("255, 255, 255", light_block)

    def test_shipped_panels_leave_a_threshold_gap(self):
        # Guards the 0.3 threshold: a future mid-tone panel landing near
        # it would make the tone flip ambiguous, so flag it here.
        for path in sorted(THEMES_DIR.glob("*.json")):
            with self.subTest(theme=path.name):
                panel = json.loads(path.read_text())["panel"]
                lum = luminance(panel)
                self.assertTrue(lum < 0.15 or lum > 0.6,
                                f"{path.name} panel {panel} lum {lum:.3f} "
                                "sits near the 0.3 tone threshold")
