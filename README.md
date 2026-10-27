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

### Propagation (operator)

A player whose client cannot build a link to this host hands the turn to a
propagation node instead — that is what nodes are for. LXMF then waits for the
*recipient* to ask for it, and nothing in the router does that by itself, so the
host asks: `--propagation-node <hash>` plus `--propagation-pull MIN` (default 5)
queries that node once per game identity for anything it is holding. What comes
back arrives through the same path as a direct send, so the turn plays
identically. With no `--propagation-node` there is no poll, and a turn the
player's client could not deliver directly is never seen here at all — the
client thinks it sent, the host never saw it, nothing fails loudly anywhere.

The poll collects only what *that* node holds. A client pointed at a different
node is still out of reach — and no node ever pushes mail toward a recipient:
`LXMRouter.jobs()` runs only store cleaning and peer distribution, so mail moves
node-to-node by peering and reaches anyone only when that recipient's router
asks. Neither flag is therefore a fix for availability, and neither is set here.
Running this host as a propagation node (`enable_propagation`) helps exactly one
thing — players who deliberately point their client at this host — and costs the
opposite: every stored message is someone else's mail held on our disk, and per
game router it is one more node announcing and peering. If that ever becomes a
feature it needs one shared router for all 31 games rather than 31 nodes, plus a
way to park our own replies locally, since naming *ourself* the outbound node
means opening a link to ourself (`LXMRouter.py:2885-2894`), which RNS does not
support.

### What the host records (operator)

`data/ledger.jsonl` carries one line per state change of every inbound player
message: an `open` line before any work, then `handed_off` with the byte counts,
then what the network did — `delivered`, `propagated`, `failed`, `rejected` — or
`dropped`/`abandoned` with a `why`. A message with an `open` and no terminal line
is either a crash mid-turn or a reply still in flight, which is the only trace
either one ever leaves. `delivered` means the transfer completed into the
player's side, not that a person read it; their next command is the only proof of
that. Rotated by size, so it cannot fill the disk it is recording.

`data/health.json` is rewritten every minute: interfaces online, whether the
config has any transport a stranger could actually use (a scaffolded or wiped
config is loopback-only — the node announces and serves its page to itself and
is invisible to everyone, which looks healthy from every other angle), whether
the save store is writable, free disk, story files that disappeared after
startup, and the outcome counters, whose names are the ledger states.

`data/days.jsonl` is the permanent day log — one line per calendar day, each day a
map of game to turns, players, delivered and unanswered. The ledger above is where
these numbers come from, but the ledger is size-rotated and forgets after a few
thousand turns, so without this file the answer to "was last Tuesday busy" stops
existing. It is written by folding the ledger window into what is already there,
and the fold only ever raises a number: a page read sees a window, not the whole
ledger, and a quiet afternoon must not be allowed to rewrite history. Empty days get
a row too, because "nobody played" and "we stopped recording" must not look alike.
A year of it is a few dozen kilobytes.

The forever numbers are not in this file and never were: players, moves and finishes
come from the autosave slot tree, where a slot is written every turn and never
deleted, plus a floor file that can only increase so a corrupt read cannot walk the
odometer backwards.

`data/host-destinations.json` remains the operator record of what addresses to
tell people about.

### What the journal shows (operator)

Uplinks come and go — that is why there are several of them. RNS logs every
socket close, reconnect attempt and reconnection, which over 30 days on the
production host was roughly 73,000 of 138,629 journal lines: expected, correct,
and enough to hide every other line in the file. So the host owns RNS's log line.
`jhost/logs.py` registers itself as the RNS log destination
(`RNS.Reticulum(configdir, logdest=...)`, accepted at `RNS/Reticulum.py:244`):
the first occurrence of each churn message prints, repeats of the same message on
the same interface are counted for 15 minutes instead of printed, and the count
goes out with the next occurrence and into `data/health.json` as `log_suppressed`.
A flapping uplink stays visible as a rate without occupying the journal.
Replaying that same month through loglevel 5 plus the filter leaves **1,282 lines
where there were 138,629**.

Nothing else is suppressed: errors, story loads, link failures and tracebacks all
pass through untouched. Keep `[logging] loglevel = 5` in the RNS config — the
scaffold writes 5 for new hosts; 6 (DEBUG) is another 109,892 lines a month of
RNS housekeeping, mostly known-destination cleaning.

### The activity page (operator)

The main page stays a game list with one thin odometer bar; the breakdown lives
one link away at `/page/stats.mu` — a third path on the same page destination, so
no new identity and no extra announce. It renders per request from the same slot
scan the main page uses, plus a bounded read of the ledger (256 KB of tail, not
the whole file).

Sections: server totals, per game (played games only, most recent first, with how
many of its players came back), recent days, and replies.

