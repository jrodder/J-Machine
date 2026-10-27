"""The external probe (service status): a check that runs against a live host
from another process, and the record that lets a failed check be seen. RNS and
subprocess are faked here; the live path is jclient ping plus the canary."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import jhost.host as host_mod
import jhost.probe as probe_mod
from jhost.host import Host
from jhost.probe import PROBE_JSON, pick_game, read_probe, run, write_probe


class FakeRNS:
    class Identity:
        @staticmethod
        def recall(h):
            return None

    class Transport:
        interfaces = []

        @staticmethod
        def await_path(h, timeout=None):
            return False


def dests(d):
    (d / "host-destinations.json").write_text(json.dumps(
        {"page": "<aa>", "games": {"h2g2": "<bb16>", "zork1": "<cc16>"}}))


class FakeSubprocess:
    """Stands in for the subprocess module so probe.run can be exercised
    without a host. TimeoutExpired is carried over: probe catches it."""
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls, self.kwargs = [], []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        self.kwargs.append(kw)
        return SimpleNamespace(returncode=self.rc, stdout=self.out,
                               stderr=self.err)


class Picking(unittest.TestCase):
    def test_the_default_pick_is_stable(self):
        d = {"games": {"h2g2": "<b>", "zork1": "<c>", "adventure": "<a>"}}
        self.assertEqual(pick_game(d), ("adventure", "<a>"))
        self.assertEqual(pick_game(d), ("adventure", "<a>"))

    def test_an_unknown_game_is_named(self):
        with self.assertRaises(KeyError) as c:
            pick_game({"games": {"h2g2": "<b>"}}, "nope")
        self.assertIn("h2g2", str(c.exception))


class Record(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def test_no_record_is_reported_as_none_not_an_error(self):
        # readiness reads this while rendering a page for a visitor
        self.assertIsNone(read_probe(self.dir))

    def test_a_half_written_record_is_none_not_an_exception(self):
        (self.dir / PROBE_JSON).write_text("{trunca")
        self.assertIsNone(read_probe(self.dir))

    def test_the_record_survives_a_round_trip(self):
        write_probe(self.dir, {"ok": True, "t": 5.0, "detail": "x"})
        self.assertEqual(read_probe(self.dir)["t"], 5.0)


class Running(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)
        dests(self.dir)
        self._real = probe_mod.subprocess

    def tearDown(self):
        probe_mod.subprocess = self._real
        self.d.cleanup()

    def test_a_good_round_trip_is_recorded_as_ok(self):
        sub = FakeSubprocess(0, "ping OK rtt=41s boot=dc3c3a60 games=31\n")
        probe_mod.subprocess = sub
        r = run(self.dir)
        self.assertTrue(r["ok"])
        self.assertEqual(r["game"], "h2g2")
        self.assertIn("boot=dc3c3a60", read_probe(self.dir)["detail"])
        self.assertEqual(sub.calls[0][1:5],
                         ["-m", "jclient", "ping", "<bb16>"])
        # persistent identity dir: the probe must not mint a new destination,
        # whose announce would be throttled by the hubs' ~1/hour limit
        self.assertIn(str(self.dir / "probe" / "identity"), sub.calls[0])
        self.assertIn("<bb16>", sub.calls[0])

    def test_the_kill_ceiling_is_the_budget_plus_margin(self):
        # Killing the client before its own window closes would record "hadn't
        # happened yet" as "did not happen" — a false failure on the page. The
        # client owns the verdict, so this ceiling has to sit above its budget.
        sub = FakeSubprocess(0, "ping OK ack=delivered\n")
        probe_mod.subprocess = sub
        run(self.dir, budget=600)
        self.assertEqual(sub.kwargs[0]["timeout"], 600 + probe_mod.ACK_MARGIN)
        argv = sub.calls[0]
        self.assertEqual(argv[argv.index("--budget") + 1], "600")
        self.assertEqual(probe_mod.DEFAULT_BUDGET, 600)

    def test_a_timeout_is_recorded_as_a_failure_not_a_crash(self):
        class Boom(FakeSubprocess):
            def run(self, cmd, **kw):
                raise self.TimeoutExpired(cmd, 1)
        probe_mod.subprocess = Boom()
        r = run(self.dir, budget=1)
        self.assertFalse(r["ok"])
        self.assertIn("killed", r["detail"])
        self.assertFalse(read_probe(self.dir)["ok"])

    def test_a_failing_client_keeps_its_own_reason(self):
        probe_mod.subprocess = FakeSubprocess(1, "", "ping FAIL stage=reply\n")
        r = run(self.dir)
        self.assertFalse(r["ok"])
        self.assertIn("stage=reply", r["detail"])


class ServiceFault(unittest.TestCase):
    """The part that matters to the page: a failed check has to be visible, and
    has to stop being visible once a later round trip succeeds."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        games = Path(self.d.name) / "games"
        games.mkdir()
        self._rns = host_mod.RNS
        host_mod.RNS = FakeRNS
        self.data = Path(self.d.name) / "data"
        self.data.mkdir()          # a live host's data dir already exists
        self.host = Host(self.data, games)

    def tearDown(self):
        host_mod.RNS = self._rns
        self.d.cleanup()

    def faults(self):
        return self.host._service()["faults"]

    def test_no_check_yet_is_not_a_fault(self):
        self.assertNotIn("external check failed", " ".join(self.faults()))

    def test_a_failed_check_is_a_fault_with_its_reason(self):
        write_probe(self.data, {"ok": False, "t": 2e9,
                                "detail": "ping FAIL stage=reply"})
        f = " ".join(self.faults())
        self.assertIn("external check failed", f)
        self.assertIn("stage=reply", f)

    def test_a_successful_check_is_not_a_fault(self):
        write_probe(self.data, {"ok": True, "t": 2e9, "detail": "ping OK"})
        self.assertNotIn("external check failed", " ".join(self.faults()))

    def test_a_round_trip_after_a_failed_check_retires_the_fault(self):
        write_probe(self.data, {"ok": False, "t": 2e9, "detail": "no reply"})
        self.assertIn("external check failed", " ".join(self.faults()))
        self.host.stats["last_roundtrip_ok"] = 2e9 + 60
        self.assertNotIn("external check failed", " ".join(self.faults()))

    def test_a_successful_check_is_enough_even_with_no_ack_this_boot(self):
        # The check proves the query arrived and the answer came back. That is
        # not undone by the host having restarted since, so the record on disk
        # stands on its own.
        write_probe(self.data, {"ok": True, "t": 2e9, "detail": "ping OK"})
        self.assertEqual(self.host._service()["verified_at"], 2e9)

    def test_the_newest_evidence_wins(self):
        write_probe(self.data, {"ok": True, "t": 2e9, "detail": "ping OK"})
        self.host.stats["last_roundtrip_ok"] = 2e9 + 120
        self.assertEqual(self.host._service()["verified_at"], 2e9 + 120)
        self.host.stats["last_roundtrip_ok"] = 2e9 - 120
        self.assertEqual(self.host._service()["verified_at"], 2e9)

    def test_a_failed_check_is_no_evidence_even_if_it_ran(self):
        write_probe(self.data, {"ok": False, "t": 2e9, "detail": "no reply"})
        self.assertIsNone(self.host._service()["verified_at"])

    def test_health_carries_the_check(self):
        write_probe(self.data, {"ok": False, "t": 2e9, "detail": "no reply"})
        h = self.host.readiness()
        self.assertIs(h["probe_ok"], False)
        self.assertEqual(h["probe_t"], 2e9)
        self.assertEqual(h["probe_detail"], "no reply")


if __name__ == "__main__":
    unittest.main()
