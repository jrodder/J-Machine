import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text

C = Path(__file__).parent / "corpus"
SEED = 10


class TestPlanetfallOpening(unittest.TestCase):
    def test_opening_matches_dfrotz(self):
        sf = StoryFile.load(C / "planetfall.z5")
        vm = VM(sf, seed=SEED)
        vm.run_until_input()
        ours = "".join(e.data for e in vm.events if isinstance(e, Text))
        ref = norm(dfrotz_transcript(C / "planetfall.z5", [], seed=SEED))
        # opening must match through the first prompt (trim at last '>')
        self.assertIn("PLANETFALL", ours.upper())
        # line-by-line comparison of the opening block
        # line-by-line comparison of the opening block (whitespace-normalized
        # on both sides: frotz pads the status line to 80 cols)
        our_lines = [" ".join(l.split()) for l in ours.split("\n") if l.strip()]
        ref_lines = [l for l in ref.split("\n") if l and l != ">"]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 8, "opening too short — opcode coverage bug")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")


if __name__ == "__main__":
    unittest.main()