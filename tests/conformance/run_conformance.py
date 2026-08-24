"""Conformance harness (plan Task 12): drive a story through the
Session API with scripted input, return the joined Text transcript.

- `play_session_lines(path, lines, seed)`: feed each line once; stop
  early at EndOfGame (feeding a finished game is a protocol error —
  Session.input returns Error("game over")).
- `play_to_end(path, seed, max_lines)`: feed blank lines until
  EndOfGame or max_lines (self-driving suites: czech, crashme).

Drive scripts per suite (probed against dfrotz -t):
  czech.z5    self-driving; quits on its own ("Last test: quit!")
  crashme.z5  self-driving; quits after "Done." (one blank line for
              its default-filename prompt)
  strictz.z5  asks "make a transcript? (Y/N)" at START (N); then
              ***MORE*** pauses consume lines; "Press any key." at end
  unicode.z5  interactive read_char demo — loops printing the ZSCII
              name of each input char; never quits on its own
  random.z5   interactive menu (1/2/q); "q" quits
"""
from pathlib import Path

from zmach.events import EndOfGame, Text
from zmach.session import Session


def play_session_lines(path, lines, seed=None):
    """Feed `lines` one per batch; return the joined Text data."""
    s = Session()
    out = [e.data for e in s.load(str(path), seed=seed)
           if isinstance(e, Text)]
    for line in lines:
        if s.done:
            break
        evs = s.input(line)
        out += [e.data for e in evs if isinstance(e, Text)]
        if isinstance(evs[-1], EndOfGame):
            break
    return "".join(out)


def play_to_end(path, seed=None, max_lines=500):
    """Feed blank lines until EndOfGame or max_lines."""
    return play_session_lines(Path(path), ["" for _ in range(max_lines)],
                              seed=seed)


if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    lines = [a for a in sys.argv[2:]] or [""] * 50
    print(play_session_lines(Path(p), lines, seed=10))