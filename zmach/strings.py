"""Z-encoded text. Algorithm verified against dork's conformance-tested
decodeText (references/dork/text.ts): bit order c1=(w>>10)&31, c2=(w>>5)&31,
c3=w&31, end=w&0x8000; shift chars 4/5; abbreviations z-chars 1-3 ->
fwords table; 10-bit ZSCII from A2 z-char 6; ZSCII 13 -> newline."""

ALPHABET = ('abcdefghijklmnopqrstuvwxyz'
            'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            '*\n0123456789.,!?_#\'"/\\-:()')

DEFAULT_ZSCII_EXTRA = ('äöüÄÖÜß»«ëïÿËÏáéíóúýÁÉÍÓÚÝàèìòùÀÈÌÒÙ'
                       'âêîôûÂÊÎÔÛåÅøØãñõÃÑÕæÆçÇþðÞÐ£œŒ¡¿')
_EXTRA_MIN, _EXTRA_MAX = 155, 251


def read_custom_tables(story):
    h = story.header
    data = story.data
    if h.version < 5:
        return DEFAULT_ZSCII_EXTRA, None
    extra = DEFAULT_ZSCII_EXTRA
    if h.header_ext_addr:
        ext_len = _w(data, h.header_ext_addr)
        if ext_len >= 3:
            taddr = _w(data, h.header_ext_addr + 2 * 3)
            if 0 < taddr < len(data):
                n = data[taddr]
                out = []
                for i in range(min(n, _EXTRA_MAX - _EXTRA_MIN + 1)):
                    out.append(chr(_w(data, taddr + 1 + i * 2)))
                # custom table overrides 155..155+N-1; the rest keeps defaults
                extra = "".join(out) + DEFAULT_ZSCII_EXTRA[len(out):]
    alpha = data[h.alphabet_addr:h.alphabet_addr + 78] if h.alphabet_addr else None
    return extra, (alpha if alpha is None or len(alpha) == 78 else None)


def _w(data, a):
    if a + 2 > len(data):
        return 0
    return (data[a] << 8) | data[a + 1]


def zscii_to_char(code, extra=DEFAULT_ZSCII_EXTRA):
    if code == 13:
        return "\n"
    if code == 0:
        return ""
    if _EXTRA_MIN <= code <= _EXTRA_MAX:
        i = code - _EXTRA_MIN
        if i < len(extra) and extra[i] != "\x00":
            return extra[i]
        return ""
    return chr(code)


def char_to_zscii(ch, extra=DEFAULT_ZSCII_EXTRA):
    if ch == "\n":
        return 13
    o = ord(ch)
    if o < 0xA0:
        return o
    # the extra table is NOT an identity map (codepoint != ZSCII code):
    # scan it (dork charToZscii semantics)
    for i, c in enumerate(extra):
        if c == ch:
            return _EXTRA_MIN + i
    return o


def decode_text(mem, fwords, addr, extra=DEFAULT_ZSCII_EXTRA, alpha=None, wide=False):
    if wide:
        return decode_wide(mem, addr, extra)
    out = []
    ts = ps = y = 0
    while True:
        w = mem.getw(addr)
        addr += 2
        for v in ((w >> 10) & 31, (w >> 5) & 31, w & 31):
            if ts == 3:                      # top half of 10-bit ZSCII
                y = v << 5
                ts = 4
            elif ts == 4:                    # bottom half
                y += v
                out.append(zscii_to_char(y, extra))
                ts = ps
            elif ts == 5:                    # abbreviation
                out.append(decode_text(mem, fwords,
                                       mem.getw(fwords + (y + v) * 2) * 2,
                                       extra, alpha)[0])
                ts = ps
            elif v == 0:
                out.append(" ")
            elif v < 4:
                ts, y = 5, (v - 1) * 32
            elif v < 6:
                if not ts:
                    ts = v - 3
                elif ts == v - 3:
                    ps = ts
                else:
                    ps = ts = 0
            elif v == 6 and ts == 2:
                ts = 3
            else:
                idx = ts * 26 + v - 6
                if alpha is not None:
                    out.append(zscii_to_char(alpha[idx], extra))
                else:
                    out.append(ALPHABET[idx])
                ts = ps
        if w & 0x8000:
            break
    return "".join(out), addr


def decode_wide(mem, addr, extra=DEFAULT_ZSCII_EXTRA):
    out = []
    while True:
        w = mem.getw(addr)
        addr += 2
        if w == 0:
            break
        c = w & 0x3FF
        out.append(zscii_to_char(13 if c == 10 else c, extra))
    return "".join(out), addr


# --- dictionary-form encoding (ZSpec §3.7) -------------------------------
# A0: letters at z-chars 6-31. Digits/punctuation route through an A2 shift
# (z-char 5 + A2 z-char). A2 row: index 0='^'(escape, unused), 1=\n, 2-11=0-9,
# 12='.', 13=',', 14='!', 15='?', 16='_', 17='#', 18="'", 19='"', 20='/',
# 21='\\', 22='-', 23=':', 24='(', 25=')'.
A2_ENC = "^\n0123456789.,!?_#\'\"/\\-:()"


def encode_text(s, mem, version):
    n = 6 if version == 3 else 9
    zc = []
    i = 0
    while i < len(s) and len(zc) < n:
        ch = s[i].lower()
        if ch.isalpha() and ch.isascii():
            zc.append(6 + (ord(ch) - 97 if ch.islower() else ord(ch) - 65))
            i += 1
        elif ch in A2_ENC and A2_ENC.index(ch) >= 2:   # 0='^' escape, 1=\n: unencodable
            if len(zc) + 2 <= n:
                zc.append(5)                 # shift to A2
                zc.append(6 + A2_ENC.index(ch))
                i += 1
            else:
                # no room for the 2-char construction: leave it incomplete
                # (shift only) rather than overflow (ZSpec §3.7)
                zc.append(5)
                break
        else:                            # unmappable -> stop (pad)
            break
    zc += [5] * (n - len(zc))            # pad char 5, incomplete constructions OK
    return pack_zchars(zc)


def pack_zchars(zc):
    words = []
    for k in range(0, len(zc), 3):
        chunk = zc[k:k + 3]
        w = (chunk[0] << 10) | ((chunk[1] if len(chunk) > 1 else 0) << 5) \
            | (chunk[2] if len(chunk) > 2 else 0)
        if k + 3 >= len(zc):
            w |= 0x8000
        words.append(w)
    return b"".join(w.to_bytes(2, "big") for w in words)
