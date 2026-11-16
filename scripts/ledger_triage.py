#!/usr/bin/env python3
"""Read the outcome ledger and say what each unanswered turn actually was.

The activity page prints only the replies that landed, so a lost one leaves no
mark a human can see except this file. Nothing else names a cause, so this is
where to look. It prints one block per unanswered turn: the states it went
through with the seconds between them, the `why` the host recorded, the reply
size, and LXMF's attempt count, plus whether the same player was ever answered
again. Counted per turn, not per ledger line — one lost reply can write several
lines. Run it against a running or retired data dir:

    scripts/ledger_triage.py /path/to/jhost/data
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ANSWERED = ("delivered", "propagated")
IN_FLIGHT = ("handed_off",)


def read(data_dir):
    """Ledger records, rotated generation first, missing files are fine."""
    base = Path(data_dir)
    out = []
    for name in ("ledger.jsonl.1", "ledger.jsonl"):
        try:
            for line in (base / name).read_text().splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("state"):
                    out.append(rec)
        except OSError:
            continue
    return out


def group(records):
    """{(boot, seq): {"open": rec, "states": [(t, state, rec)]}} by turn."""
    turns = defaultdict(lambda: {"open": None, "states": []})
    for rec in records:
        key = (rec.get("b"), rec.get("s"))
        turn = turns[key]
        if rec["state"] == "open":
            turn["open"] = rec
        else:
            turn["states"].append((rec.get("t", 0.0), rec["state"], rec))
    for turn in turns.values():
        turn["states"].sort()
    return turns


def final(turn):
    return turn["states"][-1][1] if turn["states"] else None


def detail(rec):
    """The fields a terminal line carries, in the order that explains a loss."""
    bits = []
    for field in ("why", "attempts", "bytes_in", "bytes_out"):
        if rec.get(field) is not None:
            bits.append(f"{field}={rec[field]}")
    return " ".join(bits)


def main(data_dir):
    records = read(data_dir)
    if not records:
        print(f"no readable ledger records in {data_dir}")
        return 1
    turns = group(records)
    # a player is a 6-char destination prefix (jhost/guestbook.py TAG): enough to
    # tell three people from one person, not enough to name anyone
    # per player, the times of the turns that were answered, so a later loss can
    # be read as "came back" or "gone" — the counts alone cannot tell those apart
    answered_at = defaultdict(list)
    for turn in turns.values():
        who = (turn["open"] or {}).get("player")
        if who and final(turn) in ANSWERED:
            stamp = (turn["open"] or {}).get("t", 0.0)
            answered_at[who].append(stamp)

    lost = [(key, turn) for key, turn in turns.items()
            if final(turn) not in ANSWERED + IN_FLIGHT]
    lost.sort(key=lambda kt: (kt[1]["open"] or {}).get("t", 0.0))
    print(f"{len(turns)} turns read, {len(lost)} unanswered\n")

    for key, turn in lost:
        opened = turn["open"]
        first_t = (opened or turn["states"][0][2]).get("t", 0.0)
        day = time.strftime("%d %b %H:%M:%S", time.localtime(first_t))
        who = (opened or {}).get("player", "?")
        game = (opened or {}).get("game", "?")
        later = [t for t in answered_at.get(who, []) if t > first_t]
        earlier = [t for t in answered_at.get(who, []) if t <= first_t]
        again = (f"answered again later x{len(later)}" if later
                 else (f"answered only before this x{len(earlier)}" if earlier
                       else "never answered"))
        print(f"{day}  {game}  {who}  [{again}]")
        if opened is None:
            print("    turn opened before this window; states only")
        for stamp, state, rec in turn["states"]:
            gap = f"+{stamp - first_t:6.1f}s" if first_t else "   ---- "
            print(f"    {gap}  {state:<12} {detail(rec)}")
        print()

    by_player = defaultdict(list)
    for (_b, _s), turn in lost:
        by_player[(turn["open"] or {}).get("player", "?")].append(
            final(turn))
    if len(by_player) < len(lost):
        shared = {w: v for w, v in by_player.items() if len(v) > 1}
        print("same player lost more than once: "
              + ", ".join(f"{w} x{len(v)}" for w, v in shared.items()))
    span = [r.get("t", 0.0) for r in records if r.get("t")]
    print("ledger window: " + time.strftime("%d %b %H:%M", time.localtime(min(span)))
          + " -> " + time.strftime("%d %b %H:%M", time.localtime(max(span))))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
