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
import os
import sys
import time
import uuid
from pathlib import Path

from zmach.events import Error, SaveFileError, StoryFileError, Text
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
                   story_path, seed=None):
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
    """
    if not verified:
        return "[Rejected: unverified sender]"
    if text is None:
        text = ""
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
    delta = _render(st.session.input(text))
    st.transcript += delta  # kept for the empty-message state re-fetch
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


def render_page(name, games, stats):
    """Micron page text (spec §6). games = [(stem, version, addr_str)];
    addr_str = pretty hexrep of the game's lxmf.delivery destination hash.
    stats = player_stats(...) — the page shows who plays: per-game player
    count, total unique people, recently active (spec §6). The stats lines
    are plain 2-space-indented text that parse_page's regexes ignore
    (verified by the network suite's live browse+parse)."""
    def plural(n, one, many):
        return one if n == 1 else many
    lines = [f">{name}", ">",
             "One-line Z-machine games over Reticulum.",
             "Send any message to a game's address to play;",
             "progress is saved per player automatically.",
             f"{stats.total} {plural(stats.total, 'person', 'people')} "
             f"{'has' if stats.total == 1 else 'have'} played · "
             f"{stats.recent} in the last 24 h",
             ">", ">Games"]
    for stem, version, addr in games:
        lines.append(f"> {stem} (v{version})")
        lines.append(f"  {addr}")
        n = stats.per_game.get(stem, 0)
        lines.append(f"  {n} {plural(n, 'player', 'players')}")
    return "\n".join(lines) + "\n"


RECENT_WINDOW = 24 * 3600  # spec §6 page: "in the last 24 h"


@dataclasses.dataclass
class PlayerStats:
    per_game: dict  # {game: unique player count}
    total: int      # unique players across all games (one person, N games = 1)
    recent: int     # unique players active within RECENT_WINDOW


def player_stats(saves_root, now=None):
    """Player metrics from the autosave slot files (spec §6 page). The
    slots ARE the registry: one file per (game, player), rewritten every
    turn, never deleted (spec §5) — file count = players ever, mtime =
    last turn. A player's recency uses their newest slot across games
    (played two games a day apart = one recent person). A missing dir is
    the fresh-host state: all zeros. now injectable so tests need no
    utime for the window itself."""
    now = time.time() if now is None else now
    per_game = {}
    newest = {}
    for p in sorted(Path(saves_root).glob("*/*.zmsv")):
        per_game[p.parent.name] = per_game.get(p.parent.name, 0) + 1
        mt = p.stat().st_mtime
        if mt > newest.get(p.stem, 0):
            newest[p.stem] = mt
    return PlayerStats(per_game, len(newest),
                       sum(1 for mt in newest.values()
                           if mt >= now - RECENT_WINDOW))


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
