"""External probe: one round trip against a *running* host, from a separate
process, recorded where the page can read it.

Why this is not a timer inside jhost: the question is whether a client outside
this process can send to the host and get its answer back. A check that runs
inside the thing it checks can only restate what that thing already believes.
The canary has the same shape and a different job — it spawns its own throwaway
host on a spare port, so it catches code rot and says nothing about the live
process. This one touches the live process and says nothing about the code on
disk. Both are worth running; neither substitutes for the other.

The record it leaves, `data/probe.json`, is what lets a failed check turn the
page's status pill red. Green needs no file: when a round trip succeeds the
host is told directly, by the ack. Without this file the pill would keep
showing its last success indefinitely, which is the stale green light this
whole exercise exists to avoid.

Reaching the host is the probe's own business: `jclient` scaffolds a loopback
TCP client interface under its data dir, which is the right default on the same
machine. To make the check cross the mesh instead — out to a hub and back, like
a player's traffic does — write `data/probe/rns/config` yourself. It is not
overwritten (see write_rns_config), so an operator-authored config wins and the
probe will use it.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .protocol import DEST_JSON

PROBE_JSON = "probe.json"
# The round trip is LXMF in both directions, so it is paced by the mesh rather
# than by the network: the client waits this long for the answer, and the
# subprocess gets margin over it before being killed. A generous ceiling here
# is not a latency judgement — see the note on conditions, not timelines.
# The whole check gets this: finding a route to the host and the answer coming
# back share it, and the client returns the moment the answer lands. Ten minutes
# because a client that has not learned the route yet is not a dead node — RNS
# learns routes on its own schedule, which can be immediate or can be most of
# that window. A check shorter than this can only report "hadn't happened yet".
DEFAULT_BUDGET = 600
# margin over the client's own budget: the ack goes out after the answer, so the
# subprocess needs room for it plus interpreter startup
ACK_MARGIN = 360


def pick_game(dests, game=None):
    """Which game destination to address.

    Deterministic by default (first in sorted order, the same pick every run)
    rather than random: a check whose outcome depends on which story happened to
    be drawn is not a check."""
    games = dests["games"]
    stem = game if game else sorted(games)[0]
    if stem not in games:
        raise KeyError(f"no game {stem!r} (have: {', '.join(sorted(games))})")
    return stem, games[stem]


def read_probe(data_dir):
    """The last recorded check, or None if there is none. Never raises: this is
    read while rendering a page, and a half-written file is not a reason for the
    activity page to fail."""
    try:
        return json.loads((Path(data_dir) / PROBE_JSON).read_text())
    except (OSError, ValueError):
        return None


def write_probe(data_dir, result):
    path = Path(data_dir) / PROBE_JSON
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=1))
    os.replace(tmp, path)      # same dir, so the swap is atomic
    return result


def run(data_dir, game=None, budget=DEFAULT_BUDGET, port=4242, timeout=None):
    """Send one ping to the live host and record what came back. Returns the
    record it wrote; raises nothing the record cannot hold."""
    data_dir = Path(data_dir)
    dests = json.loads((data_dir / DEST_JSON).read_text())
    stem, addr = pick_game(dests, game)
    work = data_dir / "probe"
    work.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "jclient", "ping", addr, "--budget",
           str(budget), "--port", str(port), "--data-dir", str(work),
           "--identity", str(work / "identity")]
    t0 = time.time()
    out = ""            # the failure paths below have no stdout to parse
    try:
        p = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, text=True,
                           timeout=timeout or budget + ACK_MARGIN)
        out = (p.stdout or "").strip()
        ok = p.returncode == 0 and "ping OK" in out
        detail = out.splitlines()[-1] if out else (
            (p.stderr or "").strip().splitlines() or [f"exit {p.returncode}"])[-1]
    except subprocess.TimeoutExpired:
        ok, detail = False, f"killed after {timeout or budget + ACK_MARGIN}s"
    except Exception as e:                # unreadable destinations file, no python
        ok, detail = False, f"{type(e).__name__}: {e}"
    # ack is kept apart from ok on purpose. ok answers "did the host reply to a
    # ping"; ack says whether the receipt of that reply got back to it. A missing
    # ack leaves the node NOT VERIFIED rather than DOWN, and this is the field
    # that explains why it never turned green.
    return write_probe(data_dir, {
        "t": round(time.time(), 3),
        "game": stem,
        "ok": ok,
        "ack": next((w[4:] for w in out.split() if w.startswith("ack=")), None),
        "rtt": round(time.time() - t0, 1),
        "detail": detail[:300],
    })


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jhost.probe")
    ap.add_argument("--data-dir", default="data",
                    help="the host's data dir: where host-destinations.json "
                         "and probe.json live")
    ap.add_argument("--game", default=None,
                    help="which game destination to address (default: the "
                         "first in sorted order, so the check is the same one "
                         "every time)")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="seconds for the whole check (route + answer)")
    ap.add_argument("--port", type=int, default=4242)
    a = ap.parse_args(argv)
    r = run(a.data_dir, game=a.game, budget=a.budget, port=a.port)
    print(f"probe {'OK' if r['ok'] else 'FAIL'} game={r['game']} "
          f"rtt={r['rtt']}s {r['detail']}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
