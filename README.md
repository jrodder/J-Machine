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

## Phase 2 — Reticulum game host (jhost)

`zmach` (Phase 1) is consumed by the host exactly like the CLI: one
`Session` per (game, player), the VM parked at every turn boundary.
`jhost/protocol.py` holds the whole game protocol as pure functions;
`jhost/host.py` is the thin RNS/LXMF wiring.

### Run it

```bash
python3 -m venv .venv && .venv/bin/pip install "rns>=1.5,<1.6" "lxmf>=1.1,<1.2"
.venv/bin/python -m jhost games/ --data-dir data/
```

1. First run scaffolds `data/` (minimal loopback RNS config + per-game
   identities) and prints the page-node and per-game LXMF addresses
   (also in `data/host-destinations.json` — the operator record of
   "what to tell people"). Addresses are stable across restarts
   (persisted identities).
2. Edit `data/rns/config`: add your transports — Reticulum testnet TCP
   endpoints (operator-supplied; the endpoints move, so nothing is
   hardcoded) and/or an `RNodeInterface` section for a LoRa net.
   Restart.
3. Players: browse the NomadNet micron page (share the page-node hash
   out-of-band) and send messages to a game's LXMF address — or use the
   test client, which mirrors Sideband's wire path:

```bash
.venv/bin/python -m jclient scan --data-dir ~/.jclient --identity ~/.jclient/identity
.venv/bin/python -m jclient browse <page-node-hash> --data-dir ~/.jclient --identity ~/.jclient/identity
echo "look" | .venv/bin/python -m jclient play <game-address> --data-dir ~/.jclient --identity ~/.jclient/identity
.venv/bin/python -m jclient fetch <page-node-hash> /file/<game>.pdf --out <game>.pdf --data-dir ~/.jclient --identity ~/.jclient/identity
```

A game with a `<game>.pdf` sitting next to its story in `games/` gets a
``[manual`<page-node-hash>:/file/<game>.pdf]`` micron link under its block on
the page — the PDF is served from the page node on request (the canonical
micron URL form per the NomadNet Guide: files live under `/file`; this is
how the Infocom copy-protection manuals ship alongside the story).
`jclient fetch` downloads it; any micron client that follows file links can
too. Large responses are chunked by RNS.Resource transparently, so multi-MB
manuals work over the same request path as the page itself.

The client identity file IS the player's save slot; the host autosaves after
every turn (held while an in-game `save` image is pending, rewritten by the
`restore` turn's autosave) to `data/saves/<game>/<player-hash>.zmsv`. A player who
disappears for days reconnects to exactly where they left off.
In-game `save`/`restore` verbs map to that slot with no prompt.

### Verify

`python3 scripts/run_done.py` — Phase 1 gates + the Phase 2 network gate
(RNS rig: page discovery, 10-command dfrotz byte-parity over the wire,
reconnect byte-identity, two players, in-game save/restore). Network
gates skip when `rns`/`lxmf` are not installed.
