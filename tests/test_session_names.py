"""Session default names: initial-prompt prefix as a fallback.

The bridge names a bridge-created session from the first characters of
its initial prompt (same convention as the muse TUI). Explicit renames
take precedence: a renamed session never gets auto-renamed later.

Run: python3 -m unittest tests.test_session_names -v   (from repo root)
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.msp import MspError  # noqa: E402
from server.sessions import (  # noqa: E402
    DEFAULT_SESSION_NAME_LEN,
    SessionRouter,
    default_session_name,
)

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()


class FakeConn:
    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class FakeMsp:
    """Host stub: blank names on start, records commands."""

    def __init__(self, start_name="", fail_rename=False):
        self.commands = []
        self.start_name = start_name
        self.fail_rename = fail_rename

    async def command(self, method, params=None):
        self.commands.append((method, dict(params or {})))
        if method == "session/start":
            sid = (params or {}).get("sessionId") or "sid-new"
            return {"session": {"sessionId": sid, "name": self.start_name,
                                "status": "active"}}
        if method == "session/rename":
            if self.fail_rename:
                raise MspError(-32603, "rename boom")
            return {"ok": True}
        if method == "turn/start":
            return {"turnId": "t1"}
        raise AssertionError(f"unexpected command {method}")

    async def call(self, method, params=None):
        raise AssertionError(f"unexpected call {method}")

    def renames(self):
        return [p for m, p in self.commands if m == "session/rename"]


class TestDefaultSessionName(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(default_session_name("fix auth loop"),
                         "fix auth loop")

    def test_long_text_truncates(self):
        text = "x" * (DEFAULT_SESSION_NAME_LEN + 20)
        self.assertEqual(default_session_name(text),
                         "x" * DEFAULT_SESSION_NAME_LEN)

    def test_whitespace_collapses(self):
        self.assertEqual(default_session_name("  hello\n\n  world  "),
                         "hello world")

    def test_multiline_joins_first_words(self):
        self.assertEqual(default_session_name("first line\nsecond line"),
                         "first line second line")

    def test_blank_kinds_are_empty(self):
        for blank in ("", "   ", "\n\t ", None, 123, ["x"]):
            self.assertEqual(default_session_name(blank), "",
                             msg=repr(blank))

    def test_custom_limit(self):
        self.assertEqual(default_session_name("hello world", limit=5),
                         "hello")


class TestAutoName(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_prompt_auto_names(self):
        msp = FakeMsp()
        router = SessionRouter(msp=msp, workspace_base=None)
        text = ("Fix the auth redirect loop when the token expires "
                "during a long session with extra words")
        reply = await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "prompt", "text": text})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([m for m, _ in msp.commands],
                         ["session/start", "turn/start", "session/rename"])
        rename = msp.renames()[0]
        self.assertEqual(rename["sessionId"], "sid-new")
        self.assertEqual(rename["name"], default_session_name(text))
        self.assertEqual(len(rename["name"]), DEFAULT_SESSION_NAME_LEN)

    async def test_eager_new_named_on_first_prompt_only(self):
        msp = FakeMsp()
        router = SessionRouter(msp=msp, workspace_base=None)
        fresh = await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "new", "sessionId": "sid-1"})
        self.assertTrue(fresh["ok"], fresh)
        self.assertEqual(msp.renames(), [])
        first = await router.handle_client_message(
            FakeConn(), {"id": 2, "type": "prompt",
                         "sessionId": "sid-1", "text": "hello world"})
        self.assertTrue(first["ok"], first)
        self.assertEqual([r["name"] for r in msp.renames()], ["hello world"])
        second = await router.handle_client_message(
            FakeConn(), {"id": 3, "type": "prompt",
                         "sessionId": "sid-1", "text": "another topic"})
        self.assertTrue(second["ok"], second)
        self.assertEqual(len(msp.renames()), 1)  # no second rename

    async def test_explicit_rename_wins(self):
        msp = FakeMsp()
        router = SessionRouter(msp=msp, workspace_base=None)
        await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "new", "sessionId": "sid-1"})
        renamed = await router.handle_client_message(
            FakeConn(), {"id": 2, "type": "rename",
                         "sessionId": "sid-1", "name": "Mine"})
        self.assertTrue(renamed["ok"], renamed)
        prompt = await router.handle_client_message(
            FakeConn(), {"id": 3, "type": "prompt",
                         "sessionId": "sid-1", "text": "other words here"})
        self.assertTrue(prompt["ok"], prompt)
        # Only the explicit rename; the default fallback stays out.
        self.assertEqual([r["name"] for r in msp.renames()], ["Mine"])

    async def test_foreign_session_never_renamed(self):
        msp = FakeMsp()
        router = SessionRouter(msp=msp, workspace_base=None)
        reply = await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "prompt",
                         "sessionId": "tui-made", "text": "hello world"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(msp.renames(), [])

    async def test_host_named_session_never_renamed(self):
        msp = FakeMsp(start_name="HostName")
        router = SessionRouter(msp=msp, workspace_base=None)
        reply = await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "prompt",
                         "text": "hello world"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(msp.renames(), [])

    async def test_rename_failure_keeps_turn(self):
        msp = FakeMsp(fail_rename=True)
        router = SessionRouter(msp=msp, workspace_base=None)
        reply = await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "prompt", "text": "hello world"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["sessionId"], "sid-new")
        self.assertEqual(len(msp.renames()), 1)  # attempted, then swallowed

    async def test_blank_prompt_sends_no_rename(self):
        # Whitespace-only text still starts a turn (build_turn_input
        # treats it as text), but it must never become an empty name.
        msp = FakeMsp()
        router = SessionRouter(msp=msp, workspace_base=None)
        await router.handle_client_message(
            FakeConn(), {"id": 1, "type": "new", "sessionId": "sid-1"})
        reply = await router.handle_client_message(
            FakeConn(), {"id": 2, "type": "prompt",
                         "sessionId": "sid-1", "text": "   "})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(msp.renames(), [])


class TestSessionNameUI(unittest.TestCase):
    def test_display_fallback_helper(self):
        for marker in ("function sessionDisplayName",
                       "function titleForSession"):
            self.assertIn(marker, APP_JS)

    def test_rows_and_filter_use_display_name(self):
        for marker in ("sessionDisplayName(s).toLowerCase()",
                       "nm.textContent = sessionDisplayName(s)",
                       "el(\"session-title\").textContent = "
                       "sessionDisplayName(state.session);"):
            self.assertIn(marker, APP_JS)

    def test_bridge_auto_name_seam(self):
        for marker in ("default_session_name", "_auto_name_from_prompt",
                       "_auto_name_pending"):
            self.assertIn(marker, SESSIONS_PY)

    def test_rename_reconciles_bar(self):
        # The host applies renames asynchronously: renameSession records
        # the admitted name and re-applies it over every refresh until the
        # host list catches up — otherwise the bar lagged one rename
        # behind the title.
        for marker in ("pendingNames", "applyPendingNames",
                       "state.pendingNames.set(sid",
                       "session/nameChanged"):
            self.assertIn(marker, APP_JS)


if __name__ == "__main__":
    unittest.main()
