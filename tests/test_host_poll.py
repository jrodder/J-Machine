"""Spec §3 poll: the propagation mailbox no one empties is a message-loss
hole. A client that cannot link to the host stores the turn at a node; LXMF
only delivers it when the recipient asks (LXMRouter.py jobs() never does)."""
import tempfile
import unittest
from pathlib import Path

from jhost.host import (DEFAULT_PROPAGATION_PULL, Host, PR_IN_FLIGHT)


class FakeRouter:
    def __init__(self, state=0):
        self.calls = []
        self.propagation_transfer_state = state

    def request_messages_from_propagation_node(self, identity):
        self.calls.append(identity)


class Poll(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        games = Path(self.d.name) / "games"
        games.mkdir()
        self.host = Host(Path(self.d.name) / "data", games,
                         propagation_node="ab" * 16)
        self.host.routers = {"b": FakeRouter(), "a": FakeRouter(),
                             "c": FakeRouter(state=max(PR_IN_FLIGHT))}
        self.host.identities = {k: k for k in self.host.routers}

    def tearDown(self):
        self.d.cleanup()

    def test_every_game_asks_for_its_own_mail(self):
        self.host._pull_propagation()
        self.assertEqual(self.host.routers["a"].calls, ["a"])
        self.assertEqual(self.host.routers["b"].calls, ["b"])

    def test_a_router_mid_download_is_left_alone(self):
        self.host._pull_propagation()
        self.assertEqual(self.host.routers["c"].calls, [])

    def test_no_node_no_poll(self):
        self.host.propagation_node = None
        self.host._pull_propagation()
        self.assertEqual(self.host.routers["a"].calls, [])

    def test_one_failing_router_does_not_stop_the_others(self):
        class Boom(FakeRouter):
            def request_messages_from_propagation_node(self, identity):
                raise OSError("no path")

        self.host.routers["a"] = Boom()
        self.host._pull_propagation()
        self.assertEqual(self.host.routers["b"].calls, ["b"])

    def test_zero_interval_disables_the_poll(self):
        self.host.propagation_pull = 0
        self.host._pull_propagation()
        self.assertEqual(self.host.routers["a"].calls, [])

    def test_poll_cadence(self):
        games = Path(self.d.name) / "games"
        default = Host(Path(self.d.name) / "data", games)
        self.assertEqual(default.propagation_pull, DEFAULT_PROPAGATION_PULL)
        self.assertEqual(Host(Path(self.d.name) / "data", games,
                              propagation_pull=60).propagation_pull, 60)


if __name__ == "__main__":
    unittest.main()
