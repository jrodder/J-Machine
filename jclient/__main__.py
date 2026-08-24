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
    prev = ""
    for line in sys.stdin:
        line = line.rstrip("\n")
        c.send(addr, line.encode(), "play")
        # `prev` is the only reply string seen so far: each new reply is a
        # strict extension of it (cumulative transcript), so it can never
        # equal it. A rejection ("[Rejected…]", "[Game over]") doesn't
        # start with prev and is a new string. No other message source
        # exists on this delivery identity.
        reply = c.wait_reply({prev} if prev else set(), timeout=120)
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
    # assert the reply was *accepted* (responder replies "SMOKE-REPLY:<…>"
    # only when the sender signature validates)
    c.send(unpretty(dests["games"]["smoke"]), b"hello", "smoke")
    reply = c.wait_reply(set())
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
    s = sub.add_parser("smoke")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--host-json", required=True)
    a = ap.parse_args()
    sys.exit({"scan": cmd_scan, "browse": cmd_browse, "play": cmd_play,
              "smoke": cmd_smoke}[a.cmd](a))
