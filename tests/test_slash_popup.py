"""Slash command preview: usage strings are space-padded to the longest
one shown so every description starts in the same column.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_stars.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


def _fn_body(name):
    m = re.search(r"function %s\(.*?\) \{(.*?)\n\}"
                  % re.escape(name), APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestSlashPopupAlignment(unittest.TestCase):
    def test_usages_padded_to_longest_shown(self):
        self.assertIn("Math.max(...list.map((c) => c.usage.length))", APP_JS)
        self.assertIn("c.usage.padEnd(width)", APP_JS)

    def test_padding_spaces_survive_rendering(self):
        # HTML collapses runs of spaces: without this the padding is lost.
        self.assertIn("white-space: pre;", STYLE_CSS)


class TestSlashPopupMobile(unittest.TestCase):
    def test_no_transcript_jerk_on_rerender(self):
        # scrollIntoView climbs every scrollable ancestor and jerks the
        # transcript on each keystroke (worst on Safari): the popup must
        # scroll itself via scrollTop math instead.
        body = _fn_body("updateSlashPopup")
        self.assertNotIn("scrollIntoView", body)
        self.assertIn("scrollSlashIntoView();", body)
        scroll = _fn_body("scrollSlashIntoView")
        self.assertIn("pop.scrollTop", scroll)

    def test_rerender_guard(self):
        # Same rows as shown (arrow-key walk, no-op skill refresh) must
        # only move the highlight, not tear down and rebuild the DOM.
        self.assertIn('slashKey: ""', APP_JS)
        body = _fn_body("updateSlashPopup")
        self.assertIn("state.slashKey === listKey", body)
        self.assertIn("createDocumentFragment", body)

    def test_narrow_skips_column_padding(self):
        # The padded usage column crushes the description on phones, so
        # narrow screens keep the raw usage for their stacked rows.
        body = _fn_body("updateSlashPopup")
        self.assertIn("window.innerWidth <= 560", body)
        self.assertIn("narrow ? c.usage : c.usage.padEnd(width)", body)

    def test_narrow_rows_stack(self):
        m560 = STYLE_CSS.split("@media (max-width: 560px)", 1)[1]
        self.assertIn(".slash-item", m560)
        self.assertIn("flex-direction: column", m560)
        self.assertIn("text-overflow: ellipsis", m560)


if __name__ == "__main__":
    unittest.main()
