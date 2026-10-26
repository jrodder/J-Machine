import shutil
import subprocess
import unittest
from pathlib import Path

BANNER_PREFIXES = ("Using normal formatting.", "Loading ", "dfrotz ")

# The reference interpreter is an external binary, not a dependency of the
# host, so a box without it still runs every test that needs no reference.
# Hard-coding the path made 18 tests error (not skip) on the production host.
DFROTZ = shutil.which("dfrotz") or next(
    (p for p in ("/usr/games/dfrotz", "/usr/local/bin/dfrotz")
     if Path(p).exists()), None)


def dfrotz_transcript(story, lines, seed=None):
    """Run dfrotz in plain-text mode with the given input lines + ^D, return stdout."""
    if DFROTZ is None:
        raise unittest.SkipTest("dfrotz (reference interpreter) not installed")
    cmd = [DFROTZ, "-t"]
    if seed is not None:
        cmd += ["-s", str(seed)]
    cmd.append(str(story))
    p = subprocess.run(cmd, input="".join(l + "\n" for l in lines) + "\x04",
                       capture_output=True, text=True, timeout=120)
    return p.stdout


def norm(s):
    """Collapse whitespace, drop dfrotz banner lines and blanks."""
    out = []
    for line in s.splitlines():
        if any(line.startswith(p) for p in BANNER_PREFIXES):
            continue
        line = " ".join(line.split())
        if line:
            out.append(line)
    return "\n".join(out)