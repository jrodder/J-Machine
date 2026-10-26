"""Readiness snapshot (spec §3): the checks that need no player, because a
quiet node and a broken node look identical from message counters alone."""
import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()
