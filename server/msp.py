"""MSP client: speaks newline-delimited JSON-RPC to one persistent `muse serve` child.

Verified against Muse 1.4.2, schema fingerprint
sha256:61afea3112e0906e9dc3a536144278a74cb4b36fc6e20901a91d4432ba3568e2 (v1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time

LOG = logging.getLogger("web_muse.msp")

EXPECTED_FINGERPRINT = (
    "sha256:61afea3112e0906e9dc3a536144278a74cb4b36fc6e20901a91d4432ba3568e2"
)
EXPECTED_SCHEMA_VERSION = 1

CLIENT_NAME = "web_muse"
CLIENT_VERSION = "0.1.0"


def uuid7() -> str:
    """Fresh UUIDv7 (48-bit unix_ms + ver + 12-bit rand + variant + 62-bit rand)."""
    ts = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand_a = random.getrandbits(12)
    rand_b = random.getrandbits(62)
    v = (ts << 80) | (7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    h = "%032x" % v
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


async def _readline_unlimited(stream, buf):
    """Read one newline-terminated line with no size cap.

    asyncio's StreamReader.readline() enforces the default 64 KiB buffer
    limit: a single NDJSON frame bigger than that raises LimitOverrunError
    (surfaced as ValueError). Loading a large old session emits
    multi-hundred-KiB snapshot/history lines, which used to take down the
    whole MSP read loop and wedge the bridge as "host down" while the host
    was still alive (exit=None). The host is a trusted local child, so
    accumulate read() chunks instead; `buf` carries over-read bytes across
    calls. Returns b"" only on true EOF.
    """
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            line = bytes(buf[:nl + 1])
            del buf[:nl + 1]
            return line
        chunk = await stream.read(65536)
        if not chunk:
            if buf:
                line = bytes(buf)
                buf.clear()
                return line
            return b""
        buf += chunk


class MspError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(f"msp error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class MspClient:
    """Owns one `muse serve` child process over stdio NDJSON JSON-RPC."""

    def __init__(self, serve_argv, on_notification=None, on_server_request=None,
                 loop=None):
        self._argv = list(serve_argv)
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._loop = loop
        self._proc = None
        self._reader_task = None
        self._stderr_task = None
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._pending = {}  # jsonrpc id -> Future
        self._handshake_done = asyncio.Event()
        self._stopping = False
        self.server_info = {}
        self.schema = {}
        self.granted_capabilities = []

    @property
    def alive(self):
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._handshake_done.is_set()
        )

    async def ensure_started(self):
        if self.alive:
            return
        if self._proc is not None and self._proc.returncode is None:
            # Child running but handshake not done yet: wait for it.
            await asyncio.wait_for(self._handshake_done.wait(), timeout=30)
            return
        await self.start()

    async def start(self):
        if self._proc is not None and self._proc.returncode is None:
            return
        self._stopping = False
        self._handshake_done.clear()
        LOG.info("spawning: %s", " ".join(self._argv))
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Inherit env/HOME: subscription credentials pass through untouched.
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="msp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="msp-stderr")
        # NOTE: _call_raw, not call(): call() -> ensure_started() would wait on
        # the handshake event this very method is about to complete (deadlock).
        result = await self._call_raw(
            "initialize",
            {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}},
            timeout=30,
        )
        self.server_info = result.get("serverInfo", {})
        self.schema = result.get("schema", {})
        self.granted_capabilities = result.get("grantedCapabilities", [])
        fp = self.schema.get("fingerprint", "")
        ver = self.schema.get("version")
        if fp != EXPECTED_FINGERPRINT or ver != EXPECTED_SCHEMA_VERSION:
            LOG.warning(
                "MSP schema mismatch: got %s v%s, pinned %s v%s; "
                "continuing, mapping may be stale",
                fp, ver, EXPECTED_FINGERPRINT, EXPECTED_SCHEMA_VERSION,
            )
        else:
            LOG.info("MSP handshake ok: %s v%s (%s)", fp, ver, self.server_info)
        await self.notify("initialized", {})
        self._handshake_done.set()

    async def stop(self):
        self._stopping = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(MspError(-32000, "msp client stopping"))
        self._pending.clear()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._proc = None

    async def call(self, method, params=None, timeout=60):
        await self.ensure_started()
        return await self._call_raw(method, params, timeout)

    async def _call_raw(self, method, params=None, timeout=60):
        if self._proc is None or self._proc.returncode is not None:
            raise MspError(-32000, "msp child not running")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                          "params": params or {}})
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            err = msg["error"] or {}
            raise MspError(err.get("code", -32000),
                           err.get("message", "unknown error"),
                           err.get("data"))
        return msg.get("result", {})

    async def command(self, method, params=None, timeout=60):
        """MSP command call: inject a fresh UUIDv7 commandId per call."""
        p = dict(params or {})
        p["commandId"] = uuid7()
        return await self.call(method, p, timeout=timeout)

    async def notify(self, method, params=None):
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send(self, obj):
        if self._proc is None or self._proc.stdin is None:
            raise MspError(-32000, "msp child not running")
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _read_loop(self):
        assert self._proc is not None and self._proc.stdout is not None
        buf = bytearray()
        try:
            while True:
                line = await _readline_unlimited(self._proc.stdout, buf)
                if not line:
                    break  # EOF: child exited
                try:
                    msg = json.loads(line.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    LOG.warning("dropping undecodable MSP line: %r", line[:200])
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("MSP read loop failed")
        finally:
            if not self._stopping:
                LOG.error("msp child stdout closed (exit=%s)",
                          self._proc.returncode if self._proc else "?")
                self._handshake_done.clear()
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(MspError(-32000, "msp child exited"))
                self._pending.clear()

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if "id" in msg and "method" in msg:
            # Server-initiated must-answer request (approval/request, ...).
            asyncio.create_task(self._handle_server_request(msg))
            return
        method = msg.get("method")
        if method and self._on_notification is not None:
            try:
                self._on_notification(method, msg.get("params", {}))
            except Exception:
                LOG.exception("notification handler failed for %s", method)

    async def _handle_server_request(self, msg):
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}
        rid = msg.get("id")
        # SS5.3.3: acknowledge presentation only; decision travels as command.
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {}})
        except Exception:
            LOG.exception("failed to receipt server request %s", method)
            return
        if self._on_server_request is not None:
            try:
                self._on_server_request(method, params)
            except Exception:
                LOG.exception("server-request handler failed for %s", method)

    async def _stderr_loop(self):
        assert self._proc is not None and self._proc.stderr is not None
        buf = bytearray()
        try:
            while True:
                line = await _readline_unlimited(self._proc.stderr, buf)
                if not line:
                    break
                LOG.info("serve-stderr: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("MSP stderr loop failed")
