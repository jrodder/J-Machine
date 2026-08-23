# J-Machine Implementation Plan — Z-Machine Interpreter (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A from-scratch Python Z-machine interpreter (v3/v3.3/v5/v8) with a call→batch `Session` API, opaque save/restore, and a CLI — playable to completion on the Infocom catalog and modern Inform 7, verified by conformance suites and byte-differentials against dfrotz.

**Architecture:** Flat `zmach/` package. The VM core never touches terminals or networks: output becomes structured `Event`s, input arrives as whole lines through `Session`. At every API boundary the VM is blocked waiting for input (INV2), so every batch is a complete turn and every save a clean snapshot. The CLI is one thin consumer; the Phase 2 Reticulum host will be another.

**Tech Stack:** Python ≥ 3.10, stdlib only (`struct`, `hashlib`, `dataclasses`, `pathlib`, `unittest`, `subprocess`). Dev-only oracle: dfrotz at `/usr/games/dfrotz` (`-t` plain text, `-s <seed>` RNG seed).

**Spec:** `docs/superpowers/specs/2026-08-23-zmachine-interpreter-design.md` — read it first; this plan implements it and adds verified implementation facts.

## Global Constraints

- Python ≥ 3.10, **stdlib only** — no third-party runtime or dev packages. Tests use stdlib `unittest`; run with `python3 -m unittest discover -s tests -v` from the repo root.
- Supported story versions: **3, 5, 8 only** (v3.3 files are just v3). Versions 4/6/7 → `StoryFileError` with the detected version.
- **INV1** VM never references a terminal/network. **INV2** every public API boundary is a complete turn. **INV3** no ANSI/control sequences in `Text` events (plain text, `\n` only). **INV4** no chunking in the VM.
- No Reticulum import anywhere in Phase 1.
- v8 movie/stream opcodes: no-op stubs; undo opcode: reports unsupported.
- All commits via `git commit`; one commit per task (plus the task's tests).
- Reference material (committed in Task 0, authoritative): `references/zspec10.txt` (ZSpec 1.0 full text), `references/zspec11.txt` (ZSpec 1.1 addendum), `references/dork/machine.ts`, `references/dork/text.ts`, `references/dork/vocab.ts`, `references/dork/io.ts` (conformance-tested TypeScript interpreter — structural reference for anything not fully specified here).
- **When in doubt:** (1) check `references/`, (2) run the dfrotz oracle (one command), (3) ask. Never guess at byte layouts.
- Every opcode group has a gate: a specific suite pass or a byte-exact dfrotz transcript comparison. The gates, not vibes, define done.

## Verified facts (do not re-derive; these were cross-checked against ZSpec 1.0/1.1, dork's conformance-tested code, the actual corpus story files, and dfrotz output)

### Header layout (big-endian 2-byte words unless noted)

| Offset | Meaning | zork1.z3 value |
|---|---|---|
| 0 | version (3, 5, or 8) | 3 |
| 1 | flags1. v3: bit0 byte-swap, bit1 status-type (0=score/moves, 1=time), bit4 no-status, bit5 split-window, bit6 fixed-pitch. v5+: bit0 colour, bit1 picture, bit2 bold, bit3 italic, bit4 fixed-pitch, bit7 timed-input | 0x00 |
| 2-3 | release | 0x0058 (88) |
| 4-5 | high memory base | 0x4e37 |
| 6-7 | initial program counter | 0x4f05 |
| 8-9 | dictionary address | 0x3b21 |
| 10-11 | object table address | 0x02b0 |
| 12-13 | global variables table address (word for variable **16**; 240 words) | 0x2271 |
| 14-15 | static memory base | 0x2e53 |
| 16-17 | flags2 (bit4 = game wants undo; clear on load since unsupported) | 0x0000 |
| 18-23 | serial number, 6 ASCII chars (traditional slot, NOT a spec field) | "840726" |
| 24-25 | abbreviations (fwords) table address | 0x01f0 |
| 26-27 | file length: declared bytes ÷2 (v3) / ÷4 (v5) / ÷8 (v8). **Files may contain padding beyond declared length; exclude it from the checksum** (ZSpec 1.1) | 0xa5c6 → 84876 of 92160 |
| 28-29 | checksum: sum of all 2-byte words over bytes[0:declared_len], **excluding the length word (26-27) and checksum word (28-29)** | 0xa129 |
| 30 | interpreter number (set 0 on load, v5+) | — |
| 31 | interpreter version (set 0 on load, v5+) | — |
| 32 | screen height lines (v5+, set 25) | — |
| 33 | screen width chars (v5+, set 80) | — |
| 34-35 | screen width in units (v5+, set 80) | — |
| 36-37 | screen height in units (v5+, set 25) | — |
| 38-39 | font width/height in units (v5+, set 1/1) | — |
| 44-45 | default bg/fg colour (v5+, set 9/2) | — |
| 50-51 (0x32) | standard revision (write 0x01 0x01) | — |
| 52-53 (0x34) | custom alphabet table address (v5+, 78 bytes) or 0 | 0 |
| 54-55 (0x36) | header extension table address (v5+) or 0; word 3 of that table = custom ZSCII 155..251 translation (97 × 2-byte words) | 0 |

planetfall.z5: declared length 0x855c×4 = 136560 = exact file size. risorg.z8: 0xd86d×8 = 443240 < 443392 (152 bytes padding).

### Packed addresses
v3: `2P`, v5: `4P`, v8: `8P` (P = 16-bit word). High memory extends to end of story file (max 128 KB v3 / 256 KB v5 / 512 KB v8); byte-address opcodes only reach 0..0xFFFF.

### Variables
- 0 = stack pointer: `@load` 0 **pops**; `@store` 0 **pushes**; variable-operand read (e.g. `@load var`) of 0 **pops**; variable-operand write of 0 **overwrites top without pushing** (dork `fetch`/`store`/`xfetch`/`xstore`).
- 1..15 = local variables of the current frame (arg k is stored in local slot k; extra args beyond the slot count are discarded — ZSpec §6.4.4.1).
- 16..255 = globals at `globals_base + 2*(n-16)` (v8: `+ 8*(n-16)`).
- v3 globals used by the status line: **16** = current location object, **17** = moves, **18** = score. v5+: score = 6, turns = 7 (the I6 library draws v5 status lines itself with `print`; the interpreter's `show_status` in v5 uses the same 80-column format with location from global 16 — the planetfall differential test pins this).

### Routine headers / frames
- v3: byte 0 = default local values (1 byte each), byte 1 = local count. v5+: byte 0 = local count, byte 1 = arg count, byte 2 = flags (bit 7 "no entry" — not needed for text mode; ignore).
- Locals live **in dynamic memory** (required for save images): frame region grows upward from `globals_base + 480` (v8: `+ 1920`). A call allocates `n_locals` words (v8: 8 bytes each), initializes (v3: header defaults; v5+: 0), then writes args into slots 1..n_args.
- Call chain (Python list of `{return_pc, locals_base, n_locals, n_args}`) + operand stack pointer are VM state **outside** story memory — they live in the save format's VM-state block.
- Operand stack grows **downward** from `0xFFFE` (v3/v5) or `0x3FFFE` (v8); entries are 2-byte words.
- Max call depth: 63 (v3/v5) — beyond it, spec error 14 (stack overflow).

### RNG (verified: dork + dfrotz `-s` + spec §2.4)
State: 32-bit seed. `@random n`:
- `n > 0`: `seed = (1664525*seed + 1013904223) & 0xFFFFFFFF`; result = `((seed * n) >> 32) + 1` (integer math — always 1..n; do NOT use the float form).
- `n < 0`: `seed = (-n) & 0xFFFFFFFF`, result 0.
- `n == 0`: `seed = int.from_bytes(os.urandom(4), 'big')`, result 0.
- Initial seed: `Session.load(seed=...)` or CLI `--seed`; default: `os.urandom` (Zork I does not reseed during early play, so seeded differentials work — dork byte-compared a 365-command Zork I walkthrough against dfrotz with fixed seeds).

### Text decode (verified: dork `decodeText`, conformance-tested)
Words are 16-bit big-endian: **c1 = (w>>10)&31, c2 = (w>>5)&31, c3 = w&31, end = w&0x8000** (top char first — the reverse of intuition). Per z-char `v` with temp shift `ts`, permanent shift `ps`:
- `v == 0` → space (ZSCII 32)
- `v < 4` → abbreviation start: remember `y = (v-1)*32`; next z-char `v'` → entry `y+v'` of the fwords table: `mem.getw(fwords + (y+v')*2) * 2` = byte address of the string to decode (no abbreviations inside)
- `v in (4,5)` → shift: if `ts == 0` set `ts = v-3`; if `ts == v-3` set `ps = ts`; else `ps = ts = 0`
- `v == 6 and ts == 2` → 10-bit ZSCII start: next two z-chars give top/bottom 5 bits
- else → alphabet table `[ts*26 + v - 6]`, then `ts = ps`

Default tables (ZSCII output codes): A0: z-char 0 = space, 1-3 = abbreviations, 4/5 = shift, **6-31 = `a`-`z`** (digits are NOT in A0); A1: 6-31 = `A`-`Z`; A2: 6 = escape (10-bit ZSCII start), 7 = ZSCII 13 (newline), **8-17 = `0`-`9`**, 18=`.`, 19=`,`, 20=`!`, 21=`?`, 22=`_`, 23=`#`, 24=`'` (0x27), 25=`"` (0x22), 26=`/`, 27=`\`, 28=`-`, 29=`:`, 30=`(`, 31=`)`. (Use dork's `ALPHABET` string constant verbatim: `'abcdefghijklmnopqrstuvwxyz' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' + '*\n0123456789.,!?_#\'"/\\-:()'` indexed by `ts*26 + v - 6`.)
ZSCII→char: 13 → `\n`, 0 → `''`, 1..154 → `chr(code)`, 155..251 → custom table (header ext word 3) else default table (dork `DEFAULT_ZSCII_EXTRA`, 69 chars for 155..223). v5+ custom alphabet: 78 bytes at header 0x34 (3×26 ZSCII values for A0/A1/A2 z-chars 6..31; A2 z-chars 6,7 keep escape/newline meaning).

### Dictionary (verified against zork1.z3 live: dict 0x3b21, n_sep=3, entry_len=7, count=519)
Header at dictionary address: `n` bytes (n = first byte) of word-separator ZSCII codes, 1 byte entry length, 1 word count. Entries follow, **sorted** by encoded text as a big integer (binary search). v3: 4 text bytes (6 z-chars, pad 5) + (entry_len−4) data bytes. v5+: 6 text bytes (9 z-chars) + (entry_len−6) data bytes.
Encoding typed text for lookup: lowercase, no abbreviations, pad 5, 6 z-chars (v3) or 9 (v5+), multi-zchar constructions left incomplete if no room (ZSpec §3.7; e.g. "i" → `$94a5` in v3 form).

### v3 status line (verified: dfrotz byte capture + dork)
Format, 80 columns + `\n`: `f" {name}".ljust(56) + f"Score: {score}" + " "*8 + f"Moves: {moves}"`, clipped to 80 cols (name field minimum 1 trailing space). Emitted: by `show_status`, and automatically before `read`/`read_char` in v3 (v3 games rely on it). dfrotz line 3 of Zork I is exactly: `" West of House"` + 42 spaces + `"Score: 0"` + 8 spaces + `"Moves: 0"`.

### Save format
ZMSAVE v1 — see spec §7 (fixed offsets, big-endian, 512 KB uniform memory image, VM-state block for sp/error/frames/catches, RNG seed slot, magic + story-hash + trailer-hash validation).

### Opcode encoding (ZSpec §4)
Top two opcode bits: `11` = VAR form (op number in low 5 bits; bit 5: 0→2OP, 1→VAR), `10` = short form (op number low 4 bits; bits 4-5 = operand type, `$11`→0OP), `01` = long 2OP (op number low 5 bits; bit 6/5 = operand types), `00` = illegal (bad opcode). Byte 190 (0xBE) as first opcode byte in v5+ = **extended** form: next byte = extended op number, VAR operands. Operand type nibbles: 0 = small constant (1 byte, signed), 1 = variable (1 byte), 2 = large constant (2 bytes), 3 = address constant (2 bytes → packed), 4/5 = omitted. **Operands evaluate left-to-right** (ZSpec 1.1). Branch opcodes: `pc = pc_after_instruction + signed_offset − 2` (2OP) / `− 1` style per ZSpec §4 — use the ZSpec formula: branch target = (address after instruction) + offset − 2 for 2OP forms; verify per-opcode against dork.

---

### Task 0: Repo skeleton, references, corpus, dfrotz oracle

**Files:**
- Create: `references/` (zspec10.txt, zspec11.txt, dork/*.ts), `tests/corpus/` (story + conformance files), `zmach/__init__.py`, `tests/__init__.py`, `tests/util.py`
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Consumes: nothing
- Produces: `tests/util.py` — `dfrotz_transcript(story: Path, lines: list[str], seed: int | None = None) -> str` (subprocess: `dfrotz -t [-s seed] story` with lines + `\x04` on stdin, decoded stdout; the differential oracle used by every later task) and `norm(s: str) -> str` (collapse runs of whitespace, strip lines, drop the leading dfrotz banner lines "Using normal formatting." / "Loading …").

- [ ] **Step 1: Copy references**

```bash
mkdir -p references/dork
cp /tmp/pi-web-pdf/the-z-machine-standards-document.md references/zspec10.txt
curl -sL https://inform-fiction.org/zmachine/standards/ZSpec11-latest.txt -o references/zspec11.txt
cp /tmp/dork-machine.ts references/dork/machine.ts
cp /tmp/dork-text.ts references/dork/text.ts
cp /tmp/dork-vocab.ts references/dork/vocab.ts
cp /tmp/dork-io.ts references/dork/io.ts
```

(If the `/tmp` copies are gone, re-fetch: `https://raw.githubusercontent.com/ntoskrnlexe/dork/master/src/zmachine/{machine,text,vocab,io}.ts`; ZSpec 1.0 from `https://wolldingwacht.de/if/z-spec10.pdf` via a PDF-to-text pass, or any faithful text extraction — the content must match the 182-page standard.)

- [ ] **Step 2: Download corpus + conformance files**

```bash
mkdir -p tests/corpus
B=https://raw.githubusercontent.com/jeffnyman/zifmia/master
for f in zorks/zork1.z3 zorks/minizork.z3 infocom/planetfall.z5 games/risorg.z8; do
  curl -sL "$B/$f" -o "tests/corpus/$(basename $f)"
done
# zifmia tester files live flat under testers/ (no per-suite subdirs)
for f in czech/czech.z5 czech/czech.out5 strictz/strictz.z5 crashme/crashme.z5 \
         unicode/unicode.z5 random/random.z5; do
  curl -sL "$B/testers/$(basename $f)" -o "tests/corpus/$(basename $f)"
done
# optional fifth corpus file (v3 flagship in v5): NOT available — the-infocom-files/zork1
# hosts sources only; the v5 flagship differential uses planetfall (v5) + Zork I (v3)
```

Expected sizes: zork1.z3 92160, minizork.z3 52216, planetfall.z5 136560, risorg.z8 443392, crashme.z5 37376, czech.z5 13312, czech.out5 2319, random.z5 5632, strictz.z5 4096, unicode.z5 4608. `du -b` must match or the fetch is corrupt.

- [ ] **Step 3: Package + util**

```python
# zmach/__init__.py
__version__ = "0.1.0"
```

```python
# tests/util.py
import subprocess
from pathlib import Path

BANNER_PREFIXES = ("Using normal formatting.", "Loading ", "dfrotz ")

def dfrotz_transcript(story, lines, seed=None):
    cmd = ["/usr/games/dfrotz", "-t"]
    if seed is not None:
        cmd += ["-s", str(seed)]
    cmd.append(str(story))
    p = subprocess.run(cmd, input="".join(l + "\n" for l in lines) + "\x04",
                       capture_output=True, text=True, timeout=120)
    return p.stdout

def norm(s):
    out = []
    for line in s.splitlines():
        if any(line.startswith(p) for p in BANNER_PREFIXES):
            continue
        line = " ".join(line.split())
        if line:
            out.append(line)
    return "\n".join(out)
```

- [ ] **Step 4: Scaffold test (oracle smoke test — fails if dfrotz or corpus is broken)**

```python
# tests/test_scaffold.py
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
```

- [ ] **Step 5: Run, verify PASS, commit**

Run: `python3 -m unittest tests.test_scaffold -v` → PASS.
`git add -A && git commit -m "chore: skeleton, references, corpus, dfrotz oracle"`

---

### Task 1: events.py

**Files:**
- Create: `zmach/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Event` (base), `Text(data: str)`, `Prompt(hint: str | None = None)`, `Error(message: str)`, `EndOfGame(status: int)`; `StoryFileError(Exception)`, `SaveFileError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_events.py
import unittest
from zmach.events import (Text, Prompt, Error, EndOfGame, Event,
                          StoryFileError, SaveFileError)

class TestEvents(unittest.TestCase):
    def test_events_are_events(self):
        for e in (Text("x"), Prompt(), Error("y"), EndOfGame(0)):
            self.assertIsInstance(e, Event)

    def test_fields(self):
        self.assertEqual(Text("hi").data, "hi")
        self.assertIsNone(Prompt().hint)
        self.assertEqual(EndOfGame(3).status, 3)

    def test_exceptions(self):
        self.assertRaises(StoryFileError, StoryFileError, "bad story")
        self.assertRaises(SaveFileError, SaveFileError, "bad save")
```

- [ ] **Step 2: Run, verify FAIL** (`ModuleNotFoundError: zmach.events`)

Run: `python3 -m unittest tests.test_events -v`

- [ ] **Step 3: Implement**

```python
# zmach/events.py
"""Structured events emitted by the Session (spec §5). INV3: Text.data is
plain text — no ANSI, no control sequences except \\n."""
from dataclasses import dataclass


class Event:
    pass


@dataclass
class Text(Event):
    data: str


@dataclass
class Prompt(Event):
    hint: str | None = None


@dataclass
class Error(Event):
    message: str


@dataclass
class EndOfGame(Event):
    status: int


class StoryFileError(Exception):
    pass


class SaveFileError(Exception):
    pass
```

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_events -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: event types and exceptions"`

---

### Task 2: storyfile.py (header parse + checksum + version gate)

**Files:**
- Create: `zmach/storyfile.py`
- Test: `tests/test_storyfile.py`

**Interfaces:**
- Consumes: `StoryFileError` (Task 1)
- Produces:
  - `@dataclass StoryHeader`: `version, flags1, release, highmem, pc, dictionary, objects, globals_base, static_base, flags2, serial, fwords, declared_len, checksum, interp_num, interp_ver, screen_h, screen_w, screen_w_units, screen_h_units, font_w_units, font_h_units, def_bg, def_fg, std_rev, alphabet_addr, header_ext_addr, length_divisor` (int/str; v3 fields that don't exist in the header are 0).
  - `StoryFile.load(path, strict=False) -> "StoryFile"` with attributes `data: bytes` (file bytes up to declared_len), `header: StoryHeader`, `sha256: bytes`, `path: str`, `name: str` (filename stem). Raises `StoryFileError` for version not in {3,5,8} (message includes detected version) and, when `strict`, for checksum mismatch.
  - `StoryFile.memory_size() -> int` = 524288 (uniform 512 KB image, spec §7).

- [ ] **Step 1: Write the failing test** (values verified from the corpus hex dumps)

```python
# tests/test_storyfile.py
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
        # checksum rule: sum of words over declared bytes, minus len+checksum words
        total = 0
        for i in range(0, h.declared_len, 2):
            if i in (0x1a, 0x1c):
                continue
            total = (total + (f.data[i] | f.data[i+1] << 8)) & 0xffff
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
        with self.assertRaises(StoryFileError) as cm:
            StoryFile.load(p)
        self.assertIn("6", str(cm.exception))
        p.unlink()

    def test_checksum_mismatch_warns_not_raises(self):
        f = StoryFile.load(C / "zork1.z3", strict=False)   # must not raise
        self.assertEqual(f.sha256, __import__("hashlib").sha256(f.data).digest())
```

- [ ] **Step 2: Run, verify FAIL** (`ModuleNotFoundError: zmach.storyfile`)

Run: `python3 -m unittest tests.test_storyfile -v`

- [ ] **Step 3: Implement**

```python
# zmach/storyfile.py
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
        w = lambda o: raw[o] | raw[o + 1] << 8          # big-endian word
        declared = w(0x1a) * d
        if declared > len(raw):
            raise StoryFileError(f"declared length {declared} exceeds file size {len(raw)}")
        data = raw[:declared]
        # checksum over declared bytes, excluding len (0x1a) and checksum (0x1c) words
        total = 0
        for i in range(0, declared, 2):
            if i in (0x1a, 0x1c):
                continue
            total = (total + (data[i] | data[i + 1] << 8)) & 0xffff
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
```

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_storyfile -v` — all 5 tests pass (the checksum-rule assertion is the real proof of the parsing).

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: story file loading, header parse, checksum, version gate"`

---

### Task 3: memory.py

**Files:**
- Create: `zmach/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `StoryFile` (Task 2)
- Produces: `Memory(story: StoryFile)`:
  - `mem: bytearray` (524288 bytes; story bytes copied to 0..declared_len)
  - `byte_swapped: bool` (header flags1 bit 0 — v1-4 only; v5/v8 always False)
  - `getb(addr) -> int`, `putb(addr, val)` (OOB: read 0, write ignored — spec §9)
  - `getw(addr) -> int`, `putw(addr, val)` (2-byte big-endian, or little-endian when `byte_swapped`; OOB rule per 2-byte word: read 0, write ignored)
  - `getu64(addr) -> int`, `putu64(addr, val)` (v8 64-bit, 4 words big-endian)
  - `width: int` (2 or 8 — local/global word width: v3/v5 2, v8 8)
  - `stack_top: int` (0xFFFE v3/v5, 0x3FFFE v8)
  - `reset()` — re-copy story bytes (used by `@restart`)

- [ ] **Step 1: Write the failing test**

```python
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
        self.assertEqual(self.m.getw(0x12) & 0xffff, ord("8") | ord("4") << 8)

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
        self.m.reset()
        self.assertEqual(self.m.getw(0x100), 0)
```

- [ ] **Step 2: Run, verify FAIL** (`ModuleNotFoundError: zmach.memory`)

- [ ] **Step 3: Implement**

```python
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
        self.width = 8 if story.header.version == 8 else 2
        self.stack_top = 0x3FFFE if story.header.version == 8 else 0xFFFE

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
        hi, lo = self.getb(a), self.getb(a + 1)
        if self.byte_swapped:
            return lo | hi << 8
        return hi << 8 | lo

    def putw(self, a, v):
        v &= 0xFFFF
        if self.byte_swapped:
            lo, hi = v & 0xFF, v >> 8
        else:
            hi, lo = v >> 8, v & 0xFF
        self.putb(a, hi)
        self.putb(a + 1, lo)

    def getu64(self, a):
        v = 0
        for i in range(4):
            v = (v << 16) | self.getw(a + i * 2)
        return v

    def putu64(self, a, v):
        for i in range(3, -1, -1):
            self.putw(a + i * 2, (v >> (16 * i)) & 0xFFFF)
```

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_memory -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: memory address space with OOB policy and v8 64-bit access"`

---

### Task 4: strings.py (Z-encoded text decode/encode)

**Files:**
- Create: `zmach/strings.py`
- Test: `tests/test_strings.py`

**Interfaces:**
- Consumes: `Memory` (Task 3), `StoryFile` (Task 2)
- Produces:
  - `decode_text(mem, fwords, addr, zscii_extra, alphabet, wide=False) -> (str, int)` — returns (text, address past last word). Exact algorithm in the plan's Verified facts section (bit order, shift machine, abbreviations, 10-bit ZSCII, custom tables).
  - `char_to_zscii(ch, zscii_extra) -> int` and `zscii_to_char(code, zscii_extra) -> str` (13→`\n`, 0→`''`, 155..251 via table).
  - `encode_text(s, mem, version) -> bytes` — dictionary-form encoding (lowercase, pad 5, 6 z-chars v3 / 9 v5+, per ZSpec §3.7).
  - `read_custom_tables(story) -> (zscii_extra: str, alphabet: bytes | None)` — header ext word 3 + 0x34 table (dork `readUnicodeTable`/`readAlphabetTable` logic).
  - `WideStrings` — for v8: `decode_wide(mem, addr) -> (str, int)` (16-bit big-endian ZSCII words until 0x0000; 10→13 normalization).

- [ ] **Step 1: Write the failing test** (round-trips + verified Zork I dictionary spot checks)

```python
# tests/test_strings.py
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
        for s in ("look", "open mailbox", "a", "zz", "go north"):
            b = encode_text(s, m, 5)
            off = 0x8000
            m.mem[off:off + len(b)] = b
            text, _ = decode_text(m, sf.header.fwords, off, extra, alpha)
            self.assertEqual(text, s)

    def test_dictionary_lookup_zork1(self):
        sf, m = load("zork1.z3")
        extra, _ = read_custom_tables(sf)
        b = encode_text("open", m, 3)
        # binary search zork1's dictionary (n_sep=3, entry_len=7, count=519)
        d = sf.header.dictionary
        n_sep, entry_len = m.getb(d), m.getb(d + 1 + n_sep)
        count = m.getw(d + 2 + n_sep)
        self.assertEqual((n_sep, entry_len, count), (3, 7, 519))
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
```

- [ ] **Step 2: Run, verify FAIL** (`ModuleNotFoundError: zmach.strings`)

- [ ] **Step 3: Implement**

```python
# zmach/strings.py
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
            if taddr:
                n = data[taddr]
                out = []
                for i in range(min(n, _EXTRA_MAX - _EXTRA_MIN + 1)):
                    out.append(chr(_w(data, taddr + 1 + i * 2)))
                extra = "".join(out).ljust(len(DEFAULT_ZSCII_EXTRA), "\x00")
    alpha = data[h.alphabet_addr:h.alphabet_addr + 78] if h.alphabet_addr else None
    return extra, (alpha if alpha is None or len(alpha) == 78 else None)


def _w(data, a):
    return data[a] | data[a + 1] << 8


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
    if _EXTRA_MIN <= o <= _EXTRA_MAX:
        i = o - _EXTRA_MIN
        if i < len(extra) and extra[i] == ch:
            return o
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
        elif ch in A2_ENC and ch not in "^\\n":
            zc.append(5)                 # shift to A2
            zc.append(6 + A2_ENC.index(ch))
            i += 1
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
```

- [ ] **Step 4: Run, verify PASS** (the dictionary lookup test is the real proof — it uses Zork I's actual table)

Run: `python3 -m unittest tests.test_strings -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: Z-encoded text decode/encode with custom tables and v8 wide strings"`

---

### Task 5: vm.py core + core opcodes (decode, dispatch, arithmetic, calls, RNG, print)

**Files:**
- Create: `zmach/vm.py`, `zmach/opcodes.py`
- Test: `tests/test_vm_core.py`, `tests/test_vm_planetfall.py`

**Interfaces:**
- Consumes: `StoryFile`, `Memory`, `decode_text`/`read_custom_tables`, `Event`s
- Produces:
  - `VM(story, seed=None)` with:
    - `events: list[Event]` (accumulated; drained by `Session`)
    - `needs_input: bool` — True when the input buffer is empty and the VM wants a char (set by `io` ops in Task 6; Task 5 treats it as always False until then)
    - `done: bool`, `done_status: int`
    - `run_until_input() -> None` — execute opcodes until `needs_input`, `done`, or an unrecoverable error
    - `seed: int` (RNG), `pc`, `sp`, `frames: list`, `catch_stack: list[int]`, `error: int`
    - `snapshot() -> dict` / `restore(dict)` (Task 10 builds on these)
  - Opcodes implemented in this task (v5 forms; v3 variants in Task 8, v8 in Task 9):
    - decode/dispatch (all forms incl. extended 0xBE prefix)
    - `add sub mul mod` (C-division: `-11/2 = -5`, `-13 % 5 = -3`), `or and art_shift log_shift`
    - `push pull ret ret_popped`
    - `store load storew storeb loadw loadb` (arrays = word tables; v3 array semantics identical for 16-bit)
    - `print print_char print_num print_addr print_paddr print_ret` (→ `Text` events; `print_ret` prints + returns the string)
    - `je jl jg jz dec_chk inc_chk test` (branch forms; offset = signed, target = pc_after + offset − 2 for 2OP — verify each form's exact formula against `references/zspec10.txt` §4 and dork; the CZECH gate catches errors)
    - `call call_1s call_1n call_2s call_2n call_vn call_vs call_vs2 call_vn2` (routine headers/frames per Verified facts)
    - `random` (Verified facts RNG)
    - `quit` (→ `EndOfGame`; status = global 1 (v3) / 0 placeholder for v5+ — not observable in corpus)
    - `nop restart` (restart: memory reset, pc = header pc, empty frames, RNG reseeded from `VM.seed0`, flags2 preserved)
    - `catch throw` (v5+): catch pushes `len(frames)`; `throw value token`: if `token` in the active chain, unwind frames to that depth, push `value` onto operand stack, resume; else error 581 (uncaught throw)
    - error handling: `raise_err(n)` → if a catch frame exists: unwind + push error value; else append `Error` event + stop (unrecoverable). Bad opcode / bad operand / OOB operand → error numbers per ZSpec Appendix A (read `references/zspec10.txt` "Appendix A" + dork machine.ts bad-opcode section before implementing; crashme gates it)
    - `new_line` → `Text("\n")`

- [ ] **Step 1: Write the failing C-division and dispatch unit tests first**

```python
# tests/test_vm_core.py
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
```

- [ ] **Step 2: Run, verify FAIL** (`ModuleNotFoundError: zmach.vm`)

- [ ] **Step 3: Implement `cdiv`/`cmod` + VM core + opcode decode/dispatch + the Task 5 opcode set**

Core semantics to encode exactly (see Verified facts):
- `cdiv(a,b)`: `q = abs(a)//abs(b); q if (a<0)==(b<0) else -q`; `cmod(a,b) = a - cdiv(a,b)*b`. Division by zero → `raise_err` (error 8).
- VM state: `pc = header.pc`, `sp = memory.stack_top`, `frames = []`, `catch_stack = []`, `error = 0`.
- Variable read `fetch(x)`: x==0 → pop operand stack; 1..15 → frame local slot x−1 (current frame); x≥16 → global. Variable write `xstore(x,y)` (variable-operand form): x==0 → overwrite stack top; else same addressing. `@store`/`@load` with a variable *operand value* 0: load→pop, store→push.
- Operands evaluate **left-to-right** (ZSpec 1.1). Operand types: 0 small const (1 byte, sign-extended), 1 variable, 2 large const (2 bytes), 3 packed address const (2 bytes → packed), 4/5 omitted.
- Routine call: read header (v5+: byte0 = n_locals, byte1 = n_args), allocate `n_locals` words at `frame_alloc` (grows up from `globals_base + 480`), zero-init, write args into slots 1..n_args (extras discarded), push frame {return_pc=pc_after, locals_base, n_locals, n_args}, pc = routine addr. `ret`: pop frame, `frame_alloc` back, pc = return_pc. Depth > 63 → error 14.
- `print_paddr`: packed→byte addr via version multiplier (2/4/8), `decode_text`, emit `Text`. v8: if packed & 0x8000 → wide string.
- `print_num`: signed decimal, str() (v5+); v8: 64-bit signed.
- RNG per Verified facts (integer `((seed*n) >> 32) + 1`).
- `run_until_input`: `while not (vm.done or vm.needs_input): decode one opcode; dispatch`. Opcode count guard: 10_000_000 → stop + `Error` (runaway protection, not a spec feature).

- [ ] **Step 4: Planetfall opening differential (the M0→M1 gate)**

```python
# tests/test_vm_planetfall.py
import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text

C = Path(__file__).parent / "corpus"
SEED = 10

class TestPlanetfallOpening(unittest.TestCase):
    def test_opening_matches_dfrotz(self):
        sf = StoryFile.load(C / "planetfall.z5")
        vm = VM(sf, seed=SEED)
        vm.run_until_input()
        ours = "".join(e.data for e in vm.events if isinstance(e, Text))
        ref = norm(dfrotz_transcript(C / "planetfall.z5", [], seed=SEED))
        # opening must match through the first prompt (trim at last '>')
        self.assertIn("PLANETFALL", ours.upper())
        # line-by-line comparison of the opening block
        our_lines = [l for l in (x.strip() for x in ours.split("\n")) if l]
        ref_lines = [l for l in ref.split("\n") if l and l != ">"]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 8, "opening too short — opcode coverage bug")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")
```

Run: `python3 -m unittest tests.test_vm_core tests.test_vm_planetfall -v` → PASS (this gate exercises decode, dispatch, calls, print, RNG, globals, and the planetfall library's opening routine).

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: VM core, opcode decode/dispatch, arithmetic, calls, RNG, print; planetfall opening matches dfrotz"`

---

### Task 6: io.py — input buffer, read opcodes, status line

**Files:**
- Create: `zmach/io.py`
- Modify: `zmach/vm.py` (wire `needs_input`, `feed()`, `read`/`read_char`/`read_string`/`show_status`/`output_stream`/`input_stream`/`set_text_style`/`set_font`/`buffer_mode`/`more`/`split_window`/`set_window`/`erase_window`/`erase_line`/`set_cursor`/`get_cursor` — the screen opcodes are no-ops except `show_status` and `output_stream`/`input_stream` (memory tables), per spec §6 text mode)
- Test: `tests/test_io.py`, `tests/test_vm_io_differential.py`

**Interfaces:**
- Consumes: `VM`, `Memory`, `decode_text`, `char_to_zscii`
- Produces: `InputBuffer` with `feed(line: str)`, `get() -> int` (0 when empty), `empty: bool`; VM gains:
  - `vm.feed(line)` — encodes each char via `char_to_zscii` + appends 13
  - `read_char` (v5 VAR:246 / v3 1OP form): returns next char code; **if buffer empty: `needs_input = True`, stop** (opcode re-executes after `feed` — the VM must resume the interrupted opcode, not re-dispatch it: implement by keeping `pc` at the opcode start and a `pending` flag; on next `run_until_input`, continue from `pending`)
  - `read` (VAR:228): v3 `sread text parse` / v5 `aread text parse time routine → (result)`. Text buffer: v3: byte0 = max−1 preset by game, chars at byte1.. with 0 terminator; v5: byte0 = max preset, byte1 = count written, chars at byte2..; leftover rule (v5: if byte1 > 0 on entry, append after existing chars — our buffered whole-line model satisfies this). Consume until 13 (v5: or any char in the terminating-chars table at header 0x2E). **Input-starved mid-line: same pending mechanism.** Result: 13 normal (v5 only), 0 interrupted (never — timers unsupported; note in code). Parse table (if parse ≠ 0): v5: byte0 = max words preset, byte1 = word count, from byte2: 4B/word {dict_addr u16 (0 if unknown), offset u8, length u8}; v3: word0 = count, then 5B/word {dict_addr u16, offset u16, length u8}. Word splitting per ZSpec §13.6.1 (spaces ignored, separators are their own words).
  - `show_status`: v3: auto before `read`/`read_char` + on opcode: `f" {name}".ljust(56) + f"Score: {score}" + " "*8 + f"Moves: {moves}"` + `\n`, clipped to 80 cols (name = short name of object global 16, i.e. `decode_text` at `get_prop_addr(obj16, 1) + 1`; score = g18, moves = g17 — Verified facts). v5: same format with location = object g16, score = g6, turns = g7; planetfall differential pins exactness.
  - `output_stream` (VAR:243): stream 1 → normal Text; streams 2/3 (v5+) → append ZSCII bytes into the memory table operand (table format: first word = word count, then text words — ZSpec §7.1.2); stream 4 = 2+3. `input_stream`: no-op (stream 0 only; others error 587 per spec §10 — verify against dork).
  - Screen opcodes (`set_text_style`, `set_font`, `buffer_mode`, `more`, `split_window`, `set_window`, `erase_window`, `erase_line`, `set_cursor`, `get_cursor`): no-ops; `get_cursor` writes (0,0) words; `set_font` returns 0.
  - `verify` (0OP:189, v3): branch to header.pc (restart-style) — ZSpec §15.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_io.py
import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text, Prompt

C = Path(__file__).parent / "corpus"
SEED = 10

def transcript(sf_path, lines, seed=SEED):
    sf = StoryFile.load(sf_path)
    vm = VM(sf, seed=seed)
    out, evs = [], vm.events
    def drain():
        out += [e.data for e in evs if isinstance(e, Text)]
        evs.clear()
    vm.run_until_input(); drain()
    for line in lines:
        vm.feed(line)
        vm.run_until_input(); drain()
    return "".join(out)

class TestIoDifferential(unittest.TestCase):
    def test_planetfall_commands(self):
        ours = transcript(C / "planetfall.z5", ["look", "north", "examine sky", "quit"])
        ref = norm(dfrotz_transcript(C / "planetfall.z5", ["look", "north", "examine sky", "quit"], seed=SEED))
        our_lines = [l for l in (x.strip() for x in ours.split("\n")) if l and l != ">"]
        ref_lines = [l for l in ref.split("\n") if l and l != ">"]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 12, f"transcript too short: {m} lines — read/parse bug")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")
```

- [ ] **Step 2: Run, verify FAIL** (Planetfall responds to no input; `read` unimplemented)

- [ ] **Step 3: Implement io.py + VM wiring** (pending-opcode resume is the critical piece:

```python
# in VM.run_until_input
def run_until_input(self):
    guard = 0
    while not (self.done or self.needs_input):
        guard += 1
        if guard > 10_000_000:
            self.events.append(Error("instruction limit exceeded"))
            self.done = True
            break
        if self.pending is not None:
            op, self.pending = self.pending, None   # resume interrupted read
        else:
            op = self.decode()
        self.execute(op)
```

`read_char`/`read` set `self.pending = op` (the decoded opcode re-runnable at the same pc) when the buffer is empty.)

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_io tests.test_vm_io_differential -v` — planetfall must respond to all four commands byte-identically to dfrotz (read + parse table + status + object name printing).

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: input buffer, read/read_char/read_string, parse tables, status line, output streams"`

---

### Task 7: objects, arrays, properties

**Files:**
- Modify: `zmach/opcodes.py` (add object/property/array opcodes)
- Test: `tests/test_objects.py`

**Interfaces:**
- Consumes: `VM` (Task 5), `Memory`
- Produces opcodes: `insert_obj remove_obj move_obj get_parent get_sibling get_child get_prop get_prop_addr get_next_prop put_prop test_attr set_attr clear_attr jin` + object/property helpers:
  - `obj_entry(obj) -> int` (byte offset in object table: v3: `objects + (obj-1)*9`, v5+: `objects + (obj-1)*14`; attrs: v3 4 bytes/32 attrs, v5+ 6 bytes/48 attrs; parent/sibling/child words per plan Verified facts — v3: 32-bit attrs then 3-byte parent, sibling, child (packed!), 2-byte property addr — **v3 parent/sibling/child are packed addresses** (3 bytes each, 0..0x7F range: value 0 = none, 0x40+n = object n); v5+: 3 words)
  - `propfind(obj, prop) -> (addr, length, size_byte)`: walk the object's property list (descending property numbers) from the property-table address; property defaults table (31 words v3 at `objects`, 63 words v5+) for missing properties; property block formats: v3: 1-byte size `(len-1)<<5 | propnum` (len 1..8); v5+: 1-2 byte size (top bit → 2nd byte: low 6 bits = len, 0 → 64; bit 6 → len 2, else 1; prop number in low 6 bits of first byte — dork `propLayout` lines 303-330)
  - `get_prop`: 1-byte props zero-extended, 2-byte signed; `get_prop_addr`: v5+ returns addr (or 0 for default-only properties); `get_next_prop` → (propnum, value) of next-lower property or (0, default)
  - array access for `loadw storeb loadb storew` already in Task 5 (arrays are word/byte tables; v8: `load_array`/`store_array` in Task 9)

- [ ] **Step 1: Write the failing test** (Zork I object facts from ZSpec §Overview: mailbox obj 239: attrs 30,34; parent 68; sibling 127; child 80)

```python
# tests/test_objects.py
import unittest
from pathlib import Path
from zmach.storyfile import StoryFile
from zmach.vm import VM

C = Path(__file__).parent / "corpus"

class TestObjects(unittest.TestCase):
    def test_zork1_mailbox(self):
        vm = VM(StoryFile.load(C / "zork1.z3"), seed=10)
        m = vm.memory
        e = vm.obj_entry(239)
        self.assertEqual(vm.get_parent(239), 68)
        self.assertEqual(vm.get_sibling(239), 127)
        self.assertEqual(vm.get_child(239), 80)
        self.assertTrue(vm.test_attr(239, 30))
        self.assertTrue(vm.test_attr(239, 34))
        self.assertFalse(vm.test_attr(239, 0))
        # mailbox has properties 44-49 (ZSpec overview listing)
        self.assertGreater(vm.get_prop(239, 44), 0)
```

- [ ] **Step 2: Run, verify FAIL** (methods missing)

- [ ] **Step 3: Implement helpers + opcodes** (object table layout per Verified facts + ZSpec §12; v3 packed parent/sibling/child: stored value 0 = nothing, 0x40+n = object n — verify the packing against dork machine.ts before writing, and the test above proves it on Zork I's real table)

- [ ] **Step 4: Run, verify PASS** (Zork I's real object tree validates the layout end-to-end)

Run: `python3 -m unittest tests.test_objects -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: object tree, property tables, attribute and array opcodes"`

---

### Task 8: v3 compatibility mode (Zork I playable)

**Files:**
- Modify: `zmach/vm.py`, `zmach/opcodes.py`, `zmach/io.py` (v3 variants)
- Test: `tests/test_vm_v3.py`

**Interfaces:**
- Consumes: Tasks 5-7
- Produces: version-switched behavior (VM checks `story.header.version`):
  - packed multiplier 2, 16-bit arithmetic (all results masked sign-extended to 16 bits at every arithmetic boundary — ZSpec 1.1: intermediates may be 17 bits only where dork notes it; match dork's `s16` truncation exactly: `((x & 0xffff) << 16) >> 16`)
  - v3 routine headers (default locals from header byte 0, count byte 1), v3 call (0-3 args via VAR:224)
  - v3 dictionary encoding (6 z-chars) for parse tables; v3 `read` parse format (word0 = count, 5B/word)
  - v3 `save`/`restore` (0OP branch form, 0x05/0x06): invoke the VM's save/restore handlers (Task 10 installs them; until then: branch taken on success — wire a stub handler that writes to a temp file and returns success)
  - v3 `quit`: EndOfGame status = global 1
  - v3 `verify`, v3 flags1 (bit4 no-status → suppress status line; bit1 status type 1 = time line `HH:MM` from g17/g18 — corpus uses type 0; implement both per Appendix B)
  - v3 errors: no catch/throw (opcode 0x09 = `pop` in v3! catch/throw only v5+)
  - `print`/`print_ret` v3: literal operand form identical (VAR 2OP)
  - v3 `random`: same algorithm (dork is version-agnostic here)
  - v3 `split_window`/`set_window`: no-ops (text mode)

- [ ] **Step 1: Write the failing differential test** (Zork I, v3, real file)

```python
# tests/test_vm_v3.py
import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text

C = Path(__file__).parent / "corpus"
SEED = 10
LINES = ["look", "open mailbox", "take leaflet", "read leaflet",
         "west", "north", "turn on lamp", "east", "up", "quit"]

class TestZorkI(unittest.TestCase):
    def test_first_ten_commands(self):
        sf = StoryFile.load(C / "zork1.z3")
        vm = VM(sf, seed=SEED)
        out = []
        vm.run_until_input()
        out += [e.data for e in vm.events if isinstance(e, Text)]
        vm.events.clear()
        for line in LINES:
            vm.feed(line)
            vm.run_until_input()
            out += [e.data for e in vm.events if isinstance(e, Text)]
            vm.events.clear()
        ours = "".join(out)
        ref = norm(dfrotz_transcript(C / "zork1.z3", LINES, seed=SEED))
        our_lines = [l for l in (x.strip() for x in ours.split("\n")) if l and l != ">"]
        ref_lines = [l for l in ref.split("\n") if l and l != ">"]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 20, f"too few lines ({m}) — v3 opcode coverage bug")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")
        self.assertIn("Opening the small mailbox reveals a leaflet.", ours)
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement v3 mode** (every version-dependent site: packed multiplier, arithmetic masking, routine headers, dictionary encoding, read format, save/restore form, quit status, flags1 handling. Run `minizork.z3` through the same harness as a second smoke case — it's a heavily-condensed C64 build that stresses edge cases)

- [ ] **Step 4: Run, verify PASS** (both zork1.z3 and minizork.z3 match dfrotz through the script)

Run: `python3 -m unittest tests.test_vm_v3 -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: v3 compatibility mode; Zork I matches dfrotz"`

---

### Task 9: v8 — 64-bit arithmetic, wide strings, v8 opcodes

**Files:**
- Modify: `zmach/vm.py`, `zmach/opcodes.py` (v8 branches)
- Test: `tests/test_vm_v8.py`

**Interfaces:**
- Consumes: Tasks 5-8
- Produces (v8 only):
  - 64-bit locals/globals/stack entries (`memory.width == 8`; arithmetic on Python ints masked to signed 64-bit: `((x & 0xFFFFFFFFFFFFFFFF) << 64) >> 64`; `print_num` 64-bit signed; `mul` truncates to 64 bits)
  - packed multiplier 8; stack top 0x3FFFE
  - `print`/`print_ret` wide strings (packed & 0x8000 → `decode_wide`)
  - v8 array opcodes: `new_array`, `move_array`, `get_array_size`, `load_array`, `store_array` (extended opcodes — numbers from ZSpec §14 v8 entries; array header: first word = size in words; 64-bit elements)
  - `random`, `tokenize` (v8 form incl. dictionary operand), `encode_text` (v8: 9 z-chars + optional dict) per ZSpec §15 + dork
  - `check_arg_count` (VAR:255, v5+: also in v5 — implement here if not in Task 5)
  - v8 `quit` status global (per ZSpec §15 — verify with dfrotz on a quit script; not asserted, no traceback required)

- [ ] **Step 1: Write the failing differential test**

```python
# tests/test_vm_v8.py
import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from zmach.storyfile import StoryFile
from zmach.vm import VM
from zmach.events import Text

C = Path(__file__).parent / "corpus"
SEED = 10
LINES = ["look", "north", "examine everything", "take everything", "quit"]

class TestRisorg(unittest.TestCase):
    def test_risorg_first_commands(self):
        sf = StoryFile.load(C / "risorg.z8")
        vm = VM(sf, seed=SEED)
        out = []
        vm.run_until_input()
        out += [e.data for e in vm.events if isinstance(e, Text)]
        vm.events.clear()
        for line in LINES:
            vm.feed(line)
            vm.run_until_input()
            out += [e.data for e in vm.events if isinstance(e, Text)]
            vm.events.clear()
        ours = "".join(out)
        ref = norm(dfrotz_transcript(C / "risorg.z8", LINES, seed=SEED))
        our_lines = [l for l in (x.strip() for x in ours.split("\n")) if l]
        ref_lines = [l for l in ref.split("\n") if l]
        m = min(len(our_lines), len(ref_lines))
        self.assertGreaterEqual(m, 15, f"too few lines ({m}) — v8 coverage bug")
        for i in range(m):
            self.assertEqual(our_lines[i], ref_lines[i],
                             f"line {i}: {our_lines[i]!r} != {ref_lines[i]!r}")
        # wide-string coverage: risorg's I7 library prints diacritics in some text
        self.assertNotIn("\ufffd", ours)
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement v8 mode** (64-bit pass over every arithmetic/array/variable site — a `width`-switched helper keeps it one place; extended-opcode table for v8 per ZSpec §14 + dork machine.ts)

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_vm_v8 -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: v8 64-bit mode, wide strings, v8 array opcodes; risorg matches dfrotz"`

---

### Task 10: save/restore + Session API

**Files:**
- Create: `zmach/savefile.py`, `zmach/session.py`
- Modify: `zmach/vm.py` (`snapshot`/`restore_state`, save/restore opcode handlers, input-buffer-empty assert)
- Test: `tests/test_savefile.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: Tasks 5-9
- Produces:
  - `savefile.encode(vm) -> bytes` / `savefile.decode(vm, image: bytes) -> None` — ZMSAVE v1 per spec §7 (fixed offsets, big-endian, 512 KB image, VM-state block: sp u32, error u32, frames ×{return_pc u32, locals_base u32, n_locals u8, n_args u8}, catch tokens u32, RNG seed u32; trailer + story-hash validation; `SaveFileError` on any mismatch)
  - `VM.save_handler` / `VM.restore_handler` (callables `(filename_hint: str) -> bool`; the in-game `save`/`restore` opcodes read the filename via the input stream — v5: the opcode's string operand IS the filename hint and the handler decides; v3: branch form, filename prompted through the CLI/handler)
  - `Session` exactly per spec §5: `load(path, seed=None)`, `input(line) -> list[Event]`, `save() -> bytes`, `restore(image: bytes) -> list[Event]`, `set_save_handler`, `set_restore_handler`, `story -> StoryInfo`, `done -> bool`.
    - `input`: if `done` → `[Error("game over")]`; else `vm.feed(line)`; `vm.run_until_input()`; drain `vm.events` into the list; if `vm.needs_input` append `Prompt()`; if `vm.done` append `EndOfGame(status)`.
    - `load`: run to first Prompt/EndOfGame, return events.
    - `save`: `assert vm.input_buffer.empty()` (spec §7 precondition); `savefile.encode(vm)`.
    - `restore`: `savefile.decode(vm, image)`; run to next boundary; return events.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_savefile.py
import unittest
from pathlib import Path
from zmach.session import Session

C = Path(__file__).parent / "corpus"

class TestSaveRoundTrip(unittest.TestCase):
    def test_roundtrip_transcript_identical(self):
        def play(path, seed, lines, restore_at=None):
            s = Session()
            s.load(path, seed=seed)
            out = []
            for i, line in enumerate(lines):
                out += s.input(line)
                if i + 1 == restore_at:
                    img = s.save()
                    s.restore(img)      # same session, opaque round-trip
                    out += s.input("look")
            return "".join(e.data for e in out if hasattr(e, "data"))
        lines = ["look", "north", "look", "east", "look", "north", "look", "quit"]
        a = play(C / "planetfall.z5", 10, lines)
        b = play(C / "planetfall.z5", 10, lines, restore_at=4)
        # transcript must be identical: save/restore is lossless
        self.assertEqual(a, b)
```

(plus `tests/test_session.py`: event types from `load`, `Prompt` after each `input`, `done` after quit, `StoryInfo` fields, `restore` with corrupted trailer → `SaveFileError`, story-hash mismatch between planetfall save and zork1 session → `SaveFileError`.)

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement savefile.py + Session + opcode wiring** (save/restore opcode: v3 branch form 0x05/0x06 with handler filename prompt; v5 EXT:0/1 with result 0 (fail) / 1 (success) — handler returns bool; the opcode also handles the "no handler installed → error 424/425" path)

- [ ] **Step 4: Run, verify PASS** (round-trip byte-identical, incl. RNG continuity across restore — this is the Phase 2 reconnect guarantee)

Run: `python3 -m unittest tests.test_savefile tests.test_session -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: ZMSAVE v1 save/restore, in-game save opcodes, Session API"`

---

### Task 11: CLI

**Files:**
- Create: `zmach/cli.py`, `zmach/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Session` (Task 10)
- Produces: `zmach cli` entry (`python3 -m zmach`):

```
zmach story.z5 [--strict] [--seed <int>] [--save <file>] [--restore <file>]
```

- REPL: `Text` → print; `Prompt` → print `"> "` (no newline); `Error` → print to stderr; `EndOfGame` → print, exit 0.
- In-game `save`/`restore` (opcode handlers): prompt `Save to: ` / `Restore from: ` on stderr, read one line from stdin (works in piped mode), handler writes/reads the file, returns success.
- Meta commands (never fed to the game): `@save <file>`, `@restore <file>`, `@info` (story info + version), `@quit`.
- `--save`/`--restore`: save after load / restore before running (API path).
- Exit codes: 0 normal/EndOfGame, 1 `StoryFileError`/`SaveFileError` (clean message, no traceback).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import subprocess, unittest
from pathlib import Path

C = Path(__file__).parent / "corpus"

class TestCli(unittest.TestCase):
    def test_scripted_run(self):
        p = subprocess.run(
            ["python3", "-m", "zmach", str(C / "zork1.z3"), "--seed", "10"],
            input="look\nopen mailbox\n@quit\n", capture_output=True,
            text=True, timeout=60, cwd=Path(__file__).parent.parent)
        self.assertEqual(p.returncode, 0)
        self.assertIn("ZORK I: The Great Underground Empire", p.stdout)
        self.assertIn("Opening the small mailbox reveals a leaflet.", p.stdout)

    def test_bad_story_clean_exit(self):
        p = subprocess.run(["python3", "-m", "zmach", "/nonexistent.z5"],
                           capture_output=True, text=True, timeout=30,
                           cwd=Path(__file__).parent.parent)
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_meta_save_restore(self):
        save = Path("/tmp/cli_test.zsave")
        p = subprocess.run(
            ["python3", "-m", "zmach", str(C / "zork1.z3"), "--seed", "10"],
            input="look\n@save %s\nlook\n@quit\n" % save,
            capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).parent.parent)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(save.exists())
        self.assertGreater(save.stat().st_size, 500000)  # ZMSAVE v1 size
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement cli.py + __main__.py**

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_cli -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: CLI REPL with meta commands and clean error exits"`

---

### Task 12: conformance suite harness

**Files:**
- Create: `tests/conformance/run_conformance.py`
- Test: `tests/test_conformance.py`

**Interfaces:**
- Consumes: `Session` (Task 10)
- Produces: a driver that runs each conformance story through `Session` with scripted input (the suites are interactive: CZECH/StrictZ read commands; feed blank lines — they self-drive; compare the final report against the expected output file / a reference dfrotz run):
  - `play_to_end(path, seed, max_lines) -> str` (feed blank lines until `EndOfGame` or `max_lines`; returns joined `Text` data)
  - `play_session_lines(path, lines, seed) -> str` (feed a fixed line list; used by Task 13)
  - `czech.z5` (v5): run to completion (feed ~500 blank lines or until `EndOfGame`), compare against `tests/corpus/czech.out5` (normalize whitespace; the .out file is the expected transcript)
  - `strictz.z5` (v5): run to completion, compare against a dfrotz reference run (`dfrotz -t -s 10 strictz.z5` with blank lines) — StrictZ prints per-test PASS/FAIL lines; our transcript must match dfrotz's exactly (document any legitimate divergence, e.g. undo)
  - `crashme.z5`: run to completion; **must not raise** (Python traceback = fail); expect the suite's own "done" text
  - `unicode.z5`: compare against dfrotz reference (string/ZSCII coverage)
  - `random.z5`: compare against dfrotz reference (RNG determinism)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conformance.py
import subprocess, unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from tests.conformance.run_conformance import play_to_end

C = Path(__file__).parent / "corpus"

class TestConformance(unittest.TestCase):
    def test_czech(self):
        ours = norm(play_to_end(C / "czech.z5", seed=10, max_lines=800))
        ref = norm((C / "czech.out5").read_text())
        # CZECH's final line reports the pass/fail totals — they must agree
        self.assertEqual(ours.strip().splitlines()[-1],
                         ref.strip().splitlines()[-1])

    def test_crashme_no_crash(self):
        text = play_to_end(C / "crashme.z5", seed=10, max_lines=400)
        self.assertIn("DONE", text.upper())

    def test_random_matches_dfrotz(self):
        ours = norm(play_to_end(C / "random.z5", seed=10, max_lines=200))
        ref = norm(dfrotz_transcript(C / "random.z5", [""] * 100, seed=10))
        self.assertEqual(ours, ref)
```

- [ ] **Step 2: Run, verify FAIL** (or expose real conformance bugs — fix the VM until green; this is the milestone that makes "conforms" a measured fact)

- [ ] **Step 3: Implement the harness + fix whatever it finds** (each fix: identify the opcode, check ZSpec §15 + dork, add a unit test in `tests/` for the specific behavior, re-run)

- [ ] **Step 4: Run, verify PASS** — CZECH totals line matches czech.out5, crashme completes, random/unicode match dfrotz

Run: `python3 -m unittest tests.test_conformance -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: conformance harness; CZECH/crashme/random/unicode green"`

---

### Task 13: differential walkthrough + fake-transport harness

**Files:**
- Create: `tests/differential/zork1_walk.txt`, `tests/differential/run_differential.py`, `tests/faketx/channel.py`
- Test: `tests/test_differential.py`, `tests/test_faketx.py`

**Interfaces:**
- Consumes: `Session` (Task 10), `tests/util.py`
- Produces:
  - `zork1_walk.txt`: first ~120 commands of the official Zork I walkthrough (fetch: `curl -sL https://eblong.com/infocom/` → find the Zork I walkthrough link → extract the command lines (stripped of trailing periods, lowercased); fallback: ifarchive.org Infocom walkthroughs section. First command must be `open mailbox`; sanity-check the file by eye — 120+ lines, no headings)
  - `play_to_end` reused from Task 12
  - `tests/faketx/channel.py`: `FakeChannel(chunk=512, latency_fn=None)` — takes a `Session`, drives it exactly like a network transport would: `send_input(line)` chunks the line into ≤512-byte fragments (boundaries NOT line-aligned) and reassembles before `Session.input`; `drain()` reassembles `Text` events received in arbitrary chunk splits; a `delay(turns)` hook simulates the player being away (state must persist — the VM is just blocked).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_differential.py
import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from tests.conformance.run_conformance import play_session_lines
from tests.differential.run_differential import WALK

C = Path(__file__).parent / "corpus"

class TestZorkIWalkthrough(unittest.TestCase):
    def test_120_commands_byte_identical(self):
        lines = WALK[:120]
        ours = norm(play_session_lines(C / "zork1.z3", lines, seed=10))
        ref = norm(dfrotz_transcript(C / "zork1.z3", lines, seed=10))
        self.assertEqual(ours, ref,
                         "first divergence: " + next(
                             (f"{a!r} != {b!r}" for a, b in zip(
                                 ours.split("\n"), ref.split("\n")) if a != b),
                             "length mismatch"))
```

```python
# tests/test_faketx.py
import unittest
from pathlib import Path
from tests.util import dfrotz_transcript, norm
from tests.faketx.channel import FakeChannel

C = Path(__file__).parent / "corpus"
LINES = ["look", "open mailbox", "take leaflet", "west", "north", "quit"]

class TestFakeTransport(unittest.TestCase):
    def test_chunked_channel_matches_local(self):
        # every byte through the network-shaped channel — load and input
        ch = FakeChannel(chunk=7)   # tiny chunks, worst-case boundaries
        ch.load(C / "zork1.z3", seed=10)
        out = ch.drain()
        for line in LINES:
            ch.send_input(line)
            out += ch.drain()
        ours = norm("".join(out))
        ref = norm(dfrotz_transcript(C / "zork1.z3", LINES, seed=10))
        self.assertEqual(ours, ref)
```

- [ ] **Step 2: Run, verify FAIL** (channel + walkthrough don't exist)

- [ ] **Step 3: Fetch the walkthrough, build the harness, run** — the walkthrough test is the done bar's flagship: 120 real commands through the v3 engine, byte-identical to dfrotz including the in-game save/restore the walkthrough performs (if the walkthrough's save uses a filename, our handler script must answer it deterministically — the driver feeds the filename as the next line when the save prompt fires; document this in `run_differential.py`)

- [ ] **Step 4: Run, verify PASS**

Run: `python3 -m unittest tests.test_differential tests.test_faketx -v`

- [ ] **Step 5: Commit**

`git add -A && git commit -m "feat: 120-command Zork I differential vs dfrotz; fake-transport harness"`

---

### Task 14: done-bar runner + README

**Files:**
- Create: `scripts/run_done.py`, `README.md`
- Test: — (the runner IS the test)

**Interfaces:**
- Consumes: everything
- Produces: `scripts/run_done.py` — runs, in order: full `unittest` suite, conformance (Task 12), differentials (Task 13), a save round-trip, and a manual-smoke reminder line; prints a checklist with ✓/✗ per spec §11 done-bar gate; exits non-zero on any failure. `README.md`: what this is, `python3 -m zmach game.z5` usage, done-bar status, Phase 2 pointer (spec §12).

- [ ] **Step 1: Write the runner** (subprocess `python3 -m unittest discover -s tests -v`, parse the tail, run the differential + faketx + save tests by name, print the spec §11 checklist)

- [ ] **Step 2: Run the full done bar**

Run: `python3 scripts/run_done.py` — every gate ✓. This is the Phase 1 completion evidence; capture the output in the commit message.

- [ ] **Step 3: Write README.md**

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: done-bar runner, README — Phase 1 complete

<paste run_done.py checklist output>"
```

---

## Self-review notes (plan author)

- **Spec coverage:** spec §2/§3 → Tasks 2, 8, 9 (version gate, stubs); §4 → Tasks 0-11 (all 10 modules); §5 → Task 10 (exact API); §6 → Tasks 5-6 (text mode); §7 → Task 10 (ZMSAVE v1); §8 → Task 6 (line-into-buffer model, pending-opcode resume); §9 → Tasks 2, 5, 12 (bad-opcode recovery, crashme gate); §10 → Task 11; §11 → Tasks 12-14 (all five done-bar gates); §12 → nothing built (Phase 2); §13 → Task 0. No gaps.
- **Type consistency:** `StoryFile.load → StoryFile(data, header, sha256, name)` used identically in Tasks 3-9; `VM(story, seed)`, `vm.run_until_input()`, `vm.feed(line)`, `vm.events`, `vm.needs_input`, `vm.done` consistent across Tasks 5-13; `Session` API matches spec §5 verbatim (Task 10).
- **Known risk areas (gated, not guessed):** v3 packed parent/sibling/child (Task 7 test proves on Zork I's real table); branch-offset formulas per form (CZECH); C-division (unit test + CZECH); RNG (random.z5 + StrictZ); parse-table formats (planetfall + Zork I differentials); v8 wide-string details (risorg differential). If a gate and dork disagree, the conformance suite wins, then dfrotz.