"""Spec §7 tests 6-7: protocol edge-behavior table + save sanity.
No RNS import — milliseconds each."""
import tempfile
import unittest
from pathlib import Path

from jhost.protocol import (FileSaveStore, INPUT_CAP, handle_message,
                            render_page, write_rns_config)
from tests.conformance.run_conformance import play_session_lines
from tests.util import norm
from zmach.events import Text
from zmach.session import Session

C = Path(__file__).parent / "corpus"
ZORK = C / "zork1.z3"
PF = C / "planetfall.z5"
RISORG = C / "risorg.z8"
SEED = 10
S1 = "aa" * 16  # player "A" delivery hash (hex form)


class P:
    """Fixture: fresh sessions map + FileSaveStore per test."""

    def __init__(self, story=ZORK):
        self.d = tempfile.TemporaryDirectory()
        self.store = FileSaveStore(Path(self.d.name) / "saves")
        self.sessions = {}
        self.story = story
        self.game = Path(story).stem

    def call(self, text="", sender=S1, verified=True, seed=SEED):
        return handle_message(self.game, sender, text, verified,
                              self.sessions, self.store, str(self.story), seed)

    def close(self):
        self.d.cleanup()


def text_of(events):
    return "".join(e.data for e in events if isinstance(e, Text))


class Protocol(unittest.TestCase):
    def setUp(self):
        self.p = P()

    def tearDown(self):
        self.p.close()

    def test_unverified_rejected(self):
        r = self.p.call(text="look", verified=False)
        self.assertEqual(r, "[Rejected: unverified sender]")
        self.assertEqual(self.p.sessions, {})

    def test_first_contact_intro(self):
        r = self.p.call(text="")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, [], seed=SEED)))

    def test_first_contact_with_line(self):
        r = self.p.call(text="look")
        self.assertEqual(norm(r),
                         norm(play_session_lines(ZORK, ["look"], seed=SEED)))

    def test_first_contact_restore(self):
        # pre-build a 3-turn save, then first contact from that identity
        s = Session()
        s.load(str(ZORK), seed=SEED)
        for l in ["look", "north", "look"]:
            s.input(l)
        img = s.save()
        self.p.store.save(self.p.game, S1, img)
        s2 = Session()
        s2.load(str(ZORK), seed=SEED)  # intro discarded by the protocol
        ref = text_of(s2.restore(img))
        self.assertEqual(norm(self.p.call(text="")), norm(ref))

    def test_first_contact_restore_with_line(self):
        s = Session()
        s.load(str(ZORK), seed=SEED)
        for l in ["look", "north", "look"]:
            s.input(l)
        img = s.save()
        self.p.store.save(self.p.game, S1, img)
        s2 = Session()
        s2.load(str(ZORK), seed=SEED)
        ref = text_of(s2.restore(img)) + text_of(s2.input("south"))
        self.assertEqual(norm(self.p.call(text="south")), norm(ref))

    def test_corrupt_save_fresh_start(self):
        self.p.store.save(self.p.game, S1, b"not-a-save" * 50)
        r = self.p.call(text="")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, [], seed=SEED)))

    def test_story_unavailable(self):
        # unreadable story file at first contact -> reply, not exception
        r = handle_message("zork1", S1, "", True, self.p.sessions,
                           self.p.store, "/nonexistent/story.z5", SEED)
        self.assertEqual(r, "[Game unavailable]")
        self.assertEqual(self.p.sessions, {})  # nothing stored, sender may retry

    def test_existing_session_empty_line(self):
        self.assertEqual(self.p.call(text=""), self.p.call(text=""))

    def test_replies_are_per_turn_deltas(self):
        # Spec §4 (2026-08-25): a reply after first contact carries ONLY
        # the new turn's text — the phone's chat scrollback is the
        # transcript (Sideband renders each LXMF message as a bubble, so a
        # cumulative re-send is O(n^2) text). Regression: turn N+1's reply
        # must not re-send turn N's text.
        r0 = self.p.call(text="")  # first contact: full intro batch
        self.assertIn("open field", norm(r0))
        r1 = self.p.call(text="look")
        self.assertIn("open field", norm(r1))  # the west-of-house turn
        r2 = self.p.call(text="north")
        self.assertIn("North of House", norm(r2))
        self.assertNotIn("open field", norm(r2),
                         "turn 2's reply must not re-send turn 1's text")

    def test_input_cap(self):
        r = self.p.call(text="x" * (INPUT_CAP + 1))
        self.assertEqual(r, "[Input rejected: line too long (>200)]")
        # session intact: the next valid line plays (delta reply)
        s = Session()
        s.load(str(ZORK), seed=SEED)
        ref = text_of(s.input("look"))
        self.assertEqual(norm(self.p.call(text="look")), norm(ref))

    def test_done_session(self):
        p = P(story=PF)
        for l in ["look", "open mailbox", "take leaflet", "north", "east",
                  "south", "west", "look"]:
            p.call(text=l)
        p.call(text="quit")
        p.call(text="y")  # planetfall's quit asks "Do you wish to leave the game? (Y is affirmative):"
        self.assertTrue(p.sessions[("planetfall", S1)].session.done)
        for t in ["", "look", "whatever"]:
            self.assertEqual(p.call(text=t), "[Game over]")
        p.close()

    def test_ingame_save_restore(self):
        # risorg's in-game SAVE/RESTORE verbs -> host-local slot, no prompt.
        # Under seed 10 the intro fills the screen: load parks at the
        # ***MORE*** boundary. The protocol never feeds an empty line
        # (empty = transcript-only reply), so one non-empty line is fed
        # to absorb the MORE pause before the first game verb reaches
        # the parser (the VM discards the line at the pause).
        p = P(story=RISORG)
        self.assertIsNone(p.store.load("risorg", S1))
        p.call(text="")
        p.call(text="more")
        self.assertIn("Ok.", p.call(text="save"))
        self.assertIsNotNone(p.store.load("risorg", S1))
        self.assertIn("Ok.", p.call(text="restore"))
        # the slot image round-trips into a fresh Session (spec §7 test 7)
        img = p.store.load("risorg", S1)
        s = Session()
        s.load(str(RISORG), seed=SEED)
        s.restore(img)
        # and the look after restore shows the save-point room — the delta
        # reply is the turn's own text (no preceding prompt), so reference
        # it at Session level: load + "" (absorbed at the ***MORE*** pause,
        # as the wire's "more" line) + look
        s = Session()
        s.load(str(RISORG), seed=SEED)
        s.input("")
        ref_look = text_of(s.input("look"))
        self.assertEqual(norm(p.call(text="look")), norm(ref_look))
        p.close()

    def test_autosave_roundtrip(self):
        p = P()
        p.call(text="look")
        r2 = p.call(text="north")
        img = p.store.load("zork1", S1)
        self.assertIsNotNone(img)
        s = Session()
        s.load(str(ZORK), seed=SEED)
        s.restore(img)
        ref = text_of(s.input("south"))
        r3 = p.call(text="south")
        # reply is the turn's delta -> r3 IS the new turn's text
        self.assertEqual(norm(r3), norm(ref))
        p.close()


