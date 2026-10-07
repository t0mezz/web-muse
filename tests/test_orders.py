"""Agent orders channel: workspace dropbox the bridge acts on.

Agents cannot write outside their workspace, so customization requests
arrive as `.web-muse/orders.json` files (see server/orders_skill.md,
seeded into every session workspace). The bridge validates each order:
`theme.apply` executes at once (fanned out as a themeApply event),
`allowedCommands.update` waits for a human's ordersDecide, and anything
else is rejected with a reason in the receipt file.

Run: python3 -m unittest tests.test_orders -v   (from repo root)
"""

import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import SessionRouter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()
APP_JS = (ROOT / "web" / "app.js").read_text()
SKILL_MD = (ROOT / "server" / "orders_skill.md").read_text()
THEME_JS = (ROOT / "web" / "theme.js").read_text()


def theme_keys():
    m = re.search(r"var COLORS = \{(.*?)\};", THEME_JS, re.S)
    assert m, "COLORS map missing from theme.js"
    return set(re.findall(r"^\s*([A-Za-z0-9]+):", m.group(1), re.M))


class FakeMsp:
    def __init__(self):
        self.commands = []

    async def command(self, method, params=None, timeout=60):
        self.commands.append((method, dict(params or {})))
        if method == "session/start":
            return {"session": {"sessionId": "sid-o1", "name": ""}}
        return {}

    async def call(self, method, params=None, timeout=60):
        return {}


class FakeConn:
    def __init__(self):
        self.sessions = set()
        self.frames = []

    def queue_frame(self, frame):
        self.frames.append(frame)


def write_orders(root, payload):
    odir = Path(root) / ".web-muse"
    odir.mkdir(parents=True, exist_ok=True)
    (odir / "orders.json").write_text(json.dumps(payload))


