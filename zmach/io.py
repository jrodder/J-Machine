# zmach/io.py
"""Input buffer and dictionary vocabulary (ZSpec §13, §13.6).

Input model: whole-line feeds (the Session layer feeds one line at a time;
dfrotz -t does not echo piped input, so no echo/CR events are modelled).
Vocabulary mirrors dork's conformance-tested Vocabulary class
(references/dork vocab.ts): word-separators table from the dictionary
header, decoded-entry text map, and the cost-limited truncation used for
lookups (6 z-chars in v3, 9 in v5).
"""
from collections import deque

from .strings import DEFAULT_ZSCII_EXTRA, char_to_zscii, decode_text, zscii_to_char

# cheap chars cost 2 z-chars (A2 shift pair); everything else 4 (unencodable)
_CHEAP = set("0123456789.,!?_#\'\"/\\:-()")
_WS = " \n\t"


class InputBuffer:
    """Queued ZSCII input codes. feed() appends a whole line + 13 (CR)."""

    def __init__(self, extra=DEFAULT_ZSCII_EXTRA):
        self.extra = extra
        self.codes = deque()

    def feed(self, line):
        for ch in line:
            self.codes.append(char_to_zscii(ch, self.extra))
        self.codes.append(13)

    def get(self):
        return self.codes.popleft() if self.codes else 0

    @property
    def empty(self):
        return not self.codes


class Vocabulary:
    """word -> dictionary byte address, plus word splitting (§13.6.1).

    Dictionary header (ZSpec §13.2): [sep-count][seps...][entry-size]
    [entry-count (signed word)][entries...]. Entries are Z-encoded text
    (4 bytes v3 / 6 bytes v5, padded with z-char 5) followed by optional
    data bytes up to entry-size. All story-derived sizes are untrusted:
    the scan is bounded by the story data edge.
    """

    def __init__(self, vm):
        self.max_cost = 6 if vm.version < 5 else 9
        self.seps = ""
        self.map = {}
        data = vm.story.data
        d = vm.story.header.dictionary
        if not d or d + 3 >= len(data):
            return
        sep_count = data[d]
        end = d + 1 + sep_count
        if end + 3 > len(data):
            return
        self.seps = "".join(zscii_to_char(c) for c in data[d + 1:end])
        p = end
        entry_size = data[p]
        n = (data[p + 1] << 8) | data[p + 2]
        n = n - 0x10000 if n & 0x8000 else n  # signed entry count
        p += 3
        if entry_size < 4 or not 0 < n:
            return
        text_bytes = 4 if vm.version < 5 else 6
        n = min(n, (len(data) - p) // entry_size)  # hostile-file bound
        for _ in range(n):
            if p + entry_size > len(data):
                break
            s, _ = decode_text(vm.mem, vm.fwords, p, vm.zscii_extra,
                               vm.alphabet, max_bytes=text_bytes)
            # no two entries share an encoding (ZSpec §13.5); keep the first
            self.map.setdefault(s, p)
            p += entry_size

    def split(self, s):
        """ZSpec §13.6.1: spaces divide words and are ignored; each
        word-separator is a word in its own right. Returns [(offset, word)]."""
        words, i, n = [], 0, len(s)
        while i < n:
            ch = s[i]
            if ch in _WS:
                i += 1
                continue
            if self.seps and ch in self.seps:
                words.append((i, ch))
                i += 1
                continue
            j = i
            while j < n and s[j] not in _WS and not (self.seps and s[j] in self.seps):
                j += 1
            words.append((i, s[i:j]))
            i = j
        return words

    def lookup(self, word):
        """Dictionary byte address of word, or 0 if unrecognised.

        The key is truncated the way the compiler encoded it: letters cost
        1 z-char, cheap punctuation 2, anything else 4; the word must fit
        in the entry's z-char budget (6 v3 / 9 v5) to match.
        """
        cost, out = 0, []
        for ch in word:
            cost += 1 if "a" <= ch <= "z" else 2 if ch in _CHEAP else 4
            if cost > self.max_cost:
                break
            out.append(ch)
        return self.map.get("".join(out), 0)