"""Panel-state persistence: toggles survive reload via localStorage.

Every panel mutator saves both panels' `.open` state; page init restores
the stored state, falling back to the width-based defaults only when
nothing was stored yet.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_session_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()


def body_of(name):
    m = re.search(r"function " + name + r"\(\) \{(.*?)\n\}", APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestPanelPersistence(unittest.TestCase):
    def test_storage_keys(self):
        self.assertIn('"webmuse.sessionsOpen"', APP_JS)
        self.assertIn('"webmuse.inspectorOpen"', APP_JS)

    def test_every_mutator_saves(self):
        for fn in ("toggleSessions", "closeDrawer", "toggleInspector",
                   "closeInspector"):
            self.assertIn("savePanelState();", body_of(fn),
                          f"{fn} does not persist")

    def test_save_writes_both_panels(self):
        body = body_of("savePanelState")
        self.assertIn("localStorage.setItem", body)
        self.assertIn("PANEL_KEYS.sessions", body)
        self.assertIn("PANEL_KEYS.inspector", body)

    def test_restore_prefers_stored_over_defaults(self):
        body = body_of("restorePanelState")
        self.assertIn("localStorage.getItem", body)
        # Stored state wins...
        self.assertIn('classList.toggle("open", s === "1")', body)
        self.assertIn('classList.toggle("open", insp === "1")', body)
        # ...first run keeps the old width defaults.
        self.assertIn(
            'if (!isNarrow()) el("sessions").classList.add("open");',
            body)
        self.assertIn(
            'if (window.innerWidth >= 1300) '
            'el("inspector").classList.add("open");', body)

    def test_init_restores_instead_of_width_only(self):
        self.assertIn("restorePanelState();", APP_JS)


if __name__ == "__main__":
    unittest.main()
