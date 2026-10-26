"""Spec §3: every inbound message ends in a named outcome, and a turn that
cannot be answered is not played. RNS is faked here; the live path is covered
by tests/network."""
import json
import tempfile
import unittest
from pathlib import Path

import jhost.host as host_mod
from jhost.host import Host


class Msg:
    """Stands in for an LXMessage handed to the delivery callback."""

    def __init__(self, text="look", boom=None):
        self.source_hash = b"\xab" * 16
        self.signature_validated = True
        self.unverified_reason = None
        self.packed = b""
        self._text, self._boom = text, boom

    def content_as_string(self):
        if self._boom:
            raise self._boom
        return self._text


class FakeRNS:
    class Identity:
        @staticmethod
        def recall(h):
            return None            # the network never tells us who sent it

    class Transport:
        awaited = []

        @staticmethod
        def await_path(h, timeout=None):
            FakeRNS.Transport.awaited.append(h)
            return False


class Record(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        games = Path(self.d.name) / "games"
        games.mkdir()
        self._real_rns = host_mod.RNS
        host_mod.RNS = FakeRNS
        self.host = Host(Path(self.d.name) / "data", games)

    def tearDown(self):
        host_mod.RNS = self._real_rns
        self.d.cleanup()

    def rows(self):
        p = Path(self.d.name) / "data" / "ledger.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()]

    def test_an_unanswerable_turn_is_recorded_and_not_played(self):
        # FakeRNS can never recall the sender. The old order fed the game and
        # autosaved the slot first, then threw the reply away: the player's
        # story advanced with nothing to show for it.
        self.host._on_message("zork1", Msg())
        states = [r["state"] for r in self.rows()]
        self.assertEqual(states, ["open", "abandoned"])
        self.assertEqual(self.rows()[-1]["why"], "no_recalled_identity")
        self.assertEqual(self.host.sessions, {})
        slot_dir = Path(self.d.name) / "data" / "saves"
        self.assertFalse(slot_dir.exists() and any(slot_dir.iterdir()),
                         "no save slot may be written for an unplayed turn")

    def test_an_exception_never_escapes_to_lxmf(self):
        # LXMF wraps handler calls in an except that logs one line; swallowing
        # it there is how a turn vanishes with no trace.
        self.host._on_message("zork1", Msg(boom=ValueError("bad payload")))
        states = [r["state"] for r in self.rows()]
        self.assertEqual(states, ["open", "abandoned"])
        self.assertIn("ValueError", self.rows()[-1]["why"])

    def test_every_inbound_gets_an_open_line_before_any_work(self):
        self.host._on_message("zork1", Msg())
        self.host._on_message("zork1", Msg())
        self.assertEqual([r["state"] for r in self.rows()],
                         ["open", "abandoned", "open", "abandoned"])

    def test_a_message_with_no_usable_source_is_not_recorded_as_a_turn(self):
        class Garbage:
            signature_validated = True
            unverified_reason = None
            packed = b""

            def content_as_string(self):
                return "look"

            @property
            def source_hash(self):
                raise TypeError("no hash")

        self.host._on_message("zork1", Garbage())
        self.assertFalse((Path(self.d.name) / "data" / "ledger.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
