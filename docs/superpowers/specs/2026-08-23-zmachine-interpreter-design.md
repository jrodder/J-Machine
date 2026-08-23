# J-Machine: Z-Machine Interpreter — Design Spec

Date: 2026-08-23
Status: Draft for review
Phase 1 scope: local interpreter + session API. Phase 2 (Reticulum host + client) gets its own spec.

## 1. Purpose

A from-scratch Z-machine interpreter in Python. It exists so an Infocom-era /
Inform text adventure can be played over the Reticulum network (Phase 2) into
terminals like nomadnet. The network context drives every design choice:

- **Text only.** No ANSI, no cursor codes, no scroll assumptions. Small text
  per turn (LoRa frame sizes are tiny).
- **Line-based input.** One network message per player line, not per keystroke.
- **Bounded turn latency.** Reticulum is store-and-forward; the player's node
  may be gone for hours. The host must survive indefinitely with a clean
  machine state.
- **Save/restore are first-class primitives**, not a CLI convenience. The
  Phase 2 server owns save policy (slots, autosave, restore-on-reconnect);
  the interpreter only exposes the snapshot mechanism.

## 2. Goals / Non-goals

**Goals (Phase 1):**

- Play story files **v3, v3.3, v5, v8** to completion (the Infocom catalog +
  modern Inform 7 from ifarchive's open-source section).
- A public `Session` API (call → batch) that a Reticulum game host can wrap
  without touching the VM.
- Opaque, self-contained save images.
- A verifiable done bar: conformance suites + differential testing vs dfrotz.

**Non-goals (Phase 1):**

- v4/v6/v7 story files (clean "unsupported version" error on load).
- v8 movie/stream opcodes (no-op stubs; games continue).
- Undo (opcode reports unsupported; the standard marks it optional and the
  conformance suites treat it as optional).
- Graphics, ANSI/color, screen paging / `[MORE]` (multi-session hosting is
  Phase 2 server territory; `Session` objects are independent by design).
- Any Reticulum or nomadnet code. Zero Reticulum dependency in Phase 1.
- Save-image transfer between nodes (saves live on the host node only).

## 3. Story file version support

| Version | Typical content | Status |
|---|---|---|
| v3.0–3.3 | Infocom classics (Zork I) | Supported |
| v5 | Infocom late era, most I6 games | Supported |
| v8 | Modern Inform 7, 64-bit ints, wide strings | Supported |
| v4/v6/v7 | Rare, mostly non-English | Clean error on load |

The per-version opcode tables follow the reference specs (§14) verbatim;
this document does not restate them.

## 4. Architecture

```
        Phase 2 (separate spec)
[nomadnet client]  ⇄  Reticulum  ⇄  [transport adapter]
                                          │
═════════════════ Phase 1 boundary ═════════════════
        [cli REPL]      [Session]      [fake transport harness]
                              │
              [storyfile] [memory] [vm] [opcodes] [strings]
                        [io] [savefile] [events]
```

**Modules** (flat package `zmach/`, split further only if a file outgrows its
purpose):

| Module | Responsibility |
|---|---|
| `storyfile.py` | Header parse, checksum, version detection, memory map |
| `memory.py` | 64 KB address space; version-dependent widths; OOB policy |
| `vm.py` | Main loop, opcode dispatch, spec-mandated error recovery |
| `opcodes.py` | Opcode implementations (split by version if it grows) |
| `strings.py` | v5 packed strings, v8 wide strings → Unicode |
| `io.py` | Input buffer; `read_char` / `read_string` / `read` semantics |
| `savefile.py` | Snapshot encode/decode (§7) |
| `events.py` | Event dataclasses |
| `session.py` | Public call→batch API |
| `cli.py` | Terminal REPL |

**Invariants:**

- **INV1** — The VM never references a terminal or a network. All output
  becomes Events; all input arrives through the session.
- **INV2** — At every public API boundary the VM is either blocked waiting
  for input or finished. Therefore every returned batch is a complete
  logical turn, and every save is a clean snapshot. No mid-opcode state can
  ever reach a consumer.
- **INV3** — Events are structured and text is plain. No ANSI or control
  sequences ever appear in `Text` events.
- **INV4** — Transport concerns (chunking, ordering, retry) live outside the
  interpreter. The VM never chunks.

## 5. Session API

```python
@dataclass
class StoryInfo:
    name: str          # filename stem — the z-machine header has no
                       # name/author field (the title banner is printed
                       # game text)
    version: int       # 3, 5, or 8
    release: int       # header bytes 2..3
    serial: str        # 6 ASCII chars, header bytes 18..23
    file_sha256: bytes

class Session:
    def load(self, path: str | os.PathLike,
             seed: int | None = None) -> list[Event]: ...
    def input(self, line: str) -> list[Event]: ...
    def save(self) -> bytes: ...
    def restore(self, image: bytes) -> list[Event]: ...
    def set_save_handler(self, cb): ...      # cb(filename_hint: str) -> bool
    def set_restore_handler(self, cb): ...   # cb(filename_hint: str) -> bool
    @property
    def story(self) -> StoryInfo: ...
    @property
    def done(self) -> bool: ...
```

Events:

```python
@dataclass class Text:      data: str
@dataclass class Prompt:    hint: str | None = None
@dataclass class Error:     message: str
@dataclass class EndOfGame: status: int
```

**Semantics (call → batch):**

- `load` runs the VM to the first `Prompt` or `EndOfGame` and returns the
  events (opening text).
- `input` feeds the line into the input buffer and runs until the VM asks
  for more input with an empty buffer, or the game ends.
- `save` returns the opaque image (§7); the consumer decides where it goes
  (CLI: a file; future server: a slot).
- `restore` loads a full image; identity mismatch (different story file)
  raises `SaveFileError`.
- `seed` (if given) overrides the story's stored random seed so runs are
  reproducible — required for differential testing.
- **In-game save/restore:** I6 libraries parse `save 3` and invoke the
  z-machine's `save`/`restore` opcodes; the VM calls the installed handlers.
  No handler installed → opcode reports the error, game continues. The CLI
  installs handlers that prompt for a filename; the Phase 2 server installs
  handlers that map to host-local slots.

Exceptions: `StoryFileError` (bad/unsupported story), `SaveFileError`
(bad image), both with clean messages; the CLI prints and exits without
traceback.

## 6. Screen model and output

- **Text mode.** The z-machine's window API is ignored: one flat text
  stream, no split windows, no status-line re-rendering, `more` is a
  no-op, cursor/style ops are no-ops. Standard Infocom games degrade
  cleanly to sequential text.
- **v8 wide strings** (16-bit ZSCII sequences) decode directly to
  Unicode; output is UTF-8. The standard v8 wide-string table is
  built-in; custom Blorb IFwid tables are out of scope (corpus games use
  the standard table).
- Chunking of `Text` into small messages is the transport's job (Phase 2),
  never the VM's (INV4).

## 7. Save format ("ZMSAVE v1")

Fixed offsets, big-endian integers throughout. The memory image is a
uniform 524,288 bytes (512 KB) for all versions — the maximum v8 story
size (ZSpec 1.0 §1.1.4; verified against risorg.z8, declared length
0xd86d×8 = 443,240 bytes). Oversized for v3 (128 KB max) and v5 (256 KB
max); deliberately uniform so the format has a single trailer offset and
no version-dependent parsing. Pointer/PC fields are u32 (v8 addresses
exceed 16 bits; high memory extends to 512 KB via 8× packed addresses).

```
offset   size   field
0        8      magic b"ZMSV0001"
8        64     story file header (first 64 bytes; byte 0 = version)
72       32     SHA-256 of the story file as loaded
104      4      sp (operand-stack top address)
108      4      error state (0 = none, else current error number)
112      1      n_frames (0 = top level)
113      2550   call frames: n_frames × 10 bytes
               {return_pc u32, locals_base u32, n_locals u8, n_args u8},
               zero-padded to 255 entries
2663     1      n_catches
2664     1020   catch tokens u32, zero-padded to 255
3684     32     RNG: seed u32 + 28 zero bytes
3716     524288 full memory image (512 KB, zero-padded beyond the story)
528004   32     SHA-256 of all preceding bytes (trailer)
```

Total: 528,036 bytes, all versions. Uncompressed (saves are host-local
— spec §2 — so size is a non-issue; no compression, no dependency,
no decode cost).

- The call chain, operand-stack pointer, and error state are **not** part
  of the story file's memory, so they live in the VM-state block above;
  together with the memory image (globals, object table, arrays, and
  operand-stack contents all live in dynamic memory — locals/args in a
  frame region growing up from just above the globals table, arrays
  growing down from the high-memory base) this is the complete machine
  state.
- RNG: seed u32 in predictable mode (a negative `@random` argument
  reseeds; `@random 0` reseeds from the host clock). After a restore the
  machine is in predictable mode with the saved seed.
- Precondition: the input buffer is empty when a save is taken (true at
  every API boundary by INV2, and true for in-game saves because the
  library consumes the line before invoking the `save` opcode). `save`
  asserts this.
- Restore validates magic, story hash, and trailer hash in that order;
  any mismatch → `SaveFileError`.
- The server owns all policy (slots, rotation, autosave cadence,
  retention) by calling `Session.save`/`restore`.

## 8. Input model

- `Session.input(line)` appends the line plus a newline to the internal
  buffer. Games reading character-by-character (`read_char`, `read_string`)
  consume it normally; name-entry prompts and the like work unchanged.
- When the buffer drains and the VM requests input again, the current batch
  ends (a `Prompt` event is emitted). No partial-line edge cases exist
  because the line is always buffered whole.
- No keystroke echo simulation, no mid-line backspace — the player sends
  completed lines (this is a network terminal, not a local TTY).
- The VM has no timers or idle machinery: if the player disappears, the VM
  sits blocked, waiting, which is exactly the state a save snapshot needs.
- A missing handler or game `quit` produces `EndOfGame`; the session stays
  inspectable (`done` is true).

## 9. Robustness and error handling

- **Header:** bad magic or unsupported version → `StoryFileError` with the
  detected version. Checksum mismatch → warning + continue; `--strict`
  (CLI) / a `strict=True` (API) option fails.
- **Bad opcodes / bad operands / out-of-bounds:** follow ZSpec 1.1
  bad-opcode recovery. OOB reads return 0, OOB writes are silently
  ignored, error codes are raised per spec — the goal is to survive
  `crashme`-style inputs without a Python traceback (INV1: the VM reports
  via `Error` events, never via exceptions to the consumer, except the two
  documented file-level errors).
- **Int widths:** Python ints are unbounded; every arithmetic boundary
  clamps to the version's width (16-bit v3, 32-bit v5, 64-bit v8).
- **Recursion depth:** z-machine sub calls map to Python calls with an
  explicit stack-depth check (spec: 63 in v5, deeper in v8) → spec error
  instead of a Python stack overflow.

## 10. CLI

```
zmach story.z5 [--strict] [--seed <int>] [--save <file>] [--restore <file>]
```

- Plain REPL: print `Text` data, show `>`, read a line, loop.
- In-game `save`/`restore` work via the opcode handlers (prompts for a
  filename).
- Meta commands: `@save <file>`, `@restore <file>`, `@info`, `@quit` —
  direct calls to the session API, so the API path is exercised in
  production use, not just tests.
- Exit code: 0 normal/`EndOfGame`, 1 file errors.

## 11. Testing and done bar

**Corpus** (fetched at M0 from zifmia; all freely redistributable):

- Zork I — pinned, `.z3` and `.z5` (the differential flagship).
- One v5-era game and one I7/v8 game chosen from zifmia + ifarchive
  open-source section (v8 exercises wide strings and 64-bit ops).

**Gates:**

1. **Conformance:** StrictZ (v5) and CZECH (v5, output comparison vs
   czech.out5) pass; crashme.z5 runs to completion without a Python
   traceback; unicode.z5 output matches dfrotz; v3 verified via corpus
   differential (zork1.z3, minizork.z3); v8 via risorg differential +
   v8-specific unit tests. Document any failure rather than paper over
   it (undo legitimately unsupported).
2. **Differential vs dfrotz:** scripted walkthrough of Zork I (target
   100+ commands including an in-game save/restore), byte-identical output
   after whitespace normalization, same `--seed`. Plus a short v8
   walkthrough for wide-string coverage. dfrotz is a dev-only system
   binary, never a runtime dependency.
3. **Save round-trip:** N turns → `save` → `restore` → N more turns; the
   transcript is byte-identical to the uninterrupted run (seeded RNG).
   This gate is also the Phase 2 reconnect flow (restore + continued
   input).
4. **Fake transport:** the same session driven through a simulated channel
   — 512-byte chunks at arbitrary (non-line-aligned) boundaries, artificial
   latency, message boundaries on input — must produce a transcript
   byte-identical to the local run. This is the Phase 2 dress rehearsal
   and directly targets the previously painful "getting it to work over
   the network" step.
5. **Smoke:** all three corpus games playable to a known point in a normal
   terminal.

**Done = gates 1–5 pass and `zmach zork1.z5` is playable to completion.**

## 12. Phase 2 boundary (hooks only — nothing built here)

- The Reticulum game host is a *second consumer* of `Session`, replacing
  the CLI: server-mode IN destination + handshake (standard Reticulum
  client-server pattern); game text → `Buffer` stream of `Text` events
  chunked ≤ ~1 KB; input → one message per line.
- **Multi-session.** The host keeps one independent `Session` per player
  (fully self-contained: own memory, input buffer, save handler) in a
  `{reticulum_identity: Session}` map, plus one save file per identity.
  Player connects → host restores that identity's latest save (or starts
  fresh) → resume where they left off. Designed for very slow links: a
  player who plays a few turns and disappears for hours is the normal
  case — autosave-per-turn (a 65 KB local write) makes
  restore-on-reconnect lossless.
