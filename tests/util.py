import subprocess
from pathlib import Path

BANNER_PREFIXES = ("Using normal formatting.", "Loading ", "dfrotz ")


def dfrotz_transcript(story, lines, seed=None):
    """Run dfrotz in plain-text mode with the given input lines + ^D, return stdout."""
    cmd = ["/usr/games/dfrotz", "-t"]
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