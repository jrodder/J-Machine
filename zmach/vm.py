"""VM — Z-machine instruction decode and dispatch (ZSpec 1.0 §4, §5, §6, §14).

v5 primary; version switches are localized on self.version / self.width.
Task 5 scope: decode/dispatch (all forms), arithmetic, variables, stack,
calls, branches, RNG, print family, quit/restart, catch/throw, new_line.
Input seam: @sread/@read_char set needs_input and rewind pc (task 6 feeds input).
INV1: output only via self.events. INV2: run_until_input returns only on
needs_input / done / unrecoverable error.
"""
import os
from dataclasses import dataclass

from . import opcodes as ops
from .events import EndOfGame, Error, SaveFileError, Text
from .io import InputBuffer, Vocabulary
from .memory import Memory
from .strings import (char_to_zscii, decode_text, encode_text, read_custom_tables,
                      zscii_to_char)

# frotz ext handlers that call store()/branch(): the extended instruction
# carries a trailing byte (store form; branch form for v3 ext 0/1). The
# no-result opcodes (set_margins 8, print_unicode 11, window_size 17,
# window_style 18, read_mouse 22, print_form 26) and reserved 0x1D-0xFF
# do not (frotz __extended__ returns after the operands). Derived from the
# frotz 2.55 source per handler (Task 10).
_EXT_TRAILING = frozenset(
    {0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 12, 13, 16, 19, 20, 21, 23, 24, 25, 27, 28})

# frotz object-0 diagnostics (err.c err_messages[], errnum 19-32). frotz's
# default ERR_REPORT_ONCE mode: errors <= 18 are fatal; 19-32 print a
# "Warning: <msg> (PC = <hex>) (will ignore further occurrences)" line ONCE
# per error number and the game continues. The PC is frotz's pcp at handler
# entry (the store/branch byte address) == our self.pc at handler entry.
_ERR_MESSAGES = {
    19: "@jin called with object 0",
    20: "@get_child called with object 0",
    21: "@get_parent called with object 0",
    22: "@get_sibling called with object 0",
    23: "@get_prop_addr called with object 0",
    24: "@get_prop called with object 0",
    25: "@put_prop called with object 0",
    26: "@clear_attr called with object 0",
    27: "@set_attr called with object 0",
    28: "@test_attr called with object 0",
    29: "@move_object called moving object 0",
    30: "@move_object called moving into object 0",
    31: "@remove_object called with object 0",
    32: "@get_next_prop called with object 0",
}

# ponytail: non-spec safety net so a bad story file can't hang a session;
# raise only if a legitimate game trips it (a normal turn is far below this).
INSTRUCTION_LIMIT = 10_000_000


def cdiv(a, b):
    """C-style division: truncate toward zero (ZSpec §4.2)."""
    q = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        q = -q
    return q


def cmod(a, b):
    """C-style remainder: a - cdiv(a, b) * b (sign follows the dividend)."""
    return a - cdiv(a, b) * b


def s16(x):
    # ponytail: ((x & 0xFFFF) << 16) >> 16 is identity in Python (positive >> is
    # logical), which silently un-signed every two's-complement value >= 0x8000
    # (garbage pc + unbounded text decode = the OOM that froze the desktop)
    x &= 0xFFFF
    return x - 0x10000 if x & 0x8000 else x


def s64(x):
    x &= 0xFFFFFFFFFFFFFFFF
    return x - 0x10000000000000000 if x & 0x8000000000000000 else x


@dataclass
class Frame:
    return_pc: int    # byte address after the call instruction (where the store byte sits)
    locals: list      # local values (NOT story memory — see _readvar note)
    n_locals: int
    n_args: int       # args actually passed, uncapped (for check_arg_count)
    sp: int           # operand-stack pointer on entry
    discard: bool     # True for call_n variants (no store on return)
    catch_n: int = 0  # catch tokens attributed to this frame


