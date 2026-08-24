"""ZMSAVE v1 save/restore (spec §7, Task 10 amendments — see
zmach/savefile.py header) and the in-game save/restore opcode forms.

Round-trip gate: play a session with an opaque save/restore mid-run;
the transcript must be byte-identical to the never-saved run (RNG
continuity across restore = the Phase 2 reconnect guarantee).
"""
import unittest
from pathlib import Path

from zmach.events import Error, SaveFileError, Text
from zmach.session import Session
from zmach.storyfile import StoryFile
from zmach.vm import VM

C = Path(__file__).parent / "corpus"


def play_text(path, seed, lines, restore_at=None):
    """Play `lines`; if restore_at given (1-based index), take an opaque
    save/restore immediately BEFORE feeding that line. Return the
    joined transcript (Text events only)."""
    s = Session()
    s.load(path, seed=seed)
    out = []
    for i, line in enumerate(lines):
        if restore_at is not None and i + 1 == restore_at:
            img = s.save()
            s.restore(img)
        evs = s.input(line)
        out += [e.data for e in evs if isinstance(e, Text)]
    return "".join(out)


class TestSaveRoundTrip(unittest.TestCase):
    """Save/restore is lossless: identical transcript either way."""

    def test_planetfall_roundtrip(self):
        lines = ["look", "north", "look", "east", "look", "north", "look", "quit"]
        a = play_text(C / "planetfall.z5", 10, lines)
        b = play_text(C / "planetfall.z5", 10, lines, restore_at=4)
        self.assertGreater(len(a), 500, "session too short to be a gate")
        self.assertEqual(a, b)

    def test_risorg_roundtrip(self):
        lines = ["take everything", "inventory", "look", "inventory", "east", "look"]
        a = play_text(C / "risorg.z8", 7, lines)
        b = play_text(C / "risorg.z8", 7, lines, restore_at=3)
        self.assertIn("an exercise book", a)
        self.assertEqual(a, b)

    def test_rng_state_preserved_into_fresh_vm(self):
        # Restore into a FRESH VM (the reconnect case): full machine
        # state, including the RNG, must match the saved VM exactly.
        s1 = Session()
        s1.load(C / "risorg.z8", seed=7)
        s1.input("look")
        vm1 = s1._vm
        before = (vm1._rng_a, vm1._rng_interval, vm1._rng_counter, vm1.sp, vm1.pc)
        img = s1.save()
        s2 = Session()
        s2.load(C / "risorg.z8", seed=7)
        s2.restore(img)
        vm2 = s2._vm
        after = (vm2._rng_a, vm2._rng_interval, vm2._rng_counter, vm2.sp, vm2.pc)
        self.assertEqual(after, before)
        self.assertEqual(vm2.done, vm1.done)


class TestSaveFileFormat(unittest.TestCase):
    """decode validates magic, story hash, trailer hash (spec §7)."""

    @classmethod
    def setUpClass(cls):
        s = Session()
        s.load(C / "planetfall.z5", seed=10)
        s.input("look")
        cls.image = s.save()
        cls.session = s

    def test_bad_magic(self):
        s = Session()
        s.load(C / "planetfall.z5", seed=10)
        bad = bytearray(self.image)
        bad[0] = 0xFF
        with self.assertRaises(SaveFileError):
            s.restore(bytes(bad))

    def test_corrupt_trailer(self):
        bad = bytearray(self.image)
        bad[-5] ^= 0x01  # a byte inside the trailer hash
        with self.assertRaises(SaveFileError):
            self.session.restore(bytes(bad))

    def test_story_hash_mismatch(self):
        # a planetfall image into a zork1 session
        s = Session()
        s.load(C / "zork1.z3", seed=10)
        with self.assertRaises(SaveFileError):
            s.restore(self.image)

    def test_image_length_mismatch(self):
        import struct
        bad = bytearray(self.image)
        # claim a different image length; the trailer then mismatches
        o = 11864 + struct.unpack_from(">I", self.image, 11860)[0]
        struct.pack_into(">I", bad, o - 4,
                         struct.unpack_from(">I", self.image, o - 4)[0] + 1)
        with self.assertRaises(SaveFileError):
            self.session.restore(bytes(bad))


