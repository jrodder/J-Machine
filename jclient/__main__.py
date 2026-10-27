"""python3 -m jclient <scan|browse|play|smoke> (spec §7).

play mirrors Sideband's wire path: own delivery identity (persisted at
--identity — the player's save-slot file), one message per game line,
print each reply verbatim. Replies are per-turn deltas (spec §4), so the
client's stdout is the session transcript. ^D (EOF) exits; the host's
per-turn autosave already persisted the state."""
import argparse
import json
import sys
import time
from pathlib import Path

import RNS

from .client import Client, parse_page
from jhost.protocol import PING, PING_ACK, unpretty  # stdlib-only helpers


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
            # RNS 1.5.0: Destination.hash(identity, app_name, *aspects)
            # accepts an RNS.Identity directly (Destination.py:116-130 —
            # verified against installed source).
            page_hash = RNS.Destination.hash(ident, "nomadnetwork", "node")
        except Exception:
            continue
        if page_hash in RNS.Identity.known_destinations:
            print(f"{app_data.decode(errors='replace')}\t{RNS.prettyhexrep(page_hash)}")
            found += 1
    if not found:
        print("(no page nodes seen — the host must have announced first)",
              file=sys.stderr)
    return 0


def cmd_browse(a):
    """RNS request to a page path (default /page/index.mu; --path
    /page/guestbook.mu for the board); print the page, then extracted
    'name <addr>' lines (spec §6). --field NAME=VALUE turns it into a micron
    form submit: the keys go out prefixed field_<NAME>, exactly as NomadNet
    and meshchat send them."""
    c = Client(a.data_dir, a.identity, name="jclient", port=a.port)
    page_ident = c.recall(unpretty(a.page_hash), "page node")
    page_dest = RNS.Destination(page_ident, RNS.Destination.OUT,
                                RNS.Destination.SINGLE, "nomadnetwork",
                                "node")
    fields = {}
    for f in a.field or []:
        k, _, v = f.partition("=")
        fields["field_" + k] = v
    page = c.request(page_dest, a.path, data=fields or None)
    if page is None:
        print("page fetch failed", file=sys.stderr)
        return 1
    text = page.decode(errors="replace")
    print(text, end="")
    print("== games ==")
    for name, addr in parse_page(text):
        print(f"{name} {addr}")
    return 0


def cmd_play(a):
    c = Client(a.data_dir, a.identity, name="jclient", port=a.port)
    addr = unpretty(a.game_address)
    # # ponytail: the arrival count assumes a clean delivery queue at process
    # start (the test client sends and waits synchronously); a stale queued
    # reply from a crashed session would shift the count by one — drain the
    # queue at startup if that ever bites in practice.
    for i, line in enumerate(sys.stdin):
        line = line.rstrip("\n")
        c.send(addr, line.encode(), "play")
        # replies are per-turn deltas (spec §4): print verbatim. wait_reply
        # is by arrival index, not content — identical deltas (two "Taken."
        # turns) are distinct messages, and retransmission dedup is the
        # transport's job (spec §2).
        reply = c.wait_reply(i, timeout=120)
        # no added newline — a turn ends at the prompt, mid line; the
        # client's stdout is the session transcript itself, spec §7 data
        # contract
        sys.stdout.write(reply)
        sys.stdout.flush()
    return 0


def cmd_fetch(a):
    """Download a file served by a page destination (manual PDF, spec §6).
    Writes the exact response bytes to --out (default: basename of path)."""
    c = Client(a.data_dir, a.identity, name="jclient", port=a.port)
    ident = c.recall(unpretty(a.dest_address), "destination")
    dest = RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE,
                           "nomadnetwork", "node")
    data = c.request(dest, a.path, timeout=a.timeout,
                     max_response_size=a.max_size)
    if data is None:
        print("fetch failed (no response)", file=sys.stderr)
        return 1
    out = out_path(a.out, a.path)
    out.write_bytes(data)
    print(f"fetched {len(data)} bytes -> {out}", file=sys.stderr)
    return 0


