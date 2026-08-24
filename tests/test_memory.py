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
        n = len(self.m.mem)  # zork1: story image + 0x2000-word stack
        self.assertEqual(self.m.getb(n), 0)
        self.assertEqual(self.m.getw(n - 1), 0)
        self.m.putb(n, 0xAA)               # ignored, no exception
        self.m.putw(n - 1, 0xBB)           # ignored, no exception
        self.assertEqual(self.m.mem[n - 1], 0)

    def test_word_endianness(self):
        self.m.putw(0x100, 0x1234)
        self.assertEqual((self.m.mem[0x100], self.m.mem[0x101]), (0x12, 0x34))
        self.assertEqual(self.m.getw(0x100), 0x1234)

    def test_stack_placement(self):
        # frotz 2.55: variables are 16-bit in ALL versions (v8 included);
        # the data stack sits directly above the story image. Stack sizes in
        # words: v1-v3 0x2000, v4-v7 0x4000, v8 0x20000.
        m8 = Memory(StoryFile.load(C / "risorg.z8"))
        self.assertEqual(m8.stack_top, 0x6C368 + 0x3FFFE)  # risorg declared_len
        self.assertEqual(self.m.stack_top, 0x14B8C + 0x3FFE)  # zork1
        # 16-bit word access everywhere (no 64-bit values)
        m8.putw(0x100, 0x1234)
        self.assertEqual(m8.getw(0x100), 0x1234)

    def test_stack_within_buffer(self):
        # frotz allocates story image + stack; the stack top must be INSIDE
        # the buffer (a fixed 512 KB image is too small for v8: risorg's
        # stack top 0xAC366 sat past 0x80000 and every push/pop silently
        # no-op'd, corrupting all data-stack traffic).
        m8 = Memory(StoryFile.load(C / "risorg.z8"))
        self.assertLess(m8.stack_top, len(m8.mem))
        m8.putw(m8.stack_top - 2, 0xD86D)   # push at stack top must persist
        self.assertEqual(m8.getw(m8.stack_top - 2), 0xD86D)

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