class TestInGameSaveOpcodes(unittest.TestCase):
    """v5+ EXT:0/EXT:1 (store form) and v3 0xB5/0xB6 (branch form), per
    frotz fastmem.c z_save/z_restore and ZSpec 1.1 (@save -> (result),
    restore stores 2 on success). Results go to variable 16 (store byte
    0x10 — bytes 1-15 would be frame locals)."""

    G16 = 0xA965            # risorg globals base: variable 16
    G17 = 0xA965 + 2        # variable 17

    def _v5_vm(self, ext_byte, ok):
        sf = StoryFile.load(C / "risorg.z8")
        vm = VM(sf)
        a = sf.header.declared_len + 0x1000
        # be <ext> c0 10 ba: ext save/restore (no operands), result ->
        # variable 16, then quit
        for i, b in enumerate((0xBE, ext_byte, 0xC0, 0x10, 0xBA)):
            vm.mem.putb(a + i, b)
        vm.mem.putw(self.G17, 12)
        vm.pc = a
        if ok is not None:
            if ext_byte == 0:
                vm.save_handler = lambda hint: ok
            else:
                vm.restore_handler = lambda hint: ok
        return vm

    def test_v5_save_success_stores_1(self):
        vm = self._v5_vm(0, True)
        vm.run_until_input()
        self.assertTrue(vm.done and vm.done_status == 0)
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, 1)

    def test_v5_save_failure_stores_0(self):
        vm = self._v5_vm(0, False)
        vm.run_until_input()
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, 0)

    def test_v5_save_no_handler_stores_0(self):
        vm = self._v5_vm(0, None)
        vm.save_handler = None  # VM defaults to the host stub; disable it
        vm.run_until_input()
        self.assertTrue(vm.done)  # no crash, game continues
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, 0)

    def test_v5_restore_success_stores_2(self):
        # frotz restore_quetzal returns 2 on success (not 1)
        vm = self._v5_vm(1, True)
        vm.run_until_input()
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, 2)

    def test_v5_restore_failure_stores_0(self):
        vm = self._v5_vm(1, False)
        vm.run_until_input()
        self.assertEqual(vm.mem.getw(self.G16) & 0xFFFF, 0)

    def test_v3_save_branch_form(self):
        # b5 c6 48 11 02 10 ba (zork1): v3 save is a BRANCH opcode —
        # success jumps over the `or G17,2 -> G16`; failure falls
        # through and executes it.
        gb = StoryFile.load(C / "zork1.z3").header.globals_base

        def make(vm, ok):
            a = vm.story.header.declared_len + 0x1000
            for i, b in enumerate((0xB5, 0xC6, 0x48, 0x11, 0x02, 0x10, 0xBA)):
                vm.mem.putb(a + i, b)
            vm.mem.putw(gb + 2, 12)  # G17 = 12
            vm.pc = a
            vm.save_handler = lambda hint: ok

        vm = VM(StoryFile.load(C / "zork1.z3"))
        make(vm, True)
        vm.run_until_input()
        self.assertTrue(vm.done and vm.done_status == 0)
        self.assertEqual(vm.mem.getw(gb) & 0xFFFF, 0)  # branched: or skipped

        vm2 = VM(StoryFile.load(C / "zork1.z3"))
        make(vm2, False)
        vm2.run_until_input()
        self.assertTrue(vm2.done)
        self.assertEqual(vm2.mem.getw(gb) & 0xFFFF, 12 | 2)  # fell through


class TestStubHandlers(unittest.TestCase):
    """The default host-side handlers: a ZMSAVE v1 file round-trip that
    restores frame locals too (the Task 5 stub only wrote memory)."""

    def test_stub_save_restore_roundtrip(self):
        import tempfile
        s = Session()
        s.load(C / "risorg.z8", seed=7)
        s.input("look")
        vm = s._vm
        gb = vm.globals_base
        marker = vm.mem.getw(gb + 2 * (50 - 16)) & 0xFFFF
        path = tempfile.mktemp(suffix=".zmsave")
        self.assertTrue(vm.save_handler(path))  # snapshot of the clean state
        vm.mem.putw(gb + 2 * (50 - 16), marker ^ 0x5A5A)  # clobber G50
        self.assertTrue(vm.restore_handler(path))
        self.assertEqual(vm.mem.getw(gb + 2 * (50 - 16)) & 0xFFFF, marker)
        self.assertGreater(Path(path).stat().st_size, 100000)


if __name__ == "__main__":
    unittest.main()