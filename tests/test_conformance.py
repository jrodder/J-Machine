"""Conformance gate (plan Task 12): the five self/interactive test
suites vs the reference (czech.out5 / live dfrotz -t runs).

This is the milestone that makes "conforms" a measured fact: each
suite hammers a different part of the instruction set, and a
transcript difference = a real conformance bug.

Suite drive scripts (probed against dfrotz -t, see
tests/conformance/run_conformance.py):
  czech    self-driving (blank lines), quits on its own
  crashme  self-driving, quits after "Done."
  strictz  N (no transcript) + blank lines (MORE pauses)
  unicode  read_char loop — fixed script, compare vs dfrotz
  random   menu — fixed script (run both graphs, quit), compare vs dfrotz

Plan-test notes:
- test_czech also asserts the "Performed N tests" totals line matches
  czech.out5 (the plan's last-line assertion alone only checks the
  static "Last test: quit!" line).
- strictz/unicode/random are compared against a live dfrotz -t run
  with the SAME drive script (norm-collapsed), not against a static
  file — they are interactive/MORE-sensitive.
"""
import unittest
from pathlib import Path

from tests.conformance.run_conformance import play_session_lines, play_to_end
from tests.util import dfrotz_transcript, norm

C = Path(__file__).parent / "corpus"

STRICTZ_LINES = ["N"] + [""] * 24          # N = no transcript; MORE lines
UNICODE_LINES = ["", "", "a", "B", "3", "$", "e", ""]
RANDOM_LINES = ["1", "", "2", "", "q"]     # bellcurve, spread, quit


class TestConformance(unittest.TestCase):

    def test_czech_totals(self):
        ours = norm(play_to_end(C / "czech.z5", seed=10, max_lines=80))
        ref = norm((C / "czech.out5").read_text())
        ours_l, ref_l = ours.strip().splitlines(), ref.strip().splitlines()
        # the plan's gate: the final line
        self.assertEqual(ours_l[-1], ref_l[-1])
        # the real gate: the pass/fail totals line
        o = [l for l in ours_l if l.startswith("Passed:")]
        r = [l for l in ref_l if l.startswith("Passed:")]
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0], r[0])
        self.assertIn("Failed: 0", o[0])

    def test_crashme_no_crash(self):
        text = play_to_end(C / "crashme.z5", seed=10, max_lines=400)
        self.assertIn("Done.", text)
        # the suite's own programming-error prints are EXPECTED output
        # (it deliberately abuses the machine) — what matters is that
        # the VM survived them and reached the end.
        self.assertIn("Programming error", text)

    def test_strictz_matches_dfrotz(self):
        ours = norm(play_session_lines(C / "strictz.z5", STRICTZ_LINES,
                                       seed=10))
        ref = norm(dfrotz_transcript(C / "strictz.z5", STRICTZ_LINES, seed=10))
        self.assertIn("Test completed!", ours)
        self.assertEqual(ours, ref)

    def test_unicode_matches_dfrotz(self):
        ours = norm(play_session_lines(C / "unicode.z5", UNICODE_LINES,
                                       seed=10))
        ref = norm(dfrotz_transcript(C / "unicode.z5", UNICODE_LINES, seed=10))
        # the read_char loop must name the ZSCII codes identically
        self.assertIn("ZSCII $0061 = a", ours)
        self.assertEqual(ours, ref)

    def test_random_matches_dfrotz(self):
        ours = norm(play_session_lines(C / "random.z5", RANDOM_LINES, seed=10))
        ref = norm(dfrotz_transcript(C / "random.z5", RANDOM_LINES, seed=10))
        self.assertIn("Bellcurve graph.", ours)
        self.assertIn("Spread graph.", ours)
        self.assertEqual(ours, ref)


if __name__ == "__main__":
    unittest.main()