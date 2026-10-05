"""File-based session delete: rm -rf the store dirs, never the host.

`/delete` removes the dated main dir (sessions/YYYY/MM/DD/<sessionId>/)
plus the view-store dir (sessions/.msp-view-v1/<sessionId>/) from disk
instead of calling session/delete (the host registry rejects back-to-back
deletes with Store(Busy)). The bridge tombstones removed ids so list and
resume read as gone while the running host still lists them from memory.

Run: python3 -m unittest tests.test_session_rm_delete -v   (from repo root)
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import SessionRouter, delete_session_files  # noqa: E402


def mkstore(store, sid, dated="2026/10/05"):
    main = Path(store, *dated.split("/"), sid)
    main.mkdir(parents=True)
    (main / "session.jsonl").write_text("{}\n")
    view = Path(store, ".msp-view-v1", sid)
    view.mkdir(parents=True)
    (view / "HEAD.json").write_text("{}")
    return main, view


class FakeConn:
    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class SilentMsp:
    """Host that must never receive deletes; static list rows."""

    def __init__(self, rows):
        self.rows = rows

    async def command(self, method, params=None):
        raise AssertionError(f"delete path must not call host: {method}")

    async def call(self, method, params=None):
        assert method == "session/list"
        return {"sessions": [dict(r) for r in self.rows]}


class StartMsp(SilentMsp):
    """SilentMsp that also serves session/start (for re-create tests)."""

    async def command(self, method, params=None):
        if method == "session/start":
            return {"session": {"sessionId": params.get("sessionId")}}
        raise AssertionError(f"unexpected host command: {method}")


class TestDeleteSessionFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = str(Path(tmp.name) / "store")

    def test_removes_main_and_view_dirs(self):
        main, view = mkstore(self.store, "gone-sid")
        removed = delete_session_files("gone-sid", base=self.store)
        self.assertEqual(sorted(removed), sorted([str(main), str(view)]))
        self.assertFalse(main.exists())
        self.assertFalse(view.exists())

    def test_missing_dirs_are_success(self):
        self.assertEqual(
            delete_session_files("ghost-sid", base=self.store), [])

    def test_missing_base_is_success(self):
        self.assertEqual(
            delete_session_files("ghost-sid",
                                 base=str(Path(self.store) / "nope")), [])

    def test_only_view_dir_present(self):
        view = Path(self.store, ".msp-view-v1", "half-sid")
        view.mkdir(parents=True)
        (view / "HEAD.json").write_text("{}")
        self.assertEqual(delete_session_files("half-sid", base=self.store),
                         [str(view)])
        self.assertFalse(view.exists())

    def test_unsafe_ids_rejected(self):
        mkstore(self.store, "victim")
        for bad in ("", ".", "..", ".hidden", "../evil", "a/b", "a*b",
                    "x" * 129):
            with self.assertRaises(ValueError, msg=bad):
                delete_session_files(bad, base=self.store)
        # The rejections touched nothing.
        self.assertTrue(Path(self.store, "2026/10/05/victim").is_dir())

    def test_non_directory_refused(self):
        parent = Path(self.store, "2026/10/05")
        parent.mkdir(parents=True)
        (parent / "file-sid").write_text("nope")
        with self.assertRaises(ValueError):
            delete_session_files("file-sid", base=self.store)
        self.assertTrue((parent / "file-sid").is_file())

    def test_symlink_unlinked_not_followed(self):
        outside = Path(self.store, "outside-target")
        outside.mkdir(parents=True)
        (outside / "keep.txt").write_text("keep")
        link = Path(self.store, ".msp-view-v1", "link-sid")
        link.parent.mkdir(parents=True)
        link.symlink_to(outside, target_is_directory=True)
        removed = delete_session_files("link-sid", base=self.store)
        self.assertEqual(removed, [str(link)])
        self.assertFalse(link.is_symlink() or link.exists())
        self.assertTrue((outside / "keep.txt").is_file())

    def test_symlinked_store_parts_fail_closed(self):
        # Store parts symlinked OUTSIDE the root resolve outside it and
        # are refused (fail closed, nothing removed).
        Path(self.store).mkdir(parents=True)
        outside = Path(self.store + "-outside")
        outside.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, outside, True)
        trap = outside / "10" / "05" / "trap-sid"
        trap.mkdir(parents=True)
        (Path(self.store) / "2026").symlink_to(outside,
                                               target_is_directory=True)
        with self.assertRaises(ValueError):
            delete_session_files("trap-sid", base=self.store)
        self.assertTrue(trap.is_dir())
        # Same for a symlinked .msp-view-v1: refused, never followed.
        (Path(self.store) / ".msp-view-v1").symlink_to(
            outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            delete_session_files("trap-sid", base=self.store)
        self.assertTrue(trap.is_dir())


class TestRouterDelete(unittest.TestCase):
    def _router(self, msp=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = str(Path(tmp.name) / "store")
        main, view = mkstore(store, "gone-sid")
        router = SessionRouter(
            msp=msp if msp is not None else SilentMsp(
                [{"sessionId": "gone-sid", "name": "g"}]),
            workspace_base=None, sessions_base=store)
        return router, store, main, view

    def test_delete_removes_files_drops_routing(self):
        router, _, main, view = self._router()
        conn = FakeConn()
        router._subs.setdefault("gone-sid", set()).add(conn)
        conn.sessions.add("gone-sid")
        router._cursors["gone-sid"] = "c"
        reply = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "delete",
                         "sessionId": "gone-sid"}))
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["result"]["deleted"])
        self.assertEqual(reply["result"]["sessionId"], "gone-sid")
        self.assertEqual(len(reply["result"]["removed"]), 2)
        self.assertFalse(main.exists())
        self.assertFalse(view.exists())
        self.assertNotIn("gone-sid", router._subs)
        self.assertNotIn("gone-sid", conn.sessions)
        self.assertNotIn("gone-sid", router._cursors)

    def test_deleted_hidden_from_list_resume_read(self):
        router, _, _, _ = self._router()
        asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "delete",
                         "sessionId": "gone-sid"}))
        listed = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 2, "type": "list"}))
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(listed["result"]["sessions"], [])
        for mtype in ("resume", "read"):
            reply = asyncio.run(router.handle_client_message(
                FakeConn(), {"id": 3, "type": mtype,
                             "sessionId": "gone-sid"}))
            self.assertFalse(reply["ok"])
            self.assertIn("unknown session", reply["error"]["message"])

    def test_missing_session_still_replies_deleted(self):
        router, _, _, _ = self._router()
        reply = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "delete",
                         "sessionId": "ghost-sid"}))
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["result"]["deleted"])
        self.assertEqual(reply["result"]["removed"], [])

    def test_unsafe_id_is_error(self):
        router, _, _, _ = self._router()
        for bad in ("../evil", None):
            reply = asyncio.run(router.handle_client_message(
                FakeConn(), {"id": 1, "type": "delete",
                             "sessionId": bad}))
            self.assertFalse(reply["ok"])
            self.assertIn("unsafe", reply["error"]["message"])

    def test_recreate_clears_tombstone(self):
        rows = [{"sessionId": "gone-sid", "name": "g"}]
        router, _, _, _ = self._router(msp=StartMsp(rows))
        asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "delete",
                         "sessionId": "gone-sid"}))
        listed = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 2, "type": "list"}))
        self.assertEqual(listed["result"]["sessions"], [])
        created = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 3, "type": "new",
                         "sessionId": "gone-sid"}))
        self.assertTrue(created["ok"], created)
        relisted = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 4, "type": "list"}))
        self.assertEqual(
            [s["sessionId"] for s in relisted["result"]["sessions"]],
            ["gone-sid"])


if __name__ == "__main__":
    unittest.main()
