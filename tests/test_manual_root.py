"""Manual session directories (stage 1 of external sessions).

Bridge: client-supplied workspaceRoot/workspaceRoots must be existing
directories — the bridge creates nothing outside its own workspace base.
UI (source-level, no JS harness): /new --path parsing, + button path
prompt, and the first-use confirm gate before session/start.

Run: python3 -m unittest tests.test_manual_root -v   (from repo root)
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import validate_manual_root  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()


class TestValidateManualRoot(unittest.TestCase):
    def test_existing_dir_passes_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            out = validate_manual_root(d)
            self.assertEqual(out, str(Path(d).resolve()))

    def test_missing_dir_rejected(self):
        with self.assertRaises(ValueError):
            validate_manual_root("/no/such/dir-xyz-123")

    def test_file_not_dir_rejected(self):
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(ValueError):
                validate_manual_root(f.name)

    def test_empty_and_non_string_rejected(self):
        for bad in ("", "   ", None, 123, ["x"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_manual_root(bad)

    def test_bridge_is_never_asked_to_create(self):
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / "newdir"
            with self.assertRaises(ValueError):
                validate_manual_root(str(target))
            self.assertFalse(target.exists())

    def test_wired_into_new_handler(self):
        self.assertIn("validate_manual_root(msg.get(\"workspaceRoot\"))",
                      SESSIONS_PY)
        self.assertIn("validate_manual_root(r)", SESSIONS_PY)


class TestManualRootUI(unittest.TestCase):
    def test_path_flag_forms(self):
        self.assertIn('"--path"', APP_JS)
        self.assertIn('"--path="', APP_JS)
        self.assertIn("out.workspaceRoot = workspaceRoot;", APP_JS)

    def test_usage_mentions_path(self):
        self.assertIn("/new [name] [--mcp a,b] [--path <dir>]", APP_JS)

    def test_first_use_confirm_gate(self):
        for marker in ("webmuse.allowedRoots", "function ensureRootConfirmed",
                       "window.confirm(`Allow this session to access",
                       "req.workspaceRoot = root;"):
            self.assertIn(marker, APP_JS)
        # Unconfirmed roots never reach the wire.
        self.assertIn("session not created: directory not confirmed", APP_JS)

    def test_plus_button_asks_for_directory(self):
        m = re.search(r'el\("btn-new"\)\.onclick = \(\) => \{(.*?)\n\};',
                      APP_JS, re.S)
        self.assertIsNotNone(m, "btn-new handler missing")
        body = m.group(1)
        self.assertIn('prompt("Session directory', body)
        self.assertIn("workspaceRoot: p", body)


if __name__ == "__main__":
    unittest.main()
