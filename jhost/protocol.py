"""Pure host protocol (spec §4) — no RNS/LXMF imports; unit-testable with
fakes. Reply semantics (2026-08-25): the first-contact reply is the load/
restore batch (+ that line's turn if the first message carried one) — all
new data. Every reply after that carries ONLY the new turn's text (Error
events inline as '[error] ...'); the chat scrollback is the transcript — the
phone client (Sideband) renders each LXMF message as a bubble, so re-sending
the accumulated text every turn would be O(n^2). The one exception: an empty
message is a deliberate state re-fetch and returns the full cumulative
transcript (recovery if the client's history was lost). A host restart is
the only case where the transcript restarts (restored batch = prompt +
status line; the phone's message history keeps the rest, spec §5).
"""
import dataclasses
import json
import os
import sys
import time
import uuid
from pathlib import Path

from zmach.events import (EndOfGame, Error, SaveFileError, StoryFileError,
                          Text)
from zmach.session import Session

INPUT_CAP = 200  # trust boundary: player lines are short (spec §4)


def unpretty(s):
    """'<ab:cd:…>' (RNS pretty hexrep) or plain hex -> 16 bytes."""
    return bytes.fromhex(s.strip("<>").replace(":", ""))


DEST_JSON = "host-destinations.json"  # host writes it; also the rig's hash handoff


class SaveStore:
    """(game, sender) <-> ZMSAVE v1 image. FileSaveStore on the host."""

    def load(self, game, sender):      # -> bytes | None
        raise NotImplementedError

    def save(self, game, sender, image):  # -> None
        raise NotImplementedError

    # Slot metadata (the page's move count + win flag, spec §6). A store
    # that keeps no metadata reports none and the page omits those pieces,
    # so the test/side fakes need not implement these.
    def load_meta(self, game, sender):        # -> dict
        return {}

    def save_meta(self, game, sender, meta):  # -> None
        pass


class FileSaveStore(SaveStore):
    """root/<game>/<sender>.zmsv. Atomic (temp + os.replace). Never
    deleted, never expires (spec §5)."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, game, sender):
        return self.root / game / f"{sender}.zmsv"

    def load(self, game, sender):
        p = self._path(game, sender)
        return p.read_bytes() if p.exists() else None

    def save(self, game, sender, image):
        p = self._path(game, sender)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(image)
        os.replace(tmp, p)

    def _meta_path(self, game, sender):
        # sibling of the slot, deliberately NOT inside the image (ZMSAVE v1
        # stays opaque, spec §7): the slots stay the registry, so pruning a
        # .zmsv retires that player's counters too (player_stats only reads
        # the sibling of a slot it found)
        return self.root / game / f"{sender}.json"

    def load_meta(self, game, sender):
        return _read_meta_json(self._meta_path(game, sender))

    def save_meta(self, game, sender, meta):
        p = self._meta_path(game, sender)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, p)


@dataclasses.dataclass
class GameState:
    session: Session
    transcript: str  # cumulative Text since this session's load/restore
    # an in-game @save wrote the slot this session: the per-turn autosave
    # holds until @restore consumes it (spec §5 test 5 slot lifecycle: the
    # slot is written by the in-game save and rewritten by the autosave
    # after the restore turn) — an autosave in between would clobber the
    # mid-turn image the story's @restore is meant to reload
    save_pending: bool = False
    # page odometer pieces (spec §6), restored from the slot's metadata at
    # session start so a host restart does not reset a player's history
    turns: int = 0
    won: bool = False


def _read_meta_json(path):
    """Slot metadata: absent, unreadable or non-object -> {} (the page
    omits those pieces; a stray or half-written file never fails a render,
    and a game never fails to load because of one). Atomic writes (temp +
    os.replace) are why a half-written file is not an expected case."""
    try:
        m = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _count(v):
    """Tolerant counter read: this is our own metadata, but a hand-edited
    file must not take down a page render or a player's game."""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def ago(ts, now=None):
    """Relative time for the page's 'last move' and guestbook pieces —
    micron has no date formatting, and a 5-minute-resolution clock is the
    whole point of a LoRa-paced game. now injectable so tests need no clock
    fiddling."""
    s = (time.time() if now is None else now) - ts
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400 * 2:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} d ago"


