"""The outbound stamp table, which is what stopped the host (2026-11-23).

lxmf 1.1.1 `LXMRouter.update_stamp_cost` (LXMRouter.py:1027-1032) starts a
brand-new thread for every stamp cost it learns, and each thread copies and
msgpack-packs the router's ENTIRE table to disk. `clean_outbound_stamp_costs`
is called only from `__init__` (LXMRouter.py:276) — never while running — so
the table only grows, and every later write is bigger than the last.

Measured on the deployed host (one CPU core, one router per game, 31 games):
1.75 thread spawns per second, 99.4% of the core with no players online,
812 KB per game's table, `delivered == 0` for six straight days because
Reticulum's link-establishment deadlines were never met. A restart took it to
14.9%, and the tables grew back at ~78 KB/hour each.

This host replies to a handful of players, so it needs a handful of stamp
costs — not one for every destination on the testnet. Writes are batched to
the run() tick and the table is bounded by age while running.

No real router is built here: instantiating LXMRouter needs a live Reticulum
instance, and what is under test is who writes, how often, and how big the
table gets — not LXMF's crypto.
"""
import time
import unittest

from jhost.host import (Host, HostRouter, STAMP_MAX_AGE, flush_stamp_table)


class FakeRouter:
    """The two things a router contributes to the flush: the table, and the
    save the parent class would have done on its own thread."""

    def __init__(self, table=None, dirty=False):
        self.outbound_stamp_costs = table if table is not None else {}
        self.stamps_dirty = dirty
        self.writes = 0

    def save_outbound_stamp_costs(self):
        self.writes += 1


def update(router, dest, cost, now=None):
    """HostRouter.update_stamp_cost, called with a stand-in self: the point is
    that it neither writes nor spawns a thread."""
    HostRouter.update_stamp_cost(router, dest, cost)


class NoWriteOnEveryStamp(unittest.TestCase):
    def test_recording_a_cost_does_not_write_the_table(self):
        r = FakeRouter()
        update(r, b"\x01" * 16, 10)
        self.assertEqual(r.writes, 0)

    def test_recording_a_cost_defers_the_write(self):
        r = FakeRouter()
        update(r, b"\x01" * 16, 10)
        self.assertTrue(r.stamps_dirty)
        self.assertEqual(r.outbound_stamp_costs[b"\x01" * 16][1], 10)

    def test_a_hundred_costs_cost_one_write(self):
        r = FakeRouter()
        for i in range(100):
            update(r, bytes([i]) * 16, i)
        self.assertEqual(len(r.outbound_stamp_costs), 100)
        self.assertTrue(flush_stamp_table(r))
        self.assertEqual(r.writes, 1)


class Flush(unittest.TestCase):
    def test_a_dirty_table_is_written_once_then_clean(self):
        r = FakeRouter(dirty=True)
        self.assertTrue(flush_stamp_table(r))
        self.assertEqual(r.writes, 1)
        self.assertFalse(r.stamps_dirty)
        self.assertFalse(flush_stamp_table(r))
        self.assertEqual(r.writes, 1)

    def test_an_untouched_table_is_not_rewritten(self):
        r = FakeRouter()
        self.assertFalse(flush_stamp_table(r))
        self.assertEqual(r.writes, 0)

    def test_a_cost_older_than_the_window_is_dropped_while_running(self):
        # the library prunes only at startup; a run that lasts days is the
        # failure, so the bound has to hold during the run
        old = time.time() - STAMP_MAX_AGE - 1
        r = FakeRouter(table={b"\x01" * 16: [old, 3],
                              b"\x02" * 16: [time.time(), 5]})
        self.assertTrue(flush_stamp_table(r))
        self.assertEqual(list(r.outbound_stamp_costs), [b"\x02" * 16])
        self.assertEqual(r.writes, 1)

    def test_pruning_alone_makes_a_write_necessary(self):
        # nothing new arrived, so nothing is dirty, yet the table changed
        r = FakeRouter(table={b"\x01" * 16: [time.time() - STAMP_MAX_AGE - 1, 3]})
        self.assertTrue(flush_stamp_table(r))
        self.assertEqual(r.outbound_stamp_costs, {})


class HostWiring(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.d = tempfile.TemporaryDirectory()
        root = Path(self.d.name)
        (root / "games").mkdir()
        (root / "data").mkdir()
        self.host = Host(root / "data", root / "games")
        self.host.routers = {"b": FakeRouter(dirty=True), "a": FakeRouter()}
        self.addCleanup(self.d.cleanup)

    def test_every_games_table_is_flushed(self):
        self.host._flush_stamp_tables()
        self.assertEqual(self.host.routers["b"].writes, 1)

    def test_a_router_that_cannot_write_does_not_stop_the_others(self):
        class Boom(FakeRouter):
            def save_outbound_stamp_costs(self):
                super().save_outbound_stamp_costs()
                raise OSError("disk full")

        self.host.routers = {"a": Boom(dirty=True), "b": FakeRouter(dirty=True)}
        self.host._flush_stamp_tables()
        self.assertEqual(self.host.routers["b"].writes, 1)


if __name__ == "__main__":
    unittest.main()
