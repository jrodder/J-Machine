# zmach/memory.py
"""64K-plus address space (512 KB uniform image). OOB reads -> 0,
writes ignored (spec §9). Big-endian words; byte_swapped (v1-4 flag)
flips word endianness."""


class Memory:
    def __init__(self, story):
        self.story = story
        self.mem = bytearray(story.memory_size())
        n = story.header.declared_len
        self.mem[:n] = story.data[:n]
        self.byte_swapped = bool(story.header.flags1 & 1) and story.header.version < 5
        # frotz 2.55 model: variables/stack are 16-bit in ALL versions
        # (v8 stories run on the v7 instruction set here, no 64-bit words).
        # The stack sits directly above the story image; memory_size() already
        # includes it, so the top word is the last word of the buffer.
        self.stack_top = story.memory_size() - 2

    def reset(self):
        self.mem[:] = bytearray(self.story.memory_size())
        n = self.story.header.declared_len
        self.mem[:n] = self.story.data[:n]

    def getb(self, a):
        return self.mem[a] if 0 <= a < len(self.mem) else 0

    def putb(self, a, v):
        if 0 <= a < len(self.mem):
            self.mem[a] = v & 0xFF

    def getw(self, a):
        b0, b1 = self.getb(a), self.getb(a + 1)
        return (b1 << 8 | b0) if self.byte_swapped else (b0 << 8 | b1)

    def putw(self, a, v):
        v &= 0xFFFF
        if self.byte_swapped:
            self.putb(a, v & 0xFF)
            self.putb(a + 1, v >> 8)
        else:
            self.putb(a, v >> 8)
            self.putb(a + 1, v & 0xFF)