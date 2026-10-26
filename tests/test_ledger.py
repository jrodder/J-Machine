"""Spec §3 ledger: the record is what makes "nothing happened" readable."""
import json
import tempfile
import unittest
from pathlib import Path

from jhost.ledger import STATE_OPEN, Ledger


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = Path(self.d.name) / "ledger.jsonl"

    def tearDown(self):
        self.d.cleanup()

    def lines(self):
        return [json.loads(l) for l in self.p.read_text().splitlines()]

    def test_open_then_close_leaves_nothing_unresolved(self):
        led = Ledger(self.p, boot="ffff")
        seq = led.open("zork1", "aabbccdd")
        led.close(seq, "delivered", bytes_in=4, bytes_out=120)
        self.assertEqual(led.unresolved(), [])
        self.assertEqual([r["state"] for r in self.lines()],
                         [STATE_OPEN, "delivered"])
        self.assertEqual(self.lines()[1]["bytes_out"], 120)

    def test_an_open_turn_is_the_crash_fingerprint(self):
        led = Ledger(self.p, boot="ffff")
        led.open("zork1", "aabbccdd")
        self.assertEqual(len(led.unresolved()), 1)
        self.assertEqual(led.unresolved()[0]["game"], "zork1")

    def test_a_second_terminal_line_is_kept_not_merged(self):
        # handed_off is our boundary, delivered is the network's answer later
        led = Ledger(self.p, boot="ffff")
        seq = led.open("zork1", "aabbccdd")
        led.close(seq, "handed_off", bytes_out=10)
        led.close(seq, "delivered")
        self.assertEqual([r["state"] for r in self.lines()],
                         [STATE_OPEN, "handed_off", "delivered"])

    def test_a_restart_does_not_close_a_previous_run_turn(self):
        first = Ledger(self.p, boot="aaaa")
        first.open("zork1", "aabbccdd")           # never closed: it died here
        second = Ledger(self.p, boot="bbbb")
        second.close(1, "delivered")              # same seq, different boot
        self.assertEqual(len(second.unresolved()), 1)

    def test_rotation_keeps_the_record_bounded(self):
        led = Ledger(self.p, boot="ffff", max_bytes=200)
        for i in range(60):
            seq = led.open("zork1", "aabbccdd")
            led.close(seq, "delivered", bytes_out=100)
        self.assertTrue((Path(self.d.name) / "ledger.jsonl.1").exists())
        self.assertLess(self.p.stat().st_size, 2000)

    def test_an_unwritable_ledger_never_loses_a_turn(self):
        blocked = Path(self.d.name) / "not_a_file"
        blocked.mkdir()                           # a directory where we write
        led = Ledger(blocked)
        seq = led.open("zork1", "aabbccdd")       # must not raise
        led.close(seq, "delivered")
        self.assertEqual(led.unresolved(), [])


if __name__ == "__main__":
    unittest.main()
