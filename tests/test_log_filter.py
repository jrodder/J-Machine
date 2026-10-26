"""Journal noise (spec §5): 59,000 of the 138,629 journal lines on the
production host were hub reconnect churn. Those lines are expected — many
uplinks exist precisely because they come and go — and they made the journal
unreadable for everything else."""
import io
import tempfile
import unittest
from pathlib import Path

from jhost.host import Host
from jhost.logs import LogFilter

SOCKET = "[2026-10-26 17:11:53] [Warning]  The socket for TCPInterface[Hub: Qortal] was closed, attempting to reconnect..."
RECONN = "[2026-10-26 17:11:54] [Info]     Reconnected socket for TCPInterface[Hub: Qortal]."
SOCKET_OTHER = SOCKET.replace("Qortal", "Istanbul")
BARE = "[2026-10-26 17:11:55] [Warning]  Attempting to reconnect..."
REAL = "[2026-10-26 17:11:56] [Error]    An unrecoverable error occurred on the interface"


class Stream(io.StringIO):
    """Collects lines; StringIO keeps flush() honest."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def write(self, s):
        self.lines.append(s.rstrip("\n"))
        return len(s)


class Broken:
    def write(self, s):
        raise OSError("log stream closed")

    def flush(self):
        raise OSError("log stream closed")


def clock():
    t = [1_700_000_000.0]

    def now():
        return t[0]

    def advance(seconds):
        t[0] += seconds

    now.advance = advance
    return now


class Churn(unittest.TestCase):
    def setUp(self):
        self.out = Stream()
        self.now = clock()
        self.f = LogFilter(stream=self.out, clock=self.now)

    def test_the_first_occurrence_always_prints(self):
        self.f.emit(SOCKET)
        self.assertEqual(self.out.lines, [SOCKET])

    def test_repeats_inside_the_window_are_counted_not_printed(self):
        self.f.emit(SOCKET)
        for _ in range(99):
            self.f.emit(SOCKET)
        self.assertEqual(self.out.lines, [SOCKET])
        self.assertEqual(self.f.stats(), {"socket_closed": 99})

    def test_the_next_window_reports_what_it_held(self):
        self.f.emit(SOCKET)
        for _ in range(4):
            self.f.emit(SOCKET)
        self.now.advance(16 * 60)
        self.f.emit(SOCKET)
        self.assertIn("4 more 'socket_closed' line", self.out.lines[1])
        self.assertIn("[Hub: Qortal]", self.out.lines[1])
        self.assertEqual(self.out.lines[2], SOCKET)

    def test_one_flapping_hub_does_not_mute_another(self):
        self.f.emit(SOCKET)
        self.f.emit(SOCKET)
        self.f.emit(SOCKET_OTHER)
        self.assertEqual(self.out.lines, [SOCKET, SOCKET_OTHER])

    def test_each_family_is_counted_apart(self):
        for line in (SOCKET, RECONN, BARE):
            self.f.emit(line)
            self.f.emit(line)
        self.assertEqual(self.f.stats(),
                         {"socket_closed": 1, "reconnected": 1,
                          "reconnecting": 1})

    def test_the_reconnect_wording_in_a_socket_line_is_not_its_own_family(self):
        self.f.emit(SOCKET)   # contains "attempting to reconnect"
        self.f.emit(SOCKET)
        self.assertEqual(self.f.stats(), {"socket_closed": 1})

    def test_everything_else_passes_through_untouched(self):
        for line in (REAL, "jhost: announcing page + games", "no level tag"):
            self.f.emit(line)
        self.assertEqual(self.out.lines,
                         [REAL, "jhost: announcing page + games",
                          "no level tag"])
        self.assertEqual(self.f.stats(), {})

    def test_a_failing_log_stream_never_reaches_rns(self):
        # RNS treats a raising callback as fatal to callback logging and dumps
        # everything to the console forever (RNS/__init__.py:157-163).
        f = LogFilter(stream=Broken(), clock=self.now)
        f.emit(SOCKET)
        f.emit(SOCKET)
        f.emit(REAL)


class HealthVisibility(unittest.TestCase):
    """Quiet must not mean blind: the counts reach the health snapshot."""

    def test_suppressed_counts_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            games = root / "games"
            games.mkdir()
            h = Host(root / "data", games)
            out = Stream()
            h.logfilter = LogFilter(stream=out, clock=clock())
            h.logfilter.emit(SOCKET)
            for _ in range(3):
                h.logfilter.emit(SOCKET)
            self.assertEqual(h.readiness()["log_suppressed"],
                             {"socket_closed": 3})


if __name__ == "__main__":
    unittest.main()