- Save/restore handlers map in-game verbs to host-local slots; save
  images never leave the host node.
- **No scale, no DoS hardening** (deliberate): niche deployment, the only
  accepted input is plain game lines; a flood that OOMs the host is
  acceptable. An identity allow-list remains a one-line option if ever
  wanted.
- Dependency direction is strictly one-way: `reticulum → session`.
  Phase 1 has no Reticulum import anywhere.

## 13. Dependencies and project layout

- **Runtime:** Python ≥ 3.10, stdlib only (`struct`, `hashlib`,
  `dataclasses`, `pathlib`). No runtime third-party packages.
- **Dev:** dfrotz (installed at `/usr/games/dfrotz`; `-t` plain text,
  `-s <seed>` RNG seed), test corpus + conformance suites from zifmia,
  dork (github.com/ntoskrnlexe/dork) as structural reference.
- **Committed references** (authoritative local copies, fetched in the
  first implementation task): ZSpec 1.0 full text, ZSpec 1.1 addendum,
  and dork `machine.ts` / `text.ts` / `vocab.ts` under `references/`.
- Tests use stdlib `unittest` (zero extra deps).

```
J-Machine/
  zmach/            # the 10 modules (§4)
  tests/
    test_*.py       # per-module unit tests
    conformance/    # ztest / CZECH / StrictZ harness
    differential/   # dfrotz oracle comparison
    faketx/         # fake-transport harness
    corpus/         # story files
  docs/superpowers/specs/
```

