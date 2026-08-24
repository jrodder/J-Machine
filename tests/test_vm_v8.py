"""v8 conformance against the dfrotz 2.55 oracle.

Re-scope ruling (Task 9): frotz 2.55 contains no ZNE support — no 64-bit
arithmetic, no array opcodes, no v8-specific random/tokenize/encode_text
forms. Its only V8 branches are `pc = routine << 3` (process.c:417),
string address `<< 3` (text.c:420), story-size x4 scaling (fastmem.c),
and the 1..8 version gate. So v8 = the v7 instruction set, 16-bit words,
packed multiplier 8. These tests pin that surface on risorg.z8 (the
corpus's only v8 game), which is byte-exact against dfrotz -t.
"""
import unittest
from pathlib import Path

from tests.util import dfrotz_transcript
from zmach.events import Error, Text
from zmach.storyfile import StoryFile
from zmach.vm import VM

C = Path(__file__).parent / "corpus"


def run_risorg(lines, seed):
    vm = VM(StoryFile.load(C / "risorg.z8"), seed=seed)
    out = []
    vm.run_until_input()
    out += [e.data for e in vm.events if isinstance(e, Text)]
    vm.events.clear()
    for line in lines:
        vm.feed(line)
        vm.run_until_input()
        out += [e.data for e in vm.events if isinstance(e, Text)]
        vm.events.clear()
        if vm.done:
            break
    return vm, "".join(out)


class TestV8Header(unittest.TestCase):
    """risorg header facts pinned against the raw file (443392 bytes)."""

    @classmethod
    def setUpClass(cls):
        cls.sf = StoryFile.load(C / "risorg.z8")

    def test_header_fields(self):
        h = self.sf.header
        self.assertEqual(h.version, 8)
        self.assertEqual(h.pc, 0xFD21)
        self.assertEqual(h.globals_base, 0xA965)
        self.assertEqual(h.flags2, 0x50)  # UNDO|COLOUR
        self.assertEqual(h.length_divisor, 8)
        self.assertEqual(h.serial, "030925")

    def test_story_size_scaling(self):
        # frotz fastmem.c: story_size = file_size * 2 (bytes) * 2 (v4+)
        # * 2 (v6+) = header file_size word (0xD86D) * 8
        self.assertEqual(self.sf.header.declared_len, 0xD86D * 8)  # 0x6C368
        # memory = story + data stack (2 * 1024 words = 0x40000 bytes)
        self.assertEqual(self.sf.memory_size(), 0x6C368 + 0x40000)

    def test_v8_call_scaling(self):
        vm = VM(self.sf)
        self.assertEqual(vm.pack_mult, 8)
        # instruction 0 at start PC: call_v(0x1FA5) — frotz v8 executes
        # the routine at 0x1FA5 << 3 = 0xFD28 (process.c:417)
        vm.pc = self.sf.header.pc
        inst, operands, nops = vm._decode()
        self.assertEqual(inst, 224)  # call_vs
        self.assertEqual(operands, [0x1FA5])


class TestV8Decode(unittest.TestCase):
    """Short-form 0x80-0xAF 1OP range on risorg's real bytes (the
    status-line clear loop at 0x146D1):

        e5 7f 20   print_char(32)   (VAR 229, small const)
        96 ff      dec var 255      (G239; type nibble = width only)
        a0 ff 3f   jz var 255       (short branch)

    frotz load_operand: type&2 -> variable byte, type&1 -> 1-byte
    unsigned, else 2-byte; z_dec/z_jz interpret the value as a variable
    index (variable.c:30) — matching our xfetch/xstore model.
    """

    @classmethod
    def setUpClass(cls):
        cls.vm = VM(StoryFile.load(C / "risorg.z8"))

    def test_print_char_var_form(self):
        self.vm.pc = 0x146D1
        inst, operands, nops = self.vm._decode()
        self.assertEqual(inst, 229)
        self.assertEqual(operands, [0x20])

    def test_1op_range_dec(self):
        self.vm.pc = 0x146D4
        inst, operands, nops = self.vm._decode()
        self.assertEqual(inst, 0x86)  # dec
        self.assertEqual(operands, [0xFF])

    def test_1op_range_jz(self):
        # variable-type operand: the byte is a variable INDEX; the
        # decoded operand is the variable's VALUE (frotz load_operand
        # derefs; variable 0 pops the stack). Set G239 first so the
        # expectation pins the deref, not the index.
        # variable operand byte 0xFF = variable 255 = globals + 2*(255-16)
        var255 = self.vm.globals_base + 2 * (255 - 16)
        self.vm.mem.putw(var255, 0x42)
        self.vm.pc = 0x146D6
        inst, operands, nops = self.vm._decode()
        self.assertEqual(inst, 0x80)  # jz
        self.assertEqual(operands, [0x42])


