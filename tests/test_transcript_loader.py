"""Generating indicator: flip-loader row while a turn runs.

The loader shows on turn/started, swaps out for real content on the
first content item/started, and clears when the turn settles
(completed/retracted). Animation freezes under prefers-reduced-motion.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_session_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


def case_body(marker):
    m = re.search(r'case "' + marker + r'":(.*?)break;', APP_JS, re.S)
    assert m, f'case "{marker}" missing'
    return m.group(1)


class TestLoaderAssets(unittest.TestCase):
    def test_loader_css_and_keyframes_present(self):
        self.assertIn(".loader {", STYLE_CSS)
        self.assertIn(".loader::before {", STYLE_CSS)
        for kf in ("@keyframes l6-0 {", "@keyframes l6-0-0 {",
                   "@keyframes l6-1 {"):
            self.assertIn(kf, STYLE_CSS)

    def test_loader_freezes_under_reduced_motion(self):
        m = re.search(
            r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}",
            STYLE_CSS, re.S)
        self.assertIsNotNone(m, "reduced-motion block missing")
        self.assertIn(".loader", m.group(1))


class TestLoaderWiring(unittest.TestCase):
    def test_helpers_exist(self):
        self.assertIn("function showLoader()", APP_JS)
        self.assertIn("function hideLoader()", APP_JS)
        self.assertIn('id = "loader-row"', APP_JS)

    def test_turn_started_shows_loader(self):
        self.assertIn("showLoader();", case_body("turn/started"))

    def test_loader_sits_where_answer_prints(self):
        m = re.search(r"function showLoader\(\) \{(.*?)\n\}", APP_JS,
                      re.S)
        self.assertIsNotNone(m, "showLoader missing")
        body = m.group(1)
        self.assertIn('"tline agent generating"', body)
        self.assertIn('"txt"', body)

    def test_loader_is_text_sized(self):
        self.assertIn(".tline.generating .loader", STYLE_CSS)

    def test_first_content_item_hides_loader(self):
        body = case_body("item/started")
        self.assertIn("hideLoader();", body)
        # System chatter must not dismiss it: only content rows swap it.
        self.assertIn('!== "system"', body)

    def test_settled_turns_hide_loader(self):
        for marker in ("turn/completed", "turn/retracted"):
            self.assertIn("hideLoader();", case_body(marker),
                          f"{marker} leaves the loader behind")


if __name__ == "__main__":
    unittest.main()
