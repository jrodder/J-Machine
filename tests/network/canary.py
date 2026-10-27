"""End-to-end canary: prove the whole player path on one machine, offline.

A real `python -m jhost` process (loopback-only RNS config, fresh temp data
dir) plus the real `python -m jclient` CLI, driven the way a player is
driven: fetch the NomadNet page over the RNS request path, then send game
lines one at a time and wait for each reply. Success is exactly one line on
stdout and exit 0; any failure is one `canary FAIL` line naming the stage
that broke. Either way everything this module started is torn down.

    python -m tests.network.canary          # or: scripts/canary.sh

CANARY_KEEP=1 keeps the temp dir (host/client logs, saves, RNS configs) so a
failure can be read afterwards; it is printed in the FAIL line.
"""
import ctypes
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.network import netrig

GAME = "zork1"
# Real Zork I path (the mailbox is in West of House, so it comes before the
# move north). Every turn must produce text.
TURNS = ["look", "open mailbox", "take leaflet", "north", "south"]

# Every wait is bounded and every bound has a FAIL. The deadlines are
# generous on purpose: this stack is delay-tolerant even over loopback, so
# hitting one means something is actually broken, not merely slow.
HOST_TIMEOUT = 120      # host up + host-destinations.json written
PAGE_TIMEOUT = 180      # page fetch: link handshake + response
PING_TIMEOUT = 420        # ping: budget + ack, generous because the
                        # rig is loopback and should never need the budget
TURN_TIMEOUT = 120      # one game line -> its reply
EXIT_TIMEOUT = 60       # client exit after stdin close
KILL_TIMEOUT = 10       # teardown escalation TERM -> KILL

PR_SET_PDEATHSIG = 1


# ------------------------------------------------------------------ process
def _pdeathsig():
    """Die with the canary (preexec): a SIGKILLed canary must not leave a
    host serving on a loopback port forever. Never raises."""
    try:
        ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:
        pass


def spawn(argv, name, out_dir):
    """netrig.spawn, plus a private session and PDEATHSIG so that nothing
    this module starts can outlive it (even if the canary is SIGKILLed)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"proc-{name}.log", "wb") as fh:
        return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True, preexec_fn=_pdeathsig)


def kill(proc):
    """TERM the process group, then KILL it. Never raises, never hangs."""
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except OSError:
            try:
                proc.kill()
            except OSError:
                return
        try:
            proc.wait(timeout=KILL_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            continue


def free_port():
    """An OS-picked loopback port, so the canary never collides with a real
    host or the network suite already listening on the default port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Fail(Exception):
    """A stage's failure, pre-carried with what the summary line needs."""

    def __init__(self, name, turns, detail):
        super().__init__(f"stage={name} turns={turns} {detail}")
        self.name = name
        self.turns = turns
        self.detail = " ".join(str(detail).split())[:240] or "?"


@contextmanager
def stage(name, done):
    """Label whatever fails inside with the stage it failed in, so the FAIL
    line says where the player path broke, not just that it broke."""
    try:
        yield
    except Exception as e:
        raise Fail(name, f"{done['turns']}/{len(TURNS)}",
                   str(e) or type(e).__name__) from e


# --------------------------------------------------------------------- host
def make_games_dir(dst):
    """Temp games dir holding just the one story (symlink; copy if that
    fails) — the host serves every *.z[3458] it finds."""
    story = next((p for p in (ROOT / "games" / f"{GAME}.z3",
                              ROOT / "tests" / "corpus" / f"{GAME}.z3")
                  if p.exists()), None)
    if story is None:
        raise RuntimeError(f"no {GAME}.z3 under games/ or tests/corpus/")
    dst.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(story, dst / story.name)
    except OSError:
        shutil.copy2(story, dst / story.name)
    return dst


