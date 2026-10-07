"""MSP call hygiene: a failed send must not leak the pending entry.

When the serve child's stdin pipe is dead (host crashed mid-call),
_call_raw used to let the raw OSError escape and left the request id in
_pending with a forever-pending future. The failure must surface as a
typed MspError(-32000) and leave no residue, so the client stays
reusable and later responses can't trip on stale entries.

Run: python3 -m unittest tests.test_msp_send_fail -v   (from repo root)
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.msp import MspClient, MspError  # noqa: E402


class BrokenStdin:
    def write(self, data):
        raise BrokenPipeError("closed")

    async def drain(self):
        pass


class HangingStdin:
    def write(self, data):
        pass

    async def drain(self):
        await asyncio.sleep(3600)


class SilentStdin:
    """Accepts the write but never answers: forces a response timeout."""

    def write(self, data):
        pass

    async def drain(self):
        pass


class FakeProc:
    returncode = None
    stdout = None
    stderr = None

    def __init__(self, stdin):
        self.stdin = stdin


class TestSendFailure(unittest.IsolatedAsyncioTestCase):
    async def test_broken_pipe_raises_msp_error_and_clears_pending(self):
        client = MspClient(["muse", "serve"])
        client._proc = FakeProc(BrokenStdin())
        with self.assertRaises(MspError) as ctx:
            await client._call_raw("session/list", {}, timeout=5)
        self.assertEqual(ctx.exception.code, -32000)
        self.assertEqual(client._pending, {})
        # Client stays reusable: a second failure is just as clean.
        with self.assertRaises(MspError):
            await client._call_raw("session/list", {}, timeout=5)
        self.assertEqual(client._pending, {})

    async def test_cancelled_waiter_clears_pending(self):
        client = MspClient(["muse", "serve"])
        client._proc = FakeProc(HangingStdin())
        task = asyncio.create_task(
            client._call_raw("session/list", {}, timeout=30))
        await asyncio.sleep(0.05)
        self.assertEqual(len(client._pending), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client._pending, {})

    async def test_response_timeout_raises_msp_error_and_clears_pending(self):
        client = MspClient(["muse", "serve"])
        client._proc = FakeProc(SilentStdin())
        with self.assertRaises(MspError) as ctx:
            await client._call_raw("session/list", {}, timeout=0.05)
        self.assertEqual(ctx.exception.code, -32000)
        self.assertEqual(client._pending, {})


if __name__ == "__main__":
    unittest.main()
