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
import threading
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


# The move odometer needs a floor. Its numbers come off slot metadata, which
# only exists for slots written after that feature landed (the reference
# host has 105 slots and 3 metadata files), and a purely derived total goes
# DOWN whenever metadata is lost or a slot is pruned -- an odometer that
# cannot go backwards must not depend on files that can vanish. So a turn that
# really lands also advances moves.json, and the page shows the higher of the
# two. Per-save story move counters are NOT used: the v3 score/turn globals
# 17/18 are an Inform convention and most titles here are Infocom's native
# engine, where those globals hold something else entirely.
MOVES_FILE = "moves.json"
WINS_FILE = "wins.json"
AGAIN = "again"        # host verb: [Game over] is not the end of a player's interest
_moves_lock = threading.Lock()


def _moves_path(saves_root):
    # inside the root, so one directory holds all the host's mutable state and
    # a test's temp saves dir can never reach a shared file. The slot scan is
    # '*/*.zmsv', so a top-level file here is never mistaken for a slot.
    return Path(saves_root) / MOVES_FILE


def _read_moves(saves_root):
    return _count(_read_meta_json(_moves_path(saves_root)).get("moves"))


def _write_moves(saves_root, n):
    p = _moves_path(saves_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(MOVES_FILE + ".tmp")
    tmp.write_text(json.dumps({"moves": int(n)}))
    os.replace(tmp, p)
    return p


def bump_moves(saves_root):
    """One fed turn advanced the all-time move odometer."""
    with _moves_lock:
        _write_moves(saves_root, _read_moves(saves_root) + 1)


def _wins_path(saves_root):
    # inside the root like moves.json; the slot scan is '*/*.zmsv' so a
    # top-level file is never mistaken for a slot
    return Path(saves_root) / WINS_FILE


def _read_wins(saves_root):
    w = _read_meta_json(_wins_path(saves_root)).get("wins")
    return w if isinstance(w, dict) else {}


def record_win(saves_root, game, sender):
    """A run the host saw end is remembered per (game, sender). The win flag
    in a slot's sidecar dies with the slot -- pruning a player retired their
    win -- and the Z-machine's end-of-run state gives no way to recover it
    once the evidence is gone, so the witness lives in its own file."""
    with _moves_lock:
        wins = _read_wins(saves_root)
        seen = wins.get(game)
        if not isinstance(seen, list):
            seen = []
        if sender not in seen:
            seen.append(sender)
            wins[game] = seen
            p = _wins_path(saves_root)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(WINS_FILE + ".tmp")
            tmp.write_text(json.dumps({"wins": wins}))
            os.replace(tmp, p)


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


def chunk_text(text, limit, parts=12):
    """Split a reply into <= `parts` pieces of at most `limit` UTF-8 bytes,
    breaking on newlines where one exists inside the budget.

    LXMF delivers anything longer than one link packet as an RNS Resource,
    which is a pull mechanism: the recipient has to ask for the parts. A phone
    on a mesh client does not, so a long reply is advertised forever and read
    as "no answer" -- while the turn it describes was already played. Short
    messages arrive; intros and long descriptions silently do not.

    `parts` bounds the burst: the transcript re-fetch returns the whole run, so
    an unbounded split would be dozens of transmissions over the radio."""
    if limit < 1:
        limit = 1
    lines = text.splitlines(keepends=True)
    out, buf = [], ""
    for line in lines:
        while len(line.encode()) > limit:       # one long line: hard split
            cut = len(line)
            while len(line[:cut].encode()) > limit:
                cut -= 1
            rest = line[cut:]
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:cut])
            line = rest
        candidate = buf + line
        if buf and len(candidate.encode()) > limit:
            out.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        out.append(buf)
    if len(out) > parts:
        kept = out[:parts]
        kept[-1] += f"\n[... {len(out) - parts} more parts cut -- send an " \
                    f"empty message for the rest]"
        out = kept
    return out or [""]


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
    text      message.content_as_string() (None -> ""); "again" on a finished
              run starts a new run in the same slot (counters kept)
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
    # done is checked after BOTH paths: the end-of-run flag rides inside the
    # autosave image, so a host restart replays a dead slot dead. Feeding a
    # dead run would otherwise count a move for a line the story refuses, and
    # would leave the player no way back but the operator's rm.
    if st.session.done:
        if text.strip().lower() == AGAIN:
            return _restart_run(game, sender, story_path, store, st, seed)
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
    bump_moves(store.root)
    if not st.won and any(isinstance(e, EndOfGame) for e in events):
        st.won = True
        record_win(store.root, game, sender)
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


def _restart_run(game, sender, story_path, store, st, seed):
    """`again` after [Game over]: a fresh run in the same slot. The counters
    are the player's history and stay exactly as they are -- turns fed, and a
    latched win -- only the story rewinds. The dead image is overwritten now
    instead of left to be replayed: the slot is rewritten, never deleted
    (spec §5)."""
    s = Session()
    st.session = s
    st.save_pending = False
    _install_handlers(s, st, store, game, sender)
    batch = s.load(str(story_path), seed=seed)
    st.transcript = _render(batch)
    _autosave(store, game, sender, s)
    return st.transcript


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


