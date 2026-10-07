"""Client effort default: medium unless the user picked otherwise.

The topbar picker, /effort, and /default-effort remember the pick, but a
fresh profile (or a retired stored tier) falls back to medium — creation
paths then always carry an explicit reasoningEffort.

Run: python3 -m unittest tests.test_effort_client -v   (from repo root)
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()


class TestEffortMediumDefault(unittest.TestCase):
    def test_initial_and_fallback_is_medium(self):
        self.assertIn('pickedEffort: "medium"', APP_JS)
        self.assertIn("? raw : \"medium\"", APP_JS)

    def test_clear_and_placeholder_reset_to_medium(self):
        self.assertIn("Default reasoning effort reset to medium.", APP_JS)
        self.assertIn("default effort → medium", APP_JS)
        # The old effort fallbacks are gone (model strings may still
        # mention the host default — out of scope here).
        self.assertNotIn("Default reasoning effort cleared", APP_JS)
        self.assertNotIn("default effort cleared", APP_JS)


if __name__ == "__main__":
    unittest.main()
