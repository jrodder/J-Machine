"""ZMSAVE v1 snapshot format (spec §7, amended — Task 10 rulings):

Fixed offsets, big-endian throughout. Amended from the spec's original
layout because (a) memory is story + data stack, which can exceed the
spec's uniform 512 KB image (risorg: 0xAC368 bytes), so the image length
is explicit; (b) locals live OUTSIDE story memory (Python lists per
frame — Task 8), so frame entries carry inline locals instead of the
spec's stale locals_base; (c) pc is part of the machine state (omitted
by the spec); (d) the full RNG state (a/interval/counter) is saved, not
just the predictable-mode seed, so special-mode games also restore
losslessly; (e) a JSON state block carries the VM's non-memory
interpreter state (blocked read, MORE/screen counters, output-stream-3
capture) so a restore into a FRESH VM is byte-identical to a
never-saved run — the Phase 2 reconnect guarantee.

    offset   size   field
    0        8      magic b"ZMSV0001"
    8        64     story file header (first 64 bytes of the story data)
    72       32     SHA-256 of the story file as loaded
    104      4      pc u32
    108      4      sp u32 (operand-stack top address)
    112      4      error state u32 (0 = none, else current error number)
    116      1      n_frames (0 = top level)
    117      10710  call frames: 255 x 42 bytes, zero-padded
                    {return_pc u32, sp u32, n_locals u8, n_args u8,
                     discard u8, catch_n u8, locals[15] u16}
    10827    1      n_catches
    10828    1020   catch tokens u32, zero-padded to 255
    11848    4      RNG a u32
    11852    4      RNG interval u32 (0 = standard mode)
    11856    4      RNG counter u32
    11860    4      state_len u32
    11864    L      state: UTF-8 JSON (blocked read, screen/MORE counters,
                    output-stream-3 capture, done/needs_input, instrs, seed)
    11864+L  4      image_len u32 (len of the memory image)
    11864+L+4  N    full memory image (story + data stack)
    …      +32      SHA-256 of all preceding bytes (trailer)

decode validates magic, story hash, and trailer hash in that order; any
mismatch raises SaveFileError (spec §7).
"""
import hashlib
import json
import struct

from .events import SaveFileError
from .vm import Frame

MAGIC = b"ZMSV0001"

_OFF_PC = 104
_OFF_SP = 108
_OFF_ERROR = 112
_OFF_NFRAMES = 116
_OFF_FRAMES = 117
_FRAMESIZE = 42
_MAX_FRAMES = 255
_OFF_NCATCHES = _OFF_FRAMES + _MAX_FRAMES * _FRAMESIZE      # 10827
_OFF_CATCHES = _OFF_NCATCHES + 1                            # 10828
_MAX_CATCHES = 255
_OFF_RNG = _OFF_CATCHES + _MAX_CATCHES * 4                  # 11848
_OFF_STATE_LEN = _OFF_RNG + 12                              # 11860
_STATE_HEAD = _OFF_STATE_LEN + 4                            # 11864

_FRAME_FMT = ">II4B15H"  # return_pc, sp, n_locals, n_args, discard, catch_n, locals[15]
assert struct.calcsize(_FRAME_FMT) == _FRAMESIZE


def _state_dict(vm):
    return {
        "pending": vm.pending,
        "needs_input": vm.needs_input,
        "done": vm.done,
        "done_status": vm.done_status,
        "instrs": vm.instrs,
        "seed": vm.seed,
        "pc_save": vm.pc_save,
        "io": {
            "_win": vm._win,
            "_status": vm._status,
            "_status_col": vm._status_col,
            "_status_dirty": vm._status_dirty,
            "_status_changed": vm._status_changed,
            "_status_ref": vm._status_ref,
            "_lines": vm._lines,
            "_line": vm._line,
            "_col": vm._col,
            "_line_count": vm._line_count,
            "_more_pending": vm._more_pending,
            "_at_more": vm._at_more,
            "_row": vm._row,
            "_batch_row": vm._batch_row,
            "_emit_rest": vm._emit_rest,
            "_stream3": vm._stream3,
            "_stream3_table": vm._stream3_table,
            "_stream3_buf": vm._stream3_buf,
        },
    }