class VM:
    def __init__(self, story, seed=None):
        self.story = story
        self.mem = Memory(story)
        h = story.header
        self.version = h.version
        self.screen_width = 80  # frotz -t default; status line is 80 cols
        # frotz 2.55: 16-bit values in ALL versions (v8 included — the
        # installed dfrotz runs v8 on the v7 instruction set; no ZNE 64-bit
        # words). Width is fixed at 2 bytes.
        self.width = 2
        self.trunc = s16
        self.pack_mult = h.length_divisor
        self.globals_base = h.globals_base
        self.obj_size = 9 if self.version == 3 else 14
        # Object table (ZSpec §12.3): v3 = 9-byte entries, v4+ = 14-byte.
        # Header word 0x0A holds the property-defaults table start (31/63
        # words); the object entries begin 62 bytes on MINUS obj_size, i.e.
        # entry #1 overlaps the last obj_size bytes of the defaults region
        # (v3: defaults words 28-30 sit inside object 1's entry — standard
        # Z-machine layout, mirrors dork's init formula; object 0 is never
        # dereferenced, all accessors short-circuit on it).
        self.objects_base = h.objects + 2 * (31 if self.version == 3 else 63) \
            - self.obj_size
        self._off_parent = 4 if self.version == 3 else 6
        self._off_sibling = 5 if self.version == 3 else 8
        self._off_child = 6 if self.version == 3 else 10
        self._off_propaddr = 7 if self.version == 3 else 12
        self.defprop = h.objects - 2  # property defaults table (§12.2)
        self.zscii_extra, self.alphabet = read_custom_tables(story)
        self.seed0 = (seed & 0xFFFFFFFF) if seed is not None \
            else int.from_bytes(os.urandom(4), "big")
        # frotz keeps the RNG in file-scope statics (random.c): the seed
        # happens ONCE per process and @restart does NOT reseed it.
        # Mirrored here: seed in __init__ only (never in _init, which
        # re-runs on @restart).
        self.seed = self.seed0
        self._rng_a = self.seed0
        self._rng_interval = 0
        self._rng_counter = 0
        self.flags2 = h.flags2  # header flags as written back by dfrotz -t
        self.events = []
        self.needs_input = False
        self.done = False
        self.done_status = 0
        self.error = 0
        # frotz object-0 warning state: once-per-error-number, and it
        # persists across @restart (frotz init_err runs once per process,
        # not per restart — so this lives in __init__, not _init).
        self._err_warned = set()
        self.instrs = 0
        self.pc = 0
        self.pc_save = 0
        self.sp = 0
        self.frames = []
        self.catch_stack = []
        self.fwords = 0
        # Task 10 hooks: (filename_hint: str) -> bool. Defaults are whole-
        # memory stubs so in-game SAVE/RESTORE round-trip successfully until
        # task 10 installs the real (version-correct) formats.
        self._stub_save_path = None
        self.save_handler = self._stub_save
        self.restore_handler = self._stub_restore
        self._init()
        # dictionary vocabulary (static in the story file; fwords must be set
        # first, hence after _init) — abbreviations inside entries need it
        self.vocab = Vocabulary(self)
        self._handlers = self._build_handlers()

    def _init_io_state(self):
        """Input/output state; also reset by @restart via _init."""
        self.pending = None          # (inst, operands, nops, pc_save) blocked read
        self.input = InputBuffer(self.zscii_extra)
        # read_char line buffer (frotz dumb os_read_key): holds the current
        # line's chars, returned one at a time. A non-empty line's CR is
        # stripped; an empty line yields a single CR (13). Empty when the
        # game must block for the next line.
        from collections import deque as _deque
        self._rc_line_buf = _deque()
        # Output windows (text model, dfrotz -t parity): window 1 is the
        # status line. The 80-col buffer is PERSISTENT (the game updates it
        # in place); the line is emitted when its content changes, at the
        # input seam (or machine end).
        self._win = 0
        self._status = [" "] * 80
        self._status_col = 0
        self._status_dirty = False
        # frotz dumb_set_cell marks a cell changed only when the written
        # cell actually DIFFERS from what is on the row, so the status
        # row is re-emitted at a seam only if some cell really changed
        # since the row's last emission (or the start, when the row was
        # blank). Games that clear the row with printed spaces before
        # re-writing it (Inform 6's ShowStatusLine) therefore re-emit
        # even on identical text; direct identical re-writes (frotz's
        # v3 z_show_status redraw, planetfall's library) emit nothing.
        self._status_changed = False
        self._status_ref = [" "] * 80  # last on-screen row content
        # dfrotz -t screen model (COMPRESSION_SPANS): main-window lines are
        # buffered per line and flushed at each read; the cursor row (the
        # last row written before the read) is emitted bare (no newline)
        # by the prompt path and merges into the next flush's first line
        # ('> <status>' in the byte stream).
        # (frotz dumb_show_prompt has no trailing newline).
        self._lines = []
        self._line = ""
        self._col = 0
        # dfrotz -t MORE model (frotz screen.c): the 24-row screen has
        # the cursor at the bottom after each newline, so the prompt fires
        # when line_count reaches above + below - 1 = (23-1) + (24-23+1)
        # - 1 = 23; the prompt line is read and discarded
        # (dumb_read_misc_line). line_count resets to 0 after EVERY
        # consumed input line (screen.c console_read_input/console_read_key
        # zero all window counters), after erase_window/erase_screen, and
        # after a MORE prompt. frotz 2.55 has no set_line_count opcode,
        # so the counter is interpreter-internal.
        self._line_count = 0
        self._more_pending = False
        self._at_more = False
        # Cursor row within the main window (0-based; 0 = the row just
        # below the status row). frotz's implicit bottom-row scroll keeps
        # the cursor on the last row, so the row only grows (clamped). A
        # content row is adjacent to the status row only when its batch
        # started at the window top (_batch_row snapshots _row when the
        # current line buffer starts, i.e. at the last flush).
        self._row = 0
        self._batch_row = 0
        # Unemitted tail of a text batch that was still pending when the
        # MORE threshold hit (frotz pauses the machine mid-stream; the
        # remaining characters print AFTER the prompt is answered).
        self._emit_rest = ""
        # Output stream 3 (ZSpec §7.1.2.1): captures text into a memory
        # table; nothing reaches the screen while selected.
        self._stream3 = False
        self._stream3_table = 0
        self._stream3_buf = []

    # ------------------------------------------------------------ init
    def _init(self):
        """Header-byte initialization per ZSpec Appendix B (mirrors dork).
        Used for both first start and @restart (memory is reset first)."""
        m, h = self.mem, self.story.header
        # dfrotz -t writes the interpreter's self-description into the
        # header area at startup (os_init_screen + restart_header); the
        # game may read these raw header addresses with @loadw/@loadb
        # (e.g. I6 reads the word at 50 = the interpreter's Z-machine
        # standard version, and the flags word at 16).
        if self.version == 3:
            # v3 header byte 1 = story config bits (dfrotz: CONFIG_TANDY 0x08
            # etc.). Our oracle runs `dfrotz -t`, whose OS layer does
            # `config |= CONFIG_TANDY` for V3 only (frotz_dinit.c
            # os_init_screen) — mirror that; keep all other file bits as-is.
            m.putb(1, m.getb(1) | 0x08)
        else:
            # CONFIG_TIMEDINPUT: dumb input with default speed != 0 (dinput.c)
            m.putb(1, m.getb(1) | 0x80)
            # INTERP_DEC_20 for non-v6 (dinit.c os_init_screen)
            m.putb(30, 1)   # interpreter number (dfrotz parity: banner prints "Interpreter 1")
            m.putb(31, 0x46)  # interpreter version letter 'F'
            m.putb(32, 24)  # screen height (dfrotz dumb port: 24 rows)
            m.putb(33, 80)  # screen width
            if self.version >= 5:
                m.putw(34, 80)
                m.putw(36, 24)
                m.putb(38, 1)
                m.putb(39, 1)
                m.putb(44, 0)
                m.putb(45, 0)
            # dumb terminal has no mouse/menus (dinput.c dumb_init_input);
            # undo slots default to 25 so the UNDO bit stays set
            m.putw(16, self.flags2 & ~(0x20 | 0x100) if self.version >= 5
                   else self.flags2)
        # Z-machine Standard 1.1 self-description (init_header:
        # standard_high = standard_low = 1); written for every version.
        m.putb(50, 1)
        m.putb(51, 1)
        self.fwords = m.getw(24)
        self.pc = m.getw(6)
        self.sp = self.mem.stack_top
        self.frames = []
        self.catch_stack = []
        self._init_io_state()

    # ------------------------------------------------- operand plumbing
    def _pcgetb(self):
        v = self.mem.getb(self.pc)
        self.pc += 1
        return v

    def _pcget(self):
        v = self.trunc(self.mem.getw(self.pc))
        self.pc += 2
        return v

    def _pcfetch(self):
        # variable operand (frotz load_operand): the byte is a variable
        # INDEX (0 = stack top, popped; 1-15 locals; 16-255 globals) —
        # NOT a raw memory address; the loaded value is what handlers see.
        x = self._pcgetb()
        return self.fetch(x)

    def _opfetch(self, t):
        """Decode one operand from a VAR/EXT type nibble."""
        if t == 3:
            return None
        if t == 0:
            return self._pcget()
        if t == 1:
            return self._pcgetb()
        return self._pcfetch()

    # ------------------------------------------------- variable access
    def _put(self, a, v):
        v = self.trunc(v)
        self.mem.putw(a, v)

    def _readvar(self, x):
        # Locals live OUTSIDE story memory (frotz: the frame region sits in
        # the interpreter's own stack array). v3 games raw-pointer their
        # GLOBALS tables (e.g. Zork I keeps parse tables in g168+); frame
        # locals placed in the globals region collided with and corrupted
        # those tables. Top-level locals (no frame) = data-stack words,
        # exactly like frotz (fp at stack top: local i = fp - i).
        if x < 16:
            f = self.frames[-1] if self.frames else None
            if f is not None:
                return self.trunc(f.locals[x - 1]) if x - 1 < len(f.locals) else 0
            a = self.mem.stack_top - (x - 1) * self.width
            return self.trunc(self.mem.getw(a))
        return self.trunc(self.mem.getw(self.globals_base + (x - 16) * self.width))

    def fetch(self, x):
        """Direct read: 0 pops the stack, 1..15 locals, 16+ globals."""
        if x == 0:
            return self._pop()
        return self._readvar(x)

    def xfetch(self, x):
        """In-place read: 0 peeks the stack top without popping (ZSpec §4.4)."""
        if x == 0:
            a = self.sp + self.width
            return s16(self.mem.getw(a))
        return self._readvar(x)

    def _putvar(self, x, v):
        if x < 16:
            f = self.frames[-1] if self.frames else None
            if f is not None:
                if x - 1 < len(f.locals):
                    f.locals[x - 1] = self.trunc(v)
            else:
                a = self.mem.stack_top - (x - 1) * self.width
                self._put(a, v)
        else:
            self._put(self.globals_base + (x - 16) * self.width, v)

    def xstore(self, x, y):
        """In-place write: 0 overwrites the stack top (ZSpec §4.4)."""
        if x == 0:
            self._put(self.sp + self.width, y)
        else:
            self._putvar(x, y)

    def _push(self, v):
        self._put(self.sp, v)
        self.sp -= self.width

    def _pop(self):
        # stack grows down: sp points at the next free slot, top value at sp+width
        # (reading at sp returned the slot BELOW the top — stale garbage values)
        a = self.sp + self.width
        v = s16(self.mem.getw(a))
        self.sp += self.width
        return v

    def op_store(self, y):
        """Store-form opcodes: result -> trailing store byte.
        Store byte 0 (sp) PUSHES (ZSpec §6.3); other bytes store in place."""
        x = self._pcgetb()
        if x == 0:
            self._push(y)
        else:
            self._putvar(x, y)

    # ------------------------------------------------- decode / dispatch
    def _decode(self):
        """Decode one instruction -> (key, operands, nops).
        Key per ZSpec §4.3: 2OP/variable-2OP opnum 0..31, 1OP 128..159,
        0OP full byte 176..191, VAR 224..255, EXT 256+N. Key -1 = illegal."""
        m = self.mem
        inst = m.getb(self.pc)
        self.pc += 1
        if inst < 0x80:
            # long form (ZSpec §4.3.2): always 2OP; bit 6 = op0 type,
            # bit 5 = op1 type (0 = small constant, 1 = variable)
            o0 = self._pcfetch() if inst & 0x40 else self._pcgetb()
            o1 = self._pcfetch() if inst & 0x20 else self._pcgetb()
            return inst & 0x1F, [o0, o1], 2
        if inst < 0xC0:
            # short form (ZSpec §4.3.1)
            if inst == 0xBE:  # 190: extended prefix (all versions; frotz
                # handles it in one opcode table for V1-V8)
                opnum = m.getb(self.pc)
                self.pc += 1
                t = m.getb(self.pc)
                self.pc += 1
                # frotz __extended__ + load_all_operands: break at the
                # first omitted pair (nothing fetched after it)
                ops_list = []
                for s in (6, 4, 2, 0):
                    tt = (t >> s) & 3
                    if tt == 3:
                        break
                    ops_list.append(self._opfetch(tt))
                return ops.EXT_BASE + opnum, ops_list, len(ops_list)
            t = (inst >> 4) & 3
            if t == 3:
                # 0OP ($B0-$BF): opnum is the full byte, no operand bytes
                return inst, [], 0
            key = 0x80 + (inst & 0x0F)
            if t == 0:
                return key, [self._pcget()], 1
            if t == 1:
                return key, [self._pcgetb()], 1
            return key, [self._pcfetch()], 1
        # variable form (ZSpec §4.3.3): operand types in the next byte(s);
        # frotz load_all_operands BREAKS at the first omitted pair, so
        # nothing is fetched after it (don't decode past the end).
        t = m.getb(self.pc)
        self.pc += 1
        ops_list = []
        specs = [t]
        if inst in (0xEC, 0xFA):  # call_vs2 / call_vn2: a second type byte
            t2 = m.getb(self.pc)
            self.pc += 1
            specs.append(t2)
        for spec in specs:
            for s in (6, 4, 2, 0):
                tt = (spec >> s) & 3
                if tt == 3:
                    break
                ops_list.append(self._opfetch(tt))
        key = inst & 0x1F if inst < 0xE0 else inst
        return key, ops_list, len(ops_list)

    def run_until_input(self):
        """Run until needs_input, done, or an unrecoverable error (INV2).

        A read blocked on empty input is parked in self.pending (fully
        decoded operands — the timer-routine call side effects of aread
        must happen exactly once); feed() + run_until_input resumes it
        without re-decoding."""
        self.needs_input = False
        while not (self.done or self.needs_input):
            self.instrs += 1
            if self.instrs > INSTRUCTION_LIMIT:
                self.events.append(Error("instruction limit exceeded"))
                self.done = True
                return
            if self.pending is not None:
                if self.input.empty:  # still nothing to read: keep parked
                    self.needs_input = True
                    return
                inst, operands, nops, self.pc_save = self.pending
                self.pending = None
            else:
                self.pc_save = self.pc
                inst, operands, nops = self._decode()
                blocked = (inst == 228 and self.input.empty) or \
                          (inst == 246 and not self._rc_line_buf
                           and self.input.empty)
                if blocked:
                    if inst == 228 and self.version < 4:
                        # frotz z_read_line (input.c): "Draw status line
                        # for V1 to V3 games" — redraw the status row
                        # before every LINE read (z_read_char does NOT,
                        # so a single-key confirm prompt does not
                        # re-emit the status row).
                        self._emit_v3_status()
                    self._before_read()
                    self.pending = (inst, operands, nops, self.pc_save)
                    self.needs_input = True
                    return
            handler = self._handlers.get(inst)
            if inst < 0 or handler is None:
                if inst >= ops.EXT_BASE:
                    # frotz __extended__ (process.c:641): opcodes outside
                    # its 29-entry table (0x1D-0xFF) and table slots a text
                    # interpreter doesn't implement (picture/mouse/undo/
                    # menu/colour) consume their operands and do nothing
                    # observable — NOT a runtime error.
                    # Trailing byte: frotz's handlers for result-bearing ext
                    # opcodes call store()/branch() — consume the store byte
                    # and report failure (0); no-result opcodes and the
                    # reserved range have none.
                    if inst - ops.EXT_BASE in _EXT_TRAILING:
                        self.op_store(0)
                    continue
                self.raise_err(ops.ERR_ILLEGAL_OPCODE)
                continue
            handler(operands, nops)
            if self._more_pending:
                # 23rd line flushed: dfrotz prints ***MORE*** (no
                # trailing newline) and reads a line it discards.
                self._more_pending = False
                self._at_more = True
                self._flush_at_seam()
                self.events.append(Text("***MORE***"))
                self.needs_input = True
                return
        if self.done:
            self._flush_at_seam(final=True)

    # ------------------------------------------------- control flow
    def _predicate(self, p):
        """Consume the branch suffix; branch iff flip == truthy (ZSpec §4.7)."""
        x = self._pcgetb()
        flip = bool(x & 0x80)
        if x & 0x40:
            off = x & 0x3F
        else:
            off = ((x & 0x3F) << 8) | self._pcgetb()
        if not (flip == bool(p)):
            return
        if off in (0, 1):
            self._ret(off)
            return
        if off & 0x2000:
            off -= 0x4000
        self.pc += off - 2  # target = end-of-instruction + off - 2

    def _pop_frame(self):
        f = self.frames.pop()
        if f.catch_n:  # tokens are LIFO-grouped per live frame
            del self.catch_stack[-f.catch_n:]
        return f

    def _ret(self, value):
        if not self.frames:
            # Returning from the top-level routine ends the game
            self.events.append(EndOfGame(0))
            self.done = True
            self.done_status = 0
            return
        f = self._pop_frame()
        self.sp = f.sp
        self.pc = f.return_pc
        if not f.discard:
            self.op_store(value)

    def raise_err(self, n):
        """Raise Z-machine error n: unwind to the innermost catch, else stop."""
        self.error = n
        if not self.catch_stack:
            self.events.append(Error(f"error {n}"))
            self.done = True
            self.done_status = n
            return
        token = self.catch_stack[-1]
        while len(self.frames) > token:
            self._pop_frame()
        f = self.frames[-1]
        self.sp = f.sp
        self.pc = f.return_pc
        self.op_store(n)

    def _warn(self, errnum):
        """frotz object-0 diagnostic (ERR_REPORT_ONCE): print the warning
        ONCE per error number, through the normal output stream (so it
        wraps / MORE-pauses like game text). No effect on pc/store."""
        if errnum in self._err_warned:
            return
        self._err_warned.add(errnum)
        self._emit(f"Warning: {_ERR_MESSAGES[errnum]} "
                   f"(PC = {self.pc & 0xFFFF:x}) "
                   "(will ignore further occurrences)\n")

    # ------------------------------------------------- branches (2OP:1-7)
    def op_je(self, o, n):
        p = o[0] == o[1] or (n > 2 and o[0] == o[2]) or (n == 4 and o[0] == o[3])
        self._predicate(p)

    def op_jl(self, o, n):
        self._predicate(o[0] < o[1])

    def op_jg(self, o, n):
        self._predicate(o[0] > o[1])

    def op_jz(self, o, n):
        self._predicate(not o[0])

    def op_dec_chk(self, o, n):
        x = self.trunc(self.xfetch(o[0]) - 1)
        self.xstore(o[0], x)
        self._predicate(x < o[1])

    def op_inc_chk(self, o, n):
        x = self.trunc(self.xfetch(o[0]) + 1)
        self.xstore(o[0], x)
        self._predicate(x > o[1])

    def op_test(self, o, n):
        self._predicate((o[0] & o[1]) == o[1])

    # ------------------------------------------------- arithmetic
    def op_or(self, o, n):
        self.op_store(o[0] | o[1])

    def op_and(self, o, n):
        self.op_store(o[0] & o[1])

    def op_not(self, o, n):
        self.op_store(~o[0])

    def op_add(self, o, n):
        self.op_store(o[0] + o[1])

    def op_sub(self, o, n):
        self.op_store(o[0] - o[1])

    def op_mul(self, o, n):
        self.op_store(o[0] * o[1])

    def op_div(self, o, n):
        if o[1] == 0:
            self.raise_err(ops.ERR_DIV_ZERO)
            return
        self.op_store(cdiv(o[0], o[1]))

    def op_mod(self, o, n):
        if o[1] == 0:
            self.raise_err(ops.ERR_DIV_ZERO)
            return
        self.op_store(cmod(o[0], o[1]))

    def op_art_shift(self, o, n):
        places = o[1]
        if places >= 0:
            self.op_store((o[0] << places) & 0xFFFF)
        else:
            self.op_store(o[0] >> -places)

    def op_log_shift(self, o, n):
        places = o[1]
        if places >= 0:
            self.op_store(((o[0] & 0xFFFF) << places) & 0xFFFF)
        else:
            self.op_store((o[0] & 0xFFFF) >> -places)

    # ------------------------------------------------- stack / variables
    def op_push(self, o, n):
        self._push(o[0])

    def op_pull(self, o, n):
        self.xstore(o[0], self._pop())

    def op_pop(self, o, n):
        # v1-v4 0OP:185 pop: discard the stack top, no operand (frotz
        # op0_opcodes[0x09] = z_pop for h_version <= V4; 0xB9 = catch v5+).
        self._pop()

    def op_inc(self, o, n):
        # operand 0 = top of stack, in place (ZSpec §4.2.2); xstore covers
        # stack/locals/globals like op_pull
        self.xstore(o[0], self.trunc(self.xfetch(o[0]) + 1))

    def op_dec(self, o, n):
        self.xstore(o[0], self.trunc(self.xfetch(o[0]) - 1))

    def op_load(self, o, n):
        self.op_store(self.xfetch(o[0]))

    def op_store_op(self, o, n):
        self.xstore(o[0], o[1])

    def op_loadw(self, o, n):
        # address wraps mod 2^16 (frotz LOW_WORD): operand values >= 0x8000
        # are negative as s16; an unmasked sum would go negative and Memory's
        # OOB guard would silently return 0.
        self.op_store(self.mem.getw((o[0] + o[1] * 2) & 0xFFFF))

    def op_storew(self, o, n):
        self.mem.putw((o[0] + o[1] * 2) & 0xFFFF, o[2])

    def op_loadb(self, o, n):
        self.op_store(self.mem.getb((o[0] + o[1]) & 0xFFFF))

    # ------------------------------------------------ object tree / properties
    def _obj_field(self, obj, off):
        if obj == 0:
            return 0
        base = self.objects_base + obj * self.obj_size + off
        return self.mem.getb(base) if self.version == 3 else self.mem.getw(base)

    def _set_obj_field(self, obj, off, val):
        if obj == 0:
            return
        base = self.objects_base + obj * self.obj_size + off
        if self.version == 3:
            self.mem.putb(base, val & 0xFF)
        else:
            self.mem.putw(base, self.trunc(val))

    def _get_parent(self, obj):
        return self._obj_field(obj, self._off_parent)

    def _get_sibling(self, obj):
        return self._obj_field(obj, self._off_sibling)

    def _get_child(self, obj):
        return self._obj_field(obj, self._off_child)

    def _get_prop_addr(self, obj):
        if obj == 0:
            return 0
        return self.mem.getw(self.objects_base + obj * self.obj_size + self._off_propaddr)

    def _flagset(self, obj, attr):
        """(word-addr, bit-mask, current-word) for an attribute (dork flagset).
        MSB-first within the word; returns None for object 0."""
        if obj == 0:
            return None
        mask = 1 << (15 & ~attr)
        addr = self.objects_base + obj * self.obj_size + (attr >> 4) * 2
        return addr, mask, self.mem.getw(addr)

    def _prop_layout(self, header):
        """Property header -> (num, size, data-offset). v3: 1 byte (size-1)<<5|num;
        v4+: 1-2 bytes, high bit = 2-byte form (size byte 0 = 64), bit 6 = size 2."""
        b1 = self.mem.getb(header)
        if self.version == 3:
            return b1 & 31, (b1 >> 5) + 1, 1
        num = b1 & 0x3F
        if b1 & 0x80:
            return num, (self.mem.getb(header + 1) & 0x3F) or 64, 2
        return num, 2 if b1 & 0x40 else 1, 1

    def _propfind(self, obj, num):
        """(data-addr, size) of property num on object obj, else None.
        Table: [name-len][name] then headers in descending num order, 0-terminated."""
        if obj == 0:
            return None
        z = self._get_prop_addr(obj)
        z += self.mem.getb(z) * 2 + 1  # skip short name
        while z and self.mem.getb(z):
            pnum, psize, doff = self._prop_layout(z)
            if pnum == num:
                return z + doff, psize
            z += doff + psize
        return None

    # --- public object API (tests + future tasks; opcodes use the private
    # forms above, same code paths)
    def obj_entry(self, obj):
        """Byte address of object obj's table entry (ZMS sect12.3)."""
        return self.objects_base + obj * self.obj_size

    def get_parent(self, obj):
        return self._obj_field(obj, self._off_parent)

    def get_sibling(self, obj):
        return self._obj_field(obj, self._off_sibling)

    def get_child(self, obj):
        return self._obj_field(obj, self._off_child)

    def test_attr(self, obj, attr):
        f = self._flagset(obj, attr)
        return bool(f and f[2] & f[1])

    def get_prop(self, obj, prop):
        """op_get_prop value semantics: 1-byte props zero-extend; missing
        props fall back to the defaults table (defprop + 2*prop)."""
        f = self._propfind(obj, prop)
        if f:
            return self.mem.getw(f[0]) if f[1] == 2 else self.mem.getb(f[0])
        return self.mem.getw(self.defprop + 2 * prop)

    def get_prop_addr(self, obj, prop):
        f = self._propfind(obj, prop)
        return f[0] if f else 0

    def _move_obj(self, x, y):
        if x == 0:
            return
        p = self._get_parent(x)
        if p:
            if self._get_child(p) == x:
                self._set_obj_field(p, self._off_child, self._get_sibling(x))
            else:
                z = self._get_child(p)
                w = 0
                while z != x:
                    w = z
                    z = self._get_sibling(z)
                self._set_obj_field(w, self._off_sibling, self._get_sibling(x))
        self._set_obj_field(x, self._off_parent, y)
        if y:
            self._set_obj_field(x, self._off_sibling, self._get_child(y))
            self._set_obj_field(y, self._off_child, x)
        else:
            self._set_obj_field(x, self._off_sibling, 0)

    def op_jin(self, o, n):
        if (o[0] & 0xFFFF) == 0:
            self._warn(19)
        self._predicate(self._get_parent(o[0] & 0xFFFF) == o[1] & 0xFFFF)

    def op_test_attr(self, o, n):
        if (o[0] & 0xFFFF) == 0:
            self._warn(28)
        fs = self._flagset(o[0] & 0xFFFF, o[1] & 0xFFFF)
        self._predicate(bool(fs) and bool(fs[2] & fs[1]))

    def op_set_attr(self, o, n):
        if (o[0] & 0xFFFF) == 0:
            self._warn(27)
        fs = self._flagset(o[0] & 0xFFFF, o[1] & 0xFFFF)
        if fs:
            self.mem.putw(fs[0], self.trunc(fs[2] | fs[1]))

    def op_clear_attr(self, o, n):
        if (o[0] & 0xFFFF) == 0:
            self._warn(26)
        fs = self._flagset(o[0] & 0xFFFF, o[1] & 0xFFFF)
        if fs:
            self.mem.putw(fs[0], self.trunc((fs[2] & ~fs[1]) & 0xFFFF))

    def op_insert_obj(self, o, n):
        x, y = o[0] & 0xFFFF, o[1] & 0xFFFF
        if x == 0:
            self._warn(29)
            return
        if y == 0:
            # frotz z_move_object: moving INTO object 0 is an error and a
            # no-op (NOT a removal — that is @remove_object).
            self._warn(30)
            return
        self._move_obj(x, y)

    def op_get_prop(self, o, n):
        obj, prop = o[0] & 0xFFFF, o[1] & 0xFFFF
        if obj == 0:
            self._warn(24)
            self.op_store(0)
            return
        f = self._propfind(obj, prop)
        if f:
            val = self.mem.getw(f[0]) if f[1] == 2 else self.mem.getb(f[0])
        else:
            val = self.mem.getw(self.defprop + 2 * prop)
        self.op_store(val)

    def op_get_prop_addr(self, o, n):
        if (o[0] & 0xFFFF) == 0:
            self._warn(23)
        f = self._propfind(o[0] & 0xFFFF, o[1] & 0xFFFF)
        self.op_store(f[0] if f else 0)

    def op_put_prop(self, o, n):
        # VAR:227 put_prop object property value (dork PUTP): 2-byte slot ->
        # word write, else byte write; propfind miss -> byte to addr 0 (dork).
        obj, prop, val = o[0] & 0xFFFF, o[1] & 0xFFFF, o[2]
        if obj == 0:
            self._warn(25)
            return
        f = self._propfind(obj, prop)
        if f and f[1] == 2:
            self.mem.putw(f[0], val)
        else:
            self.mem.putb(f[0] if f else 0, val)

    def op_get_next_prop(self, o, n):
        obj = o[0] & 0xFFFF
        if obj == 0:
            self._warn(32)
            self.op_store(0)
            return
        if o[1] & 0xFFFF:
            f = self._propfind(obj, o[1] & 0xFFFF)
            after = f[0] + f[1] if f else 0
            self.op_store(self._prop_layout(after)[0] if self.mem.getb(after) else 0)
        else:
            x = self._get_prop_addr(obj)
            first = x + self.mem.getb(x) * 2 + 1
            self.op_store(self._prop_layout(first)[0] if self.mem.getb(first) else 0)

    def op_get_parent(self, o, n):
        if (o[0] & 0xFFFF) == 0:
            self._warn(21)
        self.op_store(self._get_parent(o[0] & 0xFFFF))

    def op_get_sibling(self, o, n):
        # 1OP:129 NEXT?: store result, then ?(label) branch if nonzero (ZSpec §14)
        if (o[0] & 0xFFFF) == 0:
            self._warn(22)
        x = self._get_sibling(o[0] & 0xFFFF)
        self.op_store(x)
        self._predicate(x != 0)

    def op_get_child(self, o, n):
        # 1OP:130 FIRST?: store result, then ?(label) branch if nonzero (ZSpec §14)
        if (o[0] & 0xFFFF) == 0:
            self._warn(20)
        x = self._get_child(o[0] & 0xFFFF)
        self.op_store(x)
        self._predicate(x != 0)

    def op_get_prop_len(self, o, n):
        data = (o[0] & 0xFFFF) & 0xFFFF
        if data == 0:
            self.op_store(0)  # Z-Machine 1.1 / Praxix: get_prop_len 0 = 0
            return
        b1 = self.mem.getb(data - 1)
        if self.version == 3:
            self.op_store((b1 >> 5) + 1)
        else:
            self.op_store(self._prop_layout(data - 2 if b1 & 0x80 else data - 1)[1])

    def op_remove_obj(self, o, n):
        x = o[0] & 0xFFFF
        if x == 0:
            self._warn(31)
            return
        self._move_obj(x, 0)

    def op_print_obj(self, o, n):
        obj = o[0] & 0xFFFF
        if obj:
            pa = self._get_prop_addr(obj)
            if pa:
                s, _ = decode_text(self.mem, self.fwords, pa + 1,
                                   self.zscii_extra, self.alphabet)
                self._emit(s)

    def op_storeb(self, o, n):
        self.mem.putb((o[0] + o[1]) & 0xFFFF, o[2])

    def op_copy_table(self, o, n):
        # VAR:253 copy_table src dst size (dork): dst 0 = zero the source
        # region; size<0 = forced forward byte copy; size>=0 = memmove.
        src, dst = o[0] & 0xFFFF, o[1] & 0xFFFF
        size, nm = s16(o[2]), abs(s16(o[2]))
        if dst == 0:
            for i in range(nm):
                self.mem.putb(src + i, 0)
        elif size < 0:
            for i in range(nm):
                self.mem.putb((dst + i) & 0xFFFF, self.mem.getb((src + i) & 0xFFFF))
        else:
            buf = [self.mem.getb((src + i) & 0xFFFF) for i in range(nm)]
            for i, b in enumerate(buf):
                self.mem.putb((dst + i) & 0xFFFF, b)

    # ------------------------------------------------- calls / return
    def _do_call(self, o, n, store_result):
        if n == 0 or o[0] == 0:
            if store_result:
                self.op_store(0)
            return
        if len(self.frames) >= ops.MAX_CALL_DEPTH:
            self.raise_err(ops.ERR_STACK_OVERFLOW)
            return
        fn = (o[0] & 0xFFFF) * self.pack_mult
        nlocals = self.mem.getb(fn)
        nargs = min(n - 1, nlocals)
        # v1-v4 routines keep default local values in the header (frotz:
        # h_version <= V4 consumes nlocals 2-byte defaults from the stream);
        # v5+ defaults are 0. Args fill locals 1..nargs, rest get defaults.
        defaults = ([self.mem.getw(fn + 1 + i * 2) for i in range(nlocals)]
                    if self.version <= 4 else [0] * nlocals)
        locals_ = [o[1 + i] if i < nargs else defaults[i] for i in range(nlocals)]
        self.frames.append(Frame(self.pc, locals_, nlocals, n - 1,
                                 self.sp, not store_result))
        self.pc = fn + 1 + (nlocals * 2 if self.version <= 4 else 0)

    def op_call_1s(self, o, n):
        self._do_call(o, n, True)

    def op_call_1n(self, o, n):
        self._do_call(o, n, False)

    def op_call_2s(self, o, n):
        self._do_call(o, n, True)

    def op_call_2n(self, o, n):
        self._do_call(o, n, False)

    def op_call_vs(self, o, n):
        self._do_call(o, n, True)

    def op_call_vn(self, o, n):
        self._do_call(o, n, False)

    def op_call_vs2(self, o, n):
        self._do_call(o, n, True)

    def op_call_vn2(self, o, n):
        self._do_call(o, n, False)

    def op_ret(self, o, n):
        # 1OP:139 ret value: the operand is variable-by-value (ZSpec §4.2.3) —
        # o[0] is already the value (dork: ret(op0)); do NOT re-fetch.
        self._ret(o[0])

    def op_ret_popped(self, o, n):
        # ZSpec: "pops top of stack and returns that" (equivalent to ret sp)
        self._ret(self._pop())

    def op_jump(self, o, n):
        # ZSpec §15: NOT an absolute label — pc-relative, 2-byte signed offset
        # (dork case 140: pc += op0 - 2)
        self.pc += o[0] - 2

    def op_print_table(self, o, n):
        # ZSpec §15 / dork case 254: raw ZSCII bytes at a BYTE address (no
        # packing), 13 -> newline, newline between rows but not after last.
        width = o[1] if n > 1 else 0
        height = o[2] if n > 2 else 1
        skip = o[3] if n > 3 else 0
        p = o[0] & 0xFFFF
        parts = []
        for row in range(height):
            line = "".join(chr(self.mem.getb(p + i)) for i in range(width))
            line = line.replace("\r", "\n")
            parts.append(line + ("\n" if row < height - 1 else ""))
            p += width + skip
        self._emit("".join(parts))

    def op_check_arg_count(self, o, n):
        nargs = self.frames[-1].n_args if self.frames else 0
        self._predicate(o[0] <= nargs)

    # ------------------------------------------------- catch / throw / err
    def op_catch(self, o, n):
        self.catch_stack.append(len(self.frames))
        self.frames[-1].catch_n += 1
        self.op_store(len(self.frames))

    def op_throw(self, o, n):
        value, token = o[0], o[1]
        if not (1 <= token <= len(self.frames)):
            self.raise_err(ops.ERR_UNCAUGHT_THROW)
            return
        while len(self.frames) > token:
            self._pop_frame()
        f = self.frames[-1]
        self.sp = f.sp
        self.pc = f.return_pc
        self.op_store(value)

    # ------------------------------------------------- RNG (frotz 2.55 src/common/random.c)
    # Startup/restart: seed_random(os seed) -> A = seed, standard mode.
    # In-game `random -S`: 0 < S < 1000 -> special mode cycling 0..S-1;
    # S >= 1000 -> A = S. Standard: A = 0x015a4e35*A + 1 (32-bit),
    # result = (A >> 16) & 0x7fff; random K stores result % K + 1.
    # (The old +1013904223 LCG diverged from frotz; seed 10 in planetfall
    # routes the ambassador event differently.)
    def _seed_rng(self, value):
        # frotz seed_random (random.c): 0 = OS seed, 1-999 = special
        # (interval) mode, >= 1000 = standard A seed.
        if value == 0:
            self._rng_a = self.seed0
            self._rng_interval = 0
        elif value < 1000:
            self._rng_interval = value
        else:
            self._rng_a = value & 0xFFFFFFFF
            self._rng_interval = 0
        self._rng_counter = 0

    def op_random(self, o, n):
        k = o[0]
        if k <= 0:  # reseed (0 = interpreter os seed)
            self._seed_rng(-k)
            self.op_store(0)
            return
        if self._rng_interval:
            r = self._rng_counter
            self._rng_counter = (r + 1) % self._rng_interval
        else:
            self._rng_a = (0x015A4E35 * self._rng_a + 1) & 0xFFFFFFFF
            r = (self._rng_a >> 16) & 0x7FFF
        self.op_store(r % k + 1)

    # ------------------------------------------------- print family
    def _emit(self, s):
        if not s:
            return
        if self._stream3:
            self._stream3_buf.extend(s)
            return
        if self._win == 1:
            # Status line: write into the 80-col buffer at the cursor,
            # tracking cell changes vs the last on-screen content
            # (frotz dumb_set_cell semantics — see _status_ref).
            i = self._status_col
            for ch in s:
                if ch == "\n":
                    continue
                if i < 80:
                    if ch != self._status_ref[i]:
                        self._status_changed = True
                    self._status[i] = ch
                i += 1
            self._status_col = i
            self._status_dirty = True
            return
        # Main window: buffer lines; they flush at the next read (dfrotz -t
        # screen model). A `>` at column 0 is the game's read prompt; it
        # stays as line content (later text extends the same line, e.g.
        # '>[I don't know ...]' or '> Deck Nine ...' when the status line
        # merges in at the next seam).
        # frotz counts every screen row the cursor completes — explicit
        # newlines AND soft wraps — and pauses the machine at the 23rd
        # (***MORE***), even mid-print: the rest of the batch is held in
        # _emit_rest and re-emitted after the prompt line is consumed.
        text, self._col = self._wrap(s, self._col)
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\n":
                self._lines.append(self._line)
                self._line = ""
                i += 1
                self._line_count += 1
                self._row = min(self._row + 1, 23)  # scroll keeps the
                # cursor on the bottom row (frotz os_scroll_area)
                if self._line_count >= 23:
                    self._more_pending = True
                    self._emit_rest = text[i:]
                    return
            else:
                self._line += ch
                i += 1

    def _status_line(self):
        """The status buffer rstripped if any of its cells changed since
        the last emission, else None (frotz dumb terminal: a cell is
        'changed' only when the written value differs — an identical
        re-draw re-emits nothing). The buffer is PERSISTENT (the game
        updates it in place), so never clear it here."""
        if not self._status_dirty:
            return None
        self._status_dirty = False
        changed = self._status_changed
        self._status_changed = False
        self._status_ref = list(self._status)  # row content is now this
        if not changed:
            return None
        if not any(c != " " for c in self._status):
            return None  # the row was cleared: nothing to show
        return "".join(self._status).rstrip()

    def _flush_at_seam(self, final=False):
        """dfrotz -t flush at a read (or machine end), mirroring the dumb
        terminal's SPANS emission (verified against instrumented frotz 2.55):

        - the status row (if the game wrote it since the last flush) goes
          first, followed by one blank line only when the first content
          row is NOT adjacent to it (a content row is adjacent when its
          batch started at the main-window top; v3 status blocks sit far
          above the main window, so v3 content is never adjacent);
        - leading blank rows are never emitted (SPANS starts at the first
          changed row; the screen's top rows were blank at start);
        - a blank row is emitted (as a bare newline) only if a non-blank row
          follows it (SPANS: row r is shown when row r+1 changed);
        - the cursor row — the in-progress line at the seam — is emitted
          BARE (no trailing newline) by the prompt path, so in the byte
          stream it merges with the next seam's first emission
          ('> <status>'); a completed last row (the batch ended with a
          newline) gets a newline;
        - final=True (machine end) forces the trailing newline.
        """
        status = self._status_line()
        lines = list(self._lines)
        cursor_row = bool(self._line)
        if self._line:
            lines.append(self._line)
        self._lines, self._line, self._col = [], "", 0
        # SPANS starts at the first changed row: leading blank rows never
        # reach stdout.
        i = 0
        while i < len(lines) and not lines[i].rstrip():
            i += 1
        raw = lines[i:]
        lines = [l.rstrip() for l in raw]
        if status is not None:
            # One blank line between the status row and the content only
            # when the first content row is not adjacent to the status
            # row (see _batch_row); adjacent content follows the status
            # row directly in the byte stream.
            blank = bool(lines) and (self.version < 4 or self._batch_row != 0)
            self.events.append(Text(status + "\n" + ("\n" if blank else "")))
        for k, l in enumerate(lines):
            last = k == len(lines) - 1
            if not l:
                if last or not lines[k + 1]:
                    continue
                self.events.append(Text("\n"))
            elif last and cursor_row and not final:
                # Bare prompt row: the exact cells before the cursor —
                # frotz prints them verbatim, trailing spaces included.
                self.events.append(Text(raw[k]))
            else:
                self.events.append(Text(l + "\n"))
        # The next line buffer starts at the current cursor row.
        self._batch_row = self._row

    def _before_read(self):
        """Runs when the machine blocks on input (ZSpec §7.1.1.1, §8.2.4).
        frotz 2.55 does NOT auto-redraw the v3 status line before a read:
        it is drawn only when the game executes @show_status (v3 table
        entry 49), so the status row is re-emitted at a seam only if the
        game actually redrew it since the last flush."""
        self._flush_at_seam()

    def _emit_v3_status(self):
        if self.mem.getb(1) & 0x10:
            return  # CONFIG_NOSTATUSLINE: the interpreter draws no status line
        # v3 status line (frotz z_show_status, mirrored column-exactly):
        # ' ' + g16 short name, padded so 'Score: ' starts at col 50 and
        # 'Moves: ' at col 66 (0-based); time games: 'Time: ' at col 60,
        # hours = (g17+11)%12+1, minutes zero-padded, am/pm from g17>=12.
        # g16/g17/g18 = frotz's global0/global1/global2 (h_globals + 0/2/4).
        obj = self._readvar(16)
        name = ""
        if obj:
            pa = self._get_prop_addr(obj)
            if pa:
                name, _ = decode_text(self.mem, self.fwords, pa + 1,
                                      self.zscii_extra, self.alphabet)
        score, moves = self._readvar(17), self._readvar(18)
        line = " " + name
        if self.mem.getb(1) & 2:  # CONFIG_TIME
            hours = (score + 11) % 12 + 1
            line = line.ljust(60)
            line += "Time: " + (f" {hours}" if hours < 10 else str(hours)) + ":"
            line += ("0" if moves < 10 else "") + str(moves)
            line += " " + ("p" if score >= 12 else "a") + "m"
        else:
            line = line.ljust(50)
            line += "Score: " + str(score)
            line = line.ljust(66)
            line += "Moves: " + str(moves)
        for i in range(80):
            ch = line[i] if i < len(line) else " "
            if ch != self._status_ref[i]:
                self._status_changed = True
            self._status[i] = ch
        self._status_dirty = True

    # ------------------------------------------------- input / read family
    def feed(self, line):
        """Feed one input line (encoded ZSCII + 13). dfrotz -t does not
        echo piped input, so no echo events are modelled."""
        if self._at_more:
            # frotz dumb_read_misc_line: the line typed at ***MORE*** is
            # read and discarded; the machine then resumes (the game's
            # own @read consumes the NEXT line) — and the held tail of
            # the interrupted print goes out first.
            self._at_more = False
            self._line_count = 0
            if self._emit_rest:
                rest = self._emit_rest
                self._emit_rest = ""
                self._emit(rest)
            return
        self.input.feed(line)

    def _consume_line(self):
        """Drain input up to (not including) the CR; lowercase it
        (ZSpec: v1-4 explicit; dork lowercases all versions). The seam
        guarantees a CR is present (feed always appends one)."""
        out = []
        while True:
            c = self.input.get()
            if c in (0, 13):
                break
            out.append(c + 32 if 65 <= c <= 90 else c)
        return out

    def _read_screen_newline(self):
        # frotz console_read_input: a normal (RETURN) read zeroes line_count
        # then calls screen_new_line() — the cursor drops one row (clamped at
        # the bottom, i.e. scrolling) and line_count becomes 1. Model that row
        # so the ***MORE*** count and the physical row layout match dfrotz.
        # (No text is emitted for it; it only moves the cursor.)
        self._row = min(self._row + 1, 23)
        self._line_count = 1
        self._col = 0

    def op_read(self, o, n):
        # VAR:228: v3 sread text parse (no store); v5 aread text parse
        # time routine -> (result). Timers are unsupported: the v4+
        # time/routine operands were evaluated at decode time (their call
        # side effects happen exactly once — that is why the pending tuple
        # stores decoded operands), and a read never times out, so the
        # v5 result is always 13 (the CR terminator) — matching frotz
        # (stores the terminator key) and dork (store(13)); ZSpec's "10"
        # is only an author recommendation.
        t1 = o[0] & 0xFFFF
        t2 = o[1] & 0xFFFF
        codes = self._consume_line()
        self._read_screen_newline()  # frotz console_read_input post-read newline
        if self.version >= 5:
            # byte0 = max chars; byte1 = count written; chars at byte2..
            # Leftover rule: byte1 > 0 on entry = chars left from an
            # interrupted previous input; new chars go after them.
            maxn = self.mem.getb(t1)
            left = min(self.mem.getb(t1 + 1), maxn)
            new = codes[:maxn - left]
            for i, c in enumerate(new):
                self.mem.putb(t1 + 2 + left + i, c)
            self.mem.putb(t1 + 1, left + len(new))
            self._tokenise(t1, t2)
            self.op_store(13)
        else:
            # v1-4: byte0 = max-1 preset; chars at byte1.., 0-terminated.
            # Prior contents are STALE — replace them (dork handleInput:
            # slice(0, bytes[t1]-1), write at t1+1.., 0-terminate). The
            # v5 leftover rule above does not apply: it models interrupted
            # (timed-out) reads, and a v3 sread always starts fresh.
            maxn = self.mem.getb(t1)
            new = codes[:maxn - 1]
            for j, c in enumerate(new):
                self.mem.putb(t1 + 1 + j, c)
            self.mem.putb(t1 + 1 + len(new), 0)
            self._tokenise(t1, t2)

    def op_read_char(self, o, n):
        # VAR:246: one keypress -> its ZSCII code. o[0] must be 1
        # (keyboard); v4+ o[1], o[2] = timer (unsupported; evaluated at
        # decode time like aread's).
        # frotz dumb os_read_key (dinput.c): when its per-line buffer is
        # empty it reads a WHOLE line and strips the CR, then returns the
        # line's chars one at a time. An empty line (CR with no prior char)
        # yields a single CR (13). The block condition (run_until_input)
        # only parks read_char when BOTH this buffer and the input queue
        # are empty, so here the input queue has at least one line.
        if not self._rc_line_buf:
            line = []
            c = 0
            while True:
                c = self.input.get()
                if c in (0, 13):
                    break
                line.append(c)
            if line:
                self._rc_line_buf.extend(line)
            elif c == 13:
                # empty line: we consumed its CR; yield it as 13
                self._rc_line_buf.append(13)
        v = self._rc_line_buf.popleft() if self._rc_line_buf else 0
        self._read_screen_newline()  # frotz console_read_key post-read newline
        self.op_store(v)

    def _tokenise(self, t1, t2):
        """Lexical analysis of the text buffer -> parse table (ZSpec
        §13.6, dork handleInput). Entry layout: {dict-addr u16, len u8,
        text-buffer-offset u8}; t2 = 0 skips it (v5+)."""
        if t2 == 0:
            return
        if self.version >= 5:
            cnt = self.mem.getb(t1 + 1)
            buf = bytes(self.mem.getb(t1 + 2 + i) for i in range(cnt))
            off0 = 2
        else:
            maxn = self.mem.getb(t1)
            end = 1
            while end <= maxn and self.mem.getb(t1 + end) != 0:
                end += 1
            buf = bytes(self.mem.getb(t1 + k) for k in range(1, end))
            off0 = 1
        s = "".join(zscii_to_char(c) for c in buf)
        maxw = self.mem.getb(t2)
        entries = []
        for off, word in self.vocab.split(s):
            if len(entries) >= maxw:
                break  # ZSpec: stop before going beyond the max words
            entries.append((self.vocab.lookup(word), len(word), off0 + off))
        self.mem.putb(t2 + 1, len(entries))
        for i, (addr, ln, off) in enumerate(entries):
            self.mem.putw(t2 + 2 + 4 * i, addr)
            self.mem.putb(t2 + 4 + 4 * i, ln)
            self.mem.putb(t2 + 5 + 4 * i, off)

    def op_scan_table(self, o, n):
        # VAR:247: scan the table for x; store the found address (or 0) and
        # branch if found. v5+ form: bit 7 = word scan, low 7 bits = entry
        # size (0 = 128); the 3-operand form defaults to word scan size 2
        # (dork case 247).
        x = o[0] & 0xFFFF
        table = o[1] & 0xFFFF
        count = max(0, o[2])
        form = o[3] & 0xFF if n > 3 else 0x82
        is_word = bool(form & 0x80)
        size = (form & 0x7F) or 128
        found, a = 0, table
        for _ in range(count):
            v = self.mem.getw(a) if is_word else self.mem.getb(a)
            if v == x:
                found = a
                break
            a = (a + size) & 0xFFFF
        self.op_store(found)
        self._predicate(found != 0)

    def op_tokenise(self, o, n):
        # VAR:251: re-parse the existing text buffer (no new input).
        # The user-dictionary operand and flag (keep unknown slots) are
        # unsupported — dork ignores both as well.
        self._tokenise(o[0] & 0xFFFF, o[1] & 0xFFFF)

    def op_encode_text(self, o, n):
        # VAR:252 (v5+): zscii-text length from coded-text. NOT a store
        # instruction (ZSpec §14 table has no "-> (result)").
        t, length, frm, dst = (o[0] & 0xFFFF, o[1], o[2] & 0xFFFF,
                               o[3] & 0xFFFF)
        if length <= 0:
            return
        end = min(t + frm + length, len(self.mem.mem))
        s = "".join(zscii_to_char(self.mem.getb(k)) for k in range(t + frm, end))
        out = encode_text(s, self.mem, self.version)
        for i, b in enumerate(out):
            self.mem.putb(dst + i, b)

    def op_show_status(self, o, n):
        # 0OP:188 show_status: v3 only; v5+ treats it as nop (Wishbringer
        # v5 rel 23 contains it by accident — ZSpec note). v3: forces the
        # interpreter to update the row-0 status buffer (ZSpec §8.2.4);
        # the text model emits it at the next seam flush (task 8: P2-3
        # resolved against the Zork I oracle — status line orders first at
        # the seam, deduped by content, exactly like the v5 window-1 line).
        if self.version < 4:
            self._emit_v3_status()

    def op_input_stream(self, o, n):
        # VAR:244: dork is a no-op (keyboard only). Non-zero input streams
        # are not modelled (the spec suggests error 587; the reference
        # interpreter in our oracle corpus just ignores them).
        pass

    def _wrap(self, s, col):
        """Soft-wrap at the screen width, word-boundary preferred (frotz -t).
        col = current column mid-line; returns (text, ending column)."""
        width = self.screen_width
        out, cur, col = [], [], col
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == "\n":
                out.append("".join(cur)); cur, col = [], 0
                i += 1
                continue
            if ch == " ":
                # a space landing past the line end is dropped (frotz -t)
                if col < width:
                    cur.append(ch); col += 1
                i += 1
                continue
            j = i
            while j < n and s[j] != " " and s[j] != "\n":
                j += 1
            word = s[i:j]
            if col + len(word) <= width:
                cur.append(word); col += len(word); i = j
            else:
                # frotz also breaks at hyphens when the word doesn't fit
                k = word.rfind("-", 0, width - col)
                if k > 0:
                    prefix = word[:k + 1]
                    cur.append(prefix)
                    col += len(prefix)
                    i += len(prefix)
                    continue
                if col > 0:
                    out.append("".join(cur)); cur, col = [], 0
                while len(word) > width:  # pathological long word: hard split
                    out.append(word[:width]); word = word[width:]
                cur.append(word); col = len(word); i = j
        out.append("".join(cur))
        return "\n".join(out), col

    def op_print(self, o, n):
        s, end = decode_text(self.mem, self.fwords, self.pc,
                             self.zscii_extra, self.alphabet)
        self.pc = end
        self._emit(s)

    def op_print_ret(self, o, n):
        s, end = decode_text(self.mem, self.fwords, self.pc,
                             self.zscii_extra, self.alphabet)
        self.pc = end
        self._emit(s + "\n")
        self._ret(1)

    def op_print_addr(self, o, n):
        s, _ = decode_text(self.mem, self.fwords, o[0] & 0xFFFF,
                           self.zscii_extra, self.alphabet)
        self._emit(s)

    def op_print_paddr(self, o, n):
        # frotz z_print_paddr: decode_text(HIGH_STRING, packed addr) — the
        # byte address is packed * length_divisor in EVERY version (v8: *8);
        # v8 strings are normal 5-bit z-chars, there is no wide form in
        # frotz 2.55.
        p = o[0] & 0xFFFF
        s, _ = decode_text(self.mem, self.fwords, p * self.pack_mult,
                           self.zscii_extra, self.alphabet)
        self._emit(s)

    def op_print_char(self, o, n):
        self._emit(zscii_to_char(o[0], self.zscii_extra))

    def op_print_num(self, o, n):
        self._emit(str(o[0]))

    def op_print_unicode(self, o, n):
        # EXT 11 (v4+): print a Unicode char. frotz z_print_unicode:
        # zargs[0] < 0x20 -> '?', else print_char(zargs[0]). No result.
        cp = o[0] & 0xFFFF
        self._emit("?" if cp < 0x20 else chr(cp))

    def op_check_unicode(self, o, n):
        # EXT 12 (v4+): report whether a Unicode char can be printed (bit 0)
        # and read (bit 1). frotz z_check_unicode (dumb terminal, USE_UTF8
        # build): 0x20-0x7e -> 3 (print+read); 0xa0 -> 1 (print); c >= 0xa1
        # -> mask & os_check_unicode, and dumb os_check_unicode returns 1, so
        # -> 1; else 0. Result goes to the trailing store byte.
        c = o[0] & 0xFFFF
        if 0x20 <= c <= 0x7E:
            self.op_store(3)
        elif c >= 0xA0:
            self.op_store(1)
        else:
            self.op_store(0)

    # ------------------------------------------------- misc / no-ops
    def op_new_line(self, o, n):
        self._emit("\n")

    def op_quit(self, o, n):
        # v1-v3: exit status = global 1 (score) per ZMS/Inform convention;
        # ZSpec 1.0 leaves it unspecified. v4+: 0.
        status = self._readvar(16) if self.version <= 3 else 0
        self.events.append(EndOfGame(status))
        self.done = True
        self.done_status = status

    def op_nop(self, o, n):
        pass

    def op_restart(self, o, n):
        self.mem.reset()
        self._init()

    def op_verify(self, o, n):
        body = self.story.data[0x40:self.story.header.declared_len]
        self._predicate(sum(body) & 0xFFFF == self.story.header.checksum)

    def op_piracy(self, o, n):
        self._predicate(True)

    def op_rtrue(self, o, n):
        # ZSpec 0OP:176: RETURN 1 to the caller (not a push!)
        self._ret(1)

    def op_rfalse(self, o, n):
        # ZSpec 0OP:177: RETURN 0 to the caller
        self._ret(0)

    def op_save_v3(self, o, n):
        assert self.input.empty  # spec §7: the library has consumed the line
        ok = self.save_handler(self._decode_hint(o)) if self.save_handler else False
        self._predicate(ok)

    def op_restore_v3(self, o, n):
        assert self.input.empty
        ok = self.restore_handler(self._decode_hint(o)) if self.restore_handler else False
        self._predicate(ok)

    def op_save_v5(self, o, n):
        assert self.input.empty
        ok = self.save_handler(self._decode_hint(o)) if self.save_handler else False
        self.op_store(1 if ok else 0)

    def op_restore_v5(self, o, n):
        assert self.input.empty
        ok = self.restore_handler(self._decode_hint(o)) if self.restore_handler else False
        # frotz restore_quetzal (fastmem.c): returns 2 on success, 0 on
        # pre-damage failure (-1 = fatal, the game dies before storing)
        self.op_store(2 if ok else 0)

    def _stub_save(self, hint):
        # ponytail: host-side save — ZMSAVE v1 image to a file (in-game
        # SAVE works out of the box; the CLI / Phase 2 server replaces
        # these with slot-based handlers).
        from . import savefile
        path = hint or self._stub_save_path or "zmach-save.bin"
        try:
            with open(path, "wb") as f:
                f.write(savefile.encode(self))
            self._stub_save_path = path
            return True
        except OSError:
            return False

    def _stub_restore(self, hint):
        from . import savefile
        path = hint or self._stub_save_path
        if not path:
            return False
        try:
            with open(path, "rb") as f:
                savefile.decode(self, f.read())
            return True
        except (OSError, SaveFileError):
            return False

    def _decode_hint(self, o):
        if not o:
            return ""
        s, _ = decode_text(self.mem, self.fwords, (o[0] & 0xFFFF) * self.pack_mult,
                           self.zscii_extra, self.alphabet)
        return s

    def op_get_cursor(self, o, n):
        # VAR:240 get_cursor line column [window(v6)]: v4/v5 takes a plain
        # memory address (dork semantics; no packing)
        a = o[0] & 0xFFFF
        self.mem.putw(a, 0)
        self.mem.putw(a + 2, 0)

    def op_set_font(self, o, n):
        # Text mode: only font 1 is available (dork: op0 0/1 -> 1, else 0)
        self.op_store(1 if (o[0] & 0xFFFF) in (0, 1) else 0)

    def op_save_undo(self, o, n):
        self.op_store(-1)

    def op_restore_undo(self, o, n):
        self._predicate(True)

    def op_set_window(self, o, n):
        # VAR:235 set_window window. Window 1 = status line (buffered),
        # 0 = main. The buffer flushes when main output resumes (dfrotz -t).
        self._win = 1 if (o[0] & 0xFFFF) == 1 else 0

    def op_set_cursor(self, o, n):
        # VAR:239 set_cursor line column [window(v6)]: v4/v5 targets the
        # status line; columns are 1-based; only the column matters in the
        # text model.
        if n > 2 and (o[2] & 0xFFFF) != 1:
            return
        self._status_col = min(max((o[1] & 0xFFFF) - 1, 0), 79)

    def op_output_stream(self, o, n):
        # VAR:243 output_stream number [table]. 3 = capture into the table
        # (text discarded from screen); any other number deselects it
        # (writing the character count to the table's first word, ZSpec
        # §7.1.2.1).
        num = o[0] & 0xFFFF
        if num == 3:
            if self._stream3:
                # nested: resume previous table on deselect (depth <= 16);
                # ponytail: single level, corpus never nests
                pass
            self._stream3 = True
            self._stream3_table = o[1] & 0xFFFF if n > 1 else 0
            self._stream3_buf = []
        elif self._stream3:
            # Deselect (any non-3 number; the compiler emits -3): word 0 =
            # char count, then the captured zscii chars at table+2.. (ZSpec
            # §7.1.2.1) — games read this back (e.g. the a/an article check).
            buf = "".join(self._stream3_buf)
            self.mem.putw(self._stream3_table, len(buf))
            for i, ch in enumerate(buf):
                self.mem.putb(self._stream3_table + 2 + i,
                              char_to_zscii(ch, self.zscii_extra))
            self._stream3 = False
            self._stream3_buf = []

    def _noop(self, o, n):
        # Screen/style/window opcodes: no-ops in the text-only screen model
        pass

    # ------------------------------------------------- dispatch table
    def _build_handlers(self):
        H = {
            # 2OP
            1: self.op_je, 2: self.op_jl, 3: self.op_jg, 4: self.op_dec_chk,
            5: self.op_inc_chk, 6: self.op_jin, 7: self.op_test,
            8: self.op_or, 9: self.op_and,
            10: self.op_test_attr, 11: self.op_set_attr, 12: self.op_clear_attr,
            13: self.op_store_op,
            14: self.op_insert_obj,
            15: self.op_loadw, 16: self.op_loadb,
            17: self.op_get_prop, 18: self.op_get_prop_addr,
            19: self.op_get_next_prop,
            20: self.op_add, 21: self.op_sub, 22: self.op_mul,
            23: self.op_div, 24: self.op_mod,
            25: self.op_call_2s, 26: self.op_call_2n,
            27: self._noop,
            28: self.op_throw,
            # 1OP
            0x81: self.op_get_sibling, 0x82: self.op_get_child,
            0x83: self.op_get_parent, 0x84: self.op_get_prop_len,
            0x80: self.op_jz, 0x85: self.op_inc, 0x86: self.op_dec,
            0x87: self.op_print_addr, 0x88: self.op_call_1s, 0x89: self.op_remove_obj,
            0x8A: self.op_print_obj,
            0x8B: self.op_ret, 0x8C: self.op_jump, 0x8D: self.op_print_paddr,
            0x8E: self.op_load,
            # 0OP
            176: self.op_rtrue, 177: self.op_rfalse, 178: self.op_print,
            179: self.op_print_ret, 180: self.op_nop, 183: self.op_restart,
            184: self.op_ret_popped, 186: self.op_quit, 187: self.op_new_line,
            189: self.op_verify, 191: self.op_piracy,
            # VAR
            224: self.op_call_vs, 225: self.op_storew, 226: self.op_storeb,
            227: self.op_put_prop, 228: self.op_read, 229: self.op_print_char, 230: self.op_print_num,
            231: self.op_random, 232: self.op_push, 233: self.op_pull,
            234: self._noop, 235: self.op_set_window, 236: self.op_call_vs2,
            237: self._noop, 238: self._noop, 239: self.op_set_cursor,
            240: self.op_get_cursor,
            241: self._noop, 242: self._noop, 243: self.op_output_stream,
            244: self.op_input_stream, 245: self._noop,
            246: self.op_read_char, 247: self.op_scan_table,
            249: self.op_call_vn, 250: self.op_call_vn2,
            251: self.op_tokenise, 252: self.op_encode_text,
            253: self.op_copy_table,
            255: self.op_check_arg_count,
            # EXT
            ops.EXT_BASE + 0: self.op_save_v5,
            ops.EXT_BASE + 1: self.op_restore_v5,
            ops.EXT_BASE + 2: self.op_log_shift,
            ops.EXT_BASE + 3: self.op_art_shift,
            ops.EXT_BASE + 4: self.op_set_font,
            ops.EXT_BASE + 11: self.op_print_unicode,
            ops.EXT_BASE + 12: self.op_check_unicode,
        }
        if self.version < 5:
            # v1-v4 forms (ZSpec §14 version columns)
            H[143] = self.op_not
            H[185] = self.op_pop  # v1-v4: pop (0xB9); catch is v5+ only
            # frotz's single 0OP table maps 0xB5/0xB6 to z_save/z_restore in
            # every version: branch form in v3, store form in v4+ (fastmem.c
            # z_save: version <= V3 -> branch, else store). The EXT forms
            # (BE 00/01) follow the same split.
            if self.version == 3:
                H[181] = self.op_save_v3
                H[182] = self.op_restore_v3
                H[188] = self.op_show_status  # v3 interpreter-drawn status line
                H[ops.EXT_BASE + 0] = self.op_save_v3
                H[ops.EXT_BASE + 1] = self.op_restore_v3
                H[ops.EXT_BASE + 15] = self._noop  # split_window: text model
            else:
                H[181] = self.op_save_v5
                H[182] = self.op_restore_v5
        else:
            H[143] = self.op_call_1n
            H[181] = self.op_save_v5
            H[182] = self.op_restore_v5
            H[185] = self.op_catch
            H[188] = self._noop  # show_status illegal in v5+; nop per ZSpec
            H[248] = self.op_not
            H[254] = self.op_print_table
            H[265] = self.op_save_undo
            H[266] = self.op_restore_undo
        return H


if __name__ == "__main__":
    # ponytail: tiny self-check for the C-math edge cases
    assert cdiv(-7, 2) == -3 and cdiv(7, -2) == -3
    assert cmod(-7, 3) == -1 and cmod(7, -3) == 1
    assert s16(0xFFEF) == -17 and s16(0x7FFF) == 32767 and s16(0xFFFF) == -1
    assert s64(0xFFFFFFFFFFFFFFFF) == -1 and s64(0x7FFF) == 32767
    print("vm self-check ok")