How many of a game's players came back prints per game, beside the moves and players
it qualifies: it is what separates `24 moves · 24 players` (24 people having a look)
from `58 moves · 1 player` (one long afternoon), and it appears only over a crowd —
`58 moves · 1 player` has already said the one player stayed, and a row that restates
its own numbers trains the reader to skip the piece when it carries news. The same fact
at node level gets its own quiet line under the totals — `27 of those 94 sent more than
one move` — rather than a piece inside them, because the totals counting every identity
that ever sent a command is a public node working as intended, and the extra line is
there to be read alongside, not as a correction. What used to sit in the game rows was
the newest player's move count, which needed a paragraph to explain, changed every time
a stranger arrived, and said nothing about the game once they left. Recent days reads the permanent day log when there is
one, so it shows seven days and says how far back the log goes. A day the log saw and
recorded as empty prints `0 moves`; a day from before the log existed prints
`no record`, because the ledger itself only remembers from the day it was introduced
and printing those as zeros would report the busiest stretch of the node's life as
nobody playing. Only on a host that has not written a day log yet does the section
fall back to the ledger window, where it labels itself as the window it is. A day log that has stopped
recording today is a fault, and turns the status button red — every player-facing
number looks normal while that is happening, which is why it is checked. Two rules hold it honest. A section with nothing to say
is omitted rather than rendered as a zero, because a number that appears, vanishes
and reappears lower reads as a fault. And the reply counts carry their own window
label (`since 26 Oct 13:55`) because the ledger is size-rotated — an unlabelled
count would read as all-time and then quietly shrink, `jhost/window.py` reports
how far back it actually reached and whether the byte cap left records unread.

Nothing on the page names a player. The save tree could attribute every number to
a destination hash, and a hash is a persistent handle on a person, so the page
carries aggregates only (pinned by a test that fails if a 32-hex string appears).
Nothing on a game line is shed or abbreviated. Rows used to be trimmed to fit one
screen row, because on the main game list a wrapped entry really does read as two
games — but this page is read, not tapped, clients wrap it, and that trim cost the
operator's own game its statistics purely because "Hitchhikers Guide to the Galaxy"
is long. Pieces now say what they mean in as many words as it takes: `179 moves · 2
players · 2 played more than once · last played 11 d ago`, including the
`last played`, which a bare `11 d ago` left to be guessed.

### Completion is not counted, and why

The page used to print `N finished`. It could not mean that. The Z-machine reports only
that a run *ended* — the `quit` opcode, or a return from the top-level routine — and
Infocom stories reach that path for a victory, a death and a player giving up alike,
with nothing left over to tell them apart. The score globals would be the usual
heuristic, but they are an Inform convention and most titles here run on Infocom's own
engine, where those words hold something else entirely. So the label promised a fact the
engine cannot supply, and the machinery came out: the `won` flag in a slot's sidecar,
`wins.json`, and the piece on both pages. Reaching the end of a story still behaves
correctly — `[Game over]`, the dead-slot replay, and `again` all still work — it simply
is not a statistic any more. If a genuine victory signal is ever wanted, the honest route
is to keep the story's closing lines when a run ends and read the difference off the text,
which is where that difference actually appears.

### Is it awake? (operator)

Three instruments, answering three different versions of the question.

`data/health.json`, rewritten every 60 s from inside the host, lists what has to
be true for the next player to be served: story files present, save store
writable, an interface online, disk free, no turn left with no outcome. Those are
capability checks, deliberately not traffic rates — from counters alone a healthy
node with no players is indistinguishable from a dead one. It says nothing about
whether the mesh can reach this host, because there is no inside view of that.

`jclient ping <game address>` is one round trip shaped exactly like one of a
player's turns: an LXMF message in, the answer waited for, an ack sent back. The
client's own receipt is the proof. It got an answer, which means the query arrived
and the reply came back — both directions in one observation. The ack is a
courtesy copy: it lets the host count the round trip itself (`pings`/`acks` in
health.json), and being inbound traffic it cannot be faked from the server. The verb is answered before any ledger record is
opened: no slot, no meta, no move counted, because a health check must not
inflate the numbers on the page it is being checked against.

`jhost.probe` runs that ping on a timer and writes the outcome to
`data/probe.json`. It is a separate process, not a loop inside `jhost`, because a
check that runs inside the thing it checks can only repeat what that thing already
believes. Install it with:

```
sudo cp deploy/jhost-probe.service deploy/jhost-probe.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jhost-probe.timer
systemctl list-timers jhost-probe.timer          # when it next runs
.venv/bin/python -m jhost.probe --data-dir data  # one run, now
```

It addresses the same game every run — first in sorted order — so a failure never
depends on which story happened to be unlucky (`--game` to choose). By default it
reaches the host over loopback; to make the check go out to a public hub and come
back, like a player's traffic does, write `data/probe/rns/config` by hand. It is
never overwritten, so an operator-authored config wins.

