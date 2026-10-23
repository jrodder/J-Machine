"""Reply delivery fallback (jhost.host._reply_failed).

Skipped where LXMF/Reticulum are not installed (the host box runs it).
LXMessage is stubbed: the point is which message jhost hands back to the
router after a direct send fails, not LXMF's own crypto.
"""
import unittest

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

        self.sent = []
        real = host.LXMessage

        class Fake:
            PROPAGATED = "propagated"
            SINGLE_MESSAGE = "single"

            def __init__(self, dest, src, content=b"", title="",
                         desired_method=None):
                self.dest, self.content = dest, content
                self.desired_method = desired_method
                self.failed_callback = None

        self.addCleanup(setattr, host, "LXMessage", real)
        host.LXMessage = Fake
        self.host = object.__new__(host.Host)   # no Reticulum, no filesystem
        self.host.destinations = {"zork1": "GAME-DEST"}
        self.host.routers = {"zork1": type("R", (), {
            "handle_outbound": lambda _s, m: self.sent.append(m),
            "direct_links": {}})()}

    def _fail(self, node):
        class Dest:
            hash = b"\x02" * 16

        self.host.propagation_node = node
        self.host._reply_failed("zork1", "a" * 32, Dest(),
                                "West of House\nYou are standing...")

    def test_failed_reply_hands_the_text_to_the_node(self):
        self._fail(b"\x01" * 16)
        self.assertEqual(len(self.sent), 1)
        m = self.sent[0]
        self.assertEqual(m.desired_method, "propagated")
        self.assertEqual(m.content.decode(),
                         "West of House\nYou are standing...")
        self.assertEqual(m.dest.hash, b"\x02" * 16)
        self.assertEqual(self.host.routers["zork1"].direct_links, {})

    def test_without_a_node_nothing_is_requeued(self):
        self._fail(None)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
