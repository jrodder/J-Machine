"""Session API (spec §5): call -> batch with Prompt/EndOfGame boundaries.

Gate: planetfall load (events end in Prompt, first event is Text),
input per line, quit -> EndOfGame, input-after-done ->
Error("game over"), StoryInfo fields, cross-story restore ->
SaveFileError.
"""
import hashlib
import unittest
from pathlib import Path

from zmach.events import EndOfGame, Error, Prompt, SaveFileError, StoryFileError, Text
from zmach.session import Session

C = Path(__file__).parent / "corpus"
PF = C / "planetfall.z5"


class TestSessionBasics(unittest.TestCase):

    def test_load_events(self):
        s = Session()
        evs = s.load(PF, seed=10)
        self.assertIsInstance(evs[0], Text)
        self.assertIsInstance(evs[-1], Prompt)
        self.assertFalse(s.done)

    def test_story_info(self):
        s = Session()
        s.load(PF, seed=10)
        data = PF.read_bytes()
        si = s.story
        self.assertEqual(si.name, "planetfall")
        self.assertEqual(si.version, 5)
        self.assertEqual(si.release, int.from_bytes(data[2:4], "big"))
        self.assertEqual(si.serial, data[18:24].decode("ascii"))
        self.assertEqual(si.file_sha256, hashlib.sha256(data).digest())

    def test_input_batches(self):
        s = Session()
        s.load(PF, seed=10)
        evs = s.input("look")
        self.assertTrue(any(isinstance(e, Text) for e in evs))
        self.assertIsInstance(evs[-1], Prompt)

    def test_quit_ends_game(self):
        s = Session()
        s.load(PF, seed=10)
        self.assertIsInstance(s.input("look")[-1], Prompt)
        self.assertIsInstance(s.input("quit")[-1], Prompt)  # confirmation
        evs = s.input("yes")
        self.assertIsInstance(evs[-1], EndOfGame)
        self.assertEqual(evs[-1].status, 0)
        self.assertTrue(s.done)

    def test_input_after_done(self):
        s = Session()
        s.load(PF, seed=10)
        s.input("quit")
        s.input("yes")
        evs = s.input("look")
        self.assertEqual(evs, [Error("game over")])
        self.assertIsInstance(evs[0], Error)
        self.assertEqual(evs[0].message, "game over")

    def test_done_before_load_raises(self):
        s = Session()
        with self.assertRaises(StoryFileError):
            s.input("look")
        with self.assertRaises(StoryFileError):
            _ = s.story


class TestSessionRestore(unittest.TestCase):

    def test_restore_continues(self):
        s = Session()
        s.load(PF, seed=10)
        s.input("look")
        img = s.save()
        self.assertIsInstance(img, (bytes, bytearray))
        self.assertTrue(len(img) > 11864 + 100000)  # header + image
        evs = s.restore(img)
        self.assertIsInstance(evs[-1], Prompt)
        self.assertIsInstance(s.input("north")[-1], Prompt)

    def test_cross_story_restore_raises(self):
        s = Session()
        s.load(PF, seed=10)
        img = s.save()
        z = Session()
        z.load(C / "zork1.z3", seed=10)
        with self.assertRaises(SaveFileError):
            z.restore(img)

    def test_restore_corrupt_trailer_raises(self):
        s = Session()
        s.load(PF, seed=10)
        img = bytearray(s.save())
        img[-1] ^= 0x01
        with self.assertRaises(SaveFileError):
            s.restore(bytes(img))

    def test_restore_bad_magic_raises(self):
        s = Session()
        s.load(PF, seed=10)
        img = bytearray(s.save())
        img[0] = 0xFF
        with self.assertRaises(SaveFileError):
            s.restore(bytes(img))


if __name__ == "__main__":
    unittest.main()