def _render(events):
    """Events -> transcript text. Error inline as '[error] ...' (spec §4);
    Prompt/EndOfGame add no text."""
    out = []
    for e in events:
        if isinstance(e, Text):
            out.append(e.data)
        elif isinstance(e, Error):
            out.append(f"[error] {e.message}")
    return "".join(out)


def handle_message(game, sender, text, verified, sessions, store,
                   story_path, seed=None, sign=None):
    """One inbound LXMF message -> reply text. Never raises on protocol
    failures: rejection/done/corrupt-save/unavailable-story are replies
    (spec §4).

    First contact -> the load/restore batch (+ the line's turn if one was
    attached). Existing session, non-empty line -> ONLY the new turn's
    rendered text (per-turn delta, spec §4). Empty message -> the full
    cumulative transcript (state re-fetch).

    game      story stem (e.g. "zork1")
    sender    32-hex-char lxmf.delivery destination hash of the sender
    text      message.content_as_string() (None -> "")
    verified  message.signature_validated
    sessions  {(game, sender): GameState}, mutated in place
    store     SaveStore
    seed      RNG seed for fresh sessions (dfrotz -s parity in tests)
    sign      callable(sender, line) -> reply text, called for a message that
              starts with "sign: " (the guestbook verb, spec §6). Intercepted
              before any session work, so it neither starts a game nor counts
              as a turn. None -> the prefix is ordinary game input.
    """
    if not verified:
        return "[Rejected: unverified sender]"
    if text is None:
        text = ""
    if sign is not None and text[:5].lower() == "sign:":
        # the board rides the signing channel: LXMF has already proven
        # `sender`, which a page view never does (guestbook.py cites why)
        return sign(sender, text[5:].strip())
    key = (game, sender)
    st = sessions.get(key)
    first = st is None
    if st is None:
        try:
            sessions[key] = st = _new_game(game, sender, story_path, store, seed)
        except (StoryFileError, OSError) as e:
            print(f"jhost: {game}/{sender[:8]}: story load failed ({e}); "
                  f"game unavailable", file=sys.stderr)
            return "[Game unavailable]"
    elif st.session.done:
        return "[Game over]"
    if text == "":
        return st.transcript
    if len(text) > INPUT_CAP:
        return "[Input rejected: line too long (>200)]"
    events = st.session.input(text)
    delta = _render(events)
    st.transcript += delta  # kept for the empty-message state re-fetch
    # Page odometer (spec §6): only a turn actually fed counts (rejections,
    # [Game over] and state re-fetches returned above). The win flag latches
    # here because the Z-machine's done flag is host-side (zmach/vm.py) and
    # is not part of the save image — this turn's EndOfGame is the only
    # evidence a run ever ended. Metadata is its own small file, so the
    # save_pending hold (which protects the mid-turn ZMSAVE image) does not
    # apply: the counters advance even when the autosave holds.
    st.turns += 1
    if not st.won and any(isinstance(e, EndOfGame) for e in events):
        st.won = True
    store.save_meta(game, sender, {"turns": st.turns, "won": st.won})
    if not st.save_pending:
        _autosave(store, game, sender, st.session)
    # first contact: the batch + this turn are all new -> send both.
    # afterwards: the delta only (the scrollback keeps the rest).
    return st.transcript if first else delta


def _new_game(game, sender, story_path, store, seed):
    """First contact: fresh Session + load; restore from the slot if a
    save exists (intro discarded). Corrupt/mismatched save -> log + fresh
    start (spec §4/§5)."""
    s = Session()
    st = GameState(s, "")
    m = store.load_meta(game, sender)
    st.turns, st.won = _count(m.get("turns")), bool(m.get("won"))
    _install_handlers(s, st, store, game, sender)
    batch = s.load(str(story_path), seed=seed)
    img = store.load(game, sender)
    if img is not None:
        try:
            batch = s.restore(img)
        except SaveFileError as e:
            print(f"jhost: {game}/{sender[:8]}: save restore failed ({e}); "
                  f"fresh start", file=sys.stderr)
    st.transcript = _render(batch)
    return st


