"""Frame-mapping tests: WS<->MSP shapes against recorded fixtures.

Run: python3 -m unittest discover -s tests -v   (from repo root)
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.msp import uuid7  # noqa: E402
from server.sessions import (  # noqa: E402
    VALID_APPROVAL_MODES,
    SessionRouter,
    build_session_mcp_config,
    build_turn_input,
    map_notification_to_ws,
    map_server_request_to_ws,
    notification_session_id,
    read_mcp_servers,
    session_workspace_dir,
)
from server.ws import (  # noqa: E402
    encode_frame,
    host_allowed,
    normalize_allowed_hosts,
    ws_accept_key,
)

SID = "01a10797-3751-7dd0-8396-7eeba49b5ebb"
TID = "01a10797-37df-71f2-b6d5-44e5b57a6d2f"
IID = "f129476c-a9c9-43fd-beb4-5b4d042a55b0"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class TestUuid7(unittest.TestCase):
    def test_shape_and_uniqueness(self):
        a, b = uuid7(), uuid7()
        self.assertRegex(a, UUID_RE)
        self.assertRegex(b, UUID_RE)
        self.assertNotEqual(a, b)

    def test_monotonic_ms(self):
        import time
        a = uuid7()
        time.sleep(0.002)
        b = uuid7()
        self.assertLess(a, b)  # same ms-prefix ordering


class TestTurnInput(unittest.TestCase):
    def test_text_part(self):
        self.assertEqual(build_turn_input("hi"), [{"type": "text", "text": "hi"}])

    def test_image_parts(self):
        parts = build_turn_input("", [{"mediaType": "image/png",
                                       "base64Data": "AAA"}])
        self.assertEqual(parts, [{"type": "image", "mediaType": "image/png",
                                  "base64Data": "AAA"}])

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            build_turn_input("")

    def test_skill_part(self):
        self.assertEqual(
            build_turn_input("", None, [{"selector": "plan"}]),
            [{"type": "skill", "selector": "plan"}])

    def test_skill_arguments_and_order(self):
        parts = build_turn_input(
            "extra", None,
            [{"selector": "grill-me", "arguments": "the plan"}])
        self.assertEqual(parts, [
            {"type": "skill", "selector": "grill-me",
             "arguments": "the plan"},
            {"type": "text", "text": "extra"},
        ])

    def test_skill_rejects_blank_selector(self):
        for bad in ({}, {"selector": ""}, {"selector": "  "},
                    {"selector": 42}, {"arguments": 42}, ["plan"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_turn_input("", None, [bad])


class TestRouting(unittest.TestCase):
    def test_session_scoped(self):
        self.assertEqual(
            notification_session_id("item/delta",
                                    {"sessionId": SID, "itemId": IID}),
            SID)

    def test_global_broadcast(self):
        self.assertIsNone(notification_session_id("session/listChanged", {}))

    def test_session_started_unpacks(self):
        self.assertEqual(
            notification_session_id("session/started",
                                    {"session": {"sessionId": SID}}),
            SID)

    def test_event_envelope(self):
        f = map_notification_to_ws("item/delta", {"sessionId": SID})
        self.assertEqual(f["type"], "event")
        self.assertEqual(f["method"], "item/delta")

    def test_approval_request_maps_to_card(self):
        f = map_server_request_to_ws("approval/request",
                                     {"sessionId": SID, "approvalId": "a1"})
        self.assertEqual(f["type"], "approval")
        self.assertEqual(f["approval"]["approvalId"], "a1")

    def test_userinput_request_maps(self):
        f = map_server_request_to_ws("userInput/request",
                                     {"sessionId": SID, "userInputId": "u1"})
        self.assertEqual(f["type"], "userInput")

    def test_allowall_permitted(self):
        # allowAll ("approve all" in the Session panel) is a selectable
        # mode: the bridge forwards it, with the warning living in the UI.
        self.assertIn("allowAll", VALID_APPROVAL_MODES)




class TestWsBits(unittest.TestCase):
    def test_accept_vector(self):
        # RFC 6455 section 1.3 example vector.
        self.assertEqual(ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_small_frame_roundtrip_shape(self):
        data = encode_frame(0x1, b"hi")
        self.assertEqual(data[:2], bytes([0x81, 0x02]))
        self.assertEqual(data[2:], b"hi")


class TestRecordedDeltaFixture(unittest.TestCase):
    """Recorded `muse serve --provider echo` item/delta shape stays mappable."""

    FIXTURE = {"delta": "echo: hello echo", "field": "text",
               "itemId": IID, "sessionId": SID,
               "viewCursor": f"v:{SID}:3.2"}

    def test_fixture_routes_and_maps(self):
        self.assertEqual(notification_session_id("item/delta", self.FIXTURE),
                         SID)
        f = map_notification_to_ws("item/delta", self.FIXTURE)
        self.assertEqual(f["params"]["delta"], "echo: hello echo")
        self.assertEqual(f["params"]["itemId"], IID)


class FakeMsp:
    """Records command/query calls; replays canned results."""

    def __init__(self, unserved=(), refused=()):
        self.commands = []
        self.calls = []
        self.unserved = set(unserved)
        self.refused = set(refused)

    async def command(self, method, params=None, timeout=60):
        from server.msp import MspError
        if method in self.unserved:
            raise MspError(-32601, "method not found")
        if method in self.refused:
            raise MspError(-32603, "retained session refused (class c): "
                           "compose session permission profile: permission "
                           "profile ':auto-review' cannot be used")
        self.commands.append((method, dict(params or {})))
        return {"commandId": "cmd", "status": "accepted"}

    async def call(self, method, params=None, timeout=60):
        from server.msp import MspError
        if method in self.unserved:
            raise MspError(-32601, "method not found")
        if method in self.refused:
            raise MspError(-32603, "retained session refused (class c): "
                           "compose session permission profile: permission "
                           "profile ':auto-review' cannot be used")
        self.calls.append((method, dict(params or {})))
        if method == "skill/list":
            return {"skills": [{"selector": "x", "displayName": "X",
                                "description": "d", "source": "user"}]}
        if method == "item/readOutput":
            return {"byteLen": 3, "content": "abc", "encoding": "text",
                    "eof": True, "mediaType": "text/plain", "offsetBytes": 0}
        if method == "session/list":
            return {"sessions": [{"sessionId": SID, "name": "n",
                                   "status": "idle"}]}
        if method == "approval/listPending":
            return {"approvals": [], "userInputs": []}
        return {}


class FakeConn:
    def __init__(self):
        self.sessions = set()
        self.frames = []

    def queue_frame(self, obj):
        self.frames.append(obj)


class TestRouterDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_steer_requires_turn(self):
        r = SessionRouter(FakeMsp())
        conn = FakeConn()
        # No turn tracked and none supplied -> clear error, no MSP call.
        f = await r.handle_client_message(
            conn, {"id": 1, "type": "steer", "sessionId": SID,
                   "text": "faster"})
        self.assertFalse(f["ok"])
        self.assertIn("expectedTurnId", f["error"]["message"])
        self.assertEqual(r._msp.commands, [])
        # Tracked turn from turn/started fills the gap.
        r.on_notification("turn/started",
                          {"sessionId": SID, "turnId": TID,
                           "viewCursor": "v:1"})
        f = await r.handle_client_message(
            conn, {"id": 2, "type": "steer", "sessionId": SID,
                   "text": "faster"})
        self.assertTrue(f["ok"], f)
        method, params = r._msp.commands[0]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["expectedTurnId"], TID)
        # turn/completed clears the tracked turn.
        r.on_notification("turn/completed",
                          {"sessionId": SID, "turnId": TID,
                           "terminal": "completed", "viewCursor": "v:2"})
        f = await r.handle_client_message(
            conn, {"id": 3, "type": "steer", "sessionId": SID,
                   "text": "faster"})
        self.assertFalse(f["ok"])

    async def test_steer_explicit_turn_wins(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "steer", "sessionId": SID,
                         "expectedTurnId": TID, "text": "go"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(r._msp.commands[0][1]["expectedTurnId"], TID)

    async def test_set_effort_validation(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "setEffort",
                         "sessionId": SID, "reasoningEffort": "turbo"})
        self.assertFalse(f["ok"])
        self.assertIn("reasoning effort", f["error"]["message"])
        # The `none`/`ultra` extremes are excluded from the offered tiers.
        for bad in ("none", "ultra"):
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "setEffort",
                             "sessionId": SID, "reasoningEffort": bad})
            self.assertFalse(f["ok"], bad)
        f = await r.handle_client_message(
            FakeConn(), {"id": 2, "type": "setEffort",
                         "sessionId": SID, "reasoningEffort": "max"})
        self.assertTrue(f["ok"], f)
        method, params = r._msp.commands[0]
        self.assertEqual(method, "session/setReasoningEffort")
        self.assertEqual(params["reasoningEffort"], "max")

    async def test_approval_mode_validation(self):
        r = SessionRouter(FakeMsp())
        for bad in ("never", "onFailure", ""):
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "setApprovalMode",
                             "sessionId": SID, "mode": bad})
            self.assertFalse(f["ok"], bad)
        f = await r.handle_client_message(
            FakeConn(), {"id": 2, "type": "setApprovalMode",
                         "sessionId": SID, "mode": "denyUnmatched"})
        self.assertTrue(f["ok"], f)

    async def test_allowall_dispatch(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "setApprovalMode",
                         "sessionId": SID, "mode": "allowAll"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(r._msp.commands[0],
                         ("session/setApprovalMode",
                          {"sessionId": SID, "mode": "allowAll"}))

    async def test_page_direction_validation(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "page", "sessionId": SID,
                         "limit": 5, "direction": "before"})
        self.assertFalse(f["ok"])
        self.assertIn("direction", f["error"]["message"])

    async def test_skills_and_read_output_passthrough(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "skills", "sessionId": SID})
        self.assertTrue(f["ok"], f)
        self.assertIn("skills", f["result"])
        f = await r.handle_client_message(
            FakeConn(), {"id": 2, "type": "readOutput", "sessionId": SID,
                         "itemId": IID, "outputRef": "o1"})
        self.assertTrue(f["ok"], f)
        self.assertEqual(f["result"]["content"], "abc")
        method, params = r._msp.calls[1]
        self.assertEqual(method, "item/readOutput")
        self.assertEqual(params["outputRef"], "o1")

    async def test_prompt_forwards_skill_parts(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "prompt", "sessionId": SID,
                         "text": "", "displayText": "/plan depot",
                         "skills": [{"selector": "plan",
                                     "arguments": "depot"}]})
        self.assertTrue(f["ok"], f)
        method, params = r._msp.commands[0]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"], [
            {"type": "skill", "selector": "plan", "arguments": "depot"}])
        self.assertEqual(params["displayText"], "/plan depot")

    async def test_prompt_rejects_bad_skill_shape(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "prompt", "sessionId": SID,
                         "text": "", "skills": [{"selector": ""}]})
        self.assertFalse(f["ok"])
        self.assertIn("selector", f["error"]["message"])
        self.assertEqual(r._msp.commands, [])

    async def test_mcp_never_touches_msp(self):
        r = SessionRouter(FakeMsp())
        f = await r.handle_client_message(FakeConn(), {"id": 1, "type": "mcp"})
        self.assertTrue(f["ok"], f)
        self.assertIn("servers", f["result"])
        self.assertEqual(r._msp.calls, [])
        self.assertEqual(r._msp.commands, [])

    async def test_resume_fallback_on_32601(self):
        r = SessionRouter(FakeMsp(unserved=("session/resume",)))
        conn = FakeConn()
        f = await r.handle_client_message(
            conn, {"id": 1, "type": "resume", "sessionId": SID})
        self.assertTrue(f["ok"], f)
        self.assertEqual(f["result"]["session"]["sessionId"], SID)
        self.assertEqual(f["result"]["history"]["mode"], "none")
        self.assertEqual(f["result"]["fallback"], "resume_unserved_by_host")
        self.assertIn(SID, conn.sessions)  # still attached for live events

    async def test_resume_fallback_on_32603_retained_refused(self):
        r = SessionRouter(FakeMsp(refused=("session/resume",)))
        conn = FakeConn()
        f = await r.handle_client_message(
            conn, {"id": 1, "type": "resume", "sessionId": SID})
        self.assertTrue(f["ok"], f)
        self.assertEqual(f["result"]["session"]["sessionId"], SID)
        self.assertEqual(f["result"]["history"]["mode"], "none")
        self.assertEqual(f["result"]["history"]["noneReason"],
                         "resume_refused_by_host")
        self.assertEqual(f["result"]["fallback"], "resume_refused_by_host")
        self.assertIn(SID, conn.sessions)  # still attached for live events

    async def test_resume_fallback_unknown_session(self):
        r = SessionRouter(FakeMsp(unserved=("session/resume",)))
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "resume", "sessionId": "nope"})
        self.assertFalse(f["ok"])
        self.assertIn("unknown session", f["error"]["message"])

    async def test_read_fallback_without_attach(self):
        r = SessionRouter(FakeMsp(unserved=("session/read",)))
        conn = FakeConn()
        f = await r.handle_client_message(
            conn, {"id": 1, "type": "read", "sessionId": SID})
        self.assertTrue(f["ok"], f)
        self.assertEqual(f["result"]["session"]["sessionId"], SID)
        self.assertNotIn(SID, conn.sessions)

    async def test_subscribe_fallback_local_attach(self):
        r = SessionRouter(FakeMsp(unserved=("view/subscribe",)))
        conn = FakeConn()
        f = await r.handle_client_message(
            conn, {"id": 1, "type": "subscribe", "sessionId": SID})
        self.assertTrue(f["ok"], f)
        self.assertIn("viewCursor", f["result"])
        self.assertIn(SID, conn.sessions)

    async def test_page_reports_unserved_honestly(self):
        r = SessionRouter(FakeMsp(unserved=("view/page",)))
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "page", "sessionId": SID,
                         "limit": 5, "direction": "backward"})
        self.assertFalse(f["ok"])
        self.assertIn("view/page", f["error"]["message"])


class TestMcpInventory(unittest.TestCase):
    def test_missing_file_reports_hint(self):
        r = read_mcp_servers(path="/nonexistent-dir-xyz/settings.json")
        self.assertEqual(r["servers"], [])
        self.assertFalse(r["configured"])
        self.assertIn("mcpServers", r["hint"])

    def test_parses_entries_without_secrets(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            fh.write('{"mcpServers": {"gh": {"transport": "streamableHttp", '
                     '"url": "https://mcp.example/x", '
                     '"headers": {"Authorization": "Bearer SECRET"}}}}')
            name = fh.name
        try:
            r = read_mcp_servers(path=name)
        finally:
            Path(name).unlink()
        self.assertTrue(r["configured"])
        self.assertEqual(len(r["servers"]), 1)
        srv = r["servers"][0]
        self.assertEqual(srv["name"], "gh")
        self.assertEqual(srv["url"], "https://mcp.example/x")
        self.assertTrue(srv["hasHeaders"])
        self.assertNotIn("SECRET", json.dumps(r))


class TestHostAllowlist(unittest.TestCase):
    def test_loopback_allowed(self):
        for h in ("127.0.0.1", "127.0.0.1:8000", "localhost",
                  "localhost:8000", "LOCALHOST", "[::1]", "[::1]:8000",
                  "127.0.0.1.", None):
            self.assertTrue(host_allowed(h), h)

    def test_foreign_rejected(self):
        for h in ("evil.test", "evil.test:8000", "example.com",
                  "192.168.1.2", "10.0.0.1", "0.0.0.0", "",
                  "127.0.0.1.evil.test"):
            self.assertFalse(host_allowed(h), h)

    def test_extra_hosts_trusted(self):
        extra = normalize_allowed_hosts(["Muse.Kortec.Me", "tun.example:443",
                                         "", "  "])
        self.assertEqual(extra, {"muse.kortec.me", "tun.example"})
        for h in ("muse.kortec.me", "muse.kortec.me:443", "MUSE.KORTEC.ME.",
                  "tun.example"):
            self.assertTrue(host_allowed(h, extra), h)
        self.assertFalse(host_allowed("other.example", extra))
        self.assertFalse(host_allowed("muse.kortec.me.evil.test", extra))
        # Loopback still passes with extras configured.
        self.assertTrue(host_allowed("127.0.0.1:8000", extra))

    def test_flag_and_env_merge(self):
        import os
        from unittest import mock
        from server.main import extra_allowed_hosts, parse_args
        args = parse_args(["--allow-host", "A.Example", "--allow-host",
                           "b.example:8443"])
        with mock.patch.dict(os.environ,
                             {"WEB_MUSE_ALLOWED_HOSTS": " c.example ,,d.example:1 "}):
            got = extra_allowed_hosts(args)
        self.assertEqual(got, {"a.example", "b.example", "c.example",
                               "d.example"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEB_MUSE_ALLOWED_HOSTS", None)
            self.assertEqual(extra_allowed_hosts(parse_args([])), set())


class TestSessionMcpConfig(unittest.TestCase):
    SETTINGS = {"mcpServers": {
        "stdio-srv": {"transport": "stdio", "command": "/bin/echo",
                      "args": ["a"], "env": {"TOK": "s3cret"},
                      "framing": "auto", "mode": "optional"},
        "http-srv": {"transport": "streamableHttp",
                     "url": "https://mcp.example/x",
                     "headers": {"Authorization": "Bearer s3cret"},
                     "mode": "required"},
        "weird": {"command": "/bin/x", "mode": "bogus",
                  "framing": "bogus"},
        "empty": {"transport": "stdio"},
    }}

    def _file(self, payload=None):
        import tempfile
        fh = tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False)
        fh.write(json.dumps(payload if payload is not None
                             else self.SETTINGS))
        fh.close()
        self.addCleanup(Path(fh.name).unlink)
        return fh.name

    def test_stdio_arm(self):
        cfg, warns = build_session_mcp_config(["stdio-srv"],
                                              path=self._file())
        arm = cfg["mcpServers"]["stdio-srv"]
        self.assertEqual(arm["transport"], "stdio")
        self.assertEqual(arm["command"], "/bin/echo")
        self.assertEqual(arm["args"], ["a"])
        self.assertEqual(arm["env"], {"TOK": "s3cret"})
        self.assertEqual(arm["framing"], "auto")
        self.assertEqual(arm["mode"], "optional")
        self.assertEqual(warns, [])

    def test_http_arm(self):
        cfg, warns = build_session_mcp_config(["http-srv"],
                                              path=self._file())
        arm = cfg["mcpServers"]["http-srv"]
        self.assertEqual(arm["transport"], "streamableHttp")
        self.assertEqual(arm["url"], "https://mcp.example/x")
        self.assertEqual(arm["headers"], {"Authorization": "Bearer s3cret"})
        self.assertEqual(arm["mode"], "required")
        self.assertEqual(warns, [])

    def test_unknown_name_lists_known(self):
        with self.assertRaises(ValueError) as cm:
            build_session_mcp_config(["nope"], path=self._file())
        self.assertIn("nope", str(cm.exception))
        self.assertIn("stdio-srv", str(cm.exception))

    def test_invalid_mode_and_framing_dropped(self):
        cfg, warns = build_session_mcp_config(["weird"], path=self._file())
        arm = cfg["mcpServers"]["weird"]
        self.assertNotIn("mode", arm)
        self.assertNotIn("framing", arm)
        self.assertEqual(len(warns), 2)

    def test_entry_without_command_or_url_skipped(self):
        cfg, warns = build_session_mcp_config(["empty"], path=self._file())
        self.assertEqual(cfg, {})
        self.assertEqual(len(warns), 1)

    def test_missing_file_means_no_known_servers(self):
        with self.assertRaises(ValueError) as cm:
            build_session_mcp_config(["x"],
                                     path="/nonexistent-dir-xyz/s.json")
        self.assertIn("(none configured)", str(cm.exception))

    def test_empty_names_is_empty_config(self):
        cfg, warns = build_session_mcp_config([], path=self._file())
        self.assertEqual(cfg, {})
        self.assertEqual(warns, [])


class SessionStartMsp(FakeMsp):
    async def command(self, method, params=None, timeout=60):
        if method == "session/start":
            self.commands.append((method, dict(params or {})))
            return {"session": {"sessionId": SID, "name": "",
                                "status": "active"}}
        return await super().command(method, params, timeout)


class TestNewMcpAttach(unittest.IsolatedAsyncioTestCase):
    SETTINGS = {"mcpServers": {
        "gh": {"transport": "streamableHttp", "url": "https://mcp.example/x"}}}

    async def test_attach_builds_config(self):
        from unittest import mock
        r = SessionRouter(SessionStartMsp())
        with mock.patch("server.sessions.mcp.read_settings_raw",
                        return_value=self.SETTINGS):
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "new",
                             "mcpAttach": ["gh"]})
        self.assertTrue(f["ok"], f)
        method, params = r._msp.commands[0]
        self.assertEqual(method, "session/start")
        arm = params["config"]["mcpServers"]["gh"]
        self.assertEqual(arm["transport"], "streamableHttp")
        self.assertEqual(f["result"]["mcpAttached"], ["gh"])

    async def test_unknown_attach_is_clear_error(self):
        from unittest import mock
        r = SessionRouter(SessionStartMsp())
        with mock.patch("server.sessions.mcp.read_settings_raw",
                        return_value=self.SETTINGS):
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "new",
                             "mcpAttach": ["nope"]})
        self.assertFalse(f["ok"])
        self.assertIn("known", f["error"]["message"])
        self.assertEqual(r._msp.commands, [])

    async def test_non_list_attach_rejected(self):
        r = SessionRouter(SessionStartMsp())
        f = await r.handle_client_message(
            FakeConn(), {"id": 1, "type": "new", "mcpAttach": "gh"})
        self.assertFalse(f["ok"])
        self.assertIn("mcpAttach", f["error"]["message"])

    async def test_explicit_config_wins_per_name(self):
        from unittest import mock
        r = SessionRouter(SessionStartMsp())
        mine = {"transport": "stdio", "command": "/bin/mine"}
        with mock.patch("server.sessions.mcp.read_settings_raw",
                        return_value=self.SETTINGS):
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "new", "mcpAttach": ["gh"],
                             "config": {"mcpServers": {"gh": mine}}})
        self.assertTrue(f["ok"], f)
        arm = r._msp.commands[0][1]["config"]["mcpServers"]["gh"]
        self.assertEqual(arm, mine)


class TestSessionWorkspaceDir(unittest.TestCase):
    def test_uuid_gets_created_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            d = session_workspace_dir(base, SID)
            self.assertTrue(Path(d).is_dir())
            self.assertTrue(Path(d).is_absolute())
            self.assertEqual(Path(d).parent, Path(base).resolve())
            # Idempotent: second call reuses the same dir.
            self.assertEqual(session_workspace_dir(base, SID), d)

    def test_unsafe_ids_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            for bad in ("", "../x", "..", ".", ".hidden", "/abs",
                        "a/b", "a\\b", "x" * 129, "has space"):
                with self.assertRaises(ValueError, msg=bad):
                    session_workspace_dir(base, bad)
            # Nothing escaped the base.
            self.assertEqual(list(Path(base).iterdir()), [])


class TestNewWorkspace(unittest.IsolatedAsyncioTestCase):
    async def test_new_mints_id_and_workspace(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(SessionStartMsp(), workspace_base=base)
            f = await r.handle_client_message(FakeConn(), {"id": 1,
                                                           "type": "new"})
            self.assertTrue(f["ok"], f)
            method, params = r._msp.commands[0]
            self.assertEqual(method, "session/start")
            sid = params["sessionId"]
            self.assertRegex(sid, UUID_RE)  # minted UUIDv7
            wsdir = params["workspaceRoot"]
            self.assertTrue(Path(wsdir).is_dir())
            self.assertEqual(Path(wsdir).parent, Path(base).resolve())
            self.assertEqual(f["result"]["workspaceRoot"], wsdir)

    async def test_client_session_id_reused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(SessionStartMsp(), workspace_base=base)
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "new", "sessionId": SID})
            self.assertTrue(f["ok"], f)
            params = r._msp.commands[0][1]
            self.assertEqual(params["sessionId"], SID)
            self.assertEqual(Path(params["workspaceRoot"]).name, SID)

    async def test_explicit_root_respected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            mine = str(Path(base) / "mine")
            Path(mine).mkdir()
            r = SessionRouter(SessionStartMsp(), workspace_base=base)
            f = await r.handle_client_message(
                FakeConn(), {"id": 1, "type": "new",
                             "workspaceRoot": mine})
            self.assertTrue(f["ok"], f)
            params = r._msp.commands[0][1]
            self.assertEqual(params["workspaceRoot"], mine)
            self.assertNotIn("sessionId", params)  # host mints it
            self.assertEqual(list(Path(base).iterdir()),
                             [Path(mine)])  # nothing else created

    async def test_no_base_preserves_old_behavior(self):
        r = SessionRouter(SessionStartMsp())
        f = await r.handle_client_message(FakeConn(), {"id": 1,
                                                       "type": "new"})
        self.assertTrue(f["ok"], f)
        params = r._msp.commands[0][1]
        self.assertNotIn("workspaceRoot", params)
        self.assertNotIn("sessionId", params)


if __name__ == "__main__":
    unittest.main()
