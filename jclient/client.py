"""Minimal LXMF client (spec §7). Mirrors Sideband's wire path: own
delivery identity + one LXMRouter per process; send message to a delivery
address; receive replies via the delivery callback. Test client — the real
client is Sideband on a phone."""
import json
import sys
import time
from pathlib import Path

import RNS
from LXMF import LXMessage, LXMRouter

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
        # The config loglevel=-1 is clamped to 0 (LOG_CRITICAL) at parse
        # time (site-packages/RNS/Reticulum.py:468) and LOG_NONE is not
        # reachable via the 1.5.0 constructor either (Reticulum.py:314) —
        # the runtime override is required. The client's stdout is a data
        # contract (spec §7: norm(stdout) == session transcript); one log
        # line would pollute it.
        RNS.loglevel = RNS.LOG_NONE
        self.router = LXMRouter(identity=self.ident,
                                storagepath=str(self.data_dir / "lxmf"),
                                name=name)
        # register_delivery_identity creates AND registers the lxmf/delivery
        # RNS destination (a second manual one raises "already registered")
        self.dest = self.router.register_delivery_identity(
            self.ident, display_name=name, stamp_cost=0)
        # Announce the delivery destination: the host recalls the sender
        # identity and routes replies via the destination announce (spec §7)
        self.router.announce(self.dest.hash)
        self._replies = []
        self.router.register_delivery_callback(
            lambda msg: self._replies.append(msg.content_as_string() or ""))

    def recall(self, h, what, timeout=90):
        """Poll recall until the key appears (announce path, spec §7).
        A peer joining after the host's announce retransmit window (~11s,
        RNS 1.5.0: PATHFINDER_R=1, LOCAL_REBROADCASTS_MAX=2) must request
        the path — RNS serves path requests with the destination announce
        (the designed bootstrap; verified in site-packages/RNS/Transport.py)."""
        deadline = time.time() + timeout
        last_pr = 0.0
        while time.time() < deadline:
            ident = RNS.Identity.recall(h)
            if ident is not None:
                return ident
            if time.time() - last_pr > 10:
                RNS.Transport.request_path(h)
                last_pr = time.time()
            time.sleep(3)
        raise RuntimeError(f"cannot recall {what} after {timeout}s "
                           f"(was it announced? check host logs)")

    def request(self, dest, path, timeout=30, max_response_size=65536):
        """RNS request/response (page fetch). Returns the response bytes.
        A Link establishes asynchronously (verified+encrypted handshake);
        request() needs the link ACTIVE (mdu is set on establishment,
        site-packages/RNS/Link.py) — wait for that first.
        max_response_size=None accepts any size (manual PDFs are MBs; a
        large response arrives chunked via RNS.Resource, completing on
        this same callback — verified Link.py response_resource_concluded)."""
        got = []
        link = RNS.Link(dest)
        deadline = time.time() + timeout
        while link.status != RNS.Link.ACTIVE and time.time() < deadline:
            time.sleep(0.5)
        if link.status != RNS.Link.ACTIVE:
            return None
        def _recv(r):
            # file responses (NomadNet serve_file shape) arrive as a
            # BufferedReader when chunked via RNS.Resource — and RNS
            # closes the handle + unlinks the storage file right after
            # this callback returns (Resource.py assemble), so the read
            # must happen here, not later
            data = r.response
            if hasattr(data, "read"):
                data = data.read()
            got.append(data)
        link.request(path, b"",
                     _recv,
                     None, None, timeout=timeout,
                     max_response_size=max_response_size)
        deadline = time.time() + timeout
        while not got and time.time() < deadline:
            time.sleep(1)
        return got[0] if got else None

    def send(self, game_addr, content, title):
        if isinstance(content, str): content = content.encode()
        ident = self.recall(game_addr, "game address")
        dest = RNS.Destination(ident, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        m = LXMessage(dest, self.dest,
                      content=content, title=title)
        self.router.handle_outbound(m)

    def wait_reply(self, after_index, timeout=90):
        """Block until more than `after_index` replies have arrived; return
        the reply at that index (0-based arrival position).

        By arrival count, not content: LXMF/RNS dedupe retransmissions at
        the delivery layer (spec §2), so each send adds exactly one reply.
        Content-based filtering would swallow legitimate replies: under the
        per-turn delta contract (spec §4) two turns can emit identical text
        (e.g. "Taken." twice), which a seen-set would treat as a duplicate."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self._replies) > after_index:
                return self._replies[after_index]
            time.sleep(2)
        raise TimeoutError("no reply")


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
