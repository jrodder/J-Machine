"""Per-message outcome ledger (spec §3) — stdlib only, no RNS.

One durable line per state change of every inbound player message. Written this
way because "the player sent a turn and nothing came back" currently has no
local trace at all: LXMF swallows an exception from the delivery handler with a
single log line, a reply can be abandoned after its turn was already applied,
and a reply that never finishes produces nothing. An absence is not readable;
a record with no terminal line is.

Two lines per message: `open`, written before any work, then at least one
terminal line. A sequence opened without a terminal line is either a crash
mid-turn or a reply still in flight — which is the whole reason the open line
is written first. Ledger failure never fails a turn.

Rotation is by size with one generation kept: the record of what the server did
must not itself become the disk fault it is recording.
"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

STATE_OPEN = "open"
DEFAULT_MAX_BYTES = 1 << 20    # 1 MB, ~4k turns of two lines


class Ledger:
    def __init__(self, path, boot=None, max_bytes=DEFAULT_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        # restarts reuse sequence numbers, so a record is (boot, seq)
        self.boot = boot or uuid.uuid4().hex[:8]
        self._seq = 0
        self._lock = threading.Lock()

    def open(self, game, player):
        """Before any work on the message. Returns the sequence to close."""
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._write({"s": seq, "b": self.boot, "t": _now(), "game": game,
                     "player": player, "state": STATE_OPEN})
        return seq

    def close(self, seq, state, **fields):
        """A terminal outcome for a sequence. A second one (handed_off then
        delivered, failed then propagated) is wanted: it is the network's
        answer arriving after ours."""
        rec = {"s": seq, "b": self.boot, "t": _now(), "state": state}
        rec.update({k: v for k, v in fields.items() if v is not None})
        self._write(rec)

    def unresolved(self):
        """Sequences opened and never closed in the current generation (a
        rotation ends the trail — that is the trade for bounded disk)."""
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        open_, done = {}, set()
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            key = (r.get("b"), r.get("s"))
            if r.get("state") == STATE_OPEN:
                open_[key] = r
            else:
                done.add(key)
        return [r for key, r in open_.items() if key not in done]

    def _write(self, rec):
        try:
            with self._lock:
                if self.max_bytes and self.path.exists() \
                        and self.path.stat().st_size > self.max_bytes:
                    os.replace(self.path, self.path.with_name(
                        self.path.name + ".1"))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError as e:
            # the turn matters more than the record of the turn
            print(f"jhost: ledger write failed ({e}); continuing unrecorded",
                  file=sys.stderr)


def _now():
    return round(time.time(), 3)
