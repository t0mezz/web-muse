"""Frontend color scheme: every major color lives in web/theme.js.

theme.js holds the current colors as defaults (= fallback), applies them
as :root custom properties, and accepts JSON overrides from
localStorage["web-muse:theme"]. These tests pin that contract without a
browser: theme defaults match the active palette, no stylesheet
grows hex literals outside its :root fallback block, and every page
loads theme.js before the scripts that read it.

Run: python3 -m unittest tests.test_theme -v   (from repo root)
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
THEME_JS = (ROOT / "web" / "theme.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
APP_JS = (ROOT / "web" / "app.js").read_text()
STARS_JS = (ROOT / "web" / "stars.js").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()
PROGRESS_HTML = (ROOT / "web" / "progress.html").read_text()

HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")

def theme_colors():
    """Parse the COLORS map out of web/theme.js."""
    m = re.search(r"var COLORS = \{(.*?)\};", THEME_JS, re.S)
    assert m, "COLORS map missing from theme.js"
    pairs = re.findall(r"^\s*([A-Za-z0-9]+):\s*'([^']+)'",
                       m.group(1), re.M)
    return dict(pairs)


def root_vars(css):
    """Parse the first :root { ... } fallback block of a stylesheet."""
    m = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert m, "no :root fallback block found"
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", m.group(1)))


def without_root(css):
    """Stylesheet text with the :root fallback block removed."""
    return re.sub(r":root\s*\{.*?\}", "", css, count=1, flags=re.S)


class TestThemeConfig(unittest.TestCase):
    def test_config_shape(self):
        colors = theme_colors()
        self.assertGreaterEqual(len(colors), 20)  # every major color
        for key in ("bg", "panel", "fg", "dim", "accent", "ok", "warn",
                    "err", "user", "agent", "select", "warnBg", "warnFg",
                    "errFg", "codeBg", "cardBg", "pickedBg", "onOk",
                    "onAccent", "chipInk", "light", "glow", "scrim",
                    "star", "starBg0", "starBg1"):
            self.assertIn(key, colors)
        self.assertIn("WebMuseTheme", THEME_JS)
        self.assertIn("web-muse:theme", THEME_JS)  # override storage key

    # Active palette (Color Hunt fcf0daaeac78f2c46a4c4541 + supporting
    # shades mixed from its extremes; see web/theme.js).
    PALETTE = {
        "bg": "#FCF0DA", "panel": "#F1E6D1", "panel2": "#E9DDC9",
        "line": "#D5CAB8", "fg": "#4C4541", "dim": "#736B63",
        "faint": "#81786F", "accent": "#AEAC78", "ok": "#78735A",
        "warn": "#F2C46A", "err": "#8B7551", "user": "#F6D89C",
        "agent": "#E9DDC9", "select": "#F6D89C", "warnBg": "#F4CC7E",
        "warnFg": "#71614A", "errFg": "#77664C", "codeBg": "#EEE2CE",
        "cardBg": "#F8DEAD", "pickedBg": "#F6D697", "onOk": "#FCF0DA",
        "onAccent": "#4C4541", "chipInk": "#4C4541", "light": "#fff",
        "glow": "174, 172, 120", "scrim": "rgba(241, 230, 209, 0.4)",
        "star": "#4C4541", "starBg0": "#F7DAA2", "starBg1": "#FCF0DA",
    }

    def test_theme_matches_palette(self):
        """theme.js defaults are exactly the active palette."""
        self.assertEqual(theme_colors(), self.PALETTE)

    def test_no_hex_outside_style_root(self):
        """Every style.css hex outside :root is a pinned fallback twin.

        The only literals left are progressive-enhancement fallbacks,
        each immediately overridden by its var() twin so the runtime
        value is theme-driven: `color: #fff` (pinned by
        test_copy_button) and `--uib-color: #6ea8fe` (pinned by
        test_thinking_status).
        """
        rest = without_root(STYLE_CSS)
        self.assertEqual(sorted(HEX_RE.findall(rest)),
                         ["#6ea8fe", "#fff"])
        self.assertIn("color: #fff; color: var(--light);", rest)
        self.assertIn("--uib-color: #6ea8fe; --uib-color: var(--accent);",
                      rest)

    def test_progress_fallback_keys(self):
        # progress.html keeps its own (older) :root fallback values by
        # design — stylesheets are not re-themed, theme.js overrides them
        # at load. This pins the key set so a renamed role is noticed.
        root = {k: v.strip() for k, v in root_vars(PROGRESS_HTML).items()}
        for var in ("bg", "panel", "line", "fg", "dim", "ok", "warn",
                    "err", "accent", "faint", "codeBg"):
            self.assertIn(var, root, f"progress :root --{var} missing")
        self.assertEqual(HEX_RE.findall(without_root(PROGRESS_HTML)), [])

    def test_pages_load_theme_first(self):
        theme_pos = INDEX_HTML.index("/theme.js")
        self.assertLess(theme_pos, INDEX_HTML.index("/stars.js"))
        self.assertLess(theme_pos, INDEX_HTML.index("/app.js"))
        self.assertIn("/theme.js", PROGRESS_HTML)

    def test_js_reads_theme_with_literal_fallback(self):
        # stars.js: theme roles with the current colors as fallback; the
        # full gradient literal stays as the fallback constant (pinned by
        # test_stars).
        for key, fallback in (("star", "#fff"),
                              ("starBg0", "#1a2334"),
                              ("starBg1", "#0c0e12")):
            self.assertIn(f"themeColor('{key}', '{fallback}')", STARS_JS)
        self.assertIn(
            "radial-gradient(ellipse at bottom, #1a2334 0%, #0c0e12 100%)",
            STARS_JS)
        self.assertIn("WebMuseTheme", STARS_JS)
        # app.js: starfield hue literal (pinned by test_stars), re-sourced
        # from the theme right after.
        self.assertIn('starColor: "#e6e9ef"', APP_JS)
        self.assertIn('WebMuseTheme.get("fg", "#e6e9ef")', APP_JS)


if __name__ == "__main__":
    unittest.main()
