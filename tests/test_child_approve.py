"""Subagent approvals: card labeling + allowAll-gated auto-approve toggle.

The host never propagates a parent session's approval mode to its
children (each spawn resolves its own profile, defaulting to
prompting), so an allowAll session still parks on every subagent
prompt. The bridge therefore (a) labels cards carrying
`subagentOrigin` so they don't look like the session's own mode
failing, and (b) offers a Settings toggle that auto-decides child
approvals bridge-side when the parent's effective mode is allowAll.

Run: python3 -m unittest tests.test_child_approve -v   (from repo root)
"""

import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import SessionRouter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()


def child_approval(sid="sid-1", choices=None):
    """One approval/request params dict from a subagent child."""
    return {
        "sessionId": sid,
        "approvalId": "aid-1",
        "currentRequirementId": "rid-1",
        "toolName": "bash",
        "subject": {"kind": "shell", "command": "rm -rf /tmp/x"},
        "subagentOrigin": {"childRunId": "run-1",
                           "childSessionId": "csid-1",
                           "subagentId": "sub-1"},
        "availableChoices": choices if choices is not None else [
            {"choiceId": "allow_once", "decision": "approved",
             "scope": "once"},
            {"choiceId": "abort", "decision": "abort"},
        ],
    }


class FakeMsp:
    """Records commands; serves modes like the host would."""

    def __init__(self):
        self.commands = []
        self.calls = []
        self.start_mode = None
        self.resume_mode = "allowAll"

    async def command(self, method, params=None, timeout=60):
        self.commands.append((method, dict(params or {})))
        if method == "session/start":
            sess = {"sessionId": (params or {}).get("sessionId")
                    or "sid-new", "name": ""}
            if self.start_mode:
                sess["approvalMode"] = {"mode": self.start_mode,
                                        "source": "startup",
                                        "lastCommandId": None}
            return {"session": sess}
        if method == "session/resume":
            return {
                "session": {
                    "sessionId": (params or {}).get("sessionId"),
                    "name": "r",
                    "approvalMode": {"mode": self.resume_mode,
                                     "source": "replay",
                                     "lastCommandId": None}},
                "history": {"items": [], "mode": "inline"},
                "pendingRequests": [], "viewCursor": ""}
        if method == "session/setApprovalMode":
            return {"effectiveMode": {
                        "mode": (params or {}).get("mode"),
                        "source": "approvalReconfigure",
                        "lastCommandId": "c1"},
                    "applyOutcome": "completed", "commandId": "c1",
                    "status": "completed"}
        return {}

    async def call(self, method, params=None, timeout=60):
        self.calls.append((method, dict(params or {})))
        return {}


class FakeConn:
    def __init__(self):
        self.sessions = set()
        self.frames = []

    def queue_frame(self, frame):
        self.frames.append(frame)


def _modes(router):
    return dict(router._approval_modes)


