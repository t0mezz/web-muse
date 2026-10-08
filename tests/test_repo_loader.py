"""Repo fetch loader: animated bars while `gh` lists repos/branches.

Opening the repo or branch menu fetches from the `gh` CLI; while that
round-trip runs the menu shows a small bar loader (Uiverse aryamitra06,
theme-matched) instead of a static "loading…" line. These tests pin the
wiring without a browser, following tests/test_github.py's
source-marker style.

Run: python3 -m unittest tests.test_repo_loader -v   (from repo root)
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class TestRepoLoader(unittest.TestCase):
    def test_loader_row_builder_present(self):
        # Builds the menu row: bars in front, visible status text behind.
        for marker in ("function repoLoadingRow(",
                       '"loader"', '"bar"', '"repo-loading-label"',
                       "loading repos…", "loading branches…"):
            self.assertIn(marker, APP_JS)
        builder = APP_JS[APP_JS.index("function repoLoadingRow("):
                         APP_JS.index("async function toggleRepoMenu")]
        self.assertNotIn('"vh"', builder)

    def test_loader_used_for_both_fetches(self):
        # Repo list and branch list each show it while `gh` responds.
        uses = re.findall(r"m\.append\(repoLoadingRow\(", APP_JS)
        self.assertGreaterEqual(len(uses), 2,
                                f"expected repo+branch loader, found {len(uses)}")

    def test_loader_styled_and_theme_driven(self):
        for marker in (".loader {", ".bar {", "@keyframes repo-bar-grow",
                       "animation: repo-bar-grow"):
            self.assertIn(marker, STYLE_CSS)
        # Theme roles only (the no-hex gate in test_theme pins this too),
        # shrunk to sit inside a menu row.
        bar_rule = STYLE_CSS[STYLE_CSS.index(".bar {"):]
        bar_rule = bar_rule[:bar_rule.index("}")]
        self.assertIn("var(--dim)", bar_rule)
        self.assertIn("height: 4px", bar_rule)
        self.assertIn(".repo-loading {", STYLE_CSS)
        self.assertIn(".repo-loading-label {", STYLE_CSS)

    def test_loading_text_matches_pill_text_size(self):
        # The "loading repos…" text reads at the "select a repo" size.
        def font_size(rule):
            block = STYLE_CSS[STYLE_CSS.index(rule):]
            block = block[:block.index("}")]
            return re.search(r"font-size:\s*([^;]+);", block).group(1).strip()
        self.assertEqual(font_size(".repo-loading-label {"),
                         font_size(".repo-pill {"))
        peak = STYLE_CSS[STYLE_CSS.index("@keyframes repo-bar-grow"):]
        self.assertIn("var(--fg)", peak[:500])

    def test_loader_respects_reduced_motion(self):
        self.assertIsNotNone(re.search(
            r"@media \(prefers-reduced-motion: reduce\) \{\s*"
            r"\.loader \.bar \{\s*animation: none;\s*\}\s*\}",
            STYLE_CSS))
