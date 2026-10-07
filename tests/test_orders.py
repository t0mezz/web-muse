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
                       "theme.apply", "theme.save", "allowedCommands.update",
                       "bridge.restart",
                       "needsConfirm", "rejected", "Done when:"):
            self.assertIn(marker, SKILL_MD)

    def test_theme_quality_guidance(self):
        # Palette docs, value shapes, pair rules, contrast advice and
        # replace semantics are what lift agent themes above one-key
        # recolors; the doc must keep teaching them.
        for marker in ("glow", "scrim", "rgba(", "#rrggbb",
                       "Change paired roles together",
                       "Contrast (advisory",
                       "Replaces the whole theme",
                       "/theme default"):
            self.assertIn(marker, SKILL_MD)

    def test_embedded_defaults_match_theme_js(self):
        m = re.search(r"```json\n(.*?)```", SKILL_MD, re.S)
        self.assertIsNotNone(m, "embedded defaults palette missing")
        cm = re.search(r"var COLORS = \{(.*?)\};", THEME_JS, re.S)
        self.assertIsNotNone(cm, "COLORS map missing from theme.js")
        self.assertEqual(json.loads(m.group(1)),
                         dict(re.findall(r"^\s*([A-Za-z0-9]+):\s*'([^']+)'",
                                         cm.group(1), re.M)))

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
        tdir = str(Path(tmp) / "themes")
        r = SessionRouter(FakeMsp(), workspace_base=str(Path(tmp) / "ws"),
                          allowed_commands_path=cfg, themes_dir=tdir)
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

    async def test_bad_color_shapes_rejected_with_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            conn = FakeConn()
            r._conns.add(conn)
            write_orders(root, {"orders": [
                {"id": "s1", "action": "theme.apply",
                 "params": {"colors": {"accent": "olive"}}},
                {"id": "s2", "action": "theme.apply",
                 "params": {"colors": {"glow": "#AEAC78"}}},
                {"id": "s3", "action": "theme.apply",
                 "params": {"colors": {"scrim": "blue"}}},
                {"id": "s4", "action": "theme.apply",
                 "params": {"colors": {"accent": "#AEAC78",
                                       "glow": "174, 172, 120",
                                       "scrim": "rgba(241, 230, 209, 0.4)"}}},
            ]})
            await r._check_orders("sid-o1")
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            proc = receipt["processed"]
            self.assertEqual(proc["s1"]["status"], "rejected")
            self.assertIn("#rrggbb", proc["s1"]["reason"])
            self.assertEqual(proc["s2"]["status"], "rejected")
            self.assertIn("r, g, b", proc["s2"]["reason"])
            self.assertEqual(proc["s3"]["status"], "rejected")
            self.assertIn("rgba(", proc["s3"]["reason"])
            # Well-shaped values (incl. non-hex roles) still apply.
            self.assertEqual(proc["s4"]["status"], "applied")
            evs = self._events(conn, "themeApply")
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["colors"]["glow"], "174, 172, 120")

    async def test_theme_save_waits_for_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            conn = FakeConn()
            r._conns.add(conn)
            write_orders(root, {"orders": [
                {"id": "sv1", "action": "theme.save",
                 "params": {"name": "harbor-dusk",
                           "colors": {"bg": "#1A2334",
                                      "accent": "#6EA8FE"}}}]})
            await r._check_orders("sid-o1")
            # Held, not written: no file yet, card event emitted.
            self.assertEqual(list(Path(tmp, "themes").glob("*.json")), [])
            pend = self._events(conn, "ordersPending")
            self.assertEqual(len(pend), 1)
            self.assertTrue(pend[0]["needsConfirm"])
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            self.assertEqual(receipt["processed"]["sv1"]["status"],
                             "needsConfirm")

    async def test_theme_save_bad_name_and_shape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            write_orders(root, {"orders": [
                {"id": "n1", "action": "theme.save",
                 "params": {"name": "../evil",
                           "colors": {"bg": "#1A2334"}}},
                {"id": "n2", "action": "theme.save",
                 "params": {"name": "good-name",
                           "colors": {"bg": "not-a-color"}}},
                {"id": "n3", "action": "theme.save",
                 "params": {"colors": {"bg": "#1A2334"}}},
            ]})
            await r._check_orders("sid-o1")
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            proc = receipt["processed"]
            self.assertEqual(proc["n1"]["status"], "rejected")
            self.assertIn("name", proc["n1"]["reason"])
            self.assertEqual(proc["n2"]["status"], "rejected")
            self.assertIn("#rrggbb", proc["n2"]["reason"])
            self.assertEqual(proc["n3"]["status"], "rejected")

    async def test_theme_save_approve_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            conn = FakeConn()
            colors = {"bg": "#1A2334", "accent": "#6EA8FE",
                      "glow": "110, 168, 254"}
            write_orders(root, {"orders": [
                {"id": "sv1", "action": "theme.save",
                 "params": {"name": "harbor-dusk", "colors": colors}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "sv1",
                       "approved": True})
            self.assertTrue(reply["ok"], reply)
            saved = json.loads(
                (Path(tmp, "themes") / "harbor-dusk.json").read_text())
            self.assertEqual(saved, colors)
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            self.assertEqual(receipt["processed"]["sv1"]["status"],
                             "approved")

    async def test_theme_save_deny_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, root, _ = self._router(tmp)
            conn = FakeConn()
            write_orders(root, {"orders": [
                {"id": "sv1", "action": "theme.save",
                 "params": {"name": "harbor-dusk",
                           "colors": {"bg": "#1A2334"}}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "sv1",
                       "approved": False})
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(list(Path(tmp, "themes").glob("*.json")), [])
            receipt = json.loads(
                (Path(root) / ".web-muse" / "orders.receipt.json")
                .read_text())
            self.assertEqual(receipt["processed"]["sv1"]["status"], "denied")

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


class TestRestartOrders(unittest.IsolatedAsyncioTestCase):
    def _router(self, tmp, hook=None):
        cfg = str(Path(tmp) / "allowed.json")
        Path(cfg).write_text(json.dumps({"allow": ["^echo(\\s|$)"],
                                         "deny": []}))
        tdir = str(Path(tmp) / "themes")
        r = SessionRouter(FakeMsp(), workspace_base=str(Path(tmp) / "ws"),
                          allowed_commands_path=cfg, themes_dir=tdir,
                          on_restart=hook)
        for sid in ("sid-o1", "sid-o2"):
            root = str(Path(tmp) / f"wsroot-{sid}")
            Path(root).mkdir()
            r._workspace_roots[sid] = root
        return r

    def _events(self, conn, method):
        return [f["params"] for f in conn.frames
                if f.get("method") == method]

    def _receipt(self, tmp, sid):
        return json.loads(
            (Path(tmp) / f"wsroot-{sid}" / ".web-muse"
             / "orders.receipt.json").read_text())["processed"]

    async def test_bad_restart_params_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._router(tmp)
            write_orders(Path(tmp) / "wsroot-sid-o1", {"orders": [
                {"id": "r1", "action": "bridge.restart",
                 "params": {"reason": 42}},
                {"id": "r2", "action": "bridge.restart",
                 "params": {"reason": "x" * 501}},
                {"id": "r3", "action": "bridge.restart",
                 "params": {"delaySeconds": -1}},
                {"id": "r4", "action": "bridge.restart",
                 "params": {"delaySeconds": 31}},
                {"id": "r5", "action": "bridge.restart",
                 "params": {"delaySeconds": "2"}},
                {"id": "r6", "action": "bridge.restart",
                 "params": {"delaySeconds": True}},
                {"id": "r7", "action": "bridge.restart",
                 "params": {}},
            ]})
            await r._check_orders("sid-o1")
            proc = self._receipt(tmp, "sid-o1")
            for oid in ("r1", "r2"):
                self.assertEqual(proc[oid]["status"], "rejected")
                self.assertIn("reason", proc[oid]["reason"])
            for oid in ("r3", "r4", "r5", "r6"):
                self.assertEqual(proc[oid]["status"], "rejected")
                self.assertIn("delaySeconds", proc[oid]["reason"])
            # Bare params are valid: staged, never applied directly.
            self.assertEqual(proc["r7"]["status"], "needsConfirm")

    async def test_restart_waits_for_human_and_broadcasts(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            async def hook(delay):
                calls.append(delay)

            r = self._router(tmp, hook=hook)
            here, away = FakeConn(), FakeConn()
            away.sessions.add("sid-elsewhere")
            r._conns.update((here, away))
            write_orders(Path(tmp) / "wsroot-sid-o1", {"orders": [
                {"id": "r1", "action": "bridge.restart",
                 "params": {"reason": "server code changed",
                            "delaySeconds": 3}}]})
            await r._check_orders("sid-o1")
            self.assertEqual(calls, [])
            self.assertEqual(self._receipt(tmp, "sid-o1")["r1"]["status"],
                             "needsConfirm")
            # A restart affects every browser tab, so the card fans out to
            # all connections — including ones subscribed elsewhere.
            for conn in (here, away):
                pend = self._events(conn, "ordersPending")
                self.assertEqual(len(pend), 1)
                self.assertEqual(pend[0]["action"], "bridge.restart")
                self.assertTrue(pend[0]["needsConfirm"])
            self.assertEqual(self._events(here, "bridgeRestarting"), [])

    async def test_approve_runs_hook_and_marks_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            async def hook(delay):
                calls.append(delay)

            r = self._router(tmp, hook=hook)
            conn = FakeConn()
            r._conns.add(conn)
            write_orders(Path(tmp) / "wsroot-sid-o1", {"orders": [
                {"id": "r1", "action": "bridge.restart",
                 "params": {"delaySeconds": 0}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "r1",
                       "approved": True})
            self.assertTrue(reply["ok"], reply)
            self.assertTrue(reply["result"]["approved"])
            self.assertTrue(reply["result"]["restarting"])
            self.assertEqual(self._receipt(tmp, "sid-o1")["r1"]["status"],
                             "approved")
            restarting = self._events(conn, "bridgeRestarting")
            self.assertEqual(len(restarting), 1)
            self.assertEqual(restarting[0]["orderId"], "r1")
            await asyncio.sleep(0.2)
            self.assertEqual(calls, [0])

    async def test_deny_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            async def hook(delay):
                calls.append(delay)

            r = self._router(tmp, hook=hook)
            conn = FakeConn()
            r._conns.add(conn)
            write_orders(Path(tmp) / "wsroot-sid-o1", {"orders": [
                {"id": "r1", "action": "bridge.restart", "params": {}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "r1",
                       "approved": False})
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(self._receipt(tmp, "sid-o1")["r1"]["status"],
                             "denied")
            self.assertEqual(calls, [])
            self.assertEqual(self._events(conn, "bridgeRestarting"), [])
            # Deciding twice fails: staged orders are one-shot.
            again = await r.handle_client_message(
                conn, {"id": 2, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "r1",
                       "approved": True})
            self.assertFalse(again["ok"])

    def test_restart_environment_detector(self):
        import os
        from server.main import running_under_systemd
        old = dict(os.environ)
        try:
            os.environ.pop("INVOCATION_ID", None)
            os.environ.pop("JOURNAL_STREAM", None)
            self.assertFalse(running_under_systemd())
            os.environ["INVOCATION_ID"] = "abc123"
            self.assertTrue(running_under_systemd())
        finally:
            os.environ.clear()
            os.environ.update(old)

    async def test_approve_waits_out_grace_delay(self):
        # The grace delay must defer the hook past the approval reply:
        # approving must return while the restart is still pending, so
        # the reply frame flushes before the process exits.
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            async def hook(delay):
                calls.append(delay)

            r = self._router(tmp, hook=hook)
            conn = FakeConn()
            write_orders(Path(tmp) / "wsroot-sid-o1", {"orders": [
                {"id": "r1", "action": "bridge.restart",
                 "params": {"delaySeconds": 30}}]})
            await r._check_orders("sid-o1")
            reply = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersDecide",
                       "sessionId": "sid-o1", "orderId": "r1",
                       "approved": True})
            self.assertTrue(reply["ok"], reply)
            await asyncio.sleep(0.3)
            self.assertEqual(calls, [])

    async def test_orders_list_returns_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._router(tmp)
            conn = FakeConn()
            write_orders(Path(tmp) / "wsroot-sid-o1", {"orders": [
                {"id": "r1", "action": "bridge.restart", "params": {}}]})
            write_orders(Path(tmp) / "wsroot-sid-o2", {"orders": [
                {"id": "p1", "action": "allowedCommands.update",
                 "params": {"allow": ["^ls(\\s|$)"]}}]})
            await r._check_orders("sid-o1")
            await r._check_orders("sid-o2")
            all_orders = await r.handle_client_message(
                conn, {"id": 1, "type": "ordersList"})
            self.assertTrue(all_orders["ok"], all_orders)
            self.assertEqual(
                {(o["sessionId"], o["orderId"]) for o in
                 all_orders["result"]["orders"]},
                {("sid-o1", "r1"), ("sid-o2", "p1")})
            scoped = await r.handle_client_message(
                conn, {"id": 2, "type": "ordersList",
                       "sessionId": "sid-o1"})
            self.assertTrue(scoped["ok"], scoped)
            self.assertEqual(
                [o["orderId"] for o in scoped["result"]["orders"]], ["r1"])
            for o in scoped["result"]["orders"]:
                self.assertTrue(o["needsConfirm"])


class TestFrontendWiring(unittest.TestCase):
    def test_theme_apply_handler(self):
        self.assertIn('"themeApply"', APP_JS)
        self.assertIn("WebMuseTheme", APP_JS)

    def test_theme_apply_replaces_not_merges(self):
        # Wholesale replace (same as /theme <name>): the stored override
        # becomes exactly the order's colors, so no earlier-order colors
        # leak across switches. Revert path is advertised in the line.
        self.assertIn("localStorage.setItem(T.storageKey, "
                      "JSON.stringify(clean))", APP_JS)
        self.assertNotIn("Object.assign(merged, clean)", APP_JS)
        self.assertIn("/theme default", APP_JS)

    def test_theme_value_shapes_checked(self):
        # Frontend twin of the bridge's theme_color_error: hex roles,
        # glow triplet, scrim rgba.
        self.assertIn("themeValueOk", APP_JS)
        self.assertIn("rgba(", APP_JS)

    def test_orders_card_and_decide(self):
        self.assertIn('"ordersPending"', APP_JS)
        self.assertIn("ordersDecide", APP_JS)

    def test_restart_and_resync_wiring(self):
        # Restart progress event, staged-order resync, and the armed
        # two-click restart confirm all have frontend handlers.
        self.assertIn('"bridgeRestarting"', APP_JS)
        self.assertIn('"ordersList"', APP_JS)
        self.assertIn('"bridge.restart"', APP_JS)
        self.assertIn("Confirm restart", APP_JS)


if __name__ == "__main__":
    unittest.main()
