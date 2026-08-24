# J-Machine Phase 2 Implementation Plan — Reticulum Game Host (jhost)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A long-running game host that serves Z-machine stories to multiple players over Reticulum: NomadNet micron page for discovery, one static LXMF address per game, one `Session` per player per game, autosave-per-turn, lossless reconnect.

**Architecture:** `jhost/` is a second consumer of Phase 1's `Session` API. All protocol logic lives in `jhost/protocol.py` as pure functions (no RNS import — unit-testable in milliseconds); `jhost/host.py` is thin RNS/LXMF wiring (one process, one RNS instance, one `LXMRouter` per game, one global lock). `jclient/` is a minimal Python LXMF client mirroring Sideband's wire path (test client; the real client is Sideband on a phone). Network tests run two real RNS processes over loopback TCP.

**Tech Stack:** Python ≥ 3.10. `zmach/` stays stdlib-only. `jhost/` and `jclient/` import `rns>=1.5,<1.6` + `lxmf>=1.1,<1.2` (verified API: rns 1.5.0 / lxmf 1.1.1, spec §2). Dev oracle: dfrotz at `/usr/games/dfrotz`.

**Spec:** `docs/superpowers/specs/2026-08-24-reticulum-game-host-design.md` — read it first; this plan implements it. Spec §2 "Verified facts" are verified against clean PyPI installs (rns 1.5.0, lxmf 1.1.1, rns-page-node 1.5.1) with line numbers — **do not re-derive them**; when in doubt, read the cited source file in the venv (`site-packages/RNS/Destination.py` etc.).

## Global Constraints

- **One-way dependency:** `zmach/` and its tests never import RNS/LXMF. Only `jhost/` and `jclient/` do (spec §1). `jhost/protocol.py` specifically is stdlib-only (it imports `zmach` only) — that split is what makes the protocol unit-testable without RNS.
- **Version pins:** `rns>=1.5,<1.6`, `lxmf>=1.1,<1.2` (installed in a `.venv`; `zmach` itself remains hard-dependency-free stdlib).
- **RNS is a process singleton** — re-init in one process raises `OSError`. Host and client in the test rig are therefore **separate OS processes** (loopback TCP: host `TCPServerInterface` 127.0.0.1:4242, client `TCPClientInterface` → 127.0.0.1:4242; `share_instance=yes`, unique `instance_name` per process).
- **Game protocol:** LXMF delivery messages. One game line in per message → one reply out = the **cumulative transcript** of that host-side session (intro/restored batch + every turn, `Error` events inline as `[error] ...`). Reply is self-contained; no client-side state needed. (Spec §4: "reply = transcript so far + the turn's Text data".)
- **Input cap:** 200 chars per line (trust boundary, untrusted network input). Output uncapped (RNS auto-chunks large responses, verified spec §2).
- **Identity = save slot:** player's LXMF delivery destination hash (32-hex) is the save-slot key. Saves: `data/saves/<game>/<sender_hex>.zmsv`, ZMSAVE v1, atomic write, never deleted.
- **Stamps:** host inbound stamp cost 0 (free to play). Stamps are proof-of-work CPU, no credits to administer.
- **No application-level dedup or ordering queue** — LXMF hash-dedup covers retransmission (verified); the one-line-in/one-reply-out play pattern makes crossing moot.
- **No scale/DoS hardening** (spec §1 non-goals). One global lock for turn atomicity with a `# ponytail:` comment naming the upgrade path.
- Tests: stdlib `unittest`. Unit tests run under plain `python3`; network tests skip cleanly when `RNS`/`LXMF` are not importable (stdlib suite stays green anywhere). Network tests run with the venv interpreter (`.venv/bin/python`).
- **All source files end with a trailing newline (POSIX).** One commit per task.
- Commit message convention: `feat: …` / `test: …` / `docs: …` (Phase 1 history).

## Protocol decisions (plan-level, arguing from the spec)

1. **Cumulative reply** (above): the session's transcript starts at its `load`/`restore` in this host process's lifetime. A *client* restart against a live host gets the full transcript back (session is in the in-memory map). A *host* restart gets the restored batch (prompt + status line) — the phone's message history keeps the older turns; spec §5 says exactly this ("reply = the restored batch's text (prompt + status line)").
2. **Session map value:** `GameState(session, transcript)` — a dataclass, because the reply needs the accumulated text, not just the `Session`.
3. **Two players, one game** = two independent `GameState` entries (session map is keyed `(game, sender)`; spec §5: "Two different identities: fully independent"). The two-players network test therefore asserts *each* player's final reply equals dfrotz of *that player's own* line sequence (same host seed), with alternating sends to exercise the lock — "no cross-talk".
4. **Config scaffold is non-clobbering:** `write_rns_config` only writes when the config file is missing (an operator-edited config with testnet/LoRa sections survives restarts). Fresh temp dirs in tests always get the scaffold.
5. **jclient is the test client** (spec §7); its scaffolded RNS config (loopback) is by design — the production client is Sideband using the phone's real `~/.rns`.

---

### Task 1: `jhost/protocol.py` — pure protocol core + unit tests (spec tests 6–7)

**Files:**
- Create: `jhost/__init__.py` (empty), `jhost/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces (used by Tasks 3–5, exact signatures):
  - `INPUT_CAP = 200`
  - `unpretty(s) -> bytes` — `"<ab:cd:…>"` (RNS pretty hexrep) or plain hex → 16 bytes
  - `DEST_JSON = "host-destinations.json"` — the destinations filename (host writes it; also the rig's hash handoff)
  - `class SaveStore` — `load(game, sender) -> bytes | None`, `save(game, sender, image) -> None`
  - `class FileSaveStore(SaveStore)` — `FileSaveStore(root)`, same methods; files at `root/<game>/<sender>.zmsv`, atomic (temp + `os.replace`)
  - `@dataclass class GameState` — fields `session: Session`, `transcript: str`
  - `handle_message(game, sender, text, verified, sessions, store, story_path, seed=None) -> str` — one inbound message → reply text; mutates `sessions: dict{(game, sender): GameState}` in place; never raises on protocol failures (rejection/done/corrupt-save are replies)
  - `render_page(name, games) -> str` — `games: list[(stem, version:int, addr_str)]`
  - `write_rns_config(config_dir, role, port=4242, instance_name=None, overwrite=False) -> Path` — `role` `"host"`|`"client"`; returns the config path; does not overwrite an existing config unless `overwrite=True`
  - Reply constants (exact strings): `"[Rejected: unverified sender]"`, `"[Input rejected: line too long (>200)]"`, `"[Game over]"`

- [ ] **Step 1: Write the failing tests** — `tests/test_protocol.py`

```python
"""Spec §7 tests 6-7: protocol edge-behavior table + save sanity.
No RNS import — milliseconds each."""
import tempfile
import unittest
from pathlib import Path

from jhost.protocol import (FileSaveStore, INPUT_CAP, handle_message,
                            render_page, write_rns_config)
from tests.conformance.run_conformance import play_session_lines
from tests.util import norm
from zmach.events import Text
from zmach.session import Session

C = Path(__file__).parent / "corpus"
ZORK = C / "zork1.z3"
PF = C / "planetfall.z5"
RISORG = C / "risorg.z8"
SEED = 10
S1 = "aa" * 16  # player "A" delivery hash (hex form)


class P:
    """Fixture: fresh sessions map + FileSaveStore per test."""

    def __init__(self, story=ZORK):
        self.d = tempfile.TemporaryDirectory()
        self.store = FileSaveStore(Path(self.d.name) / "saves")
        self.sessions = {}
        self.story = story
        self.game = Path(story).stem

    def call(self, text="", sender=S1, verified=True, seed=SEED):
        return handle_message(self.game, sender, text, verified,
                              self.sessions, self.store, str(self.story), seed)

    def close(self):
        self.d.cleanup()


def text_of(events):
    return "".join(e.data for e in events if isinstance(e, Text))


