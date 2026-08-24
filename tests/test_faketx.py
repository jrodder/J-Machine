"""Fake-transport harness (plan Task 13): every byte through the
network-shaped channel must yield the same transcript as the oracle."""
import unittest
from pathlib import Path

from tests.util import dfrotz_transcript, norm
from tests.faketx.channel import FakeChannel

C = Path(__file__).parent / "corpus"
LINES = ["look", "open mailbox", "take leaflet", "west", "north", "quit"]


class TestFakeTransport(unittest.TestCase):
    def test_chunked_channel_matches_local(self):
        ch = FakeChannel(chunk=7)   # tiny chunks, worst-case boundaries
        ch.load(C / "zork1.z3", seed=10)
        out = ch.drain()
        for line in LINES:
            ch.send_input(line)
            out += ch.drain()
        ours = norm(out)
        ref = norm(dfrotz_transcript(C / "zork1.z3", LINES, seed=10))
        self.assertEqual(ours, ref)


if __name__ == "__main__":
    unittest.main()