class TestModeTracking(unittest.IsolatedAsyncioTestCase):
    async def test_new_requested_mode_recorded(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "new",
                         "approvalMode": "allowAll"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(_modes(r), {"sid-new": "allowAll"})

    async def test_new_session_object_mode_recorded(self):
        msp = FakeMsp()
        msp.start_mode = "promptUnmatched"
        r = SessionRouter(msp, workspace_base=None)
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "new"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(_modes(r), {"sid-new": "promptUnmatched"})

    async def test_set_approval_mode_records_effective(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "setApprovalMode",
                         "sessionId": "sid-1", "mode": "denyUnmatched"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(_modes(r), {"sid-1": "denyUnmatched"})

    async def test_resume_records_session_mode(self):
        msp = FakeMsp()
        msp.resume_mode = "promptUnmatched"
        r = SessionRouter(msp, workspace_base=None)
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "resume",
                         "sessionId": "sid-r"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(_modes(r), {"sid-r": "promptUnmatched"})

    async def test_mode_changed_notification_recorded(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        r.on_notification("session/approvalModeChanged",
                          {"sessionId": "sid-1", "mode": "allowAll",
                           "source": "approvalReconfigure"})
        self.assertEqual(_modes(r), {"sid-1": "allowAll"})

    async def test_unknown_mode_never_recorded(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        r.on_notification("session/approvalModeChanged",
                          {"sessionId": "sid-1", "mode": "never"})
        r.on_notification("session/approvalModeChanged",
                          {"sessionId": "sid-2"})
        self.assertEqual(_modes(r), {})

    async def test_drop_forgets_mode(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        r.on_notification("session/approvalModeChanged",
                          {"sessionId": "sid-1", "mode": "allowAll"})
        r._drop_routing("sid-1")
        self.assertEqual(_modes(r), {})


class TestSubagentAutoApprove(unittest.IsolatedAsyncioTestCase):
    def _router(self, mode="allowAll", enabled=True):
        msp = FakeMsp()
        r = SessionRouter(msp, workspace_base=None)
        conn = FakeConn()
        r.add_conn(conn)
        if mode is not None:
            r.on_notification(
                "session/approvalModeChanged",
                {"sessionId": "sid-1", "mode": mode})
        r._subagent_auto_approve = enabled
        return r, msp, conn

    async def _settle(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_consumed_when_enabled_and_allow_all(self):
        r, msp, conn = self._router()
        r.on_server_request("approval/request", child_approval())
        cards = [f for f in conn.frames if f.get("type") == "approval"]
        self.assertEqual(cards, [])
        notices = [f for f in conn.frames
                   if f.get("method") == "subagentAutoApproved"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["params"]["sessionId"], "sid-1")
        self.assertEqual(notices[0]["params"]["approvalId"], "aid-1")
        await self._settle()
        decides = [p for m, p in msp.commands
                   if m == "approval/decide"]
        self.assertEqual(len(decides), 1)
        self.assertEqual(decides[0]["approvalId"], "aid-1")
        self.assertEqual(decides[0]["choiceId"], "allow_once")
        self.assertEqual(decides[0]["requirementId"], "rid-1")

    async def test_forwarded_when_toggle_off(self):
        r, msp, conn = self._router(enabled=False)
        r.on_server_request("approval/request", child_approval())
        await self._settle()
        cards = [f for f in conn.frames if f.get("type") == "approval"]
        self.assertEqual(len(cards), 1)
        self.assertIn("subagentOrigin", cards[0]["approval"])
        decides = [p for m, p in msp.commands
                   if m == "approval/decide"]
        self.assertEqual(decides, [])

    async def test_forwarded_when_parent_not_allow_all(self):
        r, msp, conn = self._router(mode="onRequest")
        r.on_server_request("approval/request", child_approval())
        await self._settle()
        cards = [f for f in conn.frames if f.get("type") == "approval"]
        self.assertEqual(len(cards), 1)
        decides = [p for m, p in msp.commands
                   if m == "approval/decide"]
        self.assertEqual(decides, [])

    async def test_forwarded_when_mode_unknown(self):
        r, msp, conn = self._router(mode=None)
        r.on_server_request("approval/request", child_approval())
        await self._settle()
        cards = [f for f in conn.frames if f.get("type") == "approval"]
        self.assertEqual(len(cards), 1)
        decides = [p for m, p in msp.commands
                   if m == "approval/decide"]
        self.assertEqual(decides, [])

    async def test_parent_own_approval_always_forwarded(self):
        # No subagentOrigin: the parent's own prompt is never consumed,
        # even with the toggle on and the parent on allowAll.
        r, msp, conn = self._router()
        own = child_approval()
        del own["subagentOrigin"]
        r.on_server_request("approval/request", own)
        await self._settle()
        cards = [f for f in conn.frames if f.get("type") == "approval"]
        self.assertEqual(len(cards), 1)
        decides = [p for m, p in msp.commands
                   if m == "approval/decide"]
        self.assertEqual(decides, [])

    async def test_no_approve_choice_forwarded(self):
        r, msp, conn = self._router()
        r.on_server_request("approval/request", child_approval(choices=[
            {"choiceId": "abort", "decision": "abort"}]))
        await self._settle()
        cards = [f for f in conn.frames if f.get("type") == "approval"]
        self.assertEqual(len(cards), 1)
        decides = [p for m, p in msp.commands
                   if m == "approval/decide"]
        self.assertEqual(decides, [])

    async def test_toggle_roundtrip_and_validation(self):
        r = SessionRouter(FakeMsp(), workspace_base=None)
        conn = FakeConn()
        self.assertFalse(r._subagent_auto_approve)
        f = await r.handle_client_message(
            conn, {"id": 1, "type": "setSubagentAutoApprove",
                   "enabled": True})
        self.assertTrue(f["ok"], f)
        self.assertEqual(f["result"], {"enabled": True})
        self.assertTrue(r._subagent_auto_approve)
        f = await r.handle_client_message(
            conn, {"id": 2, "type": "setSubagentAutoApprove",
                   "enabled": False})
        self.assertTrue(f["ok"], f)
        self.assertFalse(r._subagent_auto_approve)
        for bad in (None, "yes", 1):
            f = await r.handle_client_message(
                conn, {"id": 3, "type": "setSubagentAutoApprove",
                       "enabled": bad})
            self.assertFalse(f["ok"])
            self.assertIn("enabled", f["error"]["message"])


def _fn_body(name):
    m = re.search(r"function " + name + r"\([^)]*\) \{(.*?)\n\}",
                  APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestSubagentCardLabel(unittest.TestCase):
    def test_card_notes_subagent_origin(self):
        body = _fn_body("onApproval")
        self.assertIn("subagentOrigin", body)
        self.assertIn("from subagent", body)

    def test_auto_approve_notice_renders(self):
        self.assertIn("subagentAutoApproved", APP_JS)


class TestSubagentToggleClient(unittest.TestCase):
    def test_setting_registered_and_persisted(self):
        self.assertIn('defineSetting("subagentAutoApprove"', APP_JS)
        self.assertIn('setSetting("subagentAutoApprove"', APP_JS)
        self.assertIn('getSetting("subagentAutoApprove")', APP_JS)
        self.assertIn("webmuse.subagentAutoApprove", APP_JS)
        self.assertIn("function applySubagentAutoApprovePick(", APP_JS)

    def test_settings_row_and_bridge_sync(self):
        self.assertIn("Subagent auto-approve", APP_JS)
        # Setter syncs the bridge; connect() re-syncs on (re)connect.
        self.assertGreaterEqual(
            APP_JS.count("setSubagentAutoApprove"), 2)


if __name__ == "__main__":
    unittest.main()
