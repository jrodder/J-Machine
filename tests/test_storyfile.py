import unittest
from pathlib import Path

from zmach.storyfile import StoryFile
from zmach.events import StoryFileError

C = Path(__file__).parent / "corpus"


class TestStoryFile(unittest.TestCase):
    def test_zork1_v3(self):
        f = StoryFile.load(C / "zork1.z3")
        h = f.header
        self.assertEqual((h.version, h.release, h.serial), (3, 88, "840726"))
        self.assertEqual((h.highmem, h.pc, h.dictionary), (0x4e37, 0x4f05, 0x3b21))
        self.assertEqual((h.objects, h.globals_base, h.static_base), (0x02b0, 0x2271, 0x2e53))
        self.assertEqual(h.declared_len, 0xa5c6 * 2)          # 84876 < file size 92160
        self.assertEqual(len(f.data), h.declared_len)          # padding excluded
        self.assertEqual(h.checksum, 0xa129)
        # ZSpec §15 (verify): checksum = sum of each byte from 0x40 to the
        # declared length, modulo 0x10000
        total = sum(f.data[0x40:h.declared_len]) & 0xffff
        self.assertEqual(total, h.checksum)

    def test_planetfall_v5_exact_len(self):
        f = StoryFile.load(C / "planetfall.z5")
        self.assertEqual(f.header.version, 5)
        self.assertEqual(f.header.declared_len, len(C.joinpath("planetfall.z5").read_bytes()))
        self.assertEqual(f.name, "planetfall")
        self.assertEqual(f.header.serial, "880531")

    def test_risorg_v8(self):
        f = StoryFile.load(C / "risorg.z8")
        self.assertEqual(f.header.version, 8)
        self.assertEqual(f.header.declared_len, 0xd86d * 8)

    def test_unsupported_version(self):
        data = bytearray((C / "planetfall.z5").read_bytes())
        data[0] = 6
        p = C.parent / "bad.z6"
        p.write_bytes(bytes(data))
        try:
            with self.assertRaises(StoryFileError) as cm:
                StoryFile.load(p)
            self.assertIn("6", str(cm.exception))
        finally:
            p.unlink()

    def test_checksum_mismatch_warns_not_raises(self):
        f = StoryFile.load(C / "zork1.z3", strict=False)   # must not raise
        self.assertEqual(f.sha256, __import__("hashlib").sha256(f.data).digest())


if __name__ == "__main__":
    unittest.main()