## 14. References

- Z-Machine Standard 1.1 — inform-fiction.org/zmachine/standards/
- DGK "The Z-machine" 0.6e — ifarchive (v3 minutiae)
- zifmia (github.com/jeffnyman/zifmia) — conformance suites + free
  story files (incl. Zork I)
- dfrotz — differential-testing oracle
- Reticulum manual + NomadNet README — Phase 2 context only

## 15. Decisions log

- Call→batch session API (`events = session.input(line)`), not sink
  callbacks or generators — approved 2026-08-23.
- v3/v3.3 in scope (whole Infocom catalog) — approved.
- Phase 1 = CLI + seam + fake-transport, zero Reticulum code — approved.
- Smoke/differential flagship: Zork I (freely redistributable; no user
  action needed) — approved.
- Undo out of scope; v8 movie/stream opcodes are no-op stubs.
- Saves are host-local opaque blobs; network transport of save images is
  explicitly not a Phase 2 requirement.
- Phase 2 hosting is multi-session: one `Session` + one save file per
  Reticulum identity, autosave-per-turn, resume-on-reconnect; no scale or
  DoS hardening (niche deployment) — approved 2026-08-23.
- Conformance set = StrictZ + CZECH + crashme + unicode + corpus
  differentials; ztest dropped (canonical hosting not reliably
  available) — 2026-08-23.
- `StoryInfo` carries no author field: the z-machine header has no
  name/author fields at all (the title banner is printed game text; the
  serial at bytes 18..23 is the only traditional header string) —
  verified against dork + dfrotz output, 2026-08-23.
- RNG: 32-bit LCG (a=1664525, c=1013904223), `result =
  floor((seed / 0xffffffff) * n) + 1`; seed via negative `@random`
  argument; initial seed via `--seed`. Verified against dork (conformance
  tested) and dfrotz `-s`, 2026-08-23.
- Save format v1 includes an explicit VM-state block (sp, error state,
  call frames, catch tokens) because the call chain is not part of story
  memory; the memory image is a uniform 512 KB (v8 max story size,
  ZSpec 1.0 §1.1.4) so the format has no version-dependent offsets —
  2026-08-23.