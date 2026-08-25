# J-Machine Phase 2 Design — Reticulum Game Host (jhost)

**Date:** 2026-08-24
**Builds on:** `2026-08-23-zmachine-interpreter-design.md` (Phase 1, complete — 109 tests green, done bar passed)
**Scope:** a game host that serves Z-machine stories to multiple players over the Reticulum network: a NomadNet micron page for discovery, one static LXMF address per game, one `Session` per player per game, autosave-per-turn, lossless reconnect.

---

## 1. Purpose and scope

Phase 1 delivered a conformance-tested `Session` API (call → batch, VM blocked at every
turn boundary) with a CLI. Phase 2 makes the host a **second consumer of `Session`**
(spec §12): a long-running process on a VPS that players reach through the Reticulum
network.

The end-user path:

1. Host announces a NomadNet page node ("J-Machine Games") — visible in existing
   NomadNet browsers (Sideband, MeshChat, rBrowser, Ren-Browser, Micron-Navigator).
2. Player browses the page, reads the game list with one **static LXMF delivery
   address per game**, and saves the address.
3. Player sends a message (anything) to the address → host replies with the game
   transcript (fresh intro, or restored state if that player's identity has a save).
4. Player sends one game line per message → host replies with the turn's text.
5. Player disappears for hours/days → reconnects, first message → restored exactly
   where they left off.

The real first client is **Sideband on a phone** (an LXMF client). The test client is
a minimal Python LXMF client exercising the identical wire path.

### Non-goals (deliberate, per Phase 1 spec §12)

- No scale, no DoS hardening. Niche deployment; a flood that OOMs the host is acceptable.
- No transport code. RNS interfaces (TCP/LoRa/I2P/anything) are operator config only.
- No LXST streaming, no player↔player chat, no broadcast features (Phase 3 candidates).
- No game discovery beyond the micron page. No central registry exists on a
  permissionless network; out-of-band hash sharing is the bootstrap, by design.
- `zmach/` stays stdlib-only. Only `jhost/` and `jclient/` import RNS/LXMF — the
  one-way dependency `reticulum → session` is enforced by import structure.

---

## 2. Verified facts (do not re-derive)

All verified 2026-08-24 against **clean PyPI installs in throwaway venvs** (`rns==1.5.0`,
`lxmf==1.1.1`, `rns-page-node==1.5.1`) — source code read directly, not docs, not prior
project material. Line numbers refer to those versions.

### RNS 1.5.0

- `RNS.Reticulum(configdir)` is a **process singleton**: re-init raises `OSError`.
  Two RNS nodes on one machine = two OS processes (test rig requirement).
- Identity persistence is native: `RNS.Identity()`, `.to_file(path)`,
  `Identity.from_file(path)` (Identity.py:639,653).
- Destinations: `RNS.Destination(identity, direction, type, app_name, *aspects)`;
  `IN=0x11`, `OUT=0x12`, `SINGLE=0x00`, proof strategies `PROVE_NONE/APP/ALL`
  (Destination.py:57-103). A destination hash is a deterministic function of
  (name, aspects, identity) → **persisted identity = stable address**.
- `destination.announce(app_data=...)` (Destination.py:244); periodic announce +
  app_data is how nodes get names in browser node lists (pattern verified in
  rns-page-node 1.5.1 `core.py:_announce_loop`).
- `register_request_handler(path, response_generator, allow=..., auto_compress=...)`
  (Destination.py:381); handler is called with 5 or 6 args
  `(path, data, request_id, [link_id,] remote_identity, requested_at)` (Link.py:828-831).
- Client side: `RNS.Identity.recall(hash)` → identity → OUT destination →
  `RNS.Link(destination)` → `link.request(path, data, response_callback,
  failed_callback, progress_callback, timeout, max_response_size)` (Link.py:1306+).
  `RequestReceipt` states `SENT→DELIVERED→RECEIVING→READY`; `.response` holds the
  payload. Path discovery: `RNS.Transport.has_path(hash)`, `await_path(hash, timeout)`.
- **Auto-chunking of large responses (verified):** if a request response exceeds the
  link MDU, RNS re-transfers it as a streamed `RNS.Resource` (Link.py ~846-852); the
  receipt's `.response` arrives complete either way. This satisfies the Phase 1 spec's
  "Buffer stream of Text events chunked ≤ ~1 KB" requirement at zero app code.
- Known-destination table: `RNS.Identity.known_destinations` (persisted; entries
  `[time, packet_hash, public_key, app_data, ...]`, Identity.py:101-113);
  `recall_app_data(destination_hash)` (Identity.py:162). Basis of `jclient scan`.
- **Config format** (CORRECTED 2026-08-24 against installed rns 1.5.0 source:
  `Reticulum.py:745` parses interfaces from a top-level `[interfaces]`
  section with nested `[[name]]` subsections; the wheel's bundled example
  config (`Reticulum.py:~2140-2170`) is the canonical shape. The earlier
  1.3.x-style block below is REPLACED — it parsed into zero system
  interfaces on 1.5.0; `TCPInterface.py` key parsing for
  listen_ip/listen_port/target_host/target_port unchanged):

```ini
[reticulum]
enable_transport = yes
share_instance = yes
instance_name = <unique-per-node-on-machine>

[logging]
loglevel = 5

[interfaces]

  [[LAN TCP Server]]
  type = TCPServerInterface
  enabled = yes
  listen_ip = 0.0.0.0
  listen_port = 4242

  [[TCP Client]]
  type = TCPClientInterface
  enabled = yes
  target_host = <ip>
  target_port = 4242
```

  `log_to_file`/`log_file`/`log_level` do not exist in 1.5.0 (the
  `[logging]` section takes `loglevel`; file logging is a constructor/
  global concern). Other 1.5.0 API deltas found at runtime (verified
  against installed source, Task 2): `RNS.prettyhexrep` (not
  `prettyhash`); LXMF exports `LXMessage` (not `LXMMessage`); a peer
  joining after the announce retransmit window (~11 s:
  PATHFINDER_R=1, LOCAL_REBROADCASTS_MAX=2) must use
  `RNS.Transport.request_path(hash)` — RNS serves path requests with the
  destination announce; a client must announce its delivery destination
  for the host to route replies; `RNS.Link` establishes asynchronously
  (wait for `Link.ACTIVE` before `request()`); delivery destinations must
  be obtained from `register_delivery_identity()` (a second manual
  `RNS.Destination` for the same identity+aspects raises "already
  registered").

### lxmf 1.1.1 (builds on rns 1.5.x — no version conflict)

- `LXMF.LXMRouter(identity=..., storagepath=..., name=..., enforce_ratchets=...)`.
  **No class-level mutable state** — every dict/list/lock is an instance attribute
  (verified by grep) → multiple router instances per process are isolated.
- **One delivery identity per router** — `register_delivery_identity` logs an error
  and returns `None` on a second registration (LXMRouter.py:348-352). Hence one
  router per game.
- `register_delivery_identity(identity, display_name=..., stamp_cost=...)` creates
  `RNS.Destination(identity, IN, SINGLE, "lxmf", "delivery")` with ratchets enabled
  (LXMRouter.py:356-365). **This delivery destination's hash is the player-facing
  static game address.**
- `register_delivery_callback(cb)` → `cb(message)` on delivery (LXMRouter.py:1913-1916).
- **Inbound pipeline (verified):** `delivery_packet` calls `packet.prove()`
  (LXMRouter.py:1926-1927) → threaded `lxmf_delivery` → **dedup by message hash**:
  `if not allow_duplicate and self.has_message(message.hash): ignore`
  (LXMRouter.py:1906). Retransmitted messages never reach the handler.
- **Inbound message facts:** `message.content` is **bytes**; use
  `message.content_as_string()` (returns `None` if not valid UTF-8)
  (LXMessage.py:202-215). `message.source_hash` = 16-byte destination hash of the
  sender's `lxmf.delivery`; `message.signature_validated` (bool) — validated against
  the source identity's public key (LXMessage.py ~795-812).
- **Outbound:** `LXMessage(destination, source, content=..., title=...)` then
  `router.handle_outbound(lxmessage)` (LXMRouter.py ~1749). Stamp cost auto-configures
  from the destination's last-announced cost (LXMRouter.py ~1755-1760); payload
  `PACKET` or auto-split to `RESOURCE` for large content (LXMessage.py:463-511).
- **Reply pattern (verified, LXMessage.py ~770-786):**
  `RNS.Identity.recall(source_hash)` → `RNS.Destination(identity, OUT, SINGLE,
  "lxmf", "delivery")` → `LXMessage(dest, content=turn_text, title=game_name)` →
  `handle_outbound`.
- **Stamps are proof-of-work, not credits:** `LXStamper.generate_stamp(message_id,
  stamp_cost)` burns CPU rounds (LXStamper.py:123+). No ledger, no faucet, nothing
  to administer on the host. Host inbound stamp cost 0 = free to play.
- **Ordering:** RNS link data packets carry no sequence numbers (the only "sequence"
  in Link.py is a TODO inside resource transfers, Link.py ~1131). Ordering is not
  protocol-guaranteed for opportunistic multi-hop delivery, but: (a) the play pattern
  is one line in → wait for reply → next line, so a single player never has two game
  lines in flight; (b) the pathological case (two lines crossed) is harmless — the
  game sees valid inputs in receive order. Message *uniqueness* (hash dedup) covers
  the retransmission case. **No application-level dedup or ordering queue is built.**

### rns-page-node 1.5.1 (the NomadNet page convention, verified in source)

- Page node destination: `RNS.Destination(identity, IN, SINGLE, "nomadnetwork",
  "node")` — the app-name convention NomadNet browsers scan.
- Pages are plain RNS request handlers: `register_request_handler("/page/index.mu",
  handler, allow=ALLOW_ALL)` returning the page bytes (core.py:register_pages).
- Announced on an interval with `app_data=node_name` so browsers list it by name.
- ~700 lines, `Requires: cryptography, rns` — **no LXMF**. A NomadNet page in the
  current stack is an RNS request/response on a conventionally-named destination;
  LXMF is the *messaging* half of the ecosystem, not the page layer.

---

## 3. Architecture

One process on the VPS, one RNS instance:

```
jhost  (python3 -m jhost <games-dir> [--data-dir DIR] [--name NAME] [--seed N]
        [--port N] [--announce-interval MIN])
├── RNS instance (config: data/rns/config)
├── page destination      nomadnetwork.node     identity: data/identities/page
│     /page/index.mu      micron page: one section per game with its LXMF address
│     announced every --announce-interval (default 15 min; 2026-08-25 —
│     was a hardcoded 300 s), app_data = node name (default "J-Machine Games")
└── per game (one per story file in <games-dir>):
      identity            data/identities/<stem>      (persisted → static address)
      LXMRouter           storage: data/lxmf/<stem>
      delivery dest       lxmf.delivery  ← the address the page lists
      saves               data/saves/<stem>/<player_hexhash>.zmsv

Global (shared by all games): session map {(game, player_hexhash): Session}
— in memory, created by first contact, serialized by the one global lock.
```

- The **session map** is `{(game, player_hexhash): Session}`. A second player is a
  second dict entry — no shared-state machinery, no player cap beyond memory.
- **Concurrency:** one global `threading.Lock`, held across lookup/create →
  `session.input()` → autosave. LXMF delivery arrives on router threads; turns
  serialize as whole quanta (a game line in, a turn of text out — atomic by
  design). One global lock deliberately: turn-based games at ~100 ms of VM work
  don't need per-session locks. `# ponytail: global lock, per-session locks if
  throughput matters` names the upgrade path.
- **Address stability:** game address = hash of (`lxmf.delivery` name, aspects,
  per-game identity). Identity files persist under `data/identities/`; addresses
  are stable across restarts and printed at startup to
  `data/host-destinations.json` (and stdout) — the operator record of "what to
  tell people".
- **Startup:** missing config dir → scaffold minimal config (internal-test shape,
  loopback server on 127.0.0.1:4242) and print where to add the real transport.
  Missing identities → generate and persist. Announce immediately, then on the
  re-announce interval — `--announce-interval MIN` (minutes, default 15; 2026-08-25,
  was hardcoded 300 s; operator knob: a VPS that always has a path needs a
  slower cadence, a flaky LoRa node a faster one) — both the page destination
  and each game's delivery destination, with the game name as app_data, so the
  host's node list shows "J-Machine Games", "Zork I", "Planetfall".
- **Runtime dependency:** `rns>=1.5,<1.6` (+ `lxmf>=1.1,<1.2`, which requires rns
  1.5.x). Declared in `pyproject.toml`. `zmach/` and its tests never import them.

## 4. Game protocol (LXMF)

All state keyed by `(game, sender)`. Inbound message handling (under the global
lock, one code path — there is no separate "hello" request; the first message from
an identity *is* the hello, with an optional line attached):

```
on lxmf message M (game G, sender S = M.source_hash):
  if not M.signature_validated:
      → reply "[Rejected: unverified sender]"     # trust boundary: identity = save slot
      → return
  text = M.content_as_string()
  if text is None: text = ""                        # binary/invalid → treat as empty

  if (G, S) not in sessions:                          # first contact
      save = data/saves/G/S.hexhash + ".zmsv"
      s = fresh Session; s.load(story)
      if save exists:
          discard s's intro batch
          s.restore(save)                            # SaveFileError → log, fresh start
      sessions[(G, S)] = s                           # (restore batch text kept for reply)
  else:
      if sessions[(G, S)].done:
          → reply "[Game over]"                     # no feed; save persists
          → return

  if text == "":
      → reply = full transcript so far (the state re-fetch — the only message
        that carries accumulated text)
  else:
      validate: len(text) <= 200                    # input cap (player lines are short)
        fail → reply "[Input rejected: line too long (>200)]", session untouched
      feed text; run to boundary
      autosave (atomic write) — a failed write never fails the turn (log, retry next turn)
      → reply = ONLY the turn's Text data (Error events rendered inline as
        "[error] ...") — except on first contact, where the intro/restored batch
        (all-new data) precedes the turn
      (the turn's autosave above already persists the done state — no extra write)
reply = LXMessage(recall(S) delivery dest, content=reply_text, title=game_name)
```

Notes:

- **Replies are per-turn deltas (2026-08-25 change).** The phone client
  (Sideband) renders each LXMF message as a chat bubble; re-sending the
  accumulated transcript every turn would make the chat O(n²) text. So after
  first contact the reply carries only the new turn's text — the chat
  scrollback IS the transcript, and the phone does the accumulating. First
  contact still sends the full batch (all-new data), and an empty message
  still returns the full transcript: that deliberate re-fetch is the recovery
  path if the client's message history was ever lost (reinstall, wiped
  conversation). A host restart is the one transcript reset (restored batch
  is empty for a boundary save — §5), which is safe for the same reason: the
  phone's scrollback already has everything before the boundary.
- **Output is uncapped.** Turn text of any size rides the link directly; beyond the
  link MDU RNS auto-chunks it as a Resource (verified §2). A 40-line room description
  is one packet over TCP, many chunks over LoRa — same client code.
- **First contact with a line** (e.g. the player's very first message is `look`,
  not an empty hello): the line is fed after the restore/fresh-start batch; the
  reply carries both. An empty first message just returns the transcript. There is
  no reserved hello word — the game gets whatever the player typed, as they'd expect.
- **200-char input cap** — trust-boundary validation on untrusted network input
  (spec's no-DoS-hardening stance is about scale, not one absurd line). Rejection
  is a normal reply, never a silent drop.
- **`quit` is a game line**, not a protocol op: the story handles it; `EndOfGame`
  fires; the session stays in the map in done state (frees nothing, costs ~512 KB —
  accepted: N players × 512 KB is trivial). Next first-contact from that identity
  restores (or starts fresh) per the save on disk.
- **Duplicate suppression is the protocol's job** (verified dedup, §2) — the host
  never sees a retransmission.
- **Title** carries the game name (e.g. "Zork I") so phones can distinguish
  responses. No custom fields; the protocol is content + title.

## 5. State and saves

- **On disk:** `data/saves/<game>/<player_hexhash>.zmsv` — ZMSAVE v1 images
  (~65 KB), one per player per game. Written after every turn and on done (the
  per-turn autosave is held while an in-game `@save` image is pending; the
  `@restore` turn's autosave rewrites the slot — spec §7 test 5 lifecycle); atomic
  (temp file + `os.replace`). Never deleted, never expires — the player who
  vanishes for weeks is the *normal* case (spec §12). 10 players × 5 games ≈ 3 MB.
- **Save/restore integrity:** the image embeds the story file's sha256 (Phase 1).
  Story file changed or image corrupt → `SaveFileError` at restore → logged, host
  falls back to fresh start, player keeps playing. Nothing is ever "stacked".
- **Reconnect (the whole point):** first message from a saved identity → fresh
  `Session` + `load` (intro discarded) + `restore` → state byte-identical to the
  uninterrupted run (Phase 1 gate 3 proves the engine side). The restored
  batch's text is **empty for a boundary save** (verified 2026-08-24 against the
  Phase 1 engine: the VM parks at the read *after* the prompt cell was already
  emitted in the pre-park batch, so `restore`'s batch is `[Prompt]`, `""` text —
  byte-exact engine behavior; the prompt the player saw is already in their
  phone's message history). The reply to a first empty message after a host
  restart is therefore zero bytes (a client no-op); a first message with a line
  gets that line's turn. From then on the player just types.
- **In-game `@save`/`@restore` opcodes:** mapped to the host-local slot with no
  prompt — the opcode's hint string is ignored; the handler reads/writes
  `data/saves/<game>/<player>.zmsv` directly (the save slot *is* the identity,
  so no filename exists to ask for). This is spec §12's "save/restore handlers map
  in-game verbs to host-local slots".
- **Two links from the same identity** (two devices, same identity file): drive
  the same session, serialized by the lock. Two different identities: fully
  independent.

## 6. Network and deployment

**Transport is not in the design.** No interface type, IP, or port appears in
`jhost` or `jclient` code. The RNS config file is the only deployment surface; RNS
transport machinery (gravity, path discovery, multiple interfaces) does the rest.

| Deployment | RNS config | Notes |
|---|---|---|
| Internal test rig | host: `TCPServerInterface` 127.0.0.1:4242; client process: `TCPClientInterface` → 127.0.0.1:4242; `share_instance=yes`, distinct `instance_name` per process | two real RNS nodes on one box (process-singleton verified §2) |
| VPS on the Reticulum testnet | one `[[TCP Client]]` section → public testnet TCP endpoints (operator-supplied; published by the community, deliberately not hardcoded — they move) | the real users; the page node + game addresses reach them |
| VPS + rnode/LoRa net | add an `RNodeInterface` section; testnet and LoRa net mesh through RNS path discovery | a phone attached to the LoRa side reaches the games with zero code/config changes on our side |

Client/phone side is symmetric: the phone's RNS config decides how it reaches the
network; it needs only a path to the host's destinations.

Operator flow on the VPS:

1. `python3 -m jhost games/` → scaffolds `data/` (config, identities), prints
   addresses, announces.
2. Edit `data/rns/config`: add the testnet (and/or rnode) section, restart.
3. Share the page-node hash (out-of-band) or rely on announce discovery by existing
   NomadNet users. Players save the per-game LXMF addresses from the page.

The micron page (`/page/index.mu`), rendered on demand by the request handler (no
disk files — the host already knows its games and addresses):

```
>J-Machine Games
>
One-line Z-machine games over Reticulum.
Send any message to a game's address to play;
progress is saved per player automatically.
4 players all time
>
>Games
> Zork I (v5)
  <ab:cd:12:...:ef>        # prettyhexrep of the game's lxmf.delivery hash
  1 today · 4 this week · 5 this month
> Planetfall (v5)
  <90:12:...:34>
  0 today · 0 this week · 1 this month
```

**Player stats on the page (2026-08-25):** the per-game line is today /
this week / this month — players with a slot mtime since midnight, this
Monday, and this 1st (host's local clock); the overall bar is the
all-time unique player count across all games (one identity playing two
games counts once). Everything derives from the autosave slot files
(spec §5) — one slot per (game, player), rewritten every turn, never
deleted — mtime = last turn; no separate counters exist and nothing new
to maintain. "Playing right now" is deliberately not reported: the
protocol is stateless fire-and-forget with no session start/stop, so an
exact online count would be a guess. A host with no players renders
zeros. The stats lines are plain 2-space-indented text; `parse_page`'s
regexes ignore them (the network suite's live browse+parse is the
regression gate).

Addresses are printed as plain `prettyhexrep` hashes (RNS's own `<ab:cd:...>` form)
— trivially parseable by a regex and rendered by every browser; `rns:`-link markup
is left to the browser rather than asserted. `jclient browse` extracts the hashes
and prints `name + address` lines.

The rendered game name is the **story filename stem** (e.g. `zork1`, not a
display name like `Zork I`) — the example above is illustrative; the stem is
the convention used throughout §3/§4/§5 (save slots, identities, session keys)
and is what the `render_page`/`parse_page` format carries (no spaces).

## 7. Testing and done bar

**Layering:** all protocol logic lives in `jhost/protocol.py` as pure functions
(handlers receive `(game, sender_hexhash, text, sessions, save_store)`, return the
reply) — unit-testable with fakes, no RNS import, milliseconds each, zero flake.
The network suite exists only to prove what RNS/LXMF touch: announce → recall →
delivery → reply, across a real transport.

**The rig** (`tests/network/netrig.py`): two OS processes, real RNS instances,
loopback TCP pairing, temp data dirs. First contact is the announce path: host
announces at startup; client polls `RNS.Identity.recall(game_delivery_hash)` until
the key appears, then `has_path`, then sends. Hash handoff via
`data/host-destinations.json` (written by the host — also the operator feature).
RNS log capture from both processes; on failure the last ~40 lines of both logs go
into the assertion message (RNS failures are opaque without them).

**The client** (`jclient/`, also `python3 -m jclient`): minimal LXMF client mirroring
Sideband's exact behavior — enter a delivery address, register own delivery identity
(persisted at `~/.jclient/identity` — the player's save-slot file), send message,
await reply. ~80 lines on the verified API. Subcommands:

- `jclient scan` — offline page-node classification: for each entry in
  `RNS.Identity.known_destinations` with non-empty `app_data` (announced, named),
  `recall` its identity, compute `Destination.hash(identity, "nomadnetwork",
  "node")`, and print the entry if that exact hash is also in the known table (we
  have heard the page-node destination itself announce). No guessing by app name
  from a hash — membership test only. (what's on my network; zero typed hashes)
- `jclient browse <page-node-hash>` — RNS request to `/page/index.mu`, print games
- `jclient play <game-address>` — connect-and-play loop: stdin line → LXMF message →
  print reply; `^D` exits (per-turn autosave already persisted the state)

**Tests:**

| # | Test | Proves |
|---|---|---|
| 0 | Rig smoke: page fetch + one LXMF message → one reply | transport, announce, link, delivery, request all work on 1.5.0/1.1.1 |
| 1 | Page discovery: client parses a game address **out of the page text**, connects using the parsed address | the real discovery chain end-to-end |
| 2 | Play vs dfrotz: 10 commands over the network; `norm(client) == norm(dfrotz -s seed)` (Phase 1 harness reused; host `--seed` for determinism) | whole stack byte-identical to the oracle |
| 3 | **Reconnect (flagship):** 5 turns → kill client process → new process, same identity file → 5 more turns; combined transcript byte-identical to dfrotz with all 10 uninterrupted | lossless restore-on-reconnect — the Phase 2 goal |
| 4 | Two players: identities A/B interleave turns on one game; merged view == dfrotz with the interleaved sequence, same seed | session map, autosave, no cross-talk |
| 5 | In-game `save`/`restore` opcodes over the wire (both exist in the Zork walkthrough); slot file rewritten between them | spec §12 host-local slot mapping |
| 6 | Protocol edges (unit, no RNS): unverified-signature rejection; >200-char rejection; first-contact restore vs fresh; done-session replies; corrupt save → fresh start | §4's behavior table, one assertion per row |
| 7 | Save sanity (unit): autosave file exists, restores into a fresh `Session` | ZMSAVE integrity through the full path |

`test_network_*` skips cleanly when `rns`/`lxmf` are not importable — the stdlib
suite stays green anywhere. `scripts/run_done.py` grows a Phase 2 gate block next to
Phase 1's.

**Done bar (Phase 2):**

1. Unit suite green (Phase 1 + protocol units).
2. Rig: page → LXMF → 10 commands byte-identical to dfrotz.
3. Reconnect byte-identical (flagship).
4. Two players clean.
5. In-game save/restore over the wire.
6. **Manual: Sideband phone on the testnet** — page visible, play a few turns,
   reconnect restores. (This is the "other host" confirm; by the time it runs, the
   only untested variable is real-network reachability.)

**Task 0 is the rig smoke test** (Phase 1's oracle-smoke pattern): RNS 1.5.0 local
pairing is this phase's highest-risk unknown. If loopback TCP pairing doesn't
cooperate, the identified fallback is a unix-socket `LocalInterface` pair (same
verified code path); if neither works, that's a design-level finding to surface
before building the protocol on top.

## 8. Layout

```
J-Machine/
  zmach/              # Phase 1 — unchanged, stdlib-only
  jhost/
    __init__.py
    host.py           # RNS instance, page destination, per-game routers, lock, announce
    protocol.py       # pure message-handling logic (unit-testable without RNS)
    __main__.py       # CLI + config scaffolding
  jclient/
    __init__.py
    client.py         # RNS + LXMF client wiring
    __main__.py       # scan / browse / play
  tests/
    test_protocol.py  # tests 6–7 (no RNS import)
    network/
      netrig.py       # process spawn, hash handoff, log capture (test-only RNS code)
      test_netrig.py  # test 0
      test_network.py # tests 1–5
  pyproject.toml      # rns>=1.5,<1.6 ; lxmf>=1.1,<1.2 (runtime deps for jhost/jclient)
  scripts/run_done.py # + Phase 2 gate block
  README.md           # + operator/deployment section
```

## 9. Decisions log

- Game protocol = LXMF delivery messages (one line in, one turn out), not RNS
  request paths — the real client (Sideband) is an LXMF client; RNS request paths
  remain for the page layer. Replaces the 2026-08-24 interim request-path design.
- One host process, one RNS instance, one LXMRouter per game (verified 1
  delivery-identity-per-router limit in lxmf 1.1.1); per-game persisted identity =
  the static per-game address.
- Micron page via the `nomadnetwork.node` convention (verified in rns-page-node
  1.5.1): RNS request handler, rendered on demand, announced with node name. No
  LXMF in the page layer.
- No application-level message dedup or ordering queue: LXMF hash-dedup covers
  retransmission (verified LXMRouter.py:1906); the one-line-in/one-reply-out play
  pattern makes crossing moot; a crossed pair is harmless (valid lines in receive
  order).
- 200-char input cap (trust boundary); output uncapped (RNS auto-chunks, verified).
- Stamps: host inbound cost 0 (free to play); stamps are proof-of-work CPU, no
  credits to administer (verified LXStamper).
- Save slot = the player's RNS identity (their `~/.jclient/identity`); in-game
  `@save`/`@restore` map to the host-local slot with no prompt.
- One global lock for turn atomicity; upgrade path = per-session locks
  (ponytail comment in code).
- Test client built (minimal Python LXMF client), not borrowed (nomadnet): the
  done bar needs programmatic transcript access for dfrotz byte-identity; the
  built client exercises Sideband's exact wire path.
- Transport = operator RNS config only; testnet endpoints never hardcoded.
- rns 1.5.x + lxmf 1.1.x pinned (current lines, verified API surface); prior-world
  (infoticulum-era rns 1.3/lxmf 1.0) knowledge discarded per owner direction.