# J-Machine

A Z-Machine interpreter in pure Python (stdlib only), built to the
[design spec](docs/superpowers/specs/2026-08-23-zmachine-interpreter-design.md).

Phase 1 delivers a correct, conformance-tested engine behind the
`Session` API (spec §5): `load` / `input` / `save` / `restore`, call →
batch, with the VM blocked waiting for input at every boundary.

## Usage

```bash
python3 -m zmach tests/corpus/zork1.z3     # play in the REPL
python3 scripts/run_done.py                # run the done bar
```

REPL meta commands: `:info` (story header), `:save FILE`, `:restore FILE`,
`:quit`. Bad story files and EOF exit cleanly with non-zero status.

## Testing

- `python3 -m unittest discover -s tests` — full suite (109 tests).
- Conformance (plan Task 12): czech / strictz / unicode byte-compare vs
  dfrotz; crashme runs to completion; see
  `tests/conformance/run_conformance.py` for the per-suite drive notes.
- Differential (plan Task 13): 120-command Zork I walkthrough,
  byte-identical to dfrotz (same `-s` seed). The walkthrough is
  hand-transcribed, so `python3 tests/differential/run_differential.py`
  prints the first-divergence report for manual verification. dfrotz's
  interactive in-game save prompts are handled deterministically (temp
  cwd, pre-existing default save file, `""`+`y` injected per save).
- Fake transport (plan Task 13): `tests/faketx/channel.py` drives a
  `Session` through ≤N-byte non-line-aligned fragments; transcript must
  match the oracle. Phase 2 network dress rehearsal.

dfrotz (`/usr/games/dfrotz`, `-t` plain text) is a **dev-only** oracle —
never a runtime dependency.

## Done bar (spec §11)

```
✓ 1. Conformance             StrictZ, CZECH, unicode vs dfrotz; crashme completes
✓ 2. Differential vs dfrotz  432/432 transcript lines byte-identical (Zork I, 120 cmds)
✓ 3. Save round-trip         9-turn transcript identical through save -> fresh-session restore
✓ 4. Fake transport          chunked channel transcript matches the oracle
? 5. Smoke (manual)          play the corpus games to a known point in a real terminal
Done = gates 1–5 pass and `zmach zork1.z5` is playable to completion.
```

`python3 scripts/run_done.py` re-runs gates 1–4 and the unit suite,
prints this checklist, and exits non-zero on any failure.

## Phase 2 pointer

Phase 2 (spec §12) adds the Reticulum game host as a second consumer of
`Session` — nothing here imports Reticulum; the dependency is strictly
`reticulum → session`. Multi-session via a `{reticulum_identity: Session}`
map with autosave-per-turn; restore-on-reconnect is exactly the
fresh-session restore flow validated by gate 3.