"""Inspector (right-panel) toggle: icon button plus Ctrl/Cmd+. shortcut.

Mirrors the sessions-panel toggle: `#btn-inspector` flips `.open` and the
global keydown handler maps Ctrl/Cmd+. to `toggleInspector()`. The button
title advertises the shortcut.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_session_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()


class TestInspectorToggle(unittest.TestCase):
    def test_icon_button_advertises_shortcut(self):
        self.assertIn('id="btn-inspector"', INDEX_HTML)
        self.assertIn("Ctrl+.", INDEX_HTML)

    def test_button_wired_to_toggle(self):
        self.assertIn('el("btn-inspector").onclick = toggleInspector;',
                      APP_JS)

    def test_toggle_flips_open_class(self):
        m = re.search(r"function toggleInspector\(\) \{(.*?)\n\}", APP_JS,
                      re.S)
        self.assertIsNotNone(m, "toggleInspector missing")
        self.assertIn('classList.toggle("open")', m.group(1))

    def test_ctrl_dot_shortcut(self):
        self.assertIn('} else if (k === ".") {', APP_JS)
        self.assertIn("toggleInspector();", APP_JS)


if __name__ == "__main__":
    unittest.main()
