"""Slash command preview: usage strings are space-padded to the longest
one shown so every description starts in the same column.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_stars.py.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class TestSlashPopupAlignment(unittest.TestCase):
    def test_usages_padded_to_longest_shown(self):
        self.assertIn("Math.max(...list.map((c) => c.usage.length))", APP_JS)
        self.assertIn("c.usage.padEnd(width)", APP_JS)

    def test_padding_spaces_survive_rendering(self):
        # HTML collapses runs of spaces: without this the padding is lost.
        self.assertIn("white-space: pre;", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
