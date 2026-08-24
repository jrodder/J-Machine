import unittest
from pathlib import Path

from tests.util import dfrotz_transcript
from zmach.events import Text
from zmach.storyfile import StoryFile
from zmach.vm import VM, cdiv, cmod, s16

C = Path(__file__).parent / "corpus"


class TestRisorgByteExact(unittest.TestCase):
    """Byte-exact dfrotz -t conformance: the dumb-terminal flush rules.

    Verified against instrumented frotz 2.55 (show_row/dumb_show_prompt
    logging + strace of the fd-1 writes):

    - the status row is re-emitted at a seam only if one of its cells
      actually CHANGED since the last emission (frotz dumb_set_cell
      compares new vs old cell content). risorg's Inform-6 library
      clears the row with printed spaces before re-writing it, so even
      an identical status line re-emits (the quit confirmation);
      a direct identical re-write emits nothing;
    - one blank line separates the status row from the first content
      row only when the content row is not adjacent to the status row
      (SPANS: the blank row just above the first changed row is shown
      because the row below it changed);
    - leading blank rows are never emitted (SPANS starts at the first
      changed row; the screen's top rows were blank at start);
    - the cursor row (the last row written before a read) is emitted
      bare — no trailing newline — by the prompt path, so it merges
      with the NEXT seam's first emission (the status row) in the
      byte stream: '> <status>'.
    """

    LINES = ["look", "look", "quit", "yes"]

    @staticmethod
    def _run(lines):
        vm = VM(StoryFile.load(C / "risorg.z8"), seed=10)
        out = []
        vm.run_until_input()
        out += [e.data for e in vm.events if isinstance(e, Text)]
        vm.events.clear()
        for line in lines:
            vm.feed(line)
            vm.run_until_input()
            out += [e.data for e in vm.events if isinstance(e, Text)]
            vm.events.clear()
        return "".join(out)

    @staticmethod
    def _ref(lines):
        ref = dfrotz_transcript(C / "risorg.z8", lines, seed=10)
        # strip dfrotz's startup banner (two lines)
        return ref.split("\n", 2)[2]

    def test_short_session_bytes(self):
        self.assertEqual(self._run(self.LINES), self._ref(self.LINES))

    def test_quit_confirm_seam(self):
        # Same status line re-emitted at the confirm seam (identical
        # text, rewritten by the library), then one blank line, then
        # the question as the bare cursor row.
        tail = self._run(["look", "look", "quit"])
        self.assertIn("> Lecture Hall", tail,
                      "status row must merge with the previous prompt")
        self.assertTrue(tail.endswith(
            "Moves: 1\n\nAre you sure you want to quit? "),
            repr(tail[-90:]))

    def test_intro_has_no_leading_newline(self):
        out = self._run([])
        self.assertTrue(out.startswith("You've heard"), repr(out[:40]))

    def test_indefinite_article_uses_interpreter_standard(self):
        # seed=7 seeds an "exercise book" into the starting room. The I6
        # library picks the article (a/an) from the interpreter's Z-machine
        # standard version, which the game reads via @loadw from header
        # bytes 50-51 (0x32-0x33). dfrotz writes 1.1 there at startup
        # (init_header: standard_high/low = 1); the reference therefore
        # prints "an exercise book".
        vm = VM(StoryFile.load(C / "risorg.z8"), seed=7)
        out = []
        vm.run_until_input()
        out += [e.data for e in vm.events if isinstance(e, Text)]
        vm.events.clear()
        for line in ["take everything", "inventory"]:
            vm.feed(line)
            vm.run_until_input()
            out += [e.data for e in vm.events if isinstance(e, Text)]
            vm.events.clear()
        out = "".join(out)
        ref = dfrotz_transcript(C / "risorg.z8",
                                ["take everything", "inventory"], seed=7)
        self.assertIn("an exercise book", ref)
        self.assertIn("an exercise book", out, repr(out[-400:]))


