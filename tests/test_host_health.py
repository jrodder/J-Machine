"""Readiness snapshot (spec §3): the checks that need no player, because a
quiet node and a broken node look identical from message counters alone."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from jhost.host import Host

LOOPBACK = """
[reticulum]
enable_transport = yes

[interfaces]

    [[Loopback]]
    type = TCPServerInterface
    enabled = yes
    listen_ip = 127.0.0.1
    listen_port = 4242
"""

PUBLIC = """
[reticulum]
enable_transport = yes

[interfaces]

    [[LAN TCP Server]]
    type = TCPServerInterface
    enabled = yes
    listen_ip = 0.0.0.0
    listen_port = 4242

    [[Testnet Hub: Sydney]]
    type = TCPClientInterface
    enabled = no
    target_host = sydney.reticulum.au
"""

DISABLED = PUBLIC.replace("[[LAN TCP Server]]", "[[LAN TCP Server]]").replace(
    "listen_ip = 0.0.0.0", "listen_ip = 0.0.0.0\n    enabled = no")


class Health(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        self.games = self.root / "games"
        self.games.mkdir()
        self.story = self.games / "zork1.z3"
        self.story.write_bytes(b"not-a-story")

    def tearDown(self):
        self.d.cleanup()

    def host(self, config):
        data = self.root / "data"
        if config is not None:
            (data / "rns").mkdir(parents=True, exist_ok=True)
            (data / "rns" / "config").write_text(config)
        h = Host(data, self.games)
        h.stories = {"zork1": self.story}
        return h

    def test_loopback_only_config_is_reported_as_unreachable(self):
        self.assertFalse(self.host(LOOPBACK)._transport_configured())

    def test_a_public_bind_counts_as_transport(self):
        self.assertTrue(self.host(PUBLIC)._transport_configured())

    def test_disabled_interfaces_are_no_transport(self):
        self.assertFalse(self.host(DISABLED)._transport_configured())

    def test_missing_config_is_not_configured_transport(self):
        h = self.host(None)
        self.assertFalse(h._transport_configured())
        self.assertIsNone(h._config_flag("enable_transport"))

    def test_enable_transport_is_read(self):
        self.assertTrue(self.host(PUBLIC)._config_flag("enable_transport"))
        self.assertFalse(self.host(
            PUBLIC.replace("enable_transport = yes",
                           "enable_transport = no"))._config_flag(
                               "enable_transport"))

    def test_a_story_file_that_vanished_after_startup_is_named(self):
        h = self.host(PUBLIC)
        h.stories = {"zork1": self.root / "gone.z5"}
        health = h.readiness()
        self.assertEqual(health["games"], 1)
        self.assertEqual(health["games_missing_story"], ["zork1"])

    def test_open_ledger_turns_are_counted(self):
        h = self.host(PUBLIC)
        h.ledger.open("zork1", "aabbccdd")
        self.assertEqual(h.readiness()["ledger_unresolved"], 1)

    def test_health_is_written_whole_and_readable(self):
        h = self.host(PUBLIC)
        h.started = 1000.0
        h.write_health()
        health = json.loads((self.root / "data" / "health.json").read_text())
        self.assertTrue(health["transport_configured"])
        self.assertTrue(health["saves_writable"])
        self.assertEqual(health["inbound"], 0)
        self.assertEqual(health["boot"], h.ledger.boot)
        self.assertFalse((self.root / "data" / "health.json.tmp").exists())

    def test_an_unwritable_save_store_is_reported(self):
        h = self.host(PUBLIC)
        h.store.root.mkdir(parents=True, exist_ok=True)
        (h.store.root / ".write-probe").mkdir()   # blocks the probe write
        self.assertFalse(h._saves_writable())

    def test_interface_counts_are_reported_without_an_instance(self):
        # whether any interface object exists depends on whether a Reticulum
        # instance was started in this process, so the health file reports the
        # counts rather than an opinion: this is the field a check reads, and a
        # node whose interfaces were all torn down shows 0 of N
        health = self.host(PUBLIC).readiness()
        self.assertGreaterEqual(health["interfaces_online"], 0)
        self.assertLessEqual(health["interfaces_online"],
                            health["interfaces_total"])
        self.assertIs(health["transport_configured"], True)


    def test_the_activity_page_counters_reach_health(self):
        # Both halves, because the failure half is the point: RNS swallows a
        # handler that raises, so "nobody clicked it" and "it is broken" look
        # identical unless the page counts its own requests and its own errors.
        health = self.host(PUBLIC).readiness()
        for key in ("stats_requests", "stats_errors", "last_stats"):
            self.assertIn(key, health)

class WhatReachedAPlayer(unittest.TestCase):
    """The blind spot behind a four-day silence (2026-11-23).

    The pill stayed green for six days while every player reply died: nothing
    in readiness() asked whether a reply reached anyone, so the outage's own
    evidence -- `lost over link` in the journal, delivered == 0 in the day log
    -- never reached the face of the page. These faults look only at what this
    process recorded itself, so they need no player and no outside observer.
    """

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        root = Path(self.d.name)
        (root / "games").mkdir()
        (root / "data").mkdir()
        self.host = Host(root / "data", root / "games")
        self.addCleanup(self.d.cleanup)

    def faults(self):
        return " ".join(self.host._service()["faults"])

    def test_turns_in_and_nobody_reached_is_named(self):
        s = self.host.stats
        s["inbound"] = s["handed_off"] = s["failed"] = s["dropped"] = 4
        self.assertIn("replies lost", self.faults())

    def test_the_names_that_are_not_a_loss_do_not_alone_convict(self):
        # handed_off means handed to the mesh, and propagated means a node is
        # holding it for the player: neither is a loss
        s = self.host.stats
        s["inbound"] = s["handed_off"] = 4
        s["propagated"] = 3
        s["failed"] = 1
        self.assertNotIn("replies lost", self.faults())

    def test_mostly_getting_through_is_not_a_fault(self):
        s = self.host.stats
        s["inbound"] = 21
        s["delivered"] = 20
        s["failed"] = s["dropped"] = s["handed_off"] = 1
        self.assertNotIn("replies lost", self.faults())

    def test_a_node_with_no_players_is_still_green(self):
        # the rule that keeps every other readiness check honest: no traffic
        # is not a fault, or a quiet node would be red forever
        self.assertNotIn("replies lost", self.faults())

    def test_a_saturated_cpu_is_named(self):
        # the fault that broke everything was intra-process: LXMF routers
        # out-competing Reticulum for the box's single core. 10/10 interfaces
        # online and probe_ok true while every reply failed
        self.host._cpu_pct = lambda: 96.0
        self.assertIn("CPU", self.faults())

    def test_cpu_headroom_is_not_a_fault(self):
        self.host._cpu_pct = lambda: 12.0
        self.assertNotIn("CPU", self.faults())

    def test_a_host_that_cannot_measure_its_cpu_says_nothing(self):
        self.host._cpu_pct = lambda: None
        self.assertNotIn("CPU", self.faults())

    def test_cpu_reaches_the_health_file(self):
        self.host._cpu_pct = lambda: 33.3
        self.assertEqual(self.host.readiness()["cpu_pct"], 33.3)

    def test_a_uplink_lost_is_named_not_tolerated(self):
        # the old rule asked whether *no* uplink was up, so a host running on 8
        # of 10 -- two torn down after "unrecoverable error", which RNS cannot
        # reopen without a new process -- stayed green while flapping
        self.host.started = time.time() - 3600
        h = self.host.readiness()
        h["interfaces_total"] = 10
        h["interfaces_online"] = 8
        self.assertIn("2 of 10", " ".join(self.host._faults(h)))

    def test_all_uplinks_down_is_still_named(self):
        self.host.started = time.time() - 3600
        h = self.host.readiness()
        h["interfaces_total"] = 10
        h["interfaces_online"] = 0
        self.assertIn("no interface online", " ".join(self.host._faults(h)))

    def test_a_host_still_bringing_uplinks_up_is_not_faulted(self):
        # interfaces connect at their own pace after a start; red for the first
        # few minutes of every boot would train everyone to ignore the pill
        self.host.started = time.time() - 60
        h = self.host.readiness()
        h["interfaces_total"] = 10
        h["interfaces_online"] = 8
        self.assertNotIn("uplinks down", " ".join(self.host._faults(h)))

    def test_repeated_uplink_churn_is_named(self):
        # 1,899 reconnects in 83 minutes on the live host, against a historical
        # 17 a day: the journal line that would have shown it was suppressed by
        # design, so the count has to reach the verdict or it is unseen
        self.host.started = time.time() - 3600
        h = self.host.readiness()
        h["log_suppressed"] = {"socket_closed": 900, "reconnected": 900}
        self.assertIn("uplink churn", " ".join(self.host._faults(h)))

    def test_normal_hub_blips_are_not_churn(self):
        self.host.started = time.time() - 3600
        h = self.host.readiness()
        h["log_suppressed"] = {"socket_closed": 3, "reconnected": 3}
        self.assertNotIn("uplink churn", " ".join(self.host._faults(h)))

    def test_the_cpu_number_is_this_process_and_this_window(self):
        self.host.CPU_MIN_SPAN = 0
        self.assertIsNone(self.host._cpu_pct())   # no window has closed yet
        t0 = time.time()
        while time.time() - t0 < 0.3:             # spend a little of the core
            pass
        pct = self.host._cpu_pct()
        self.assertIsNotNone(pct)
        self.assertGreater(pct, 0)
        self.assertLessEqual(pct, 100.0 * (os.cpu_count() or 1) + 5)


if __name__ == "__main__":
    unittest.main()
