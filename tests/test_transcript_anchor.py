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


class TestChatAlignment(unittest.TestCase):
    def test_user_lines_dock_right(self):
        m = re.search(r"\.tline\.user \{(.*?)\}", STYLE_CSS, re.S)
        self.assertIsNotNone(m, ".tline.user rule missing")
        self.assertIn("justify-content: flex-end;", m.group(1))

    def test_user_body_has_no_box_background(self):
        m = re.search(r"\.tline\.user \.body \{(.*?)\}", STYLE_CSS,
                      re.S)
        self.assertIsNotNone(m, ".tline.user .body rule missing")
        body = m.group(1)
        self.assertIn("background: none;", body)
        self.assertIn("max-width:", body)

    def test_agent_lines_stay_left(self):
        # Agent rows keep the default full-width left flow: no right
        # docking of any kind.
        m = re.search(r"\.tline\.agent \{(.*?)\}", STYLE_CSS, re.S)
        self.assertIsNotNone(m, ".tline.agent rule missing")
        self.assertNotIn("flex-end", m.group(1))
        self.assertNotIn("margin-left: auto", m.group(1))

    def test_composer_wrap_has_no_background_or_top_line(self):
        # The base rule (line-anchored: the welcome override also
        # mentions #composer-wrap).
        m = re.search(r"^#composer-wrap \{(.*?)\}", STYLE_CSS,
                      re.S | re.M)
        self.assertIsNotNone(m, "#composer-wrap rule missing")
        body = m.group(1)
        self.assertIn("background: none;", body)
        self.assertIn("border-top: none;", body)


if __name__ == "__main__":
    unittest.main()
