"""Plugin listing: bridge `plugins` -> MSP `plugin/list` plus the `/plugins`
slash command.

Mirrors tests/test_skills_preview.py: the recent skill-preview commit wired
skill/list selectors into the `/` popup, but installed plugin records had no
surface at all (`unknown type 'plugins'`), so plugins were never shown.
"""

import unittest
from pathlib import Path

from server.sessions import SessionRouter

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()

SID = "01a10797-3751-7dd0-8396-7eeba49b5ebb"


class FakeMsp:
    def __init__(self):
        self.calls = []

    async def call(self, method, params=None, timeout=60):
        self.calls.append((method, dict(params or {})))
        if method == "plugin/list":
            return {"plugins": [{"id": "acme", "version": "1.0.0",
                                 "enabled": True, "source": "marketplace"}]}
        raise AssertionError(f"unexpected call {method}")

    async def command(self, method, params=None, timeout=60):
        raise AssertionError(f"unexpected command {method}")


class FakeConn:
    def __init__(self):
        self.sessions = set()
        self.frames = []

    def queue_frame(self, obj):
        self.frames.append(obj)


class TestPluginsPassthrough(unittest.IsolatedAsyncioTestCase):
    async def test_plugins_forwards_to_plugin_list(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "plugins", "sessionId": SID})
        self.assertTrue(f["ok"], f)
        self.assertIn("plugins", f["result"])
        self.assertEqual(f["result"]["plugins"][0]["id"], "acme")
        self.assertEqual(r._msp.calls[0],
                         ("plugin/list", {"sessionId": SID}))


class TestPluginsCommand(unittest.TestCase):
    def test_slash_lists_plugins(self):
        self.assertIn('"plugins"', APP_JS)
        self.assertIn("cmdPlugins()", APP_JS)

    def test_plugins_sends_plugin_list_request(self):
        self.assertIn('type: "plugins"', APP_JS)
        self.assertIn("r.plugins", APP_JS)


if __name__ == "__main__":
    unittest.main()
