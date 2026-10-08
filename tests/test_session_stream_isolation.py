"""Session stream isolation: leaving a session stops its live transcript.

Defect: switching sessions never unsubscribed the previous one, so the
bridge kept fanning the old turn out to the same tab — and the `onEvent`
guard only dropped foreign traffic while a session was open, so a fresh
view with `state.sessionId === null` (lazy new session) rendered the old
stream into the new transcript.

Client contract (web/app.js, guarded at source level like
tests/test_transcript_echo.py — no JS harness in this repo):
- openSession/newSession unsubscribe the session being left.
- onEvent drops any event whose sessionId is not the open one, even when
  no session is open (global events carry no sessionId and still pass).
- handleSubscribeResult takes the subscribed session id and drops replays
  that land after the user moved on.

Bridge seam: subscribe + unsubscribe stops fan-out to that conn (locks
the server contract the client fix relies on).

Run: python3 -m unittest tests.test_session_stream_isolation -v
(from repo root)
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


def fn_body(src, name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  src, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestLeaveUnsubscribes(unittest.TestCase):
    def test_leave_helper_unsubscribes(self):
        body = fn_body(APP_JS, "leaveSession")
        self.assertIn('type: "unsubscribe"', body)

    def test_open_session_leaves_previous(self):
        body = fn_body(APP_JS, "openSession")
        # The previous session's stream must end when switching: without
        # this the old turn keeps fanning out to this tab.
        self.assertIn("leaveSession(state.sessionId)", body)

    def test_new_session_leaves_current(self):
        body = fn_body(APP_JS, "newSession")
        # The lazy blank view (sessionId null) has no guard of its own,
        # so it must stop the old subscription up front.
        self.assertIn("leaveSession(state.sessionId)", body)


class TestForeignEventsDropped(unittest.TestCase):
    def test_guard_has_no_open_session_hole(self):
        body = fn_body(APP_JS, "onEvent")
        # Old guard required a session to be open
        # (`p.sessionId && state.sessionId && ...`), so with sessionId
        # null every foreign event rendered. The fixed guard drops any
        # event whose sessionId is not the open one.
        self.assertIn(
            "if (p.sessionId && p.sessionId !== state.sessionId) {", body)
        self.assertNotIn("p.sessionId && state.sessionId &&", body)


class TestSubscribeReplayGuarded(unittest.TestCase):
    def test_stale_replay_dropped(self):
        body = fn_body(APP_JS, "handleSubscribeResult")
        # A slow subscribe response must not paint a session the user
        # already left: the replay carries which session it belongs to.
        self.assertIn("sessionId", body)
        self.assertIn("state.sessionId !== ", body)


class FakeConn:
    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class FakeMsp:
    async def call(self, method, params=None):
        assert method in ("view/subscribe", "view/unsubscribe")
        return {"viewCursor": "c0", "events": []}

    async def command(self, method, params=None):
        raise AssertionError(f"unexpected command {method}")


class TestRouterUnsubscribeStopsFanout(unittest.TestCase):
    def test_unsubscribed_session_no_longer_fans_out(self):
        async def go():
            router = SessionRouter(msp=FakeMsp(), workspace_base=None)
            conn = FakeConn()
            router.add_conn(conn)
            await router.subscribe(conn, "sid-A")
            await router.unsubscribe(conn, "sid-A")
            self.assertNotIn("sid-A", conn.sessions)
            router.on_notification(
                "item/delta",
                {"sessionId": "sid-A", "itemId": "i1",
                 "delta": "old-stream", "viewCursor": "c1"})
            return [f for f in conn.frames
                    if (f.get("params") or {}).get("sessionId") == "sid-A"]
        self.assertEqual(asyncio.run(go()), [])


if __name__ == "__main__":
    unittest.main()