class Protocol(unittest.TestCase):
    def setUp(self):
        self.p = P()

    def tearDown(self):
        self.p.close()

    def test_unverified_rejected(self):
        r = self.p.call(text="look", verified=False)
        self.assertEqual(r, "[Rejected: unverified sender]")
        self.assertEqual(self.p.sessions, {})

    def test_first_contact_intro(self):
        r = self.p.call(text="")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, [], seed=SEED)))

    def test_first_contact_with_line(self):
        r = self.p.call(text="look")
        self.assertEqual(norm(r),
                         norm(play_session_lines(ZORK, ["look"], seed=SEED)))

    def test_first_contact_restore(self):
        # pre-build a 3-turn save, then first contact from that identity
        s = Session()
        s.load(str(ZORK), seed=SEED)
        for l in ["look", "north", "look"]:
            s.input(l)
        img = s.save()
        self.p.store.save(self.p.game, S1, img)
        s2 = Session()
        s2.load(str(ZORK), seed=SEED)  # intro discarded by the protocol
        ref = text_of(s2.restore(img))
        self.assertEqual(norm(self.p.call(text="")), norm(ref))

    def test_first_contact_restore_with_line(self):
        s = Session()
        s.load(str(ZORK), seed=SEED)
        for l in ["look", "north", "look"]:
            s.input(l)
        img = s.save()
        self.p.store.save(self.p.game, S1, img)
        s2 = Session()
        s2.load(str(ZORK), seed=SEED)
        ref = text_of(s2.restore(img)) + text_of(s2.input("south"))
        self.assertEqual(norm(self.p.call(text="south")), norm(ref))

    def test_corrupt_save_fresh_start(self):
        self.p.store.save(self.p.game, S1, b"not-a-save" * 50)
        r = self.p.call(text="")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, [], seed=SEED)))

    def test_existing_session_empty_line(self):
        self.assertEqual(self.p.call(text=""), self.p.call(text=""))

    def test_input_cap(self):
        r = self.p.call(text="x" * (INPUT_CAP + 1))
        self.assertEqual(r, "[Input rejected: line too long (>200)]")
        # session intact: the next valid line plays
        r2 = self.p.call(text="look")
        self.assertEqual(norm(r2),
                         norm(play_session_lines(ZORK, ["look"], seed=SEED)))

    def test_done_session(self):
        p = P(story=PF)
        for l in ["look", "open mailbox", "take leaflet", "north", "east",
                  "south", "west", "look"]:
            p.call(text=l)
        p.call(text="quit")
        self.assertTrue(p.sessions[("planetfall", S1)].session.done)
        for t in ["", "look", "whatever"]:
            self.assertEqual(p.call(text=t), "[Game over]")
        p.close()

    def test_ingame_save_restore(self):
        # risorg's in-game SAVE/RESTORE verbs -> host-local slot, no prompt.
        # risorg consumes one startup line before its intro -> the
        # first-contact line "" IS that startup line.
        p = P(story=RISORG)
        self.assertIsNone(p.store.load("risorg", S1))
        p.call(text="")
        self.assertIn("Ok.", p.call(text="save"))
        self.assertIsNotNone(p.store.load("risorg", S1))
        self.assertIn("Ok.", p.call(text="restore"))
        # the slot image round-trips into a fresh Session (spec §7 test 7)
        img = p.store.load("risorg", S1)
        s = Session()
        s.load(str(RISORG), seed=SEED)
        s.restore(img)
        # and the look after restore shows the save-point room
        room = norm(play_session_lines(RISORG, ["", "look"], seed=SEED))
        intro = norm(play_session_lines(RISORG, [""], seed=SEED))
        self.assertIn(room[len(intro):], norm(p.call(text="look")))
        p.close()

    def test_autosave_roundtrip(self):
        p = P()
        p.call(text="look")
        r2 = p.call(text="north")
        img = p.store.load("zork1", S1)
        self.assertIsNotNone(img)
        s = Session()
        s.load(str(ZORK), seed=SEED)
        s.restore(img)
        ref = text_of(s.input("south"))
        r3 = p.call(text="south")
        # reply is cumulative -> the last turn's text is the suffix
        self.assertEqual(norm(r3[len(r2):]), ref)


class Helpers(unittest.TestCase):
    def test_file_save_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            st = FileSaveStore(d)
            self.assertIsNone(st.load("g", "s"))
            st.save("g", "s", b"img")
            self.assertEqual(st.load("g", "s"), b"img")
            self.assertFalse(list(Path(d).rglob("*.tmp")))

    def test_render_page(self):
        page = render_page("J-Machine Games",
                           [("zork1", 3, "<ab:cd:ef>"),
                            ("planetfall", 5, "<90:12:34>")])
        self.assertTrue(page.startswith(">J-Machine Games\n>"))
        self.assertIn("> zork1 (v3)\n  <ab:cd:ef>", page)
        self.assertIn("> planetfall (v5)\n  <90:12:34>", page)

    def test_write_rns_config(self):
        with tempfile.TemporaryDirectory() as d:
            h = write_rns_config(d + "/hostcfg", "host")
            c = write_rns_config(d + "/clientcfg", "client")
            ht, ct = h.read_text(), c.read_text()
            for k in ("type = TCPServerInterface", "listen_port = 4242",
                      "share_instance = yes", "[[Logging]]"):
                self.assertIn(k, ht)
            for k in ("type = TCPClientInterface", "target_port = 4242"):
                self.assertIn(k, ct)
            # non-clobbering: second call must not replace the file
            first = h.read_text()
            write_rns_config(d + "/hostcfg", "host")
            self.assertEqual(first, h.read_text())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jhost'`
- [ ] **Step 3: Implement `jhost/__init__.py` (empty) and `jhost/protocol.py`**

```python
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

from zmach.events import Error, SaveFileError, Text
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
            out.append(f"[error] {e.data}")
    return "".join(out)


def handle_message(game, sender, text, verified, sessions, store,
                   story_path, seed=None):
    """One inbound LXMF message -> reply text. Never raises on protocol
    failures: rejection/done/corrupt-save are replies (spec §4).

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
        sessions[key] = st = _new_game(game, sender, story_path, store, seed)
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
    """Scaffold a minimal RNS 1.5 config (format verified, spec §2).
    role "host" -> loopback TCPServerInterface; "client" ->
    TCPClientInterface to 127.0.0.1:port. Non-clobbering: an
    operator-edited config (testnet/LoRa sections) survives restarts.
    Returns the config path."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config"
    if path.exists() and not overwrite:
        return path
    inst = instance_name or f"jmachine-{role}-{uuid.uuid4().hex[:8]}"
    if role == "host":
        ifc = ("[[LAN TCP Server]]\ntype = TCPServerInterface\n"
               "enabled = yes\n"
               f"listen_ip = 127.0.0.1\nlisten_port = {port}\n")
    else:
        ifc = ("[[TCP Client]]\ntype = TCPClientInterface\n"
               "enabled = yes\n"
               f"target_host = 127.0.0.1\ntarget_port = {port}\n")
    path.write_text(
        "[reticulum]\n"
        "enable_transport = yes\n"
        "share_instance = yes\n"
        f"instance_name = {inst}\n\n"
        f"{ifc}\n"
        "[[Logging]]\n"
        "log_to_file = yes\n"
        f"log_file = {config_dir / 'rns.log'}\n"
        "log_level = 5\n")
    return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 13 PASS

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python3 -m unittest discover -s tests`
Expected: all green (Phase 1 count + 13)

- [ ] **Step 6: Commit**

```bash
git add jhost/ tests/test_protocol.py
git commit -m "feat: jhost protocol core — pure message handling, save stores, page/config (spec §4/§6)"
```

---

### Task 2: venv + rig smoke test (spec test 0) — the highest-risk unknown

