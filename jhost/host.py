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
from LXMF import LXMessage, LXMRouter

from .protocol import (DEST_JSON, FileSaveStore, handle_message,
                       player_stats, render_page, write_rns_config)


# re-announce cadence default: 15 min (operator ruling 2026-08-25;
# was 300 s — spec §3). Keeps the page node + game addresses in the
# NomadNet node list for late joiners (initial ~11 s retransmit window).
DEFAULT_ANNOUNCE_INTERVAL = 15 * 60


class Host:
    def __init__(self, data_dir, games_dir, name="J-Machine Games",
                 seed=None, port=4242, announce_interval=None):
        self.data_dir = Path(data_dir)
        self.games_dir = Path(games_dir)
        self.name = name
        self.seed = seed
        self.port = port
        self.announce_interval = (announce_interval
                                  if announce_interval is not None
                                  else DEFAULT_ANNOUNCE_INTERVAL)
        self.lock = threading.Lock()
        self.sessions = {}            # {(game, sender): GameState}
        self.store = FileSaveStore(self.data_dir / "saves")
        self.routers = {}             # stem -> LXMRouter
        self.destinations = {}        # stem -> delivery Destination
        self.stories = {}             # stem -> story Path
        self.versions = {}            # stem -> header version (read once)
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
            print(f"jhost:   {stem}: {RNS.prettyhexrep(d.hash)}",
                  file=sys.stderr)

    def run(self):
        """Block, re-announcing on self.announce_interval (spec §3)."""
        while True:
            time.sleep(self.announce_interval)
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
        # stamp_cost=0: free to play (spec §9; stamps are PoW, no credits).
        # register_delivery_identity creates AND registers the lxmf/delivery
        # RNS destination and returns it (a second manual RNS.Destination for
        # the same identity raises "already registered" — verified 1.1.1)
        dl = router.register_delivery_identity(ident, display_name=stem,
                                               stamp_cost=0)
        router.register_delivery_callback(
            lambda msg: self._on_message(stem, msg))
        self.routers[stem] = router
        self.destinations[stem] = dl
        self.stories[stem] = story
        # header bytes 0-1 = version (Phase 1 verified fact); read once,
        # not per page render
        with open(story, "rb") as f:
            self.versions[stem] = int.from_bytes(f.read(2), "big")

    def _page_handler(self, path, request_data, request_id,
                      remote_identity, requested_at):
        # RNS 1.5.0 response generators must take exactly 5 (or 6) params
        # (Link.handle_request inspects the signature — verified 1.5.0)
        games = [(stem, self.versions[stem], RNS.prettyhexrep(d.hash))
                 for stem, d in sorted(self.destinations.items())]
        # page is rendered per request (spec §6): stats computed fresh from
        # the slot files — a handful of file stats, no cache to invalidate
        stats = player_stats(self.store.root)
        return render_page(self.name, games, stats).encode()

    def _on_message(self, stem, msg):
        sender = msg.source_hash.hex()
        text = msg.content_as_string()  # None if not valid UTF-8
        with self.lock:
            reply = handle_message(stem, sender, text,
                                   msg.signature_validated, self.sessions,
                                   self.store, str(self.stories[stem]),
                                   self.seed)
        src = RNS.Identity.recall(msg.source_hash)
        if src is None:
            print(f"jhost: {stem}: cannot recall {sender[:8]} for reply",
                  file=sys.stderr)
            return
        dest = RNS.Destination(src, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        # reply pattern verified spec §2; LXMessage requires an explicit
        # source (the game's delivery destination); output uncapped
        m = LXMessage(dest, self.destinations[stem],
                      content=reply.encode(), title=stem)
        self.routers[stem].handle_outbound(m)

    def _announce_all(self):
        self.page_dest.announce(app_data=self.name.encode())
        for stem, d in self.destinations.items():
            # router.announce() carries the LXMF delivery app_data
            # (stamp cost) the client needs — verified 1.1.1
            self.routers[stem].announce(d.hash)

    def _write_destinations(self):
        out = {"page": RNS.prettyhexrep(self.page_dest.hash),
               "games": {s: RNS.prettyhexrep(d.hash)
                         for s, d in sorted(self.destinations.items())}}
        (self.data_dir / DEST_JSON).write_text(json.dumps(out, indent=1))