def cmd_ping(a):
    """One round trip, shaped exactly like one of a player's turns: an LXMF
    message to a game address, the reply waited for, an ack sent back.

    The ack is the point. The host cannot see a client collect a reply, so its
    own delivery callback says only that the transfer finished; an outside
    checker that received the answer and says so is the only attestation that
    the outbound leg worked, and the ack is itself inbound traffic, so it
    cannot be faked from the server side.

    Nothing here writes a save slot or a ledger line on the host (see
    tests/test_ping.py), which is what makes it safe to run against a live node
    without inflating the numbers on its own page."""
    c = Client(a.data_dir, a.identity, name="jprobe", port=a.port)
    addr = unpretty(a.game_address)
    t0 = time.time()
    c.send(addr, PING.encode(), "ping")
    reply = c.wait_reply(0, timeout=a.wait)
    if reply is None:
        # inbound worked (the message left here); the answer never came back
        print(f"ping FAIL stage=reply waited={a.wait}s", file=sys.stderr)
        return 1
    if "jhost ok" not in reply:
        print(f"ping FAIL stage=content reply={reply[:60]!r}", file=sys.stderr)
        return 1
    boot = reply.partition("boot=")[2].split()[0] if "boot=" in reply else ""
    # The ack is the last thing this process does, so it has to be watched:
    # handle_outbound only queues, and exiting now drops it silently. Measured on
    # the live host before that was true -- two pings answered, zero acks.
    ack = c.wait_delivered(c.send(addr, f"{PING_ACK} {boot}".encode(), "ping"),
                           timeout=a.ack_wait)
    # Still success even if the ack itself got lost. What was measured is that
    # the host answered a ping; a receipt that failed to arrive on a slow mesh is
    # not evidence that anything is wrong with the node, and calling it a fault
    # would turn the page red over paperwork. The node then simply stays NOT
    # VERIFIED, which is the honest reading, and the reason is on this line for
    # whoever has to look.
    print(f"ping OK rtt={time.time() - t0:.0f}s boot={boot} ack={ack} "
          f"reply={reply[:70]}")
    return 0



def out_path(explicit, page_path):
    """Where `fetch` writes: --out when given, otherwise the basename of the
    requested path, in the working directory.

    The double Path() is the fix, not a quirk. `Path(a.path).name` is a str,
    and a str has no write_bytes, so every fetch that omitted --out died with
    AttributeError *after* the response had already arrived in full. That
    reads like a broken transport and is not one; it is also why --out was
    always passed by anything that used this command, and so the default was
    never run."""
    return Path(explicit) if explicit else Path(Path(page_path).name)


def cmd_smoke(a):
    c = Client(a.data_dir, Path(a.data_dir) / "identity", name="jclient",
               port=a.port)
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
    # assert the reply was *accepted* (responder replies "SMOKE-REPLY:<…>"
    # only when the sender signature validates)
    c.send(unpretty(dests["games"]["smoke"]), b"hello", "smoke")
    reply = c.wait_reply(0)
    print(reply, end="")
    if not reply.startswith("SMOKE-REPLY"):
        print(f"\nunexpected smoke reply: {reply!r}", file=sys.stderr)
        return 1
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
    sub.choices["browse"].add_argument("--path", default="/page/index.mu")
    sub.choices["browse"].add_argument(
        "--field", action="append", metavar="NAME=VALUE",
        help="submit as a micron form field (repeatable); sent field_<NAME>")
    sub.choices["play"].add_argument("game_address")
    s = sub.add_parser("fetch")
    s.add_argument("dest_address")
    s.add_argument("path")
    s.add_argument("--out", default=None)
    s.add_argument("--max-size", type=int, default=None,
                   help="max accepted response bytes (default: no cap)")
    s.add_argument("--timeout", type=int, default=600,
                   help="seconds to wait for the link + response")
    s.add_argument("--data-dir", default=".jclient")
    s.add_argument("--identity", default=".jclient/identity")
    s.add_argument("--port", type=int, default=4242)
    s = sub.add_parser("ping")
    s.add_argument("game_address")
    s.add_argument("--wait", type=int, default=300,
                   help="seconds to wait for the answer (spec §2: minutes is "
                        "normal on a LoRa-paced mesh, so this is not a timeout "
                        "in the latency sense)")
    s.add_argument("--ack-wait", type=int, default=240,
                   help="seconds to stay alive for the ack to be delivered "
                        "(exiting earlier drops it)")
    s.add_argument("--data-dir", default=".jclient")
    s.add_argument("--identity", default=".jclient/identity")
    s.add_argument("--port", type=int, default=4242)

    s = sub.add_parser("smoke")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--host-json", required=True)
    s.add_argument("--port", type=int, default=4242)
    a = ap.parse_args()
    sys.exit({"scan": cmd_scan, "browse": cmd_browse, "play": cmd_play,
              "fetch": cmd_fetch, "ping": cmd_ping,
              "smoke": cmd_smoke}[a.cmd](a))