class TestExtFallback(unittest.TestCase):
    """frotz __extended__ (process.c:641): opcodes outside its 29-entry
    table (0x1D-0xFF) and table slots a text interpreter doesn't
    implement (picture/mouse/undo/menu/colour) CONSUME their operands
    and do nothing observable — they are not runtime errors.
    Trailing byte: result-bearing ext opcodes carry one (frotz handlers
    call store()); reserved and no-result opcodes do not (Task 10).
    Crafted instruction streams at a writable address:
      be <ext> c0 [store] 48 11 02 10 ba
      48 11 02 10 = 2OP or(var 17, small 2) -> var 16: a desynced decode
      would misalign this instruction and G16 would not be 14."""

    G16 = 0xA965        # risorg globals base: variable 16
    G17 = 0xA965 + 2    # variable 17

    EXPECT = 12 | 2  # or(G17, 2)

    @staticmethod
    def _ext_vm(ext_byte, store_byte=None):
        sf = StoryFile.load(C / "risorg.z8")
        vm = VM(sf)
        a = sf.header.declared_len + 0x1000  # data-stack area: writable
        code = (0xBE, ext_byte, 0xC0)
        if store_byte is not None:
            code += (store_byte,)
        code += (0x48, 0x11, 0x02, 0x10, 0xBA)  # or G17,small 2 -> G16; quit
        for i, b in enumerate(code):
            vm.mem.putb(a + i, b)
        vm.mem.putw(TestExtFallback.G17, 12)
        vm.pc = a
        return vm

    def _assert_noop(self, ext_byte, store_byte):
        vm = self._ext_vm(ext_byte, store_byte)
        vm.run_until_input()
        self.assertTrue(vm.done)
        self.assertEqual(vm.done_status, 0)  # quit, not a runtime error
        self.assertFalse([e for e in vm.events if isinstance(e, Error)],
                         "unimplemented ext opcode must not raise")
        # the 2OP or must have executed exactly once, correctly aligned
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, self.EXPECT)

    def test_result_bearing_ext_stores_failure(self):
        # ext 5 (draw_picture) has a result: the store byte is consumed,
        # failure (0) is reported to it.
        vm = self._ext_vm(0x05, 0x00)  # store byte 0 = push
        vm.run_until_input()
        self.assertTrue(vm.done and vm.done_status == 0)
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, self.EXPECT)
        # the pushed 0 is the stack top
        self.assertEqual(vm.mem.getw(vm.sp + 2) & 0xFFFF, 0)

    def test_reserved_ext_has_no_trailing_byte(self):
        # ext 0x3F (>= 0x1D, reserved): operands only, no store byte
        self._assert_noop(0x3F, None)

    def test_no_result_ext_has_no_trailing_byte(self):
        # ext 8 (set_margins): no result in frotz (z_set_margins never
        # calls store()) — the next instruction follows immediately
        self._assert_noop(0x08, None)


class TestRisorgV8Session(unittest.TestCase):
    """Byte-exact v8 breadth: a seed/room combination distinct from the
    TestRisorgByteExact sessions (tests/test_vm_core.py)."""

    LINES = ["look", "take everything", "inventory", "east", "look", "inventory"]

    def test_session_bytes(self):
        vm, out = run_risorg(self.LINES, seed=21)
        self.assertFalse(vm.done, "game must not end early")
        ref = dfrotz_transcript(C / "risorg.z8", self.LINES, seed=21)
        ref = ref.split("\n", 2)[2]  # strip dfrotz startup banner
        self.assertEqual(out, ref)


if __name__ == "__main__":
    unittest.main()