**Files:**
- Create: `.venv/` (not committed), `tests/network/__init__.py` (empty), `tests/network/netrig.py`, `tests/network/test_netrig.py`
- Modify: `.gitignore` (add `.venv/`)

**Interfaces:**
- Consumes: `jhost.protocol.write_rns_config` (Task 1)
- Produces (used by Task 5, exact names):
  - `netrig.DEST_JSON` — re-export of `jhost.protocol.DEST_JSON`
  - `netrig.spawn(argv, name, out_dir) -> subprocess.Popen` — host-style subprocess; stdout+stderr → `out_dir/proc-<name>.log`
  - `netrig.run_captured(argv, name, out_dir, timeout=180) -> (rc, out_text, err_text)` — run to completion, tee to `proc-<name>.log`
  - `netrig.play_once(game_addr, lines, work_dir, name, timeout=180, port=4242) -> (rc, stdout_text)` — `jclient play` subprocess, all lines on stdin at once
  - `netrig.play_proc(game_addr, work_dir, name, port=4242) -> subprocess.Popen` — same but live pipes (stdin pacing for the two-players test)
  - `netrig.wait_file(path, timeout=120, interval=1.0) -> bool`
  - `netrig.logs_tail(dir, n=40) -> str` — tail of every `*.log` under dir
  - `netrig.unpretty` — re-export of `jhost.protocol.unpretty` (stdlib-only)

- [ ] **Step 1: Create the venv and install the pins**

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip "rns>=1.5,<1.6" "lxmf>=1.1,<1.2"
.venv/bin/python -c "import RNS; print(RNS.__version__)"
```
Expected: `1.5.x`. **If the install fails on this Python (3.14):** create the venv with the newest available older interpreter that works (e.g. `python3.12 -m venv .venv`) and note it in the commit message. If no interpreter can install rns 1.5.x, **stop and report** — that is a design-level finding (spec §7 Task-0 exit condition), not something to paper over.

- [ ] **Step 2: Write the failing test** — `tests/network/test_netrig.py`

```python
"""Spec §7 test 0: rig smoke. Real RNS 1.5.0 / LXMF 1.1.1 over loopback
TCP in two OS processes: announce -> recall -> RNS request (page) ->
LXMF delivery -> reply. Proves the transport layer before the protocol
is built on it."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    import RNS  # noqa: F401
    import LXMF  # noqa: F401
    HAVE_RNS = True
except ImportError:
    HAVE_RNS = False


@unittest.skipUnless(HAVE_RNS, "rns/lxmf not installed (pip install rns lxmf)")
class Smoke(unittest.TestCase):
    def test_smoke(self):
        from tests.network import netrig
        with tempfile.TemporaryDirectory() as d:
            host_d, client_d = Path(d) / "host", Path(d) / "client"
            h = netrig.spawn([sys.executable, "-u", "-m", "jhost",
                              str(Path(__file__).parent.parent / "corpus"),
                              "--data-dir", str(host_d), "--name", "Smoke"],
                             "host", host_d)
            try:
                self.assertTrue(
                    netrig.wait_file(host_d / netrig.DEST_JSON, 120),
                    netrig.logs_tail(host_d))
                rc, out, err = netrig.run_captured(
                    [sys.executable, "-u", "-m", "jclient", "smoke",
                     "--data-dir", str(client_d),
                     "--host-json", str(host_d / netrig.DEST_JSON)],
                    "client", client_d, timeout=180)
                self.assertEqual(rc, 0,
                                 out + "\n== host ==\n" + netrig.logs_tail(host_d))
                self.assertIn("SMOKE-OK", out)
            finally:
                h.terminate()
                h.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m unittest tests.network.test_netrig -v`
Expected: FAIL — `No module named jhost` (host not built yet) or `No module named jclient`

- [ ] **Step 4: Implement `tests/network/__init__.py` (empty) and `tests/network/netrig.py`**

The rig helpers are subprocess/log plumbing. The smoke *client* (`jclient smoke`, built in Task 4) is raw RNS/LXMF — it proves the 1.5.0/1.1.1 API surface (announce → recall → RNS request → LXMF delivery → reply) on its own, with no jhost protocol involved. Every RNS/LXMF call used anywhere in this plan is a spec-§2-verified signature; if one is wrong at runtime, read the cited venv source file (`site-packages/RNS/*.py`, `site-packages/LXMF/*.py`) rather than guessing.

```python
"""Test-only RNS rig (spec §7): two OS processes, real RNS instances,
loopback TCP pairing, temp data dirs. RNS is a process singleton (spec §2),
so host and client are subprocesses. Host = `python -m jhost`; clients =
`python -m jclient`. Hash handoff via data/host-destinations.json (also the
operator feature, spec §3).
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jhost.protocol import DEST_JSON, unpretty  # re-exports (stdlib-only)


# ---------------------------------------------------------------- rig
def spawn(argv, name, out_dir):
    """Host-style subprocess: stdout+stderr to out_dir/proc-<name>.log."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fh = open(out_dir / f"proc-{name}.log", "wb")
    return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT)


def run_captured(argv, name, out_dir, timeout=180):
    """Run argv to completion; tee output to out_dir/proc-<name>.log.
    Returns (rc, stdout_text, stdout_text) — client CLI uses one stream."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(out_dir / f"proc-{name}.log", "wb")
    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        raise AssertionError(
            f"{name} timed out:\n{logs_tail(out_dir, 60)}") from None
    log.write(out)
    log.close()
    text = out.decode(errors="replace")
    return p.returncode, text, text


