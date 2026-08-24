"""Pure host protocol (spec §4) — no RNS/LXMF imports; unit-testable with
fakes. Reply semantics: every reply is the cumulative transcript of that
host-side session (intro/restored batch + every turn since), so a reply is
self-contained and the player's phone needs no client-side state. A host
restart is the only case where the transcript restarts (restored batch =
prompt + status line; the phone's message history keeps the rest, spec §5).
"""
import dataclasses
import os
import sys
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
    st.transcript += _render(st.session.input(text))
    _autosave(store, game, sender, st.session)
    return st.transcript


def _new_game(game, sender, story_path, store, seed):
    """First contact: fresh Session + load; restore from the slot if a
    save exists (intro discarded). Corrupt/mismatched save -> log + fresh
    start (spec §4/§5)."""
    s = Session()
    _install_handlers(s, store, game, sender)
    batch = s.load(str(story_path), seed=seed)
    img = store.load(game, sender)
    if img is not None:
        try:
            batch = s.restore(img)
        except SaveFileError as e:
            print(f"jhost: {game}/{sender[:8]}: save restore failed ({e}); "
                  f"fresh start", file=sys.stderr)
    return GameState(s, _render(batch))


def _install_handlers(s, store, game, sender):
    """In-game @save/@restore -> host-local slot, no prompt (spec §5).
    The opcode's filename hint is ignored: the slot IS the identity."""
    def _save(_hint):
        try:
            store.save(game, sender, s.save())
            return True
        except OSError:
            return False

    def _restore(_hint):
        img = store.load(game, sender)
        if img is None:
            return False
        try:
            s.restore_image(img)
            return True
        except SaveFileError:
            return False

    s.set_save_handler(_save)
    s.set_restore_handler(_restore)


def _autosave(store, game, sender, s):
    """After every fed turn. A failed write never fails the turn
    (spec §4): log, retry next turn."""
    try:
        store.save(game, sender, s.save())
    except OSError as e:
        print(f"jhost: autosave failed {game}/{sender[:8]}: {e}",
              file=sys.stderr)


def render_page(name, games):
    """Micron page text (spec §6). games = [(stem, version, addr_str)];
    addr_str = pretty hexrep of the game's lxmf.delivery destination hash."""
    lines = [f">{name}", ">",
             "One-line Z-machine games over Reticulum.",
             "Send any message to a game's address to play;",
             "progress is saved per player automatically.",
             ">", ">Games"]
    for stem, version, addr in games:
        lines.append(f"> {stem} (v{version})")
        lines.append(f"  {addr}")
    return "\n".join(lines) + "\n"


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