class Helpers(unittest.TestCase):
    def test_file_save_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            st = FileSaveStore(d)
            self.assertIsNone(st.load("g", "s"))
            st.save("g", "s", b"img")
            self.assertEqual(st.load("g", "s"), b"img")
            self.assertFalse(list(Path(d).rglob("*.tmp")))

    def test_render_page(self):
        page = render_page("J-Machine Games",
                           [("zork1", 3, "<ab:cd:ef>"),
                            ("planetfall", 5, "<90:12:34>")])
        self.assertTrue(page.startswith(">J-Machine Games\n>"))
        self.assertIn("> zork1 (v3)\n  <ab:cd:ef>", page)
        self.assertIn("> planetfall (v5)\n  <90:12:34>", page)

    def test_write_rns_config(self):
        with tempfile.TemporaryDirectory() as d:
            h = write_rns_config(d + "/hostcfg", "host")
            c = write_rns_config(d + "/clientcfg", "client")
            ht, ct = h.read_text(), c.read_text()
            for k in ("type = TCPServerInterface", "listen_port = 4242",
                      "share_instance = yes", "[interfaces]", "[logging]"):
                self.assertIn(k, ht)
            for k in ("type = TCPClientInterface", "target_port = 4242"):
                self.assertIn(k, ct)
            # non-clobbering: second call must not replace the file
            first = h.read_text()
            write_rns_config(d + "/hostcfg", "host")
            self.assertEqual(first, h.read_text())


if __name__ == "__main__":
    unittest.main()
