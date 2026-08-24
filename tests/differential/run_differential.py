"""Differential walkthrough harness (plan Task 13).

WALK is the hand-transcribed Zork I walkthrough (zork1_walk.txt). Because it
is a transcription, it can legitimately drift from the exact story file in
the corpus (release version, typos). So a full-run byte match is a *report*,
not a *gate*:

    python3 tests/differential/run_differential.py

prints where ours and dfrotz first diverge, with context on both sides, for
manual verification. tests/test_differential.py gates only what must be
engine-true (first 10 commands byte-identical) plus a full-run smoke.

In-game `save` (walkthrough lines 26/68/119): our side uses an in-memory
ZMSAVE handler (spec section 5 host layer) and never prompts. dfrotz does
prompt ("Please enter a filename [zork1.qzl]:" and, if the file exists,
"Overwrite existing file?"), so its stdin stream desyncs from the plain
walkthrough. The harness makes dfrotz deterministic: it runs dfrotz in a
temp cwd where the default save file zork1.qzl already exists, and injects
"" (accept default) + "y" (overwrite) after every `save` line. dfrotz emits
the prompt echoes with no leading newline (they land inline on the ">Ok."
line), so the echoes are stripped by regex from the raw output before norm;
the story's own "Ok." (identical on both sides) remains the only save text.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.conformance.run_conformance import play_session_lines
from tests.util import norm

WALK_PATH = Path(__file__).parent / "zork1_walk.txt"
WALK = [l for l in WALK_PATH.read_text().splitlines() if l.strip()]

STORY = _ROOT / "tests" / "corpus" / "zork1.z3"
# dfrotz's interactive save-prompt echoes, emitted inline (no leading \n)
SAVE_PROMPT_RE = re.compile(
    r"Please enter a filename \[[^\]]*\]:\s*|Overwrite existing file\?\s*")


def _walk_lines(n=None):
    return WALK[:n] if n else WALK


def walk_ours(n=None, seed=10):
    """Play the first n walkthrough commands through our Session."""
    def handlers(s):
        store = {}
        def save(hint):
            store["img"] = s.save()
            return True
        def restore(hint):
            s.restore_image(store["img"])
            return True
        return save, restore
    return norm(play_session_lines(STORY, _walk_lines(n), seed=seed,
                                   handlers=handlers))


def walk_ref(n=None, seed=10):
    lines = _walk_lines(n)
    script = []
    for l in lines:
        script.append(l)
        if l.strip().lower() == "save":
            script += ["", "y"]  # default filename; overwrite (pre-existed)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "zork1.qzl").write_text("")
        p = subprocess.run(
            ["/usr/games/dfrotz", "-t", "-s", str(seed), str(STORY)],
            input="".join(l + "\n" for l in script) + "\x04",
            capture_output=True, text=True, cwd=d, timeout=120)
    # strip dfrotz's inline prompt echoes; the story's own "Ok." stays
    return norm(SAVE_PROMPT_RE.sub("", p.stdout))


def first_divergence_index(a_lines, b_lines):
    """Index of the first differing line, or min(len) if one is a prefix."""
    for i, (x, y) in enumerate(zip(a_lines, b_lines)):
        if x != y:
            return i
    return min(len(a_lines), len(b_lines))


def report(n=None):
    ours, ref = walk_ours(n), walk_ref(n)
    a, b = ours.split("\n"), ref.split("\n")
    i = first_divergence_index(a, b)
    print(f"commands fed: {len(_walk_lines(n))} | transcript lines: "
          f"ours {len(a)}, dfrotz {len(b)} | matching lines: {i}")
    if i < min(len(a), len(b)):
        lo = max(0, i - 3)
        hi = min(max(len(a), len(b)), i + 4)
        print("--- first divergence at transcript line %d ---" % i)
        print("line   ours | dfrotz")
        for k in range(lo, hi):
            ours_k = a[k] if k < len(a) else "<eof>"
            ref_k = b[k] if k < len(b) else "<eof>"
            print(f"{k:4d}  {ours_k} | {ref_k}{'  <-- DIVERGES' if k == i else ''}")
    return i


if __name__ == "__main__":
    report()