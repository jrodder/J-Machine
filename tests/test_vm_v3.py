# tests/test_vm_v3.py
"""Task 8 gate: v3 compatibility — Zork I (corpus release 88) matches
dfrotz -t through a 10-command walkthrough."""
import unittest
from pathlib import Path

from tests.util import dfrotz_transcript, norm
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text

C = Path(__file__).parent / "corpus"
SEED = 10
LINES = ["look", "open mailbox", "take leaflet", "read leaflet",
         "west", "north", "turn on lamp", "east", "up", "quit"]


def run_vm(path, lines, seed=SEED):
    vm = VM(StoryFile.load(path), seed=seed)
    out = []
    vm.run_until_input()
    out += [e.data for e in vm.events if isinstance(e, Text)]
    vm.events.clear()
    for line in lines:
        vm.feed(line)
        vm.run_until_input()
        out += [e.data for e in vm.events if isinstance(e, Text)]
        vm.events.clear()
    return "".join(out)


class TestZorkI(unittest.TestCase):
    def test_first_ten_commands(self):
        ours = norm(run_vm(C / "zork1.z3", LINES))
        ref = norm(dfrotz_transcript(C / "zork1.z3", LINES, seed=SEED))
        our_lines = [l for l in ours.split("\n") if l]
        ref_lines = [l for l in ref.split("\n") if l]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 20, f"too few lines ({m}) — v3 coverage bug")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")
        self.assertEqual(len(our_lines), len(ref_lines),
                         f"line count: {len(our_lines)} vs {len(ref_lines)}")
        self.assertIn("Opening the small mailbox reveals a leaflet.", ours)

    def test_v3_status_line_columns(self):
        """norm() collapses whitespace, so the frotz z_show_status column
        layout (name padded to col 50, 'Score: ' at 50, 'Moves: ' at 66) is
        pinned here on the RAW transcript: the first status line is the
        exact 74-char string."""
        raw = run_vm(C / "zork1.z3", LINES)
        expected = " West of House" + " " * 36 + "Score: 0" + " " * 8 + "Moves: 0"
        self.assertIn(expected, [l.rstrip() for l in raw.split("\n")])


class TestMiniZork(unittest.TestCase):
    """Task 8 second v3 case (plan Step 3): the heavily-condensed C64
    build exercises the v3 layout path differently from zork1."""

    LINES = ["look", "east", "open door", "inventory", "quit"]

    def test_minizork(self):
        ours = norm(run_vm(C / "minizork.z3", self.LINES))
        ref = norm(dfrotz_transcript(C / "minizork.z3", self.LINES, seed=SEED))
        our_lines = [l for l in ours.split("\n") if l]
        ref_lines = [l for l in ref.split("\n") if l]
        self.assertGreaterEqual(len(ref_lines), 5, "minizork produced no output?")
        for i, (o, r) in enumerate(zip(our_lines, ref_lines)):
            self.assertEqual(o, r, f"line {i}: {o!r} != {r!r}")
        self.assertEqual(len(our_lines), len(ref_lines),
                         f"line count: {len(our_lines)} vs {len(ref_lines)}")


if __name__ == "__main__":
    unittest.main()