def wait_file(path, timeout=120, interval=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(interval)
    return False


def logs_tail(d, n=40):
    """Last n lines of every *.log under d (RNS failures are opaque
    without them, spec §7)."""
    out = []
    for p in sorted(Path(d).rglob("*.log")):
        out.append(f"== {p} ==\n"
                   + "\n".join(p.read_text(errors="replace").splitlines()[-n:]))
    return "\n".join(out) if out else "(no logs)"


# -------------------------------------------------------- jclient driver
def _client_argv(game_addr, work_dir, port=4242):
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    return [sys.executable, "-u", "-m", "jclient", "play", game_addr,
            "--identity", str(work_dir / "identity"),
            "--data-dir", str(work_dir), "--port", str(port)]


def play_once(game_addr, lines, work_dir, name, timeout=180, port=4242):
    """jclient play, all lines on stdin at once. Returns (rc, stdout).
    The client prints each reply's text verbatim (plus its own markers),
    so the tests compare `norm(stdout)` — see tests/network/test_network.py."""
    return _play_once_impl(_client_argv(game_addr, work_dir, port), name,
                           work_dir,
                           "".join(l + "\n" for l in lines).encode(), timeout)


def _play_once_impl(argv, name, work_dir, stdin_bytes, timeout):
    out_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(out_dir / f"proc-{name}.log", "wb")
    p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        raise AssertionError(
            f"{name} timed out:\n{logs_tail(out_dir, 60)}") from None
    log.write(out)
    log.close()
    return p.returncode, out.decode(errors="replace")


def play_proc(game_addr, work_dir, name, port=4242):
    """jclient play with live stdin pipes (two-players interleaving).
    stdout is line-streamed (python -u) so reply blocks arrive as printed."""
    out_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(_client_argv(game_addr, work_dir, port),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
```

- [ ] **Step 5: Implement the `jclient` stub needed by the smoke** — `jclient/__init__.py` (empty), `jclient/client.py`, `jclient/__main__.py` with ONLY the `smoke` command working (Task 4 adds `scan`/`browse`/`play`).

`jclient/client.py`:

```python
"""Minimal LXMF client (spec §7). Mirrors Sideband's wire path: own
delivery identity + one LXMRouter per process; send message to a delivery
address; receive replies via the delivery callback. Test client — the real
client is Sideband on a phone."""
import json
import sys
import time
from pathlib import Path

import RNS
from LXMF import LXMMessage, LXMRouter

from jhost.protocol import write_rns_config  # stdlib-only helper


def load_identity(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return RNS.Identity.from_file(str(path))
    ident = RNS.Identity()
    ident.to_file(str(path))
    return ident


class Client:
    def __init__(self, data_dir, identity_path, name="jclient", port=4242):
        self.data_dir = Path(data_dir)
        self.ident = load_identity(identity_path)
        cfg_dir = write_rns_config(self.data_dir / "rns", "client", port).parent
        RNS.Reticulum(str(cfg_dir))
        self.router = LXMRouter(identity=self.ident,
                                storagepath=str(self.data_dir / "lxmf"),
                                name=name)
        self.dest = RNS.Destination(self.ident, RNS.Destination.IN,
                                    RNS.Destination.SINGLE, "lxmf",
                                    "delivery")
        self.router.register_delivery_identity(self.ident, display_name=name,
                                               stamp_cost=0)
        self._replies = []
        self.router.register_delivery_callback(
            lambda msg: self._replies.append(msg.content_as_string() or ""))

    def recall(self, h, what, timeout=90):
        """Poll recall until the key appears (announce path, spec §7)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ident = RNS.Identity.recall(h)
            if ident is not None:
                return ident
            time.sleep(3)
        raise RuntimeError(f"cannot recall {what} after {timeout}s "
                           f"(was it announced? check host logs)")

    def request(self, dest, path, timeout=30):
        """RNS request/response (page fetch). Returns the response bytes."""
        got = []
        RNS.Link(dest).request(path, b"",
                               lambda r: got.append(r.response),
                               None, None, timeout=timeout,
                               max_response_size=65536)
        deadline = time.time() + timeout
        while not got and time.time() < deadline:
            time.sleep(1)
        return got[0] if got else None

    def send(self, game_addr, content, title):
        ident = self.recall(game_addr, "game address")
        dest = RNS.Destination(ident, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        m = LXMMessage(dest, self.dest,
                       content=content.encode(), title=title)
        self.router.handle_outbound(m)

    def wait_reply(self, known, timeout=90):
        """Block until a reply arrives that isn't in `known` (set of the
        transcript strings seen so far). Returns the new reply string."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for r in list(self._replies):
                if r not in known:
                    return r
            time.sleep(2)
        raise TimeoutError("no reply")
```

`jclient/__main__.py` (Task 2 version — `smoke` only):

```python
"""python3 -m jclient <smoke|scan|browse|play> ... — Task 2: smoke only;
Task 4 adds scan/browse/play."""
import argparse
import json
import sys
from pathlib import Path

import RNS

from .client import Client
from jhost.protocol import unpretty  # stdlib-only helper


def cmd_smoke(a):
    c = Client(a.data_dir, Path(a.data_dir) / "identity", name="jclient")
    dests = json.loads(Path(a.host_json).read_text())
    # 1) page fetch (RNS request/response — the NomadNet page convention)
    page_ident = c.recall(unpretty(dests["page"]), "page node")
    page_dest = RNS.Destination(page_ident, RNS.Destination.OUT,
                                RNS.Destination.SINGLE, "nomadnetwork",
                                "node")
    page = c.request(page_dest, "/page/index.mu")
    if page is None or b">" not in page:
        print(f"page fetch failed: {page!r}", file=sys.stderr)
        return 1
    print(page.decode(errors="replace"), end="")
    # 2) LXMF message -> delivery -> reply
    c.send(unpretty(list(dests["games"].values())[0]), b"hello", "smoke")
    print(c.wait_reply(set()), end="")
    print("\nSMOKE-OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="jclient")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--host-json", required=True)
    a = ap.parse_args()
    if a.cmd == "smoke":
        sys.exit(cmd_smoke(a))
```

Note: the `smoke` command is test-only by design (a production client would type/paste hashes and use `scan`); `unpretty` lives in `jhost/protocol.py` (stdlib-only) so both `jclient` and `tests/network/netrig` share it.

- [ ] **Step 6: Run the smoke — this is the risk gate**

Run: `.venv/bin/python -m unittest tests.network.test_netrig -v`
Expected: 1 PASS (may take 30–90 s — RNS startup + announce + path discovery).

**If it fails:** the assertion message carries the client output + tail of both `proc-*.log` and `rns.log` files. The identified fallback (spec §7): a unix-socket `LocalInterface` pair instead of loopback TCP — change only `write_rns_config`'s interface sections (verified same code path). **If neither works: stop and report as a design-level finding. Do not build the protocol on a broken transport.**

- [ ] **Step 7: Commit**

```bash
git add .gitignore tests/network/ jclient/
git commit -m "test: RNS rig smoke (spec test 0) — real 1.5.0/1.1.1 loopback pairing green"
```
(Include the `.gitignore` change: add `.venv/`.)

---

### Task 3: `jhost/host.py` + CLI — the real host (spec §3)

**Files:**
- Create: `jhost/host.py`, `jhost/__main__.py`

**Interfaces:**
- Consumes: `jhost.protocol.{handle_message, FileSaveStore, render_page, write_rns_config}`; `Session` (zmach); raw RNS/LXMF (spec §2 signatures)
- Produces:
  - `Host(data_dir, games_dir, name="J-Machine Games", seed=None, port=4242)` with `.start()` (RNS init, destinations, routers, first announce, destinations file) and `.run()` (blocks; re-announces every `ANNOUNCE_INTERVAL = 300` s)
  - writes `<data_dir>/host-destinations.json` = `{"page": "<hash>", "games": {stem: "<hash>"}}` (RNS pretty hexrep) — the operator record and the rig's hash handoff
  - `python3 -m jhost <games-dir> [--data-dir DIR] [--name NAME] [--seed N] [--port N]`
- No new unit tests: the pure logic was tested in Task 1; the RNS wiring is proven end-to-end by the Task 2 smoke (the smoke test runs the REAL host — Step 5 re-runs it after this task) and Task 5.

- [ ] **Step 1: Implement `jhost/host.py`**

```python
"""Reticulum game host (spec §3): one process, one RNS instance, one
LXMRouter per game (lxmf 1.1.1 allows one delivery identity per router —
verified spec §2), per-game persisted identity = the static per-game
address.

# ponytail: global lock, per-session locks if throughput matters
"""
import json
import sys
import threading
import time
from pathlib import Path

import RNS
from LXMF import LXMMessage, LXMRouter

from .protocol import (DEST_JSON, FileSaveStore, handle_message,
                       render_page, write_rns_config)

ANNOUNCE_INTERVAL = 300  # seconds (spec §3)


class Host:
    def __init__(self, data_dir, games_dir, name="J-Machine Games",
                 seed=None, port=4242):
        self.data_dir = Path(data_dir)
        self.games_dir = Path(games_dir)
        self.name = name
        self.seed = seed
        self.port = port
        self.lock = threading.Lock()
        self.sessions = {}            # {(game, sender): GameState}
        self.store = FileSaveStore(self.data_dir / "saves")
        self.routers = {}             # stem -> LXMRouter
        self.destinations = {}        # stem -> delivery Destination
        self.stories = {}             # stem -> story Path
        self.page_dest = None

    # ------------------------------------------------ lifecycle
    def start(self):
        cfg_dir = self.data_dir / "rns"
        existed = (cfg_dir / "config").exists()
        write_rns_config(cfg_dir, "host", self.port)
        if not existed:
            print(f"jhost: scaffolded RNS config at {cfg_dir / 'config'} "
                  f"(loopback only) — add your transports there and "
                  f"restart", file=sys.stderr)
        RNS.Reticulum(str(cfg_dir))

        page_ident = self._identity("page")
        self.page_dest = RNS.Destination(page_ident, RNS.Destination.IN,
                                         RNS.Destination.SINGLE,
                                         "nomadnetwork", "node")
        self.page_dest.register_request_handler(
            "/page/index.mu", self._page_handler,
            allow=RNS.Destination.ALLOW_ALL)

        for story in sorted(self.games_dir.glob("*.z[358]")):
            self._add_game(story)

        self._announce_all()
        self._write_destinations()
        print(f"jhost: serving {len(self.routers)} game(s):",
              file=sys.stderr)
        for stem in sorted(self.destinations):
            d = self.destinations[stem]
            print(f"jhost:   {stem}: {RNS.prettyhash(d.hash)}",
                  file=sys.stderr)

    def run(self):
        """Block, re-announcing on the interval (spec §3)."""
        while True:
            time.sleep(ANNOUNCE_INTERVAL)
            self._announce_all()

    # ------------------------------------------------ internals
    def _identity(self, stem):
        """Persisted identity = stable address across restarts (spec §3:
        destination hash is a deterministic function of identity)."""
        p = self.data_dir / "identities" / stem
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            return RNS.Identity.from_file(str(p))
        ident = RNS.Identity()
        ident.to_file(str(p))
        return ident

    def _add_game(self, story):
        stem = story.stem
        ident = self._identity(stem)
        router = LXMRouter(identity=ident,
                           storagepath=str(self.data_dir / "lxmf" / stem),
                           name=stem)
        # stamp_cost=0: free to play (spec §9; stamps are PoW, no credits)
        router.register_delivery_identity(ident, display_name=stem,
                                          stamp_cost=0)
        dl = RNS.Destination(ident, RNS.Destination.IN,
                             RNS.Destination.SINGLE, "lxmf", "delivery")
        router.register_delivery_callback(
            lambda msg: self._on_message(stem, msg))
        self.routers[stem] = router
        self.destinations[stem] = dl
        self.stories[stem] = story

    def _page_handler(self, *args):
        # (path, data, request_id, [link_id,] remote_identity, requested_at)
        games = [(stem, self._version(stem), RNS.prettyhash(d.hash))
                 for stem, d in sorted(self.destinations.items())]
        return render_page(self.name, games).encode()

    def _version(self, stem):
        # story header bytes 0-1 (Phase 1 verified fact)
        data = Path(self.stories[stem]).read_bytes()
        return int.from_bytes(data[0:2], "big")

    def _on_message(self, stem, msg):
        sender = msg.source_hash.hex()
        text = msg.content_as_string()  # None if not valid UTF-8
        with self.lock:
            reply = handle_message(stem, sender, text,
                                   msg.signature_validated, self.sessions,
                                   self.store, str(self.stories[stem]),
                                   self.seed)
        src = RNS.Identity.recall(bytes.fromhex(sender))
        if src is None:
            print(f"jhost: {stem}: cannot recall {sender[:8]} for reply",
                  file=sys.stderr)
            return
        dest = RNS.Destination(src, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        # reply pattern verified spec §2; output uncapped (RNS auto-chunks)
        m = LXMMessage(dest, content=reply.encode(), title=stem)
        self.routers[stem].handle_outbound(m)

    def _announce_all(self):
        self.page_dest.announce(app_data=self.name.encode())
        for stem, d in self.destinations.items():
            d.announce(app_data=stem.encode())

    def _write_destinations(self):
        out = {"page": RNS.prettyhash(self.page_dest.hash),
               "games": {s: RNS.prettyhash(d.hash)
                         for s, d in sorted(self.destinations.items())}}
        (self.data_dir / DEST_JSON).write_text(json.dumps(out, indent=1))
```

- [ ] **Step 2: Implement `jhost/__main__.py`**

```python
"""python3 -m jhost <games-dir> [--data-dir DIR] [--name NAME] [--seed N]
[--port N]"""
import argparse

from .host import Host


def main():
    ap = argparse.ArgumentParser(prog="jhost")
    ap.add_argument("games_dir")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--name", default="J-Machine Games")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--port", type=int, default=4242)
    a = ap.parse_args()
    host = Host(a.data_dir, a.games_dir, a.name, a.seed, a.port)
    host.start()
    host.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Re-run the smoke test against the real host**

The Task 2 smoke test already spawns `python -m jhost` — it now exercises the full real host (page handler, LXMRouter, delivery callback, reply).

Run: `.venv/bin/python -m unittest tests.network.test_netrig -v`
Expected: 1 PASS

- [ ] **Step 4: Manual boot check (10 seconds)**

```bash
.venv/bin/python -m jhost tests/corpus --data-dir /tmp/jhost-manual
```
Expected: "scaffolded RNS config" line, then `serving 9 game(s):` with one pretty-hex address per stem, then blocks. `Ctrl-C`. `cat /tmp/jhost-manual/host-destinations.json` shows `page` + `games`. Run the same command again → **no** "scaffolded" line (non-clobbering) and the **same** addresses (persisted identities = static addresses, spec §3).

- [ ] **Step 5: Run the full unit suite (no regressions)**

Run: `python3 -m unittest discover -s tests`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add jhost/host.py jhost/__main__.py
git commit -m "feat: jhost — RNS page node + per-game LXMRouter, autosave slots, destinations file (spec §3)"
```

---

### Task 4: `jclient` complete — `scan` / `browse` / `play` (spec §7)

**Files:**
- Modify: `jclient/client.py` (add `parse_page`)
- Replace: `jclient/__main__.py` (add `scan`, `browse`, `play` to the Task 2 `smoke`)

**Interfaces:**
- Consumes: `Client` (Task 2), `jhost.protocol.{unpretty, render_page}` (for the page-format contract)
- Produces:
  - `jclient.client.parse_page(text) -> list[(name:str, version:int, addr:str)]` — pure; parses the `render_page` format (`> name (vN)` line followed by a 2-space-indented `<hex:addr>` line); returns [] for unparseable text
  - CLI: `python3 -m jclient scan --data-dir D --identity I`; `browse <page-hash> --data-dir D --identity I`; `play <game-address> --data-dir D --identity I`; plus the Task 2 `smoke`
  - `play` reads game lines from stdin (one per line; empty line = empty message); per line: send → wait reply → print the **increment** `reply[len(prev):]` (or the whole reply if it doesn't extend `prev` — e.g. a rejection string) → `prev = reply`; flush after each block; exit 0 on stdin EOF. The host's reply is the cumulative transcript (Task 1), so printing the increment is exactly what a chat app prints, and `norm(stdout)` over a full session == the session transcript.

- [ ] **Step 1: Add `parse_page` to `jclient/client.py`** (append at end of file)

```python
def parse_page(text):
    """Parse the render_page format (jhost/protocol.py): a
    '> name (vN)' line followed by a 2-space-indented '<hex:addr>' line.
    Returns [(name, version:int, addr:str)]. Pure; [] if unparseable."""
    import re
    out = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^> (\S+) \(v(\d+)\)\s*$", line)
        if m:
            cur = (m.group(1), int(m.group(2)))
            continue
        m = re.match(r"^\s{2}(<[0-9a-fA-F:]+>)\s*$", line)
        if m and cur is not None:
            out.append((cur[0], cur[1], m.group(1)))
            cur = None
    return out
```

- [ ] **Step 2: Replace `jclient/__main__.py` with the full CLI**

```python
"""python3 -m jclient <scan|browse|play|smoke> (spec §7).

play mirrors Sideband's wire path: own delivery identity (persisted at
--identity — the player's save-slot file), one message per game line,
print the new part of each reply. ^D (EOF) exits; the host's per-turn
autosave already persisted the state."""
import argparse
import json
import sys
from pathlib import Path

import RNS

from .client import Client, parse_page
from jhost.protocol import unpretty  # stdlib-only helper


def cmd_scan(a):
    """Offline page-node classification (spec §7): for each
    known-destination entry with non-empty app_data, recall its identity,
    compute the nomadnetwork.node destination hash, and print the entry
    only if THAT exact hash is also in the known table (membership test —
    no guessing by app name)."""
    c = Client(a.data_dir, a.identity, name="jclient", port=a.port)
    found = 0
    for dh, entry in sorted(RNS.Identity.known_destinations.items()):
        app_data = entry[3] if isinstance(entry, (list, tuple)) and len(entry) > 3 else None
        if not app_data:
            continue
        ident = RNS.Identity.recall(dh)
        if ident is None:
            continue
        try:
            # RNS 1.5: Destination.hash(public_key, app_name, *aspects);
            # if it rejects an identity, pass ident.public_key (verify in venv source).
            page_hash = RNS.Destination.hash(ident, "nomadnetwork", "node")
        except Exception:
            continue
        if page_hash in RNS.Identity.known_destinations:
            print(f"{app_data.decode(errors='replace')}\t{RNS.prettyhash(page_hash)}")
            found += 1
    if not found:
        print("(no page nodes seen — the host must have announced first)",
              file=sys.stderr)
    return 0


def cmd_browse(a):
    """RNS request to /page/index.mu; print the page, then extracted
    'name <addr>' lines (spec §6)."""
    c = Client(a.data_dir, a.identity, name="jclient", port=a.port)
    page_ident = c.recall(unpretty(a.page_hash), "page node")
    page_dest = RNS.Destination(page_ident, RNS.Destination.OUT,
                                RNS.Destination.SINGLE, "nomadnetwork",
                                "node")
    page = c.request(page_dest, "/page/index.mu")
    if page is None:
        print("page fetch failed", file=sys.stderr)
        return 1
    text = page.decode(errors="replace")
    print(text, end="")
    print("== games ==")
    for name, _version, addr in parse_page(text):
        print(f"{name} {addr}")
    return 0


def cmd_play(a):
    c = Client(a.data_dir, a.identity, name="jclient", port=a.port)
    addr = unpretty(a.game_address)
    prev = ""
    for line in sys.stdin:
        line = line.rstrip("\n")
        c.send(addr, line.encode(), "play")
        reply = c.wait_reply({r for r in []} | set())  # see note below
        # every reply is a cumulative transcript: print the increment
        new = reply[len(prev):] if reply.startswith(prev) else reply
        sys.stdout.write(new + "\n")
        sys.stdout.flush()
        prev = reply
    return 0


def cmd_smoke(a):
    c = Client(a.data_dir, Path(a.data_dir) / "identity", name="jclient")
    dests = json.loads(Path(a.host_json).read_text())
    page_ident = c.recall(unpretty(dests["page"]), "page node")
    page_dest = RNS.Destination(page_ident, RNS.Destination.OUT,
                                RNS.Destination.SINGLE, "nomadnetwork",
                                "node")
    page = c.request(page_dest, "/page/index.mu")
    if page is None or b">" not in page:
        print(f"page fetch failed: {page!r}", file=sys.stderr)
        return 1
    print(page.decode(errors="replace"), end="")
    # pick a known-stable game (crashme is deliberately broken Z-code)
    games = dests["games"]
    stem = "planetfall" if "planetfall" in games else list(games)[0]
    c.send(unpretty(games[stem]), b"hello", "smoke")
    print(c.wait_reply(set()), end="")
    print("\nSMOKE-OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="jclient")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("scan", "browse", "play"):
        s = sub.add_parser(n)
        s.add_argument("--data-dir", default=".jclient")
        s.add_argument("--identity", default=".jclient/identity")
        s.add_argument("--port", type=int, default=4242)
    sub.choices["browse"].add_argument("page_hash")
    sub.choices["play"].add_argument("game_address")
    s = sub.add_parser("smoke")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--host-json", required=True)
    a = ap.parse_args()
    {"scan": cmd_scan, "browse": cmd_browse, "play": cmd_play,
     "smoke": cmd_smoke}[a.cmd](a)
```

Fix the `cmd_play` wait: `wait_reply` takes the set of reply strings already seen; in `play` that is just `{prev}` when `prev != ""` (each new reply extends the previous, so it can never equal it). Replace the marked line with:

```python
        reply = c.wait_reply({prev} if prev else set(), timeout=120)
```

**Why `{prev}` is a safe "seen" set:** the host's reply is always the cumulative transcript of that session (Task 1), strictly growing per turn; a rejected input (`[Rejected…]`, `[Game over]`) does not start with `prev` and is a new string. No other message source exists on this delivery identity.

- [ ] **Step 3: Smoke-test the CLI manually (2 minutes, venv)**

```bash
# terminal 1: host
.venv/bin/python -m jhost tests/corpus --data-dir /tmp/jh --name "J-Machine Games"
# terminal 2:
ADDR=$(python3 -c "import json;d=json.load(open('/tmp/jh/host-destinations.json'));print(d['games']['zork1'])")
PAGE=$(python3 -c "import json;d=json.load(open('/tmp/jh/host-destinations.json'));print(d['page'])")
.venv/bin/python -m jclient browse "$PAGE" --data-dir /tmp/jc --identity /tmp/jc/identity
echo "look" | .venv/bin/python -m jclient play "$ADDR" --data-dir /tmp/jc --identity /tmp/jc/identity
```
Expected: browse prints the micron page + `== games ==` section with `zork1 <…>`; play prints the Zork I field intro + the `look` turn. Both commands use the SAME identity file (so `play`'s save slot is stable).

- [ ] **Step 4: Re-run the Task 2 smoke (regression)**

Run: `.venv/bin/python -m unittest tests.network.test_netrig -v`
Expected: 1 PASS

- [ ] **Step 5: Commit**

```bash
git add jclient/
git commit -m "feat: jclient — scan/browse/play (spec §7 test client, Sideband wire path)"
```

---

### Task 5: network tests 1–5 (spec §7)

**Files:**
- Create: `tests/network/test_network.py`

**Interfaces:**
- Consumes: `netrig.{spawn, play_once, play_proc, run_captured, wait_file, logs_tail, DEST_JSON}` (Task 2), `jclient` CLI (Task 4), `jclient.client.parse_page`, `tests.util.{dfrotz_transcript, norm}`, `tests.differential.run_differential.WALK` (the hand-transcribed Zork I walkthrough; its first 10 commands are already gated byte-identical in Phase 1)
- Produces: the Phase 2 done-bar gates (executed by `scripts/run_done.py`, Task 6)

**Test design notes (read before implementing):**

- **Ports:** each test gets its own port (4341–4345) so host/client data dirs never collide and rebinds are clean. `play_once`/`play_proc`/`jclient` take `--port`.
- **The host's `--seed 10`** matches the dfrotz `-s 10` oracle (Phase 1 differential harness convention).
- **Cumulative replies:** the host replies with the session's full transcript each time; the `jclient play` prints only the *increment* (Task 4), so `norm(client stdout)` over a session == the session transcript == `norm(dfrotz -t -s 10)` for the same lines (Phase 1 differential gate proves the engine side).
- **Reconnect (test 3, flagship):** the host process is **also restarted** between the two client phases (same host data dir) — that is the real reconnect: new host, persisted identities → same addresses, session restored from the autosave slot on disk. The new client's first reply is the restored batch (the turn-5 boundary prompt), which overlaps the tail of phase 1's output; `dedup_join` removes that one overlap before comparing (a phone shows the restored prompt twice — history + new message; the game STATE is what must be byte-identical). Phase 1 done-bar gate 3 (save round-trip) already proves the seam is byte-exact, so the overlap is exactly that boundary prompt.
- **Announce timing:** the host announces immediately at startup, then every 300 s; RNS re-announces to newly-connected peers on link establishment. If a test fails with a recall timeout and both logs show no announce reaching the client, verify the link-join re-announce path in the venv source (`site-packages/RNS/Destination.py`, `Link.py`); the deployment-tuning fallback is a fast initial announce burst in `Host.run()` (15 s for the first 2 minutes) — a knob, not a protocol change. Surface it, don't silently change the 300 s interval.

- [ ] **Step 1: Write `tests/network/test_network.py`**

```python
"""Spec §7 tests 1-5: the real protocol over a real transport.

Each test: real host process (python -m jhost, own port + temp data dir)
+ real jclient subprocesses (persisted identity per player). Gates:
  1 page discovery (address parsed out of the page text)
  2 play vs dfrotz: 10 commands byte-identical
  3 reconnect (flagship): 5 turns -> restart host AND client -> 5 more;
    combined transcript byte-identical to the uninterrupted dfrotz run
  4 two players interleave, no cross-talk
  5 in-game save/restore opcodes over the wire; slot file rewritten
"""
import contextlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    import RNS  # noqa: F401
    import LXMF  # noqa: F401
    HAVE_RNS = True
except ImportError:
    HAVE_RNS = False

from tests.conformance.run_conformance import play_session_lines
from tests.differential.run_differential import WALK
from tests.network import netrig
from tests.util import dfrotz_transcript, norm
from jclient.client import parse_page

C = Path(__file__).resolve().parents[1] / "corpus"
SEED = 10


def start_host(data_root, games, port):
    """Spawn jhost on a fresh (or existing) data dir; wait for the
    destinations file. Returns (proc, dests-dict)."""
    data_root = Path(data_root)
    gd = data_root / "games"
    gd.mkdir(parents=True, exist_ok=True)
    for g in games:
        link = gd / g
        if not link.exists():
            link.symlink_to(C / g)
    hdir = data_root / "host"
    h = netrig.spawn([sys.executable, "-u", "-m", "jhost", str(gd),
                      "--data-dir", str(hdir), "--seed", str(SEED),
                      "--port", str(port)], "host", hdir)
    if not netrig.wait_file(hdir / netrig.DEST_JSON, 120):
        h.terminate()
        h.wait(timeout=10)
        raise AssertionError("host did not start:\n" + netrig.logs_tail(hdir))
    return h, json.loads((hdir / netrig.DEST_JSON).read_text())


def stop_host(h):
    h.terminate()
    h.wait(timeout=10)


@contextlib.contextmanager
def host(games, port, data_root):
    h, dests = start_host(data_root, games, port)
    try:
        yield dests
    finally:
        stop_host(h)


def dedup_join(a, b):
    """Join two client outputs, removing the one overlap: the restored
    batch (start of b) re-prints the boundary prompt that ends a."""
    for n in range(min(len(a), len(b)), 0, -1):
        if a[-n:] == b[:n]:
            return a + b[n:]
    return a + b


@unittest.skipUnless(HAVE_RNS, "rns/lxmf not installed (pip install rns lxmf)")
class Network(unittest.TestCase):
    def test_1_page_discovery(self):
        """Spec test 1: the client parses a game address OUT OF THE PAGE
        TEXT and plays using the parsed address — the real discovery
        chain end to end."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with host(["zork1.z3"], 4341, d) as dests:
                work = d / "c1"
                rc, out, _ = netrig.run_captured(
                    [sys.executable, "-u", "-m", "jclient", "browse",
                     dests["page"], "--data-dir", str(work),
                     "--identity", str(work / "identity"),
                     "--port", "4341"], "browse", work, timeout=180)
                self.assertEqual(rc, 0, out + "\n" + netrig.logs_tail(work))
                self.assertIn("== games ==", out)
                games = parse_page(out)
                addr = dict((n, a) for n, _v, a in games)["zork1"]
                self.assertEqual(addr, dests["games"]["zork1"])
                rc2, out2, _ = netrig.play_once(addr, ["look"], work, "p1",
                                                timeout=300, port=4341)
                self.assertEqual(rc2, 0, out2 + "\n" + netrig.logs_tail(work))
                self.assertIn("white house", out2)  # Zork I intro/field

    def test_2_play_10_commands_vs_dfrotz(self):
        """Spec test 2: 10 commands over the network, byte-identical to
        dfrotz -s 10 (whole-stack oracle parity)."""
        lines = WALK[:10]
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with host(["zork1.z3"], 4342, d) as dests:
                rc, out, _ = netrig.play_once(dests["games"]["zork1"], lines,
                                              d / "c2", "p2", timeout=300,
                                              port=4342)
                self.assertEqual(rc, 0, out + "\n" + netrig.logs_tail(d / "c2"))
                ref = dfrotz_transcript(C / "zork1.z3", lines, seed=SEED)
                self.assertEqual(norm(out), norm(ref))

    def test_3_reconnect(self):
        """Spec test 3 (flagship): 5 turns -> restart the HOST (same data
        dir: persisted identities = same addresses, session restored from
        the autosave slot) -> new client process, same identity file ->
        5 more turns. Combined transcript byte-identical to dfrotz with
        all 10 uninterrupted."""
        l1, l2 = WALK[:5], WALK[5:10]
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            h1, dests1 = start_host(d, ["zork1.z3"], 4343)
            try:
                rc1, out1, _ = netrig.play_once(dests1["games"]["zork1"],
                                                l1, d / "c", "p3a",
                                                timeout=300, port=4343)
                self.assertEqual(rc1, 0,
                                 out1 + "\n" + netrig.logs_tail(d / "c"))
            finally:
                stop_host(h1)
            # host down. Same data dir -> same identities -> same address.
            h2, dests2 = start_host(d, ["zork1.z3"], 4343)
            try:
                self.assertEqual(dests1["games"]["zork1"],
                                 dests2["games"]["zork1"],
                                 "address must be stable across host "
                                 "restarts (persisted identities)")
                rc2, out2, _ = netrig.play_once(dests2["games"]["zork1"],
                                                l2, d / "c", "p3b",
                                                timeout=300, port=4343)
                self.assertEqual(rc2, 0,
                                 out2 + "\n" + netrig.logs_tail(d / "c"))
            finally:
                stop_host(h2)
            ref = dfrotz_transcript(C / "zork1.z3", l1 + l2, seed=SEED)
            self.assertEqual(norm(dedup_join(out1, out2)), norm(ref))

    def test_4_two_players(self):
        """Spec test 4: identities A/B interleave turns on one game (live
        pipes, one line from each per round — the host's global lock
        serializes the turns); each player's transcript == dfrotz of that
        player's own lines (same host seed). No cross-talk."""
        a_lines, b_lines = WALK[:5], WALK[5:10]
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with host(["zork1.z3"], 4344, d) as dests:
                addr = dests["games"]["zork1"]
                pa = netrig.play_proc(addr, d / "c4a", "p4a", port=4344)
                pb = netrig.play_proc(addr, d / "c4b", "p4b", port=4344)
                try:
                    for la, lb in zip(a_lines, b_lines):
                        pa.stdin.write((la + "\n").encode())
                        pa.stdin.flush()
                        pb.stdin.write((lb + "\n").encode())
                        pb.stdin.flush()
                        time.sleep(1)  # let each pair of replies settle
                    pa.stdin.close()
                    pb.stdin.close()
                    out_a = pa.stdout.read().decode(errors="replace")
                    out_b = pb.stdout.read().decode(errors="replace")
                    rc_a, rc_b = pa.wait(timeout=120), pb.wait(timeout=120)
                finally:
                    for p in (pa, pb):
                        if p.poll() is None:
                            p.kill()
                tail = netrig.logs_tail(d / "c4a") + netrig.logs_tail(d / "c4b") \
                    + netrig.logs_tail(d / "host")
                self.assertEqual((rc_a, rc_b), (0, 0), tail)
                ref_a = dfrotz_transcript(C / "zork1.z3", a_lines, seed=SEED)
                ref_b = dfrotz_transcript(C / "zork1.z3", b_lines, seed=SEED)
                self.assertEqual(norm(out_a), norm(ref_a))
                self.assertEqual(norm(out_b), norm(ref_b))

    def test_5_ingame_save_restore(self):
        """Spec test 5: risorg's in-game SAVE/RESTORE verbs over the wire;
        the host-local slot file (keyed by the player's identity hash) is
        written by the in-game save and rewritten by the autosave after
        the restore turn. Two client phases (same identity): phase 1
        '' (risorg startup line) + save + look; phase 2 restore + look."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with host(["risorg.z8"], 4345, d) as dests:
                addr = dests["games"]["risorg"]
                work = d / "c5"
                rc1, out1, _ = netrig.play_once(addr, ["", "save", "look"],
                                                work, "p5a", timeout=300,
                                                port=4345)
                self.assertEqual(rc1, 0,
                                 out1 + "\n" + netrig.logs_tail(work))
                self.assertEqual(out1.count("Ok."), 1)  # the save verb
                slot = next(iter((d / "host" / "saves" / "risorg")
                                 .glob("*.zmsv")), None)
                self.assertIsNotNone(slot, "in-game save must write the "
                                           "host-local slot")
                st1 = slot.stat()
                rc2, out2, _ = netrig.play_once(addr, ["restore", "look"],
                                                work, "p5b", timeout=300,
                                                port=4345)
                self.assertEqual(rc2, 0,
                                 out2 + "\n" + netrig.logs_tail(work))
                self.assertEqual(out2.count("Ok."), 1)  # the restore verb
                st2 = slot.stat()
                self.assertGreater(st2.st_mtime_ns, st1.st_mtime_ns,
                                   "slot file must be rewritten by the "
                                   "post-restore autosave")
                # the full two-phase wire transcript (cumulative replies)
                # must equal the Session-level reference with identical
                # handler semantics (in-memory slot, as the host's is a file)
                def handlers(s):
                    store = {}
                    def save(hint):
                        store["img"] = s.save()
                        return True
                    def restore(hint):
                        s.restore_image(store["img"])
                        return True
                    return save, restore
                ref = play_session_lines(C / "risorg.z8",
                                         ["", "save", "look", "restore",
                                          "look"],
                                         seed=SEED, handlers=handlers)
                self.assertEqual(norm(out2), norm(ref))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests 1–3**

Run: `.venv/bin/python -m unittest tests.network.test_network.Network.test_1_page_discovery tests.network.test_network.Network.test_2_play_10_commands_vs_dfrotz tests.network.test_network.Network.test_3_reconnect -v`
Expected: 3 PASS (a few minutes — each test spawns a host + 1–2 client RNS instances).

**If a test fails:** the assertion carries client stdout + tails of every `proc-*.log`/`rns.log` in the data dir. Diagnose from the logs (host `proc-host.log` has the `serving N game(s)` line + per-message errors; client `proc-p*.log` has the recall/page steps). Fix the wiring, not the assertion — the assertions are the spec's behavior table.

- [ ] **Step 3: Run tests 4–5**

Run: `.venv/bin/python -m unittest tests.network.test_network.Network.test_4_two_players tests.network.test_network.Network.test_5_ingame_save_restore -v`
Expected: 2 PASS

- [ ] **Step 4: Run the whole network suite + the unit suite**

Run: `.venv/bin/python -m unittest discover -s tests -v` (with the venv interpreter so the network tests run)
Expected: all green — Phase 1 + protocol units + smoke + tests 1–5.
Then: `python3 -m unittest discover -s tests` (system interpreter)
Expected: all green, network tests SKIPPED (the stdlib suite stays green anywhere).

- [ ] **Step 5: Commit**

```bash
git add tests/network/test_network.py
git commit -m "test: network suite 1-5 — discovery, 10-cmd dfrotz parity, reconnect, two players, in-game save/restore"
```

---

### Task 6: done bar + README (spec §7 done bar, §6 operator flow)

**Files:**
- Modify: `scripts/run_done.py` (Phase 2 gate block)
- Modify: `README.md` (operator/deployment section)

- [ ] **Step 1: Extend `scripts/run_done.py`** — add after `gate_save_roundtrip`:

```python
def gate_network():
    """Phase 2 gates (spec §7 done bar 2-5): the RNS rig suite (smoke +
    tests 1-5) under the venv interpreter. Skips cleanly when .venv or
    rns/lxmf are absent (the stdlib suite stays green anywhere)."""
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        return True, "skipped (no .venv)"
    probe = subprocess.run([str(py), "-c", "import RNS, LXMF"],
                           capture_output=True, cwd=ROOT)
    if probe.returncode != 0:
        return True, "skipped (rns/lxmf not in .venv)"
    return _run([str(py), "-m", "unittest", "discover", "-s", "tests",
                 "-p", "test_net*.py"])
```

and in `main()`, insert the gate and renumber the manual reminder:

```python
    gates = [
        ("1. Conformance", gate_conformance),
        ("2. Differential vs dfrotz", gate_differential),
        ("3. Save round-trip", gate_save_roundtrip),
        ("4. Fake transport", gate_faketx),
        ("5. Network: RNS rig (Phase 2)", gate_network),
        ("all unit tests", gate_unit_tests),
    ]
```

and replace the manual line with:

```python
    print("? 6. Smoke (manual)            Phase 1: play the corpus games in a "
          "real terminal; Phase 2: Sideband phone on the testnet — page "
          "visible, play a few turns, reconnect restores")
```

Note: `unittest discover -s tests -p "test_net*.py"` picks up `tests/network/test_netrig.py` + `tests/network/test_network.py` only (discover scans subdirectories too).

- [ ] **Step 2: Append the README section**

```markdown
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
```

The client identity file IS the player's save slot; the host autosaves
after every turn to `data/saves/<game>/<player-hash>.zmsv`. A player who
disappears for days reconnects to exactly where they left off.
In-game `save`/`restore` verbs map to that slot with no prompt.

### Verify

`python3 scripts/run_done.py` — Phase 1 gates + the Phase 2 network gate
(RNS rig: page discovery, 10-command dfrotz byte-parity over the wire,
reconnect byte-identity, two players, in-game save/restore). Network
gates skip when `rns`/`lxmf` are not installed.
```

- [ ] **Step 3: Final verification**

Run: `python3 scripts/run_done.py`
Expected: every automated gate ✓ (the network gate runs the rig suite under `.venv`; takes several minutes). The `? 6` manual line is the reminder — the Sideband-on-testnet step is human work, done when the VPS transport is configured.

- [ ] **Step 4: Commit**

```bash
git add scripts/run_done.py README.md
git commit -m "docs: Phase 2 done-bar gate + operator/deployment README"
```

---

## Spec coverage (self-review)

| Spec § | Where |
|---|---|
| §3 architecture (process/RNS/page/per-game routers/lock/addresses/startup) | Tasks 3 (host), 1 (config scaffold), 5-test3 (address stability) |
| §4 game protocol (first contact, caps, done, reply, in-game opcodes, no dedup) | Task 1 (protocol + unit tests 6–7), Task 3 (wiring), Task 5-test5 |
| §5 state and saves (slots, atomic, reconnect, @save/@restore mapping) | Task 1 (FileSaveStore, handlers), Task 5-test3 (reconnect flagship) |
| §6 network/deployment (config-only transport, operator flow, micron page format) | Task 1 (render_page/write_rns_config), Task 3 (host), Task 6 (README) |
| §7 testing/done bar (rig, tests 0–7, skip behavior, gates) | Tasks 2 (test 0 + rig), 1 (tests 6–7), 5 (tests 1–5), 6 (done bar) |
| §8 layout | Tasks 1–5 file list (matches §8 exactly) |
| §9 decisions (pins, global lock, stamps 0, no dedup, test client built) | Global Constraints + ponytail comments in Task 1/3 code |

**Placeholder scan:** none — every step carries runnable code/commands. **Type consistency:** `handle_message`/`GameState`/`SaveStore` signatures used identically in Tasks 1/3/5; `netrig` helper names used identically in Tasks 2/5; `Client`/`parse_page`/`unpretty` consistent across Tasks 2/4/5. One deliberate runtime check flagged in-plan: `RNS.Destination.hash(identity, ...)` vs `ident.public_key` (Task 4 `scan` — verify against venv source; the verified-fact cites the pattern, the exact first-argument type is confirmed at runtime).
