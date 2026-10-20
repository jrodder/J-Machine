"""Guestbook (spec §6) — pure unit tests. No RNS, no network; the clock is
injected through post(now=...) so no sleeping or utime."""
import json
import tempfile
import unittest
from pathlib import Path

from jhost.guestbook import (ANON_RATE, CAP, KEEP, NOTES, RATE, SHOW, _trim,
                             banned, clean, entries, post, render)

A = "a" * 32
B = "b" * 32


class Fix:
    """Fixture: fresh log + denylist paths per test (the files need not
    exist — absent is the fresh-host state)."""

    def __init__(self):
        self.d = tempfile.TemporaryDirectory()
        self.log = Path(self.d.name) / "guestbook.jsonl"
        self.ban = Path(self.d.name) / "guestbook-banned.txt"

    def close(self):
        self.d.cleanup()

    def post(self, text="hi", author=A, now=1_000_000.0):
        return post(self.log, self.ban, author, text, now=now)


class Clean(unittest.TestCase):
    def test_collapses_to_one_line(self):
        # the page is line-structured: a newline in user text would forge
        # '> name' lines and micron blocks out of nothing
        self.assertEqual(clean("  hi\r\nthere\t\tyou  "), "hi there you")

    def test_escapes_micron(self):
        # backtick is micron's only format/escape char, so escaping it is
        # enough: no restyle, no forged `[label`url] link
        self.assertEqual(clean("```"), "\\`\\`\\`")
        self.assertEqual(clean("[a`b`c]"), "[a\\`b\\`c]")

    def test_cap_before_escaping(self):
        self.assertEqual(len(clean("x" * (CAP + 999))), CAP)
        # the escape backslashes must not eat the player's own characters
        self.assertEqual(clean("x" * CAP + "`").endswith("x"), True)


class Post(unittest.TestCase):
    def setUp(self):
        self.f = Fix()

    def tearDown(self):
        self.f.close()

    def test_empty_is_not_a_post(self):
        for text in ["", "   ", "\n\r\t "]:
            self.assertEqual(self.f.post(text=text), "empty")
        self.assertFalse(self.f.log.exists())

    def test_saved_record(self):
        self.assertEqual(self.f.post(text="hello world", now=500.0), "saved")
        r = json.loads(self.f.log.read_text().strip())
        self.assertEqual(r, {"ts": 500.0, "who": A, "text": "hello world"})
        # who is the destination hash the link proved, and the board's tag is
        # its 6-char prefix — the prefix the player's own client shows them
        self.assertEqual(len(r["who"]), 32)

    def test_anonymous_can_post(self):
        # browsing identifies nobody by default (NomadNet Directory.py:377-382,
        # meshchat has no UI for it): refusing anonymous posts would be a board
        # nobody can use, so they are stored untagged and labelled 'anon'
        self.assertEqual(self.f.post(author=None), "saved")
        self.assertIsNone(json.loads(self.f.log.read_text().strip())["who"])

    def test_ban_is_checked_before_anything_is_written(self):
        self.f.ban.write_text(f"# spammers\n{A.upper()}\n")   # case-insensitive
        self.assertEqual(self.f.post(), "banned")
        self.assertFalse(self.f.log.exists())
        self.assertTrue(banned(self.f.ban, B) is False)

    def test_no_ban_file_is_not_banned(self):
        self.assertFalse(banned(self.f.ban, A))

    def test_an_authorless_post_cannot_be_banned(self):
        # the denylist names hashes; there is nothing verified to match
        self.f.ban.write_text(A + "\n")
        self.assertEqual(self.f.post(author=None), "saved")

    def test_rate_limit_per_identified_player(self):
        self.assertEqual(self.f.post(text="one", now=1000.0), "saved")
        self.assertEqual(self.f.post(text="two", now=1000.0 + RATE - 1), "rate")
        self.assertEqual(self.f.post(text="two", author=B,
                                     now=1000.0 + RATE - 1), "saved")
        self.assertEqual(self.f.post(text="three", now=1000.0 + RATE), "saved")
        self.assertEqual([e["text"] for e in entries(self.f.log)],
                         ["three", "two", "one"])

    def test_rate_limit_does_not_count_rejected_posts(self):
        for _ in range(5):
            self.f.post(text="", now=1000.0)          # all rejected
        self.assertEqual(self.f.post(text="first real line", now=1000.0),
                         "saved")

    def test_anonymous_posts_share_one_global_slot(self):
        # no author to key on, so the throttle is server-wide: keeps a busy
        # board human and a scripted one pointless
        self.assertEqual(self.f.post(author=None, now=1000.0), "saved")
        self.assertEqual(self.f.post(author=None, now=1000.0 + ANON_RATE - 1),
                         "busy")
        self.assertEqual(self.f.post(author=None, now=1000.0 + ANON_RATE),
                         "saved")

    def test_the_anonymous_slot_does_not_gag_identified_players(self):
        self.assertEqual(self.f.post(author=None, now=1000.0), "saved")
        self.assertEqual(self.f.post(text="by hash", author=A, now=1000.0),
                         "saved")

    def test_notes_are_short_and_micron_free(self):
        # notes render inside a `!bold` line on the board
        self.assertEqual(set(NOTES), {"saved", "empty", "banned", "rate",
                                      "busy"})
        for k, v in NOTES.items():
            self.assertTrue(v and "\n" not in v and "`" not in v, k)