# Micron colour, chosen from what the clients actually paint rather than from
# the micron spec. Measured by running the npm micron-parser@1.1.0 that meshchat
# imports (components/nomadnetwork/NomadNetworkPage.vue:581 calls
# `new MicronParser()` with no argument, so darkTheme is always true) over
# tagged samples, then reading the container that html is injected into (same
# file, line 106: `<div class="... bg-black text-white nodeContainer">`, with no
# dark: variant). Three rules came out of that:
#   * The canvas is ALWAYS black, in both themes, so an accent only has to clear
#     black and no hue has to work on white: WCAG against #000 gives `Ffb5
#     12.5:1, `F8cf 12.1:1, `Fc9f 9.6:1, `F999 7.4:1.
#   * A '> ' line is a heading and the parser paints its OWN band (#222 on #bbb)
#     with its own foreground. Colouring the foreground alone loses to that band:
#     gold on it measured 1.1:1, which is exactly what reads as "monochrome".
#     Setting BOTH `F and `B overrides band and text (the parser copies
#     state.bg_color onto the heading div), and once both are ours the visitor's
#     theme cannot move the contrast: #eef on #442266 = 10.0:1, #9cf on
#     #222266 = 8.4:1, #aff on #115533 = 7.8:1.
#   * 3-hex only: the `FT + 6-hex truecolour form is NomadNet's, and meshchat's
#     parser honours truecolour in a form our markup never reaches.
# Tags: `Fxxx foreground, `Bxxx background, `f resets foreground, `b resets
# background, `! toggles bold, a bare ` resets fg+bg+formatting. One parser
# state covers the whole document, so style survives a newline: every coloured
# line closes itself.
# Bars are bg+fg together, hence theme-independent by construction:
BAR_TITLE = "`B631`Ffda`!"       # warm amber: this page paints its own headings
BAR_SECTION = "`B153`Faff`!"     # pale cyan on green: section headings
BAR_GAME = "`B226`F9cf`!"        # sky on indigo: one per game (the page's bulk)
BAR_ODOMETER = "`B321`!"         # warm band; the numbers keep their own hues
BAR_BOARD = "`B313`Ffaf`!"       # pink on dark magenta: the guestbook head
# Game names are TEXT, not a bar: a filled block behind 25+ names reads as a
# stack of grey slabs, and the parser's own heading band would add another.
# Warm + bold so a name still outranks the cool hues under it (cyan address,
# violet manual, green/grey/clay stats).
AMBER = "`Ffc9"                   # same hue without the bold toggle (the
GAME = AMBER + "`!"               # odometer band is already bold)
RESET_BAR = "`b"                 # drop the band, keep going on the line
# Accents on the black canvas, each closed with `f:
GOLD, TEAL, GREEN, CLAY = "`Ffb5", "`F4cb", "`F8c5", "`Ff88"
CYAN, VIOLET, PINK, GREY = "`F8cf", "`Fc9f", "`Ffaf", "`F999"
RESET = "`f"
RESET_ALL = "`"
BOLD = "`!"
# Closers, chosen by running the npm parser's own convertMicronToHtml (the
# path meshchat calls) over candidate markup: a bare ` after a ']' is eaten by
# link parsing, so it does NOT reset state and the band/bold bled into the next
# line. Closing every tag by name always works; `! is a toggle, so it pairs
# with the `! inside each BAR_*. CLOSE_HUE is for lines that never opened bold
# (toggling `! there would switch bold ON).
CLOSE_BAR = "`b`f`!"
CLOSE_HUE = "`b`f"
CLOSE_TEXT = "`f`!"               # bold text with no band behind it
DIV = "-"
# The Android map helper link (a plain string, not an f-string — see the PEP
# 701 note in render_page): meshchat's onNodePageUrlClick opens http(s) targets
# in a new browser tab (NomadNetworkPage.vue), so this leaves the mesh cleanly.
# Defined after the tags it uses.
MAPPER_LINK = (VIOLET + "`[Droid-Mapper (Android map helper)`"
               + "https://github.com/jrodder/Droid-Mapper]" + RESET)


