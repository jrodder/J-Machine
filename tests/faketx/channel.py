"""Fake network transport (plan Task 13): drives a Session the way a
chunked network channel would.

- send_input(line): the line bytes are delivered in fragments of at most
  `chunk` bytes with fragment boundaries NOT line-aligned; the channel
  reassembles the line (at the newline) before Session.input sees it.
- drain(): Text events are delivered in arbitrary <=`chunk`-byte splits;
  drain() reassembles them into the transcript.

The plan's delay(turns) hook is skipped: in this model an "away" player is
just a Session holding a blocked VM in memory — state persistence is
trivial and untestable beyond what the session already guarantees.
"""
from zmach.events import Text
from zmach.session import Session


class FakeChannel:
    def __init__(self, chunk=512):
        self.chunk = max(1, int(chunk))
        self.session = None
        self._recv = b""      # inbound bytes not yet forming a full line
        self._out = []        # outbound text byte chunks in flight

    def load(self, path, seed=None):
        self.session = Session()
        self._emit(self.session.load(str(path), seed=seed))
        return self

    def _emit(self, events):
        for e in events:
            if isinstance(e, Text):
                data = e.data.encode("utf-8")
                for i in range(0, len(data), self.chunk):
                    self._out.append(data[i:i + self.chunk])
        return events

    def drain(self):
        out = b"".join(self._out).decode("utf-8")
        self._out = []
        return out

    def send_input(self, line):
        if self.session.done:
            return
        data = (line + "\n").encode("utf-8")
        for i in range(0, len(data), self.chunk):
            self._recv += data[i:i + self.chunk]
            j = self._recv.find(b"\n")
            if j >= 0:
                # \n is the last byte of the line, so everything before it
                # is a complete line -> always decodable as utf-8.
                self._emit(self.session.input(self._recv[:j].decode("utf-8")))
                self._recv = self._recv[j + 1:]
                break