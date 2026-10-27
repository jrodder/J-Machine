"""The permanent day log (jhost/rollup.py). The rules it exists to enforce: never
shrink, never lose a day, never raise while a page is rendering."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from jhost.rollup import (DAYS_FILE, merge, newest, read_days, recent,
                          totals, write_days)


def bucket(turns=0, players=0, delivered=0, failed=0):
    return {"turns": turns, "players": players,
            "delivered": delivered, "failed": failed}


class FakeActivity:
    def __init__(self, per_day):
        self.per_day = per_day


class Merge(unittest.TestCase):
    def test_a_smaller_later_read_cannot_lower_a_count(self):
        # A page read sees a window, not the whole ledger. If the merge could
        # lower a number, one quiet afternoon would rewrite history.
        days = merge({}, FakeActivity(
            {"2026-10-26": {"h2g2": bucket(turns=9, players=3, delivered=9)}}),
            today="2026-10-27")
        merge(days, FakeActivity(
            {"2026-10-26": {"h2g2": bucket(turns=2, players=1, delivered=1)}}),
            today="2026-10-27")
        self.assertEqual(days["2026-10-26"]["h2g2"],
                         bucket(turns=9, players=3, delivered=9))

    def test_a_new_failure_is_not_rejected_by_the_maximum(self):
        # "only ever goes up" is about the good counts; a turn that failed today
        # is new information, not a revision of yesterday.
        days = merge({}, FakeActivity(
            {"2026-10-26": {"h2g2": bucket(turns=1, delivered=1)}}),
            today="2026-10-26")
        merge(days, FakeActivity(
            {"2026-10-26": {"h2g2": bucket(turns=3, delivered=2, failed=1)}}),
            today="2026-10-26")
        self.assertEqual(days["2026-10-26"]["h2g2"],
                         bucket(turns=3, delivered=2, failed=1))

    def test_a_bare_number_still_counts_as_turns(self):
        days = merge({}, FakeActivity({"2026-10-26": {"h2g2": 4}}),
                     today="2026-10-26")
        self.assertEqual(days["2026-10-26"]["h2g2"]["turns"], 4)

    def test_today_gets_a_row_even_when_nothing_happened(self):
        # Without this, "nobody played" and "we stopped recording" look the same.
        days = merge({}, FakeActivity({}), today="2026-10-27")
        self.assertIn("2026-10-27", days)
        self.assertEqual(totals(days, "2026-10-27")["turns"], 0)

    def test_days_accumulate_instead_of_replacing(self):
        days = merge({}, FakeActivity({"2026-10-26": {"h2g2": bucket(1)}}),
                     today="2026-10-26")
        merge(days, FakeActivity({"2026-10-27": {"h2g2": bucket(2)}}),
              today="2026-10-27")
        self.assertEqual(sorted(days), ["2026-10-26", "2026-10-27"])


class File(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = str(Path(self.d.name) / DAYS_FILE)

    def tearDown(self):
        self.d.cleanup()

    def test_a_round_trip_keeps_every_day(self):
        days = {"2026-10-26": {"h2g2": bucket(turns=3, players=2)},
                "2026-10-27": {}}
        write_days(self.path, days)
        self.assertEqual(read_days(self.path), days)

    def test_no_file_is_the_fresh_host_not_a_failure(self):
        self.assertEqual(read_days(str(Path(self.d.name) / "absent.jsonl")), {})

    def test_a_torn_line_is_skipped_rather_than_raising(self):
        # The page renders from this file. A half-written last line is not a
        # reason to hand a player an error page.
        Path(self.path).write_text(
            json.dumps({"day": "2026-10-26", "games": {}}) + "\n"
            + '{"day": "2026-10-27", "gam')
        self.assertEqual(read_days(self.path), {"2026-10-26": {}})

    def test_the_previous_version_survives_a_write(self):
        write_days(self.path, {"2026-10-26": {}})
        write_days(self.path, {"2026-10-27": {}})
        self.assertTrue(Path(self.path + ".prev").exists())

    def test_the_write_is_atomic_so_a_crash_leaves_a_whole_file(self):
        write_days(self.path, {"2026-10-26": {}})
        self.assertFalse(Path(self.path + ".tmp").exists())


class Window(unittest.TestCase):
    def test_quiet_days_come_back_as_zeros_not_as_gaps(self):
        rows = recent({"2026-10-26": {"h2g2": bucket(turns=5, players=1)}},
                      count=3, now=time.mktime(
                          time.strptime("2026-10-27 12:00", "%Y-%m-%d %H:%M")))
        self.assertEqual([r["day"] for r in rows],
                         ["2026-10-25", "2026-10-26", "2026-10-27"])
        self.assertEqual([r["turns"] for r in rows], [0, 5, 0])

    def test_newest_names_the_last_recorded_day(self):
        self.assertIsNone(newest({}))
        self.assertEqual(newest({"2026-10-26": {}, "2026-10-27": {}}),
                         "2026-10-27")




class FakeRNS:
    class Transport:
        interfaces = []


class HostWiring(unittest.TestCase):
    """The part the page and health.json read: a recorder that stops is visible."""

    def setUp(self):
        import jhost.host as host_mod
        from jhost.host import Host
        self.mod = host_mod
        self._rns = host_mod.RNS
        host_mod.RNS = FakeRNS
        self.d = tempfile.TemporaryDirectory()
        root = Path(self.d.name)
        (root / "games").mkdir()
        self.data = root / "data"
        self.data.mkdir()          # a live host's data dir already exists
        self.host = Host(self.data, root / "games")

    def tearDown(self):
        self.mod.RNS = self._rns
        self.d.cleanup()

    def faults(self):
        return " ".join(self.host._service()["faults"])

    def test_a_day_log_that_errors_is_a_fault(self):
        self.host.days = {"2026-10-27": {}}
        self.host.rollup_error = "OSError: disk full"
        self.assertIn("day log not updating", self.faults())
        self.assertIn("disk full", self.faults())

    def test_a_day_log_stuck_in_the_past_is_a_fault(self):
        # every player-facing number still looks healthy, which is the point
        self.host.days = {"2020-01-01": {}}
        self.assertIn("day log has not recorded today", self.faults())

    def test_a_fresh_host_with_no_day_log_yet_is_not_a_fault(self):
        self.host.days = {}
        self.assertNotIn("day log", self.faults())

    def test_health_reports_the_day_log(self):
        self.host.days = {"2026-10-26": {}, "2026-10-27": {}}
        h = self.host.readiness()
        self.assertEqual(h["rollup_days"], 2)
        self.assertEqual(h["rollup_newest"], "2026-10-27")
        self.assertIsNone(h["rollup_error"])

    def test_advancing_folds_the_ledger_into_a_day(self):
        today = time.strftime("%Y-%m-%d", time.localtime())
        (self.data / "ledger.jsonl").write_text(json.dumps(
            {"s": 1, "b": "b1", "t": time.time(), "game": "zork1",
             "player": "aaaa11", "state": "open"}) + "\n")
        self.host._advance_rollup()
        self.assertIsNone(self.host.rollup_error)
        days = read_days(str(self.data / DAYS_FILE))
        self.assertEqual(days[today]["zork1"]["turns"], 1)
        self.assertEqual(days[today]["zork1"]["players"], 1)

    def test_advancing_twice_changes_nothing(self):
        (self.data / "ledger.jsonl").write_text(json.dumps(
            {"s": 1, "b": "b1", "t": time.time(), "game": "zork1",
             "player": "aaaa11", "state": "open"}) + "\n")
        self.host._advance_rollup()
        first = json.dumps(self.host.days, sort_keys=True)
        self.host._advance_rollup()
        self.assertEqual(json.dumps(self.host.days, sort_keys=True), first)


if __name__ == "__main__":
    unittest.main()
