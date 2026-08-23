# tests/test_memory.py
import unittest
from pathlib import Path
from zmach.memory import Memory
from zmach.storyfile import StoryFile

C = Path(__file__).parent / "corpus"

class TestMemory(unittest.TestCase):
    def setUp(self):
        self.m = Memory(StoryFile.load(C / "zork1.z3"))

    def test_story_copied(self):
        self.assertEqual(self.m.mem[0], 3)
        self.assertEqual(self.m.getw(0x12), ord("8") << 8 | ord("4"))  # serial "84…" BE

    def test_oob(self):
        self.assertEqual(self.m.getb(524288), 0)
        self.assertEqual(self.m.getw(524286), 0)
        self.m.putb(524288, 0xAA)          # ignored, no exception
        self.m.putw(524287, 0xBB)          # ignored, no exception
        self.assertEqual(self.m.mem[524287], 0)

    def test_word_endianness(self):
        self.m.putw(0x100, 0x1234)
        self.assertEqual((self.m.mem[0x100], self.m.mem[0x101]), (0x12, 0x34))
        self.assertEqual(self.m.getw(0x100), 0x1234)

    def test_v8_width(self):
        m8 = Memory(StoryFile.load(C / "risorg.z8"))
        self.assertEqual(m8.width, 8)
        self.assertEqual(m8.stack_top, 0x3FFFE)
        self.assertEqual(self.m.stack_top, 0xFFFE)
        m8.putu64(0x100, 0x1122334455667788)
        self.assertEqual(m8.getu64(0x100), 0x1122334455667788)

    def test_reset(self):
        self.m.putw(0x100, 0xDEAD)
        self.m.putw(0x15000, 0xBEEF)   # past zork1's declared_len (84876)
        self.m.reset()
        self.assertEqual(self.m.getw(0x100), 0x20d3)  # story bytes restored
        self.assertEqual(self.m.getw(0x15000), 0)     # non-story memory cleared

    def test_byte_swapped(self):
        class FakeHeader:
            version, flags1, declared_len = 3, 1, 16
        class FakeStory:
            header = FakeHeader()
            data = bytes(16)
            def memory_size(self): return 512
        m = Memory(FakeStory())
        self.assertTrue(m.byte_swapped)
        m.putw(0, 0x1234)
        self.assertEqual((m.mem[0], m.mem[1]), (0x34, 0x12))  # low byte first
        self.assertEqual(m.getw(0), 0x1234)


if __name__ == "__main__":
    unittest.main()