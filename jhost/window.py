"""Ledger window rollup (spec §6, stats page) — stdlib only, no RNS.

The stats page wants "moves today" and "what happened to the replies", and the
only place a turn carries a timestamp is the ledger — which is size-rotated on
purpose, because a record of what the server did must not itself become the disk
fault it is recording (ledger.py). So this reads what is actually there, reports
how far back that reaches, and never implies it covers more.

It runs on every stats-page request, so it reads at most `max_bytes` from the end,
newest file first, and says so when that leaves records unread; the default is the
full rotated pair, because the ledger bounds itself. It must never raise: a stats page that fails to render is indistinguishable from a node
that is not answering, which is the exact confusion the rest of this subsystem
exists to remove. What it could read is what it returns.

Day buckets are calendar days in the host's local time. "Yesterday" on the page
should mean the host operator's yesterday, not UTC's.
"""
import json
import time
from pathlib import Path

DEFAULT_MAX_BYTES = 1 << 21      # 2 MB: the whole ledger as designed
# (ledger.DEFAULT_MAX_BYTES 1 MB live + one 1 MB .1 generation). The cap is
# a guard against the ledger outgrowing its own bound, not the normal cost
# of a page view — a smaller one would leave 'Recent days' credibly showing
# a few hours while reading like a week.

OPEN = "open"
# Not an outcome: the reply is on its way. A turn whose newest record is
# handed_off has no answer yet, so it is in flight, not delivered.
INTERMEDIATE = ("handed_off",)


class Activity:
    """What one read of the ledger covers. `since`/`until` are the timestamps of
    the first and last record actually read, which is how the page labels its own
    window; `truncated` says the byte cap left older records unread."""

    def __init__(self, per_day=None, outcomes=None, in_flight=0, since=None,
                 until=None, truncated=False):
        self.per_day = per_day if per_day is not None else {}
        self.outcomes = outcomes if outcomes is not None else {}
        self.in_flight = in_flight
        self.since = since
        self.until = until
        self.truncated = truncated

    def recent(self, days, now=None):
        """Turns fed in the last N calendar days (host-local), 1 = today."""
        now = time.time() if now is None else now
        cutoff = time.strftime(
            "%Y-%m-%d", time.localtime(now - 86400 * max(days - 1, 0)))
        return sum(sum(games.values())
                   for day, games in self.per_day.items() if day >= cutoff)

    def __bool__(self):
        return self.since is not None


def _read_tail(path, budget):
    """(bytes, whole_file_read). Only the tail is read; a partial first line is
    dropped by the caller, which is why whole is reported separately."""
    try:
        size = path.stat().st_size
        if size == 0:
            return b"", True
        take = min(size, budget)
        with open(path, "rb") as f:
            f.seek(size - take)
            return f.read(), take >= size
    except OSError:
        return b"", True


def activity(paths, now=None, max_bytes=DEFAULT_MAX_BYTES):
    """Roll up `paths` (ledger files, oldest first) into an Activity. Missing
    files are the fresh-host state; unreadable lines are skipped."""
    per_day, outcomes, turn_keys, last_state = {}, {}, set(), {}
    since = until = None
    truncated = False
    chunks, used = [], 0
    for path in reversed([Path(p) for p in paths]):
        if used >= max_bytes:
            truncated = True
            break
        data, whole = _read_tail(path, max_bytes - used)
        if data:
            if not whole:
                truncated = True
                # seek() can land mid-line; the leading fragment is not a record
                nl = data.find(b"\n")
                data = data[nl + 1:] if nl >= 0 else b""
            chunks.append(data)
            used += len(data)
    for raw in b"".join(reversed(chunks)).splitlines():
        try:
            rec = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        state = rec.get("state")
        stamp = rec.get("t")
        if not state or not isinstance(stamp, (int, float)):
            continue
        since = stamp if since is None else min(since, stamp)
        until = stamp if until is None else max(until, stamp)
        key = (rec.get("b"), rec.get("s"))
        if state == OPEN:
            day = time.strftime("%Y-%m-%d", time.localtime(stamp))
            per_day.setdefault(day, {})
            game = rec.get("game") or "?"
            per_day[day][game] = per_day[day].get(game, 0) + 1
            turn_keys.add(key)
        else:
            outcomes[state] = outcomes.get(state, 0) + 1
            prev = last_state.get(key)
            # newest record for the turn wins: handed_off then delivered reads
            # as delivered, which is the network's answer arriving after ours
            if prev is None or stamp >= prev[0]:
                last_state[key] = (stamp, state)
    in_flight = sum(1 for key in turn_keys
                    if key not in last_state
                    or last_state[key][1] in INTERMEDIATE)
    return Activity(per_day=per_day, outcomes=outcomes, in_flight=in_flight,
                    since=since, until=until, truncated=truncated)
