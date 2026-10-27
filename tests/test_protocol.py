"""Spec §7 tests 6-7: protocol edge-behavior table + save sanity.
No RNS import — milliseconds each."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from jhost.protocol import (FileSaveStore, GameStats, INPUT_CAP, PlayerStats,
                            STATUS, ago, bump_moves, handle_message,
                            player_stats, render_page,
                            write_rns_config)
from tests.conformance.run_conformance import play_session_lines
from tests.util import norm
from zmach.events import Text
from zmach.session import Session

C = Path(__file__).parent / "corpus"
ZORK = C / "zork1.z3"
PF = C / "planetfall.z5"
RISORG = C / "risorg.z8"
SEED = 10
S1 = "aa" * 16  # player "A" delivery hash (hex form)


class P:
    """Fixture: fresh sessions map + FileSaveStore per test."""

    def __init__(self, story=ZORK):
        self.d = tempfile.TemporaryDirectory()
        self.store = FileSaveStore(Path(self.d.name) / "saves")
        self.sessions = {}
        self.story = story
        self.game = Path(story).stem

    def call(self, text="", sender=S1, verified=True, seed=SEED, sign=None):
        return handle_message(self.game, sender, text, verified,
                              self.sessions, self.store, str(self.story),
                              seed, sign=sign)

    def close(self):
        self.d.cleanup()


def text_of(events):
    return "".join(e.data for e in events if isinstance(e, Text))


class Protocol(unittest.TestCase):
    def setUp(self):
        self.p = P()

    def tearDown(self):
        self.p.close()

    def test_unverified_rejected(self):
        r = self.p.call(text="look", verified=False)
        self.assertEqual(r, "[Rejected: unverified sender]")
        self.assertEqual(self.p.sessions, {})

    def test_sign_verb_never_touches_the_game(self):
        # spec §6 board: the verb is intercepted before any session work, so
        # posting neither starts a game nor spends a turn nor counts as a move
        seen = []

        def sign(who, line):
            seen.append((who, line))
            return "signed"

        self.assertEqual(self.p.call(text="sign: hello`!", sign=sign),
                         "signed")
        self.assertEqual(seen, [(S1, "hello`!")])
        self.assertEqual(self.p.sessions, {})
        self.assertEqual(list(Path(self.p.store.root).rglob("*")), [])

    def test_sign_verb_needs_the_signature(self):
        # the tag is only honest if the sender hash was verified
        self.assertEqual(
            self.p.call(text="sign: hi", verified=False,
                        sign=lambda who, line: "x"),
            "[Rejected: unverified sender]")

    def test_sign_verb_takes_the_rest_as_one_line(self):
        got = []

        def sign(who, line):
            got.append(line)
            return "ok"

        self.p.call(text="SIGN: north  and look", sign=sign)
        self.assertEqual(got, ["north  and look"])

    def test_sign_without_colon_is_a_game_move(self):
        r = self.p.call(text="sign", sign=lambda who, line: "must not run")
        self.assertEqual(norm(r),
                         norm(play_session_lines(ZORK, ["sign"], seed=SEED)))

    def test_sign_prefix_is_inert_when_the_board_is_not_wired(self):
        # no sign callable -> the prefix is ordinary player text, never
        # silently swallowed
        r = self.p.call(text="sign: look")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, ["sign: look"],
                                                          seed=SEED)))

    def test_first_contact_intro(self):
        r = self.p.call(text="")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, [], seed=SEED)))

    def test_first_contact_with_line(self):
        r = self.p.call(text="look")
        self.assertEqual(norm(r),
                         norm(play_session_lines(ZORK, ["look"], seed=SEED)))

    def test_first_contact_restore(self):
        # pre-build a 3-turn save, then first contact from that identity
        s = Session()
        s.load(str(ZORK), seed=SEED)
        for l in ["look", "north", "look"]:
            s.input(l)
        img = s.save()
        self.p.store.save(self.p.game, S1, img)
        s2 = Session()
        s2.load(str(ZORK), seed=SEED)  # intro discarded by the protocol
        ref = text_of(s2.restore(img))
        self.assertEqual(norm(self.p.call(text="")), norm(ref))

    def test_first_contact_restore_with_line(self):
        s = Session()
        s.load(str(ZORK), seed=SEED)
        for l in ["look", "north", "look"]:
            s.input(l)
        img = s.save()
        self.p.store.save(self.p.game, S1, img)
        s2 = Session()
        s2.load(str(ZORK), seed=SEED)
        ref = text_of(s2.restore(img)) + text_of(s2.input("south"))
        self.assertEqual(norm(self.p.call(text="south")), norm(ref))

    def test_corrupt_save_fresh_start(self):
        self.p.store.save(self.p.game, S1, b"not-a-save" * 50)
        r = self.p.call(text="")
        self.assertEqual(norm(r), norm(play_session_lines(ZORK, [], seed=SEED)))

    def test_story_unavailable(self):
        # unreadable story file at first contact -> reply, not exception
        r = handle_message("zork1", S1, "", True, self.p.sessions,
                           self.p.store, "/nonexistent/story.z5", SEED)
        self.assertEqual(r, "[Game unavailable]")
        self.assertEqual(self.p.sessions, {})  # nothing stored, sender may retry

    def test_empty_line_gets_status_not_history(self):
        """An empty line never re-sends history (spec §4): first contact gets
        the load/restore batch, a warm session gets the one status line — so
        the reply stays bounded no matter how long the run gets."""
        r0 = self.p.call(text="")          # first contact: the batch
        self.assertIn("open field", norm(r0))
        for line in ["look", "north", "look"]:
            self.p.call(text=line)
        r = self.p.call(text="")
        self.assertEqual(r, STATUS.format(game=self.p.game, turns=3))
        self.assertNotIn("open field", norm(r),
                         "an empty line must not re-send history")

    def test_undecodable_content_is_rejected(self):
        """content_as_string() -> None when the payload is not valid UTF-8.
        That must be a rejection, not the empty-message branch (where it used
        to trigger the largest reply the host can make)."""
        self.assertEqual(self.p.call(text=None),
                         "[Rejected: message is not text]")
        self.assertEqual(self.p.sessions, {})   # no session, no turn

    def test_replies_are_per_turn_deltas(self):
        # Spec §4 (2026-08-25): a reply after first contact carries ONLY
        # the new turn's text — the phone's chat scrollback is the
        # transcript (Sideband renders each LXMF message as a bubble, so a
        # cumulative re-send is O(n^2) text). Regression: turn N+1's reply
        # must not re-send turn N's text.
        r0 = self.p.call(text="")  # first contact: full intro batch
        self.assertIn("open field", norm(r0))
        r1 = self.p.call(text="look")
        self.assertIn("open field", norm(r1))  # the west-of-house turn
        r2 = self.p.call(text="north")
        self.assertIn("North of House", norm(r2))
        self.assertNotIn("open field", norm(r2),
                         "turn 2's reply must not re-send turn 1's text")

    def test_input_cap(self):
        r = self.p.call(text="x" * (INPUT_CAP + 1))
        self.assertEqual(r, "[Input rejected: line too long (>200)]")
        # session intact: the next valid line plays (delta reply)
        s = Session()
        s.load(str(ZORK), seed=SEED)
        ref = text_of(s.input("look"))
        self.assertEqual(norm(self.p.call(text="look")), norm(ref))

    def test_done_session(self):
        p = P(story=PF)
        for l in ["look", "open mailbox", "take leaflet", "north", "east",
                  "south", "west", "look"]:
            p.call(text=l)
        p.call(text="quit")
        p.call(text="y")  # planetfall's quit asks "Do you wish to leave the game? (Y is affirmative):"
        self.assertTrue(p.sessions[("planetfall", S1)].session.done)
        for t in ["", "look", "whatever"]:
            self.assertEqual(p.call(text=t), "[Game over]")
        p.close()

    def test_ingame_save_restore(self):
        # risorg's in-game SAVE/RESTORE verbs -> host-local slot, no prompt.
        # Under seed 10 the intro fills the screen: load parks at the
        # ***MORE*** boundary. The protocol never feeds an empty line
        # (empty = status reply), so one non-empty line is fed
        # to absorb the MORE pause before the first game verb reaches
        # the parser (the VM discards the line at the pause).
        p = P(story=RISORG)
        self.assertIsNone(p.store.load("risorg", S1))
        p.call(text="")
        p.call(text="more")
        self.assertIn("Ok.", p.call(text="save"))
        self.assertIsNotNone(p.store.load("risorg", S1))
        self.assertIn("Ok.", p.call(text="restore"))
        # the slot image round-trips into a fresh Session (spec §7 test 7)
        img = p.store.load("risorg", S1)
        s = Session()
        s.load(str(RISORG), seed=SEED)
        s.restore(img)
        # and the look after restore shows the save-point room — the delta
        # reply is the turn's own text (no preceding prompt), so reference
        # it at Session level: load + "" (absorbed at the ***MORE*** pause,
        # as the wire's "more" line) + look
        s = Session()
        s.load(str(RISORG), seed=SEED)
        s.input("")
        ref_look = text_of(s.input("look"))
        self.assertEqual(norm(p.call(text="look")), norm(ref_look))
        p.close()

    def test_autosave_roundtrip(self):
        p = P()
        p.call(text="look")
        r2 = p.call(text="north")
        img = p.store.load("zork1", S1)
        self.assertIsNotNone(img)
        s = Session()
        s.load(str(ZORK), seed=SEED)
        s.restore(img)
        ref = text_of(s.input("south"))
        r3 = p.call(text="south")
        # reply is the turn's delta -> r3 IS the new turn's text
        self.assertEqual(norm(r3), norm(ref))
        p.close()


class PlayerStatsTests(unittest.TestCase):
    """spec §6 page stats: the autosave slot tree IS the player registry
    (one slot per (game, player), rewritten every turn, never deleted —
    spec §5), so slot count = players who ever started a game and mtime =
    that player's last turn; the sibling <sender>.json adds the move count
    and the win flag. The page reports what is on disk (pruning a slot takes
    its counters with it), so there is no separate ledger to reconcile."""

    NOW = time.time()

    def _slot(self, root, game, sender, age_min=5, meta=None):
        p = root / game / f"{sender}.zmsv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"img")
        if meta is not None:
            p.with_suffix(".json").write_text(json.dumps(meta))
        t = self.NOW - age_min * 60
        os.utime(p, (t, t))
        return p

    def test_counts_dedup_and_recency(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b, c = "aa" * 16, "bb" * 16, "cc" * 16
            self._slot(root, "zork1", a, meta={"turns": 40})
            self._slot(root, "zork1", b, age_min=30, meta={"turns": 2})
            self._slot(root, "zork1", c, age_min=90)  # no metadata at all
            self._slot(root, "planetfall", a, age_min=15,
                       meta={"turns": 7})
            st = player_stats(root)
            # st_mtime is nanosecond-rounded by the kernel, so the recency
            # timestamp gets a delta, never an ==  comparison
            z = st.per_game["zork1"]
            # c has no metadata: a slot is only written after a turn, so it
            # still proves one move rather than zero
            self.assertEqual((z.explorers, z.turns), (3, 43))
            self.assertAlmostEqual(z.last_move, self.NOW - 5 * 60, delta=1)
            pf = st.per_game["planetfall"]
            self.assertEqual((pf.explorers, pf.turns), (1, 7))
            self.assertAlmostEqual(pf.last_move, self.NOW - 15 * 60, delta=1)
            self.assertEqual(st.total, 3)      # a dedupes across games
            self.assertEqual(st.turns, 50)

    def test_unparseable_metadata_reads_as_absent(self):
        """A stray/truncated/hand-edited sidecar must never fail a render
        or zero out the rest of the tree."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._slot(root, "zork1", "aa" * 16).with_suffix(".json") \
                .write_text("{not json")
            self._slot(root, "zork1", "bb" * 16, meta={"turns": "many"})
            self._slot(root, "zork1", "cc" * 16, meta=[1, 2])  # not an object
            st = player_stats(root)
            g = st.per_game["zork1"]
            self.assertEqual((g.explorers, g.turns), (3, 3))

    def test_repeat_players_are_counted_per_slot_and_per_identity(self):
        """Two counts, deliberately not the same number: a game's row counts slots
        in that game, the page's headline line counts identities whose moves across
        the whole tree add to more than one. Someone who poked two games once each
        stayed away from both and came back to the node, so they count once, at the
        node — and not in either game."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._slot(root, "zork1", "aa" * 16, meta={"turns": 9})
            self._slot(root, "zork1", "bb" * 16, meta={"turns": 1})
            self._slot(root, "zork1", "cc" * 16, meta={"turns": 1})
            self._slot(root, "planetfall", "cc" * 16, meta={"turns": 1})
            st = player_stats(root)
            self.assertEqual(st.per_game["zork1"].repeat, 1)      # aa only
            self.assertEqual(st.per_game["planetfall"].repeat, 0)
            self.assertEqual(st.repeat, 2)                        # aa and cc
            self.assertEqual(st.total, 3)

    def test_pruning_is_reflected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = self._slot(root, "zork1", "aa" * 16, meta={"turns": 9})
            self.assertEqual(player_stats(root).turns, 9)
            p.unlink()   # operator prunes a player
            self.assertEqual((player_stats(root).per_game,
                              player_stats(root).turns), ({}, 9))
            # explorers follow the tree; the odometer does not fall back down

    def test_move_odometer_never_shrinks(self):
        """The turn total is the higher of the derived sum and the moves.json
        floor, so lost metadata or a pruned tree cannot walk it backwards."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._slot(root, "zork1", "aa" * 16, meta={"turns": 3})
            self._slot(root, "zork1", "bb" * 16, meta={"turns": 4})
            self.assertEqual(player_stats(root).turns, 7)   # floor seeded here
            for p in root.glob("*/*.json"):
                p.unlink()                                  # metadata lost
            self.assertEqual(player_stats(root).turns, 7)
            bump_moves(root)
            bump_moves(root)
            self.assertEqual(player_stats(root).turns, 9)

    def test_again_restarts_a_finished_run(self):
        """Dying used to end a (game, sender) for good: the end-of-run flag
        rides inside the autosave, so even a host restart replayed it dead.
        `again` rewinds the story and keeps the player's counters."""
        p = P()
        self.addCleanup(p.close)
        for line in ("north", "open window", "east", "look"):
            p.call(line)
        p.call("quit")
        p.call("y")
        self.assertEqual("[Game over]", p.call("look"))
        meta = p.store.load_meta(p.game, S1)
        ledger = (p.store.root / "moves.json").read_text()
        fresh = p.call("again")
        self.assertTrue(fresh.strip() and "Game over" not in fresh)
        kept = p.store.load_meta(p.game, S1)
        self.assertGreaterEqual(kept["turns"], meta["turns"])
        # the verb itself is not a move the story was fed
        self.assertEqual((p.store.root / "moves.json").read_text(), ledger)
        self.assertNotIn("Game over", p.call("look"))
        p.sessions.clear()                       # exactly what a host restart does
        self.assertNotIn("game over", p.call("look").lower())

    def test_empty_or_missing_dir(self):
        with tempfile.TemporaryDirectory() as d:
            st = player_stats(Path(d) / "nope")
            self.assertEqual((st.per_game, st.total, st.turns), ({}, 0, 0))

    def test_ago_resolution(self):
        n = 1_000_000.0
        for age, want in [(10, "just now"), (89, "just now"),
                          (91, "1 min ago"), (630, "10 min ago"),
                          (3599, "59 min ago"), (3700, "1 h ago"),
                          (86400 * 25, "25 d ago")]:
            self.assertEqual(ago(n - age, now=n), want, f"age={age}")

    def test_counters_survive_a_host_restart(self):
        """GameState lives in memory; the sidecar is the durable copy, so a
        player's move count continues where it left off (the test_3_reconnect
        pattern one level down)."""
        p = P()
        try:
            p.call(text="look")          # first contact + 1 turn
            p.call(text="inventory")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 2})
            p.sessions.clear()           # host restart
            p.call(text="")              # reconnect batch: not a turn
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 2})
            p.call(text="look")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 3})
        finally:
            p.close()

    def test_win_latches_and_only_fed_turns_count(self):
        """quit/y reaches the story's end -> the win flag latches (the only
        evidence, since the Z-machine's done flag is not in the save image),
        and the [Game over] reply adds no move."""
        p = P()
        try:
            p.call(text="look")
            p.call(text="quit")
            p.call(text="y")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 3})
            self.assertEqual(p.call(text="look"), "[Game over]")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 3})
        finally:
            p.close()

    def test_rejections_add_no_moves(self):
        p = P()
        try:
            p.call(text="look", verified=False)     # rejected sender
            p.call(text="x" * (INPUT_CAP + 1))      # too long
            p.call(text="")                          # status line, no turn
            self.assertNotEqual(p.call(text="look"), "")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 1})
        finally:
            p.close()

    def test_render_page_stats(self):
        page = render_page("J", [("zork1", "<ab:cd:ef>")],
                           PlayerStats({"zork1": GameStats(3, 42, 0.0)}, 12, 42))
        self.assertIn("`B321`!`Ffb542 moves`f · `F4cb12 players all time`f",
                      page)
        self.assertIn("`Ffc9`!zork1`f`!\n  `F8cf`[<ab:cd:ef>`lxmf@abcdef]`b`f\n"
                      "  `F8c53 explorers`f", page)

    def test_render_page_zero_pieces_omitted(self):
        """The odometer never shows a 0 it has no reason to show, and a game
        nobody has started gets no stats line at all."""
        page = render_page("J", [("zork1", "<ab:cd:ef>"),
                                 ("yomama", "<be:ef:00>")],
                           PlayerStats({}, 0, 0))
        self.assertIn("progress is saved per player automatically.\n"
                      "`B321`!`F4cb0 players all time`f", page)
        self.assertNotIn("moves", page)
        self.assertNotIn("win", page)
        self.assertEqual(page.count("explorers"), 0)

    def test_operator_display_titles_render_and_parse(self):
        """games/names.txt shows a real title ('Hitchhikers Guide to the
        Galaxy' for h2g2) while the stem stays the identity/save key. A
        multi-word title must still parse as exactly one game: the address
        line under the name is what disambiguates the anchor."""
        stats = PlayerStats({"h2g2": GameStats(2, 9, time.time() - 70)}, 1, 9)
        page = render_page("J", [("h2g2", "<ab:cd:ef>"), ("zork1", "<11:22:33>")],
                           stats, names={"h2g2": "Hitchhikers Guide to the Galaxy"})
        self.assertIn("`Ffc9`!Hitchhikers Guide to the Galaxy`f`!\n"
                      "  `F8cf`[<ab:cd:ef>`lxmf@abcdef]", page)
        # no names.txt entry -> the stem is the title, unchanged behaviour
        self.assertIn("`Ffc9`!zork1`f`!\n  `F8cf`[<11:22:33>`lxmf@112233]", page)
        try:
            from jclient.client import parse_page
        except ImportError:
            self.skipTest("jclient needs RNS")
        self.assertEqual(parse_page(page),
                         [("Hitchhikers Guide to the Galaxy", "<ab:cd:ef>"),
                          ("zork1", "<11:22:33>")])


