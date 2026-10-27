"""The permanent day log: what the ledger remembers until it forgets.

The save tree already keeps every forever number — players, moves, finishes —
because a slot file is never deleted. What it cannot keep is *when*: which day a
game was played, how many players that day, how many replies landed. That lives
only in `ledger.jsonl`, which is size-rotated, so a month from now the answer to
"was last Tuesday busy" is gone. This module keeps it, in a file small enough to
keep forever (a year of days is a few dozen kilobytes).

Two rules, both about never losing what was already learned:

1. **Merging only raises numbers.** A page read looks at a window, not the whole
   ledger, so a later read can see *less* than an earlier one. If a merge could
   lower a count, a quiet afternoon would quietly rewrite history.
2. **A day gets a row even when it was empty.** Otherwise "nothing happened
   today" and "we stopped recording" look identical, which is the failure this
   whole layer exists to make visible.

It is written by the host because it is bookkeeping about the host's own files,
not a judgement about whether the host is working — that stays outside the
process, where a lie is impossible.
"""
import json
import os
import time

DAYS_FILE = "days.jsonl"
# a torn last line is survivable (the atomic write means it should not happen,
# but the page reads this and must never fail because of it)
KINDS = ("turns", "players", "delivered", "failed")


def read_days(path):
    """{day: {game: {...}, ...}} for whatever is readable. Never raises: the
    activity page renders from this, and a half-written file is not a reason to
    show a player an error."""
    days = {}
    try:
        with open(os.fspath(path), "r") as fh:
            lines = fh.readlines()
    except OSError:
        return days
    for line in lines:
        try:
            row = json.loads(line)
            day, games = row["day"], row["games"]
            if isinstance(day, str) and isinstance(games, dict):
                days[day] = games
        except (ValueError, KeyError, TypeError):
            continue
    return days


def write_days(path, days):
    """Whole-file write, atomic, with the previous version kept.

    The file is tiny and it is the only copy of the day-by-day record, so a bad
    write is worth one extra rename to be safe about.
    """
    path = os.fspath(path)      # callers pass a Path, the writes want a str
    tmp = path + ".tmp"
    prev = path + ".prev"
    if os.path.exists(path):
        try:
            os.replace(path, prev)
        except OSError:
            pass
    with open(tmp, "w") as fh:
        for day in sorted(days):
            fh.write(json.dumps({"day": day, "games": days[day]}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(days)


def merge(days, activity, today=None):
    """Fold one window read into `days`, in place, and return it.

    Counts take the maximum rather than the new value, per game, per day. The
    totals in each day are recomputed from its games afterwards, so they cannot
    drift from the rows they summarise."""
    games_by_day = getattr(activity, "per_day", None) or {}
    for day, games in games_by_day.items():
        seen = days.setdefault(day, {})
        for game, counts in games.items():
            row = seen.setdefault(game, {})
            for kind in KINDS:
                new = counts.get(kind) if isinstance(counts, dict) else counts
                if isinstance(new, int):
                    row[kind] = max(row.get(kind, 0), new)
    if today is None:
        today = time.strftime("%Y-%m-%d", time.localtime())
    days.setdefault(today, {})
    return days


def totals(days, day):
    """(turns, players, delivered, failed) for one day, summed across games.

    Players are summed per game, so one person playing two games counts twice —
    same as the per-game rows. The all-time player count stays the save tree's,
    where one identity is one person."""
    row = days.get(day, {})
    out = {k: sum(g.get(k, 0) for g in row.values()) for k in KINDS}
    return out


def recent(days, count, now=None):
    """The last `count` calendar days, oldest first, today included. Days with no
    row are still returned with zeros, because a gap in the list would read as a
    missing fact rather than a quiet day."""
    now = time.time() if now is None else now
    out = []
    for back in range(count - 1, -1, -1):
        day = time.strftime("%Y-%m-%d",
                            time.localtime(now - 86400 * back))
        t = totals(days, day)
        t["day"] = day
        # A zero from a recorded quiet day and a zero because the log has never
        # seen that day are different facts, and only one of them is a fact.
        # The ledger itself only exists from the day it was introduced, so the
        # days before it are not quiet — they are unknown.
        t["recorded"] = day in days
        out.append(t)
    return out


def newest(days):
    return max(days) if days else None
