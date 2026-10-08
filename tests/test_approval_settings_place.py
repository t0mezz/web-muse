"""Approval settings live only in the Settings page.

The inspector Session tab keeps no approval controls: it shows the
current mode with a reference into Settings, where the full control
(select + allowAll confirm) lives. These tests pin the placement
without a browser, following tests/test_allowed.py's marker style.

Run: python3 -m unittest tests.test_approval_settings_place -v
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()

SESSION_HTML = INDEX_HTML[INDEX_HTML.index('id="tab-session"'):
                          INDEX_HTML.index('<aside id="settings"')]
SETTINGS_HTML = INDEX_HTML[INDEX_HTML.index('<aside id="settings"'):]


class TestApprovalSettingsPlace(unittest.TestCase):
    def test_session_tab_has_no_approval_controls(self):
        self.assertNotIn('id="approval-mode"', SESSION_HTML)
        self.assertNotIn('id="approval-warn"', SESSION_HTML)
        self.assertNotIn("approval-confirm-yes", SESSION_HTML)

    def test_defaults_group_owns_approval_controls(self):
        # Built by the Defaults group render (fresh nodes per render);
        # static markup carries none of them: exactly one approval home.
        for marker in ('id="approval-mode"', 'id="approval-warn"',
                       'id="approval-confirm-yes"',
                       'id="approval-confirm-no"', 'value="allowAll"'):
            self.assertIn(marker, APP_JS)
            self.assertNotIn(marker, INDEX_HTML)

    def test_session_tab_references_settings(self):
        self.assertIn('id="btn-approval-settings"', SESSION_HTML)
        self.assertIn('id="sess-approval-mode"', SESSION_HTML)
        self.assertIn('el("btn-approval-settings")', APP_JS)
        self.assertIn("WebMuseSettings.open()", APP_JS)

    def test_reference_label_follows_pick(self):
        m = re.search(r"function syncApprovalSelect\(\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "syncApprovalSelect missing")
        self.assertIn("sess-approval-mode", m.group(1))

    def test_reference_is_a_link_not_a_pill(self):
        self.assertIn(".approval-ref", STYLE_CSS)
        rule = STYLE_CSS[STYLE_CSS.index("#btn-approval-settings {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("border: none", rule)
        self.assertNotIn("border-radius", rule)
        self.assertIn("text-decoration: underline", STYLE_CSS)
