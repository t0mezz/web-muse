"""Full-stack smoke test: HTTP + WebSocket against a live echo bridge.

Spins the real server (MspClient over `muse serve --provider echo` plus the
stdlib HTTP+WS front) on an ephemeral loopback port and drives it with a
minimal raw-socket WS client. No third-party deps.

Run:  python3 -m unittest tests.test_smoke -v   (from repo root)
"""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.msp import MspClient  # noqa: E402
from server.sessions import SessionRouter  # noqa: E402
from server.ws import HttpWsServer  # noqa: E402

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def serve_argv():
    # Durable mode: --no-session-log hosts emit NO transcript events
    # (running->idle only), so streaming assertions need the session log.
    return ["muse", "serve", "--provider", "echo"]


class RawWs:
    """Minimal asyncio WS client (masked text sends, fragmented recv)."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self._buf = bytearray()

    @classmethod
    async def connect(cls, host, port):
        reader, writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((
            "GET /ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        status = head.decode("latin-1").split("\r\n", 1)[0]
        assert "101" in status, f"expected 101, got {status!r}"
        expect = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        assert expect in head.decode("latin-1"), "bad accept key"
        return cls(reader, writer)

    async def send_obj(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        hdr = bytes([0x81, 0x80 | len(data)]) if len(data) < 126 else (
            struct.pack("!BBH", 0x81, 0x80 | 126, len(data)))
        if len(data) >= 65536:  # pragma: no cover (test payloads are small)
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, len(data))
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        self.writer.write(hdr + mask + body)
        await self.writer.drain()

    async def recv_obj(self, timeout=30):
        text = await self._recv_text(timeout)
        return json.loads(text)

    async def _recv_text(self, timeout):
        async def rd(n):
            return await asyncio.wait_for(self.reader.readexactly(n), timeout)
        msg = bytearray()
        while True:
            hdr = await rd(2)
            b0, b1 = hdr[0], hdr[1]
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", await rd(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", await rd(8))[0]
            if b1 & 0x80:  # server must not mask, but tolerate it
                mask = await rd(4)
            else:
                mask = None
            payload = await rd(ln) if ln else b""
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if op == 0x9:  # ping -> pong
                self.writer.write(
                    struct.pack("!BB", 0x8A, len(payload)) + payload)
                await self.writer.drain()
                continue
            if op == 0x8:
                raise ConnectionError("ws closed by server")
            if op in (0x1, 0x0):
                msg += payload
                if fin:
                    return bytes(msg).decode()
            # ignore other opcodes


class SmokeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Same wiring as server/main.py: host notifications/requests must
        # reach the router or no live event ever fans out to browsers.
        self.msp = MspClient(
            serve_argv(),
            on_notification=lambda m, p: self.router.on_notification(m, p),
            on_server_request=lambda m, p: self.router.on_server_request(m, p),
        )
        self.router = SessionRouter(self.msp)
        await self.msp.start()
        self.assertTrue(self.msp.alive, "echo serve host did not come up")

        def hello():
            return {"type": "hello", "server": self.msp.server_info,
                    "schema": self.msp.schema, "mspAlive": self.msp.alive}

        web_dir = str(Path(__file__).resolve().parent.parent / "web")
        self.httpd = HttpWsServer(self.router, web_dir, on_ws_open=hello)
        self.server = await asyncio.start_server(
            self.httpd.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        # Close test WS clients FIRST so server.wait_closed() can't wedge on
        # an orphaned open connection, then bound every shutdown await.
        for ws in getattr(self, "_clients", []):
            try:
                ws.writer.close()
                await asyncio.wait_for(ws.writer.wait_closed(), timeout=5)
            except (Exception, asyncio.TimeoutError):
                pass
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), timeout=10)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(self.msp.stop(), timeout=15)
        except asyncio.TimeoutError:
            pass

    def http_get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    async def test_http_surfaces(self):
        st, _, body = await asyncio.to_thread(self.http_get, "/")
        self.assertEqual(st, 200)
        self.assertIn(b"web-muse", body)
        self.assertIn(b"progress.html", body)  # topbar link present
        st, _, body = await asyncio.to_thread(self.http_get, "/health")
        self.assertEqual(st, 200)
        h = json.loads(body)
        self.assertTrue(h["ok"])
        self.assertTrue(h["mspAlive"])
        self.assertIn("uptimeS", h)
        self.assertIn("wsConns", h)
        st, _, body = await asyncio.to_thread(self.http_get, "/progress.html")
        self.assertEqual(st, 200)
        self.assertIn(b"Bridge progress", body)
        self.assertIn(b"/health", body)
        for asset in ("/app.js", "/style.css"):
            st, hdrs, body = await asyncio.to_thread(self.http_get, asset)
            self.assertEqual(st, 200, asset)
            self.assertTrue(len(body) > 1000, asset)
            # UI must never run stale from browser cache.
            self.assertEqual(hdrs.get("Cache-Control"), "no-store", asset)
        # Missing dotted asset -> real 404 (not the SPA shell); extensionless
        # client routes still fall back to the shell.
        st, _, _ = await asyncio.to_thread(self.http_get, "/missing-asset.js")
        self.assertEqual(st, 404)
        st, _, body = await asyncio.to_thread(self.http_get,
                                              "/some-client-route")
        self.assertEqual(st, 200)
        self.assertIn(b"web-muse", body)

    async def test_host_allowlist(self):
        st, _, _ = await asyncio.to_thread(
            self.http_get, "/health", {"Host": "evil.test"})
        self.assertEqual(st, 403)
        st, _, _ = await asyncio.to_thread(
            self.http_get, "/health", {"Host": "127.0.0.1"})
        self.assertEqual(st, 200)
        # WebSocket upgrade with a foreign Host is rejected (no 101).
        reader, writer = await asyncio.open_connection("127.0.0.1",
                                                       self.port)
        try:
            writer.write((
                "GET /ws HTTP/1.1\r\n"
                "Host: evil.test\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            await writer.drain()
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), 10)
            status = head.decode("latin-1").split("\r\n", 1)[0]
            self.assertIn("403", status, status)
        finally:
            writer.close()

    async def test_new_unknown_mcp_attach(self):
        ws = await RawWs.connect("127.0.0.1", self.port)
        self._clients = getattr(self, "_clients", []) + [ws]
        hello = await ws.recv_obj()
        self.assertEqual(hello["type"], "hello")
        await ws.send_obj({"id": 1, "type": "new",
                           "mcpAttach": ["no-such-server-xyz"]})
        while True:
            f = await ws.recv_obj(timeout=30)
            if f.get("type") == "result" and f.get("id") == 1:
                self.assertFalse(f["ok"], f)
                self.assertIn("known", f["error"]["message"])
                break
        ws.writer.close()

    async def test_ws_full_interaction(self):
        ws = await RawWs.connect("127.0.0.1", self.port)
        self._clients = getattr(self, "_clients", []) + [ws]
        hello = await ws.recv_obj()
        self.assertEqual(hello["type"], "hello")
        self.assertTrue(hello["mspAlive"])

        rid = 0

        async def call(msg, timeout=30):
            nonlocal rid
            rid += 1
            msg["id"] = rid
            await ws.send_obj(msg)
            while True:
                f = await ws.recv_obj(timeout)
                if f.get("type") == "result" and f.get("id") == rid:
                    return f

        # --- prompt round-trip (auto-creates a session, echo streams back)
        rid += 1
        pid = rid
        await ws.send_obj({"id": pid, "type": "prompt",
                           "text": "hello smoke"})
        ack = None
        saw_delta = False
        saw_completed = False
        session_id = None

        async def drain_turn():
            nonlocal ack, saw_delta, saw_completed, session_id
            while not (ack and saw_completed):
                f = await ws.recv_obj(timeout=60)
                if f.get("type") == "result" and f.get("id") == pid:
                    ack = f
                    self.assertTrue(ack["ok"], ack)
                    session_id = ack["result"]["sessionId"]
                elif f.get("type") == "event" and f.get("method") == "item/delta":
                    saw_delta = True
                elif f.get("type") == "event" and f.get("method") == "turn/completed":
                    saw_completed = True
                    self.assertEqual(f["params"].get("terminal"), "completed")

        # Overall deadline: per-recv timeouts alone can't catch an endless
        # stream of non-terminal events.
        await asyncio.wait_for(drain_turn(), timeout=120)
        self.assertTrue(session_id)
        self.assertTrue(saw_delta, "no item/delta streamed for echo turn")

        # --- models (/models + /model backend)
        r = await call({"type": "models", "sessionId": session_id})
        self.assertTrue(r["ok"], r)
        self.assertTrue(len(r["result"].get("models", [])) >= 1)
        first = r["result"]["models"][0]["modelId"]
        # Echo has no model runtime: the host rejects setModel
        # (invalid_target); on meta it is accepted. Either way the bridge
        # must pass a decision through, never hang or crash.
        r = await call({"type": "setModel", "sessionId": session_id,
                        "model": {"modelId": first}})
        if r["ok"]:
            self.assertEqual(r["result"].get("status"), "accepted")
        else:
            self.assertIn("model", r["error"]["message"].lower(), r)

        # --- usage (/usage backend: {usage?}, omitted until observed)
        r = await call({"type": "usage"})
        self.assertTrue(r["ok"], r)
        self.assertIsInstance(r["result"], dict)

        # --- mcp (/mcp backend: local settings inventory)
        r = await call({"type": "mcp"})
        self.assertTrue(r["ok"], r)
        self.assertIn("servers", r["result"])
        self.assertIsInstance(r["result"]["servers"], list)

        # --- skills + effort (/skills, /effort backends)
        r = await call({"type": "skills", "sessionId": session_id})
        self.assertTrue(r["ok"], r)
        self.assertIn("skills", r["result"])
        self.assertTrue(len(r["result"]["skills"]) >= 1)
        r = await call({"type": "setEffort", "sessionId": session_id,
                        "reasoningEffort": "low"})
        self.assertTrue(r["ok"], r)
        r = await call({"type": "setEffort", "sessionId": session_id,
                        "reasoningEffort": "turbo"})
        self.assertFalse(r["ok"])
        self.assertIn("reasoning effort", r["error"]["message"])

        # --- resume fallback (host serves no session/resume): metadata +
        # honest none-history, same envelope shape as a real resume.
        r = await call({"type": "resume", "sessionId": session_id})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["session"]["sessionId"], session_id)
        hist = r["result"].get("history", {})
        if r["result"].get("fallback") == "resume_unserved_by_host":
            self.assertEqual(hist.get("mode"), "none")
        else:
            self.assertIn(hist.get("mode"),
                          ("inline", "snapshot", "anchoredSnapshot", "none"))

        # --- subscribe fallback (host serves no view/subscribe): local
        # attach with the last-known cursor.
        r = await call({"type": "subscribe", "sessionId": session_id})
        self.assertTrue(r["ok"], r)
        self.assertIn("viewCursor", r["result"])

        # --- page: served hosts return {events, nextCursor}; this host
        # answers -32601 and the bridge reports it honestly.
        r = await call({"type": "page", "sessionId": session_id,
                        "limit": 10, "direction": "backward"})
        if r["ok"]:
            self.assertIn("events", r["result"])
            self.assertIn("nextCursor", r["result"])
        else:
            self.assertIn("view/page", r["error"]["message"])
        r = await call({"type": "page", "sessionId": session_id,
                        "limit": 10, "direction": "before"})
        self.assertFalse(r["ok"])
        self.assertIn("direction", r["error"]["message"])

        # --- interrupt on an idle session: honest host rejection passes thru.
        r = await call({"type": "interrupt", "sessionId": session_id})
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"]["message"], r)

        # --- guards: unknown type + allowAll + bad approval mode
        r = await call({"type": "nope"})
        self.assertFalse(r["ok"])
        r = await call({"type": "setApprovalMode",
                        "sessionId": session_id, "mode": "allowAll"})
        self.assertFalse(r["ok"])
        r = await call({"type": "setApprovalMode",
                        "sessionId": session_id, "mode": "never"})
        self.assertFalse(r["ok"])
        self.assertIn("approval mode", r["error"]["message"])

        # --- session list shows our session
        r = await call({"type": "list", "limit": 50})
        self.assertTrue(r["ok"], r)
        ids = [s["sessionId"] for s in r["result"].get("sessions", [])]
        self.assertIn(session_id, ids)

        # --- fork works (returns the resume envelope for the new session)
        r = await call({"type": "fork", "sessionId": session_id})
        self.assertTrue(r["ok"], r)
        fork_id = r["result"]["session"]["sessionId"]
        self.assertNotEqual(fork_id, session_id)

        # --- cleanup: delete what the smoke created (best effort)
        for sid in (fork_id, session_id):
            await call({"type": "delete", "sessionId": sid})

        ws.writer.close()


if __name__ == "__main__":
    unittest.main()
