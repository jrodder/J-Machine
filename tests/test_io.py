# tests/test_io.py
"""Task 6 gate: planetfall must respond to 4 commands line-for-line vs
dfrotz -t (plan Step 1), plus unit checks for the input buffer and the
dictionary vocabulary (ZSpec §13)."""
import unittest
from pathlib import Path

from tests.util import dfrotz_transcript, norm
from zmach.io import InputBuffer
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text, Prompt

C = Path(__file__).parent / "corpus"
SEED = 10


def transcript(sf_path, lines, seed=SEED):
    sf = StoryFile.load(sf_path)
    vm = VM(sf, seed=seed)
    out, evs = [], vm.events
    def drain():
        out.extend(e.data for e in evs if isinstance(e, Text))
        evs.clear()
    vm.run_until_input(); drain()
    for line in lines:
        vm.feed(line)
        vm.run_until_input(); drain()
    return "".join(out)


class TestRandomFrotz(unittest.TestCase):
    """RNG must match frotz 2.55 (src/common/random.c): startup A = seed,
    standard mode A = 0x015a4e35*A + 1 (32-bit), result = (A >> 16) & 0x7fff,
    random K -> result % K + 1. Values hardcoded from the frotz reference
    for the call order planetfall makes on seed 10 (look turn)."""

    K = [180, 90, 3, 10] + [7] * 17 + [2, 1000, 100, 100]
    EXPECTED = [44, 88, 2, 9, 3, 2, 6, 3, 7, 2, 7, 7, 7, 6, 7, 6, 4, 2, 6,
                2, 4, 1, 305, 10, 34]

    def test_seed10_sequence(self):
        vm = VM(StoryFile.load(C / "planetfall.z5"), seed=SEED)
        vm.mem.putb(0x30000, 0)  # store byte 0 = push, scratch (zeroed mem)
        for k, want in zip(self.K, self.EXPECTED):
            vm.pc = 0x30000
            vm.op_random((k,), 1)
            self.assertEqual(vm._pop(), want, f"random({k})")

    def test_in_game_reseed_special_mode(self):
        # `random -10` (0 < 10 < 1000) -> special mode: results cycle 0..9,
        # value = counter % K + 1 (frotz seed_random, value < 1000 branch).
        vm = VM(StoryFile.load(C / "planetfall.z5"), seed=SEED)
        vm.mem.putb(0x30000, 0)
        vm.pc = 0x30000
        vm.op_random((-10,), 1)
        vm._pop()  # the 0 the reseed stores
        got = []
        for _ in range(12):
            vm.pc = 0x30000
            vm.op_random((7,), 1)
            got.append(vm._pop())
        self.assertEqual(got, [1, 2, 3, 4, 5, 6, 7, 1, 2, 3, 1, 2])


class TestIoDifferential(unittest.TestCase):
    def test_planetfall_commands(self):
        ours = transcript(C / "planetfall.z5", ["look", "north", "examine sky", "quit"])
        ref = norm(dfrotz_transcript(C / "planetfall.z5", ["look", "north", "examine sky", "quit"], seed=SEED))
        our_lines = [" ".join(l.split()) for l in (x.strip() for x in ours.split("\n")) if l and l != ">"]
        ref_lines = [l for l in ref.split("\n") if l and l != ">"]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 12, f"transcript too short: {m} lines — read/parse bug")
        self.assertEqual(len(our_lines), len(ref_lines),
                         "line count differs from dfrotz (missing/extra lines)")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")

    def test_planetfall_ambassador_arrives(self):
        # Seed 10 routes the random event check (look turn, 0x13C78 region)
        # so the Blow'k-bibben-Gordo ambassador arrives; dfrotz prints the
        # arrival paragraph and a later "offers you a bit of celery" line.
        ours = transcript(C / "planetfall.z5", ["look", "north"])
        self.assertIn(
            "The alien ambassador from the planet Blow'k-bibben-Gordo ambles toward you",
            ours)
        self.assertIn("The ambassador offers you a bit of celery.", ours)


class TestInputBuffer(unittest.TestCase):
    def test_feed_get_empty(self):
        b = InputBuffer()
        self.assertTrue(b.empty)
        self.assertEqual(b.get(), 0)
        b.feed("ab")
        self.assertFalse(b.empty)
        self.assertEqual(b.get(), 97)
        self.assertEqual(b.get(), 98)
        self.assertEqual(b.get(), 13)
        self.assertTrue(b.empty)

    def test_feed_encodes_accents(self):
        b = InputBuffer()
        b.feed("ä")
        self.assertEqual(b.get(), 155)
        self.assertEqual(b.get(), 13)


class TestVocabulary(unittest.TestCase):
    def setUp(self):
        self.vm = VM(StoryFile.load(C / "planetfall.z5"), seed=SEED)
        self.v = self.vm.vocab

    def test_separator_table(self):
        # Inform default (ZSpec §13.2): full stop, comma, double quote
        self.assertEqual(self.v.seps, chr(46) + chr(44) + chr(34))

    def test_lookup(self):
        self.assertNotEqual(self.v.lookup("look"), 0)
        self.assertNotEqual(self.v.lookup("north"), 0)
        self.assertNotEqual(self.v.lookup("examine"), 0)  # 7 letters, v5 budget
        self.assertEqual(self.v.lookup("sky"), 0)

    def test_split(self):
        # ZSpec §13.6.1: separators are words in their own right
        self.assertEqual(self.v.split("fred,go fishing"),
                         [(0, "fred"), (4, ","), (5, "go"), (8, "fishing")])
        self.assertEqual(self.v.split("  spaced   out  "), [(2, "spaced"), (11, "out")])


if __name__ == "__main__":
    unittest.main()