def _install_handlers(s, st, store, game, sender):
    """In-game @save/@restore -> host-local slot, no prompt (spec §5).
    The opcode's filename hint is ignored: the slot IS the identity.
    @save writes the mid-turn image the story's @restore reloads (engine/
    dfrotz semantics: the restore replays the save routine's tail, "Ok.")
    — so the autosave holds while one is pending; the restore turn's
    autosave rewrites the slot."""
    def _save(_hint):
        try:
            store.save(game, sender, s.save())
            st.save_pending = True
            return True
        except OSError:
            return False

    def _restore(_hint):
        img = store.load(game, sender)
        if img is None:
            return False
        try:
            s.restore_image(img)
            st.save_pending = False
            return True
        except SaveFileError:
            return False

    s.set_save_handler(_save)
    s.set_restore_handler(_restore)


def _autosave(store, game, sender, s):
    """After every fed turn (except while an in-game @save is pending —
    see GameState.save_pending). A failed write never fails the turn
    (spec §4): log, retry next turn."""
    try:
        store.save(game, sender, s.save())
    except OSError as e:
        print(f"jhost: autosave failed {game}/{sender[:8]}: {e}",
              file=sys.stderr)


# Micron colour tags for page and board copy. `F + 3 hex digits sets the foreground,
# `f resets to the visitor's theme default - both verified by running meshchat's own
# src/frontend/js/MicronParser.js over tagged samples (`Fd33 emits #dd3333; the
# spans after `f came back #222 on light / #ddd on dark, so a closed tag cannot
# bleed). Three rules came out of the same check:
#   * 3-hex ONLY: the `FT + 6-hex truecolour form is NomadNet's, and meshchat
#     renders it with NO colour at all, so truecolour would be invisible there.
#   * no `B backgrounds: both clients band headings with the theme's own
#     background (#777 light / #bbb dark), so a band of ours would fight it.
#   * every hue must read on white AND black, since one page renders in
#     whichever theme the visitor uses (meshchat stamping a dark palette onto a
#     light background is exactly its grey-input defect).
#   * colour goes on BODY lines only. A '> ' line is a heading, and both clients
#     paint a heading with the theme's own band (#bbb dark / #777 light) and its
#     own foreground; any hue of ours lands on that band at ~1.4:1 contrast
#     (measured off a render of the page through meshchat's parser). Headings get
#     emphasis from `! bold instead.
# Colour tags: `F + 3 hex digits sets the foreground, `f resets to the
# visitor's theme default (both verified by running meshchat's own
# src/frontend/js/MicronParser.js over tagged samples: `Fd33 -> #dd3333,
# and the spans after `f came back #222 on light / #ddd on dark, so a
# closed tag never bleeds). Rules learned the same way:
#   * 3-hex ONLY. The `FT + 6-hex truecolour form is NomadNet's and meshchat
#     renders it with NO colour at all, so truecolour means invisible there.
#   * no `B background tags: both clients already band headings with their
#     theme's own background (#777 light / #bbb dark), and ours would fight it.
#   * every hue has to read on white AND black, because the same page is
#     rendered in whichever theme the visitor uses (meshchat stamping a dark
#     palette onto a light background is exactly its grey-input defect).
GOLD, TEAL, CLAY, GREY = "`Fb83", "`F3a8", "`Fd44", "`F777"
RESET = "`f"
DIV = "-"


