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


if __name__ == "__main__":
    unittest.main()