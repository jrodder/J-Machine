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

from . import guestbook
from .protocol import (DEST_JSON, FileSaveStore, handle_message,
                       player_stats, render_page, write_rns_config)


# Re-announce cadence default: 60 min (spec §3). RNS rate-limits announce
# rebroadcast to DEFAULT_AR_TARGET = 3600 s per destination
# (Interfaces/Interface.py:90); announcing more often than ~1/hour is
# treated as a rate violation and, after grace (5), the destination is
# blocked from rebroadcast for an hour (Transport.py:2234-2246) — so a
# faster cadence *reduces* visibility (meshchat stops seeing the node).
# 3600 s is the fastest safe cadence; startup announce + the ~11 s
# retransmit window cover initial discovery.
DEFAULT_ANNOUNCE_INTERVAL = 60 * 60


class Host:
    def __init__(self, data_dir, games_dir, name="🕹️ J-Machine Games 🕹️",
                 seed=None, port=4242, announce_interval=None,
                 propagation_node=None):
        self.data_dir = Path(data_dir)
        self.games_dir = Path(games_dir)
        self.name = name
        self.seed = seed
        self.port = port
        self.announce_interval = (announce_interval
                                  if announce_interval is not None
                                  else DEFAULT_ANNOUNCE_INTERVAL)
        self.lock = threading.Lock()
        # A reply is a link transfer to the player. A phone on a slow mesh
        # often cannot hold that link open for a multi-packet transcript: the
        # link closes mid-transfer, LXMF exhausts its attempts and drops the
        # message -- the turn is played, the player sees nothing. With a node
        # set, a failed reply is handed to the LXMF network instead and the
        # player's own router holds it until their next inbox poll.
        self.propagation_node = (bytes.fromhex(propagation_node.replace(":", ""))
                                 if propagation_node else None)
        self.sessions = {}            # {(game, sender): GameState}
        self.store = FileSaveStore(self.data_dir / "saves")
        self.routers = {}             # stem -> LXMRouter
        self.destinations = {}        # stem -> delivery Destination
        self.stories = {}             # stem -> story Path
        self.manuals = {}             # stem -> games/<stem>.pdf Path
        # games/names.txt: 'stem=Display Title' lines, read once at startup.
        # Display only — identities, saves and stats stay keyed by stem, so a
        # title is never a migration. Same convention as <stem>.pdf: an asset
        # that sits next to the game it labels (both under gitignored games/).
        self.names = {}
        nf = self.games_dir / "names.txt"
        if nf.exists():
            for line in nf.read_text().splitlines():
                stem, eq, shown = line.partition("=")
                if eq and stem.strip() and not stem.lstrip().startswith("#"):
                    self.names[stem.strip()] = shown.strip()
        # guestbook log + operator denylist (spec §6): plain files under
        # data/, read per request, so pruning/moderating needs no restart
        self.gb_log = self.data_dir / "guestbook.jsonl"
        self.gb_ban = self.data_dir / "guestbook-banned.txt"
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
        # the board is a second path on the SAME page destination: a micron
        # form submits to its own link's path with the field data attached, so
        # posting and viewing need no new identity and no extra announce
        self.page_dest.register_request_handler(
            "/page/guestbook.mu", self._guest_handler,
            allow=RNS.Destination.ALLOW_ALL)

        for story in sorted(self.games_dir.glob("*.z[3458]")):
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
        if self.propagation_node:
            router.set_outbound_propagation_node(self.propagation_node)
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
        manual = self.games_dir / f"{stem}.pdf"
        if manual.exists():
            # served from the PAGE destination (the public one) under the
            # /file path — NomadNet's convention for downloadable files
            # (NomadNet Guide: pages /page, files /file); exact-path handler
            # (RNS has no wildcards). Response is the canonical NomadNet
            # serve_file shape: [open file handle, {name: filename}] —
            # RNS turns that into a streaming Resource response with
            # metadata (Link.handle_request), and that is what the clients
            # expect (verified: NomadNet Node.py serve_file, meshchat's
            # NomadnetFileDownloader). auto_compress off: PDFs are binary.
            # ponytail: no per-request path validation beyond the exact
            # registered path (the handler map IS the allow-list)
            p = manual
            name = manual.name
            self.page_dest.register_request_handler(
                f"/file/{stem}.pdf",
                lambda path, data, rid, remote, requested_at:
                    [open(p, "rb"), {"name": name.encode()}],
                allow=RNS.Destination.ALLOW_ALL, auto_compress=False)
            self.manuals[stem] = manual

    def _page_handler(self, path, request_data, request_id,
                      remote_identity, requested_at):
        # RNS 1.5.0 response generators must take exactly 5 (or 6) params
        # (Link.handle_request inspects the signature — verified 1.5.0)
        games = [(stem, RNS.prettyhexrep(d.hash))
                 for stem, d in sorted(self.destinations.items())]
        # page is rendered per request (spec §6): stats computed fresh from
        # the slot tree — a file stat per player plus one small sidecar read,
        # no cache to invalidate
        stats = player_stats(self.store.root)
        page_hex = RNS.hexrep(self.page_dest.hash, delimit=False)
        # micron URL format (NomadNet Guide): <32-hex dest hash>:<path>
        # — rns:// URLs are for terminal tools (rngit/rnsh), not micron
        manuals = {s: f"{page_hex}:/file/{s}.pdf" for s in self.manuals}
        return render_page(self.name, games, stats, manuals=manuals,
                           guestbook=f"{page_hex}:/page/guestbook.mu",
                           names=self.names).encode()

    def _guest_handler(self, path, request_data, request_id,
                       remote_identity, requested_at):
        """Board view, and the post target when a field came in. A micron
        submit arrives as request_data["field_message"] — that prefix is what
        both NomadNet (Browser.py:425-440) and meshchat (meshchat.py:2198-2212)
        send — so one handler serves both; request_data is b"" for a plain
        fetch, which is what the dict test separates. The 5-parameter shape is
        required (RNS 1.5.0 inspects the handler signature).

        remote_identity is None for most visitors while browsing (guestbook.py
        cites why), so an unidentified post is stored with who: null and reads
        as 'anon' — nobody gets turned away for not identifying."""
        note = None
        if isinstance(request_data, dict) and "field_message" in request_data:
            who = (RNS.hexrep(remote_identity.hash, delimit=False)
                   if remote_identity is not None else None)
            note = guestbook.post(self.gb_log, self.gb_ban, who,
                                  request_data.get("field_message"))
        return guestbook.render(
            self.name, RNS.hexrep(self.page_dest.hash, delimit=False),
            guestbook.entries(self.gb_log), note).encode()

    def _sign(self, sender, line):
        """"sign: <line>" in any game conversation -> the same board, on the
        channel that always identifies its sender, so the reply can promise a
        verified tag (guestbook.py: the two message boards in the wild work the
        same way). Not advertised on the page; posting never requires it."""
        note = guestbook.post(self.gb_log, self.gb_ban, sender, line)
        if note != "saved":
            return guestbook.NOTES[note]
        return f"{guestbook.NOTES[note]} #{sender[:guestbook.TAG]}"

    def _on_message(self, stem, msg):
        if not msg.signature_validated and \
                msg.unverified_reason == LXMessage.SOURCE_UNKNOWN:
            # source was unknown when LXMF parsed the message (fresh node,
            # or the sender's delivery announce hasn't propagated — the
            # ~1/hour announce rate limit). Request the path, which is
            # answered with the sender's announce, then re-parse so the
            # signature validates against the now-known identity.
            RNS.Transport.await_path(msg.source_hash, timeout=15)
            try:
                msg = LXMessage.unpack_from_bytes(msg.packed)
            except Exception as e:
                print(f"jhost: {stem}: re-parse after path request failed "
                      f"({e})", file=sys.stderr)
        sender = msg.source_hash.hex()
        text = msg.content_as_string()  # None if not valid UTF-8
        with self.lock:
            reply = handle_message(stem, sender, text,
                                   msg.signature_validated, self.sessions,
                                   self.store, str(self.stories[stem]),
                                   self.seed, sign=self._sign)
        src = RNS.Identity.recall(msg.source_hash)
        if src is None:
            # sender not in our known-destinations table yet (fresh node, or
            # its delivery announce hasn't propagated — the ~1/hour announce
            # rate limit). Ask the network for its path; a path request is
            # answered with the sender's announce, which lets us recall it.
            RNS.Transport.await_path(msg.source_hash, timeout=15)
            src = RNS.Identity.recall(msg.source_hash)
            if src is None:
                print(f"jhost: {stem}: cannot recall {sender[:8]} for reply "
                      f"(path request failed)", file=sys.stderr)
                return
        dest = RNS.Destination(src, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        # reply pattern verified spec §2; LXMessage requires an explicit
        # source (the game's delivery destination); output uncapped. One
        # message per reply, however long: RNS re-advertises a resource until
        # the recipient pulls it and never cancels, so a slow phone gets the
        # whole reply in one readable message instead of parts that arrive
        # out of order.
        m = LXMessage(dest, self.destinations[stem],
                      content=reply.encode(), title=stem)
        m.failed_callback = lambda _m: self._reply_failed(stem, sender, dest,
                                                          reply)
        self.routers[stem].handle_outbound(m)

    def _reply_failed(self, stem, sender, dest, reply):
        """LXMF drops a reply once its direct attempts run out (only a debug
        log line says so). Say it, and hand the text to the propagation
        network if the operator named a node that can hold it for the player."""
        print(f"jhost: {stem}: reply to {sender[:guestbook.TAG]} lost over link",
              file=sys.stderr)
        # LXMF keeps reusing a link the peer abandoned ("Using available direct
        # link" + "Waiting for proof" for an hour, in the log), so every later
        # reply to that player queues behind a link that can never deliver.
        # Drop it: the next attempt builds a fresh one. This is the state a
        # service restart used to be the only way to clear.
        dead = self.routers[stem].direct_links.pop(dest.hash, None)
        if dead is not None:
            try:
                dead.teardown()
            except Exception:
                pass
        if self.propagation_node is None:
            print(f"jhost: {stem}: no --propagation-node set; reply dropped",
                  file=sys.stderr)
            return
        m = LXMessage(dest, self.destinations[stem], content=reply.encode(),
                      title=stem, desired_method=LXMessage.PROPAGATED)
        m.failed_callback = lambda _m: print(
            f"jhost: {stem}: reply to {sender[:guestbook.TAG]} lost via "
            f"propagation too", file=sys.stderr)
        try:
            self.routers[stem].handle_outbound(m)
        except OSError as e:
            print(f"jhost: {stem}: propagation handoff failed ({e})",
                  file=sys.stderr)

    def _announce_all(self):
        # the announce itself is silent at loglevel 6 — this line is the
        # operator's proof the re-announce cycle (and the startup announce)
        # is actually firing
        print("jhost: announcing page + games", file=sys.stderr)
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
