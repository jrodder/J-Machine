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


class ChunkReply(unittest.TestCase):
    """LXMF turns content over one link packet into an RNS Resource the
    recipient must pull, and phone clients never pull it: every reply has to
    fit in `limit` bytes or the player sees nothing."""

    def test_reassembles_exactly(self):
        from jhost.protocol import chunk_text
        for limit in (320, 64, 8):
            text = "\n".join(f"line {i} of the transcript, some length" for
                             i in range(40)) + "\n"
            parts = chunk_text(text, limit, parts=1000)
            self.assertEqual("".join(parts), text)
            for p in parts:
                self.assertLessEqual(len(p.encode()), limit,
                                     f"limit {limit}: {p[:30]!r}")

    def test_splits_multibyte_and_long_lines(self):
        from jhost.protocol import chunk_text
        parts = chunk_text("é" * 50 + "\n" + "x" * 500, 64, parts=1000)
        self.assertEqual("".join(parts), "é" * 50 + "\n" + "x" * 500)
        for p in parts:
            self.assertLessEqual(len(p.encode()), 64)
            p.encode().decode("utf-8")         # never a split codepoint

    def test_burst_is_bounded(self):
        from jhost.protocol import chunk_text
        parts = chunk_text("x\n" * 400, 64, parts=3)
        self.assertEqual(len(parts), 3)
        self.assertIn("more parts cut", parts[-1])

    def test_empty_reply_is_one_empty_message(self):
        from jhost.protocol import chunk_text
        self.assertEqual(chunk_text("", 320), [""])


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
