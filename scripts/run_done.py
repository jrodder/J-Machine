#!/usr/bin/env python3
"""Done-bar runner (plan Task 14, spec section 11).

Runs each gate in order, prints a checklist, exits non-zero on any failure.
Gate 5 (manual smoke) is a reminder, not an automated check.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.util import norm  # noqa: E402

CORPUS = ROOT / "tests" / "corpus"


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    out = p.stdout + p.stderr  # unittest prints failures to stderr
    ran = next((l for l in out.splitlines() if l.startswith("Ran ")), "")
    ok = p.returncode == 0 and "OK" in out
    return ok, (ran + " OK").strip() if ok else "\n".join(out.strip().splitlines()[-3:])


def gate_unit_tests():
    return _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])


def gate_conformance():
    return _run([sys.executable, "-m", "unittest", "tests.test_conformance"])


def gate_faketx():
    return _run([sys.executable, "-m", "unittest", "tests.test_faketx"])


def gate_differential():
    from tests.differential.run_differential import report
    matched, total = report()
    return matched == total, f"{matched}/{total} transcript lines byte-identical"


def gate_save_roundtrip():
    """Spec gate 3: N turns -> save -> restore (fresh session) -> N more
    turns; transcript byte-identical to the uninterrupted run. The
    fresh-session restore is also the Phase 2 reconnect flow."""
    from zmach.events import EndOfGame, Text
    from zmach.session import Session

    story = CORPUS / "planetfall.z5"
    lines = ["look", "open mailbox", "take leaflet", "north", "east",
             "south", "west", "look", "quit"]

    def play(checkpoint=None):
        s = Session()
        parts = [e.data for e in s.load(str(story), seed=10)
                 if isinstance(e, Text)]
        for i, line in enumerate(lines):
            if checkpoint is not None and i == checkpoint:
                img = s.save()
                s = Session()
                s.load(str(story))   # VM scaffold only; intro discarded
                s.restore(img)       # back to checkpoint state
            evs = s.input(line)
            parts += [e.data for e in evs if isinstance(e, Text)]
            if isinstance(evs[-1], EndOfGame):
                break
        return norm("".join(parts))

    a, b = play(), play(checkpoint=4)
    return a == b, ("9-turn transcript identical through save -> "
                    "fresh-session restore (planetfall.z5)")


def main():
    gates = [
        ("1. Conformance", gate_conformance),
        ("2. Differential vs dfrotz", gate_differential),
        ("3. Save round-trip", gate_save_roundtrip),
        ("4. Fake transport", gate_faketx),
        ("all unit tests", gate_unit_tests),
    ]
    results = [(name, fn()) for name, fn in gates]
    failed = 0
    print()
    print("J-Machine done bar (spec section 11)")
    print("-" * 62)
    for name, (ok, detail) in results:
        print(f"{'✓' if ok else '✗'} {name:<26} {detail}")
        failed += (not ok)
    print("? 5. Smoke (manual)            play the corpus games to a known "
          "point in a real terminal")
    print("-" * 62)
    print("PASS" if failed == 0 else f"FAIL ({failed} gate(s))")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())