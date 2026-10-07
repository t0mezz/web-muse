"""Session bar: directory tracking + external-delete handling.

UI (source-level, no JS harness): rows hide the workspace path and keep
open/rename/fork/delete behind a per-row config button; opening a
session that was deleted outside the app is treated like an in-app
delete (best-effort host delete, drop from the bar, fresh session)
instead of erroring on every open.
Bridge seam: the resume fallback raises unknown-session for a sid the
host no longer lists, and `list` passes workspace rows through untouched.

Run: python3 -m unittest tests.test_session_bar -v   (from repo root)
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.msp import MspError  # noqa: E402
from server.sessions import SessionRouter, _workspace_under_base  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class FakeConn:
    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class FakeMsp:
    """Host with no such session (deleted elsewhere) but a healthy list."""

    async def command(self, method, params=None):
        if method == "session/resume":
            raise MspError(-32601, "Method not found")
        raise AssertionError(f"unexpected command {method}")

    async def call(self, method, params=None):
        if method == "session/list":
            return {"sessions": [{"sessionId": "other-sid",
                                  "workspaceRoot": "/home/pi/work/x"}]}
        if method == "approval/listPending":
            return {"approvals": [], "userInputs": []}
        raise AssertionError(f"unexpected call {method}")


class FakeMspPassthrough:
    """Host list carrying workspace rows; records what the bridge asked."""

    def __init__(self):
        # "/" always exists, so no workspaceMissing flag is added here:
        # this locks the pure passthrough contract.
        self.rows = [
            {"sessionId": "sid-1", "name": "a", "workspaceRoot": "/"},
            {"sessionId": "sid-2", "name": "b"},
        ]

    async def command(self, method, params=None):
        raise AssertionError(f"unexpected command {method}")

    async def call(self, method, params=None):
        assert method == "session/list"
        return {"sessions": self.rows}


class TestResumeUnknownSession(unittest.TestCase):
    def test_fallback_names_missing_sid(self):
        router = SessionRouter(msp=FakeMsp(), workspace_base=None)
        reply = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "resume",
                         "sessionId": "gone-sid"}))
        self.assertFalse(reply["ok"])
        self.assertIn("unknown session", reply["error"]["message"])
        self.assertIn("gone-sid", reply["error"]["message"])

    def test_list_passes_workspace_rows_through(self):
        msp = FakeMspPassthrough()
        router = SessionRouter(msp=msp, workspace_base=None)
        reply = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "list", "limit": 100}))
        self.assertTrue(reply["ok"], reply)
        # The bar's directory comes straight from the host: the bridge must
        # not reshape, drop, or invent it.
        self.assertEqual(reply["result"]["sessions"], msp.rows)

    def test_list_auto_deletes_removed_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            import os
            wsbase = os.path.join(d, "ws")
            os.makedirs(os.path.join(wsbase, "kept"))
            store = os.path.join(d, "store")
            os.makedirs(store)

            def mkstore(sid):
                main = Path(store) / "2026" / "10" / "05" / sid
                main.mkdir(parents=True)
                (main / "session.jsonl").write_text("{}\n")

            mkstore("kept")
            mkstore("gone")

            class MspWithDirs:
                async def command(self, method, params=None):
                    raise AssertionError(method)

                async def call(self, method, params=None):
                    return {"sessions": [
                        {"sessionId": "kept", "workspaceRoot": os.path.join(wsbase, "kept")},
                        {"sessionId": "gone",
                         "workspaceRoot": os.path.join(wsbase, "gone")},
                    ]}

            router = SessionRouter(msp=MspWithDirs(), workspace_base=wsbase,
                                   sessions_base=store)
            reply = asyncio.run(router.handle_client_message(
                FakeConn(), {"id": 1, "type": "list"}))
            self.assertTrue(reply["ok"], reply)
            ids = [s["sessionId"] for s in reply["result"]["sessions"]]
            self.assertIn("kept", ids)
            self.assertNotIn("gone", ids)


class TestWorkspaceUnderBase(unittest.TestCase):
    def test_contained_and_outside(self):
        with tempfile.TemporaryDirectory() as d:
            base = str(Path(d) / "ws")
            self.assertTrue(_workspace_under_base(str(Path(d) / "ws" / "a"), base))
            self.assertFalse(_workspace_under_base("/media/usb-xyz/repo", base))
            self.assertFalse(_workspace_under_base("", base))
            self.assertFalse(_workspace_under_base(str(Path(d) / "ws" / "a"), None))


class TestAutoDeleteMissing(unittest.TestCase):
    """list auto-deletes sessions whose workspace dir is gone (scoped)."""

    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "store"
        wsbase = Path(tmp.name) / "ws"
        (wsbase / "kept").mkdir(parents=True)

        def mkstore(sid):
            main = store / "2026" / "10" / "05" / sid
            main.mkdir(parents=True)
            (main / "session.jsonl").write_text("{}\n")
            view = store / ".msp-view-v1" / sid
            view.mkdir(parents=True)
            (view / "HEAD.json").write_text("{}")
            return main, view

        gone_main, gone_view = mkstore("gone-sid")
        mkstore("kept-sid")

        class Msp:
            async def command(self, method, params=None):
                raise AssertionError(
                    f"auto-delete must not call the host (got {method})")

            async def call(self, method, params=None):
                assert method == "session/list"
                return {"sessions": [
                    {"sessionId": "kept-sid", "name": "kept",
                     "workspaceRoot": str(wsbase / "kept")},
                    {"sessionId": "gone-sid", "name": "gone",
                     "workspaceRoot": str(wsbase / "gone")},
                    {"sessionId": "ext-sid", "name": "ext",
                     "workspaceRoot": "/media/usb-xyz-123/repo"},
                    {"sessionId": "noroot-sid", "name": "noroot"},
                ]}

        router = SessionRouter(msp=Msp(),
                               workspace_base=str(wsbase),
                               sessions_base=str(store))
        return router, gone_main, gone_view

    def test_list_auto_deletes_and_tombstones(self):
        router, gone_main, gone_view = self._setup()
        reply = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 1, "type": "list"}))
        self.assertTrue(reply["ok"], reply)
        ids = [s["sessionId"] for s in reply["result"]["sessions"]]
        self.assertIn("kept-sid", ids)
        self.assertNotIn("gone-sid", ids)
        self.assertIn("ext-sid", ids)
        self.assertIn("noroot-sid", ids)
        self.assertFalse(gone_main.exists())
        self.assertFalse(gone_view.exists())
        resumed = asyncio.run(router.handle_client_message(
            FakeConn(), {"id": 2, "type": "resume",
                         "sessionId": "gone-sid"}))
        self.assertFalse(resumed["ok"])
        self.assertIn("unknown session", resumed["error"]["message"])


class TestSessionBarUI(unittest.TestCase):
    def test_row_hides_workspace_path(self):
        # Path hidden by design: no dir line is rendered and the helper
        # is gone with it. The bridge still passes rows through untouched
        # (test_list_passes_workspace_rows_through above).
        self.assertNotIn("shortenDir", APP_JS)
        self.assertNotIn("shortenDir(s.workspaceRoot)", APP_JS)
        self.assertNotIn("dir.title = s.workspaceRoot", APP_JS)

    def test_row_actions_behind_config(self):
        # open/rename/fork/delete live in a per-row menu behind the
        # 3-dot config button — no always-visible action bar remains.
        for marker in ("config-btn", "row-menu", "row-opt",
                       "closeRowMenus", "mkItem("):
            self.assertIn(marker, APP_JS)
        for action in ("openSession(s.sessionId)",
                       "renameSession(s.sessionId)",
                       "forkSession(s.sessionId)",
                       "deleteSession(s.sessionId)"):
            self.assertIn(action, APP_JS)
        self.assertNotIn('"acts"', APP_JS)

    def test_composer_typing_placeholder(self):
        import json
        hints = json.loads((ROOT / "web" / "composer-hints.json").read_text())
        self.assertEqual(len(hints), 100)
        self.assertTrue(all(isinstance(h, str) and h.strip() for h in hints))
        self.assertIn("startComposerHints()", APP_JS)
        self.assertIn("composer-hints.json", APP_JS)
        self.assertIn("input.placeholder", APP_JS)
        self.assertIn('input.addEventListener("focus"', APP_JS)
        self.assertIn('input.addEventListener("blur"', APP_JS)
        self.assertIn('input.placeholder = "Ask Muse…"', APP_JS)
        self.assertIn('input.placeholder = ""', APP_JS)
        self.assertIn("Math.random()", APP_JS)

    def test_refresh_spin_plays_fully(self):
        self.assertIn("void btn.offsetWidth", APP_JS)
        self.assertIn('setTimeout(() => btn.classList.remove("spin"), 650)', APP_JS)
        self.assertIn('[aria-busy="true"]', STYLE_CSS)

    def test_config_second_click_closes_menu(self):
        self.assertIn('cfg.addEventListener("mousedown"', APP_JS)
        self.assertIn("if (!menu.hidden) e.preventDefault()", APP_JS)

    def test_config_hamburger_animates_on_menu_open(self):
        self.assertIn("line-top-bottom", APP_JS + STYLE_CSS)
        self.assertIn('config-btn[aria-expanded="true"]', STYLE_CSS)
        self.assertIn("stroke-dashoffset", STYLE_CSS)

    def test_config_no_spin_on_hover(self):
        # No hover rotation: the icon only animates into an X while its
        # menu is open (aria-expanded), never on hover.
        self.assertNotIn("rotate(-90deg)", STYLE_CSS)
        self.assertNotIn(".config-btn:hover svg", STYLE_CSS)
        self.assertNotIn('cfg.classList.toggle("open"', APP_JS)
        self.assertNotIn(".config-btn.open", APP_JS + STYLE_CSS)

    def test_external_delete_treated_as_manual_delete(self):
        for marker in ("dropExternallyDeleted(sessionId, e.message)",
                       "state.sessionsCache.some((s) => s.sessionId === sessionId)",
                       "was already deleted outside the app"):
            self.assertIn(marker, APP_JS)
        # Same aftermath as the in-app delete: best-effort host delete,
        # drop from the bar, fresh session — and no confirm, since there
        # is nothing left to protect.
        for marker in ('await send({ type: "delete", sessionId })',
                       'toast("deleted " + shortId(sessionId))',
                       "if (sessionId === state.sessionId) newSession();"):
            self.assertIn(marker, APP_JS)
        # A failed refresh means offline, not deleted: never delete on that.
        self.assertIn("let fresh = false", APP_JS)

    def test_drawer_refreshes_on_open(self):
        self.assertIn("refreshSessions().catch(() => {});", APP_JS)

    def test_missing_dir_rows_auto_deleted(self):
        for marker in ("state.sessionsCache = rows", "_workspace_under_base",
                       "Auto-sync: the bridge deletes sessions"):
            self.assertIn(marker, APP_JS + SESSIONS_PY)
        for marker in ("s.workspaceMissing", "state.sessionsHidden",
                       "hidden — workspace directory removed",
                       "Sync now", "sync-btn", "cmdSync", "pruneMissing",
                       "_prune_missing", 'name: "sync"'):
            self.assertNotIn(marker, APP_JS + SESSIONS_PY + STYLE_CSS)


class BridgeMsp:
    """Host list with bridge + TUI rows; records starts."""

    def __init__(self, rows):
        self.rows = rows
        self.starts = []

    async def command(self, method, params=None):
        if method == "session/start":
            sid = (params or {}).get("sessionId") or "minted-sid"
            self.starts.append(dict(params or {}))
            return {"session": {"sessionId": sid, "name": ""}}
        raise AssertionError(f"unexpected command {method}")

    async def call(self, method, params=None):
        assert method == "session/list"
        return {"sessions": [dict(r) for r in self.rows]}


class TestBridgeFirstOrdering(unittest.TestCase):
    def _router(self, base):
        Path(base).mkdir(parents=True, exist_ok=True)
        return SessionRouter(msp=BridgeMsp([]), workspace_base=base,
                             sessions_base=str(Path(base).parent / "sess"))

    def test_new_session_marks_and_annotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / "ws")
            router = self._router(base)
            msp = router._msp
            msp.rows = [
                {"sessionId": "tui-1", "name": "t"},
                {"sessionId": "web-1", "name": "w"},
            ]
            asyncio.run(router._do_new(
                FakeConn(), {"sessionId": "web-1",
                             "workspaceRoot": tmp}))
            reply = asyncio.run(router.handle_client_message(
                FakeConn(), {"id": 1, "type": "list"}))
            by_id = {s["sessionId"]: s
                     for s in reply["result"]["sessions"]}
            self.assertTrue(by_id["web-1"].get("bridgeCreated"))
            # Untouched rows stay byte-identical to the host's.
            self.assertNotIn("bridgeCreated", by_id["tui-1"])
            self.assertEqual(by_id["tui-1"], msp.rows[0])

    def test_grouping_survives_bridge_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / "ws")
            sess = str(Path(tmp) / "sess")
            router = self._router(base)
            router._remember_bridge_sid("web-9")
            fresh = SessionRouter(msp=BridgeMsp([]), workspace_base=base,
                                  sessions_base=sess)
            self.assertIn("web-9", fresh._bridge_sids)

    def test_delete_drops_sid(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / "ws")
            sess = str(Path(tmp) / "sess")
            router = self._router(base)
            router._remember_bridge_sid("web-9")
            router._drop_routing("web-9")
            self.assertNotIn("web-9", router._bridge_sids)
            fresh = SessionRouter(msp=BridgeMsp([]), workspace_base=base,
                                  sessions_base=sess)
            self.assertNotIn("web-9", fresh._bridge_sids)

    def test_ui_sorts_bridge_first(self):
        for marker in ("b.bridgeCreated ? 1 : 0",
                       "Bridge-created sessions first"):
            self.assertIn(marker, APP_JS)


class TestSessionViewCursor(unittest.TestCase):
    """Cursor-like session view: grouped rows, relative time, human status."""

    def test_human_helpers_present(self):
        for marker in ("sessRelTime", "sessStatusLabel", "sessPreview",
                       "sessIsUnnamed", "sess-group", "sess-time",
                       "sess-preview", "sess-icon"):
            self.assertIn(marker, APP_JS)

    def test_no_raw_host_status_in_rows(self):
        self.assertNotIn("${s.turnCount", APP_JS)
        self.assertNotIn("notLoaded ·", APP_JS)

    def test_singular_turn_and_group_labels(self):
        for marker in ('turn${n === 1 ? "" : "s"}',
                       '"Web session"', 'sessN(other.length, "Session")',
                       "Showing ${sessN", "toLocaleDateString"):
            self.assertIn(marker, APP_JS)
        self.assertNotIn("Yesterday", APP_JS)

    def test_row_accessibility_and_layout(self):
        for marker in ("sess-open", "sess-line", "data-sid",
                       "session-count", "(hover: none)",
                       "aria-expanded", "focus-within"):
            self.assertIn(marker, APP_JS + STYLE_CSS)
        self.assertIn(":focus-visible", STYLE_CSS)
        self.assertIn("listitem", APP_JS)
        self.assertNotIn("function esc(", APP_JS)
        self.assertNotIn(" esc(it", APP_JS)

    def test_honest_fallbacks(self):
        self.assertIn("Untitled ${shortId", APP_JS)
        self.assertIn('return "Unknown"', APP_JS)
        self.assertIn('return "Failed"', APP_JS)
        self.assertNotIn(".status-dot", STYLE_CSS)

    def test_group_semantics_and_menu_behavior(self):
        for marker in ("sess-sec", 'role", "menu"', "aria-label",
                       "flip", "focusout", "_opener",
                       "toLocaleString", "sessN", "sess-count",
                       "aria-current", "60000", "rawFilter",
                       'menu.addEventListener("keydown"',
                       'el("session-filter").value = ""',
                       "box-shadow: 0 8px 24px", "sess-group-${kind}",
                       'createElement("h2")', "aria-labelledby",
                       'el("session-filter").focus()',
                       'el("btn-sessions").focus()',
                       'role", "list"', "box.append(h)",
                       'prompt("Session name:", cur)',
                       "listBusy", "sess-spin", "prevScroll",
                       "aria-controls", "createElement(\"time\")",
                       "sessions refreshed", "sess-body",
                       "btn-clear-filter", "Starting", "Paused"):
            self.assertIn(marker, APP_JS + STYLE_CSS)
        self.assertNotIn("sec.append(h)", APP_JS)

    def test_search_toggle_hides_on_empty_escape(self):
        self.assertIn("filter-box", APP_JS)
        self.assertIn('btn-search-sessions").focus()', APP_JS)

    def test_rename_prefills_real_name_only(self):
        self.assertIn('(hit.name || "").trim()', APP_JS)
        self.assertIn("Ambiguous prefix", APP_JS)
        self.assertNotIn('sessN(sessions.length, "Session")', APP_JS)

    def test_other_collapse_persists(self):
        self.assertIn("webmuse.otherCollapsed", APP_JS)
        self.assertIn("saveOtherCollapsed()", APP_JS)
        self.assertIn("localStorage.setItem(OTHER_COLLAPSED_KEY", APP_JS)
        self.assertIn("localStorage.getItem(OTHER_COLLAPSED_KEY", APP_JS)

    def test_header_icons_pinned_right_and_dim(self):
        self.assertIn("#btn-search-sessions", STYLE_CSS)
        self.assertIn("margin-left: auto", STYLE_CSS)
        for marker in ("#btn-search-sessions:hover", "position: absolute",
                        ".session-row:hover .sess-preview"):
            self.assertIn(marker, STYLE_CSS)

    def test_sidebar_chrome(self):
        index_html = (ROOT / "web" / "index.html").read_text()
        self.assertNotIn("btn-new-session", index_html + APP_JS + STYLE_CSS)
        self.assertIn("btn-search-sessions", index_html)
        self.assertIn("sess-toggle", APP_JS)
        self.assertIn("sessions-h1", index_html)
        self.assertIn("aria-labelledby", index_html + APP_JS)
        for marker in (".sess-group", ".sess-time", ".sess-preview",
                       ".sess-icon", ".sess-toggle",
                       ".session-row.active { background:"):
            self.assertIn(marker, STYLE_CSS)
        self.assertNotIn("border-left: 3px solid var(--accent)", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