def render_page(name, games, stats, manuals=None, guestbook=None):
    """Micron page text (spec §6). games = [(stem, addr_str)];
    addr_str = pretty hexrep of the game's lxmf.delivery destination hash.
    stats = player_stats(...) — cumulative counts + last-move recency: one
    odometer bar under the title, one stats line per game (spec §6). Only
    pieces worth saying are rendered (a 0-count piece is omitted, so a
    number never appears, vanish and reappear lower). The stats lines are
    plain indented text that parse_page's regexes ignore (verified by the
    network suite's live browse+parse). manuals = {stem: url} — a game with a manual gets a
    micron file link line under its block: `[manual`<hash>:/file/<stem>.pdf]
    (NomadNet Guide micron spec: links are backtick-delimited `[label`url]`,
    not markdown [label](url); files are served under the /file path). url
    = <32-hex page-dest hash>:/file/<stem>.pdf. parse_page ignores the line:
    neither the '> name' nor the '<hex>' pattern matches it. The game's
    address line is itself a clickable link: label = prettyhexrep <xx:..:xx>,
    url = lxmf@<32-hex-hash> (meshchat/Sideband open a conversation).
    guestbook = the page-destination URL of the guestbook board; when given,
    one link line to it sits in the top section, under the odometer bar.
    The page footer links the Android mapper (Droid-Mapper): a plain
    https:// link target — meshchat's onNodePageUrlClick opens http(s)
    targets in a new browser tab (NomadNetworkPage.vue, verified)."""
    def plural(n, one, many):
        return one if n == 1 else many
    # Odometer bar: what only ever accumulates (moves fed, players seen,
    # runs finished) rather than a window that reads empty most of the day.
    # The players piece always shows (a new node's honest 0); the other two
    # appear once they are non-zero.
    bar = []
    if stats.turns:
        bar.append(GOLD + f"{stats.turns} "
                   + plural(stats.turns, "move", "moves") + RESET)
    bar.append(TEAL + f"{stats.total} "
               + plural(stats.total, "player", "players") + " all time"
               + RESET)
    if stats.won:
        bar.append(CLAY + f"{stats.won} "
                   + plural(stats.won, "win", "wins") + RESET)
    lines = [">`!" + name + "`!", ">",
             "Infocom-era text adventures, playable by message.",
             "Send any message to a game's address to play;",
             "progress is saved per player automatically.",
             " · ".join(bar),
             DIV]
    if guestbook:
        # one line, in the top section where a visitor sees it before the game
        # list, and it cannot be read as a game: parse_page's name anchor
        # needs '> word' and its address line needs an indented
        # [label`lxmf@…] link, neither of which this is
        lines.append("🖊️ `[Guestbook (post by message)`" + guestbook + "]")
        lines.append(DIV)   # a rule separates more strongly than a blank line
    lines.append(">Games")
    for stem, addr in games:
        # the bare '> name' line is jclient's parse anchor (spec §7): the
        # Z-machine version suffix used to sit here and added nothing a
        # player reads, while costing the parse contract a strict '(vN)'
        lines.append("> `" + "!" + stem + "`" + "!")
        # the address is its own link: the visible label is the prettyhexrep
        # <xx:..:xx> (copyable), and the link target is lxmf@<32-hex-hash>,
        # which meshchat/Sideband turn into an open-conversation action
        # (NomadNetworkPage.vue strips "lxmf@" and requires 32 hex chars,
        # no delimiters).
        lines.append("  `[" + addr + "`lxmf@"
                     + addr.strip("<>").replace(":", "").lower() + "]")
        g = stats.per_game.get(stem)
        if g:  # no slot for this game -> no stats line at all (nothing true
               # to say; a fresh host does not advertise 0s per game)
            parts = [f"{g.explorers} "
                     + plural(g.explorers, "explorer", "explorers")]
            if g.last_move:
                parts.append(f"last move {ago(g.last_move)}")
            if g.won:
                parts.append(f"{g.won} finished")
            lines.append("  " + GREY + " · ".join(parts) + RESET)
        if manuals and stem in manuals:
            # micron link syntax: leading backtick enters format mode, then
            # `[label`url] (verified: NomadNet Guide.py + reference
            # MicronParser.py + meshchat's MicronParser.js). Concatenated,
            # not an f-string: a literal '[' plus a subscript expression
            # trips PEP 701 bracket matching in CPython 3.14 (SyntaxError)
            lines.append("  `[manual`" + manuals[stem] + "]")
    # footer link (plain string, not an f-string — see the PEP 701 note
    # above): meshchat opens https:// link targets in a new browser tab
    lines.append(">")
    lines.append(DIV)
    lines.append("Mapping Helper App for Android: `[Droid-Mapper`https://github.com/jrodder/Droid-Mapper]")
    return "\n".join(lines) + "\n"


