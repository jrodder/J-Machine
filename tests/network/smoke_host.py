"""Task 2 rig smoke responder (spec §7 test 0): a minimal RNS+LXMF node
that proves the transport — announce -> recall -> RNS request -> LXMF
delivery -> reply — before jhost exists. Task 3's jhost/host.py is this
exact wiring (page node + one delivery router + reply callback) moved into
the Host class.

Run: python -m tests.network.smoke_host <data-dir> <port> [--name]
Writes <data-dir>/host-destinations.json = {"page": <hash>,
"games": {"smoke": <hash>}}; announces; then serves until killed.
"""
import json
import sys
import time
from pathlib import Path

import RNS
from LXMF import LXMessage, LXMRouter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jhost.protocol import DEST_JSON, write_rns_config  # stdlib-only helpers

NAME = "Smoke"


def main():
    data_dir = Path(sys.argv[1])
    port = int(sys.argv[2])
    name = sys.argv[3] if len(sys.argv) > 3 else NAME

    write_rns_config(data_dir / "rns", "host", port)
    RNS.Reticulum(str(data_dir / "rns"))

    # page node (nomadnetwork.node convention, spec §2)
    page_ident = _identity(data_dir, "page")
    page_dest = RNS.Destination(page_ident, RNS.Destination.IN,
                                RNS.Destination.SINGLE, "nomadnetwork", "node")
    page_dest.register_request_handler("/page/index.mu", _page,
                                       allow=RNS.Destination.ALLOW_ALL)

    # one game delivery identity + reply callback (lxmf.delivery, spec §2)
    game_ident = _identity(data_dir, "smoke")
    router = LXMRouter(identity=game_ident,
                       storagepath=str(data_dir / "lxmf"), name="smoke")
    # register_delivery_identity creates AND registers the lxmf/delivery
    # RNS destination; a second manual RNS.Destination for the same
    # (identity, protocol) pair raises "already registered" (1.1.1)
    game_dest = router.register_delivery_identity(game_ident,
                                                  display_name="smoke",
                                                  stamp_cost=0)
    router.register_delivery_callback(lambda msg: _on_message(router, msg,
                                                              game_dest))

    (data_dir / DEST_JSON).write_text(json.dumps({
        "page": RNS.prettyhexrep(page_dest.hash),
        "games": {"smoke": RNS.prettyhexrep(game_dest.hash)},
    }, indent=1))

    page_dest.announce(app_data=name.encode())
    # router.announce() carries the LXMF delivery app_data (stamp cost);
    # the client learns the return stamp cost from it
    router.announce(game_dest.hash)

    print(f"smoke_host: serving on port {port}", file=sys.stderr, flush=True)
    while True:
        time.sleep(300)


def _identity(data_dir, stem):
    p = data_dir / "identities" / stem
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return RNS.Identity.from_file(str(p))
    ident = RNS.Identity()
    ident.to_file(str(p))
    return ident


def _page(path, request_data, request_id, remote_identity, requested_at):
    # RNS 1.5.0 response generators must take exactly 5 (or 6) params
    # (site-packages/RNS/Link.py handle_request inspects the signature)
    return (f">{NAME}\n>\nSMOKE-RIG\n").encode()


def _on_message(router, message, source_dest):
    """Reply to a delivery. A non-validated sender gets the rejection
    string — this is the assertion the smoke client checks (signature
    validation must actually work, not just transport)."""
    if not message.signature_validated:
        reply = "[Rejected: unverified sender]"
    else:
        reply = "SMOKE-REPLY:" + (message.content_as_string() or "")
    src = RNS.Identity.recall(message.source_hash)
    if src is None:
        return
    dest = RNS.Destination(src, RNS.Destination.OUT,
                           RNS.Destination.SINGLE, "lxmf", "delivery")
    router.handle_outbound(LXMessage(dest, source_dest, content=reply.encode(),
                                     title="smoke"))


if __name__ == "__main__":
    main()