"""Code-block copy buttons: every fenced block gets a small transparent
copy/check button pinned top-right, clipboard write with fallback and
check feedback that reverts after a delay.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_stars.py.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class TestCopyButton(unittest.TestCase):
    def test_buttons_added_on_markdown_render(self):
        self.assertIn("addCodeCopyButtons(rec.body)", APP_JS)
        self.assertIn("function addCodeCopyButtons(root) {", APP_JS)

    def test_button_shape_and_labels(self):
        self.assertIn('btn.className = "copy-btn";', APP_JS)
        self.assertIn('aria-label", "Copy code to clipboard"', APP_JS)
        self.assertIn('aria-label", "Copied"', APP_JS)
        # One button per block, never doubled on re-render.
        self.assertIn("code-wrap", APP_JS)

    def test_clipboard_with_fallback(self):
        self.assertIn("navigator.clipboard.writeText(text)", APP_JS)
        self.assertIn('document.execCommand("copy")', APP_JS)

    def test_check_feedback_reverts(self):
        self.assertIn("btn.innerHTML = CHECK_SVG;", APP_JS)
        self.assertIn("btn.innerHTML = COPY_SVG;", APP_JS)
        self.assertIn("}, 3000);", APP_JS)

    def test_pinned_top_right_transparent_white(self):
        self.assertIn(".copy-btn {", STYLE_CSS)
        self.assertIn("position: absolute; top: 6px; right: 6px;", STYLE_CSS)
        self.assertIn("background: transparent;", STYLE_CSS)
        self.assertIn("color: #fff;", STYLE_CSS)
        self.assertIn(".tline .txt.md .code-wrap { position: relative; }", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