class Helpers(unittest.TestCase):
    def test_file_save_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            st = FileSaveStore(d)
            self.assertIsNone(st.load("g", "s"))
            st.save("g", "s", b"img")
            self.assertEqual(st.load("g", "s"), b"img")
            self.assertFalse(list(Path(d).rglob("*.tmp")))

    def test_render_page(self):
        lm = time.time() - 630  # "10 min ago", robust to a second of drift
        stats = PlayerStats({"zork1": GameStats(2, 30, lm),
                             "planetfall": GameStats(1, 4, lm)}, 2, 34)
        page = render_page("J-Machine Games",
                           [("zork1", "<ab:cd:ef>"),
                            ("planetfall", "<90:12:34>")], stats)
        self.assertTrue(page.startswith(
            "`B631`Ffda`!J-Machine Games`b`f`!\nInfocom-era"))
        # No micron headings anywhere on the page: a bare ">" line is an empty
        # heading1, and theme-aware clients band it with their own colours
        # (meshchat dark heading1 bg = bbb) -> a white/grey bar over the tagline.
        self.assertFalse([l for l in page.split("\n") if l.startswith(">")])
        # the address line is itself the link: label <hex>, url lxmf@<hex>
        self.assertIn("`Ffc9`!zork1`f`!\n  `F8cf`[<ab:cd:ef>`lxmf@abcdef]`b`f\n"
                      "  `F8c52 explorers`f · `F999last move 10 min ago`f", page)
        # a game with no recent play omits that piece (the odometer never leads
        # with a zero), and singular/plural stays honest
        self.assertIn("`Ffc9`!planetfall`f`!\n  `F8cf`[<90:12:34>`lxmf@901234]`b`f\n"
                      "  `F8c51 explorer`f · `F999last move 10 min ago`f\n", page)
        # the odometer also names the newest move anywhere, in which game: the
        # counters alone cannot say whether anyone is here now
        self.assertIn("`B321`!`Ffb534 moves`f · `F4cb2 players all time`f · "
                      "`F999last move 10 min ago`f in `Ffc9zork1`f", page)
        # names are TEXT on the canvas: no `B block behind a name (a filled bar
        # behind 25 names reads as a stack of grey slabs) and no '> ' heading
        # (the parser paints its own #bbb band there)
        for line in page.splitlines():
            if line.startswith("`Ffc9`!"):
                self.assertNotIn("`B", line)
        # no micron headings anywhere either (only the bare '>' spacer lines),
        # so nothing paints the parser's #bbb band
        self.assertEqual([l for l in page.splitlines()
                          if l.startswith(">") and l.strip() != ">"], [])
        # manual line: only for games in the manuals mapping, in the
        # canonical micron form `[label`<hash>:/file/<name>] (NomadNet
        # Guide), and the url must survive parse_page's regexes untouched
        paged = render_page("J-Machine Games",
                            [("zork1", "<ab:cd:ef>"),
                             ("planetfall", "<90:12:34>")], stats,
                            manuals={"zork1": "ab:/file/zork1.pdf"},
                            guestbook="abcd:/page/guestbook.mu")
        self.assertIn("`Ffc9`!zork1`f`!\n  `F8cf`[<ab:cd:ef>`lxmf@abcdef]`b`f\n"
                      "  `F8c52 explorers`f · `F999last move 10 min ago`f\n"
                      "  `Fc9f`[manual`ab:/file/zork1.pdf]`b`f", paged)
        self.assertNotIn("`[manual`", paged.split("`!planetfall`f`!")[1])
        # guestbook link line, and it must not be readable as a game
        self.assertIn("🖊️ `[Guestbook (post by message)`abcd:/page/guestbook.mu]",
                      paged)
        # ...separated from the game list by a micron rule
        self.assertIn("Droid-Mapper]`f\n-\n`B153`Faff`!Games", paged)
        # parse_page round-trips the address out of the link label (the real
        # parse_page is exercised over the wire in the network suite)
        import re
        # the name anchor carries styling tags (bold here): not part of the
        # name. jclient's parse_page strips micron styling so players still see
        # "zork1" — proved over the wire in the network suite (test 1), since
        # jclient needs RNS and this suite stays stdlib-only
        game_lines = re.findall(r"^`Ffc9`!(.+?)`f`!$", paged, re.M)
        self.assertEqual(game_lines, ["zork1", "planetfall"])
        addrs = re.findall(r"^\s{2}`F8cf`\[(<[0-9a-fA-F:]+>)`lxmf@[0-9a-fA-F]+\]",
                           paged, re.M)
        self.assertEqual(addrs, ["<ab:cd:ef>", "<90:12:34>"])
        # full-length address: label keeps the prettyhexrep, url is the
        # delimiters-free 32-char hash
        full = render_page("J", [("zork1",
                                  "<ab:cd:12:34:56:78:9a:bc:de:f0:11:22:33:44:55:66>")],
                           PlayerStats({}, 0, 0, 0))
        self.assertIn("`[<ab:cd:12:34:56:78:9a:bc:de:f0:11:22:33:44:55:66>`"
                      "lxmf@abcd123456789abcdef0112233445566]", full)        # mapper link: it no longer sits at the bottom of a 25-game scroll, it
        # rides the guestbook line to the RIGHT of the board link (meshchat
        # opens https:// targets in a new browser tab)
        gb_line = next(l for l in paged.splitlines() if "Guestbook" in l)
        self.assertIn("`[Droid-Mapper (Android map helper)`"
                      "https://github.com/jrodder/Droid-Mapper]", gb_line)
        self.assertLess(gb_line.index("Guestbook"), gb_line.index("Droid-Mapper"))
        self.assertNotIn("Droid-Mapper", paged.split("`B153`Faff`!Games")[1])

    def test_write_rns_config(self):
        with tempfile.TemporaryDirectory() as d:
            h = write_rns_config(d + "/hostcfg", "host")
            c = write_rns_config(d + "/clientcfg", "client")
            ht, ct = h.read_text(), c.read_text()
            for k in ("type = TCPServerInterface", "listen_port = 4242",
                      "share_instance = yes", "[interfaces]", "[logging]"):
                self.assertIn(k, ht)
            for k in ("type = TCPClientInterface", "target_port = 4242"):
                self.assertIn(k, ct)
            # non-clobbering: second call must not replace the file
            first = h.read_text()
            write_rns_config(d + "/hostcfg", "host")
            self.assertEqual(first, h.read_text())


if __name__ == "__main__":
    unittest.main()
