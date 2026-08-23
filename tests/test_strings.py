import unittest
from pathlib import Path

from zmach.memory import Memory
from zmach.storyfile import StoryFile
from zmach.strings import decode_text, encode_text, read_custom_tables

C = Path(__file__).parent / "corpus"


def load(name):
    sf = StoryFile.load(C / name)
    return sf, Memory(sf)


class TestStrings(unittest.TestCase):
    def test_default_tables(self):
        sf, m = load("zork1.z3")
        extra, alpha = read_custom_tables(sf)
        self.assertEqual(alpha, None)           # zork1 has no custom alphabet
        self.assertEqual(len(extra), 69)        # default 155..223 table

    def test_decode_simple_words(self):
        sf, m = load("zork1.z3")
        extra, _ = read_custom_tables(sf)
        # "open" as v3 dictionary-form encoding: letters sit at A0 z-chars 6-31
        zc = [6 + ord(c) - 97 for c in "open"] + [5, 5]
        w1 = (zc[0] << 10) | (zc[1] << 5) | zc[2]
        w2 = (zc[3] << 10) | (zc[4] << 5) | zc[5]
        w2 |= 0x8000  # end bit
        base = 0x8000
        for i, w in enumerate((w1, w2)):
            m.putw(base + i * 2, w)
        text, end = decode_text(m, sf.header.fwords, base, extra, None)
        self.assertEqual(text, "open")
        self.assertEqual(end, base + 4)

    def test_encode_decode_roundtrip(self):
        sf, m = load("planetfall.z5")
        extra, alpha = read_custom_tables(sf)
        # dictionary-form encoding is per-word (ZSpec §3.7): single words only
        for s in ("look", "open", "a", "zz", "go"):
            b = encode_text(s, m, 5)
            off = 0x8000
            m.mem[off:off + len(b)] = b
            text, _ = decode_text(m, sf.header.fwords, off, extra, alpha)
            self.assertEqual(text, s)

    def test_dictionary_lookup_zork1(self):
        sf, m = load("zork1.z3")
        extra, _ = read_custom_tables(sf)
        b = encode_text("open", m, 3)
        # binary search zork1's dictionary (n_sep=3, entry_len=7, count=697)
        d = sf.header.dictionary
        n_sep = m.getb(d)
        entry_len = m.getb(d + 1 + n_sep)
        count = m.getw(d + 2 + n_sep)
        self.assertEqual((n_sep, entry_len, count), (3, 7, 697))
        base = d + 2 + n_sep + 2
        lo, hi = 0, count - 1
        found = False
        while lo <= hi:
            mid = (lo + hi) // 2
            off = base + mid * entry_len
            key = int.from_bytes(m.mem[off:off + 4], "big")
            want = int.from_bytes(b, "big")
            if key == want:
                found = True
                break
            if key < want:
                lo = mid + 1
            else:
                hi = mid - 1
        self.assertTrue(found, "'open' must be in Zork I's dictionary")


if __name__ == "__main__":
    unittest.main()