def start_host(games, data_dir, port):
    """`python -m jhost` on a fresh data dir. Waits for
    data/host-destinations.json — the file the host writes when it is
    serving — instead of sleeping a fixed guess."""
    host = spawn([sys.executable, "-u", "-m", "jhost", str(games),
                  "--data-dir", str(data_dir), "--seed", "10",
                  "--port", str(port)], "host", data_dir)
    dest_file = Path(data_dir) / netrig.DEST_JSON
    if not netrig.wait_file(dest_file, HOST_TIMEOUT):
        raise RuntimeError(f"{netrig.DEST_JSON} absent after {HOST_TIMEOUT}s "
                           f"(host alive={host.poll() is None}); host log: "
                           f"{netrig.logs_tail(data_dir, 15)[-300:]}")
    dests = json.loads(dest_file.read_text())
    if GAME not in dests.get("games", {}):
        raise RuntimeError(f"{GAME} not served "
                           f"(served={sorted(dests['games'])})")
    return host, dests


# --------------------------------------------------------------------- page
def fetch_page(page_addr, work, port, path="/page/index.mu"):
    """`jclient browse` is an RNS request for a path on the page destination —
    the path a real NomadNet visitor takes. Returns stdout."""
    rc, out, _ = netrig.run_captured(
        [sys.executable, "-u", "-m", "jclient", "browse", page_addr,
         "--path", path,
         "--data-dir", str(work), "--identity", str(work / "identity"),
         "--port", str(port)], "browse", work, timeout=PAGE_TIMEOUT)
    if rc != 0:
        raise RuntimeError(f"browse exit {rc}: {out.strip()[-300:]}")
    return out


def parsed_games(page_text):
    """The client echoes the page, then a `== games ==` block of
    `name <address>` lines built by its own page parser. Playing through
    those addresses proves the discovery chain, not just that bytes arrived."""
    tail = page_text.split("== games ==", 1)
    if len(tail) < 2:
        raise RuntimeError("no == games == block in the browse output")
    out = {}
    for line in tail[1].splitlines():
        name, _, addr = line.strip().rpartition(" ")
        if name and addr.startswith("<"):
            out[name] = addr
    if not out:
        raise RuntimeError("page parsed as zero games")
    return out


def check_stats(stats_text, page_addr):
    """The stats page has to arrive over the request path like any other page,
    and arrive clean. The privacy rule says a player's destination hash never
    appears on it; a unit test can only prove that about bytes this process
    assembled, so over the wire is the half worth having.

    The page's own address is allowed and expected -- the back link needs it, and
    it is public by definition -- in either the delimited or the bare hex form
    the host uses."""
    if "activity" not in stats_text.lower():
        raise RuntimeError("stats page has no activity heading")
    if not re.search(r"\d+ moves", stats_text):
        # the turns this canary just played should be visible: the ledger is the
        # only place a turn carries a timestamp, so a page with no move count
        # means the rollup is not reading what those turns wrote
        raise RuntimeError("stats page shows no moves after played turns")
    own = page_addr.strip("<>")
    stripped = stats_text.replace(page_addr, "").replace(own, "")
    leaked = re.search(r"[0-9a-f]{32}", stripped)
    if leaked:
        raise RuntimeError(f"stats page carries a 32-hex address: "
                           f"{leaked.group(0)}")


def ping_roundtrip(game_addr, work, port):
    """`jclient ping` against the live rig host: a real LXMF turn-shaped round
    trip from a separate process, plus the ack that tells the host the answer
    arrived. Returns the command's own summary line."""
    rc, out, _ = netrig.run_captured(
        [sys.executable, "-u", "-m", "jclient", "ping", game_addr,
         "--budget", "240", "--ack-wait", "120", "--data-dir", str(work),
         "--identity", str(work / "identity"), "--port", str(port)],
        "ping", work, timeout=PING_TIMEOUT)
    if rc != 0:
        raise RuntimeError(f"ping exit {rc}: {out.strip()[-300:]}")
    if "ping OK" not in out:
        raise RuntimeError(f"ping did not report success: {out.strip()[-200:]}")
    # The ack has to be reported DELIVERED, not merely sent: it is the last thing
    # the client does, so a client that exits without watching the transfer loses
    # it. Loopback is fast enough to hide that -- it did once -- which is why the
    # canary checks the receipt rather than only the reply.
    if "ack=delivered" not in out:
        raise RuntimeError(f"ack was not delivered: {out.strip()[-200:]}")
    return out.strip().splitlines()[-1]


