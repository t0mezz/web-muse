"""Manual session directories (stage 1 of external sessions).

Bridge: client-supplied workspaceRoot/workspaceRoots must be existing
directories — the bridge creates nothing outside its own workspace base.
UI (source-level, no JS harness): /new --path parsing, + button path
prompt, and the first-use confirm gate before session/start.

Run: python3 -m unittest tests.test_manual_root -v   (from repo root)
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import browse_dir, browse_home, validate_manual_root  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
SESSIONS_PY = "".join(p.read_text() for p in sorted((ROOT / "server" / "sessions").glob("*.py")))


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

    def test_plus_button_opens_explorer(self):
        for marker in ("openDirDialog()", 'el("btn-new").onclick',
                       "dir-use", "dir-default", "dir-path",
                       "newSession(undefined, { workspaceRoot: p })"):
            self.assertIn(marker, APP_JS)
        # The old blocking prompt is gone; picking flows through the
        # explorer's "Use this folder" into the same confirm gate.
        self.assertNotIn('prompt("Session directory', APP_JS)

    def test_explorer_lists_via_browse(self):
        for marker in ("type: \"browse\"", "function loadDir",
                       "function renderDir", "function renderCrumbs",
                       "dir-crumbs", "dir-list"):
            self.assertIn(marker, APP_JS)
        self.assertIn('"browse"', SESSIONS_PY)


class TestBrowseDir(unittest.TestCase):
    def test_empty_path_opens_home(self):
        home = browse_home()
        self.assertTrue(home.startswith("/"))
        r = browse_dir("")
        self.assertEqual(r["path"], home)
        self.assertIn("entries", r)

    def test_dirs_first_files_shown_hidden_shown(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "zebra").mkdir()
            (base / "apple").mkdir()
            (base / "mfile.txt").write_text("x")
            (base / ".hidden").mkdir()
            (base / ".hfile").write_text("y")
            r = browse_dir(d)
            names = [e["name"] for e in r["entries"]]
            self.assertIn(".hidden", names)
            self.assertIn(".hfile", names)
            self.assertIn("mfile.txt", names)
            kinds = {e["name"]: e["isDir"] for e in r["entries"]}
            self.assertTrue(kinds["apple"])
            self.assertFalse(kinds["mfile.txt"])
            # Dirs-first, case-insensitive alphabetical within each group.
            self.assertEqual(names, [".hidden", "apple", "zebra",
                                     ".hfile", "mfile.txt"])
            for e in r["entries"]:
                self.assertEqual(e["isHidden"], e["name"].startswith("."))
            self.assertEqual(r["parent"], str(base.parent))

    def test_missing_file_and_relative_rejected(self):
        with self.assertRaises(ValueError):
            browse_dir("/no/such/dir-xyz-123")
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(ValueError):
                browse_dir(f.name)
        with self.assertRaises(ValueError):
            browse_dir("relative/path")


if __name__ == "__main__":
    unittest.main()
