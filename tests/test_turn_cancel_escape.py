"""Turn cancel on Escape: pressing Escape cancels the running turn.

- `cmdCancel` sends the bridge `cancel` (turn/cancel) for the current
  session/turn.
- The composer Escape branch dismisses popups AND cancels when a turn
  runs (guarded by `state.running` so idle Escape stays quiet).
- A document-level Escape covers presses outside the composer (but not
  while the new-session directory dialog owns Escape).

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_inspector_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()


def fn_body(name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestCmdCancel(unittest.TestCase):
    def test_cmd_cancel_sends_cancel(self):
        body = fn_body("cmdCancel")
        self.assertIn('"cancel"', body)
        self.assertIn("sessionId", body)


class TestEscapeCancelsTurn(unittest.TestCase):
    def test_composer_escape_cancels_when_running(self):
        m = re.search(
            r'el\("input"\)\.addEventListener\("keydown", \(ev\) => \{(.*?)\n\}\)',
            APP_JS, re.S)
        self.assertIsNotNone(m, "composer keydown missing")
        body = m.group(1)
        self.assertIn('"Escape"', body)
        self.assertIn("cmdCancel", body)
        self.assertIn("state.running", body)

    def test_document_escape_cancels_outside_composer(self):
        m = re.search(
            r'document\.addEventListener\("keydown", \(ev\) => \{(.*?)\n\}\)',
            APP_JS, re.S)
        self.assertIsNotNone(m, "document keydown missing")
        body = m.group(1)
        self.assertIn('"Escape"', body)
        self.assertIn("cmdCancel", body)


if __name__ == "__main__":
    unittest.main()