class TestHighAddressOps(unittest.TestCase):
    """16-bit address arithmetic wraps mod 2^16 (frotz LOW_WORD).

    Operand values >= 0x8000 are negative as s16; without masking the
    computed address goes negative and Memory's OOB guard silently reads
    0 / ignores writes. risorg (v8) hits this in the Inform library's
    get_prop_addr path: `loadw 0x911C, 0` must read 73, not 0.
    """

    def setUp(self):
        self.vm = VM(StoryFile.load(C / "risorg.z8"), seed=10)

    def _run_store0(self, op, operands):
        """Run a store-form op with a store byte of 0 (push) and pop it."""
        vm = self.vm
        scratch = len(vm.mem.mem) - 8  # stack region, zeroed
        vm.mem.putb(scratch, 0)
        vm.pc = scratch
        op(operands, len(operands))
        return vm._pop()

    def test_loadw_high_address(self):
        # word at 0x911C in risorg = 0x0049 (73)
        self.assertEqual(
            self._run_store0(self.vm.op_loadw, [s16(0x911C), 0]), 73)

    def test_loadw_high_address_plus_index(self):
        # 0x911C + 2*1 wraps mod 2^16 -> 0x911E; word = 0x0227
        self.assertEqual(
            self._run_store0(self.vm.op_loadw, [s16(0x911C), 1]), 0x0227)

    def test_loadb_high_address(self):
        self.assertEqual(
            self._run_store0(self.vm.op_loadb, [s16(0x911C), 1]), 0x49)

    def test_storew_high_address(self):
        vm = self.vm
        vm.op_storew([s16(0x911C), 0, 0xABCD], 3)
        self.assertEqual(vm.mem.getw(0x911C), 0xABCD)

    def test_storeb_high_address(self):
        vm = self.vm
        vm.op_storeb([s16(0x911C), 1, 0x77], 3)
        self.assertEqual(vm.mem.getb(0x911D), 0x77)

    def test_risorg_starts_clean(self):
        # the message-73 dispatch must not raise the v8 programming
        # error ("no property 73"); risorg reaches the first ***MORE***
        vm = VM(StoryFile.load(C / "risorg.z8"), seed=10)
        vm.run_until_input()
        text = "".join(e.data for e in vm.events if isinstance(e, Text))
        self.assertNotIn("Programming error", text)
        self.assertIn("Risorgimento Represso", text)


class TestMorePrompt(unittest.TestCase):
    """dfrotz -t screen model (frotz screen.c screen_new_line): 24-row
    screen, cursor at the bottom after each newline, so the MORE prompt
    fires when line_count reaches above + below - 1 = 22 + 2 - 1 = 23.
    The prompt line is read and DISCARDED (dumb_read_misc_line) and
    line_count resets to context_lines (0)."""

    def test_risorg_more_before_room(self):
        vm = VM(StoryFile.load(C / "risorg.z8"), seed=10)
        vm.run_until_input()
        text = "".join(e.data for e in vm.events if isinstance(e, Text))
        # dfrotz stops at ***MORE*** before the room description; the
        # prompt has no trailing newline, and the 23rd line is the
        # "NonCommercial License..." line (the machine stream has 3
        # leading newlines, so the visible line count is 20)
        self.assertTrue(text.endswith("***MORE***"), repr(text[-120:]))
        self.assertNotIn("Lecture Hall (on the seat)", text)
        lines = [l for l in text.split("\n") if l]
        self.assertEqual(lines[-2],
                         "NonCommercial License. Type LICENSE to find out "
                         "more about the terms of the",
                         repr(lines[-2]))

    def test_more_consumes_discarded_line(self):
        vm = VM(StoryFile.load(C / "risorg.z8"), seed=10)
        vm.run_until_input()
        vm.events.clear()
        vm.feed("look")  # the line typed at MORE: consumed and discarded
        vm.run_until_input()  # the game's @read then blocks at '>'
        text = "".join(e.data for e in vm.events if isinstance(e, Text))
        self.assertIn("Lecture Hall (on the seat)", text)
        self.assertTrue(text.endswith(">"), repr(text[-120:]))
        # the discarded "look" was not parsed as a command: the room
        # is still the unexamined opening view (no re-description)
        self.assertEqual(text.count("Lecture Hall (on the seat)"), 1)


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


if __name__ == "__main__":
    unittest.main()