def fetch_default_out(page_addr, work, port, path):
    """`jclient fetch` with no --out, run with `work` as the working directory.

    Separate from fetch_page on purpose: browse echoes to stdout, fetch writes a
    file, and the bug this stage exists for was in choosing that file's name.
    Omitting --out produced a str where a Path was needed, so every fetch died
    with AttributeError after the response had arrived complete — a client
    crash that looks identical to a transport failure from the outside. The
    file on disk is the assertion."""
    work.mkdir(parents=True, exist_ok=True)
    # cwd is the temp dir so the default-named file lands where it can be
    # checked, which puts the repo off sys.path -- restore it via PYTHONPATH
    repo = Path(__file__).resolve().parents[2]
    p = subprocess.run(
        [sys.executable, "-u", "-m", "jclient", "fetch", page_addr, path,
         "--data-dir", str(work), "--identity", str(work / "identity"),
         "--port", str(port)], cwd=str(work), capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(repo)), timeout=PAGE_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError(f"fetch exit {p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()[-300:]}")
    landed = work / Path(path).name
    if not landed.exists() or landed.stat().st_size < 40:
        raise RuntimeError(f"fetch reported success but wrote no {landed.name}")
    return landed.read_bytes()


def check_ledger(data_dir, played):
    """The record is part of the player path: if five turns went in and the
    ledger does not show five turns ended, the thing that is supposed to tell
    us a player was served is not telling us anything. Only our own part is
    required (handed_off and later) — whether the mesh delivered is a separate,
    slower answer and is not this probe's business."""
    path = Path(data_dir) / "ledger.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
    except OSError as e:
        raise RuntimeError(f"no ledger at {path} ({e})") from None
    opened = [r["s"] for r in rows if r.get("state") == "open"]
    ended = {r["s"] for r in rows if r.get("state") != "open"}
    unresolved = [s for s in opened if s not in ended]
    ours = [r for r in rows if r.get("state") in ("handed_off", "delivered",
                                                  "propagated")]
    if len(opened) < played:
        raise RuntimeError(f"{len(opened)} turns recorded, {played} played")
    if unresolved:
        raise RuntimeError(f"{len(unresolved)} turns never closed: {unresolved}")
    if len(ours) < played:
        raise RuntimeError(f"only {len(ours)} of {played} turns reached an "
                           f"outcome")


# --------------------------------------------------------------------- play
def _pump(pipe, state, cond, log):
    """Copy everything the client prints into state["buf"] (and the log, for
    post-mortem) and wake anyone waiting on it."""
    while True:
        try:
            chunk = pipe.read1(65536)
        except (OSError, ValueError):
            chunk = b""
        if not chunk:
            break
        log.write(chunk)
        with cond:
            state["buf"] += chunk
            cond.notify_all()
    log.flush()
    with cond:
        state["closed"] = True
        cond.notify_all()


