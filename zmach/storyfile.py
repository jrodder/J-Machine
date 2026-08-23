"""Story file loading: header parse, checksum, version gate (spec §3, §9).
See plan 'Verified facts → Header layout' for the offset table."""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .events import StoryFileError

LENGTH_DIVISOR = {3: 2, 5: 4, 8: 8}
SUPPORTED = (3, 5, 8)
MEMORY_SIZE = 524288  # uniform 512 KB image (spec §7)


@dataclass
class StoryHeader:
    version: int
    flags1: int
    release: int
    highmem: int
    pc: int
    dictionary: int
    objects: int
    globals_base: int
    static_base: int
    flags2: int
    serial: str
    fwords: int
    declared_len: int
    checksum: int
    interp_num: int = 0
    interp_ver: int = 0
    screen_h: int = 0
    screen_w: int = 0
    screen_w_units: int = 0
    screen_h_units: int = 0
    font_w_units: int = 0
    font_h_units: int = 0
    def_bg: int = 0
    def_fg: int = 0
    std_rev: int = 0
    alphabet_addr: int = 0
    header_ext_addr: int = 0
    length_divisor: int = 1


class StoryFile:
    def __init__(self, path, data, header):
        self.path = str(path)
        self.name = Path(path).stem
        self.data = data
        self.header = header
        self.sha256 = hashlib.sha256(data).digest()

    @staticmethod
    def load(path, strict=False):
        raw = Path(path).read_bytes()
        if len(raw) < 64:
            raise StoryFileError(f"not a story file (too short): {path}")
        ver = raw[0]
        if ver not in SUPPORTED:
            raise StoryFileError(f"unsupported z-machine version {ver} in {path}")
        d = LENGTH_DIVISOR[ver]
        w = lambda o: (raw[o] << 8) | raw[o + 1]          # big-endian word
        declared = w(0x1a) * d
        if declared > len(raw):
            raise StoryFileError(f"declared length {declared} exceeds file size {len(raw)}")
        data = raw[:declared]
        # ZSpec §15 (verify): checksum = sum of each byte from 0x40 to the
        # declared length, modulo 0x10000. (Verified against zork1/planetfall/risorg.)
        total = sum(data[0x40:declared]) & 0xffff
        chk = w(0x1c)
        if total != chk and strict:
            raise StoryFileError(
                f"checksum mismatch in {path}: computed {total:#06x}, header {chk:#06x}")
        h = StoryHeader(
            version=ver, flags1=raw[1], release=w(2), highmem=w(4), pc=w(6),
            dictionary=w(8), objects=w(0x0a), globals_base=w(0x0c),
            static_base=w(0x0e), flags2=w(0x10),
            serial=data[18:24].decode("ascii", "replace"),
            fwords=w(0x18), declared_len=declared, checksum=chk,
        )
        if ver >= 5:  # fields per plan header table; absent in v3 → defaults
            h.interp_num, h.interp_ver = raw[0x1e], raw[0x1f]
            h.screen_h, h.screen_w = raw[0x20], raw[0x21]
            h.screen_w_units, h.screen_h_units = w(0x22), w(0x24)
            h.font_w_units, h.font_h_units = raw[0x26], raw[0x27]
            h.def_bg, h.def_fg = raw[0x2c], raw[0x2d]
            h.std_rev = raw[0x32]
            h.alphabet_addr, h.header_ext_addr = w(0x34), w(0x36)
        h.length_divisor = d
        return StoryFile(path, data, h)

    def memory_size(self):
        return MEMORY_SIZE