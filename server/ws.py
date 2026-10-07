"""Stdlib-only HTTP + WebSocket server (no third-party deps).

One asyncio TCP server on loopback: serves static files, /health, and the
/ws endpoint with a minimal RFC 6455 implementation (text frames, ping/pong,
close, fragmented reassembly).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import struct
import time
from pathlib import Path

LOG = logging.getLogger("web_muse.ws")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 16 * 1024 * 1024  # 16 MiB cap per message (image prompts)

# Host allowlist (DNS-rebinding guard): the socket already binds loopback
# only, but a browser page on evil.test resolving to 127.0.0.1 would still
# reach us — so requests carrying a non-loopback Host are rejected.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def normalize_host(value):
    """Lowercase hostname without port/brackets/trailing dot; "" if unusable."""
    try:
        host = (value or "").strip().lower()
        if host.startswith("["):  # [::1]:port
            host = host[1:].split("]", 1)[0]
        else:
            host = host.split(":", 1)[0]
        return host.strip().rstrip(".")
    except Exception:
        return ""


def host_allowed(host_header, extra=None):
    """True when the Host header names this loopback bridge.

    Missing Host (non-browser raw clients) is allowed; a present but
    foreign Host is rejected unless listed in `extra` (tunnel hostnames
    the operator explicitly trusts). Never raises.
    """
    if host_header is None:
        return True
    host = normalize_host(host_header)
    if host in ALLOWED_HOSTS:
        return True
    return bool(host) and host in (extra or frozenset())


def normalize_allowed_hosts(names):
    """Configured tunnel hostnames -> normalized set (drops empties)."""
    out = set()
    for n in names or []:
        h = normalize_host(n)
        if h:
            out.add(h)
    return out

OP_CONT = 0x0
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

THEMES_SUBDIR = "themes"


def list_themes(web_dir):
    """Sorted saved-theme names (`<web_dir>/themes/*.json` stems).

    Dropping a `<name>.json` file into the themes dir is what makes it
    appear under bare `/theme`. Never raises: a missing or unreadable
    dir reads as no themes.
    """
    try:
        tdir = Path(web_dir) / THEMES_SUBDIR
        if not tdir.is_dir():
            return []
        return sorted(p.stem for p in tdir.iterdir()
                      if p.is_file() and p.suffix.lower() == ".json")
    except OSError:
        return []


def ws_accept_key(client_key: str) -> str:
    h = hashlib.sha1((client_key.strip() + WS_GUID).encode())
    return base64.b64encode(h.digest()).decode()


def encode_frame(opcode, payload: bytes, fin=True):
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, n)
    elif n < 65536:
        head = struct.pack("!BBH", b0, 126, n)
    else:
        head = struct.pack("!BBQ", b0, 127, n)
    return head + payload


class WsConnection:
    """One accepted WebSocket connection (server side)."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.sessions = set()  # MSP sessionIds this conn follows
        self._send_lock = asyncio.Lock()
        self._outbox = asyncio.Queue()
        self._closed = False

    def queue_frame(self, obj):
        if not self._closed:
            self._outbox.put_nowait(obj)

    async def send_text(self, text: str):
        data = encode_frame(OP_TEXT, text.encode())
        async with self._send_lock:
            self.writer.write(data)
            await self.writer.drain()

    async def send_obj(self, obj):
        await self.send_text(json.dumps(obj))

    async def _send_raw(self, opcode, payload=b""):
        data = encode_frame(opcode, payload)
        async with self._send_lock:
            self.writer.write(data)
            await self.writer.drain()

    async def recv_messages(self):
        """Async generator of decoded text messages (str)."""
        buf = bytearray()
        cur_op = None
        while True:
            hdr = await self.reader.readexactly(2)
            b0, b1 = hdr[0], hdr[1]
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if ln > MAX_FRAME:
                raise ValueError("frame too large")
            mask = await self.reader.readexactly(4) if masked else None
            payload = await self.reader.readexactly(ln) if ln else b""
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if op == OP_CLOSE:
                return
            if op == OP_PING:
                await self._send_raw(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CONT:
                if cur_op is None:
                    raise ValueError("stray continuation frame")
                buf += payload
            elif op == OP_TEXT:
                if cur_op is not None:
                    raise ValueError("interleaved data frame")
                cur_op = op
                buf = bytearray(payload)
            else:
                raise ValueError(f"unsupported opcode {op}")
            if len(buf) > MAX_FRAME:
                raise ValueError("message too large")
            if fin:
                try:
                    yield bytes(buf).decode()
                finally:
                    buf = bytearray()
                    cur_op = None

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_raw(OP_CLOSE, b"")
        except (ConnectionError, asyncio.IncompleteReadError, RuntimeError):
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass


class HttpWsServer:
    def __init__(self, router, web_dir, on_ws_open=None, allowed_hosts=None):
        self.router = router
        self.web_dir = Path(web_dir)
        self.on_ws_open = on_ws_open
        self.allowed_hosts = normalize_allowed_hosts(allowed_hosts)
        self.started_at = time.time()

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        try:
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
        except ValueError:
            writer.close()
            return
        if not host_allowed(headers.get("host"), self.allowed_hosts):
            await self._respond(writer, 403, b"foreign host rejected\n",
                                "text/plain")
            return
        if headers.get("upgrade", "").lower() == "websocket" and \
                target in ("/ws", "/ws/"):
            key = headers.get("sec-websocket-key", "")
            if not key:
                writer.close()
                return
            await self._serve_ws(reader, writer, key)
            return
        if method != "GET":
            await self._respond(writer, 405, b"method not allowed\n",
                                "text/plain")
            return
        await self._serve_http(writer, target.split("?", 1)[0])

    async def _serve_ws(self, reader, writer, key):
        accept = ws_accept_key(key)
        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode())
        await writer.drain()
        conn = WsConnection(reader, writer)
        self.router.add_conn(conn)
        if self.on_ws_open is not None:
            try:
                hello = self.on_ws_open()
                if hello is not None:
                    conn.queue_frame(hello)
            except Exception:
                LOG.exception("hello handler failed")
        pump = asyncio.create_task(self._pump_outbox(conn), name="ws-pump")
        try:
            async for text in conn.recv_messages():
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    await conn.send_obj({"type": "result", "ok": False,
                                         "error": {"message": "invalid JSON"}})
                    continue
                try:
                    reply = await self.router.handle_client_message(conn, msg)
                except Exception:
                    LOG.exception("client message handler failed")
                    reply = {"type": "result", "ok": False,
                             "error": {"message": "internal error"}}
                if reply is not None:
                    await conn.send_obj(reply)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
            LOG.info("ws conn ended: %s", e)
        finally:
            pump.cancel()
            self.router.drop_conn(conn)
            await conn.close()

    async def _pump_outbox(self, conn):
        try:
            while True:
                obj = await conn._outbox.get()
                try:
                    await conn.send_obj(obj)
                except (ConnectionError, RuntimeError, asyncio.IncompleteReadError):
                    break
        except asyncio.CancelledError:
            pass

    async def _serve_http(self, writer, path):
        if path == "/health":
            body = json.dumps(self._health()).encode()
            await self._respond(writer, 200, body, "application/json")
            return
        if path == "/themes":
            await self._serve_themes(writer)
            return
        rel = path.lstrip("/") or "index.html"
        if ".." in rel or rel.startswith("/"):
            await self._respond(writer, 400, b"bad path\n", "text/plain")
            return
        fpath = self.web_dir / rel
        if fpath.is_dir():
            fpath = fpath / "index.html"
        if not fpath.is_file():
            # SPA fallback for extensionless client routes only; a missing
            # dotted asset path is a real 404, not the app shell.
            last = rel.rsplit("/", 1)[-1]
            if "." in last:
                await self._respond(writer, 404, b"not found\n", "text/plain")
                return
            fpath = self.web_dir / "index.html"
            if not fpath.is_file():
                await self._respond(writer, 404, b"not found\n", "text/plain")
                return
        ctype, _ = mimetypes.guess_type(str(fpath))
        try:
            data = fpath.read_bytes()
        except OSError:
            await self._respond(writer, 404, b"not found\n", "text/plain")
            return
        await self._respond(writer, 200, data, ctype or "application/octet-stream")

    async def _serve_themes(self, writer):
        """Saved-theme names as JSON (bare `/theme` lists these)."""
        body = json.dumps({"themes": list_themes(self.web_dir)}).encode()
        await self._respond(writer, 200, body, "application/json")

    def _health(self):
        msp = getattr(self.router, "_msp", None)
        conns = getattr(self.router, "_conns", set()) or set()
        subs = getattr(self.router, "_subs", {}) or {}
        return {
            "ok": True,
            "mspAlive": bool(msp and msp.alive),
            "server": getattr(msp, "server_info", {}),
            "schema": getattr(msp, "schema", {}),
            "uptimeS": round(time.time() - self.started_at, 1),
            "wsConns": len(conns),
            "sessionsTracked": len(subs),
        }

    async def _respond(self, writer, code, body: bytes, ctype):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden",
                  404: "Not Found", 405: "Method Not Allowed"}.get(code, "OK")
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            # The UI ships from this same process on every edit: never let
            # browsers (notably mobile Safari) run a stale cached copy.
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()
