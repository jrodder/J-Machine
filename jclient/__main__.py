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
    # 2) LXMF message -> delivery -> reply (assert it was *accepted*:
    # the responder replies "SMOKE-REPLY:<content>" only when the sender
    # signature validates — the highest-risk unknown, gated here)
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
    s = sub.add_parser("smoke")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--host-json", required=True)
    a = ap.parse_args()
    if a.cmd == "smoke":
        sys.exit(cmd_smoke(a))