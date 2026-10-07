"""Protected allowed-commands config + allowAll Session-panel wiring.

The bridge auto-approves shell commands from server/allowed_commands.json
(validated on load, never served over WS); the Session panel offers
allowAll ("approve all") behind an explicit warning + confirm.

Run: python3 -m unittest tests.test_allowed -v   (from repo root)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import (  # noqa: E402
    ALLOWED_COMMANDS_FILE,
    SessionRouter,
    _builtin_allowed_config,
    load_allowed_commands,
    shell_allowed,
    shell_auto_allowed,
)

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()

# Commands exercising every allow branch and every deny rule.
CORPUS = [
    "gh pr list", "gh repo delete octo/hello", "git status",
    "git push --force", "git reset --hard", "git clean -fd",
    "git branch -D old", "git stash drop", "git stash clear",
    "git push origin --delete old", "ls -la", "grep -f pat file",
    "find . -name x", "find . -delete", "find . -exec rm {} \\;",
    "curl https://example.com", "ssh host", "rm -rf /", "npm test",
    "python3 -c 'pass'", "echo hi", "make build", "cat file",
]


def _write_tmp(path, obj):
    Path(path).write_text(json.dumps(obj) if not isinstance(obj, str)
                           else obj)


class TestLoader(unittest.TestCase):
    def test_shipped_file_loads_clean(self):
        cfg, warnings = load_allowed_commands()
        self.assertEqual(warnings, [])
        self.assertTrue(cfg["allow"])
        self.assertTrue(cfg["deny"])

    def test_shipped_file_matches_builtins(self):
        shipped, _ = load_allowed_commands()
        builtin = _builtin_allowed_config()
        for cmd in CORPUS:
            self.assertEqual(shell_allowed(cmd, shipped),
                             shell_allowed(cmd, builtin), cmd)

    def test_default_wiring_uses_shipped_file(self):
        shipped, _ = load_allowed_commands()
        for cmd in CORPUS:
            self.assertEqual(
                shell_auto_allowed({"kind": "shell", "command": cmd}),
                shell_allowed(cmd, shipped), cmd)

    def test_missing_file_falls_back(self):
        cfg, warnings = load_allowed_commands("/no/such/allowed-xyz.json")
        self.assertTrue(warnings)
        builtin = _builtin_allowed_config()
        for cmd in CORPUS:
            self.assertEqual(shell_allowed(cmd, cfg),
                             shell_allowed(cmd, builtin), cmd)

    def test_bad_json_and_non_dict_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = str(Path(tmp) / "bad.json")
            _write_tmp(bad, "{nope")
            cfg, warnings = load_allowed_commands(bad)
            self.assertTrue(warnings)
            self.assertTrue(shell_allowed("ls", cfg))
            arr = str(Path(tmp) / "arr.json")
            _write_tmp(arr, "[1, 2]")
            cfg, warnings = load_allowed_commands(arr)
            self.assertTrue(warnings)
            self.assertTrue(shell_allowed("ls", cfg))

    def test_invalid_entries_dropped_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "cfg.json")
            _write_tmp(p, {
                "allow": ["^echo(\\s|$)", "(bad", 123, "", "x" * 600],
                "deny": [
                    {"commands": ["sh"], "patterns": ["^echo hi$"]},
                    {"commands": ["../escape"], "patterns": ["x"]},
                    {"nope": True},
                    {"commands": ["sh"], "patterns": "(bad"},
                ],
            })
            cfg, warnings = load_allowed_commands(p)
            self.assertTrue(warnings)
            self.assertTrue(shell_allowed("echo hi", cfg))
            self.assertFalse(shell_allowed("ls", cfg))

    def test_explicit_empty_allow_denies_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "cfg.json")
            _write_tmp(p, {"allow": [], "deny": []})
            cfg, warnings = load_allowed_commands(p)
            self.assertEqual(warnings, [])
            self.assertFalse(shell_allowed("ls", cfg))
            self.assertFalse(shell_allowed("gh pr list", cfg))

    def test_global_deny_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "cfg.json")
            _write_tmp(p, {"allow": ["^echo(\\s|$)"],
                           "deny": [{"patterns": ["lol"]}]})
            cfg, warnings = load_allowed_commands(p)
            self.assertEqual(warnings, [])
            self.assertTrue(shell_allowed("echo hi", cfg))
            self.assertFalse(shell_allowed("echo lol", cfg))

    def test_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = load_allowed_commands(tmp)  # a directory, not a file
            self.assertTrue(shell_allowed("ls", cfg))


class TestRouterConfig(unittest.TestCase):
    def test_router_uses_custom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "cfg.json")
            _write_tmp(p, {"allow": ["^echo(\\s|$)"], "deny": []})
            r = SessionRouter(msp=None, workspace_base=tmp,
                              allowed_commands_path=p)
            self.assertTrue(shell_auto_allowed(
                {"kind": "shell", "command": "echo hi"}, r._allowed))
            self.assertFalse(shell_auto_allowed(
                {"kind": "shell", "command": "ls"}, r._allowed))

    def test_router_bad_file_uses_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = str(Path(tmp) / "bad.json")
            _write_tmp(bad, "{nope")
            r = SessionRouter(msp=None, workspace_base=tmp,
                              allowed_commands_path=bad)
            self.assertTrue(shell_auto_allowed(
                {"kind": "shell", "command": "gh pr list"}, r._allowed))


class TestNoExposure(unittest.TestCase):
    def test_no_ws_route_serves_config(self):
        import re
        self.assertNotIn("allowedCommands", SESSIONS_PY)
        routes = re.findall(r'mtype == "([^"]+)"', SESSIONS_PY)
        self.assertTrue(routes)  # the dispatch table was found
        self.assertEqual([r for r in routes if "allow" in r.lower()], [])

    def test_config_lives_outside_web_dir(self):
        cfg_path = Path(ALLOWED_COMMANDS_FILE).resolve()
        web_dir = (ROOT / "web").resolve()
        self.assertNotEqual(cfg_path, web_dir)
        self.assertNotIn(web_dir, cfg_path.parents)
        self.assertTrue(cfg_path.is_file())


class TestAllowAllPanel(unittest.TestCase):
    def test_option_and_warning_markup(self):
        self.assertIn('value="allowAll"', INDEX_HTML)
        self.assertIn('id="approval-warn"', INDEX_HTML)
        # Warning icon, not a text prefix; the sentence stands alone.
        self.assertIn("M12 9.00006V13.0001", INDEX_HTML)
        self.assertIn("allowAll runs every command without asking",
                      INDEX_HTML)
        self.assertNotIn("Warning: allowAll", INDEX_HTML)

    def test_handler_confirms_and_syncs_warning(self):
        # No native dialog: selecting allowAll opens the inline confirm
        # panel in the same card; only its Enable button sends the mode.
        self.assertIn('mode === "allowAll"', APP_JS)
        self.assertIn("approval-confirm-yes", APP_JS)
        self.assertIn("approval-confirm-no", APP_JS)
        self.assertIn("syncApprovalWarn", APP_JS)
        self.assertIn("dataset.prev", APP_JS)
        self.assertIn('id="approval-confirm-yes"', INDEX_HTML)
        self.assertIn('id="approval-confirm-no"', INDEX_HTML)

    def test_warning_style_uses_theme(self):
        # Normal box (panel/line/fg), warning-colored icon, red allow btn.
        self.assertIn(".approval-warn", STYLE_CSS)
        self.assertIn("background: var(--panel)", STYLE_CSS)
        self.assertIn(".approval-warn svg", STYLE_CSS)
        self.assertIn("color: var(--warn)", STYLE_CSS)
        self.assertIn("#approval-confirm-yes", STYLE_CSS)
        self.assertIn("background: none; color: var(--err)",
                      STYLE_CSS)


class TestApprovalModeDefault(unittest.TestCase):
    """The Session-panel pick works with no session loaded: it is
    remembered (localStorage) and carried onto created sessions."""

    def test_remembered_default_plumbing(self):
        for marker in ("pickedApprovalMode", "webmuse.pickedApprovalMode",
                       "savePickedApprovalMode", "loadPickedApprovalMode"):
            self.assertIn(marker, APP_JS)

    def test_all_creation_paths_carry_default(self):
        # `new`, first-prompt, and githubOpen each forward the remembered
        # pick; three carry points, one shared shape.
        self.assertGreaterEqual(
            APP_JS.count("req.approvalMode = state.pickedApprovalMode"), 3)

    def test_no_session_pick_becomes_default(self):
        self.assertIn("new sessions", APP_JS)

    def test_bridge_forwards_approval_mode_on_open(self):
        self.assertIn('"approvalMode": msg.get("approvalMode")',
                      SESSIONS_PY)


class FakeMsp:
    """Records session/start params for creation-path tests."""

    def __init__(self):
        self.commands = []

    async def command(self, method, params=None, timeout=60):
        self.commands.append((method, dict(params or {})))
        if method == "session/start":
            return {"session": {"sessionId": "sid-new", "name": ""}}
        return {}

    async def call(self, method, params=None, timeout=60):
        return {}


class FakeConn:
    def __init__(self):
        self.sessions = set()
        self.frames = []

    def queue_frame(self, frame):
        self.frames.append(frame)


class TestApprovalModeBridge(unittest.IsolatedAsyncioTestCase):
    async def test_new_carries_approval_mode(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "new",
                         "approvalMode": "allowAll"})
        self.assertTrue(f["ok"], f)
        starts = [p for m, p in r._msp.commands
                  if m == "session/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].get("approvalMode"), "allowAll")

    async def test_new_rejects_unknown_mode(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "new",
                         "approvalMode": "never"})
        self.assertFalse(f["ok"])
        self.assertIn("approvalMode", f["error"]["message"])


if __name__ == "__main__":
    unittest.main()
