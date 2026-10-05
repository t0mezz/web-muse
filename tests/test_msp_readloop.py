"""MSP NDJSON reader: single frames bigger than asyncio's 64 KiB pipe
limit must not kill the read loop.

Loading a large old session emits multi-hundred-KiB snapshot/history
lines; StreamReader.readline() raises LimitOverrunError on those and the
old loop died with "host down" while the host was still alive. The reader
below accumulates read() chunks with no cap instead.

Run: python3 -m unittest tests.test_msp_readloop -v   (from repo root)
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.msp import _readline_unlimited  # noqa: E402

MSP_PY = (Path(__file__).resolve().parent.parent
          / "server" / "msp.py").read_text()


class FakeStream:
    """StreamReader stand-in yielding scripted chunks, then EOF."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        if not self._chunks:
            await asyncio.sleep(0)
            return b""
        head = self._chunks.pop(0)
        if len(head) > n:
            self._chunks.insert(0, head[n:])
            return head[:n]
        return head


class TestReadlineUnlimited(unittest.TestCase):
    def test_big_frame_survives(self):
        async def body():
            big = b"x" * 200000 + b"\n"
            stream = FakeStream([big])
            line = await _readline_unlimited(stream, bytearray())
            self.assertEqual(line, big)
        asyncio.run(body())

    def test_split_chunks_and_carry_over(self):
        async def body():
            buf = bytearray()
            stream = FakeStream([b"ab", b"cd\nef", b"g\nrest"])
            self.assertEqual(await _readline_unlimited(stream, buf),
                             b"abcd\n")
            self.assertEqual(await _readline_unlimited(stream, buf), b"efg\n")
            # Over-read bytes stay buffered for the next call.
            self.assertEqual(await _readline_unlimited(stream, buf), b"rest")
        asyncio.run(body())

    def test_eof_partial_then_empty(self):
        async def body():
            buf = bytearray()
            stream = FakeStream([b"tail-no-newline"])
            self.assertEqual(await _readline_unlimited(stream, buf),
                             b"tail-no-newline")
            self.assertEqual(await _readline_unlimited(stream, buf), b"")
        asyncio.run(body())

    def test_against_real_stream_reader(self):
        async def body():
            reader = asyncio.StreamReader()
            reader.feed_data(b"y" * 100000 + b"\n")
            reader.feed_eof()
            line = await _readline_unlimited(reader, bytearray())
            self.assertEqual(len(line), 100001)
            self.assertEqual(await _readline_unlimited(reader, bytearray()),
                             b"")
        asyncio.run(body())


class TestReadLoopWiring(unittest.TestCase):
    def test_no_bare_readline_on_pipes(self):
        self.assertNotIn(".stdout.readline()", MSP_PY)
        self.assertNotIn(".stderr.readline()", MSP_PY)
        self.assertIn("_readline_unlimited(self._proc.stdout", MSP_PY)
        self.assertIn("_readline_unlimited(self._proc.stderr", MSP_PY)


if __name__ == "__main__":
    unittest.main()
