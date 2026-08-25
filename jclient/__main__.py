"""python3 -m jclient <scan|browse|play|smoke> (spec §7).

play mirrors Sideband's wire path: own delivery identity (persisted at
--identity — the player's save-slot file), one message per game line,
print each reply verbatim. Replies are per-turn deltas (spec §4), so the
client's stdout is the session transcript. ^D (EOF) exits; the host's
per-turn autosave already persisted the state."""
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
    out = Path(a.out) if a.out else Path(a.path).name
    out.write_bytes(data)
    print(f"fetched {len(data)} bytes -> {out}", file=sys.stderr)
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
    s = sub.add_parser("smoke")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--host-json", required=True)
    a = ap.parse_args()
    sys.exit({"scan": cmd_scan, "browse": cmd_browse, "play": cmd_play,
              "fetch": cmd_fetch, "smoke": cmd_smoke}[a.cmd](a))
