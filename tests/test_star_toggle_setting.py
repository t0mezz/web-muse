"""Settings checkbox for the star toggle: the Appearance group owns a
native checkbox that shows/hides the floating starfield switch itself.

Contract (source level, following tests/test_settings.py — no JS
harness in this repo):

- Shell (web/settings.js): h.makeCheckbox builds a tagged
  input[type=checkbox] whose onchange pushes the checked state through
  the caller's setSetting; exported via helpers() like makeToggle.
- App (web/app.js): a persisted showStarToggle setting (default shown)
  with one canonical setter (applyShowStarTogglePick); the Appearance
  group renders the "Show star toggle" row with the checkbox.
- Style (web/style.css): .setting-checkbox sizing plus the shared
  focus ring and touch behavior.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
SETTINGS_JS = (ROOT / "web" / "settings.js").read_text()


def body_of(name, src=APP_JS):
    m = re.search(r"function " + name + r"\([^)]*\) \{(.*?)\n\}", src, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestStarToggleCheckboxShell(unittest.TestCase):
    def test_make_checkbox_builds_tagged_input(self):
        body = body_of("makeCheckbox", SETTINGS_JS)
        self.assertIn("document.createElement('input')", body)
        self.assertIn("box.type = 'checkbox'", body)
        self.assertIn("setting-checkbox", body)
        self.assertIn("tagControl(box, ariaLabel)", body)

    def test_onchange_pushes_checked_state(self):
        body = body_of("makeCheckbox", SETTINGS_JS)
        self.assertIn("box.onchange", body)
        self.assertIn("ev.target.checked", body)

    def test_helper_exported(self):
        self.assertIn("makeCheckbox: makeCheckbox", SETTINGS_JS)


class TestStarToggleCheckboxSetting(unittest.TestCase):
    def test_persisted_with_visible_default(self):
        self.assertIn('const STARS_TOGGLE_KEY = "web-muse:stars-toggle";', APP_JS)
        body = body_of("starToggleWanted")
        self.assertIn("localStorage.getItem(STARS_TOGGLE_KEY)", body)
        self.assertIn('return saved === "1";', body)
        self.assertIn("return true;", body)

    def test_canonical_setter_persists_and_syncs(self):
        body = body_of("applyShowStarTogglePick")
        self.assertIn("localStorage.setItem(STARS_TOGGLE_KEY,", body)
        self.assertIn("syncStarToggleVisibility();", body)

    def test_sync_hides_the_switch(self):
        body = body_of("syncStarToggleVisibility")
        self.assertIn('el("stars-toggle")', body)
        self.assertIn("starToggleWanted()", body)
        self.assertIn('"none"', body)
        self.assertIn("WebMuseSettings.sync()", body)

    def test_registered_through_value_registry(self):
        self.assertIn('defineSetting("showStarToggle"', APP_JS)
        self.assertIn('getSetting("showStarToggle")', APP_JS)
        self.assertIn('setSetting("showStarToggle"', APP_JS)

    def test_appearance_row_uses_checkbox(self):
        self.assertIn('h.row("Show star toggle",', APP_JS)
        self.assertIn("h.makeCheckbox(S.getSetting(\"showStarToggle\")", APP_JS)

    def test_applied_at_boot(self):
        self.assertIn("\nsyncStarToggleVisibility();\n", APP_JS)


class TestStarToggleCheckboxStyle(unittest.TestCase):
    def test_checkbox_rule(self):
        m = re.search(r"\.setting-checkbox \{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m, ".setting-checkbox rule missing")
        self.assertIn("width: 16px", m.group(1))
        self.assertIn("height: 16px", m.group(1))
        self.assertIn("accent-color: var(--accent)", m.group(1))

    def test_shared_focus_and_touch(self):
        self.assertIn(".setting-checkbox:focus-visible", STYLE_CSS)
        self.assertIn(".setting-checkbox", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
