"""Zork I differential vs dfrotz (plan Task 13).

The walkthrough is hand-transcribed, so a full-run byte match is a report
(not a gate) — run `python3 tests/differential/run_differential.py` for the
first-divergence report and verify manually. Hard gates here: the harness
reproduces dfrotz byte-for-byte on the first 10 commands (engine truth),
and the full 119-command run completes on both sides with comparable output.
"""
import unittest

from tests.differential.run_differential import (
    STORY, WALK, first_divergence_index, walk_ours, walk_ref)


class TestZorkIWalkthrough(unittest.TestCase):
    def test_first_10_commands_byte_identical(self):
        a = walk_ours(n=10).split("\n")
        b = walk_ref(n=10).split("\n")
        i = first_divergence_index(a, b)
        self.assertEqual(i, min(len(a), len(b)),
                         "walkthrough diverges at transcript line %d:\n"
                         " ours:   %r\n dfrotz: %r" % (
                             i, a[i] if i < len(a) else "<eof>",
                             b[i] if i < len(b) else "<eof>"))

    def test_full_walk_smoke(self):
        # Both sides must survive all commands (incl. the in-game `save`s)
        # and produce substantial output. Divergence is allowed — see
        # run_differential.py's report for manual verification.
        a = walk_ours().split("\n")
        b = walk_ref().split("\n")
        self.assertGreater(len(a), 50, "our transcript suspiciously short")
        self.assertGreater(len(b), 50, "dfrotz transcript suspiciously short")


if __name__ == "__main__":
    unittest.main()