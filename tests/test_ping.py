"""The ping verb (service status): an external check that must leave no trace in
player data. The rule it exists to enforce is that a health probe cannot inflate
the numbers it is checking, so every test here asserts on the ledger file and the
save tree, not only on the reply text. RNS is faked; the live path is the
canary's job."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import jclient.client as client_mod
import jhost.host as host_mod
from jclient.client import Client
from jhost.host import Host
from jhost.protocol import PING, PING_ACK, ping_reply


class Msg:
    def __init__(self, text):
        self.source_hash = b"\xab" * 16
        self.signature_validated = True
        self.unverified_reason = None
        self.packed = b""
        self._text = text

    def content_as_string(self):
        return self._text


class Ident:
    hash = b"\xcd" * 16


class FakeRNS:
    class Identity:
        @staticmethod
        def recall(h):
            return Ident()

    class Transport:
        @staticmethod
        def await_path(h, timeout=None):
            return True

    class Destination:
        OUT = 0
        SINGLE = 0

        def __init__(self, *a, **k):
            pass


class FakeLXMessage:
    sent = []

    def __init__(self, dest, source, content=None, title=None):
        FakeLXMessage.sent.append(content)

    def register_delivery_callback(self, cb):
        pass


class Router:
    def __init__(self):
        self.out = []

    def handle_outbound(self, m):
        self.out.append(m)


class FakeTime:
    """Advances on every reading so a wait loop can be exercised at all: the
    deadline is computed once, each poll moves the clock."""

    def __init__(self, start=0.0, step=5.0):
        self.t, self.step = start, step

    def time(self):
        v = self.t
        self.t += self.step
        return v

    def sleep(self, _s):
        pass


class WatchedMsg:
    def __init__(self, script):
        self.script = script
        self.i = 0

    @property
    def state(self):
        s = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return s


class AckDelivery(unittest.TestCase):
    """The bug this class exists for: the ack is the last thing a probe sends,
    and handle_outbound only queues. Exiting without watching the transfer loses
    it silently -- measured on the live host as two pings answered, zero acks.
    Loopback can finish a transfer inside the send call, so a fast rig passed
    without ever proving the point."""

    def setUp(self):
        self._time = client_mod.time
        client_mod.time = FakeTime()

    def tearDown(self):
        client_mod.time = self._time

    def wait(self, script, timeout=120):
        return Client.wait_delivered(object(), WatchedMsg(script),
                                     timeout=timeout)

    def test_waits_until_the_transfer_is_delivered(self):
        from LXMF import LXMessage
        self.assertEqual(self.wait([LXMessage.OUTBOUND, LXMessage.SENDING,
                                    LXMessage.DELIVERED]), "delivered")

    def test_a_failed_transfer_is_not_reported_as_delivered(self):
        from LXMF import LXMessage
        self.assertEqual(self.wait([LXMessage.SENDING, LXMessage.FAILED]),
                         "failed")

    def test_still_in_flight_at_the_deadline_is_its_own_answer(self):
        from LXMF import LXMessage
        self.assertEqual(self.wait([LXMessage.OUTBOUND], timeout=60),
                         "timeout")


class FakeRNSForSend:
    class Destination:
        OUT = 1
        SINGLE = 2

        def __init__(self, *a, **k):
            pass


class FakeLXForSend:
    def __init__(self, dest, source, content=None, title=None):
        self.content = content


class RouteWait(unittest.TestCase):
    """send() hands the route search whatever is left of the caller's budget.

    This is the difference between a node that is gone and a client that has not
    finished learning routes: RNS can answer instantly or after several minutes,
    so the wait has to be the whole check, polled, not a fixed 90 seconds."""

    def setUp(self):
        self._rns, self._lx = client_mod.RNS, client_mod.LXMessage
        client_mod.RNS, client_mod.LXMessage = FakeRNSForSend, FakeLXForSend

    def tearDown(self):
        client_mod.RNS, client_mod.LXMessage = self._rns, self._lx

    def wait_used(self, deadline=None):
        seen = {}

        def recall(h, what, timeout=None):
            seen["timeout"] = timeout
            return object()

        me = SimpleNamespace(recall=recall, dest=None,
                             router=SimpleNamespace(handle_outbound=lambda m: None))
        Client.send(me, b"\xaa" * 16, b"ping", "ping", deadline=deadline)
        return seen["timeout"]

    def test_the_route_wait_gets_what_is_left_of_the_budget(self):
        used = self.wait_used(deadline=time.time() + 600)
        self.assertGreater(used, 590)
        self.assertLessEqual(used, 600)

    def test_the_budget_is_consumed_not_restarted(self):
        # half the window already spent must halve the route wait, not hand back
        # the full budget for a second phase
        deadline = time.time() + 600
        time.sleep(0.2)
        self.assertLess(self.wait_used(deadline=deadline), 600)

    def test_no_deadline_keeps_the_old_short_default(self):
        self.assertEqual(self.wait_used(), 90)


class Ping(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        games = Path(self.d.name) / "games"
        games.mkdir()
        self._rns, self._lx = host_mod.RNS, host_mod.LXMessage
        host_mod.RNS, host_mod.LXMessage = FakeRNS, FakeLXMessage
        FakeLXMessage.sent = []
        self.host = Host(Path(self.d.name) / "data", games)
        self.host.routers = {"zork1": Router()}
        self.host.destinations = {"zork1": object()}

    def tearDown(self):
        host_mod.RNS, host_mod.LXMessage = self._rns, self._lx
        self.d.cleanup()

    def rows(self):
        p = Path(self.d.name) / "data" / "ledger.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines()]

    def slots(self):
        root = Path(self.d.name) / "data" / "saves"
        return [] if not root.exists() else list(root.glob("*/*"))

    def test_a_ping_writes_no_ledger_record(self):
        self.host._on_message("zork1", Msg(PING))
        self.assertEqual(self.rows(), [],
                         "a ping must not appear as a turn or an outcome")

    def test_a_ping_writes_no_save_slot(self):
        self.host._on_message("zork1", Msg(PING))
        self.assertEqual(self.slots(), [])

    def test_a_ping_is_not_counted_as_inbound_traffic(self):
        self.host._on_message("zork1", Msg(PING))
        self.assertEqual(self.host.stats["inbound"], 0)
        self.assertEqual(self.host.stats["pings"], 1)
        self.assertIsNotNone(self.host.stats["last_ping"])

    def test_the_reply_names_the_boot_and_the_game_count(self):
        self.host._on_message("zork1", Msg(PING))
        self.assertEqual(FakeLXMessage.sent,
                         [ping_reply(self.host.ledger.boot, 0).encode()])

    def test_an_ack_marks_the_round_trip_and_records_nothing(self):
        # The ack is the only attestation anywhere that the outbound leg
        # reached a client, so it has to land -- and it still writes no turn.
        self.host._on_message("zork1", Msg(PING_ACK + " " + self.host.ledger.boot))
        self.assertEqual(self.host.stats["acks"], 1)
        self.assertIsNotNone(self.host.stats["last_roundtrip_ok"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(FakeLXMessage.sent, [])   # never answered: no loop

    def test_a_real_turn_is_still_recorded(self):
        self.host._on_message("zork1", Msg("north"))
        self.assertIn("open", [r["state"] for r in self.rows()])

    def test_a_player_typing_the_word_ping_in_a_sentence_is_not_a_ping(self):
        # exact match, not prefix: "ping the wizard" is a turn
        self.host._on_message("zork1", Msg(PING + " the wizard"))
        self.assertIn("open", [r["state"] for r in self.rows()])
        self.assertEqual(self.host.stats["pings"], 0)


if __name__ == "__main__":
    unittest.main()
