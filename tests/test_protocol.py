"""Spec §7 tests 6-7: protocol edge-behavior table + save sanity.
No RNS import — milliseconds each."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from jhost.protocol import (FileSaveStore, GameStats, INPUT_CAP, PlayerStats,
                            ago, handle_message, player_stats, render_page,
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

    def test_existing_session_empty_line(self):
        self.assertEqual(self.p.call(text=""), self.p.call(text=""))

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
        # (empty = transcript-only reply), so one non-empty line is fed
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
            self._slot(root, "zork1", a, meta={"turns": 40, "won": True})
            self._slot(root, "zork1", b, age_min=30, meta={"turns": 2})
            self._slot(root, "zork1", c, age_min=90)  # no metadata at all
            self._slot(root, "planetfall", a, age_min=15,
                       meta={"turns": 7, "won": True})
            st = player_stats(root)
            # st_mtime is nanosecond-rounded by the kernel, so the recency
            # timestamp gets a delta, never an ==  comparison
            z = st.per_game["zork1"]
            self.assertEqual((z.explorers, z.won, z.turns), (3, 1, 42))
            self.assertAlmostEqual(z.last_move, self.NOW - 5 * 60, delta=1)
            pf = st.per_game["planetfall"]
            self.assertEqual((pf.explorers, pf.won, pf.turns), (1, 1, 7))
            self.assertAlmostEqual(pf.last_move, self.NOW - 15 * 60, delta=1)
            self.assertEqual(st.total, 3)      # a dedupes across games
            self.assertEqual((st.turns, st.won), (49, 2))

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
            self.assertEqual((g.explorers, g.won, g.turns), (3, 0, 0))

    def test_pruning_is_reflected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = self._slot(root, "zork1", "aa" * 16, meta={"turns": 9,
                                                           "won": True})
            self.assertEqual((player_stats(root).turns,
                              player_stats(root).won), (9, 1))
            p.unlink()   # operator prunes a player
            self.assertEqual((player_stats(root).per_game,
                              player_stats(root).turns), ({}, 0))

    def test_empty_or_missing_dir(self):
        with tempfile.TemporaryDirectory() as d:
            st = player_stats(Path(d) / "nope")
            self.assertEqual((st.per_game, st.total, st.turns, st.won),
                             ({}, 0, 0, 0))

    def test_ago_resolution(self):
        n = 1_000_000.0
        for age, want in [(10, "just now"), (89, "just now"),
                          (91, "1 min ago"), (630, "10 min ago"),
                          (3599, "59 min ago"), (3700, "1 h ago"),
                          (86400 * 25, "25 d ago")]:
            self.assertEqual(ago(n - age, now=n), want, f"age={age}")

    def test_counters_survive_a_host_restart(self):
        """GameState lives in memory; the sidecar is the durable copy, so a
        player's move count and win flag continue where they left off (the
        test_3_reconnect pattern one level down)."""
        p = P()
        try:
            p.call(text="look")          # first contact + 1 turn
            p.call(text="inventory")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 2, "won": False})
            p.sessions.clear()           # host restart
            p.call(text="")              # re-fetch only: not a turn
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 2, "won": False})
            p.call(text="look")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 3, "won": False})
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
                             {"turns": 3, "won": True})
            self.assertEqual(p.call(text="look"), "[Game over]")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 3, "won": True})
        finally:
            p.close()

    def test_rejections_add_no_moves(self):
        p = P()
        try:
            p.call(text="look", verified=False)     # rejected sender
            p.call(text="x" * (INPUT_CAP + 1))      # too long
            p.call(text="")                          # state re-fetch
            self.assertNotEqual(p.call(text="look"), "")
            self.assertEqual(p.store.load_meta(p.game, S1),
                             {"turns": 1, "won": False})
        finally:
            p.close()

    def test_render_page_stats(self):
        page = render_page("J", [("zork1", "<ab:cd:ef>")],
                           PlayerStats({"zork1": GameStats(3, 1, 42, 0.0)},
                                       12, 42, 1))
        self.assertIn("`Fb8342 moves`f · `F3a812 players all time`f · "
                      "`Fd441 win`f", page)
        self.assertIn("> `!zork1`!\n  `[<ab:cd:ef>`lxmf@abcdef]\n"
                      "  `F7773 explorers · 1 finished`f", page)

    def test_render_page_zero_pieces_omitted(self):
        """The odometer never shows a 0 it has no reason to show, and a game
        nobody has started gets no stats line at all."""
        page = render_page("J", [("zork1", "<ab:cd:ef>"),
                                 ("yomama", "<be:ef:00>")],
                           PlayerStats({}, 0, 0, 0))
        self.assertIn("progress is saved per player automatically.\n"
                      "`F3a80 players all time`f\n", page)
        self.assertNotIn("moves", page)
        self.assertNotIn("win", page)
        self.assertEqual(page.count("explorers"), 0)

    def test_operator_display_titles_render_and_parse(self):
        """games/names.txt shows a real title ('Hitchhikers Guide to the
        Galaxy' for h2g2) while the stem stays the identity/save key. A
        multi-word title must still parse as exactly one game: the address
        line under the name is what disambiguates the anchor."""
        stats = PlayerStats({"h2g2": GameStats(2, 0, 9, time.time() - 70)},
                            1, 9, 0)
        page = render_page("J", [("h2g2", "<ab:cd:ef>"), ("zork1", "<11:22:33>")],
                           stats, names={"h2g2": "Hitchhikers Guide to the Galaxy"})
        self.assertIn("> `!Hitchhikers Guide to the Galaxy`!\n"
                      "  `[<ab:cd:ef>`lxmf@abcdef]", page)
        # no names.txt entry -> the stem is the title, unchanged behaviour
        self.assertIn("> `!zork1`!\n  `[<11:22:33>`lxmf@112233]", page)
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
        stats = PlayerStats({"zork1": GameStats(2, 1, 30, lm),
                             "planetfall": GameStats(1, 0, 4, lm)}, 2, 34, 1)
        page = render_page("J-Machine Games",
                           [("zork1", "<ab:cd:ef>"),
                            ("planetfall", "<90:12:34>")], stats)
        self.assertTrue(page.startswith(">`!J-Machine Games`!\n>"))
        # the address line is itself the link: label <hex>, url lxmf@<hex>
        self.assertIn("> `!zork1`!\n  `[<ab:cd:ef>`lxmf@abcdef]\n"
                      "  `F7772 explorers · last move 10 min ago · "
                      "1 finished`f", page)
        # a game nobody has finished omits that piece (the odometer never
        # leads with a zero), and singular/plural stays honest
        self.assertIn("> `!planetfall`!\n  `[<90:12:34>`lxmf@901234]\n"
                      "  `F7771 explorer · last move 10 min ago`f\n", page)
        self.assertIn("`Fb8334 moves`f · `F3a82 players all time`f · "
                      "`Fd441 win`f", page)
        # manual line: only for games in the manuals mapping, in the
        # canonical micron form `[label`<hash>:/file/<name>] (NomadNet
        # Guide), and the url must survive parse_page's regexes untouched
        paged = render_page("J-Machine Games",
                            [("zork1", "<ab:cd:ef>"),
                             ("planetfall", "<90:12:34>")], stats,
                            manuals={"zork1": "ab:/file/zork1.pdf"},
                            guestbook="abcd:/page/guestbook.mu")
        self.assertIn("> `!zork1`!\n  `[<ab:cd:ef>`lxmf@abcdef]\n"
                      "  `F7772 explorers · last move 10 min ago · "
                      "1 finished`f\n"
                      "  `[manual`ab:/file/zork1.pdf]", paged)
        self.assertNotIn("`[manual`", paged.split("`!planetfall`!")[1])
        # guestbook link line, and it must not be readable as a game
        self.assertIn("🖊️ `[Guestbook (post by message)`abcd:/page/guestbook.mu]",
                      paged)
        # ...separated from the game list by a micron rule
        self.assertIn("`abcd:/page/guestbook.mu]\n-\n>Games", paged)
        # parse_page round-trips the address out of the link label (the real
        # parse_page is exercised over the wire in the network suite)
        import re
        # the name anchor carries styling tags (bold here): not part of the
        # name. jclient's parse_page strips micron styling so players still see
        # "zork1" — proved over the wire in the network suite (test 1), since
        # jclient needs RNS and this suite stays stdlib-only
        game_lines = re.findall(r"^> (\S+)\s*$", paged, re.M)
        self.assertEqual(game_lines, ["`!zork1`!", "`!planetfall`!"])
        addrs = re.findall(r"^\s{2}`\[(<[0-9a-fA-F:]+>)`lxmf@[0-9a-fA-F]+\]\s*$",
                           paged, re.M)
        self.assertEqual(addrs, ["<ab:cd:ef>", "<90:12:34>"])
        # full-length address: label keeps the prettyhexrep, url is the
        # delimiters-free 32-char hash
        full = render_page("J", [("zork1",
                                  "<ab:cd:12:34:56:78:9a:bc:de:f0:11:22:33:44:55:66>")],
                           PlayerStats({}, 0, 0, 0))
        self.assertIn("`[<ab:cd:12:34:56:78:9a:bc:de:f0:11:22:33:44:55:66>`"
                      "lxmf@abcd123456789abcdef0112233445566]", full)
        # footer: https:// link to the Android mapper (meshchat opens it in
        # a new tab) — canonical micron form `[label`url], and parse_page's
        # game regexes must not match it
        self.assertIn("Mapping Helper App for Android: `[Droid-Mapper`https://github.com/jrodder/Droid-Mapper]", full)

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