@dataclasses.dataclass
class GameStats:
    """One game's numbers, read off the save tree per page request.
    explorers = players with a slot; won = players whose metadata says a run
    ended; turns = moves fed for this game; last_move = newest slot mtime
    (0.0 = never played)."""
    explorers: int = 0
    won: int = 0
    turns: int = 0
    last_move: float = 0.0


@dataclasses.dataclass
class PlayerStats:
    per_game: dict  # {game: GameStats}
    total: int      # all-time unique players, all games (one identity, N games = 1)
    turns: int = 0  # moves fed, all games
    won: int = 0    # players who finished, all games


def player_stats(saves_root):
    """Player metrics from the autosave slot tree (spec §6 page). The slots
    ARE the registry (spec §5): one file per (game, player), rewritten every
    turn, never deleted — so slot count = players who ever started a game and
    mtime = that player's last turn. Each slot's sibling <sender>.json adds
    the move count and the win flag. The page reports what is on disk: prune
    the tree and every number follows it, no separate ledger to reconcile.
    A missing dir is the fresh-host state: no per-game entries, zeros."""
    per_game, identities = {}, set()
    for p in sorted(Path(saves_root).glob("*/*.zmsv")):
        m = _read_meta_json(p.with_suffix(".json"))
        g = per_game.setdefault(p.parent.name, GameStats())
        g.explorers += 1
        g.turns += _count(m.get("turns"))
        g.won += 1 if m.get("won") else 0
        g.last_move = max(g.last_move, p.stat().st_mtime)
        identities.add(p.stem)
    return PlayerStats(per_game, len(identities),
                       sum(g.turns for g in per_game.values()),
                       sum(g.won for g in per_game.values()))


def write_rns_config(config_dir, role, port=4242, instance_name=None,
                     overwrite=False):
    """Scaffold a minimal RNS 1.5 config. role "host" -> loopback
    TCPServerInterface; "client" -> TCPClientInterface to
    127.0.0.1:port. RNS 1.5.0 parses interfaces from a top-level
    [interfaces] section with nested [[name]] subsections (verified
    against site-packages/RNS/Reticulum.py and the wheel's default
    config); [logging] takes loglevel (file logging keys from older
    releases no longer exist). Non-clobbering: an operator-edited
    config (testnet/LoRa sections) survives restarts. Returns the
    config path."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config"
    if path.exists() and not overwrite:
        return path
    inst = instance_name or f"jmachine-{role}-{uuid.uuid4().hex[:8]}"
    if role == "host":
        ifc = ("    [[LAN TCP Server]]\n    type = TCPServerInterface\n"
               "    enabled = yes\n"
               f"    listen_ip = 127.0.0.1\n    listen_port = {port}\n")
    else:
        ifc = ("    [[TCP Client]]\n    type = TCPClientInterface\n"
               "    enabled = yes\n"
               f"    target_host = 127.0.0.1\n    target_port = {port}\n")
    # client stdout is a data contract (spec §7: norm(stdout) ==
    # session transcript) — scaffold it silent. NOTE: RNS 1.5.0 clamps
    # the config loglevel to 0..8 at parse time (RNS/Reticulum.py:468),
    # so the -1 value documents intent and clamps harmlessly to 0
    # (LOG_CRITICAL); the actual silencing is the runtime override in
    # Client.__init__ (RNS.loglevel = RNS.LOG_NONE). Host keeps verbose
    # (5): its stdout is not a data contract (Task 3 ruling).
    loglevel = 5 if role == "host" else -1
    path.write_text(
        "[reticulum]\n"
        "enable_transport = yes\n"
        "share_instance = yes\n"
        f"instance_name = {inst}\n\n"
        "[logging]\n"
        f"loglevel = {loglevel}\n\n"
        "[interfaces]\n\n"
        f"{ifc}\n")
    return path
