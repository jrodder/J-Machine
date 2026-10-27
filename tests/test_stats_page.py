"""The activity page (spec §6): the breakdown the main page leaves off. Two
rules are pinned here because both are easy to break by accident — a section
with nothing to say is omitted rather than shown as a zero, and no identity
appears anywhere on a public page."""
import re
import time
import unittest

from jhost import protocol as P

from jhost.protocol import GameStats, PlayerStats, render_stats
from jhost.window import Activity

HASH_32 = re.compile(r"\b[0-9a-f]{32}\b")


def bucket(turns=0, players=0, delivered=0, failed=0):
    """One game's counts for one day, in the shape window.activity produces."""
    return {"turns": turns, "players": players,
            "delivered": delivered, "failed": failed}


def stats(**games):
    per_game = {stem: g for stem, g in games.items()}
    return PlayerStats(per_game=per_game,
                       total=sum(g.explorers for g in per_game.values()),
                       turns=sum(g.turns for g in per_game.values()),
                       won=sum(g.won for g in per_game.values()))


class ActivityPage(unittest.TestCase):
    def test_totals_are_the_headline(self):
        out = render_stats("J", stats(h2g2=GameStats(explorers=2, turns=179,
                                                     last_move=1.0)))
        self.assertIn("179 moves", out)
        self.assertIn("2 players", out)

    def test_a_move_stays_singular(self):
        out = render_stats("J", stats(h2g2=GameStats(explorers=1, turns=1,
                                                     last_move=1.0)))
        self.assertIn("1 move", out)
        self.assertNotIn("1 moves", out)

    def test_games_are_ordered_most_recent_first(self):
        out = render_stats("J", stats(
            old=GameStats(explorers=1, turns=90, last_move=100.0),
            fresh=GameStats(explorers=1, turns=3, last_move=1e12)))
        self.assertLess(out.index("fresh"), out.index("old"),
                        "a game nobody has touched should not lead")

    def test_the_newest_players_move_count_shows_only_when_it_adds_something(self):
        one = render_stats("J", stats(zork1=GameStats(explorers=1, turns=58,
                                                      last_move=1.0)))
        self.assertNotIn("last player", one)
        many = render_stats("J", stats(
            zork1=GameStats(explorers=4, turns=180, last_move=1.0,
                            last_player_turns=58)))
        self.assertIn("last player 58", many)

    def test_an_unplayed_game_is_not_rendered_as_a_zero(self):
        out = render_stats("J", stats(quiet=GameStats()))
        self.assertNotIn("quiet", out)
        self.assertNotIn("0 moves", out)

    def test_the_day_log_shows_quiet_days_as_quiet(self):
        # The point of the durable log over the ledger window: an empty day is a
        # fact that gets printed, not a gap the reader has to interpret.
        now = 1793036879.0
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        yest = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
        out = P._plain(render_stats("J", stats(), now=now, days={
            yest: {"h2g2": bucket(turns=6, players=2, delivered=6)},
            today: {},
        }))
        self.assertIn("6 moves", out)
        self.assertIn("2 players", out)
        self.assertIn("0 moves", out)

    def test_an_unrecorded_day_does_not_claim_nobody_played(self):
        # The ledger only exists from the day it was introduced, so the week
        # before it is unknown, not quiet. Printing "0 moves" there is a lie
        # about the busiest days the node ever had.
        now = 1793036879.0
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        out = P._plain(render_stats("J", stats(), now=now,
                                    days={today: {"h2g2": bucket(turns=3)}}))
        week_ago = P._day_label(time.strftime(
            "%Y-%m-%d", time.localtime(now - 6 * 86400)))
        self.assertIn(week_ago + "  no record", out)
        self.assertEqual(out.count("0 moves"), 0)
        self.assertIn("3 moves", out)

    def test_a_recorded_quiet_day_still_says_zero_moves(self):
        # "no record" is for days the log never saw; a quiet day it saw is a fact.
        now = 1793036879.0
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        yest = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
        out = P._plain(render_stats("J", stats(), now=now,
                                    days={today: {}, yest: {}}))
        rows = {ln.strip().split("  ")[0]: ln for ln in out.splitlines()
                if "moves" in ln or "no record" in ln}
        self.assertIn("0 moves", rows[P._day_label(today)])
        self.assertIn("0 moves", rows[P._day_label(yest)])
        self.assertNotIn("no record", rows[P._day_label(today)])

    def test_the_day_log_is_preferred_over_the_ledger_window(self):
        # Both describe recent days; the durable one is the truth about them, and
        # it never shrinks when the ledger rotates.
        now = 1793036879.0
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        out = P._plain(render_stats(
            "J", stats(), now=now,
            days={day: {"h2g2": bucket(turns=41, players=3)}},
            activity=Activity(per_day={day: {"h2g2": bucket(turns=2)}},
                              outcomes={"delivered": 2}, since=now,
                              until=now)))
        self.assertIn("41 moves", out)
        self.assertNotIn("2 moves", out)

    def test_reply_counts_carry_their_window(self):
        act = Activity(per_day={"2026-10-26": {"h2g2": bucket(2, 1, 2, 1)}},
                       outcomes={"handed_off": 2, "delivered": 2, "failed": 1},
                       in_flight=1, since=1793036879.0,
                       until=1793041392.0)
        out = render_stats("J", stats(), activity=act)
        self.assertIn("2 replies delivered", out)
        self.assertIn("1 unanswered", out)
        self.assertIn("1 in flight", out)
        self.assertIn("since", out)

    def test_a_rotated_ledger_says_its_window_is_partial(self):
        act = Activity(per_day={"2026-10-26": {"h2g2": bucket(1)}},
                       outcomes={"delivered": 1}, since=1.0, until=2.0,
                       truncated=True)
        self.assertIn("most recent turns only",
                      render_stats("J", stats(), activity=act))

    def test_no_activity_reads_as_an_absent_section_not_a_zero(self):
        out = render_stats("J", stats(), activity=Activity())
        self.assertNotIn("Replies", out)
        self.assertNotIn("Recent days", out)

    def test_no_identity_appears_on_the_page(self):
        # the save tree could attribute every number here to a destination
        # hash; a hash is a persistent handle on a person, so the page must not
        # carry one even if a future caller passes something through
        act = Activity(per_day={"2026-10-26": {"h2g2": bucket(2, 1)}},
                       outcomes={"delivered": 2}, since=1.0, until=2.0)
        out = render_stats("J", stats(
            h2g2=GameStats(explorers=2, turns=9, last_move=1.0,
                           last_player_turns=7)), activity=act,
            back="a" * 32 + ":/page/index.mu")
        body = out.replace("a" * 32, "")   # the back link target is not content
        self.assertIsNone(HASH_32.search(body))

    def test_an_unknown_last_player_count_is_omitted_not_zero(self):
        # player_stats always fills this, but a slot whose sidecar is lost has
        # no count to show, and "last player 0 moves" would read as a fact
        out = render_stats("J", stats(
            zork1=GameStats(explorers=3, turns=40, last_move=1.0,
                            last_player_turns=0)))
        self.assertNotIn("last player", out)

    def test_a_game_line_stays_on_one_terminal_row(self):
        # The longest real title on the production node, measured from its
        # games/names.txt. A wrapped row reads as two games, so the row sheds
        # pieces instead of wrapping — and the pieces it keeps are the ones that
        # define the game, not the ones another section already says.
        title = "Sherlock: The Riddle of the Crown Jewels"
        out = render_stats("J", stats(
            long=GameStats(explorers=9, turns=1240, won=4, last_move=1e12,
                           last_player_turns=58)), names={"long": title},
            now=1e12)
        row = [ln for ln in out.splitlines() if title in ln][0]
        plain = P._plain(row)
        self.assertLessEqual(len(plain), P.ROW_WIDTH)
        self.assertIn("1,240 moves", plain)
        self.assertIn("9 players", plain)
        self.assertNotIn("0 d ago", plain)

    def test_a_short_title_keeps_every_piece(self):
        out = render_stats("J", stats(
            h2g2=GameStats(explorers=2, turns=179, won=1, last_move=1e12,
                           last_player_turns=2)), names={"h2g2": "h2g2"},
            now=1e12)
        row = [ln for ln in out.splitlines() if "h2g2" in ln][0]
        plain = P._plain(row)
        for piece in ("179 moves", "2 players", "1 finished",
                      "last player 2"):
            self.assertIn(piece, plain)

    def test_a_long_real_title_still_shows_the_newest_players_count(self):
        # Seen on the live node: "Hitchhikers Guide to the Galaxy" needed 93
        # columns with full wording, so the shedding rule dropped the count and
        # the one game the operator plays reported less than its short-named
        # neighbours. The pieces get trimmed before the information does.
        title = "Hitchhikers Guide to the Galaxy"
        out = render_stats("J", stats(h2g2=GameStats(explorers=2, turns=179,
                                                    last_move=1e12,
                                                    last_player_turns=2)),
                           names={"h2g2": title}, now=1e12)
        plain = P._plain([ln for ln in out.splitlines() if title in ln][0])
        self.assertLessEqual(len(plain), P.ROW_WIDTH)
        self.assertIn("last player 2", plain)

    def test_a_fault_is_red_even_next_to_a_fresh_verification(self):
        # A story file that vanished is a fact; one successful round trip is
        # evidence about one message. The fact wins.
        out = P.service_status(["story file missing: zork3"],
                               verified_at=1e12, now=1e12)
        self.assertIn(P.ORB_DOWN["body"], out)
        self.assertNotIn(P.ORB_OK["body"], out)
        self.assertIn("story file missing: zork3", out)
        # the colour is the state; spelling it out would be a second source of
        # truth for one fact, and a word is what a reader anchors on anyway
        self.assertNotIn("DOWN", out)

    def test_a_verified_round_trip_is_green_and_says_when(self):
        out = P.service_status([], verified_at=1e12 - 7200, now=1e12)
        self.assertIn(P.ORB_OK["body"], out)
        self.assertNotIn("OK", out)
        self.assertIn("verified 2 h ago", out)

    def test_an_unchecked_node_is_not_called_healthy(self):
        # The two-colour version of this indicator would have to say OK here,
        # which is how a monitoring page starts lying: nothing is wrong and
        # nobody has looked.
        out = P.service_status([], verified_at=None, now=1e12)
        self.assertIn(P.ORB_UNCHECKED["body"], out)
        self.assertNotIn(P.ORB_OK["body"], out)
        self.assertNotIn(P.ORB_DOWN["body"], out)
        self.assertNotIn("UNCHECKED", out)
        # nothing to say beside it: age is not a thing an unchecked node has
        self.assertEqual(len(out.splitlines()), 3)

    def test_age_is_printed_and_never_coloured_as_a_fault(self):
        # Staleness is information, not a threshold to exceed: a check from a
        # week ago is still a check that succeeded.
        out = P.service_status([], verified_at=1e12 - 86400 * 7, now=1e12)
        self.assertIn(P.ORB_OK["body"], out)
        self.assertIn("verified 7 d ago", out)

    def test_the_orb_is_the_same_shape_whichever_way_it_points(self):
        for faults, verified in ((["bad"], None), ([], 1e12), ([], None)):
            plain = P._plain(P.service_status(faults, verified, 1e12))
            rows = [r for r in plain.splitlines() if r]
            self.assertEqual(rows[0], P.ORB_TOP, faults or verified)
            self.assertEqual(rows[-1], P.ORB_BOT, faults or verified)

    def test_the_status_button_is_the_last_thing_on_the_page(self):
        out = render_stats("J", stats(),
                           service={"faults": [], "verified_at": 1e12},
                           back="a" * 32 + ":/page/index.mu", now=1e12)
        plain = [P._plain(l) for l in out.splitlines() if P._plain(l) != "-"]
        self.assertEqual(plain[-1], P.ORB_BOT)
        self.assertIn(P.ORB_TOP, plain)

    def test_the_status_line_carries_no_identity(self):
        out = render_stats("J", stats(),
                           service={"faults": [], "verified_at": 1e12},
                           back="a" * 32 + ":/page/index.mu", now=1e12)
        body = out.replace("a" * 32, "")
        self.assertIsNone(HASH_32.search(body))


    def test_the_back_link_is_the_way_home(self):
        out = render_stats("J", stats(), back="deadbeef:/page/index.mu")
        self.assertIn("deadbeef:/page/index.mu", out)
        self.assertIn("Back to the game list", out)


if __name__ == "__main__":
    unittest.main()
