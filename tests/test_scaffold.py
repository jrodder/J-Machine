import unittest
from pathlib import Path

from tests.util import dfrotz_transcript, norm

CORPUS = Path(__file__).parent / "corpus"


class TestScaffold(unittest.TestCase):
    def test_corpus_sizes(self):
        for name, size in [("zork1.z3", 92160), ("minizork.z3", 52216),
                           ("planetfall.z5", 136560), ("risorg.z8", 443392)]:
            self.assertEqual((CORPUS / name).stat().st_size, size)

    def test_dfrotz_zork1_opening(self):
        t = norm(dfrotz_transcript(CORPUS / "zork1.z3", ["look"], seed=10))
        self.assertIn("ZORK I: The Great Underground Empire", t)
        self.assertIn("West of House", t)
        self.assertIn("Score: 0", t)


if __name__ == "__main__":
    unittest.main()