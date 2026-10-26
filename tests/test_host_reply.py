"""Reply delivery fallback and its record (jhost.host._reply_failed).

Skipped where LXMF/Reticulum are not installed (the host box runs it).
LXMessage is stubbed: what is under test is which message jhost hands back to
the router after a direct send fails, and what the ledger says about it -- not
LXMF's own crypto.
"""
import json
import tempfile
import unittest
from pathlib import Path

try:
    import LXMF          # noqa: F401
    import RNS           # noqa: F401
    HAS_LXMF = True
except ImportError:
    HAS_LXMF = False


@unittest.skipUnless(HAS_LXMF, "needs LXMF/Reticulum")
class ReplyFallback(unittest.TestCase):
    def setUp(self):
        from jhost import host
        from jhost.ledger import Ledger

        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.sent = []
        real = host.LXMessage

        class Fake:
            PROPAGATED = "propagated"
            DELIVERED = "delivered"
            REJECTED = "rejected"

            def __init__(self, dest, src, content=b"", title="",
                         desired_method=None):
                self.dest, self.content = dest, content
                self.desired_method = desired_method
                self.method = desired_method or "opportunistic"
                self.state = "outbound"
                self.failed_callback = None
                self.delivery_callback = None

            def register_delivery_callback(self, callback):
                self.delivery_callback = callback

        self.addCleanup(setattr, host, "LXMessage", real)
        host.LXMessage = Fake
        self.host = object.__new__(host.Host)   # no Reticulum, no filesystem
        self.host.destinations = {"zork1": "GAME-DEST"}
        self.host.ledger = Ledger(Path(self.d.name) / "ledger.jsonl")
        self.host.routers = {"zork1": type("R", (), {
            "handle_outbound": lambda _s, m: self.sent.append(m),
            "direct_links": {}})()}

    def rows(self):
        p = Path(self.d.name) / "ledger.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()]

    def states(self):
        return [r["state"] for r in self.rows()]

    def _fail(self, node):
        class Dest:
            hash = b"\x02" * 16

        self.host.propagation_node = node
        self.host._reply_failed("zork1", "aabbccdd", Dest(),
                                b"West of House\nYou are standing...", 7)

    def test_failed_reply_hands_the_text_to_the_node(self):
        self._fail(b"\x01" * 16)
        self.assertEqual(len(self.sent), 1)
        m = self.sent[0]
        self.assertEqual(m.desired_method, "propagated")
        self.assertEqual(m.content, b"West of House\nYou are standing...")
        self.assertEqual(m.dest.hash, b"\x02" * 16)
        self.assertEqual(self.host.routers["zork1"].direct_links, {})

    def test_without_a_node_nothing_is_requeued(self):
        self._fail(None)
        self.assertEqual(self.sent, [])

    def test_the_failure_is_recorded_with_a_reason(self):
        self._fail(None)
        self.assertIn("failed", self.states())
        failed = [r for r in self.rows() if r["state"] == "failed"][0]
        self.assertEqual(failed["why"], "link_delivery_failed")
        self.assertEqual(failed["bytes_out"], 33)
        self.assertEqual(failed["s"], 7)

    def test_a_reply_dropped_for_want_of_a_node_is_recorded(self):
        self._fail(None)
        dropped = [r for r in self.rows() if r["state"] == "dropped"]
        self.assertEqual([r["why"] for r in dropped], ["no_propagation_node"])

    def test_the_network_answer_arrives_as_a_second_line(self):
        # the handed_off line says the host did its part; this is the mesh's
        # answer, which is a separate event and a separate line
        self._fail(b"\x01" * 16)
        m = self.sent[0]
        m.state = "delivered"
        m.delivery_callback(m)
        self.assertIn("delivered", self.states())

    def test_a_reply_lost_via_propagation_is_recorded(self):
        self._fail(b"\x01" * 16)
        m = self.sent[0]
        m.failed_callback(m)
        dropped = [r for r in self.rows() if r["state"] == "dropped"]
        self.assertEqual([r["why"] for r in dropped], ["propagation_failed"])


if __name__ == "__main__":
    unittest.main()