def play_turns(game_addr, work, port, done, host):
    """One live `jclient play` session: write a line, wait for the bytes that
    its reply adds, time it, require text. Returns per-turn seconds; the
    first line with no reply or an empty reply fails the stage."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [sys.executable, "-u", "-m", "jclient", "play", game_addr,
         "--identity", str(work / "identity"), "--data-dir", str(work),
         "--port", str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
        preexec_fn=_pdeathsig)
    log = open(work / "proc-play.log", "wb")
    state, cond = {"buf": bytearray(), "closed": False}, threading.Condition()
    threading.Thread(target=_pump, args=(p.stdout, state, cond, log),
                     daemon=True).start()
    times = []
    try:
        for i, line in enumerate(TURNS, 1):
            with cond:
                before = len(state["buf"])
            t0 = time.time()
            try:
                p.stdin.write((line + "\n").encode())
                p.stdin.flush()
            except OSError as e:
                raise RuntimeError(f"turn {i} ({line!r}): client died on write "
                                   f"({e}, rc={p.poll()}); see play log") from None
            with cond:
                while True:
                    if len(state["buf"]) > before:
                        break
                    if state["closed"] or host.poll() is not None:
                        who = "client exited" if state["closed"] else "host died"
                        raise RuntimeError(f"turn {i} ({line!r}): {who} "
                                           f"(rc={p.poll()}); see play log")
                    if time.time() - t0 > TURN_TIMEOUT:
                        raise RuntimeError(f"turn {i} ({line!r}): no reply "
                                           f"within {TURN_TIMEOUT}s")
                    cond.wait(0.5)
                reply = bytes(state["buf"][before:])
            if not reply.strip():
                raise RuntimeError(f"turn {i} ({line!r}): blank reply")
            times.append(time.time() - t0)
            done["turns"] = i
        p.stdin.close()
        try:
            rc = p.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"client still running {EXIT_TIMEOUT}s after "
                               f"stdin close") from None
        if rc != 0:
            raise RuntimeError(f"play exit {rc}; see play log")
        return times
    finally:
        kill(p)
        log.close()


# -------------------------------------------------------------------- probe
def probe(work, done):
    """host -> page -> turns -> ledger -> stats. Per-turn timings."""""
    port = free_port()
    host, data_dir = None, work / "host"
    t0 = time.time()
    try:
        with stage("host", done):
            host, dests = start_host(make_games_dir(work / "games"),
                                     data_dir, port)
        with stage("page", done):
            page = fetch_page(dests["page"], work / "client", port)
            games = parsed_games(page)
            if GAME not in page:
                raise RuntimeError(f"page text does not mention {GAME}")
            if GAME not in games:
                raise RuntimeError(f"{GAME} not listed (listed={sorted(games)})")
            if games[GAME] != dests["games"][GAME]:
                raise RuntimeError(f"parsed address {games[GAME]} != served "
                                   f"{dests['games'][GAME]}")
        with stage("turns", done):
            times = play_turns(games[GAME], work / "player", port, done, host)
        with stage("ledger", done):
            check_ledger(data_dir, len(times))
        # after the turns, because the page reports activity: fetched before
        # anyone played it would pass on an empty page
        with stage("stats", done):
            stats = fetch_page(dests["page"], work / "client2", port,
                               path="/page/stats.mu")
            check_stats(stats, dests["page"])
        # The orb is amber until someone checks, and green after a probe in
        # another process attests that the answer arrived. The state is a colour,
        # so the assertions are on the hue rather than on a word. That is the
        # whole service-status claim, observed over the wire rather than asserted
        # about a rendered string.
        from jhost.protocol import ORB_DOWN, ORB_OK, ORB_UNCHECKED
        if ORB_UNCHECKED["body"] not in stats:
            raise RuntimeError("stats page did not start out unverified")
        if ORB_OK["body"] in stats or ORB_DOWN["body"] in stats:
            raise RuntimeError("stats page did not start out unverified")
        with stage("ping", done):
            ping_roundtrip(games[GAME], work / "probe", port)
            verified = fetch_page(dests["page"], work / "client4", port,
                                  path="/page/stats.mu")
            if ORB_OK["body"] not in verified:
                raise RuntimeError("stats page still not green after an "
                                   "attested round trip")
            if ORB_UNCHECKED["body"] in verified:
                raise RuntimeError("stats page still amber after an attested "
                                   "round trip")
        # fetch, not browse: a file response, and the default output path
        with stage("fetch", done):
            blob = fetch_default_out(dests["page"], work / "client3", port,
                                    "/page/stats.mu")
            if b"activity" not in blob:
                raise RuntimeError("fetched stats page has no activity heading")
        print(f"canary: {len(times)} turns in {time.time() - t0:.1f}s ("
              + ", ".join(f"{t:.1f}s" for t in times) + ")", file=sys.stderr)
        return times
    finally:
        kill(host)


def main():
    work = Path(tempfile.mkdtemp(prefix="jmachine-canary-"))
    done, t0 = {"turns": 0}, time.time()
    try:
        try:
            times = probe(work, done)
            print(f"canary OK page=1 stats=1 ping=1 fetch=1 turns={len(times)}/{len(TURNS)} "
                  f"slowest={max(times):.1f}s total={time.time() - t0:.0f}s")
            return 0
        except Exception as e:
            f = (e if isinstance(e, Fail) else
                 Fail("harness", f"{done['turns']}/{len(TURNS)}",
                      f"{type(e).__name__}: {e}"))
            keep = os.environ.get("CANARY_KEEP") == "1"
            print(f"canary FAIL stage={f.name} turns={f.turns} "
                  f"detail={f.detail} "
                  f"logs={work if keep else '-'} total={time.time() - t0:.0f}s",
                  flush=True)
            return 1
    finally:
        if os.environ.get("CANARY_KEEP") != "1":
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
