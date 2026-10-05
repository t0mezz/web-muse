"""Session-panel toggle: Ctrl+B / icon button, desktop-persistent by default.

The panel used to be open-only on mobile (btn-menu) and force-visible on
desktop via CSS. It is now toggled by `#btn-sessions` and Ctrl/Cmd+B on
both, `.open` is the single visibility switch, desktop starts open, and
opening a session no longer auto-collapses the desktop panel.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_userinput_card.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class TestSessionToggle(unittest.TestCase):
    def test_icon_button_exists_and_menu_is_gone(self):
        self.assertIn('id="btn-sessions"', INDEX_HTML)
        self.assertIn("Ctrl+B", INDEX_HTML)
        self.assertNotIn("btn-menu", INDEX_HTML)
        self.assertNotIn("btn-menu", APP_JS)
        self.assertNotIn("btn-menu", STYLE_CSS)

    def test_button_wired_to_toggle(self):
        self.assertIn('el("btn-sessions").onclick = toggleSessions;',
                      APP_JS)

    def test_toggle_flips_open_class(self):
        m = re.search(r"function toggleSessions\(\) \{(.*?)\n\}", APP_JS,
                      re.S)
        self.assertIsNotNone(m, "toggleSessions missing")
        body = m.group(1)
        self.assertIn('classList.contains("open")', body)
        self.assertIn('classList.remove("open")', body)
        self.assertIn('classList.add("open")', body)

    def test_ctrl_b_shortcut(self):
        self.assertRegex(APP_JS, r"addEventListener\(\"keydown\"")
        self.assertIn("ev.ctrlKey || ev.metaKey", APP_JS)
        self.assertIn('if (k === "b") {', APP_JS)
        self.assertIn("toggleSessions();", APP_JS)

    def test_desktop_starts_closed(self):
        # First-run default lives in restorePanelState (persistence owns
        # initial visibility): both bars start hidden on every width.
        m = re.search(r"function restorePanelState\(\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "restorePanelState missing")
        self.assertNotIn('classList.add("open")', m.group(1))

    def test_open_class_is_the_visibility_switch(self):
        self.assertIn("#sessions.open { display: flex; }", STYLE_CSS)
        desktop = re.search(
            r"@media \(min-width: 900px\) \{(.*?)\n\}", STYLE_CSS, re.S)
        self.assertIsNotNone(desktop)
        sessions_rule = re.search(r"#sessions \{(.*?)\}",
                                  desktop.group(1), re.S)
        self.assertIsNotNone(sessions_rule)
        self.assertNotIn("display", sessions_rule.group(1))

    def test_close_drawer_is_mobile_only(self):
        m = re.search(r"function closeDrawer\(\) \{(.*?)\n\}", APP_JS,
                      re.S)
        self.assertIsNotNone(m, "closeDrawer missing")
        self.assertIn("if (!isNarrow()) return;", m.group(1))


if __name__ == "__main__":
    unittest.main()