Each run gets **ten minutes**, polled rather than slept through, and it returns the
moment the answer arrives. That window covers finding a route to the host as well as
the answer's trip, and the two cannot be separated from outside: RNS learns routes on
its own schedule, which can be immediate or can be several minutes of listening, so a
check that gives up sooner cannot tell "not yet" from "never". The first version
allowed 480 seconds and filed a failure that was really just a short window.

So a **red orb** means the last check did not complete, and the text beside it says
what was missing — no route, no answer, or an answer that says the node itself is
short-circuiting something. Read that line before concluding the machine is down.

The activity page ends with one button — last, below the back link, because it is a
strip about the node rather than what a player came to read. It is a small orb drawn
with block glyphs and painted by hand, three lines tall, light from the upper left.
The colour is the whole message and nothing spells it out; there is one hue per state:

- **green** — the last check got through. A client outside the process put an LXMF
  query to a game address and the engine answered.
- **amber** — nothing is known to be wrong and nobody has asked. Not a fault, and not
  a green light either. It stands alone with no text, because age is not a thing an
  unchecked node has.
- **red** — a fault: a story file missing, the save store unwritable, a turn with no
  outcome, no interface online, page render failures, a day log that stopped
  recording, or a probe that failed since the last good round trip.

Two rules keep it honest. A fault outranks a recent success, because a vanished
story file is a fact while one good round trip is evidence about one message. And
age is printed (`verified 43 min ago`) rather than coloured, so nobody has to
invent an expiry time and watch a healthy node turn red for being quiet. A restart
does not clear it either: the record on disk is the evidence, and this process
starting up again does not make the last successful check untrue.

What green claims is narrow and worth naming: networking alive, engine answering
requests. What it does not claim is that every story file loads (the faults above
cover that) or that a stranger can discover the node — the probe keeps its own
saved routes, so a run can succeed on a path both sides already have. Checking
discovery means running a client with an empty data dir, which is a separate,
slower question and is not what this light is for.

What none of them can prove is that a particular player's client is collecting
replies. A probe proves the path as far as the machine it ran on.

### Guestbook (operator)

One board for the whole server, linked from the top of the page
(`<page-hash>:/page/guestbook.mu`). A visitor types one line into one field and
taps **Post it**. Posts are marked with the poster's destination hash prefix
(`#6f5223`) when their client identified to the node, otherwise `anon` — most
clients browse anonymously, so `anon` is the normal case and nobody is turned
away for it. Limit: one line per 10 min per identified poster, one per 60 s
server-wide for anonymous posts.

There is also a verified door that the page doesn't advertise: sending
`sign: <line>` to any game address appends to the same board, tagged with the
sender hash its LXMF signature proves.

Two verbs go to a game address and are not story input: `sign: <line>` above,
and `again` once a run has ended (death or `quit`). `again` starts a fresh run
in the same slot and keeps the player's counters — moves stay on the odometer,
and a run the host saw end keeps its mark even if that slot is pruned later.
Both files are plain and read per request, so moderating needs no restart:

- `data/guestbook.jsonl` — the log (`{"ts", "who", "text"}` per line, newest
  last, trimmed to 200). Delete a line to drop an entry.
- `data/guestbook-banned.txt` — denylist: one destination hash per line, `#`
  comments allowed (only identified posters can be named).

To try it from a terminal: `python3 -m jclient browse <page-hash> --path
/page/guestbook.mu --field message="hello"` (that is a micron submit, sent as
`field_message` like the real clients do), or
`echo "sign: hello" | python3 -m jclient play <game-address>`.

### Verify

`python3 scripts/run_done.py` — Phase 1 gates + the Phase 2 network gate
(RNS rig: page discovery, 10-command dfrotz byte-parity over the wire,
reconnect byte-identity, two players, in-game save/restore). Network
gates skip when `rns`/`lxmf` are not installed.

### Canary

`scripts/canary.sh` asks the one question health cannot: does a player actually
get served? It starts a throwaway `jhost` on a fresh temp data dir and loopback
port, fetches the NomadNet page over the same RNS request path a real visitor
uses, checks the game addresses the page advertises against the ones the host
is serving, plays five real Zork I turns through `jclient play`, and confirms
each turn produced a reply and reached an outcome in the ledger. One line out,
exit 0 only if the whole path worked; every wait is bounded and everything it
starts is torn down even if the canary is killed. `CANARY_KEEP=1` leaves the
temp logs for a post-mortem. It is a command,
not a unit test (it is slow and needs LXMF), and it is run by hand: before a
deploy and after anything touches the message path. The timer-shaped check is
`jhost.probe`, above, and the two do not substitute for each other — the canary
starts its own host, so it catches code rot and never sees the live process.

What it cannot prove is the thing no on-box check can: that the public mesh can
reach this host, or that a human read a reply. A green canary means "this
machine serves a player correctly when one arrives over loopback".
