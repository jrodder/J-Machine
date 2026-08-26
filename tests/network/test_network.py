"""Spec §7 tests 1-5: the real protocol over a real transport.

Each test: real host process (python -m jhost, own port + temp data dir)
+ real jclient subprocesses (persisted identity per player). Gates:
  1 page discovery (address parsed out of the page text)
  2 play vs dfrotz: 10 commands byte-identical
  3 reconnect (flagship): 5 turns -> restart host AND client -> 5 more;
    combined transcript byte-identical to the uninterrupted dfrotz run
  4 two players interleave, no cross-talk
  5 in-game save/restore opcodes over the wire; slot file rewritten
  6 games/<stem>.pdf manual: micron link on the page, fetched bytes
    identical over the wire
"""
import contextlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    import RNS  # noqa: F401
    import LXMF  # noqa: F401
    from jclient.client import parse_page  # noqa: F401  (RNS at module level)
    HAVE_RNS = True
except ImportError:
    HAVE_RNS = False

from tests.conformance.run_conformance import play_session_lines
from tests.differential.run_differential import WALK
from tests.network import netrig
from tests.util import dfrotz_transcript, norm

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
                rc2, out2 = netrig.play_once(addr, ["look"], work, "p1",
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
                rc, out = netrig.play_once(dests["games"]["zork1"], lines,
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
                rc1, out1 = netrig.play_once(dests1["games"]["zork1"],
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
                rc2, out2 = netrig.play_once(dests2["games"]["zork1"],
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
        '' (transcript-only) + 'more' (absorbs seed-10's ***MORE*** intro
        pause — the protocol never feeds empty lines) + save + look; phase
        2 restore + look. Replies are per-turn deltas (spec §4), so each
        phase's output is that phase's turns only — out2 contains its own
        single "Ok." and out1+out2 is the full two-phase transcript."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with host(["risorg.z8"], 4345, d) as dests:
                addr = dests["games"]["risorg"]
                work = d / "c5"
                rc1, out1 = netrig.play_once(addr,
                                             ["", "more", "save", "look"],
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
                rc2, out2 = netrig.play_once(addr, ["restore", "look"],
                                             work, "p5b", timeout=300,
                                             port=4345)
                self.assertEqual(rc2, 0,
                                 out2 + "\n" + netrig.logs_tail(work))
                # delta reply: phase 2 shows only its own restore "Ok."
                self.assertEqual(out2.count("Ok."), 1)
                st2 = slot.stat()
                self.assertGreater(st2.st_mtime_ns, st1.st_mtime_ns,
                                   "slot file must be rewritten by the "
                                   "post-restore autosave")
                # the two phases' delta outputs joined are the full
                # two-phase wire transcript
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
                # reference: play_session_lines FEEDS "" (absorbed at the
                # ***MORE*** pause — same as the wire's "more" line), so the
                # line sequences differ by one absorbed line and the
                # transcripts are identical
                ref = play_session_lines(C / "risorg.z8",
                                         ["", "save", "look", "restore",
                                          "look"],
                                         seed=SEED, handlers=handlers)
                self.assertEqual(norm(out1 + out2), norm(ref))

    def test_6_manual_download(self):
        """games/<stem>.pdf -> the page's game block carries a micron
        `[manual`<hash>:/file/<stem>.pdf]` link (NomadNet Guide URL
        format); fetching the linked /file path over the wire (page
        destination) returns the file's exact bytes (spec §6)."""
        pdf = b"%PDF-1.4\n" + os.urandom(256 * 1024)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "games").mkdir()
            (d / "games" / "planetfall.pdf").write_bytes(pdf)
            h, dests = start_host(d, ["planetfall.z5"], 4346)
            try:
                work = d / "c6"
                rc, out, _ = netrig.run_captured(
                    [sys.executable, "-u", "-m", "jclient", "browse",
                     dests["page"], "--data-dir", str(work),
                     "--identity", str(work / "identity"),
                     "--port", "4346"], "browse", work, timeout=180)
                self.assertEqual(rc, 0, out + "\n" + netrig.logs_tail(work))
                m = re.search(r"`\[manual`([0-9a-fA-F]{32}):"
                              r"/file/planetfall\.pdf\]", out)
                self.assertIsNotNone(m, out)
                self.assertEqual(m.group(1),
                                 dests["page"].strip("<>").replace(":", ""))
                outp = work / "planetfall.pdf"
                rc2, out2, _ = netrig.run_captured(
                    [sys.executable, "-u", "-m", "jclient", "fetch",
                     dests["page"], "/file/planetfall.pdf",
                     "--out", str(outp),
                     "--data-dir", str(work),
                     "--identity", str(work / "identity"),
                     "--port", "4346"], "fetch", work, timeout=600)
                self.assertEqual(rc2, 0,
                                 out2 + "\n" + netrig.logs_tail(work))
                self.assertEqual(outp.read_bytes(), pdf)
            finally:
                stop_host(h)


@unittest.skipUnless(HAVE_RNS, "rns/lxmf not installed (pip install rns lxmf)")
class AnnounceInterval(unittest.TestCase):
    """spec §3: the re-announce cadence is a host setting (operator
    knobs live in the RNS config; this one is a launch flag because the
    cadence is host-process policy). Default 60 min (RNS announce rate
    target is 3600 s; faster cadences get rate-limited off the network).
    Host.__init__ has no RNS side effects -> instant."""

    def test_default_60_minutes(self):
        from jhost.host import Host
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Host(d, d).announce_interval, 60 * 60)

    def test_custom(self):
        from jhost.host import Host
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Host(d, d, announce_interval=7 * 60)
                             .announce_interval, 420)


if __name__ == "__main__":
    unittest.main()