def encode(vm):
    """Snapshot the VM -> opaque ZMSAVE v1 image (bytes).

    Precondition: the input buffer is empty (true at every API boundary
    by INV2, and for in-game saves the library has consumed the line
    before invoking the save opcode)."""
    assert vm.input.empty, "save requires an empty input buffer"
    frames = vm.frames
    if len(frames) > _MAX_FRAMES:
        raise SaveFileError("frame count exceeds save limit")
    if len(vm.catch_stack) > _MAX_CATCHES:
        raise SaveFileError("catch count exceeds save limit")

    head = bytearray(_STATE_HEAD)
    head[0:8] = MAGIC
    head[8:72] = vm.story.data[:64]
    head[72:104] = hashlib.sha256(vm.story.data).digest()
    struct.pack_into(">I", head, _OFF_PC, vm.pc & 0xFFFFFFFF)
    struct.pack_into(">I", head, _OFF_SP, vm.sp & 0xFFFFFFFF)
    struct.pack_into(">I", head, _OFF_ERROR, vm.error & 0xFFFFFFFF)
    head[_OFF_NFRAMES] = len(frames)
    for i, f in enumerate(frames):
        locals15 = [v & 0xFFFF for v in f.locals] + [0] * (15 - len(f.locals))
        struct.pack_into(_FRAME_FMT, head, _OFF_FRAMES + i * _FRAMESIZE,
                         f.return_pc & 0xFFFFFFFF, f.sp & 0xFFFFFFFF,
                         f.n_locals, f.n_args, 1 if f.discard else 0,
                         f.catch_n, *locals15[:15])
    head[_OFF_NCATCHES] = len(vm.catch_stack)
    for i, t in enumerate(vm.catch_stack):
        struct.pack_into(">I", head, _OFF_CATCHES + i * 4, t & 0xFFFFFFFF)
    struct.pack_into(">III", head, _OFF_RNG,
                     vm._rng_a & 0xFFFFFFFF, vm._rng_interval & 0xFFFFFFFF,
                     vm._rng_counter & 0xFFFFFFFF)

    state = json.dumps(_state_dict(vm)).encode("utf-8")
    struct.pack_into(">I", head, _OFF_STATE_LEN, len(state))
    image = bytes(vm.mem.mem)
    body = bytes(head) + state + struct.pack(">I", len(image)) + image
    return body + hashlib.sha256(body).digest()


def decode(vm, image):
    """Restore a ZMSAVE v1 image into the VM (raises SaveFileError).

    Validates magic, story hash, and trailer hash in that order."""
    if len(image) < _STATE_HEAD + 4 + 32 or image[:8] != MAGIC:
        raise SaveFileError("bad ZMSAVE magic or truncated image")
    if hashlib.sha256(vm.story.data).digest() != image[72:104]:
        raise SaveFileError("story hash mismatch (save from a different story)")
    if hashlib.sha256(image[:-32]).digest() != image[-32:]:
        raise SaveFileError("trailer hash mismatch (corrupt save)")

    state_len = struct.unpack_from(">I", image, _OFF_STATE_LEN)[0]
    img_off = _STATE_HEAD + state_len + 4
    image_len = struct.unpack_from(">I", image, img_off - 4)[0]
    if len(image) != img_off + image_len + 32:
        raise SaveFileError("image length mismatch")
    if image_len != vm.story.memory_size():
        raise SaveFileError("memory image size mismatch (save from a different story)")

    vm.mem.mem = bytearray(image[img_off:img_off + image_len])
    vm.pc, vm.sp = struct.unpack_from(">II", image, _OFF_PC)
    vm.error = struct.unpack_from(">I", image, _OFF_ERROR)[0]
    vm.fwords = vm.mem.getw(24)

    nf = image[_OFF_NFRAMES]
    frames = []
    for i in range(nf):
        o = _OFF_FRAMES + i * _FRAMESIZE
        (return_pc, fsp, n_locals, n_args, discard, catch_n,
         *raw_locals) = struct.unpack_from(_FRAME_FMT, image, o)
        # locals were stored signed-truncated (s16); H is unsigned —
        # restore the canonical signed value exactly.
        locals15 = [v - 0x10000 if v >= 0x8000 else v for v in raw_locals]
        frames.append(Frame(return_pc=return_pc, locals=locals15[:n_locals],
                            n_locals=n_locals, n_args=n_args, sp=fsp,
                            discard=bool(discard), catch_n=catch_n))
    vm.frames = frames

    nc = image[_OFF_NCATCHES]
    vm.catch_stack = [struct.unpack_from(">I", image, _OFF_CATCHES + i * 4)[0]
                      for i in range(nc)]
    vm._rng_a, vm._rng_interval, vm._rng_counter = struct.unpack_from(
        ">III", image, _OFF_RNG)

    state = json.loads(image[_STATE_HEAD:_STATE_HEAD + state_len].decode("utf-8"))
    vm.pending = tuple(state["pending"]) if state["pending"] else None
    vm.needs_input = state["needs_input"]
    vm.done = state["done"]
    vm.done_status = state["done_status"]
    vm.instrs = state["instrs"]
    vm.seed = state["seed"]
    vm.pc_save = state["pc_save"]
    for k, v in state["io"].items():
        setattr(vm, k, v)
    vm.events.clear()