class Read(unittest.TestCase):
    def setUp(self):
        self.f = Fix()

    def tearDown(self):
        self.f.close()

    def _write(self, rows):
        self.f.log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_newest_first_and_capped(self):
        self._write([{"ts": i, "who": A, "text": f"l{i}"}
                     for i in range(SHOW + 5)])
        got = entries(self.f.log)
        self.assertEqual([e["text"] for e in got],
                         [f"l{i}" for i in range(SHOW + 4, 4, -1)])

    def test_corrupt_lines_are_skipped_not_fatal(self):
        self._write([{"ts": 1, "who": A, "text": "ok"}])
        self.f.log.write_text("not json\n" + self.f.log.read_text()
                              + json.dumps({"no_text": True}) + "\n")
        self.assertEqual([e["text"] for e in entries(self.f.log)], ["ok"])

    def test_missing_log_is_empty(self):
        self.assertEqual(entries(self.f.log), [])
        self.assertEqual(entries(Path(self.f.d.name) / "nope.jsonl"), [])

    def test_trim_keeps_the_newest(self):
        self._write([{"ts": i, "who": A, "text": f"l{i}"} for i in range(10)])
        _trim(self.f.log, keep=3)
        self.assertEqual([e["text"] for e in entries(self.f.log)],
                         ["l9", "l8", "l7"])
        self.assertFalse(list(self.f.log.parent.glob("*.tmp")))

    def test_trim_is_a_noop_under_the_cap(self):
        self._write([{"ts": i, "who": A, "text": "x"} for i in range(KEEP)])
        _trim(self.f.log)
        self.assertEqual(len(self.f.log.read_text().splitlines()), KEEP)


class Render(unittest.TestCase):
    NOW = 1_000_000.0

    def test_board_shape(self):
        page = render("J", "abcd" * 8,
                      [{"ts": self.NOW - 600, "who": A,
                        "text": "hello \\`there\\`"}], now=self.NOW)
        self.assertIn(">`!🖊️ Guestbook`!", page)
        self.assertIn("`[« games`" + "abcd" * 8 + ":/page/index.mu]", page)
        # one field, one tap: the field's backtick part is its starting VALUE
        # (so it must stay empty), and the link's third component '*' tells the
        # client to send every field it rendered
        self.assertIn("`<48|message`>", page)
        self.assertIn("`[Post it`" + "abcd" * 8 + ":/page/guestbook.mu`*]",
                      page)
        # a verified post is marked with 6 chars of the signed destination
        self.assertIn("`F3a8#" + "a" * 6 + "`f · 10 min ago\n  hello \\`there\\`",
                      page)

    def test_unverified_post_is_labelled_anon(self):
        page = render("J", "abcd" * 8,
                      [{"ts": self.NOW, "who": None, "text": "sup"}],
                      now=self.NOW)
        self.assertIn("`F777anon`f · just now\n  sup", page)
        self.assertNotIn("#None", page)      # never a fake tag

    def test_note_shows_after_a_submit(self):
        # the submit re-renders this page, so the reply has to live on it
        self.assertIn(NOTES["empty"], render("J", "abcd" * 8, [], "empty"))
        self.assertIn(NOTES["busy"], render("J", "abcd" * 8, [], "busy"))
        # an unknown note must not render an empty bold line
        self.assertIn(NOTES["banned"], render("J", "abcd" * 8, [], "bogus"))
        plain = render("J", "abcd" * 8, [], None)
        for v in NOTES.values():
            self.assertNotIn(v, plain)   # a plain view shows no reply line

    def test_empty_board_says_so(self):
        page = render("J", "abcd" * 8, [])
        self.assertIn("No lines yet", page)
        self.assertNotIn(">Last", page)


if __name__ == "__main__":
    unittest.main()