def render_page(name, games, stats, manuals=None, guestbook=None, names=None):
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
    names = {stem: display title} from games/names.txt; a stem with no entry
    shows its stem. Multi-word titles are safe for the parse contract because
    a '> ' line only counts as a game when the line under it is an lxmf
    address link (see jclient parse_page).
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
    # Newest move anywhere on the host, with the game it happened in: counters
    # only ever grow, so they cannot answer "is anyone here right now?". The
    # piece is omitted until a slot exists, so a fresh host says nothing.
    recent = max(((g.last_move, stem) for stem, g in stats.per_game.items()
                  if g.last_move), default=None)
    if recent:
        bar.append(GREY + "last move " + ago(recent[0]) + RESET + " in "
                   + AMBER + (names or {}).get(recent[1], recent[1]) + RESET)
    # No bare ">" spacer: in micron that is an empty heading1, and theme-aware
    # clients paint it with their own band (meshchat's dark heading1 bg = bbb).
    lines = [BAR_TITLE + name + CLOSE_BAR,
             "Infocom-era text adventures, playable by message.",
             "Send any message to a game's address to play;",
             "progress is saved per player automatically.",
             BAR_ODOMETER + " · ".join(bar) + CLOSE_BAR,
             DIV]
    if guestbook:
        # ONE line, two links: the board on the left, the map helper to its
        # right. The mapper used to be the page's last line, i.e. below a
        # 25-game scroll, and it is a link people want before they play.
        # Each link closes its own styling: a bare ` right after ']' is eaten
        # by link parsing (measured), so the board closes with CLOSE_BAR before
        # the separator, and the mapper carries its own hue.
        lines.append(BAR_BOARD + "🖊️ `[Guestbook (post by message)`"
                     + guestbook + "]" + CLOSE_BAR
                     + GREY + " · " + RESET
                     + MAPPER_LINK)
        lines.append(DIV)   # a rule separates more strongly than a blank line
    lines.append(BAR_SECTION + "Games" + CLOSE_BAR)
    for stem, addr in games:
        # the bare '> name' line is jclient's parse anchor (spec §7): the
        # Z-machine version suffix used to sit here and added nothing a
        # player reads, while costing the parse contract a strict '(vN)'
        # shown = the display title from games/names.txt when the operator
        # gave one ('Hitchhikers Guide to the Galaxy' for h2g2); the stem is
        # still the identity/save key, and it stays the lookup key here, so a
        # rename can never orphan a player's slot tree. Operator-authored
        # file: keep titles plain text, no micron markup.
        shown = (names or {}).get(stem, stem)
        lines.append(GAME + shown + CLOSE_TEXT)
        # the address is its own link: the visible label is the prettyhexrep
        # <xx:..:xx> (copyable), and the link target is lxmf@<32-hex-hash>,
        # which meshchat/Sideband turn into an open-conversation action
        # (NomadNetworkPage.vue strips "lxmf@" and requires 32 hex chars,
        # no delimiters).
        # the leading ` before '[' is what enters micron link mode, so the
        # colour tags go OUTSIDE it and the label keeps its own `[...`...]`
        lines.append("  " + CYAN + "`[" + addr + "`lxmf@"
                     + addr.strip("<>").replace(":", "").lower() + "]"
                     + CLOSE_HUE)
        g = stats.per_game.get(stem)
        if g:  # no slot for this game -> no stats line at all (nothing true
               # to say; a fresh host does not advertise 0s per game)
            parts = [GREEN + f"{g.explorers} "
                     + plural(g.explorers, "explorer", "explorers") + RESET]
            if g.last_move:
                parts.append(GREY + f"last move {ago(g.last_move)}" + RESET)
            if g.won:
                parts.append(CLAY + f"{g.won} finished" + RESET)
            lines.append("  " + " · ".join(parts))
        if manuals and stem in manuals:
            # micron link syntax: leading backtick enters format mode, then
            # `[label`url] (verified: NomadNet Guide.py + reference
            # MicronParser.py + meshchat's MicronParser.js). Concatenated,
            # not an f-string: a literal '[' plus a subscript expression
            # trips PEP 701 bracket matching in CPython 3.14 (SyntaxError)
            lines.append("  " + VIOLET + "`[manual`" + manuals[stem] + "]"
                         + CLOSE_HUE)
    # the mapper link rides the guestbook line further up (it is not a game:
    # it is neither paired with an address link nor shaped like one)
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
    the move count and the win flag; a slot without one still proves its
    player fed at least one turn (the autosave follows a turn), so it counts
    as one move rather than zero. The turn total is the higher of that derived
    sum and the moves.json floor, so losing metadata cannot walk the odometer
    backwards. A missing dir is the fresh-host state: no per-game entries,
    zeros."""
    per_game, identities = {}, set()
    for p in sorted(Path(saves_root).glob("*/*.zmsv")):
        m = _read_meta_json(p.with_suffix(".json"))
        g = per_game.setdefault(p.parent.name, GameStats())
        g.explorers += 1
        g.turns += _count(m.get("turns")) or 1
        g.won += 1 if m.get("won") else 0
        g.last_move = max(g.last_move, p.stat().st_mtime)
        identities.add(p.stem)
    derived = sum(g.turns for g in per_game.values())
    path = _moves_path(saves_root)
    floor = _read_moves(saves_root)
    if not path.exists() and derived:
        # first look since the ledger existed: start it at what the slots
        # already prove, never at zero (that would restart the odometer)
        _write_moves(saves_root, derived)
        floor = derived
    for game, seen in _read_wins(saves_root).items():
        # a win the host witnessed stays counted even once the slot that
        # proved it is pruned (the end-of-run state is unrecoverable)
        if not isinstance(seen, list):
            continue
        g = per_game.setdefault(game, GameStats())
        g.won = max(g.won, len({s for s in seen if isinstance(s, str)}))
    return PlayerStats(per_game, len(identities), max(floor, derived),
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