class TestSkillDoc(unittest.TestCase):
    def test_protocol_markers(self):
        for marker in (".web-muse/orders.json", ".web-muse/orders.receipt.json",
                       "theme.apply", "allowedCommands.update",
                       "needsConfirm", "rejected", "Done when:"):
            self.assertIn(marker, SKILL_MD)

    def test_seed_writes_every_workspace(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                r = SessionRouter(FakeMsp(), workspace_base=tmp)
                conn = FakeConn()
                reply = await r.handle_client_message(
                    conn, {"id": 1, "type": "new", "sessionId": "sid-seed"})
                self.assertTrue(reply["ok"], reply)
                skill = Path(tmp) / "sid-seed" / ".web-muse" / "ORDERS.md"
                self.assertTrue(skill.is_file())
                self.assertIn("theme.apply", skill.read_text())
                # Workspace root remembered for order checks
                # (resolved: macOS /tmp symlinks to /private/tmp; the
                # host echoes its own session id, so read it from the
                # reply rather than assuming the requested one).
                import os
                rsid = reply["result"]["session"]["sessionId"]
                self.assertEqual(r._workspace_roots.get(rsid),
                                 os.path.realpath(Path(tmp) / "sid-seed"))
                # Seeding never overwrites an existing skill file.
                skill.write_text("mine")
                await r.handle_client_message(
                    conn, {"id": 2, "type": "new", "sessionId": "sid-seed"})
                self.assertEqual(skill.read_text(), "mine")
        asyncio.run(body())


class TestThemeKeys(unittest.TestCase):
    def test_bridge_knows_every_theme_key(self):
        m = re.search(r"THEME_KEYS = frozenset\(\{(.*?)\}\)", SESSIONS_PY,
                      re.S)
        self.assertIsNotNone(m, "THEME_KEYS missing from sessions.py")
        bridge_keys = set(re.findall(r'"([A-Za-z0-9]+)"', m.group(1)))
        self.assertEqual(bridge_keys, theme_keys())


class TestOrdersEngine(unittest.IsolatedAsyncioTestCase):
    def _router(self, tmp):
        cfg = str(Path(tmp) / "allowed.json")
        Path(cfg).write_text(json.dumps({"allow": ["^echo(\\s|$)"],
                                         "deny": []}))
        r = SessionRouter(FakeMsp(), workspace_base=str(Path(tmp) / "ws"),
                          allowed_commands_path=cfg)
        root = str(Path(tmp) / "wsroot")
        Path(root).mkdir()
        r._workspace_roots["sid-o1"] = root
        return r, root, cfg

    def _events(self, conn, method):
        return [f["params"] for f in conn.frames
                if f.get("method") == method]

    async def test_theme_apply_executes_and_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            conn = FakeConn()
            r._conns.add(conn)
            write_orders(root, {"orders": [
                {"id": "t1", "action": "theme.apply",
                 "params": {"colors": {"accent": "#AEAC78"}}}]})
            # Route the completion the way the host notification does.
            r.on_notification("turn/completed", {"sessionId": "sid-o1",
                                                 "turnId": "t"})
            await asyncio.sleep(0.2)
            evs = self._events(conn, "themeApply")
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["colors"], {"accent": "#AEAC78"})
            self.assertEqual(evs[0]["orderId"], "t1")
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            self.assertEqual(receipt["processed"]["t1"]["status"],
                             "applied")

    async def test_rejections_carry_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            conn = FakeConn()
            r._conns.add(conn)
            write_orders(root, {"orders": [
                {"id": "b1", "action": "theme.apply",
                 "params": {"colors": {"nope": "#fff"}}},
                {"id": "b2", "action": "launch.missiles",
                 "params": {}},
                {"id": "", "action": "theme.apply",
                 "params": {"colors": {"accent": "#fff"}}},
            ]})
            await r._check_orders("sid-o1")
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            proc = receipt["processed"]
            self.assertEqual(proc["b1"]["status"], "rejected")
            self.assertIn("nope", proc["b1"]["reason"])
            self.assertEqual(proc["b2"]["status"], "rejected")
            # Re-checking never re-fires receipted ids (no new events).
            await r._check_orders("sid-o1")
            self.assertEqual(self._events(conn, "themeApply"), [])

    async def test_policy_update_waits_for_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, cfg = self._router(tmp)
            conn = FakeConn()
            r._conns.add(conn)
            before = Path(cfg).read_text()
            write_orders(root, {"orders": [
                {"id": "p1", "action": "allowedCommands.update",
                 "params": {"allow": ["^ls(\\s|$)"]}}]})
            await r._check_orders("sid-o1")
            # Held, not applied: file untouched, card event emitted.
            self.assertEqual(Path(cfg).read_text(), before)
            pend = self._events(conn, "ordersPending")
            self.assertEqual(len(pend), 1)
            self.assertTrue(pend[0]["needsConfirm"])
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            self.assertEqual(receipt["processed"]["p1"]["status"],
                             "needsConfirm")

    async def test_approve_rewrites_policy_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, cfg = self._router(tmp)
            conn = FakeConn()
            write_orders(root, {"orders": [
                {"id": "p1", "action": "allowedCommands.update",
                 "params": {"allow": ["^ls(\\s|$)"]}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "p1",
                       "approved": True})
            self.assertTrue(reply["ok"], reply)
            saved = json.loads(Path(cfg).read_text())
            self.assertEqual(saved["allow"], ["^ls(\\s|$)"])
            from server.sessions import shell_auto_allowed
            self.assertTrue(shell_auto_allowed(
                {"kind": "shell", "command": "ls -la"}, r._allowed))
            self.assertFalse(shell_auto_allowed(
                {"kind": "shell", "command": "echo hi"}, r._allowed))

    async def test_deny_and_unknown_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, cfg = self._router(tmp)
            conn = FakeConn()
            before = Path(cfg).read_text()
            write_orders(root, {"orders": [
                {"id": "p1", "action": "allowedCommands.update",
                 "params": {"allow": ["^ls(\\s|$)"]}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "p1",
                       "approved": False})
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(Path(cfg).read_text(), before)
            bad = await r.handle_client_message(
                conn, {"id": 2, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "nope",
                       "approved": True})
            self.assertFalse(bad["ok"])


class TestFrontendWiring(unittest.TestCase):
    def test_theme_apply_handler(self):
        self.assertIn('"themeApply"', APP_JS)
        self.assertIn("WebMuseTheme", APP_JS)

    def test_orders_card_and_decide(self):
        self.assertIn('"ordersPending"', APP_JS)
        self.assertIn("ordersDecide", APP_JS)


if __name__ == "__main__":
    unittest.main()
