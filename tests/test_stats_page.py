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
                       won=sum(g.won for g in per_game.values()),
                       repeat=sum(g.repeat for g in per_game.values()))


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

    def test_a_game_row_says_how_many_players_stayed(self):
        # The fact belongs next to the moves and players it qualifies: it is what
        # separates 180 moves from a crowd that kept playing from 180 moves by 4
        # people who each typed once.
        out = P._plain(render_stats("J", stats(
            zork1=GameStats(explorers=4, turns=180, last_move=1.0, repeat=2))))
        self.assertIn("2 played more than once", out)

    def test_the_node_line_says_how_many_stayed_beside_the_totals(self):
        # A line under the headline, not a piece inside it: the totals count
        # everyone who ever sent a command, which is a public node working as
        # intended, and this sits alongside that rather than correcting it.
        out = P._plain(render_stats("J", stats(
            zork1=GameStats(explorers=4, turns=180, last_move=1.0, repeat=2),
            lh=GameStats(explorers=1, turns=58, last_move=1.0, repeat=1))))
        self.assertIn("3 of those 5 sent more than one move", out)
        headline = [ln for ln in out.splitlines() if "5 players" in ln][0]
        self.assertNotIn("more than one move", headline)

    def test_a_node_nobody_came_back_to_says_nothing(self):
        out = P._plain(render_stats("J", stats(
            zork1=GameStats(explorers=4, turns=4, last_move=1.0, repeat=0))))
        self.assertNotIn("more than one move", out)

    def test_a_game_row_distinguishes_a_solo_run_from_a_crowd(self):
        solo = P._plain(render_stats("J", stats(
            lh=GameStats(explorers=1, turns=58, last_move=1.0, repeat=1))))
        crowd = P._plain(render_stats("J", stats(
            pf=GameStats(explorers=24, turns=24, last_move=1.0))))
        self.assertIn("58 moves · 1 player", solo)
        # a single player with 58 moves has obviously stayed; restating it would
        # teach the reader to skip the piece when it does carry news
        self.assertNotIn("more than once", solo)
        self.assertIn("24 moves · 24 players", crowd)
        self.assertNotIn("more than once", crowd)

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
                           repeat=2)), activity=act,
            back="a" * 32 + ":/page/index.mu")
        body = out.replace("a" * 32, "")   # the back link target is not content
        self.assertIsNone(HASH_32.search(body))

    def test_a_game_nobody_came_back_to_does_not_claim_a_zero(self):
        out = render_stats("J", stats(
            zork1=GameStats(explorers=3, turns=40, last_move=1.0, repeat=0)))
        self.assertNotIn("0 played", P._plain(out))
        self.assertNotIn("more than once", P._plain(out))

    def test_a_long_title_costs_nothing_because_nothing_is_dropped(self):
        # This page is read, not tapped, and clients wrap it. Rows used to shed
        # pieces to fit one screen row, which cost the operator's own game its
        # statistics purely because "Hitchhikers Guide to the Galaxy" is long.
        title = "Hitchhikers Guide to the Galaxy"
        out = render_stats("J", stats(h2g2=GameStats(explorers=2, turns=179,
                                                    won=1, last_move=1e12,
                                                    repeat=2)),
                           names={"h2g2": title}, now=1e12)
        plain = P._plain([ln for ln in out.splitlines() if title in ln][0])
        for piece in ("179 moves", "2 players", "2 played more than once",
                      "1 finished", "last played"):
            self.assertIn(piece, plain)

    def test_recency_says_what_it_is_a_recency_of(self):
        out = P._plain(render_stats("J", stats(
            zork1=GameStats(explorers=2, turns=9, last_move=1e12)), now=1e12))
        self.assertIn("last played just now", out)

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
