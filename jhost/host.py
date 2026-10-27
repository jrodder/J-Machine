"""Reticulum game host (spec §3): one process, one RNS instance, one
LXMRouter per game (lxmf 1.1.1 allows one delivery identity per router —
verified spec §2), per-game persisted identity = the static per-game
address.

# ponytail: global lock, per-session locks if throughput matters
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import RNS
from LXMF import LXMessage, LXMRouter

from . import guestbook
from . import window
from .ledger import Ledger
from .logs import LogFilter
from .protocol import (DEST_JSON, PING, PING_ACK, FileSaveStore,
                       handle_message, ping_reply, player_stats, render_page,
                       render_stats, write_rns_config)


# Re-announce cadence default: 60 min (spec §3). RNS rate-limits announce
# rebroadcast to DEFAULT_AR_TARGET = 3600 s per destination
# (Interfaces/Interface.py:90); announcing more often than ~1/hour is
# treated as a rate violation and, after grace (5), the destination is
# blocked from rebroadcast for an hour (Transport.py:2234-2246) — so a
# faster cadence *reduces* visibility (meshchat stops seeing the node).
# 3600 s is the fastest safe cadence; startup announce + the ~11 s
# retransmit window cover initial discovery.
DEFAULT_ANNOUNCE_INTERVAL = 60 * 60

# Propagation poll cadence default: 5 min. A player whose client cannot build
# a link to this host hands the turn to a propagation node instead -- that is
# what propagation nodes are for ("store and forward messages to users or
# endpoints that are not directly reachable at the time of message emission",
# LXMF doc). LXMF then waits for the RECIPIENT to ask: the router job loop
# never pulls inbound mail (LXMRouter.py jobs(), 880-912 -- outbound, link and
# store upkeep only), so a client app that never calls
# request_messages_from_propagation_node leaves the turn at the node forever.
# Without this poll those turns vanish with no trace on either side.
DEFAULT_PROPAGATION_PULL = 5 * 60
# The router's own pull state machine (LXMRouter PR_*); between these it is
# mid-download and a second request would reset the transfer progress.
PR_IN_FLIGHT = range(1, 7)   # PR_PATH_REQUESTED .. PR_RESPONSE_RECEIVED
RUN_TICK = 15                # run() resolution, independent of both cadences
HEALTH_INTERVAL = 60         # data/health.json rewrite cadence

# The outcome names are the ledger states verbatim, so a counter and a ledger
# line can never disagree about what happened to a message.
OUTCOMES = ("abandoned", "handed_off", "delivered", "propagated", "failed",
            "dropped")


def fresh_stats():
    """The health counters' shape, defined once so a running host and a bare
    one assembled without __init__ can never disagree about the keys."""
    stats = {"inbound": 0, "page_requests": 0, "page_errors": 0,
             "stats_requests": 0, "stats_errors": 0,
             "pings": 0, "acks": 0, "ping_unanswered": 0,
             "last_ping": None, "last_roundtrip_ok": None,
             "guest_requests": 0, "guest_posts": 0, "file_requests": 0,
             "announce_cycles": 0, "poll_cycles": 0, "last_inbound": None,
             "last_page": None, "last_stats": None,
             "last_announce": None, "last_poll": None}
    for outcome in OUTCOMES:
        stats[outcome] = 0
    return stats


class Host:
    def __init__(self, data_dir, games_dir, name="🕹️ J-Machine Games 🕹️",
                 seed=None, port=4242, announce_interval=None,
                 propagation_node=None, propagation_pull=None):
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
        self.propagation_pull = (propagation_pull
                                 if propagation_pull is not None
                                 else DEFAULT_PROPAGATION_PULL)
        self.sessions = {}            # {(game, sender): GameState}
        self.store = FileSaveStore(self.data_dir / "saves")
        self.logfilter = None         # set in start(); RNS log destination
        self.routers = {}             # stem -> LXMRouter
        self.identities = {}          # stem -> RNS.Identity (the mail's owner)
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
        # one durable outcome per inbound message (spec §3, jhost/ledger.py)
        self.ledger = Ledger(self.data_dir / "ledger.jsonl")
        # current-state counters for data/health.json; the per-message detail
        # is the ledger, this is the snapshot a check reads without parsing it
        self.started = None
        self.stats = fresh_stats()
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
        # Own the RNS log line: hub churn is expected and voluminous, and at
        # that volume the journal hides the failures that matter (spec §5).
        self.logfilter = LogFilter()
        RNS.Reticulum(str(cfg_dir), logdest=self.logfilter.emit)

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
        # third path on the same page destination (no new identity, no new
        # announce): the activity breakdown the main page leaves off
        self.page_dest.register_request_handler(
            "/page/stats.mu", self._stats_handler,
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
        """Block: re-announce on self.announce_interval (spec §3) and collect
        each game's propagated mail on self.propagation_pull. start() already
        announced, so both timers start from now."""
        now = time.time()
        self.started = now
        next_announce = now + self.announce_interval
        polling = bool(self.propagation_node and self.propagation_pull)
        next_pull = now if polling else None
        next_health = now
        while True:
            time.sleep(RUN_TICK)
            now = time.time()
            if now >= next_health:
                self.write_health()
                next_health = now + HEALTH_INTERVAL
            if now >= next_announce:
                self._announce_all()
                next_announce = now + self.announce_interval
            if next_pull is not None and now >= next_pull:
                self._pull_propagation()
                next_pull = now + self.propagation_pull

    def _pull_propagation(self):
        """Ask the outbound propagation node for anything it is holding for
        each game identity. Anything it has arrives through the same
        register_delivery_callback path as a direct send, so _on_message needs
        no second entry point.

        One node, and only one: this collects what the node named by
        --propagation-node holds. A client that stored its turn on a different
        node is still out of reach -- the complete fix is for this host to run
        as a propagation node itself (enable_propagation) so players' clients
        have a node here to store to, which is an operator decision with disk
        and peering costs, not a default."""
        if self.propagation_node is None or not self.propagation_pull:
            return
        self.stats["poll_cycles"] += 1
        self.stats["last_poll"] = time.time()
        for stem in sorted(self.routers):
            router = self.routers[stem]
            if getattr(router, "propagation_transfer_state", 0) in PR_IN_FLIGHT:
                continue        # mid-download: a second request resets it
            try:
                router.request_messages_from_propagation_node(
                    self.identities[stem])
            except Exception as e:
                print(f"jhost: {stem}: propagation poll failed ({e})",
                      file=sys.stderr)
        print("jhost: polled propagation node for "
              f"{len(self.routers)} game(s)", file=sys.stderr)

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
        self.identities[stem] = ident
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
            self.page_dest.register_request_handler(
                f"/file/{stem}.pdf", self._manual_handler(manual),
                allow=RNS.Destination.ALLOW_ALL, auto_compress=False)
            self.manuals[stem] = manual

    def _manual_handler(self, manual):
        def handler(path, data, request_id, remote_identity, requested_at):
            # the 5-parameter shape is required (RNS inspects the signature);
            # the response is the canonical NomadNet serve_file shape
            self.stats["file_requests"] += 1
            if not manual.exists():
                # registered at startup, so this only happens if the file went
                # away while running -- name it instead of a bare traceback
                print(f"jhost: manual {manual.name} is gone from {manual}",
                      file=sys.stderr)
            return [open(manual, "rb"), {"name": manual.name.encode()}]
        return handler

    def _page_handler(self, path, request_data, request_id,
                      remote_identity, requested_at):
        # RNS 1.5.0 response generators must take exactly 5 (or 6) params
        # (Link.handle_request inspects the signature — verified 1.5.0)
        self.stats["page_requests"] += 1
        self.stats["last_page"] = time.time()
        try:
            return self._render_page().encode()
        except Exception as e:
            # a page that fails to render otherwise looks exactly like a page
            # nobody asked for -- and it is the one thing a first-time visitor
            # uses, so count it and say it before RNS swallows it
            self.stats["page_errors"] += 1
            print(f"jhost: page render failed ({type(e).__name__}: {e})",
                  file=sys.stderr)
            raise

    def _render_page(self):
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
                           names=self.names,
                           stats_page=f"{page_hex}:/page/stats.mu")

    def _stats_handler(self, path, request_data, request_id,
                       remote_identity, requested_at):
        """The activity page: totals, per game, recent days, and what happened
        to the replies. Rendered per request like the page and the board, and
        counted, because a link that returns nothing is indistinguishable from
        a node that is not answering (see _page_handler).

        The reply counts label their own window: the ledger is size-rotated, so
        an unlabelled count would read as all-time and then quietly shrink."""
        self.stats["stats_requests"] += 1
        self.stats["last_stats"] = time.time()
        try:
            ledger = self.ledger.path
            page_hex = RNS.hexrep(self.page_dest.hash, delimit=False)
            return render_stats(
                self.name, player_stats(self.store.root),
                activity=window.activity([ledger.with_name(ledger.name + ".1"),
                                          ledger]),
                back=f"{page_hex}:/page/index.mu",
                names=self.names, service=self._service()).encode()
        except Exception as e:
            # same reason _page_handler counts: RNS swallows a handler that
            # raises, so a broken stats page would look like nobody reading it
            self.stats["stats_errors"] += 1
            print(f"jhost: stats page render failed ({type(e).__name__}: {e})",
                  file=sys.stderr)
            raise

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
        self.stats["guest_requests"] += 1
        note = None
        if isinstance(request_data, dict) and "field_message" in request_data:
            who = (RNS.hexrep(remote_identity.hash, delimit=False)
                   if remote_identity is not None else None)
            note = guestbook.post(self.gb_log, self.gb_ban, who,
                                  request_data.get("field_message"))
        if note == "saved":
            self.stats["guest_posts"] += 1
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

    def _record(self, seq, state, **fields):
        """The only place a message outcome is written: the ledger line and
        the health counter come from one call, so they cannot disagree."""
        self.ledger.close(seq, state, **fields)
        if state in OUTCOMES:
            self.stats[state] += 1

    def _on_message(self, stem, msg):
        """One inbound message, from arrival to recorded outcome.

        The try/except is the point: LXMF calls this from its own delivery
        path and wraps the handler call in an except that logs one line
        (LXMessage.py:568-570 is the same shape), so an exception anywhere
        below is how a player sends a turn and hears nothing at all. Nothing
        here is allowed to escape unrecorded.
        """
        try:
            sender = msg.source_hash.hex()
        except Exception as e:
            print(f"jhost: {stem}: inbound message with unusable source "
                  f"({type(e).__name__}: {e}); ignored", file=sys.stderr)
            return
        short = sender[:guestbook.TAG]
        # A ping is answered before a record is opened, and opens none: no
        # ledger line, no session, no slot, no move counted. That is the design,
        # not an optimisation. An instrument that increments the numbers it
        # exists to check would be the third thing on the activity page lying
        # about players (the others being an unlabelled rotated window and a
        # self-reported health dot). The verb is matched exactly, never as a
        # prefix, so a player typing "ping the wizard" plays normally.
        try:
            verb = (msg.content_as_string() or "").strip().lower()
        except Exception:
            verb = ""
        if verb == PING:
            self._answer_ping(stem, msg)
            return
        if verb == PING_ACK or verb.startswith(PING_ACK + " "):
            # The only attestation of the outbound leg anywhere: the host cannot
            # see a player's client collect a reply, so an outside checker that
            # says "it arrived" is the evidence, and it is inbound traffic, so
            # it cannot be faked from this side.
            self.stats["acks"] += 1
            self.stats["last_roundtrip_ok"] = time.time()
            return
        seq = self.ledger.open(stem, short)
        self.stats["inbound"] += 1
        self.stats["last_inbound"] = time.time()
        try:
            self._serve(stem, sender, short, msg, seq)
        except Exception as e:
            self._record(seq, "abandoned", why=f"{type(e).__name__}: {e}")
            print(f"jhost: {stem}/{short}: message failed before any reply "
                  f"({type(e).__name__}: {e})", file=sys.stderr)

    def _answer_ping(self, stem, msg):
        """Reply to an external check without touching a player's data.

        The reply needs what a turn needs: a recalled identity to address. An
        un-recallable source is counted and said out loud, because "nobody could
        reach them" and "nobody checked" must not become the same observation."""
        self.stats["pings"] += 1
        self.stats["last_ping"] = time.time()
        src = RNS.Identity.recall(msg.source_hash)
        if src is None:
            RNS.Transport.await_path(msg.source_hash, timeout=15)
            src = RNS.Identity.recall(msg.source_hash)
        if src is None:
            self.stats["ping_unanswered"] += 1
            print("jhost: ping could not be answered (sender identity "
                  "unknown); round trip not possible", file=sys.stderr)
            return
        payload = ping_reply(self.ledger.boot, len(self.stories)).encode()
        dest = RNS.Destination(src, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        m = LXMessage(dest, self.destinations[stem], content=payload,
                      title=stem)
        self.routers[stem].handle_outbound(m)

    def _service(self):
        """The facts behind the status pill, read from the same readiness
        snapshot health.json carries, so the page and the file never hold two
        opinions about what is wrong."""
        h = self.readiness()
        faults = []
        if h["games_missing_story"]:
            faults.append("story file missing: "
                          + ", ".join(h["games_missing_story"][:3]))
        if not h["saves_writable"]:
            faults.append("save store is not writable")
        if h["ledger_unresolved"]:
            faults.append(f"{h['ledger_unresolved']} turns with no outcome")
        if h["interfaces_total"] and not h["interfaces_online"]:
            faults.append("no interface online")
        errs = h["page_errors"] + h["stats_errors"]
        if errs:
            faults.append(f"{errs} page render failure{'s' if errs > 1 else ''}")
        return {"faults": faults, "verified_at": h.get("last_roundtrip_ok")}

    def _serve(self, stem, sender, short, msg, seq):
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
        text = msg.content_as_string()  # None if not valid UTF-8
        n_in = len(text) if text else 0

        # Identity BEFORE the game, not after it. Resolving it after
        # handle_message meant the turn was fed, the slot autosaved, and only
        # then the reply abandoned — the player's story advanced with nothing
        # to show for it. A turn that cannot be answered is not played.
        src = RNS.Identity.recall(msg.source_hash)
        if src is None:
            # sender not in our known-destinations table yet (fresh node, or
            # its delivery announce hasn't propagated — the ~1/hour announce
            # rate limit). Ask the network for its path; a path request is
            # answered with the sender's announce, which lets us recall it.
            RNS.Transport.await_path(msg.source_hash, timeout=15)
            src = RNS.Identity.recall(msg.source_hash)
        if src is None:
            self._record(seq, "abandoned", bytes_in=n_in,
                         why="no_recalled_identity")
            print(f"jhost: {stem}/{short}: cannot recall sender for reply "
                  f"(path request failed); turn not played", file=sys.stderr)
            return

        with self.lock:
            reply = handle_message(stem, sender, text,
                                   msg.signature_validated, self.sessions,
                                   self.store, str(self.stories[stem]),
                                   self.seed, sign=self._sign)
        payload = reply.encode()
        dest = RNS.Destination(src, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, "lxmf", "delivery")
        # reply pattern verified spec §2; LXMessage requires an explicit
        # source (the game's delivery destination); output uncapped. One
        # message per reply, however long: RNS re-advertises a resource until
        # the recipient pulls it and never cancels, so a slow phone gets the
        # whole reply in one readable message instead of parts that arrive
        # out of order.
        m = LXMessage(dest, self.destinations[stem],
                      content=payload, title=stem)
        m.failed_callback = lambda _m: self._reply_failed(stem, short, dest,
                                                          payload, seq, _m)
        # the other half of the record: without this, a reply that never
        # finishes and a reply that arrived are the same event (nothing), and
        # "is the server working" has no answer. DELIVERED here means the
        # transfer completed into the recipient's side, NOT that a human read
        # it — the player's next command is the only receipt of that.
        m.register_delivery_callback(
            lambda _m: self._settled(stem, short, seq, _m))
        self.routers[stem].handle_outbound(m)
        # our own boundary ends here; what the mesh does with it is a second,
        # later line (§3: what can be certified and what cannot)
        self._record(seq, "handed_off", bytes_in=n_in,
                     bytes_out=len(payload))

    def _settled(self, stem, short, seq, m):
        """LXMF's terminal signal for a reply. DELIVERED (state 0x08) is the
        transfer completing; a PROPAGATED message arrives here as SENT with
        method PROPAGATED, meaning a node is holding it until the player
        collects. Neither is proof anyone saw it."""
        if m.state == LXMessage.DELIVERED:
            state = "delivered"
        elif getattr(m, "method", None) == LXMessage.PROPAGATED:
            state = "propagated"
        elif m.state == LXMessage.REJECTED:
            state = "rejected"
        else:
            state = f"concluded_{m.state}"
        self._record(seq, state,
                     attempts=getattr(m, "delivery_attempts", None))

    def _reply_failed(self, stem, short, dest, payload, seq, m=None):
        """LXMF drops a reply once its direct attempts run out (only a debug
        log line says so). Say it, record it, and hand the text to the
        propagation network if the operator named a node that can hold it for
        the player."""
        print(f"jhost: {stem}: reply to {short} lost over link",
              file=sys.stderr)
        self._record(seq, "failed", why="link_delivery_failed",
                     bytes_out=len(payload),
                     attempts=getattr(m, "delivery_attempts", None))
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
            self._record(seq, "dropped", why="no_propagation_node")
            print(f"jhost: {stem}: no --propagation-node set; reply dropped",
                  file=sys.stderr)
            return
        m = LXMessage(dest, self.destinations[stem], content=payload,
                      title=stem, desired_method=LXMessage.PROPAGATED)
        m.failed_callback = lambda _m: self._propagation_lost(stem, short, seq)
        m.register_delivery_callback(
            lambda _m: self._settled(stem, short, seq, _m))
        try:
            self.routers[stem].handle_outbound(m)
        except OSError as e:
            self._record(seq, "dropped",
                         why=f"propagation_handoff_{type(e).__name__}")
            print(f"jhost: {stem}: propagation handoff failed ({e})",
                  file=sys.stderr)

    def _propagation_lost(self, stem, short, seq):
        self._record(seq, "dropped", why="propagation_failed")
        print(f"jhost: {stem}: reply to {short} lost via propagation too",
              file=sys.stderr)

    def _announce_all(self):
        # the announce itself is silent at loglevel 6 — this line is the
        # operator's proof the re-announce cycle (and the startup announce)
        # is actually firing
        print("jhost: announcing page + games", file=sys.stderr)
        self.stats["announce_cycles"] += 1
        self.stats["last_announce"] = time.time()
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

    # ------------------------------------------------ readiness
    def readiness(self):
        """What has to be true for the next player to be served, checked
        rather than assumed (spec §3).

        Deliberately traffic-independent: from the message counters alone, a
        healthy node with no players is indistinguishable from a broken one, so
        these are capability checks, not rates. What is NOT here, on purpose:
        whether the mesh can reach this host, and whether anyone read a reply.
        There is no inside view of either, and a health file that implied one
        would be the worst kind of green light."""
        now = time.time()
        interfaces = list(getattr(RNS.Transport, "interfaces", []))
        online = [i for i in interfaces if getattr(i, "online", False)]
        try:
            free = shutil.disk_usage(self.data_dir).free
        except OSError:
            free = None
        health = {
            "t": round(now, 3),
            "boot": self.ledger.boot,
            "uptime": round(now - self.started, 1) if self.started else None,
            "interfaces_total": len(interfaces),
            "interfaces_online": len(online),
            "transport_configured": self._transport_configured(),
            "enable_transport": self._config_flag("enable_transport"),
            "saves_writable": self._saves_writable(),
            "disk_free_bytes": free,
            "log_suppressed": (self.logfilter.stats()
                               if self.logfilter else {}),
            "games": len(self.stories),
            "games_missing_story": sorted(s for s in self.stories
                                          if not self.stories[s].exists()),
            "sessions": len(self.sessions),
            "ledger_unresolved": len(self.ledger.unresolved()),
        }
        health.update(self.stats)
        return health

    def write_health(self):
        """data/health.json, atomically. A failed write never disturbs the
        service it is describing."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.data_dir / "health.json.tmp"
            tmp.write_text(json.dumps(self.readiness(), indent=1))
            os.replace(tmp, self.data_dir / "health.json")
        except OSError as e:
            print(f"jhost: health write failed ({e})", file=sys.stderr)

    def _saves_writable(self):
        probe = self.store.root / ".write-probe"
        try:
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok")
            probe.unlink()
            return True
        except OSError:
            return False

    def _config_lines(self):
        try:
            return (self.data_dir / "rns" / "config").read_text().splitlines()
        except OSError:
            return None

    def _config_flag(self, key):
        lines = self._config_lines()
        if lines is None:
            return None
        for raw in lines:
            name, _, value = raw.split("#")[0].partition("=")
            if name.strip().lower() == key:
                return value.strip().lower() in ("yes", "true", "1", "on")
        return None

    def _transport_configured(self):
        """Is there any interface in the config a stranger could use?

        The scaffolded first-run config is loopback-only on purpose, and says
        so once, to stderr, at startup. Such a node announces, serves its page
        to itself and is invisible to everyone else: healthy process, printed
        addresses, nothing reachable. Nothing else about it looks wrong, which
        is exactly why it is checked here."""
        lines = self._config_lines()
        if lines is None:
            return False
        for _name, opts in self._interface_sections(lines):
            if opts.get("enabled", "yes") != "yes":
                continue
            if opts.get("type", "") == "tcpserverinterface":
                # a loopback-only bind proves nothing about reachability
                if opts.get("listen_ip", "127.0.0.1") not in (
                        "127.0.0.1", "localhost", "::1"):
                    return True
            else:
                # a TCP client link out is a way in as well, and anything else
                # (RNode, Auto, UDP, Serial) is a real wire
                return True
        return False

    @staticmethod
    def _interface_sections(lines):
        """[[Name]] blocks -> [(name, {key: value})]. RNS's own config parser
        handles nested sections; this only reads what health needs, so it
        stays a scanner and not a second parser."""
        sections, current = [], None
        for raw in lines:
            line = raw.split("#")[0].strip()
            if line.startswith("[[") and line.endswith("]]"):
                current = {}
                sections.append((line[2:-2].strip(), current))
            elif line.startswith("["):
                current = None
            elif "=" in line and current is not None:
                key, _, value = line.partition("=")
                current[key.strip().lower()] = value.strip().lower()
        return sections

