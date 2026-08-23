import unittest
from zmach.vm import cdiv, cmod


class TestCMath(unittest.TestCase):
    def test_cdivision(self):
        self.assertEqual(cdiv(11, 2), 5)
        self.assertEqual(cdiv(-11, 2), -5)
        self.assertEqual(cdiv(-11, -2), 5)
        self.assertEqual(cdiv(11, -2), -5)
        self.assertEqual(cmod(13, 5), 3)
        self.assertEqual(cmod(-13, 5), -3)
        self.assertEqual(cmod(13, -5), 3)
        self.assertEqual(cmod(-13, -5), -3)


if __name__ == "__main__":
    unittest.main()