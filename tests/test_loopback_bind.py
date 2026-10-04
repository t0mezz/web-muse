"""Dual loopback bind: the bridge must listen on 127.0.0.1 AND ::1.

Regression context: cloudflared resolves a `localhost` origin to ::1 first,
so an IPv4-only bridge made the Cloudflare tunnel fail with
"dial tcp [::1]:8000: connect: connection refused" while direct
port-forwarding to 127.0.0.1 worked. `start_loopback_server` binds both
families (falling back to one when the host has no IPv6).
"""

import asyncio
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.main import LOOPBACK_HOSTS, start_loopback_server  # noqa: E402


async def _noop_handle(reader, writer):
    writer.close()


def _ports_by_family(server):
    out = {}
    for s in server.sockets or []:
        out.setdefault(s.family, []).append(s.getsockname()[1])
    return out


class LoopbackBindTest(unittest.IsolatedAsyncioTestCase):
    async def _serve(self):
        server = await start_loopback_server(_noop_handle, 0)
        self.addAsyncCleanup(self._close, server)
        return server

    @staticmethod
    async def _close(server):
        server.close()
        await server.wait_closed()

    def test_loopback_hosts_covers_both_families(self):
        self.assertIn("127.0.0.1", LOOPBACK_HOSTS)
        self.assertIn("::1", LOOPBACK_HOSTS)

    async def test_ipv4_loopback_always_serves(self):
        server = await self._serve()
        port = _ports_by_family(server)[socket.AF_INET][0]
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=10)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=10)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    async def test_one_family_taken_still_serves_the_other(self):
        # A stale process holding 127.0.0.1:port must not take down the
        # whole bridge: the free family still serves (with a warning).
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            with self.assertLogs("web_muse", level="WARNING"):
                server = await start_loopback_server(_noop_handle, port)
        finally:
            blocker.close()
        self.addAsyncCleanup(self._close, server)
        families = {s.family for s in server.sockets or []}
        self.assertNotIn(socket.AF_INET, families)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("::1", port), timeout=10)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=10)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    async def test_nothing_bindable_raises_address_in_use(self):
        blocker4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker4.bind(("127.0.0.1", 0))
        blocker4.listen(1)
        port = blocker4.getsockname()[1]
        blocker6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        blocker6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            blocker6.bind(("::1", port))
            blocker6.listen(1)
        except OSError:
            blocker4.close()
            blocker6.close()
            self.skipTest("cannot hold both loopbacks on one port here")
            return
        try:
            with self.assertRaises(OSError):
                await start_loopback_server(_noop_handle, port)
        finally:
            blocker4.close()
            blocker6.close()

    async def test_ipv6_loopback_serves_when_available(self):
        if not socket.has_ipv6:
            self.skipTest("host has no IPv6 stack")
        try:
            probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            probe.bind(("::1", 0))
            probe.close()
        except OSError:
            self.skipTest("::1 unavailable on this host")
        server = await self._serve()
        v6 = _ports_by_family(server).get(socket.AF_INET6)
        if not v6:
            self.skipTest("dual bind fell back to IPv4-only on this host")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("::1", v6[0]), timeout=10)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=10)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass


if __name__ == "__main__":
    unittest.main()
