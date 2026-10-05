"""Transcript bottom-anchoring: short chats sit above the composer.

New lines print at the bottom of the pane; history pushes up as lines
arrive; the pane scrolls once content exceeds the viewport. Anchoring
uses margin-top:auto on the first line, not justify-content on the pane
(which strands the top out of scroll reach when overflowing).

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_session_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class TestTranscriptAnchor(unittest.TestCase):
    def test_first_line_pinned_to_bottom(self):
        m = re.search(r"#terminal > :first-child \{(.*?)\}", STYLE_CSS,
                      re.S)
        self.assertIsNotNone(m, "bottom-anchor rule missing")
        self.assertIn("margin-top: auto;", m.group(1))

    def test_pane_stays_scrollable_from_top(self):
        # justify-content on an overflowing flex pane strands the top
        # out of scroll reach; the anchor must not use it.
        m = re.search(r"#terminal \{(.*?)\}", STYLE_CSS, re.S)
        self.assertIsNotNone(m, "#terminal rule missing")
        self.assertNotIn("justify-content", m.group(1))

    def test_pane_is_column_flow_with_overflow(self):
        m = re.search(r"#terminal \{(.*?)\}", STYLE_CSS, re.S)
        self.assertIsNotNone(m, "#terminal rule missing")
        body = m.group(1)
        self.assertIn("flex-direction: column;", body)
        self.assertIn("overflow-y: auto;", body)


if __name__ == "__main__":
    unittest.main()
