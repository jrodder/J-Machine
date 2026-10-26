"""The stats-page ledger window (spec §6): a turn's timestamp exists nowhere
else, and the ledger rotates, so the rollup has to say how far back it actually
reaches instead of implying it covers everything."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from jhost.ledger import DEFAULT_MAX_BYTES as LEDGER_MAX_BYTES
from jhost.window import DEFAULT_MAX_BYTES, activity


def stamp(day, hour=12):
    """Local-time timestamp for a calendar day, so the tests do not depend on
    the host's zone (day buckets are host-local on purpose)."""
    return time.mktime(time.strptime(f"{day} {hour:02d}:00:00",
                                     "%Y-%m-%d %H:%M:%S"))


def line(seq, boot, state, t=None, game=None, player=None, **fields):
    rec = {"s": seq, "b": boot, "t": t if t is not None else stamp("2026-10-26"),
           "state": state}
    if game is not None:
        rec["game"] = game
    if player is not None:
        rec["player"] = player
    rec.update(fields)
    return json.dumps(rec)


class LedgerWindow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = Path(self.d.name) / "ledger.jsonl"

    def write(self, lines, path=None):
        Path(path or self.path).write_text("\n".join(lines) + "\n")

    def read(self, paths=None, **kw):
        return activity(paths if paths is not None else [self.path], **kw)

    def tearDown(self):
        self.d.cleanup()

    def test_one_open_line_is_one_turn(self):
        self.write([line(1, "b1", "open", game="h2g2", player="aaaa11"),
                    line(2, "b1", "open", game="h2g2", player="aaaa11")])
        self.assertEqual(self.read().per_day,
                         {"2026-10-26": {"h2g2": 2}})

    def test_handed_off_then_delivered_reads_as_delivered(self):
        self.write([line(1, "b1", "open", game="zork1", player="aaaa11"),
                    line(1, "b1", "handed_off", t=stamp("2026-10-26", 13)),
                    line(1, "b1", "delivered", t=stamp("2026-10-26", 14))])
        a = self.read()
        self.assertEqual(a.outcomes["delivered"], 1)
        self.assertEqual(a.in_flight, 0, "handed_off is intermediate, not an "
                                        "answer")

    def test_a_turn_that_never_finished_is_in_flight_not_delivered(self):
        self.write([line(1, "b1", "open", game="zork1", player="aaaa11"),
                    line(1, "b1", "handed_off",
                         t=stamp("2026-10-26", 13))])
        a = self.read()
        self.assertEqual(a.in_flight, 1)
        self.assertEqual(a.outcomes.get("delivered", 0), 0)

    def test_sequence_numbers_reused_across_boots_do_not_merge(self):
        self.write([line(1, "b1", "open", game="zork1", player="aaaa11"),
                    line(1, "b1", "delivered", t=stamp("2026-10-26", 13)),
                    line(1, "b2", "open", game="zork1", player="bbbb22")])
        a = self.read()
        self.assertEqual(a.outcomes["delivered"], 1)
        self.assertEqual(a.in_flight, 1, "boot b2 seq 1 has no terminal record")

    def test_days_and_games_are_bucketed_apart(self):
        self.write([line(1, "b1", "open", t=stamp("2026-10-24"), game="h2g2"),
                    line(2, "b1", "open", t=stamp("2026-10-25"), game="h2g2"),
                    line(3, "b1", "open", t=stamp("2026-10-25"), game="sherlock")])
        self.assertEqual(self.read().per_day,
                         {"2026-10-24": {"h2g2": 1},
                          "2026-10-25": {"h2g2": 1, "sherlock": 1}})

    def test_recent_counts_calendar_days_not_24_hour_spans(self):
        today = time.strftime("%Y-%m-%d")
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        self.write([line(1, "b1", "open", t=stamp(today), game="h2g2"),
                    line(2, "b1", "open", t=stamp(yesterday), game="h2g2")])
        a = self.read()
        self.assertEqual(a.recent(1), 1)
        self.assertEqual(a.recent(2), 2)

    def test_the_byte_cap_reads_the_tail_and_says_so(self):
        old = [line(n, "b1", "open", t=stamp("2026-10-01"), game="h2g2",
                    player="x" * 6) for n in range(1, 200)]
        newest = line(999, "b1", "open", t=stamp("2026-10-26"), game="zork1")
        self.write(old + [newest])
        a = self.read(max_bytes=400)
        self.assertTrue(a.truncated)
        # What the cap promises is not "only today" but: the newest record is
        # there, the older ones are not all there, and the partial line the
        # seek landed inside became no record at all.
        self.assertEqual(a.per_day["2026-10-26"], {"zork1": 1})
        counted = sum(sum(g.values()) for g in a.per_day.values())
        self.assertLess(counted, 200)
        self.assertEqual(a.until, stamp("2026-10-26"),
                       "the newest record must be inside the window")

    def test_an_older_generation_is_read_when_it_fits(self):
        self.write([line(1, "b0", "open", t=stamp("2026-10-25"), game="h2g2")],
                   path=str(self.path) + ".1")
        self.write([line(1, "b1", "open", t=stamp("2026-10-26"), game="h2g2")])
        a = self.read(paths=[str(self.path) + ".1", str(self.path)])
        self.assertEqual(a.per_day["2026-10-25"]["h2g2"], 1)
        self.assertFalse(a.truncated)
        self.assertEqual(a.since, stamp("2026-10-25"))
        self.assertEqual(a.until, stamp("2026-10-26"))

    def test_missing_files_and_garbage_lines_survive(self):
        self.path.write_text("not json at all\n"
                             + line(1, "b1", "open", game="h2g2") + "\n"
                             + "{broken\n")
        a = self.read(paths=[Path(self.d.name) / "absent.jsonl", self.path])
        self.assertEqual(a.per_day, {"2026-10-26": {"h2g2": 1}})

    def test_a_fresh_host_reads_as_nothing_rather_than_failing(self):
        a = self.read(paths=[])
        self.assertIsNone(a.since)
        self.assertFalse(a)
        self.assertEqual(a.recent(7), 0)

    def test_an_outcome_without_its_open_line_is_not_counted_as_a_turn(self):
        # the open line rotated away; the record still counts as network history
        self.write([line(7, "b1", "delivered", t=stamp("2026-10-26"))])
        a = self.read()
        self.assertEqual(a.outcomes["delivered"], 1)
        self.assertEqual(a.per_day, {})
        self.assertEqual(a.in_flight, 0)


    def test_the_default_window_can_see_the_whole_rotated_ledger(self):
        # The ledger bounds itself at LEDGER_MAX_BYTES live plus one rolled
        # generation of the same size. A smaller read cap would leave "Recent
        # days" showing a few hours on a busy host while still reading like a
        # week of history — the label would be honest and the section not.
        self.assertGreaterEqual(DEFAULT_MAX_BYTES, 2 * LEDGER_MAX_BYTES)

if __name__ == "__main__":
    unittest.main()
