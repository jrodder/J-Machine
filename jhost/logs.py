"""RNS log filter: expected uplink churn is loud enough to bury everything else.

Over 30 days the production host wrote 138,629 journal lines, and the families
below account for roughly 73,000 of them: a hub socket closing (11,601), the
reconnection after it (12,663), the reconnect attempt (1,074), the interface
error behind it (1,073), failed connect attempts (33,614) and the outbound
packets that had no interface left to use (13,597). Uplinks going up and down is
the whole reason there are many of them, so those lines are correct and worthless
at the same time, and at that volume nothing else can be found in the journal.

RNS takes any callable as its log destination (RNS/Reticulum.py:244-246), so the
host can own its own lines. The first occurrence of a churn message prints;
repeats of the same message on the same interface are counted inside a window
instead of printed, and the count is reported with the next occurrence and
through health.json. The rate survives, which is the only way to see an uplink
flapping; the flood does not.

RNS switches to permanent console logging if the callback ever raises
(RNS/__init__.py:157-163), so emit() catches everything and falls back to writing
the raw line.
"""
import re
import sys
import threading
import time

# (family, pattern) pairs. The interface named in the message is part of the key,
# so one flapping hub cannot mute a healthy one. Families without an interface
# share one bucket keyed "-". Checked in order: "The socket ... was closed,
# attempting to reconnect" contains the reconnect wording too.
CHURN = (
    ("socket_closed",
     re.compile(r"The socket for TCPInterface\[(?P<iface>[^\]]*)\] was closed")),
    ("reconnected",
     re.compile(r"Reconnected socket for TCPInterface\[(?P<iface>[^\]]*)\]")),
    ("interface_error",
     re.compile(r"An interface error occurred for TCPInterface\[(?P<iface>[^\]]*)\]")),
    ("connect_failed",
     re.compile(r"Connection attempt for TCPInterface\[(?P<iface>[^\]]*)\] failed")),
    ("reconnecting", re.compile(r"Attempting to reconnect")),
    ("no_interface_for_outbound",
     re.compile(r"No interfaces could process the outbound")),
)


class LogFilter:
    """RNS log destination: pass everything through, except repeats of churn."""

    def __init__(self, window=15 * 60, stream=None, clock=time.time):
        self.window = window
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock
        self.lock = threading.Lock()
        # key -> [window started, repeats suppressed in it]
        self.open = {}
        self.suppressed = {}        # family -> lifetime suppressed count

    def emit(self, line):
        """The RNS callback. Never raises."""
        try:
            found = self._classify(line)
            if found is None:
                self._write(line)
                return
            family, iface = found
            summary = None
            now = self.clock()
            with self.lock:
                state = self.open.get((family, iface))
                if state is None or now - state[0] >= self.window:
                    if state is not None and state[1]:
                        summary = self._summary(family, iface, state[1],
                                                now - state[0])
                    self.open[(family, iface)] = [now, 0]
                else:
                    state[1] += 1
                    self.suppressed[family] = self.suppressed.get(family, 0) + 1
                    return
            if summary:
                self._write(summary)
            self._write(line)
        except Exception:
            try:
                self.stream.write(str(line) + "\n")
                self.stream.flush()
            except Exception:
                pass

    def stats(self):
        """Lifetime suppressed counts by family, for health.json."""
        with self.lock:
            return dict(self.suppressed)

    def _classify(self, line):
        for family, pattern in CHURN:
            m = pattern.search(line)
            if m:
                return family, m.groupdict().get("iface") or "-"
        return None

    def _summary(self, family, iface, count, elapsed):
        where = "" if iface == "-" else f" [{iface}]"
        return (f"jhost: log filter: {count} more '{family}' line"
                f"{'s' if count != 1 else ''}{where} in the last "
                f"{int(elapsed // 60)} min")

    def _write(self, text):
        self.stream.write(text + "\n")
        self.stream.flush()
